# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Blob retention (MVP WP2b): unreferenced-blob GC with a grace period, payload expiry, CLI.

The race tests use the real write path (``StudioStore`` puts the blob, then commits the row)
against the local store and the fake GCS store. The Postgres tests scan real tables in a
throwaway schema (skipped without ``NEXURA_TEST_POSTGRES_URI``).
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from nexura_runtime.blobs import BlobPayloads, BlobStore, LocalBlobStore
from nexura_runtime.blobs.gcs import GCSBlobStore
from nexura_runtime.blobs.payloads import EVAL_TRACE_NAMESPACE, RUN_EVENT_NAMESPACE, parse_marker
from nexura_runtime.blobs.retention import (
    REFERENCE_TABLES,
    BlobRetentionRefused,
    ReferenceScan,
    collect_blob_garbage,
    merge_reference_scans,
    require_all_reference_tables,
    sqlite_referenced_keys,
)
from nexura_runtime.cli.main import main
from nexura_runtime.evaluation.models import StoredTrace
from nexura_runtime.evaluation.store import SqliteEvaluationStore
from nexura_runtime.studio.store import StudioStore
from nexura_runtime.testing.blob_store import FakeGCSClient
from nexura_runtime.workflow import WorkflowNode, WorkflowSpec

BIG = "x" * 20_000
DAY = 86_400.0


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "K_SERVICE",
        "CLOUD_RUN_JOB",
        "NEXURA_BLOB_URL",
        "NEXURA_BLOB_PAYLOAD_RETENTION_DAYS",
        "NEXURA_DATABASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)


def _spec() -> WorkflowSpec:
    return WorkflowSpec(name="big", entry_node="n", nodes={"n": WorkflowNode("n", "echo")})


class Backdate:
    """Sets a blob's write time on either store."""

    def __init__(self, store: BlobStore, client: FakeGCSClient | None) -> None:
        self.store = store
        self.client = client

    def __call__(self, key: str, when: float) -> None:
        if isinstance(self.store, LocalBlobStore):
            os.utime(self.store.root / key, (when, when))
        else:
            assert self.client is not None
            self.client.set_updated("bkt", f"p/{key}", when)


@pytest.fixture(params=["local", "gcs"])
def blob_store(
    request: pytest.FixtureRequest, tmp_path: Path
) -> Iterator[tuple[BlobStore, Backdate]]:
    if request.param == "local":
        store: BlobStore = LocalBlobStore(tmp_path / "blobs")
        yield store, Backdate(store, None)
    else:
        client = FakeGCSClient()
        store = GCSBlobStore("bkt", "p", client=client)
        yield store, Backdate(store, client)


@pytest.fixture
def studio(tmp_path: Path, blob_store: tuple[BlobStore, Backdate]) -> Iterator[StudioStore]:
    store = StudioStore(tmp_path / "s.sqlite3", blob_payloads=BlobPayloads(blob_store[0], 8192))
    yield store
    store._close_sync()


def _scan(db: Path) -> Any:
    return lambda: merge_reference_scans([sqlite_referenced_keys(db)])


def _finish(db: Path, run_id: str, status: str = "completed") -> None:
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("UPDATE nexura_studio_runs SET status = ? WHERE run_id = ?", (status, run_id))


def _ensure_eval_table(db: Path) -> None:
    SqliteEvaluationStore(str(db)).close()  # creates eval_traces, as the service does


async def test_unreferenced_old_blobs_go_and_everything_else_stays(
    tmp_path: Path, blob_store: tuple[BlobStore, Backdate], studio: StudioStore
) -> None:
    store, backdate = blob_store
    db = tmp_path / "s.sqlite3"
    _ensure_eval_table(db)
    await studio.record_run_start("r1", None, _spec(), {"doc": BIG}, journal_workspace="ws")
    (referenced,) = [i.key for i in store.list()]
    old_orphan = store.put("ws", RUN_EVENT_NAMESPACE, b"{}", "application/json")
    new_orphan = store.put("ws", EVAL_TRACE_NAMESPACE, b"{}", "application/json")
    now = time.time()
    backdate(referenced, now - 30 * DAY)
    backdate(old_orphan.key, now - 2 * DAY)
    report = collect_blob_garbage(store, _scan(db), grace_seconds=DAY)
    assert report.deleted_unreferenced == 1 and report.deleted_keys == [old_orphan.key]
    assert report.kept_recent_unreferenced == 1 and report.referenced == 1
    assert store.exists(referenced) and store.exists(new_orphan) and not store.exists(old_orphan)
    run = await studio.get_run("r1")
    assert run is not None and run["events"][0]["data"]["initial_state"] == {"doc": BIG}
    # Idempotent: a second run finds nothing more to delete and does not fail.
    again = collect_blob_garbage(store, _scan(db), grace_seconds=DAY)
    assert (again.deleted_unreferenced, again.deleted_expired, again.already_gone) == (0, 0, 0)


async def test_dry_run_deletes_nothing(
    tmp_path: Path, blob_store: tuple[BlobStore, Backdate], studio: StudioStore
) -> None:
    store, backdate = blob_store
    db = tmp_path / "s.sqlite3"
    _ensure_eval_table(db)
    await studio.record_run_start("r1", None, _spec(), {"doc": BIG}, journal_workspace="ws")
    orphan = store.put("ws", RUN_EVENT_NAMESPACE, b"{}", "application/json")
    backdate(orphan.key, time.time() - 2 * DAY)
    report = collect_blob_garbage(store, _scan(db), grace_seconds=DAY, dry_run=True)
    assert report.deleted_keys == [orphan.key] and store.exists(orphan)


def test_concurrent_deletion_counts_as_already_gone(
    tmp_path: Path, blob_store: tuple[BlobStore, Backdate]
) -> None:
    store, backdate = blob_store
    orphan = store.put("ws", RUN_EVENT_NAMESPACE, b"{}", "application/json")
    backdate(orphan.key, time.time() - 2 * DAY)

    class RacingStore:
        """Another retention job deletes the blob between this job's listing and its delete."""

        scheme = store.scheme

        def identity(self, *, create: bool = True) -> Any:
            return store.identity(create=create)

        def describe(self) -> str:
            return store.describe()

        def list(self, prefix: str = "") -> Any:
            for info in store.list(prefix):
                store.delete(info.key)
                yield info

        def delete(self, key: Any) -> bool:
            return store.delete(key)

    report = collect_blob_garbage(
        RacingStore(),  # type: ignore[arg-type]
        frozenset,
        grace_seconds=DAY,
        force=True,  # nothing is referenced here by design
    )
    assert (report.already_gone, report.deleted_unreferenced, report.listed) == (1, 0, 1)


class _PausingStore:
    """Wraps a store: ``put`` signals, then waits until the retention scan has finished."""

    def __init__(self, inner: BlobStore) -> None:
        self.inner = inner
        self.scheme = inner.scheme
        self.put_done = threading.Event()
        self.scan_done = threading.Event()

    def put(self, *args: Any) -> Any:
        ref = self.inner.put(*args)
        self.put_done.set()
        assert self.scan_done.wait(10)
        return ref

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


@pytest.mark.parametrize("grace", [3600.0, 0.0])
async def test_retention_race_row_committed_after_the_scan_started(
    tmp_path: Path, blob_store: tuple[BlobStore, Backdate], grace: float
) -> None:
    """A writer puts its blob, the retention scan runs, then the writer commits its row.

    With a grace period the blob survives and the run reads in full. ``grace=0`` is the negative
    control: the same interleaving then deletes a blob that a committed row references, which
    shows that the grace period is what makes retention safe.
    """
    store, _ = blob_store
    db = tmp_path / "s.sqlite3"
    _ensure_eval_table(db)
    pausing = _PausingStore(store)
    studio = StudioStore(db, blob_payloads=BlobPayloads(pausing, 8192))  # type: ignore[arg-type]
    try:
        # One committed, referenced payload (so the wrong-database checks pass).
        pausing.scan_done.set()
        await studio.record_run_start("r1", None, _spec(), {"doc": BIG}, journal_workspace="ws")
        pausing.put_done.clear()
        pausing.scan_done.clear()
        writer = threading.Thread(
            target=studio._record_run_event_sync, args=("r1", "NODE_DONE", "n", {"out": BIG})
        )
        writer.start()
        assert pausing.put_done.wait(10)

        def scan() -> ReferenceScan:
            found = merge_reference_scans([sqlite_referenced_keys(db)])
            pausing.scan_done.set()  # the writer now commits its row
            writer.join(10)
            return found

        # The blob was put 30 s before retention started (the writer was slow to commit).
        report = collect_blob_garbage(
            store, scan, grace_seconds=grace, clock=lambda: time.time() + 30
        )
        assert not writer.is_alive()
        assert report.referenced == 1  # the scan saw the start event, not the new row
        studio.blob_payloads.clear_cache()  # read the store, as another instance would
        run = await studio.get_run("r1")
        assert run is not None
        data = run["events"][-1]["data"]
        if grace:
            assert report.deleted_unreferenced == 0 and report.kept_recent_unreferenced == 1
            assert data == {"out": BIG}
        else:
            assert report.deleted_unreferenced == 1
            assert data["payload_unavailable"] is True
    finally:
        studio._close_sync()


async def test_retention_under_concurrent_writers_never_loses_a_referenced_blob(
    tmp_path: Path, blob_store: tuple[BlobStore, Backdate]
) -> None:
    store, backdate = blob_store
    db = tmp_path / "s.sqlite3"
    _ensure_eval_table(db)
    studio = StudioStore(db, blob_payloads=BlobPayloads(store, 8192))
    try:
        orphans = [
            store.put("ws", RUN_EVENT_NAMESPACE, b"{}", "application/json") for _ in range(5)
        ]
        for orphan in orphans:
            backdate(orphan.key, time.time() - 2 * DAY)
        await studio.record_run_start("r", None, _spec(), {"doc": BIG}, journal_workspace="ws")
        stop = threading.Event()
        errors: list[BaseException] = []

        def write(n: int) -> None:
            try:
                for i in range(25):
                    studio._record_run_event_sync("r", f"E{n}-{i}", None, {"out": BIG})
            except BaseException as exc:
                errors.append(exc)

        def collect() -> None:
            try:
                while not stop.is_set():
                    collect_blob_garbage(store, _scan(db), grace_seconds=60)
            except BaseException as exc:
                errors.append(exc)

        writers = [threading.Thread(target=write, args=(n,)) for n in range(4)]
        gc = threading.Thread(target=collect)
        gc.start()
        for thread in writers:
            thread.start()
        for thread in writers:
            thread.join(60)
        stop.set()
        gc.join(60)
        assert errors == []
        final = collect_blob_garbage(store, _scan(db), grace_seconds=60)
        assert final.referenced == 101
        assert not any(store.exists(orphan) for orphan in orphans)
        run = await studio.get_run("r")
        assert run is not None
        assert (
            sorted(json.dumps(e["data"]) for e in run["events"][1:])
            == [json.dumps({"out": BIG})] * 100
        )
    finally:
        studio._close_sync()


async def test_payload_expiry_deletes_old_payload_blobs_even_when_referenced(
    tmp_path: Path, blob_store: tuple[BlobStore, Backdate], studio: StudioStore
) -> None:
    store, backdate = blob_store
    db = tmp_path / "s.sqlite3"
    _ensure_eval_table(db)
    await studio.record_run_start("old", None, _spec(), {"doc": BIG}, journal_workspace="ws")
    (old_key,) = [i.key for i in store.list()]
    await studio.record_run_start("pending", None, _spec(), {"doc": BIG}, journal_workspace="ws")
    pending_key = next(i.key for i in store.list() if i.key != old_key)
    await studio.record_run_start("new", None, _spec(), {"doc": BIG}, journal_workspace="ws")
    _finish(db, "old")
    _finish(db, "pending", "pending_approval")
    backdate(old_key, time.time() - 40 * DAY)
    backdate(pending_key, time.time() - 40 * DAY)
    report = collect_blob_garbage(
        store, _scan(db), grace_seconds=DAY, payload_max_age_seconds=30 * DAY
    )
    assert report.deleted_expired == 1 and report.deleted_keys == [old_key]
    # An unfinished run (here: waiting for approval) keeps its payloads, as run retention does.
    assert report.kept_unfinished_runs == 1 and store.exists(pending_key)
    assert studio.blob_payloads is not None
    studio.blob_payloads.clear_cache()  # read as another instance would
    old = await studio.get_run("old")
    new = await studio.get_run("new")
    assert old is not None and new is not None
    assert old["events"][0]["data"]["payload_unavailable"] is True
    assert old["events"][0]["data"]["reason"] == "missing"
    assert old["events"][0]["data"]["preview"].startswith('{"workflow_id"')
    assert new["events"][0]["data"]["initial_state"] == {"doc": BIG}
    # Without a maximum age a referenced payload is never expired.
    backdate([i.key for i in store.list()][0], time.time() - 400 * DAY)
    assert collect_blob_garbage(store, _scan(db), grace_seconds=DAY).deleted_expired == 0


def test_retention_refuses_when_a_reference_table_is_not_scanned(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="at least one database"):
        require_all_reference_tables([])
    db = tmp_path / "s.sqlite3"
    StudioStore(db)._close_sync()  # run events only; eval_traces is missing
    with pytest.raises(ValueError, match="eval_traces"):
        require_all_reference_tables([sqlite_referenced_keys(db)])
    with pytest.raises(FileNotFoundError):
        sqlite_referenced_keys(tmp_path / "typo.sqlite3")
    # The union over sources is what counts.
    keys = require_all_reference_tables(
        [ReferenceScan(frozenset({"a"}), frozenset({"eval_traces"})), sqlite_referenced_keys(db)]
    )
    assert keys == {"a"}
    store = LocalBlobStore(tmp_path / "blobs")
    orphan = store.put("ws", RUN_EVENT_NAMESPACE, b"{}", "application/json")
    os.utime(store.root / orphan.key, (1.0, 1.0))
    with pytest.raises(ValueError, match="eval_traces"):
        collect_blob_garbage(store, _scan(db), grace_seconds=DAY)
    assert store.exists(orphan)  # a refused run deletes nothing


# -- CLI ---------------------------------------------------------------------------------


def _service_db_with_blobs(tmp_path: Path) -> tuple[Path, LocalBlobStore, str, str]:
    db = tmp_path / "service.sqlite3"
    store = LocalBlobStore(tmp_path / "service.sqlite3.blobs")  # the service default folder
    codec = BlobPayloads(store, 8192)
    studio = StudioStore(db, blob_payloads=codec)
    studio._record_run_start_sync("r", None, _spec(), {"doc": BIG}, journal_workspace="ws")
    studio._close_sync()
    _finish(db, "r")
    evals = SqliteEvaluationStore(str(db), blob_payloads=codec)
    evals.save_trace(StoredTrace(run_id="t", timestamp=1.0, output=BIG), "ws")
    evals.close()
    orphan = store.put("ws", RUN_EVENT_NAMESPACE, b"{}", "application/json")
    old = time.time() - 3 * DAY
    for info in store.list():
        os.utime(store.root / info.key, (old, old))
    referenced = next(i.key for i in store.list() if i.key != orphan.key)
    return db, store, orphan.key, referenced


def test_cli_blob_gc_on_sqlite(tmp_path: Path, capsys: Any) -> None:
    db, store, orphan, referenced = _service_db_with_blobs(tmp_path)
    assert main(["db", "blob-gc", "--sqlite", str(db), "--dry-run"]) == 0
    dry = json.loads(capsys.readouterr().out)
    assert dry["deleted_unreferenced"] == 1 and dry["deleted_keys"] == [orphan]
    assert dry["referenced"] == 2 and store.exists(orphan)
    assert main(["db", "blob-gc", "--sqlite", str(db)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["deleted_unreferenced"] == 1 and "deleted_keys" not in out
    assert out["blob_store"] == store.root.as_uri()
    assert not store.exists(orphan) and store.exists(referenced)
    assert main(["db", "blob-gc", "--sqlite", str(db), "--payload-max-age-days", "1"]) == 0
    assert json.loads(capsys.readouterr().out)["deleted_expired"] == 2
    assert list(store.list()) == []


def test_cli_blob_gc_reads_retention_days_from_the_environment(
    tmp_path: Path, capsys: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    db, store, _, _ = _service_db_with_blobs(tmp_path)
    monkeypatch.setenv("NEXURA_BLOB_URL", store.root.as_uri())
    monkeypatch.setenv("NEXURA_BLOB_PAYLOAD_RETENTION_DAYS", "2")
    assert main(["db", "blob-gc", "--sqlite", str(db)]) == 0
    report = json.loads(capsys.readouterr().out)
    # Expiry covers every payload blob older than the limit, the orphan included.
    assert report["deleted_expired"] == 3 and report["deleted_unreferenced"] == 0
    assert list(store.list()) == []


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["--blob-url", "file:///tmp/x"], "Give the databases"),
        ([], "Set NEXURA_BLOB_URL"),
        (["--sqlite", "DB", "--grace-hours", "0.5"], "at least 1"),
        (["--sqlite", "DB", "--payload-max-age-days", "0"], "positive"),
        (["--sqlite", "DB", "--blob-url", "s3://x/y"], "Invalid blob URL"),
        (["--sqlite", "MISSING"], "does not exist"),
    ],
)
def test_cli_blob_gc_refusals_delete_nothing(
    tmp_path: Path, capsys: Any, argv: list[str], message: str
) -> None:
    db, store, orphan, _ = _service_db_with_blobs(tmp_path)
    argv = [
        str(db) if a == "DB" else str(tmp_path / "missing.sqlite3") if a == "MISSING" else a
        for a in argv
    ]
    if "MISSING" in argv or str(tmp_path / "missing.sqlite3") in argv:
        argv += ["--blob-url", store.root.as_uri()]
    assert main(["db", "blob-gc", *argv]) == 2
    assert message in capsys.readouterr().err
    assert store.exists(orphan)


def test_cli_refuses_a_database_without_the_reference_tables(tmp_path: Path, capsys: Any) -> None:
    db, store, orphan, referenced = _service_db_with_blobs(tmp_path)
    empty = tmp_path / "other.sqlite3"
    sqlite3.connect(empty).close()
    code = main(["db", "blob-gc", "--sqlite", str(empty), "--blob-url", store.root.as_uri()])
    assert code == 2 and "nexura_studio_run_events" in capsys.readouterr().err
    assert store.exists(orphan) and store.exists(referenced)


# -- Postgres reference scan (real database) -----------------------------------------------


def _postgres_tables(url: str, rows: dict[str, list[str]]) -> None:
    from nexura_postgres import Database

    db = Database.from_url(url)

    async def create(conn: Any) -> None:
        await conn.execute(
            "CREATE TABLE nexura_studio_run_events (id BIGINT GENERATED ALWAYS AS IDENTITY, "
            "run_id TEXT NOT NULL, data_json TEXT NOT NULL)"
        )
        await conn.execute(
            "CREATE TABLE eval_traces (run_id TEXT, workspace_id TEXT, trace_json TEXT)"
        )
        for value in rows.get("events", []):
            await conn.execute(
                "INSERT INTO nexura_studio_run_events (run_id, data_json) VALUES ('r', $1)", value
            )
        for value in rows.get("traces", []):
            await conn.execute("INSERT INTO eval_traces VALUES ('t', 'ws', $1)", value)

    try:
        db.run(create)
    finally:
        db.close()


def test_postgres_reference_scan(service_database_url: str, tmp_path: Path) -> None:
    from nexura_postgres import Database, referenced_blob_keys

    codec = BlobPayloads(LocalBlobStore(tmp_path / "blobs"), 100)
    event = codec.encode("ws", RUN_EVENT_NAMESPACE, json.dumps({"t": "e" * 500}))
    trace = codec.encode("ws", EVAL_TRACE_NAMESPACE, json.dumps({"t": "t" * 500}))
    db = Database.from_url(service_database_url)
    try:
        before = db.run(referenced_blob_keys)
        assert before.tables == frozenset() and before.keys == frozenset()
        _postgres_tables(
            service_database_url,
            {"events": [event, '{"small": 1}', '{"$nexura_blob": "broken"}'], "traces": [trace]},
        )
        scan = db.run(referenced_blob_keys)
    finally:
        db.close()
    assert scan.tables == REFERENCE_TABLES
    expected = {parse_marker(v)[0].key for v in (event, trace)}  # type: ignore[index]
    assert scan.keys == expected


def test_cli_blob_gc_on_postgres(
    service_database_url: str, tmp_path: Path, capsys: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = LocalBlobStore(tmp_path / "blobs")
    codec = BlobPayloads(store, 100)
    kept = codec.encode("ws", RUN_EVENT_NAMESPACE, json.dumps({"t": "k" * 500}))
    orphan = store.put("ws", RUN_EVENT_NAMESPACE, b"{}", "application/json")
    old = time.time() - 3 * DAY
    for info in store.list():
        os.utime(store.root / info.key, (old, old))
    monkeypatch.setenv("NEXURA_DATABASE_URL", service_database_url)
    argv = ["db", "blob-gc", "--blob-url", store.root.as_uri()]
    # A database without the reference tables (never migrated) is refused. The service's
    # own tables are covered in test_wp8_stores_contract.py (MVP WP8).
    assert main(argv) == 2
    assert "were not found" in capsys.readouterr().err
    assert store.exists(orphan)
    _postgres_tables(service_database_url, {"events": [kept]})
    assert main(argv) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["deleted_unreferenced"] == 1 and report["referenced"] == 1
    assert not store.exists(orphan)
    assert store.exists(parse_marker(kept)[0])  # type: ignore[index]


# -- Wrong-database protection, finite arguments, column types (review M1, M2, L2) ---------


def _dev_db(tmp_path: Path) -> Path:
    """A database with the service tables and no rows (a fresh dev or staging database)."""
    dev = tmp_path / "dev.sqlite3"
    StudioStore(dev)._close_sync()
    _ensure_eval_table(dev)
    return dev


def test_cli_refuses_a_database_that_does_not_belong_to_the_store(
    tmp_path: Path, capsys: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The review's repro: prod's bucket with a dev database must never delete prod payloads."""
    db, store, orphan, referenced = _service_db_with_blobs(tmp_path)
    dev = _dev_db(tmp_path)
    prod_store = ["--blob-url", store.root.as_uri()]
    # 1. An empty dev database (no references at all).
    assert main(["db", "blob-gc", "--sqlite", str(dev), *prod_store, "--dry-run"]) == 2
    assert "not bound to blob store" in capsys.readouterr().err
    # 2. The same through NEXURA_BLOB_URL from the environment.
    monkeypatch.setenv("NEXURA_BLOB_URL", store.root.as_uri())
    assert main(["db", "blob-gc", "--sqlite", str(dev)]) == 2
    err = capsys.readouterr().err
    assert "from the environment" in err and "hold no reference" in err
    assert "not bound to blob store" in err
    monkeypatch.delenv("NEXURA_BLOB_URL")
    # 3. A dev database with its own payloads in another store.
    dev_codec = BlobPayloads(LocalBlobStore(tmp_path / "dev.sqlite3.blobs"), 8192)
    dev_studio = StudioStore(dev, blob_payloads=dev_codec)
    dev_studio._record_run_start_sync("d", None, _spec(), {"doc": BIG}, journal_workspace="ws")
    dev_studio._close_sync()
    assert main(["db", "blob-gc", "--sqlite", str(dev), *prod_store]) == 2
    assert "are bound to blob store" in capsys.readouterr().err  # the dev store, not prod's
    assert store.exists(orphan) and store.exists(referenced)
    run = StudioStore(db, blob_payloads=BlobPayloads(store, 8192))
    try:
        data = run._get_run_sync("r")["events"][0]["data"]  # type: ignore[index]
        assert data["initial_state"] == {"doc": BIG}  # prod payload intact
    finally:
        run._close_sync()
    # 4. --force is the explicit override (dry run here: it would delete everything).
    assert main(["db", "blob-gc", "--sqlite", str(dev), *prod_store, "--dry-run", "--force"]) == 0
    assert json.loads(capsys.readouterr().out)["deleted_unreferenced"] == 3
    # 5. The right database still works.
    assert main(["db", "blob-gc", "--sqlite", str(db), *prod_store]) == 0
    assert json.loads(capsys.readouterr().out)["deleted_unreferenced"] == 1


def test_retention_refuses_a_store_without_identity_or_references(tmp_path: Path) -> None:
    store = LocalBlobStore(tmp_path / "blobs")
    orphan = store.put("ws", RUN_EVENT_NAMESPACE, b"{}", "application/json")
    os.utime(store.root / orphan.key, (1.0, 1.0))
    assert store.identity(create=False) is None  # written without the codec: no identity yet
    db = _dev_db(tmp_path)
    with pytest.raises(BlobRetentionRefused, match="id unknown"):
        collect_blob_garbage(store, _scan(db), grace_seconds=DAY)
    with pytest.raises(BlobRetentionRefused, match="none of the 1 blobs"):
        collect_blob_garbage(store, frozenset, grace_seconds=DAY)  # plain keys: breaker only
    assert store.exists(orphan)
    report = collect_blob_garbage(store, _scan(db), grace_seconds=DAY, force=True)
    assert report.deleted_unreferenced == 1
    # An empty store needs no references.
    assert collect_blob_garbage(store, _scan(db), grace_seconds=DAY).listed == 0


@pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
def test_cli_refuses_non_finite_ages(
    tmp_path: Path, capsys: Any, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    db, store, orphan, _ = _service_db_with_blobs(tmp_path)
    fresh = store.put("ws", RUN_EVENT_NAMESPACE, b"{}", "application/json")
    assert main(["db", "blob-gc", "--sqlite", str(db), f"--grace-hours={value}"]) == 2
    assert "finite" in capsys.readouterr().err
    days = ["--sqlite", str(db), f"--payload-max-age-days={value}"]
    assert main(["db", "blob-gc", *days]) == 2
    monkeypatch.setenv("NEXURA_BLOB_PAYLOAD_RETENTION_DAYS", value)
    assert main(["db", "blob-gc", "--sqlite", str(db)]) == 2
    assert store.exists(orphan) and store.exists(fresh)
    for kwargs in ({"grace_seconds": float(value)}, {"payload_max_age_seconds": float(value)}):
        with pytest.raises(ValueError, match="finite"):
            collect_blob_garbage(store, _scan(db), **kwargs)  # type: ignore[arg-type]


def test_store_identity_is_stable_and_recorded_in_references(tmp_path: Path) -> None:
    store = LocalBlobStore(tmp_path / "blobs")
    first = store.identity()
    assert first is not None and first.startswith("bs_")
    assert LocalBlobStore(tmp_path / "blobs").identity() == first  # another instance reads it
    assert [i.key for i in store.list()] == []  # the identity file is never listed as a blob
    client = FakeGCSClient()
    a, b = GCSBlobStore("bkt", "p", client=client), GCSBlobStore("bkt", "p", client=client)
    assert a.identity(create=False) is None
    assert a.identity() == b.identity() and list(a.list()) == []
    codec = BlobPayloads(store, 10)
    stored = codec.encode("ws", RUN_EVENT_NAMESPACE, json.dumps({"x": "y" * 50}))
    assert json.loads(stored)["store"] == first


def test_postgres_scan_refuses_a_jsonb_reference_column(
    service_database_url: str, tmp_path: Path, capsys: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nexura_postgres import Database, referenced_blob_keys

    store = LocalBlobStore(tmp_path / "blobs")
    codec = BlobPayloads(store, 100)
    marker = codec.encode("ws", RUN_EVENT_NAMESPACE, json.dumps({"t": "j" * 500}))
    db = Database.from_url(service_database_url)

    async def create(conn: Any) -> None:
        await conn.execute(
            "CREATE TABLE nexura_studio_run_events (run_id TEXT, data_json JSONB NOT NULL)"
        )
        await conn.execute("CREATE TABLE eval_traces (run_id TEXT, trace_json TEXT)")
        await conn.execute("INSERT INTO nexura_studio_run_events VALUES ('r', $1::jsonb)", marker)
        # JSONB reorders the keys, so the stored text no longer starts with the marker prefix.
        reordered = await conn.fetchval("SELECT data_json::text FROM nexura_studio_run_events")
        assert not reordered.startswith('{"$nexura_blob"')

    try:
        db.run(create)
        with pytest.raises(BlobRetentionRefused, match="must be stored as TEXT"):
            db.run(referenced_blob_keys)
    finally:
        db.close()
    os.utime(store.root / parse_marker(marker)[0].key, (1.0, 1.0))  # type: ignore[index]
    monkeypatch.setenv("NEXURA_DATABASE_URL", service_database_url)
    assert main(["db", "blob-gc", "--blob-url", store.root.as_uri()]) == 2
    assert "must be stored as TEXT" in capsys.readouterr().err
    assert store.exists(parse_marker(marker)[0])  # type: ignore[index]


def test_postgres_scan_reports_stores_and_unfinished_runs(
    service_database_url: str, tmp_path: Path
) -> None:
    from nexura_postgres import Database, referenced_blob_keys

    codec = BlobPayloads(LocalBlobStore(tmp_path / "blobs"), 100)
    done = codec.encode("ws", RUN_EVENT_NAMESPACE, json.dumps({"t": "d" * 500}))
    pending = codec.encode("ws", RUN_EVENT_NAMESPACE, json.dumps({"t": "p" * 500}))
    _postgres_tables(service_database_url, {})
    db = Database.from_url(service_database_url)

    async def fill(conn: Any) -> None:
        await conn.execute("CREATE TABLE nexura_studio_runs (run_id TEXT, status TEXT)")
        await conn.execute(
            "INSERT INTO nexura_studio_runs VALUES ('a', 'completed'), ('b', 'pending_approval')"
        )
        for run_id, value in (("a", done), ("b", pending)):
            await conn.execute(
                "INSERT INTO nexura_studio_run_events (run_id, data_json) VALUES ($1, $2)",
                run_id,
                value,
            )

    try:
        db.run(fill)
        scan = db.run(referenced_blob_keys)
    finally:
        db.close()
    assert scan.stores == {codec.store.identity()}
    assert scan.protected == {parse_marker(pending)[0].key}  # type: ignore[index]
    assert len(scan.keys) == 2


# -- The database binding (follow-up N1) ---------------------------------------------------


def _empty_like_run_retention(db: Path) -> None:
    """Delete every row that held a reference, as run retention (and trace cleanup) would."""
    with closing(sqlite3.connect(db)) as conn, conn:
        for table in ("nexura_studio_run_events", "nexura_studio_runs", "eval_traces"):
            conn.execute(f"DELETE FROM {table}")


def test_blob_gc_proceeds_on_the_bound_database_after_run_retention(
    tmp_path: Path, capsys: Any
) -> None:
    """The round-2 repro: retention removed every offloaded run; the right database must pass."""
    db, store, orphan, referenced = _service_db_with_blobs(tmp_path)
    from nexura_runtime.blobs.binding import SQLiteBlobBinding

    assert SQLiteBlobBinding(db).read() == store.identity()  # written at the first offload
    _empty_like_run_retention(db)
    assert main(["db", "blob-gc", "--sqlite", str(db)]) == 0  # no --force
    report = json.loads(capsys.readouterr().out)
    assert report["bound"] is True and report["referenced"] == 0
    assert report["deleted_unreferenced"] == 3
    assert list(store.list()) == []
    # The binding still refuses another store, and an unbound database without markers.
    other = LocalBlobStore(tmp_path / "other")
    leftover = other.put("ws", RUN_EVENT_NAMESPACE, b"{}", "application/json")
    other.identity()
    os.utime(other.root / leftover.key, (1.0, 1.0))
    assert main(["db", "blob-gc", "--sqlite", str(db), "--blob-url", other.root.as_uri()]) == 2
    assert "are bound to blob store" in capsys.readouterr().err
    dev = _dev_db(tmp_path)
    assert main(["db", "blob-gc", "--sqlite", str(dev), "--blob-url", other.root.as_uri()]) == 2
    assert "not bound" in capsys.readouterr().err
    assert other.exists(leftover)


def test_binding_is_written_once_and_never_rebound(tmp_path: Path, caplog: Any) -> None:
    from nexura_runtime.blobs.binding import BlobBindingError, SQLiteBlobBinding

    db = tmp_path / "s.sqlite3"
    first = LocalBlobStore(tmp_path / "a")
    studio = StudioStore(db, blob_payloads=BlobPayloads(first, 8192))
    try:
        assert SQLiteBlobBinding(db).read() is None  # nothing offloaded yet: unbound
        studio._record_run_start_sync("r", None, _spec(), {"doc": BIG}, journal_workspace="ws")
        assert SQLiteBlobBinding(db).read() == first.identity()
    finally:
        studio._close_sync()
    # The same database with another store: the payload stays inline (plain SQLite: a warning).
    second = BlobPayloads(LocalBlobStore(tmp_path / "b"), 8192)
    other = StudioStore(db, blob_payloads=second)
    try:
        with caplog.at_level("WARNING"):
            other._record_run_start_sync("r2", None, _spec(), {"doc": BIG}, journal_workspace="ws")
        assert second.inline_fallbacks == 1 and list(second.store.list()) == []
        assert "stored inline" in caplog.text
        with pytest.raises(BlobBindingError, match="bound to blob store"):
            second.check_ready()
    finally:
        other._close_sync()
    assert SQLiteBlobBinding(db).read() == first.identity()


def test_postgres_binding_and_scan(service_database_url: str, tmp_path: Path, capsys: Any) -> None:
    from nexura_postgres import Database, PostgresBlobBinding, referenced_blob_keys

    from nexura_runtime.blobs.binding import BlobBindingError

    store = LocalBlobStore(tmp_path / "blobs")
    orphan = store.put("ws", RUN_EVENT_NAMESPACE, b"{}", "application/json")
    os.utime(store.root / orphan.key, (1.0, 1.0))
    database = Database.from_url(service_database_url)
    try:
        binding = PostgresBlobBinding(database)
        assert binding.read() is None
        binding.ensure(str(store.identity()), store.describe())
        binding.ensure(str(store.identity()), store.describe())  # idempotent
        assert binding.read() == store.identity()
        with pytest.raises(BlobBindingError):
            binding.ensure(str(LocalBlobStore(tmp_path / "x").identity()), "x")
        _postgres_tables(service_database_url, {})  # reference tables with no rows
        scan = database.run(referenced_blob_keys)
    finally:
        database.close()
    assert scan.bindings == {store.identity()} and scan.keys == frozenset()
    # Bound and empty (after retention): blob-gc on Postgres proceeds without --force.
    argv = ["db", "blob-gc", "--database-url", service_database_url, "--blob-url"]
    assert main([*argv, store.root.as_uri()]) == 0
    assert json.loads(capsys.readouterr().out)["deleted_unreferenced"] == 1
