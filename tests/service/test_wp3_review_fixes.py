# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""MVP Postgres WP3 fix round (``review_wp03.md``): H1, M1, M2 and L3–L6.

* H1: a fresh database never silently claims an unowned store that already holds blobs; the
  deliberate first claim is ``nexura-runtime db blob-claim --i-own-this-store``; unreferenced
  blobs written before the claim are never deleted without ``--force``.
* M1: run owners on the shared Postgres store are judged by process identity (host name, boot
  id and pid namespace, plus a per-process id), never by host name alone; a row of another
  instance that the journal cannot judge is recoverable once it has been idle for several
  lease periods. Includes a real second process with the same host name that is then killed.
* M2: ``db reset-identity`` for copies, and ``db blob-gc`` refuses a stale copy.
* L3–L6: owner checks before any binding, SQLite services never write into an owned store,
  workflow state must be the same database, NUL ids and API-key lookup edge cases.

Postgres tests skip without ``NEXURA_TEST_POSTGRES_URI``; nothing calls a model or the network.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from nexura_runtime.blobs import BlobPayloads, LocalBlobStore
from nexura_runtime.blobs.base import new_database_id
from nexura_runtime.blobs.binding import BlobClaimRefused, BlobOwnerError, SQLiteBlobBinding
from nexura_runtime.blobs.payloads import RUN_EVENT_NAMESPACE
from nexura_runtime.blobs.retention import (
    BlobRetentionRefused,
    ReferenceScan,
    collect_blob_garbage,
)
from nexura_runtime.cli.main import main
from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.service.run_liveness import LIVE, RECOVERABLE, run_liveness
from nexura_runtime.studio import store as store_module
from nexura_runtime.testing.pytest_backend import shared_test_keys
from nexura_runtime.workflow import WorkflowSpec

BIG = "x" * 20_000
THREE_DAYS = 3 * 86400.0
FIXTURE_URL = "postgresql://fixture.example/db?sslmode=require"


def _spec() -> dict[str, Any]:
    return {
        "spec_version": 1,
        "name": "wp3-fixes",
        "entry_node": "n",
        "nodes": {"n": {"id": "n", "handler": "synthetic_order_lookup", "transitions": {}}},
    }


@pytest.fixture
def partial(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXURA_ALLOW_PARTIAL_POSTGRES", "1")
    for name in ("K_SERVICE", "CLOUD_RUN_JOB", "NEXURA_WORKFLOW_STATE_URL", "NEXURA_BLOB_URL"):
        monkeypatch.delenv(name, raising=False)


def _client(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1")


def _age(store: LocalBlobStore, key: str, seconds: float) -> None:
    when = time.time() - seconds
    os.utime(store.root / key, (when, when))


def _eval_db(tmp_path: Path) -> str:
    """The eval-trace reference table, still in SQLite until WP8."""
    import sqlite3
    from contextlib import closing

    path = tmp_path / "evals.sqlite3"
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS eval_traces (run_id TEXT, workspace_id TEXT, "
            "trace_json TEXT)"
        )
    return str(path)


# ------------------------------------------------------------------------------------- H1


async def test_h1_repro_an_unowned_store_is_not_claimed_by_a_fresh_database(
    service_database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    partial: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The review's order: a SQLite prod service writes an offloaded payload into a store that
    has no owner, the blob is three days old, then a fresh Postgres staging database starts
    against the same store. Staging must not claim it, and ``db blob-gc`` against staging must
    not delete prod's referenced blob."""
    from nexura_postgres import Database, PostgresBlobBinding

    blobs = tmp_path / "blobs"
    monkeypatch.delenv("NEXURA_DATABASE_URL", raising=False)
    prod = create_api_app(db_path=tmp_path / "prod.sqlite3", blob_url=blobs.as_uri())
    async with prod.router.lifespan_context(prod), _client(prod) as client:
        started = await client.post(
            "/api/v1/runs", json={"spec": _spec(), "initial_state": {"doc": BIG}}
        )
        assert started.status_code == 201, started.text
    store = LocalBlobStore(blobs)
    infos = list(store.list())
    assert infos
    for info in infos:
        _age(store, info.key, THREE_DAYS)
    assert store.owner() is None  # SQLite services never record an owner

    staging = create_api_app(
        db_path=tmp_path / "staging.sqlite3",
        database_url=service_database_url,
        blob_url=blobs.as_uri(),
    )
    with pytest.raises(RuntimeError, match="already holds blobs") as refused:
        async with staging.router.lifespan_context(staging):
            pass
    assert "db blob-claim --i-own-this-store" in str(refused.value)
    assert store.owner() is None  # not claimed
    database = Database.from_url(service_database_url)
    try:
        assert PostgresBlobBinding(database).read() is None  # not bound either
    finally:
        database.close()

    argv = ["db", "blob-gc", "--blob-url", blobs.as_uri(), "--grace-hours", "1"]
    argv += ["--sqlite", _eval_db(tmp_path), "--database-url", service_database_url]
    assert main(argv) == 2
    assert "not bound" in capsys.readouterr().err
    assert all(store.exists(i.key) for i in infos)  # prod's referenced blobs survive
    # Prod's own database still works with the store.
    prod_gc = ["db", "blob-gc", "--sqlite", str(tmp_path / "prod.sqlite3"), "--dry-run"]
    assert main([*prod_gc, "--blob-url", blobs.as_uri()]) == 0
    assert json.loads(capsys.readouterr().out)["referenced"] == len(infos)


def _pg_codec_for(database: Any, store: LocalBlobStore) -> BlobPayloads:
    from nexura_postgres import PostgresBlobBinding

    codec = BlobPayloads(store, 1024, strict=True)
    codec.add_binding(PostgresBlobBinding(database))
    return codec


def test_a_database_that_references_the_store_may_claim_it(
    service_database_url: str, tmp_path: Path
) -> None:
    """A store written before owners existed, by this database (for example a SQLite service
    imported into Postgres): its references name the store, so the claim is allowed."""
    from nexura_postgres import Database, PostgresStudioStore

    store = LocalBlobStore(tmp_path / "blobs")
    marker = BlobPayloads(store, 1024).encode("ws", RUN_EVENT_NAMESPACE, json.dumps({"d": BIG}))
    database = Database.from_url(service_database_url)
    try:
        studio = PostgresStudioStore(database, checkpoint_store=object(), db_path=tmp_path / "x")
        studio._record_run_start_sync("r", None, WorkflowSpec.from_dict(_spec()), {})

        async def reference(conn: Any) -> None:
            await conn.execute(
                "UPDATE nexura_studio_run_events SET data_json = $1 WHERE run_id = 'r'", marker
            )

        database.run(reference)
        codec = _pg_codec_for(database, store)
        codec.check_ready()
        from nexura_postgres import PostgresBlobBinding

        assert store.owner() == PostgresBlobBinding(database).database_id()
    finally:
        database.close()


def test_blob_claim_command(
    service_database_url: str,
    second_service_database_url: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from nexura_postgres import Database, PostgresBlobBinding

    store = LocalBlobStore(tmp_path / "blobs")
    store.put("ws", RUN_EVENT_NAMESPACE, b"{}", "application/json")  # a pre-owner blob
    base = ["db", "blob-claim", "--blob-url", store.root.as_uri(), "--database-url"]
    assert main([*base, service_database_url]) == 2  # confirmation flag missing
    assert "--i-own-this-store" in capsys.readouterr().err
    assert store.owner() is None
    assert main([*base, service_database_url, "--i-own-this-store"]) == 0
    report = json.loads(capsys.readouterr().out)
    database = Database.from_url(service_database_url)
    try:
        mine = PostgresBlobBinding(database).database_id()
        assert report["owner"] == mine and report["claimed_at"] > 0
        assert PostgresBlobBinding(database).read() == store.identity()  # bound too
        _pg_codec_for(database, store).check_ready()  # its service now starts
    finally:
        database.close()
    # Another database cannot take it over.
    assert main([*base, second_service_database_url, "--i-own-this-store"]) == 2
    assert "already belongs to database" in capsys.readouterr().err
    assert store.owner() == mine


def test_blob_claim_refuses_a_database_bound_to_another_store(
    service_database_url: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Review round 2, R1: a wrong database URL must not leave a permanent owner record."""
    from nexura_postgres import Database, PostgresBlobBinding

    first = LocalBlobStore(tmp_path / "first")
    other = LocalBlobStore(tmp_path / "other")
    other.put("ws", RUN_EVENT_NAMESPACE, b"{}", "application/json")
    database = Database.from_url(service_database_url)
    try:
        PostgresBlobBinding(database).ensure(str(first.identity()), first.describe())
    finally:
        database.close()
    command = ["db", "blob-claim", "--blob-url", other.root.as_uri()]
    assert main([*command, "--database-url", service_database_url, "--i-own-this-store"]) == 2
    assert "is bound to blob store" in capsys.readouterr().err
    assert other.owner() is None  # still claimable by the database that really writes it


def test_unreferenced_blobs_older_than_the_claim_are_kept(tmp_path: Path) -> None:
    store = LocalBlobStore(tmp_path / "blobs")
    before = store.put("ws", RUN_EVENT_NAMESPACE, b"{}", "application/json")
    _age(store, before.key, THREE_DAYS)
    owner = new_database_id()
    store.claim_owner(owner)
    after = store.put("ws", RUN_EVENT_NAMESPACE, b"{}", "application/json")
    bound = frozenset({str(store.identity())})
    scan = ReferenceScan(frozenset(), frozenset(), bindings=bound, database_ids=frozenset({owner}))
    report = collect_blob_garbage(store, lambda: scan, grace_seconds=0)
    assert report.kept_before_claim == 1 and report.deleted_keys == [after.key]
    assert store.exists(before.key) and report.owner_claimed_at is not None
    forced = collect_blob_garbage(store, lambda: scan, grace_seconds=0, force=True)
    assert forced.deleted_keys == [before.key]  # --force still can


# ------------------------------------------------------------------------------------- M2


def test_reset_identity_command(
    service_database_url: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A copy (here: the same database standing in for its branch) gets a new id and no
    binding, so it is refused by the original's store and can claim a new one."""
    from nexura_postgres import Database, PostgresBlobBinding

    original = LocalBlobStore(tmp_path / "prod-blobs")
    database = Database.from_url(service_database_url)
    try:
        _pg_codec_for(database, original).check_ready()
        old_id = PostgresBlobBinding(database).database_id()
        base = ["db", "reset-identity", "--database-url", service_database_url]
        assert main(base) == 2 and "--this-is-a-copy" in capsys.readouterr().err
        assert main([*base, "--this-is-a-copy"]) == 0
        new_id = json.loads(capsys.readouterr().out)["database_id"]
        fresh = PostgresBlobBinding(database)
        assert new_id != old_id and fresh.database_id() == new_id and fresh.read() is None
        with pytest.raises(BlobOwnerError, match="belongs to database"):
            _pg_codec_for(database, original).check_ready()
        own = LocalBlobStore(tmp_path / "copy-blobs")
        _pg_codec_for(database, own).check_ready()
        assert own.owner() == new_id and original.owner() == old_id
    finally:
        database.close()


def test_blob_gc_refuses_a_stale_copy(tmp_path: Path) -> None:
    """The copy's newest run activity is three days old; the store holds an unreferenced blob
    from two days ago (written by the live database after the copy was made)."""
    store = LocalBlobStore(tmp_path / "blobs")
    owner = new_database_id()
    store.claim_owner(owner)
    newer = store.put("ws", RUN_EVENT_NAMESPACE, b"{}", "application/json")
    _age(store, newer.key, 2 * 86400.0)
    store_module_owner = frozenset({owner})
    bound = frozenset({str(store.identity())})
    copy = ReferenceScan(
        frozenset(),
        frozenset(),
        bindings=bound,
        database_ids=store_module_owner,
        newest_activity=time.time() - THREE_DAYS,
    )
    # The claim is older than the blob in real deployments; here it is "now", so move it back.
    record = store.root / "_nexura" / "store-owner"
    record.write_bytes(json.dumps({"database_id": owner, "claimed_at": 1.0}).encode())
    with pytest.raises(BlobRetentionRefused, match="stale copy"):
        collect_blob_garbage(store, lambda: copy, grace_seconds=3600)
    assert store.exists(newer.key)
    live = ReferenceScan(
        frozenset(),
        frozenset(),
        bindings=bound,
        database_ids=store_module_owner,
        newest_activity=time.time(),
    )
    report = collect_blob_garbage(store, lambda: live, grace_seconds=3600, dry_run=True)
    assert report.deleted_unreferenced == 1  # the live database may collect it
    forced = collect_blob_garbage(store, lambda: copy, grace_seconds=3600, dry_run=True, force=True)
    assert forced.deleted_unreferenced == 1


def test_postgres_scan_reports_the_newest_activity(
    service_database_url: str, tmp_path: Path
) -> None:
    from nexura_postgres import Database, PostgresStudioStore, referenced_blob_keys

    database = Database.from_url(service_database_url)
    try:
        assert database.run(referenced_blob_keys).newest_activity is None
        studio = PostgresStudioStore(database, checkpoint_store=object(), db_path=tmp_path / "x")
        before = time.time()
        studio._record_run_start_sync("r", None, WorkflowSpec.from_dict(_spec()), {})
        newest = database.run(referenced_blob_keys).newest_activity
        assert newest is not None and newest >= before - 1
    finally:
        database.close()


# ------------------------------------------------------------------------------------- M1


def test_postgres_owner_rule_never_uses_the_host_name(sleeper: subprocess.Popen[bytes]) -> None:
    from nexura_agents.durable import _host_identity, _start_ticks
    from nexura_postgres.studio import process_run_owner, run_owner_is_gone

    me = process_run_owner()
    assert me.startswith(f"nx1.{_host_identity()}.{os.getpid()}.")
    assert process_run_owner() == me  # stable for the process
    assert run_owner_is_gone(me) is False
    assert run_owner_is_gone(None) is True  # rows that predate owners, as on SQLite
    # The review's repro: same host name, another machine (boot id differs), pid 1.
    same_name_other_machine = f"{store_module._host_id()}:1:another-boot-123"
    assert store_module._owner_is_gone(same_name_other_machine) is True  # B46 (SQLite) says gone
    assert run_owner_is_gone(same_name_other_machine) is False  # Postgres never judges it
    # Another instance (other host identity: other boot id or pid namespace).
    other = f"nx1.{'a' * 16}.{os.getpid()}.0.{'b' * 32}"
    assert run_owner_is_gone(other) is False
    # A process of this pid namespace that exited.
    owner = f"nx1.{_host_identity()}.{sleeper.pid}.{_start_ticks(sleeper.pid)}.{'c' * 32}"
    assert run_owner_is_gone(owner) is False  # alive
    sleeper.kill()
    sleeper.wait()
    assert run_owner_is_gone(owner) is True


@pytest.fixture
def sleeper() -> Iterator[subprocess.Popen[bytes]]:
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        yield proc
    finally:
        proc.kill()
        proc.wait()


class _NoJournalRow:
    """A journal that cannot tell: nothing recoverable, no row for the run."""

    async def list_recoverable(self, workspace: str) -> list[Any]:
        return []

    async def get(self, execution_id: Any) -> Any:
        raise KeyError(execution_id)


async def test_run_liveness_idle_rule_applies_only_to_shared_stores() -> None:
    from nexura_postgres.studio import run_owner_is_gone

    foreign = f"nx1.{'a' * 16}.1.0.{'b' * 32}"
    fresh = {"run_id": "fresh", "journal_workspace": "ws", "owner_process": foreign}
    idle = {**fresh, "run_id": "idle", "updated_at": time.time() - 100}
    fresh["updated_at"] = time.time()
    shared = {"owner_gone": run_owner_is_gone, "this_owner": "me", "stale_after": 90.0}
    verdicts = await run_liveness(_NoJournalRow(), [fresh, idle], **shared)
    assert verdicts == {"fresh": LIVE, "idle": RECOVERABLE}
    # SQLite keeps the local B46 rule: a foreign-host owner is never gone, idle or not.
    rows = [{**row, "owner_process": "another-host:1:x"} for row in (fresh, idle)]
    local = await run_liveness(_NoJournalRow(), rows)
    assert local == {"fresh": LIVE, "idle": LIVE}


_CHILD = """
import os, sys, time
from nexura_postgres import Database, PostgresStudioStore
from nexura_runtime.workflow import WorkflowSpec
spec = WorkflowSpec.from_dict({
    "name": "child", "entry_node": "n", "terminal_nodes": ["n"],
    "nodes": {"n": {"id": "n", "handler": "synthetic_order_lookup"}},
})
db = Database.from_url(os.environ["WP3_CHILD_DSN"])
store = PostgresStudioStore(db, checkpoint_store=object(), db_path=sys.argv[1])
store._record_run_start_sync("child-run", None, spec, {}, journal_workspace="default")
print("ready", flush=True)
time.sleep(120)
"""


async def test_a_second_process_with_the_same_host_name_is_live_until_killed(
    service_database_url: str, tmp_path: Path, partial: None
) -> None:
    """A real second process (same host name, own pid and process id) starts a run on the
    shared database and has not journaled yet. Another instance must show it running (never
    interrupted) while that process lives, and interrupted once it is killed."""
    env = {**os.environ, "WP3_CHILD_DSN": service_database_url}
    child = subprocess.Popen(
        [sys.executable, "-c", _CHILD, str(tmp_path / "child.sqlite3")],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == b"ready"
        app = create_api_app(db_path=tmp_path / "b.sqlite3", database_url=service_database_url)
        app.state.workspace_repo.record_resource_ownership(
            "run", "child-run", "default", "local-owner"
        )
        async with app.router.lifespan_context(app), _client(app) as client:
            row = (await app.state.store.list_journaled_runs(run_ids=["child-run"]))[0]
            assert row["owner_process"].split(".")[2] == str(child.pid)
            assert row["owner_process"] != app.state.store.this_owner()
            seen = (await client.get("/api/v1/runs/child-run")).json()
            assert seen["status"] == "running", seen
            child.kill()
            child.wait()
            after = (await client.get("/api/v1/runs/child-run")).json()
            assert after["status"] == "interrupted", after
            assert [e["type"] for e in after["events"]].count("RUN_INTERRUPTED") == 1
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


async def test_another_instance_never_interrupts_a_fresh_run_of_the_same_host_name(
    service_database_url: str, tmp_path: Path, partial: None
) -> None:
    """The review's M1 repro across two app instances: a run row owned by a process with the
    same host name on another machine, before its first journal write. Instance B shows it
    running; once the row has been idle for several lease periods it is recoverable."""
    blobs = (tmp_path / "blobs").as_uri()

    def make(name: str) -> Any:
        return create_api_app(
            db_path=tmp_path / name / "svc.sqlite3",
            database_url=service_database_url,
            blob_url=blobs,
            workflow_lease_ttl=1.0,
        )

    app_a, app_b = make("a"), make("b")
    spec = WorkflowSpec.from_dict(_spec())
    async with app_a.router.lifespan_context(app_a), app_b.router.lifespan_context(app_b):
        await app_a.state.store.record_run_start("r1", None, spec, {}, journal_workspace="default")
        app_a.state.workspace_repo.record_resource_ownership("run", "r1", "default", "local-owner")
        other_machine = f"{store_module._host_id()}:1:another-boot-123"

        async def set_row(conn: Any, updated_at: float) -> None:
            await conn.execute(
                "UPDATE nexura_studio_runs SET owner_process = $1, updated_at = $2 "
                "WHERE run_id = 'r1'",
                other_machine,
                updated_at,
            )

        database = app_b.state.store.database
        database.run(lambda conn: set_row(conn, time.time()))
        async with _client(app_b) as b:
            first = (await b.get("/api/v1/runs/r1")).json()
            assert first["status"] == "running", first
            assert "RUN_INTERRUPTED" not in [e["type"] for e in first["events"]]
            database.run(lambda conn: set_row(conn, time.time() - 10))  # > 3 lease periods
            later = (await b.get("/api/v1/runs/r1")).json()
            assert later["status"] == "interrupted", later


# --------------------------------------------------------------------------------- L3–L6


def test_owner_is_checked_before_any_binding_is_written(tmp_path: Path) -> None:
    """L3: an owner refusal leaves no binding behind (the SQLite binding came first)."""
    store = LocalBlobStore(tmp_path / "blobs")
    store.claim_owner(new_database_id())

    class Other:
        ident = ("fake", "other")

        def database_id(self) -> str:
            return new_database_id()

        def ensure(self, store_id: str, store: str) -> None:
            raise AssertionError("must not be bound")

        def read(self) -> str | None:
            return None

    db = tmp_path / "s.sqlite3"
    codec = BlobPayloads(store, 100, strict=True)
    codec.add_binding(SQLiteBlobBinding(db))
    codec.add_binding(Other())
    with pytest.raises(BlobOwnerError):
        codec.check_ready()
    assert SQLiteBlobBinding(db).read() is None


def test_a_sqlite_service_never_writes_into_an_owned_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """L4: a plain SQLite service pointed at another deployment's (owned) store is refused."""
    monkeypatch.delenv("NEXURA_DATABASE_URL", raising=False)
    store = LocalBlobStore(tmp_path / "blobs")
    store.claim_owner(new_database_id())
    with pytest.raises(BlobOwnerError, match="cannot own a store"):
        create_api_app(db_path=tmp_path / "s.sqlite3", blob_url=store.root.as_uri())
    assert SQLiteBlobBinding(tmp_path / "s.sqlite3").read() is None


def test_claim_refusal_is_an_owner_error(tmp_path: Path) -> None:
    store = LocalBlobStore(tmp_path / "blobs")
    store.put("ws", RUN_EVENT_NAMESPACE, b"{}", "application/json")

    class Fresh:
        ident = ("fake", "fresh")

        def database_id(self) -> str:
            return "db_" + "1" * 32

        def ensure(self, store_id: str, store: str) -> None:
            raise AssertionError("must not be bound")

        def read(self) -> str | None:
            return None

        def references(self, store_id: str) -> bool:
            return False

    codec = BlobPayloads(store, 100, strict=True)
    codec.add_binding(Fresh())
    with pytest.raises(BlobClaimRefused):
        codec.check_ready()
    assert store.owner() is None


def test_postgres_mode_refuses_workflow_state_in_another_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, partial: None
) -> None:
    """L5: runs and their checkpoints/journal must be in one database."""
    shared_test_keys(monkeypatch)  # MVP WP9: Postgres mode needs the shared keys
    from nexura_runtime.service import storage_backend

    opened: list[str] = []
    monkeypatch.setattr(
        storage_backend.ServiceStores, "open_database", lambda self: opened.append("x")
    )
    with pytest.raises(ValueError, match="same database as database_url"):
        create_api_app(
            db_path=tmp_path / "s.sqlite3",
            database_url=FIXTURE_URL,
            workflow_state_url="postgresql://other.example/db?sslmode=require",
        )
    assert not opened and not (tmp_path / "s.sqlite3").exists()


async def test_postgres_store_edge_cases(service_database_url: str, tmp_path: Path) -> None:
    """L6: NUL ids read as not found, a bad started_by row cannot break the API-key lookup,
    and a negative limit means "no limit" as on SQLite."""
    from nexura_postgres import Database, PostgresStudioStore

    database = Database.from_url(service_database_url)
    try:
        store = PostgresStudioStore(database, checkpoint_store=object(), db_path=tmp_path / "x")
        spec = WorkflowSpec.from_dict(_spec())
        await store.record_run_start("a", None, spec, {}, started_by={"api_key_id": 'k"1'})
        await store.record_run_start("b", None, spec, {}, started_by={"api_key_id": "k2"})
        await store.record_run_start("c", None, spec, {}, started_by={"note": "nul\x00here"})

        async def corrupt(conn: Any) -> None:  # text the jsonb cast would reject
            await conn.execute(
                "UPDATE nexura_studio_runs SET started_by_json = $1 WHERE run_id = 'b'",
                '{"api_key_id": "k2", "x": "\\u0000"}',
            )

        database.run(corrupt)
        assert await store.list_run_ids_started_by_key('k"1') == ["a"]
        assert await store.list_run_ids_started_by_key("k2") == ["b"]
        assert await store.list_run_ids_started_by_key("k\x00") == []
        assert await store.get_run("a\x00") is None
        assert await store.get_run_status("a\x00") is None
        assert await store.get_workflow("w\x00") is None
        assert await store.delete_workflow("w\x00") is False
        assert await store.count_runs(run_ids=["a", "a\x00"]) == 1
        assert await store.list_journaled_runs(run_ids=["a\x00"]) == []
        assert await store.update_run("a\x00", "completed", None, {}, 1) is False
        assert await store.mark_interrupted("a\x00", error="e", reason="r") is False
        await store.record_run_event("a\x00", "X")  # not recorded, no error
        assert len(await store.list_runs(limit=-1)) == 3
    finally:
        database.close()
