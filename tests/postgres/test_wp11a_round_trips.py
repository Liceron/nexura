# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""MVP WP11a: fewer database round trips without losing a guarantee.

The Postgres stores now combine statements and run one-statement calls without
``BEGIN``/``COMMIT``. These tests pin what that must not change:

* ``Database.run(autocommit=True)`` runs no transaction, refuses isolation/readonly, and costs
  two round trips fewer (counted with ``RoundTripProxy``);
* the database clock is still read *after* a lock wait (workflow checkpoints, journal leases);
* event numbers stay unique and gap-free when one-statement appends race each other and the
  older lock-then-``MAX`` appenders (an instance not yet upgraded);
* a run's first row and its RUN_STARTED event are written together or not at all;
* the batched ownership lookup and the reference scan answer as the per-key lookups did;
* the one-statement journal read equals the transactional one.

Needs ``NEXURA_TEST_POSTGRES_URI``; each test has a database of its own (``conftest``).
Nothing calls a model or the network.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from nexura_agents.contracts.execution import (
    EffectRecord,
    ExecutionId,
    StepIntent,
    StepOutcome,
)
from nexura_agents.execution_store.base import _Store

from nexura_runtime.service.auth.errors import CrossWorkspaceAccessError
from nexura_runtime.service.workspaces.repository import UNOWNED_WORKSPACE, WorkspaceRepository
from nexura_runtime.service.workspaces.scoping import ResourceScopeEnforcer
from nexura_runtime.testing.pg_wire import RoundTripProxy
from nexura_runtime.workflow import WorkflowCheckpoint, WorkflowSpec


@pytest.fixture
def url(service_database_url: str) -> str:
    return service_database_url


@pytest.fixture
def proxy(url: str) -> Iterator[RoundTripProxy]:
    parts = urlsplit(url)
    with RoundTripProxy(parts.hostname or "127.0.0.1", parts.port or 5432) as wire:
        yield wire


def _database(url: str) -> Any:
    from nexura_postgres import Database

    return Database(url)


# ----------------------------------------------------------------------------- Database


def test_autocommit_runs_no_transaction_and_saves_two_round_trips(
    url: str, proxy: RoundTripProxy
) -> None:
    db = _database(proxy.proxied_url(url))
    try:

        async def probe(conn: Any) -> tuple[bool, int]:
            return conn.is_in_transaction(), await conn.fetchval("SELECT $1::int", 7)

        assert db.run(probe) == (True, 7)  # opens the pool
        proxy.reset()
        assert db.run(probe) == (True, 7)
        default = proxy.snapshot()
        proxy.reset()
        assert db.run(probe, autocommit=True) == (False, 7)
        single = proxy.snapshot()
        assert default.count("BEGIN") == 1 and default.count("COMMIT") == 1
        assert single.count("BEGIN") == 0 and single.count("COMMIT") == 0
        # BEGIN + prepare + execute + COMMIT, against prepare + execute.
        assert (default.query_round_trips, single.query_round_trips) == (4, 2)
        assert asyncio.run(db.arun(probe, autocommit=True)) == (False, 7)
        for options in ({"isolation": "serializable"}, {"readonly": True}):
            with pytest.raises(ValueError, match="autocommit"):
                db.run(probe, autocommit=True, **options)
    finally:
        db.close()


def test_proxy_counts_setup_and_query_round_trips(url: str, proxy: RoundTripProxy) -> None:
    db = _database(proxy.proxied_url(url))
    try:

        async def one(conn: Any) -> Any:
            return await conn.fetchval("SELECT 1")

        db.run(one)
        first = proxy.snapshot()
        assert first.connections == 1 and first.setup_round_trips >= 2
        assert first.count("SELECT 1") == 1
    finally:
        db.close()


def test_proxy_delay_adds_latency_per_round_trip(url: str) -> None:
    parts = urlsplit(url)
    with RoundTripProxy(parts.hostname or "127.0.0.1", parts.port or 5432, delay=0.05) as wire:
        db = _database(wire.proxied_url(url))
        try:

            async def one(conn: Any) -> Any:
                return await conn.fetchval("SELECT $1::int", 1)

            db.run(one)
            started = time.monotonic()
            db.run(one)
            elapsed = time.monotonic() - started
            # Four round trips at 50 ms each.
            assert elapsed >= 0.19, elapsed
        finally:
            db.close()


# ----------------------------------------------------------------------------- lock, then clock


async def _hold_lock(url: str, sql: str, arg: str, held: threading.Event, seconds: float) -> None:
    import asyncpg  # type: ignore[import-untyped]

    conn = await asyncpg.connect(url)
    try:
        async with conn.transaction():
            await conn.execute(sql, arg)
            held.set()
            await asyncio.sleep(seconds)
    finally:
        await conn.close()


def _in_thread(coro: Any) -> threading.Thread:
    thread = threading.Thread(target=asyncio.run, args=(coro,))
    thread.start()
    return thread


async def test_checkpoint_time_is_read_after_the_run_lock(url: str) -> None:
    from nexura_postgres import PostgresWorkflowStore

    store = PostgresWorkflowStore(url)
    try:
        cp = WorkflowCheckpoint(
            workflow_run_id="r1",
            spec_version=1,
            spec_hash="h",
            current_node_id="a",
            state={},
            step_count=0,
            status="running",
            workspace_id="ws",
        )
        assert await store.create(cp)
        held = threading.Event()
        holder = _in_thread(
            _hold_lock(
                url,
                "SELECT pg_advisory_xact_lock(hashtextextended('nexura-workflow/' || $1, 0))",
                "r1",
                held,
                0.6,
            )
        )
        assert await asyncio.to_thread(held.wait, 10)
        asked = time.time()
        cp.step_count = 1
        await store.save(cp)
        await asyncio.to_thread(holder.join, 10)
        saved = await store.get("r1")
        assert saved is not None and saved.step_count == 1
        # The clock was read once the lock was granted, not when the save started.
        assert saved.updated_at >= asked + 0.4, (saved.updated_at, asked)
    finally:
        await store.close()


async def test_checkpoint_create_refuses_a_taken_id_in_any_workspace(url: str) -> None:
    from nexura_postgres import PostgresWorkflowStore

    other = PostgresWorkflowStore(url, workspace_id="other")
    mine = PostgresWorkflowStore(url, workspace_id="ws")
    try:

        def cp(ws: str) -> WorkflowCheckpoint:
            return WorkflowCheckpoint(
                workflow_run_id="taken",
                spec_version=1,
                spec_hash="h",
                current_node_id="a",
                state={"by": ws},
                step_count=0,
                status="running",
                workspace_id=ws,
            )

        assert await other.create(cp("other")) is True
        history = len(await other.history("taken"))
        assert await mine.create(cp("ws")) is False
        assert await other.create(cp("other")) is False
        kept = await other.get("taken")
        assert kept is not None and kept.state == {"by": "other"} and kept.revision == 1
        assert len(await other.history("taken")) == history
    finally:
        await other.close()
        await mine.close()


async def test_journal_lease_time_is_read_after_the_row_lock(url: str) -> None:
    from nexura_postgres import PostgresWorkflowStore

    store = PostgresWorkflowStore(url)
    journal = await store.open_execution_store()
    try:
        eid = ExecutionId("ws", "j1")
        await journal.create(eid, owner="a", definition_digest="d", ttl=0.01, kind="workflow")
        await asyncio.sleep(0.05)  # the first lease has expired
        held = threading.Event()
        holder = _in_thread(
            _hold_lock(
                url,
                "SELECT 1 FROM nexura_exec_runs WHERE run_id = $1 FOR UPDATE",
                "j1",
                held,
                0.6,
            )
        )
        assert await asyncio.to_thread(held.wait, 10)
        asked = time.time()
        lease = await journal.claim(eid, "b", 30.0, definition_digest="d")
        await asyncio.to_thread(holder.join, 10)
        assert lease is not None
        snapshot = await journal.get(eid)
        assert snapshot.run.lease_expires_at >= asked + 0.4 + 30.0
    finally:
        await store.close()


async def test_one_statement_journal_read_equals_the_transactional_read(url: str) -> None:
    from nexura_postgres import PostgresWorkflowStore

    store = PostgresWorkflowStore(url)
    journal = await store.open_execution_store()
    try:
        eid = ExecutionId("ws", "j2")
        await journal.create(eid, owner="a", definition_digest="d", ttl=30.0, kind="workflow")
        lease = await journal.claim(eid, "a", 30.0, definition_digest="d")
        assert lease is not None
        intent = StepIntent(step_key="a#1", payload_digest="p", idempotency_key="k1")
        await journal.commit(
            lease,
            state={"n": 1},
            cursor="b",
            steps=[intent],
            effects=[EffectRecord(execution_id=eid, intent=intent)],
            events=[{"type": "one", "x": 0.1 + 0.2}],
        )
        await journal.commit(
            lease,
            state={"n": 2},
            cursor="c",
            steps=[StepOutcome(step_key="a#1", status="completed", output={"v": 1.5})],
            events=[{"type": "two"}, {"type": "three"}],
        )
        # More than nine events: the order must be numeric (10 after 9), not textual.
        await journal.commit(
            lease, state={"n": 2}, cursor="c", events=[{"type": f"more{i}"} for i in range(9)]
        )
        fast = await journal.get(eid)
        slow = await _Store.get(journal, eid)  # the transactional read through _edit
        assert fast == slow
        assert [e.sequence for e in fast.events] == list(range(1, 13))
        assert fast.events[0].payload["x"] == 0.1 + 0.2
        assert fast.steps["a#1"].outcome is not None and fast.run.state == {"n": 2}
        assert len(fast.effects) == 1
        with pytest.raises(KeyError):
            await journal.get(ExecutionId("ws", "missing"))
        # A stale fence writes nothing, not even the journal rows.
        from nexura_agents.contracts.execution import LeaseLostError

        stale = lease
        await journal.claim(eid, "z", 30.0, definition_digest="d")  # still a's: refused
        await journal.release(lease)
        assert await journal.claim(eid, "z", 30.0, definition_digest="d") is not None
        with pytest.raises(LeaseLostError):
            await journal.commit(stale, state={"n": 9}, events=[{"type": "lost"}])
        after = await journal.get(eid)
        assert after.run.state == {"n": 2} and [e.event_type for e in after.events][-1] != "lost"
    finally:
        await store.close()


# ----------------------------------------------------------------------------- run events


def _spec() -> WorkflowSpec:
    return WorkflowSpec.from_dict(
        {
            "name": "wp11a",
            "entry_node": "a",
            "terminal_nodes": ["a"],
            "nodes": {"a": {"id": "a", "handler": "synthetic_order_lookup"}},
        }
    )


class _NoCheckpoints:
    async def close(self) -> None:
        return None

    def close_sync(self) -> None:
        return None


def _studio(url: str, tmp_path: Path) -> tuple[Any, Any]:
    from nexura_postgres import PostgresStudioStore

    database = _database(url)
    return (
        PostgresStudioStore(
            database, checkpoint_store=_NoCheckpoints(), db_path=tmp_path / "unused.sqlite3"
        ),
        database,
    )


def test_new_and_old_event_appenders_keep_numbers_unique_and_gap_free(
    url: str, tmp_path: Path
) -> None:
    """One-statement appends race appenders that lock and then read MAX in a later
    statement (the pre-WP11a code, still run by an instance not yet upgraded)."""
    from nexura_postgres.studio import RUN_LOCK_SQL, append_event

    store, database = _studio(url, tmp_path)
    old_db = _database(url)
    try:
        store._record_run_start_sync("mixed", None, _spec(), {})
        per_thread = 12
        barrier = threading.Barrier(6)
        errors: list[BaseException] = []

        def new_writer(name: str) -> None:
            try:
                barrier.wait(timeout=30)
                for i in range(per_thread):
                    store._record_run_event_sync("mixed", "NEW", data={"w": name, "i": i})
            except BaseException as exc:
                errors.append(exc)

        def old_writer(name: str) -> None:
            async def append(conn: Any, i: int) -> None:
                await conn.execute(RUN_LOCK_SQL, "mixed")
                await append_event(conn, "mixed", "OLD", None, f'{{"w":"{name}","i":{i}}}', 1.0)

            try:
                barrier.wait(timeout=30)
                for i in range(per_thread):
                    old_db.run(lambda conn, i=i: append(conn, i))
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=new_writer, args=(f"n{k}",)) for k in range(4)]
        threads += [threading.Thread(target=old_writer, args=(f"o{k}",)) for k in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120)
        assert not errors, errors
        run = store._get_run_sync("mixed")
        assert [e["seq"] for e in run["events"]] == list(range(1, 6 * per_thread + 2))
        assert run["events"][0]["type"] == "RUN_STARTED"
    finally:
        store._close_sync()
        database.close()
        old_db.close()


def test_run_start_writes_the_row_and_its_first_event_together(url: str, tmp_path: Path) -> None:
    store, database = _studio(url, tmp_path)
    try:
        store._record_run_start_sync("r-start", None, _spec(), {"q": 1})
        with pytest.raises(Exception, match="duplicate key|unique"):
            store._record_run_start_sync("r-start", None, _spec(), {"q": 2})

        async def count(conn: Any) -> Any:
            return await conn.fetchval(
                "SELECT count(*) FROM nexura_studio_run_events WHERE run_id = 'r-start'"
            )

        assert database.run(count) == 1  # the refused start added no second RUN_STARTED
        run = store._get_run_sync("r-start")
        assert run["input_state"] == {"q": 1} and run["events"][0]["seq"] == 1
        # An event for a run that does not exist is not recorded.
        store._record_run_event_sync("r-none", "TICK")

        async def orphans(conn: Any) -> Any:
            return await conn.fetchval(
                "SELECT count(*) FROM nexura_studio_run_events WHERE run_id = 'r-none'"
            )

        assert database.run(orphans) == 0
    finally:
        store._close_sync()
        database.close()


def test_run_read_keeps_exact_event_timestamps(url: str, tmp_path: Path) -> None:
    store, database = _studio(url, tmp_path)
    try:
        store._record_run_start_sync("r-ts", None, _spec(), {})
        stamp = 1761000000.1234567

        async def put(conn: Any) -> None:
            await conn.execute(
                "UPDATE nexura_studio_run_events SET timestamp = $1 WHERE run_id = 'r-ts'", stamp
            )

        database.run(put)
        run = store._get_run_sync("r-ts")
        assert run["events"][0]["timestamp"] == stamp
        assert store._get_run_sync("r-missing") is None
    finally:
        store._close_sync()
        database.close()


# ----------------------------------------------------------------------------- ownership


def test_batched_ownership_and_scan_answer_like_single_lookups(
    url: str, proxy: RoundTripProxy, tmp_path: Path
) -> None:
    db = _database(proxy.proxied_url(url))
    try:
        repo = WorkspaceRepository()
        repo.bind_database(db)
        repo.fail_closed_unowned = True
        repo.record_resource_ownership("module", "mod-b", "B", "bob")
        repo.record_resource_ownership("credential", "cred-a", "A", "alice")
        keys = [
            ("module", "mod-b"),
            ("credential", "cred-a"),
            ("workflow", "wf-none"),
            ("integration_tool", "mod-b"),
            ("module", "nul\x00id"),
        ]
        batch = repo.get_resource_workspaces(keys)
        assert batch == {k: repo.get_resource_workspace(*k) for k in keys[:4]}
        assert batch[("workflow", "wf-none")] == UNOWNED_WORKSPACE

        spec = {
            "nodes": {
                "a": {"handler": "own-mod"},
                "b": {"handler": "mod-b"},
                "c": {"credential": "cred-a"},
            }
        }
        enforcer = ResourceScopeEnforcer(repo)
        proxy.reset()
        with pytest.raises(CrossWorkspaceAccessError, match="module 'mod-b'.*workspace 'B'"):
            enforcer.scan_and_validate_references("A", spec)
        # One lookup query for six (type, id) keys, instead of one per key.
        assert proxy.snapshot().query_round_trips == 2
        enforcer.scan_and_validate_references("B", {"handler": "mod-b"})
        with pytest.raises(CrossWorkspaceAccessError, match="credential 'cred-a'"):
            enforcer.scan_and_validate_references("B", spec)
    finally:
        db.close()


# ----------------------------------------------------------------------------- per-request member


def test_a_remembered_membership_is_reused_only_for_its_own_workspace_and_principal() -> None:
    """The authentication read left on ``request.state`` answers only the same (workspace,
    principal); any other question reads the store, and ``forget_member`` drops it."""
    from types import SimpleNamespace

    from nexura_runtime.service.workspaces.membership import (
        forget_member,
        remember_member,
        remembered_member,
    )
    from nexura_runtime.service.workspaces.models import WorkspaceMember

    state = SimpleNamespace()
    assert remembered_member(state, "A", "alice") == (False, None)
    assert remembered_member(None, "A", "alice") == (False, None)
    member = WorkspaceMember(workspace_id="A", principal_id="alice", role="viewer")
    remember_member(state, "A", "alice", member)
    assert remembered_member(state, "A", "alice") == (True, member)
    assert remembered_member(state, "B", "alice") == (False, None)
    assert remembered_member(state, "A", "bob") == (False, None)
    remember_member(state, "A", "carol", None)  # "not a member" is remembered too
    assert remembered_member(state, "A", "carol") == (True, None)
    forget_member(state)
    assert remembered_member(state, "A", "carol") == (False, None)
    forget_member(state)  # nothing to forget: no error
