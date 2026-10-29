# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""MVP Postgres WP3: service wiring of the Postgres run store, and the WP2b round-3 follow-ups.

* With ``NEXURA_DATABASE_URL`` (or ``database_url=``) the service keeps Studio workflows, runs
  and run events in Postgres (``PostgresStudioStore``), with workflow state and the trigger
  queue next to them; nothing about runs reaches the SQLite file. Without it, SQLite as before.
* Postgres mode refuses a caller's SQLite run store and an empty workflow-state URL, before any
  connection. The partial-Postgres refusal (and its Cloud Run refusal) still holds.
* R3-1: the blob binding is two-way. The store records the id of the database that owns it; a
  second database started against that store is refused at startup, and ``db blob-gc``
  refuses unless the owning database is scanned. A restored database (same id) is accepted.
* R3-2: a plain SQLite service binds when the codec is attached, so the first offload never
  binds inside a database write.
* R3-3: ``/readyz`` shows operators the inline-fallback counters, and a run-event payload above
  the inline limit keeps only its preview when the blob store fails.

Postgres tests skip without ``NEXURA_TEST_POSTGRES_URI``. No network beyond the test database.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any

import httpx
import pytest

from nexura_runtime.blobs import BlobPayloads, LocalBlobStore
from nexura_runtime.blobs.base import BlobStoreError, owner_record_text
from nexura_runtime.blobs.binding import BlobOwnerError, SQLiteBlobBinding
from nexura_runtime.blobs.payloads import (
    EVAL_TRACE_NAMESPACE,
    MARKER_PREFIX,
    RUN_EVENT_NAMESPACE,
)
from nexura_runtime.blobs.retention import (
    BlobRetentionRefused,
    ReferenceScan,
    collect_blob_garbage,
)
from nexura_runtime.cli.main import main
from nexura_runtime.service import storage_backend
from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.studio.store import StudioStore
from nexura_runtime.testing.blob_store import FakeGCSClient
from nexura_runtime.testing.pytest_backend import shared_test_keys
from nexura_runtime.workflow import WorkflowSpec

BIG = "x" * 20_000
FIXTURE_URL = "postgresql://fixture.example/db?sslmode=require"


def _spec() -> dict[str, Any]:
    return {
        "spec_version": 1,
        "name": "wp3",
        "entry_node": "n",
        "nodes": {"n": {"id": "n", "handler": "synthetic_order_lookup", "transitions": {}}},
    }


def _sqlite_tables(path: Path) -> set[str]:
    if not path.exists():
        return set()
    with closing(sqlite3.connect(path)) as conn:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


@pytest.fixture
def partial(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXURA_ALLOW_PARTIAL_POSTGRES", "1")
    for name in ("K_SERVICE", "CLOUD_RUN_JOB", "NEXURA_WORKFLOW_STATE_URL"):
        monkeypatch.delenv(name, raising=False)


async def _client(app: Any) -> Any:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1")


# ------------------------------------------------------------------------------ wiring


@pytest.mark.parametrize("source", ["argument", "environment"])
async def test_postgres_mode_keeps_runs_workflow_state_and_triggers_in_postgres(
    service_database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    partial: None,
    source: str,
) -> None:
    from nexura_postgres import PostgresStudioStore, PostgresTriggerStore, PostgresWorkflowStore

    db = tmp_path / "svc.sqlite3"
    if source == "environment":
        monkeypatch.setenv("NEXURA_DATABASE_URL", service_database_url)
        app = create_api_app(db_path=db)
    else:
        monkeypatch.delenv("NEXURA_DATABASE_URL", raising=False)
        app = create_api_app(db_path=db, database_url=service_database_url)
    assert isinstance(app.state.store, PostgresStudioStore)
    assert isinstance(app.state.store.checkpoint_store, PostgresWorkflowStore)
    assert isinstance(app.state.trigger_store, PostgresTriggerStore)
    assert app.state.execution_store is app.state.store.checkpoint_store.execution_store
    async with app.router.lifespan_context(app), await _client(app) as client:
        started = await client.post(
            "/api/v1/runs", json={"spec": _spec(), "initial_state": {"doc": BIG}}
        )
        assert started.status_code == 201, started.text
        run_id = started.json()["run_id"]
        got = await client.get(f"/api/v1/runs/{run_id}")
        assert got.status_code == 200 and got.json()["status"] == "completed"
        trace = await client.get(f"/api/v1/runs/{run_id}/trace")
        assert trace.status_code == 200, trace.text  # the Traces page reads the same store
    studio_tables = {t for t in _sqlite_tables(db) if t.startswith("nexura_studio_")}
    assert studio_tables == set()  # no run row, event or workflow in the SQLite file
    assert not {t for t in _sqlite_tables(db) if t.startswith("nexura_workflow_")}


async def test_sqlite_default_is_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NEXURA_DATABASE_URL", raising=False)
    db = tmp_path / "svc.sqlite3"
    app = create_api_app(db_path=db)
    assert type(app.state.store) is StudioStore
    async with app.router.lifespan_context(app), await _client(app) as client:
        started = await client.post("/api/v1/runs", json={"spec": _spec()})
        assert started.status_code == 201, started.text
    assert {"nexura_studio_runs", "nexura_studio_run_events"} <= _sqlite_tables(db)
    assert not (tmp_path / "svc.sqlite3.blobs").exists()  # offload stays off: no folder


def test_postgres_mode_refuses_a_callers_sqlite_store_before_connecting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, partial: None
) -> None:
    shared_test_keys(monkeypatch)  # MVP WP9: Postgres mode needs the shared keys
    monkeypatch.setattr(storage_backend, "_TEST_SQLITE_STUDIO_STORE", False)
    opened: list[str] = []
    monkeypatch.setattr(
        storage_backend.ServiceStores, "open_database", lambda self: opened.append("x")
    )
    store = StudioStore(tmp_path / "own.sqlite3")
    try:
        with pytest.raises(ValueError, match="keeps its runs in the SQLite file"):
            create_api_app(db_path=tmp_path / "svc.sqlite3", database_url=FIXTURE_URL, store=store)
    finally:
        store._close_sync()
    assert not opened


@pytest.mark.parametrize("cloud_var", ["K_SERVICE", "CLOUD_RUN_JOB"])
def test_test_only_sqlite_store_opt_in_is_ignored_on_cloud_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cloud_var: str
) -> None:
    from nexura_runtime.testing.sqlite_import import allow_sqlite_studio_store

    store = StudioStore(tmp_path / "own.sqlite3")
    try:
        with allow_sqlite_studio_store():
            storage_backend.refuse_sqlite_studio_store(store)  # accepted locally
            monkeypatch.setenv(cloud_var, "svc")
            with pytest.raises(ValueError, match="SQLite file"):
                storage_backend.refuse_sqlite_studio_store(store)
    finally:
        store._close_sync()


def test_postgres_mode_refuses_sqlite_workflow_state_before_creating_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, partial: None
) -> None:
    shared_test_keys(monkeypatch)  # MVP WP9: Postgres mode needs the shared keys
    opened: list[str] = []
    monkeypatch.setattr(
        storage_backend.ServiceStores, "open_database", lambda self: opened.append("x")
    )
    with pytest.raises(ValueError, match="workflow_state_url must not be empty"):
        create_api_app(
            db_path=tmp_path / "svc.sqlite3", database_url=FIXTURE_URL, workflow_state_url=""
        )
    assert not opened and not (tmp_path / "svc.sqlite3").exists()


def test_postgres_mode_needs_no_partial_switch_and_refuses_it_on_cloud_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shared_test_keys(monkeypatch)  # MVP WP9: Postgres mode needs the shared keys
    # MVP WP8: every store is ported, so Postgres mode starts without the switch. The app gets
    # past every refusal to its first connection (stubbed here) and creates no SQLite file.
    monkeypatch.delenv("NEXURA_ALLOW_PARTIAL_POSTGRES", raising=False)
    for name in ("K_SERVICE", "CLOUD_RUN_JOB"):
        monkeypatch.delenv(name, raising=False)

    class Connecting(Exception):
        pass

    def connect(self: Any) -> Any:
        raise Connecting

    monkeypatch.setattr(storage_backend.ServiceStores, "open_database", connect)
    with pytest.raises(Connecting):
        create_api_app(db_path=tmp_path / "svc.sqlite3", database_url=FIXTURE_URL)
    assert not (tmp_path / "svc.sqlite3").exists()
    # The development switch is still refused on Cloud Run, before anything is created.
    monkeypatch.setenv("NEXURA_ALLOW_PARTIAL_POSTGRES", "1")
    monkeypatch.setenv("K_SERVICE", "svc")
    with pytest.raises(RuntimeError, match="refused on Cloud Run"):
        create_api_app(db_path=tmp_path / "svc.sqlite3", database_url=FIXTURE_URL)
    assert not (tmp_path / "svc.sqlite3").exists()


def test_partial_warning_names_runs_as_postgres(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXURA_ALLOW_PARTIAL_POSTGRES", "1")
    for name in ("K_SERVICE", "CLOUD_RUN_JOB"):
        monkeypatch.delenv(name, raising=False)
    stores = storage_backend.ServiceStores("/tmp/x.sqlite3", FIXTURE_URL)
    assert not stores.partial  # the switch alone: no SQLite-only feature, nothing partial
    stores.require_sqlite_feature("repro_capture")
    assert stores.partial
    warning = stores.partial_warning()
    assert "runs" in warning.split("uses Postgres")[0]
    # Every service store is on Postgres since MVP WP8 (WP4 chat sessions, agents and skills;
    # WP5 secrets and the usage ledger; WP6 organisations and publications; WP7 the knowledge
    # index; WP8 evaluations, channels and integrations). Only the SQLite-only feature that is
    # in use stays in SQLite.
    on_postgres, on_sqlite = warning.split("uses Postgres")
    for ported in (
        "chat sessions",
        "agents, skills",
        "secrets, the usage ledger",
        "organisations",
        "publications",
        "knowledge",
        "evaluations",
        "channels",
        "integrations",
    ):
        assert ported in on_postgres, ported
    for ported in ("secrets", "usage", "organisations", "publications", "knowledge"):
        assert ported not in on_sqlite
    for ported in ("evaluations", "channels", "integrations", "chat"):
        assert ported not in on_sqlite
    assert "repro capture" in on_sqlite and "delivery tokens" not in on_sqlite


# ----------------------------------------------------------- R3-1: two-way binding


def test_local_and_gcs_owner_records_are_create_only(tmp_path: Path) -> None:
    from nexura_runtime.blobs.base import new_database_id
    from nexura_runtime.blobs.gcs import GCSBlobStore

    first, second = new_database_id(), new_database_id()
    local = LocalBlobStore(tmp_path / "blobs")
    fake = FakeGCSClient()
    gcs = GCSBlobStore("bucket", "prod", client=fake)
    for store in (local, gcs):
        assert store.owner() is None
        assert store.claim_owner(first) == first
        assert store.claim_owner(second) == first  # never replaced
        assert store.owner() == first
    assert list(local.list()) == [] and list(gcs.list()) == []  # not a blob
    recorded = json.loads(fake.raw("bucket", "prod.nexura-store-owner"))
    assert recorded["database_id"] == first and recorded["claimed_at"] > 0  # review H1
    local_record = local.owner_record()
    assert local_record is not None and local_record[0] == first
    # Another instance won the create race (412): its owner is read back.
    racing = GCSBlobStore("bucket", "other", client=fake)
    fake.bucket("bucket").blob("other.nexura-store-owner").upload_from_string(
        owner_record_text(second, 1.0)
    )
    original = racing.owner
    calls = iter([None, second])
    racing.owner = lambda: next(calls)  # type: ignore[method-assign]
    assert racing.claim_owner(first) == second
    racing.owner = original  # type: ignore[method-assign]
    with pytest.raises(BlobStoreError):
        local.claim_owner("not-an-id")


async def test_a_second_database_is_refused_at_startup_and_left_unbound(
    service_database_url: str,
    second_service_database_url: str,
    tmp_path: Path,
    partial: None,
) -> None:
    from nexura_postgres import Database, PostgresBlobBinding

    blobs = tmp_path / "prod-blobs"
    prod = create_api_app(
        db_path=tmp_path / "prod.sqlite3",
        database_url=service_database_url,
        blob_url=blobs.as_uri(),
    )
    async with prod.router.lifespan_context(prod):
        pass
    owner = LocalBlobStore(blobs).owner()
    prod_db = Database.from_url(service_database_url)
    try:
        assert owner == PostgresBlobBinding(prod_db).database_id()
        assert PostgresBlobBinding(prod_db).read() == LocalBlobStore(blobs).identity()
    finally:
        prod_db.close()

    staging = create_api_app(
        db_path=tmp_path / "staging.sqlite3",
        database_url=second_service_database_url,
        blob_url=blobs.as_uri(),
    )
    with pytest.raises(RuntimeError, match="belongs to database"):
        async with staging.router.lifespan_context(staging):
            pass
    staging_db = Database.from_url(second_service_database_url)
    try:
        assert PostgresBlobBinding(staging_db).read() is None  # refused before binding
    finally:
        staging_db.close()
    assert LocalBlobStore(blobs).owner() == owner  # unchanged

    # The same database restored elsewhere (same id, as pg_dump/pg_restore keeps) is accepted.
    restored_db = Database.from_url(second_service_database_url)

    async def restore_id(conn: Any) -> None:
        await conn.execute("DELETE FROM nexura_database_identity")
        await conn.execute(
            "INSERT INTO nexura_database_identity (singleton, database_id, created_at) "
            "VALUES (1, $1, 0)",
            owner,
        )

    try:
        restored_db.run(restore_id)
    finally:
        restored_db.close()
    restored = create_api_app(
        db_path=tmp_path / "restored.sqlite3",
        database_url=second_service_database_url,
        blob_url=blobs.as_uri(),
    )
    async with restored.router.lifespan_context(restored):
        pass


def test_blob_gc_requires_the_owning_database(
    service_database_url: str,
    second_service_database_url: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The review's R3-1 repro: a fresh staging database bound (one-way) to prod's store, and
    prod's store holding a three-day-old referenced blob. ``db blob-gc`` against staging must
    refuse; against prod it proceeds."""
    from nexura_postgres import Database, PostgresBlobBinding, PostgresStudioStore

    blobs = LocalBlobStore(tmp_path / "blobs")
    codec = BlobPayloads(blobs, 1024, strict=True)
    prod_db = Database.from_url(service_database_url)
    staging_db = Database.from_url(second_service_database_url)
    try:
        prod = PostgresStudioStore(
            prod_db, checkpoint_store=object(), db_path=tmp_path / "p", blob_payloads=codec
        )
        codec.check_ready()  # strict startup: owner claimed, prod bound
        prod._record_run_start_sync(
            "r", None, WorkflowSpec.from_dict(_spec()), {"doc": BIG}, journal_workspace="ws"
        )
        prod._update_run_sync("r", "completed", None, {}, 1)
        (info,) = list(blobs.list())
        os.utime(blobs.root / info.key, (time.time() - 3 * 86400,) * 2)
        # A staging database that got bound one-way (as before R3-1) to prod's store.
        staging = PostgresBlobBinding(staging_db)
        staging.ensure(str(blobs.identity()), blobs.describe())
        _empty_reference_tables(staging_db)
    finally:
        prod_db.close()
        staging_db.close()

    argv = ["db", "blob-gc", "--blob-url", blobs.root.as_uri(), "--grace-hours", "1"]
    argv += ["--sqlite", _eval_db(tmp_path), "--database-url"]
    assert main([*argv, second_service_database_url]) == 2
    assert "belongs to database" in capsys.readouterr().err
    assert blobs.exists(info.key)  # nothing deleted
    assert main([*argv, service_database_url]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["owner_scanned"] is True and report["referenced"] == 1
    assert report["deleted_unreferenced"] == 0 and blobs.exists(info.key)


def _eval_db(tmp_path: Path) -> str:
    """The eval-trace reference table still lives in SQLite until WP8."""
    path = tmp_path / "evals.sqlite3"
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS eval_traces (run_id TEXT, workspace_id TEXT, "
            "trace_json TEXT)"
        )
    return str(path)


def _empty_reference_tables(database: Any) -> None:
    async def create(conn: Any) -> None:
        await conn.execute(
            "CREATE TABLE nexura_studio_run_events (run_id TEXT, data_json TEXT NOT NULL)"
        )

    database.run(create)


def test_retention_refuses_a_store_owned_by_an_unscanned_database(tmp_path: Path) -> None:
    from nexura_runtime.blobs.base import new_database_id

    store = LocalBlobStore(tmp_path / "blobs")
    owner = new_database_id()
    store.claim_owner(owner)
    # Written after the claim (an orphan older than the grace of 0 s).
    ref = store.put("ws", RUN_EVENT_NAMESPACE, b"{}", "application/json")
    bound = frozenset({str(store.identity())})
    other = ReferenceScan(frozenset(), frozenset(), bindings=bound, database_ids=frozenset())
    with pytest.raises(BlobRetentionRefused, match="belongs to database"):
        collect_blob_garbage(store, lambda: other, grace_seconds=0)
    assert store.exists(ref.key)
    mine = ReferenceScan(frozenset(), frozenset(), bindings=bound, database_ids=frozenset({owner}))
    report = collect_blob_garbage(store, lambda: mine, grace_seconds=0, dry_run=True)
    assert report.owner == owner and report.owner_scanned and report.deleted_unreferenced == 1
    forced = collect_blob_garbage(store, lambda: other, grace_seconds=0, dry_run=True, force=True)
    assert forced.deleted_unreferenced == 1


def test_codec_refuses_to_bind_a_store_owned_by_another_database(tmp_path: Path) -> None:
    from nexura_runtime.blobs.base import new_database_id

    class FakeBinding:
        ident = ("fake", "db")

        def __init__(self, database_id: str) -> None:
            self.id = database_id
            self.bound: list[str] = []

        def database_id(self) -> str:
            return self.id

        def ensure(self, store_id: str, store: str) -> None:
            self.bound.append(store_id)

        def read(self) -> str | None:
            return self.bound[0] if self.bound else None

    store = LocalBlobStore(tmp_path / "blobs")
    first = FakeBinding(new_database_id())
    codec = BlobPayloads(store, 100, strict=True)
    codec.add_binding(first)
    codec.check_ready()
    assert store.owner() == first.id and first.bound == [store.identity()]
    second = FakeBinding(new_database_id())
    other = BlobPayloads(store, 100, strict=True)
    other.add_binding(second)
    with pytest.raises(BlobOwnerError, match="belongs to database"):
        other.check_ready()
    assert second.bound == []  # never bound one-way either


# -------------------------------------------------------- R3-2: bind when attached


def test_plain_sqlite_binds_when_the_codec_is_attached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("NEXURA_DATABASE_URL", raising=False)
    db = tmp_path / "svc.sqlite3"
    app = create_api_app(db_path=db, blob_url=(tmp_path / "blobs").as_uri())
    # No lifespan and no offload yet: the database is bound already.
    assert SQLiteBlobBinding(db).read() == app.state.blob_store.identity()
    store = app.state.store
    store._record_run_start_sync("r", None, WorkflowSpec.from_dict(_spec()), {})
    # The R3-2 case: this thread already holds a write transaction on the store's connection.
    conn = store._ensure_open()
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("UPDATE nexura_studio_runs SET error = 'held' WHERE run_id = 'r'")
    started = time.monotonic()
    store._record_run_event_sync("r", "BIG", data={"doc": BIG})
    assert time.monotonic() - started < 10  # no wait on the SQLite write lock
    assert app.state.blob_payloads.inline_fallbacks == 0
    with closing(sqlite3.connect(db)) as check:
        (stored,) = check.execute(
            "SELECT data_json FROM nexura_studio_run_events WHERE event_type = 'BIG'"
        ).fetchone()
    assert stored.startswith(MARKER_PREFIX)
    store._close_sync()


def test_offload_off_binds_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NEXURA_DATABASE_URL", raising=False)
    app = create_api_app(db_path=tmp_path / "svc.sqlite3")
    assert app.state.blob_payloads.threshold == 0
    assert SQLiteBlobBinding(tmp_path / "svc.sqlite3").read() is None
    assert not (tmp_path / "svc.sqlite3.blobs").exists()


# ----------------------------------------------- R3-3: counters and the inline cap


class _Down(LocalBlobStore):
    def put(self, *args: Any, **kwargs: Any) -> Any:
        raise BlobStoreError("store is down")


def test_inline_cap_keeps_only_a_preview_of_run_events(tmp_path: Path) -> None:
    codec = BlobPayloads(_Down(tmp_path / "b"), 100, strict=True, max_inline_bytes=5000)
    small = json.dumps({"t": "s" * 1000})
    assert codec.encode("ws", RUN_EVENT_NAMESPACE, small) == small  # inline fallback
    big = json.dumps({"t": "b" * 9000})
    stored = codec.encode("ws", RUN_EVENT_NAMESPACE, big)
    value = json.loads(stored)
    assert value["payload_unavailable"] is True and value["reason"] == "not_stored"
    assert value["size"] == len(big.encode()) and value["preview"] == big[:256]
    assert len(stored) < 1000 and not stored.startswith(MARKER_PREFIX)
    # Eval traces must parse as traces: they keep the inline fallback.
    assert codec.encode("ws", EVAL_TRACE_NAMESPACE, big) == big
    stats = codec.stats()
    assert stats["inline_fallbacks"] == 2 and stats["omitted_payloads"] == 1
    assert stats["max_inline_bytes"] == 5000
    assert BlobPayloads(_Down(tmp_path / "c"), 100, strict=True).max_inline_bytes == 1024 * 1024
    lenient = BlobPayloads(_Down(tmp_path / "d"), 100)
    assert (
        lenient.max_inline_bytes is None and lenient.encode("ws", RUN_EVENT_NAMESPACE, big) == big
    )


def test_inline_cap_environment_is_validated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stores = storage_backend.ServiceStores(tmp_path / "s.sqlite3", blob_url="gs://nexura-blobs/p")
    monkeypatch.setenv(storage_backend.BLOB_MAX_INLINE_ENV, "-1")
    with pytest.raises(ValueError, match="NEXURA_BLOB_MAX_INLINE_BYTES"):
        stores.blob_max_inline_bytes()
    monkeypatch.setenv(storage_backend.BLOB_MAX_INLINE_ENV, "abc")
    with pytest.raises(ValueError):
        stores.blob_max_inline_bytes()
    monkeypatch.setenv(storage_backend.BLOB_MAX_INLINE_ENV, "2048")
    assert stores.blob_max_inline_bytes() == 2048


async def test_readyz_shows_blob_counters_to_operators_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import nexura_runtime.blobs.url as blob_url

    monkeypatch.delenv("NEXURA_DATABASE_URL", raising=False)
    monkeypatch.setenv(storage_backend.BLOB_MAX_INLINE_ENV, "10000")
    fake = FakeGCSClient()
    real_open = blob_url.open_blob_store
    monkeypatch.setattr(
        "nexura_runtime.service.storage_backend.open_blob_store",
        lambda location: real_open(location, client=fake),
    )
    app = create_api_app(db_path=tmp_path / "s.sqlite3", blob_url="gs://nexura-blobs/prod")
    async with app.router.lifespan_context(app), await _client(app) as client:
        operator = (await client.get("/readyz")).json()
        assert operator["blob_payloads"]["inline_fallbacks"] == 0
        assert operator["blob_payloads"]["store"] == "gs://nexura-blobs/prod"

        def down(*args: Any, **kwargs: Any) -> Any:
            raise BlobStoreError("outage")

        monkeypatch.setattr(app.state.blob_store, "put", down)
        for doc in ("y" * 9000, "z" * 20_000):
            started = await client.post(
                "/api/v1/runs", json={"spec": _spec(), "initial_state": {"doc": doc}}
            )
            assert started.status_code == 201, started.text
        operator = (await client.get("/readyz")).json()
        assert operator["blob_payloads"]["inline_fallbacks"] >= 1
        assert operator["blob_payloads"]["omitted_payloads"] >= 1
        run_id = started.json()["run_id"]
        first = (await client.get(f"/api/v1/runs/{run_id}")).json()["events"][0]["data"]
        assert first["payload_unavailable"] is True and first["reason"] == "not_stored"
    public = create_api_app(
        db_path=tmp_path / "s2.sqlite3",
        blob_url="gs://nexura-blobs/prod2",
        auth_provider=_public_readyz_provider(),
    )
    async with public.router.lifespan_context(public), await _client(public) as client:
        assert (await client.get("/readyz")).json() == {"status": "healthy"}


def _public_readyz_provider() -> Any:
    from nexura_runtime.service.api.auth_providers import LocalOwnerAuthProvider

    class NotOperator(LocalOwnerAuthProvider):
        def is_operator(self, request: Any) -> bool:
            return False

    return NotOperator()
