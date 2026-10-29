# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Studio run store contract on both backends (MVP Postgres WP3, store #1).

Every parametrised test runs the same assertions against the SQLite ``StudioStore`` and the
Postgres ``PostgresStudioStore`` (skipped without ``NEXURA_TEST_POSTGRES_URI``): workflows, run
records, the event stream, compare-and-set updates, listings, the open-time sweep of legacy
runs and blob offloading. The Postgres-only tests at the end cover what one SQLite file cannot
show: two instances (separate pools) appending events to one run at once get unique, gap-free
``event_seq`` values, a run read is one snapshot, and the column types the blob scan needs.

Everything is local: no model, no network beyond the test database.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest
from nexura_agents.errors import WorkflowConflictError, WorkflowValidationError

from nexura_runtime.blobs import BlobPayloads, LocalBlobStore
from nexura_runtime.blobs.payloads import MARKER_PREFIX, DeferredPayload
from nexura_runtime.studio import store as store_module
from nexura_runtime.studio.store import StudioStore
from nexura_runtime.workflow import PendingApproval, WorkflowSpec

Opener = Callable[..., StudioStore]
BIG = "x" * 20_000


def _spec(name: str = "contract") -> WorkflowSpec:
    return WorkflowSpec.from_dict(
        {
            "name": name,
            "entry_node": "a",
            "terminal_nodes": ["a"],
            "nodes": {"a": {"id": "a", "handler": "synthetic_order_lookup"}},
        }
    )


class _NoCheckpoints:
    """The Postgres store needs a checkpoint store object; these tests never use it."""

    async def close(self) -> None:
        return None

    def close_sync(self) -> None:
        return None


@pytest.fixture
async def open_store(
    backend: str, tmp_path: Path, request: pytest.FixtureRequest
) -> AsyncIterator[Opener]:
    """Open a store on this test's storage; each call is a new "instance" (process)."""
    opened: list[StudioStore] = []
    databases: list[Any] = []
    if backend == "sqlite":

        def open_sqlite(**kwargs: Any) -> StudioStore:
            store = StudioStore(tmp_path / "studio.sqlite3", **kwargs)
            opened.append(store)
            return store

        opener: Opener = open_sqlite
    else:
        from nexura_postgres import Database, PostgresStudioStore

        url = request.getfixturevalue("service_database_url")

        def open_postgres(**kwargs: Any) -> StudioStore:
            # A pool of its own per call, as another service instance has.
            database = Database(url)
            databases.append(database)
            store = PostgresStudioStore(
                database,
                checkpoint_store=_NoCheckpoints(),
                db_path=tmp_path / "unused.sqlite3",
                **kwargs,
            )
            opened.append(store)
            return store

        opener = open_postgres
    try:
        yield opener
    finally:
        for store in opened:
            await store.close()
        for database in databases:
            database.close()
    if backend == "postgres":
        assert not (tmp_path / "unused.sqlite3").exists()  # nothing touches SQLite


async def _start(store: StudioStore, run_id: str, **kwargs: Any) -> None:
    await store.record_run_start(run_id, kwargs.pop("workflow_id", None), _spec(), {}, **kwargs)


# ----------------------------------------------------------------------------- workflows


async def test_workflows_seed_create_update_delete(open_store: Opener) -> None:
    store = open_store()
    listed = await store.list_workflows()
    assert [w["workflow_id"] for w in listed] == [store_module.DEFAULT_WORKFLOW_ID]
    open_store()  # a second open never seeds twice
    assert len(await store.list_workflows()) == 1

    created = await store.create_workflow(_spec("one"), workflow_id="wf-one")
    assert created["revision"] == 1 and created["name"] == "one"
    with pytest.raises(WorkflowValidationError, match="already exists"):
        await store.create_workflow(_spec("dup"), workflow_id="wf-one")
    got = await store.get_workflow("wf-one")
    assert got is not None and got["spec"] == _spec("one").to_dict()
    assert got["spec_hash"] == _spec("one").canonical_hash()

    updated = await store.update_workflow("wf-one", _spec("two"), expected_revision=1)
    assert updated["revision"] == 2 and updated["created_at"] == created["created_at"]
    with pytest.raises(WorkflowConflictError):
        await store.update_workflow("wf-one", _spec("three"), expected_revision=1)
    with pytest.raises(WorkflowValidationError, match="not found"):
        await store.update_workflow("wf-none", _spec("x"), expected_revision=1)
    summary = {w["workflow_id"]: w for w in await store.list_workflows()}["wf-one"]
    assert summary["revision"] == 2 and summary["node_count"] == 1
    assert summary["entry_node"] == "a" and summary["name"] == "two"

    assert await store.delete_workflow("wf-one") is True
    assert await store.delete_workflow("wf-one") is False
    assert await store.get_workflow("wf-one") is None
    generated = await store.create_workflow(_spec("gen"))
    assert generated["workflow_id"].startswith("wf-")


# ------------------------------------------------------------------------- runs, events


async def test_run_lifecycle_events_and_fields(open_store: Opener) -> None:
    store = open_store()
    await store.record_run_start(
        "run-1",
        "wf-x",
        _spec(),
        {"q": "é"},
        forked_from={"run_id": "run-0"},
        started_by={"principal_id": "p", "api_key_id": "key-1"},
        journal_workspace="ws",
    )
    for i in range(3):
        await store.record_run_event("run-1", "NODE_DONE", node_id="a", data={"i": i})
    run = await store.get_run("run-1")
    assert run is not None
    assert [e["seq"] for e in run["events"]] == [1, 2, 3, 4]
    assert [e["type"] for e in run["events"]] == ["RUN_STARTED"] + ["NODE_DONE"] * 3
    assert run["events"][0]["data"] == {"workflow_id": "wf-x", "initial_state": {"q": "é"}}
    assert run["events"][3]["data"] == {"i": 2} and run["events"][3]["node_id"] == "a"
    assert run["status"] == "running" and run["current_node_id"] == "a"
    assert run["input_state"] == {"q": "é"} and run["current_state"] == {"q": "é"}
    assert run["forked_from"] == {"run_id": "run-0"} and run["spec"] == _spec().to_dict()
    assert run["pending_approval"] is None and run["pause_reason"] is None
    assert await store.get_run_status("run-1") == "running"
    assert await store.get_run_status("nope") is None
    assert await store.get_run("nope") is None

    approval = PendingApproval(
        approval_id="ap-1", workflow_run_id="run-1", node_id="a", input_state={"x": 1}
    )
    assert await store.update_run(
        "run-1", "pending_approval", "a", {"k": 1}, 2, pending_approval=approval
    )
    paused = await store.get_run("run-1")
    assert paused is not None and paused["pending_approval"]["approval_id"] == "ap-1"
    assert paused["step_count"] == 2 and paused["current_state"] == {"k": 1}

    # cancel_reason is kept once set; unless_status makes the write a compare-and-set.
    assert await store.update_run("run-1", "cancelled", "a", {}, 2, cancel_reason="revoked")
    assert not await store.update_run(
        "run-1", "completed", "a", {}, 3, unless_status=("cancelled", "failed")
    )
    assert await store.update_run("run-1", "cancelled", "a", {}, 3, unless_status=("failed",))
    final = await store.get_run("run-1")
    assert final is not None and final["status"] == "cancelled"
    assert final["cancel_reason"] == "revoked" and final["step_count"] == 3
    assert await store.update_run("missing", "running", None, {}, 0) is False

    effect = {"effect_id": "e1", "step": "a"}
    await store.record_run_start("run-2", None, _spec(), {})
    assert await store.update_run("run-2", "paused", "a", {}, 1, pending_effect=effect)
    run2 = await store.get_run("run-2")
    assert run2 is not None and run2["pending_effect"] == effect
    assert run2["pause_reason"] == "effect_unknown"
    listed = {r["run_id"]: r for r in await store.list_runs()}
    assert listed["run-2"]["pause_reason"] == "effect_unknown"
    assert listed["run-1"]["cancel_reason"] == "revoked"
    assert await store.list_run_ids_started_by_key("key-1") == ["run-1"]
    assert await store.list_run_ids_started_by_key("key-2") == []


async def test_event_for_a_missing_run_is_not_recorded(
    open_store: Opener, caplog: pytest.LogCaptureFixture
) -> None:
    store = open_store()
    with caplog.at_level(logging.WARNING):
        await store.record_run_event("ghost", "NODE_DONE", data={"x": 1})
    assert "parent run ghost does not exist" in caplog.text
    assert await store.get_run("ghost") is None


async def test_listing_order_filters_and_pages(
    open_store: Opener, monkeypatch: pytest.MonkeyPatch
) -> None:
    import types

    import nexura_postgres.studio as pg_studio

    store = open_store()
    # Equal created_at values tie-break on run_id, in byte order (upper case first).
    frozen = types.SimpleNamespace(time=lambda: 1_700_000_000.0)
    with monkeypatch.context() as patched:
        patched.setattr(store_module, "time", frozen)
        patched.setattr(pg_studio, "time", frozen)
        for run_id in ("b", "B", "a", "c"):
            await _start(store, run_id, workflow_id="wf-1" if run_id != "c" else "wf-2")
    await _start(store, "z-new")
    await store.update_run("a", "completed", None, {}, 1)

    assert [r["run_id"] for r in await store.list_runs()] == ["z-new", "c", "b", "a", "B"]
    assert [r["run_id"] for r in await store.list_runs(limit=2, offset=1)] == ["c", "b"]
    assert [r["run_id"] for r in await store.list_runs(limit=None, offset=3)] == ["a", "B"]
    assert [r["run_id"] for r in await store.list_runs(offset=-5, limit=1)] == ["z-new"]
    assert [r["run_id"] for r in await store.list_runs(workflow_id="wf-1")] == ["b", "a", "B"]
    assert [r["run_id"] for r in await store.list_runs(status="completed")] == ["a"]
    assert [r["run_id"] for r in await store.list_runs(run_ids=["B", "c", "zz"])] == ["c", "B"]
    assert await store.list_runs(run_ids=[]) == []
    assert await store.count_runs() == 5
    assert await store.count_runs(workflow_id="wf-1", status="running") == 2
    assert await store.count_runs(run_ids=["a", "b", "nope"]) == 2
    assert await store.count_runs(run_ids=[]) == 0
    # Quotes and JSON-looking ids are plain values (no json_each or string building).
    assert await store.count_runs(run_ids=['"a"', "a' OR '1'='1", "[]"]) == 0


async def test_journaled_runs_and_mark_interrupted(open_store: Opener) -> None:
    store = open_store()
    await _start(store, "j1", journal_workspace="ws1")
    await _start(store, "j2", journal_workspace="ws2")
    await _start(store, "legacy")
    await store.update_run("j2", "interrupted", None, {}, 0)
    rows = await store.list_journaled_runs()
    assert {r["run_id"]: r["status"] for r in rows} == {"j1": "running", "j2": "interrupted"}
    assert all(r["owner_process"] == store.this_owner() for r in rows)
    assert [r["run_id"] for r in await store.list_journaled_runs(("running",))] == ["j1"]
    only = await store.list_journaled_runs(workspace_id="ws2")
    assert [r["run_id"] for r in only] == ["j2"]
    # Postgres rows also carry updated_at (run liveness across instances, review M1).
    listed = await store.list_journaled_runs(run_ids=["legacy", "j1"])
    assert [{k: v for k, v in r.items() if k != "updated_at"} for r in listed] == [
        {
            "run_id": "j1",
            "status": "running",
            "journal_workspace": "ws1",
            "owner_process": store.this_owner(),
        }
    ]

    assert await store.mark_interrupted("j1", error="lease expired", reason="gone") is True
    assert await store.mark_interrupted("j1", error="again", reason="again") is False
    run = await store.get_run("j1")
    assert run is not None and run["status"] == "interrupted" and run["error"] == "lease expired"
    assert [(e["seq"], e["type"]) for e in run["events"]] == [
        (1, "RUN_STARTED"),
        (2, "RUN_INTERRUPTED"),
    ]
    assert run["events"][1]["data"] == {"reason": "gone"}


async def test_open_sweep_interrupts_only_dead_legacy_runs(
    open_store: Opener, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = open_store()
    for run_id in ("dead", "alive", "foreign", "journaled", "no-owner"):
        await _start(store, run_id, journal_workspace="ws" if run_id == "journaled" else None)
    gone = _dead_owner(store)
    owners = {
        "dead": gone,
        "alive": store.this_owner(),
        "foreign": "another-host:1:x",
        "journaled": gone,
        "no-owner": None,
    }
    _set_owners(store, owners)
    open_store()  # another process opening the store sweeps
    statuses = {r: await store.get_run_status(r) for r in owners}
    assert statuses == {
        "dead": "interrupted",
        "alive": "running",
        "foreign": "running",
        "journaled": "running",
        "no-owner": "interrupted",
    }
    dead = await store.get_run("dead")
    assert dead is not None and dead["error"] == store_module.SWEEP_ERROR
    assert [e["type"] for e in dead["events"]] == ["RUN_STARTED", "RUN_INTERRUPTED"]
    assert dead["events"][1]["data"] == {"reason": store_module.SWEEP_REASON}
    open_store()  # sweeping again changes nothing
    again = await store.get_run("dead")
    assert again is not None and len(again["events"]) == 2


def _dead_owner(store: StudioStore) -> str:
    """An owner whose process (pid 999999999, this host) is gone, in the backend's format."""
    if getattr(store, "database", None) is None:
        return f"{store_module._host_id()}:999999999:x"  # B46: host:pid:marker
    from nexura_agents.durable import _host_identity

    return f"nx1.{_host_identity()}.999999999.0.{'0' * 32}"  # lease-owner format (review M1)


def _set_owners(store: StudioStore, owners: dict[str, str | None]) -> None:
    """Rewrite ``owner_process`` directly in the backend's table (test set-up only)."""
    database = getattr(store, "database", None)
    if database is None:
        conn = store._ensure_open()
        with store._lock, conn:
            for run_id, owner in owners.items():
                conn.execute(
                    "UPDATE nexura_studio_runs SET owner_process = ? WHERE run_id = ?",
                    (owner, run_id),
                )
        return

    async def update(conn: Any) -> None:
        for run_id, owner in owners.items():
            await conn.execute(
                "UPDATE nexura_studio_runs SET owner_process = $1 WHERE run_id = $2",
                owner,
                run_id,
            )

    database.run(update)


async def test_large_payloads_go_to_the_blob_store(open_store: Opener, tmp_path: Path) -> None:
    codec = BlobPayloads(LocalBlobStore(tmp_path / "blobs"), 1024)
    store = open_store(blob_payloads=codec)
    await store.record_run_start("r", None, _spec(), {"doc": BIG}, journal_workspace="ws")
    await store.record_run_event("r", "NODE_DONE", data={"out": BIG})
    await store.record_run_event("r", "NODE_DONE", data={"small": 1})
    raw = _raw_events(store, "r")
    assert raw[0].startswith(MARKER_PREFIX) and raw[1].startswith(MARKER_PREFIX)
    assert json.loads(raw[2]) == {"small": 1}
    assert [info.key.split("/")[:2] for info in codec.store.list()] == [["ws", "run-events"]] * 2

    full = await store.get_run("r")
    assert full is not None
    assert full["events"][0]["data"]["initial_state"] == {"doc": BIG}
    assert full["events"][1]["data"] == {"out": BIG}
    deferred = await store.get_run("r", payloads=False)
    assert deferred is not None
    assert isinstance(deferred["events"][1]["data"], DeferredPayload)
    assert deferred["events"][2]["data"] == {"small": 1}
    loaded = await store.load_payloads(deferred, after_seq=1)
    assert isinstance(loaded["events"][0]["data"], DeferredPayload)
    assert loaded["events"][1]["data"] == {"out": BIG}
    partial = await store.get_run("r", payloads_after_seq=2)
    assert partial is not None and isinstance(partial["events"][1]["data"], DeferredPayload)


def _raw_events(store: StudioStore, run_id: str) -> list[str]:
    database = getattr(store, "database", None)
    if database is None:
        conn = store._ensure_open()
        with store._lock:
            rows = conn.execute(
                "SELECT data_json FROM nexura_studio_run_events WHERE run_id=? ORDER BY event_seq",
                (run_id,),
            ).fetchall()
        return [row[0] for row in rows]

    async def fetch(conn: Any) -> Any:
        return await conn.fetch(
            "SELECT data_json FROM nexura_studio_run_events WHERE run_id=$1 ORDER BY event_seq",
            run_id,
        )

    return [row["data_json"] for row in database.run(fetch)]


async def test_a_duplicate_run_id_is_refused_and_writes_nothing(open_store: Opener) -> None:
    store = open_store()
    await _start(store, "dup")
    with pytest.raises(Exception) as refused:
        await _start(store, "dup")
    assert "unique" in str(refused.value).lower() or "UNIQUE" in str(refused.value)
    run = await store.get_run("dup")
    assert run is not None and len(run["events"]) == 1


# --------------------------------------------------------------------- Postgres only


@pytest.fixture
def postgres_url(request: pytest.FixtureRequest) -> str:
    return str(request.getfixturevalue("service_database_url"))


def _pg_store(url: str, tmp_path: Path, **kwargs: Any) -> tuple[Any, Any]:
    from nexura_postgres import Database, PostgresStudioStore

    database = Database(url)
    return (
        PostgresStudioStore(
            database, checkpoint_store=_NoCheckpoints(), db_path=tmp_path / "x.sqlite3", **kwargs
        ),
        database,
    )


def test_two_instances_append_unique_gap_free_event_seq(postgres_url: str, tmp_path: Path) -> None:
    """Two instances (separate pools), 4 writer threads each, one run: 1..N with no gap."""
    a, db_a = _pg_store(postgres_url, tmp_path)
    b, db_b = _pg_store(postgres_url, tmp_path)
    try:
        a._record_run_start_sync("shared", None, _spec(), {})
        per_thread = 15
        barrier = threading.Barrier(8)
        errors: list[BaseException] = []

        def writer(store: Any, name: str) -> None:
            try:
                barrier.wait(timeout=30)
                for i in range(per_thread):
                    store._record_run_event_sync("shared", "TICK", data={"w": name, "i": i})
            except BaseException as exc:  # reported below
                errors.append(exc)

        threads = [
            threading.Thread(target=writer, args=(a if n % 2 else b, f"t{n}")) for n in range(8)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120)
        assert not errors, errors
        run = b._get_run_sync("shared")
        seqs = [event["seq"] for event in run["events"]]
        assert seqs == list(range(1, 8 * per_thread + 2))
        # Each writer's own events keep their order.
        for n in range(8):
            mine = [e["data"]["i"] for e in run["events"] if e["data"].get("w") == f"t{n}"]
            assert mine == list(range(per_thread))
    finally:
        a._close_connection()
        b._close_connection()
        db_a.close()
        db_b.close()


def test_run_read_is_one_snapshot(
    postgres_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run row and its events come from one snapshot (REPEATABLE READ READ ONLY).

    The run row query is slowed down (``pg_sleep`` in its select list); while it runs another
    instance appends an event and finishes the run. A READ COMMITTED read would return the old
    row with the new event.
    """
    import nexura_postgres.studio as pg_studio

    reader, db_r = _pg_store(postgres_url, tmp_path)
    writer, db_w = _pg_store(postgres_url, tmp_path)
    try:
        writer._record_run_start_sync("snap", None, _spec(), {})
        monkeypatch.setattr(
            pg_studio, "RUN_DETAIL_COLUMNS", pg_studio.RUN_DETAIL_COLUMNS + ", pg_sleep(1.0)"
        )

        def without_sleep(row: Any, events: Any, raw: bool) -> Any:
            return store_module.run_detail(tuple(row)[:-1], events, raw)

        monkeypatch.setattr(pg_studio, "run_detail", without_sleep)
        result: dict[str, Any] = {}
        thread = threading.Thread(target=lambda: result.update(run=reader._get_run_sync("snap")))
        thread.start()
        import time

        time.sleep(0.3)  # the reader's first statement (and so its snapshot) has started
        writer._record_run_event_sync("snap", "RUN_COMPLETED", data={})
        writer._update_run_sync("snap", "completed", None, {}, 1)
        thread.join(timeout=30)
        run = result["run"]
        assert run["status"] == "running"  # the row as of the snapshot
        assert [e["type"] for e in run["events"]] == ["RUN_STARTED"]  # same snapshot
        monkeypatch.undo()
        now = reader._get_run_sync("snap")
        assert now["status"] == "completed" and len(now["events"]) == 2
    finally:
        reader._close_connection()
        writer._close_connection()
        db_r.close()
        db_w.close()


def test_postgres_schema_keeps_names_types_and_the_blob_scan(
    postgres_url: str, tmp_path: Path
) -> None:
    from nexura_postgres import referenced_blob_keys

    codec = BlobPayloads(LocalBlobStore(tmp_path / "blobs"), 1024)
    store, database = _pg_store(postgres_url, tmp_path, blob_payloads=codec)
    try:
        store._record_run_start_sync("r", None, _spec(), {"doc": BIG}, journal_workspace="ws")

        async def columns(conn: Any) -> Any:
            return await conn.fetch(
                "SELECT table_name, column_name, data_type, is_identity "
                "FROM information_schema.columns WHERE table_schema = current_schema() "
                "AND table_name LIKE 'nexura_studio_%'"
            )

        found = {(r["table_name"], r["column_name"]): r for r in database.run(columns)}
        assert found[("nexura_studio_run_events", "data_json")]["data_type"] == "text"
        assert found[("nexura_studio_runs", "status")]["data_type"] == "text"
        assert found[("nexura_studio_run_events", "id")]["is_identity"] == "YES"
        assert found[("nexura_studio_runs", "started_by_json")]["data_type"] == "text"
        scan = database.run(referenced_blob_keys)
        assert scan.keys == {info.key for info in codec.store.list()}
        assert "nexura_studio_run_events" in scan.tables
        assert scan.protected == scan.keys  # the run is still running: never expired
    finally:
        store._close_connection()
        database.close()


def test_migrations_are_numbered_and_applied_once(postgres_url: str, tmp_path: Path) -> None:
    from nexura_postgres import migration_status
    from nexura_postgres.studio import COMPONENT, MIGRATIONS

    first, db1 = _pg_store(postgres_url, tmp_path)
    second, db2 = _pg_store(postgres_url, tmp_path)  # a second instance: no DDL error
    try:

        async def ledger(conn: Any) -> Any:
            return await conn.fetch(
                "SELECT version FROM nexura_schema_migrations WHERE component = $1", COMPONENT
            )

        assert [r["version"] for r in db1.run(ledger)] == [m[0] for m in MIGRATIONS]
        assert db1.run(migration_status)[COMPONENT] == "current"
    finally:
        first._close_connection()
        second._close_connection()
        db1.close()
        db2.close()


def test_closed_store_refuses_calls(postgres_url: str, tmp_path: Path) -> None:
    from nexura_agents.errors import WorkflowError

    store, database = _pg_store(postgres_url, tmp_path)
    try:
        store._close_connection()
        with pytest.raises(WorkflowError, match="closed"):
            store._get_run_status_sync("r")
    finally:
        database.close()


def test_concurrent_first_opens_seed_once(postgres_url: str, tmp_path: Path) -> None:
    from nexura_postgres import Database, PostgresStudioStore

    databases = [Database(postgres_url) for _ in range(4)]
    stores: list[Any] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(4)

    def open_one(database: Any) -> None:
        try:
            barrier.wait(timeout=30)
            stores.append(
                PostgresStudioStore(
                    database, checkpoint_store=_NoCheckpoints(), db_path=tmp_path / "x"
                )
            )
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=open_one, args=(d,)) for d in databases]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    try:
        assert not errors, errors
        workflows = asyncio.run(stores[0].list_workflows())
        assert [w["workflow_id"] for w in workflows] == [store_module.DEFAULT_WORKFLOW_ID]
    finally:
        for database in databases:
            database.close()
