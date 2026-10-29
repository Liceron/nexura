# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Local memory/SQLite retention contracts and disposable Postgres twins.

Postgres cases follow WP8's service_database_url fixture; the required profile is
--service-backend=postgres. No HTTP clients, provider calls or external blob stores.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from argparse import Namespace
from collections.abc import Iterator
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_wp8_stores_contract import _Backend, _race, _receipt, _summary

from nexura_runtime.blobs import BlobPayloads, LocalBlobStore
from nexura_runtime.blobs.payloads import parse_marker
from nexura_runtime.blobs.retention import collect_blob_garbage, sqlite_referenced_keys
from nexura_runtime.cli.database import prune_command
from nexura_runtime.cli.main import build_parser
from nexura_runtime.evaluation.models import StoredTrace
from nexura_runtime.evaluation.store import MemoryEvaluationStore, SqliteEvaluationStore
from nexura_runtime.integrations.channels.outbox import MemoryChannelStore, SqliteChannelStore
from nexura_runtime.store_retention import MIN_DEDUP_RETENTION_SECONDS

DAY = 86400.0
AGE = MIN_DEDUP_RETENTION_SECONDS
NOW = AGE + 100.0


@pytest.fixture(params=["memory", "sqlite", "postgres"])
def retention(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Any]:
    if request.param == "memory":
        evals = MemoryEvaluationStore()
        yield SimpleNamespace(kind="memory", channels=MemoryChannelStore(), evals=evals)
        evals.close()
        return
    backend = _Backend(request.param, tmp_path, request)
    try:
        yield SimpleNamespace(
            kind=request.param,
            channels=backend.channels(),
            evals=backend.evaluations(),
            backend=backend,
        )
    finally:
        backend.close()


def _dedup(store: Any, key: str, stamp: float, monkeypatch: pytest.MonkeyPatch) -> None:
    import nexura_runtime.integrations.channels.outbox as local

    monkeypatch.setattr(local, "time", SimpleNamespace(time=lambda: stamp))
    if store.blocking_io:
        import nexura_postgres.channels as pg_channels

        monkeypatch.setattr(pg_channels, "time", SimpleNamespace(time=lambda: stamp))
    assert store.record_dedup(key)


def test_channel_cutoff_and_independent_ages(
    retention: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = retention.channels
    for key, stamp in (("old", 99.0), ("boundary", 100.0), ("new", 101.0)):
        _dedup(store, key, stamp, monkeypatch)
        store.record_receipt(_receipt(key, stamp))
    assert store.prune(AGE, now=NOW, dry_run=True) == {"dedup_keys": 1, "outbox_receipts": 1}
    assert all(store.is_duplicate(key) for key in ("old", "boundary", "new"))
    assert len(store.list_outbox()) == 3
    assert store.prune(AGE, now=NOW, outbox=False) == {"dedup_keys": 1, "outbox_receipts": 0}
    assert not store.is_duplicate("old")
    assert store.is_duplicate("boundary") and store.is_duplicate("new")
    assert len(store.list_outbox()) == 3
    # Receipt retention may be shorter than the dedup safety minimum, independently.
    assert store.prune(1, now=101, dedup=False) == {"dedup_keys": 0, "outbox_receipts": 1}
    assert [r.receipt_id for r in store.list_outbox()] == ["boundary", "new"]
    assert store.prune(AGE, now=NOW) == {"dedup_keys": 0, "outbox_receipts": 0}


def test_channel_batches_are_bounded(retention: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    store = retention.channels
    for i in range(5):
        _dedup(store, f"k{i}", 1, monkeypatch)
        store.record_receipt(_receipt(f"r{i}", 1))
    expected = store.prune(AGE, now=NOW, limit=3, dry_run=True)
    assert sum(expected.values()) == 3
    assert len(store.list_outbox()) == 5
    assert store.prune(AGE, now=NOW, limit=3) == expected
    total = sum(expected.values())
    while counts := store.prune(AGE, now=NOW, limit=3):
        n = sum(counts.values())
        assert n <= 3
        total += n
        if n == 0:
            break
    assert total == 10
    assert store.list_outbox() == []


def test_channel_batches_select_oldest_before_limit(
    retention: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = retention.channels
    for key, stamp in (("later", 20), ("earlier", 10)):
        _dedup(store, key, stamp, monkeypatch)
        store.record_receipt(_receipt(key, stamp))
    assert store.prune(AGE, now=NOW, limit=1, dry_run=True) == {
        "dedup_keys": 1,
        "outbox_receipts": 0,
    }
    assert all(store.is_duplicate(key) for key in ("later", "earlier"))
    assert {r.receipt_id for r in store.list_outbox()} == {"later", "earlier"}
    assert store.prune(AGE, now=NOW, limit=1, outbox=False) == {
        "dedup_keys": 1,
        "outbox_receipts": 0,
    }
    assert not store.is_duplicate("earlier")
    assert store.is_duplicate("later")
    assert store.prune(AGE, now=NOW, limit=1, dedup=False, dry_run=True) == {
        "dedup_keys": 0,
        "outbox_receipts": 1,
    }
    assert {r.receipt_id for r in store.list_outbox()} == {"later", "earlier"}
    assert store.prune(AGE, now=NOW, limit=1, dedup=False) == {
        "dedup_keys": 0,
        "outbox_receipts": 1,
    }
    assert [r.receipt_id for r in store.list_outbox()] == ["later"]


@pytest.mark.parametrize("age", [AGE - 1, 1, 0, -1, float("nan"), float("inf")])
def test_refuses_unsafe_dedup_retention(retention: Any, age: float) -> None:
    assert retention.channels.record_dedup("protected")
    with pytest.raises(ValueError):
        retention.channels.prune(age, now=NOW)
    assert retention.channels.is_duplicate("protected")


@pytest.mark.parametrize("limit", [0, -1, 10001, True, 1.5])
def test_invalid_batch_limit_is_refused(retention: Any, limit: Any) -> None:
    for store in (retention.channels, retention.evals):
        with pytest.raises(ValueError, match="limit"):
            store.prune(AGE, now=NOW, limit=limit)


@pytest.mark.parametrize("now", [float("nan"), float("inf")])
def test_nonfinite_clock_is_refused(retention: Any, now: float) -> None:
    for store in (retention.channels, retention.evals):
        with pytest.raises(ValueError, match="finite"):
            store.prune(AGE, now=now)


def test_longer_delivery_retry_window(retention: Any) -> None:
    store = retention.channels
    if retention.kind == "postgres":
        from nexura_postgres import PostgresChannelStore

        store = PostgresChannelStore(store.database, delivery_retry_window_seconds=14 * DAY)
    elif retention.kind == "sqlite":
        store = SqliteChannelStore(
            str(retention.backend.path), delivery_retry_window_seconds=14 * DAY
        )
    else:
        store = MemoryChannelStore(delivery_retry_window_seconds=14 * DAY)
    try:
        with pytest.raises(ValueError, match="retry window"):
            store.prune(AGE, now=NOW)
        assert sum(store.prune(14 * DAY, now=NOW).values()) == 0
    finally:
        if hasattr(store, "close"):
            store.close()


def test_evaluation_cutoff_and_batches(retention: Any) -> None:
    store = retention.evals
    for run_id, stamp in (("old", 99), ("boundary", 100), ("new", 101)):
        store.save_run(_summary(run_id, stamp), "ws")
        store.save_trace(StoredTrace(run_id=run_id, timestamp=stamp), "ws")
    # Imports need no summary. A different workspace's summary cannot change their age.
    store.save_run(_summary("standalone", 200), "elsewhere")
    store.save_trace(StoredTrace(run_id="standalone", timestamp=2), "ws")
    # The summary is authoritative when it exists, even if the trace's timestamp differs.
    store.save_run(_summary("recent_run", 200), "ws")
    store.save_trace(StoredTrace(run_id="recent_run", timestamp=1), "ws")
    assert store.prune(AGE, now=NOW, limit=1, dry_run=True) == {"eval_traces": 1}
    assert len(store.list_traces("ws")) == 5
    assert store.prune(AGE, now=NOW, limit=1) == {"eval_traces": 1}
    assert store.prune(AGE, now=NOW, limit=1) == {"eval_traces": 1}
    assert store.prune(AGE, now=NOW, limit=1) == {"eval_traces": 0}
    assert {t.run_id for t in store.list_traces("ws")} == {"boundary", "new", "recent_run"}
    assert store.get_run("old", "ws") is not None  # summaries are kept
    # There is deliberately no newest-N guard, including for a dataset with only one run.
    store.save_run(_summary("only_run", 1), "one")
    store.save_trace(StoredTrace(run_id="only_run", timestamp=1), "one")
    assert store.prune(AGE, now=NOW) == {"eval_traces": 1}
    assert store.get_run("only_run", "one") is not None


def test_evaluation_batches_merge_oldest_timestamp_sources(retention: Any) -> None:
    store = retention.evals
    for run_id, run_stamp, trace_stamp in (("run40", 40, 1), ("run10", 10, 90)):
        store.save_run(_summary(run_id, run_stamp), "ws")
        store.save_trace(StoredTrace(run_id=run_id, timestamp=trace_stamp), "ws")
    for run_id, stamp in (("trace30", 30), ("trace20", 20), ("other_workspace", 60)):
        store.save_trace(StoredTrace(run_id=run_id, timestamp=stamp), "ws")
    store.save_run(_summary("other_workspace", 1), "elsewhere")
    store.save_run(_summary("recent_run", 200), "ws")
    store.save_trace(StoredTrace(run_id="recent_run", timestamp=1), "ws")
    store.save_trace(StoredTrace(run_id="boundary", timestamp=100), "ws")
    remaining = {t.run_id for t in store.list_traces("ws")}
    for oldest in ("run10", "trace20", "trace30", "run40", "other_workspace"):
        assert store.prune(AGE, now=NOW, limit=1, dry_run=True) == {"eval_traces": 1}
        assert {t.run_id for t in store.list_traces("ws")} == remaining
        assert store.prune(AGE, now=NOW, limit=1) == {"eval_traces": 1}
        remaining.remove(oldest)
        assert {t.run_id for t in store.list_traces("ws")} == remaining
    assert remaining == {"recent_run", "boundary"}
    assert store.prune(AGE, now=NOW, limit=1) == {"eval_traces": 0}
    assert store.get_run("run10", "ws") is not None
    assert store.get_run("run40", "ws") is not None


@pytest.mark.parametrize("dry_run", [False, True])
def test_sqlite_trace_retention_uses_indexes(tmp_path: Path, dry_run: bool) -> None:
    store = SqliteEvaluationStore(str(tmp_path / "plans.sqlite3"))
    try:
        for i in range(128):
            run_id = f"recent{i}"
            store.save_run(_summary(run_id, 200 + i), "ws")
            store.save_trace(StoredTrace(run_id=run_id, timestamp=200 + i), "ws")
        store.save_run(_summary("summary", 10), "ws")
        store.save_trace(StoredTrace(run_id="summary", timestamp=200), "ws")
        store.save_trace(StoredTrace(run_id="standalone", timestamp=20), "ws")
        statements: list[str] = []
        store._conn.set_trace_callback(statements.append)
        assert store.prune(AGE, now=NOW, limit=1, dry_run=dry_run) == {"eval_traces": 1}
        store._conn.set_trace_callback(None)
        queries = [
            sql
            for sql in statements
            if sql.startswith(("DELETE FROM eval_traces", "SELECT count(*)"))
        ]
        assert len(queries) == 1
        plan = [row[3] for row in store._conn.execute("EXPLAIN QUERY PLAN " + queries[0])]
        assert not any(detail.startswith(("SCAN t", "SCAN eval_traces")) for detail in plan), plan
        assert any("SEARCH r USING COVERING INDEX idx_eval_run_retention" in d for d in plan), plan
        assert any("SEARCH t USING INDEX idx_eval_trace_retention" in d for d in plan), plan
        assert any("SEARCH t USING COVERING INDEX sqlite_autoindex_eval_traces" in d for d in plan)
    finally:
        store.close()


@pytest.mark.parametrize("dry_run", [False, True])
def test_postgres_trace_retention_uses_indexes(
    service_database_url: str, monkeypatch: pytest.MonkeyPatch, dry_run: bool
) -> None:
    from nexura_postgres import Database, PostgresEvaluationStore

    db = Database.from_url(service_database_url)
    try:
        store = PostgresEvaluationStore(db)
        store.save_run(_summary("summary", 10), "ws")
        store.save_trace(StoredTrace(run_id="summary", timestamp=200), "ws")
        store.save_trace(StoredTrace(run_id="standalone", timestamp=20), "ws")
        plans: list[Any] = []
        database_run = db.run

        def explain_prune(fn: Any) -> Any:
            async def run(conn: Any) -> Any:
                await conn.execute("SET LOCAL enable_seqscan = off")

                class ExplainConnection:
                    async def fetchval(self, query: str, *args: Any) -> Any:
                        plan = await conn.fetchval("EXPLAIN (FORMAT JSON) " + query, *args)
                        plans.append(json.loads(plan)[0]["Plan"])
                        return await conn.fetchval(query, *args)

                return await fn(ExplainConnection())

            return database_run(run)

        monkeypatch.setattr(db, "run", explain_prune)
        assert store.prune(AGE, now=NOW, limit=1, dry_run=dry_run) == {"eval_traces": 1}
        assert len(plans) == 1

        def nodes(plan: Any) -> Iterator[Any]:
            yield plan
            for child in plan.get("Plans", []):
                yield from nodes(child)

        steps = list(nodes(plans[0]))
        assert not any(
            s["Node Type"] == "Seq Scan" and s.get("Relation Name") == "eval_traces" for s in steps
        ), plans
        indexes = {s.get("Index Name") for s in steps}
        assert "eval_run_retention" in indexes, plans
        assert "eval_trace_retention" in indexes, plans
    finally:
        db.close()


def test_pruning_trace_never_touches_its_blob(retention: Any, tmp_path: Path) -> None:
    blobs = LocalBlobStore(tmp_path / "blobs")
    codec = BlobPayloads(blobs, 100)
    store = retention.evals
    store.blob_payloads = codec
    trace = StoredTrace(run_id="blob", timestamp=1, output="large" * 1000)
    store.save_trace(trace, "ws")
    (key,) = [item.key for item in blobs.list()]
    if retention.kind == "postgres":
        marker = retention.backend.sql("SELECT trace_json FROM eval_traces")[0][0]
    else:
        marker = store._conn.execute("SELECT trace_json FROM eval_traces").fetchone()[0]
    parsed = parse_marker(marker)
    assert parsed is not None and parsed[0].key == key
    ref = parsed[0]
    raw = blobs.get(ref)
    assert store.prune(AGE, now=NOW, dry_run=True) == {"eval_traces": 1}
    assert store.list_traces("ws") == [trace]
    assert store.prune(AGE, now=NOW) == {"eval_traces": 1}
    assert store.list_traces("ws") == []
    assert blobs.exists(key) and blobs.get(ref) == raw
    if retention.kind == "sqlite":
        assert key not in sqlite_referenced_keys(retention.backend.path).keys
    elif retention.kind == "postgres":
        from nexura_postgres import referenced_blob_keys

        assert key not in store.database.run(referenced_blob_keys).keys


def test_two_connections_prune_each_row_once(
    retention: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    if retention.kind == "memory":
        channels = [retention.channels] * 2
        evals = [retention.evals] * 2
    else:
        channels = [retention.channels, retention.backend.channels()]
        evals = [retention.evals, retention.backend.evaluations()]
    for i in range(12):
        _dedup(channels[0], f"k{i}", 1, monkeypatch)
        channels[0].record_receipt(_receipt(f"r{i}", 1))
        evals[0].save_trace(StoredTrace(run_id=f"t{i}", timestamp=1), "ws")

    def drain(store: Any) -> int:
        total = 0
        for _ in range(30):
            counts = store.prune(AGE, now=NOW, limit=3)
            assert sum(counts.values()) <= 3
            total += sum(counts.values())
            if not any(counts.values()):
                return total
        raise AssertionError("prune failed to drain bounded fixture")

    results = _race([lambda s=s: drain(s) for s in channels + evals])
    assert all(isinstance(n, int) for n in results), results
    assert sum(results[:2]) == 24 and sum(results[2:]) == 12
    assert channels[0].list_outbox() == [] and evals[0].list_traces("ws") == []
    assert sum(channels[0].prune(AGE, now=NOW).values()) == 0


def test_sqlite_legacy_schema_upgrade_is_conservative(tmp_path: Path) -> None:
    path = tmp_path / "old.sqlite3"
    store = SqliteEvaluationStore(str(path))
    store.save_run(_summary("known", 1), "ws")
    store.close()
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE eval_traces")
        conn.execute(
            "CREATE TABLE eval_traces (run_id TEXT, workspace_id TEXT, trace_json TEXT, "
            "PRIMARY KEY (run_id, workspace_id))"
        )
        for run_id in ("known", "unknown"):
            conn.execute(
                "INSERT INTO eval_traces VALUES (?, 'ws', ?)",
                (run_id, StoredTrace(run_id=run_id, timestamp=1).model_dump_json()),
            )
    store = SqliteEvaluationStore(str(path))
    try:
        assert store.prune(AGE, now=NOW) == {"eval_traces": 1}
        assert [t.run_id for t in store.list_traces("ws")] == ["unknown"]
        store.save_trace(StoredTrace(run_id="unknown", timestamp=1), "ws")
        assert store.prune(AGE, now=NOW) == {"eval_traces": 1}
    finally:
        store.close()


def _args(path: Path | None, **kw: Any) -> Namespace:
    values = dict(
        database_url=None,
        sqlite=[str(path)] if path else [],
        dedup_days=7,
        outbox_days=1,
        eval_trace_days=1,
        dry_run=False,
        limit=2,
        delivery_retry_days=7,
    )
    values.update(kw)
    return Namespace(**values)


def test_cli_counts_only_then_prunes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    monkeypatch.delenv("NEXURA_DATABASE_URL", raising=False)
    path = tmp_path / "service.sqlite3"
    channels, evals = SqliteChannelStore(str(path)), SqliteEvaluationStore(str(path))
    import nexura_runtime.cli.database as cli

    monkeypatch.setattr(cli, "time", SimpleNamespace(time=lambda: NOW))
    for i in range(3):
        _dedup(channels, f"k{i}", 1, monkeypatch)
        channels.record_receipt(_receipt(f"r{i}", 1))
        evals.save_trace(StoredTrace(run_id=f"t{i}", timestamp=1), "ws")
    try:
        before = path.read_bytes()
        assert prune_command(_args(path, dry_run=True)) == 0
        report = json.loads(capsys.readouterr().out)
        assert report["dry_run"] is True and report["limit_per_category"] == 2
        assert report["counts"] == {"dedup_keys": 2, "outbox_receipts": 2, "eval_traces": 2}
        assert path.read_bytes() == before
        assert len(channels.list_outbox()) == len(evals.list_traces("ws")) == 3
        assert prune_command(_args(path)) == 0
        report = json.loads(capsys.readouterr().out)
        assert report["counts"] == {"dedup_keys": 2, "outbox_receipts": 2, "eval_traces": 2}
        assert len(channels.list_outbox()) == len(evals.list_traces("ws")) == 1
    finally:
        channels.close()
        evals.close()


@pytest.mark.parametrize(
    "kw",
    [
        dict(dedup_days=6),
        dict(outbox_days=0),
        dict(eval_trace_days=-1),
        dict(dedup_days=float("nan")),
        dict(outbox_days=float("inf")),
        dict(limit=0),
        dict(delivery_retry_days=14),
    ],
)
def test_cli_refuses_invalid_policy_before_opening_database(
    kw: dict[str, Any], capsys: Any
) -> None:
    assert prune_command(_args(None, **kw)) == 2
    assert capsys.readouterr().err


def test_cli_requires_existing_database_and_explicit_ages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("NEXURA_DATABASE_URL", raising=False)
    assert prune_command(_args(None)) == 2
    missing = tmp_path / "missing.sqlite3"
    assert prune_command(_args(missing)) == 2
    assert not missing.exists()
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["db", "prune"])
    assert exc.value.code == 2
    args = build_parser().parse_args(
        [
            "db",
            "prune",
            "--dedup-days",
            "7",
            "--outbox-days",
            "1",
            "--eval-trace-days",
            "2",
            "--dry-run",
        ]
    )
    assert args.func is prune_command and args.dry_run


def test_postgres_skips_locked_rows_and_later_deletes_them(
    service_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Force contention rather than relying only on simultaneous thread scheduling."""
    import asyncio

    from nexura_postgres import Database, PostgresChannelStore, PostgresEvaluationStore

    a, b = Database.from_url(service_database_url), Database.from_url(service_database_url)
    locked = threading.Event()
    release: Future[None] = Future()
    future: Any = None
    try:
        ca, cb = PostgresChannelStore(a), PostgresChannelStore(b)
        ea, eb = PostgresEvaluationStore(a), PostgresEvaluationStore(b)
        for key in ("locked", "free"):
            _dedup(ca, key, 1, monkeypatch)
            ca.record_receipt(_receipt(key, 1))
            ea.save_trace(StoredTrace(run_id=key, timestamp=1), "ws")

        async def hold(conn: Any) -> None:
            for table, key in (
                ("channel_dedup", "key"),
                ("channel_outbox", "receipt_id"),
                ("eval_traces", "run_id"),
            ):
                await conn.fetch(f"SELECT 1 FROM {table} WHERE {key} = 'locked' FOR UPDATE")
            locked.set()
            await asyncio.wait_for(asyncio.wrap_future(release), 15)

        future = a.submit(hold)
        assert locked.wait(5)
        assert cb.prune(AGE, now=NOW) == {"dedup_keys": 1, "outbox_receipts": 1}
        assert eb.prune(AGE, now=NOW) == {"eval_traces": 1}
        assert cb.is_duplicate("locked") and not cb.is_duplicate("free")
        assert [t.run_id for t in eb.list_traces("ws")] == ["locked"]
        release.set_result(None)
        future.result(5)
        assert cb.prune(AGE, now=NOW) == {"dedup_keys": 1, "outbox_receipts": 1}
        assert eb.prune(AGE, now=NOW) == {"eval_traces": 1}
    finally:
        if not release.done():
            release.set_result(None)
        try:
            if future is not None:
                future.result(20)
        finally:
            a.close()
            b.close()


def test_postgres_upgrades_wp8_schema_without_changing_old_ledgers(
    service_database_url: str,
) -> None:
    from nexura_postgres import Database, PostgresChannelStore, PostgresEvaluationStore
    from nexura_postgres.channels import COMPONENT as CHANNELS
    from nexura_postgres.channels import MIGRATIONS as CHANNEL_MIGRATIONS
    from nexura_postgres.evaluations import COMPONENT as EVALUATIONS
    from nexura_postgres.evaluations import MIGRATIONS as EVAL_MIGRATIONS
    from nexura_postgres.migrations import migrate_all, migration_status

    db = Database.from_url(service_database_url)
    try:

        async def wp8(conn: Any) -> None:
            await migrate_all(conn, {CHANNELS: CHANNEL_MIGRATIONS, EVALUATIONS: EVAL_MIGRATIONS})
            await conn.execute("INSERT INTO channel_dedup VALUES ('legacy', 1, 'processed')")
            for run_id in ("known", "unknown"):
                await conn.execute(
                    "INSERT INTO eval_traces VALUES ($1, 'ws', $2)",
                    run_id,
                    StoredTrace(run_id=run_id, timestamp=1).model_dump_json(),
                )

        db.run(wp8)
        channels = PostgresChannelStore(db)
        evals = PostgresEvaluationStore(db)
        evals.save_run(_summary("known", 1), "ws")
        assert channels.is_duplicate("legacy")
        assert evals.prune(AGE, now=NOW) == {"eval_traces": 1}
        assert [t.run_id for t in evals.list_traces("ws")] == ["unknown"]
        status = db.run(migration_status)
        assert status["channel_retention"] == status["evaluation_trace_retention"] == "current"

        async def versions(conn: Any) -> Any:
            return await conn.fetch(
                "SELECT component, version FROM nexura_schema_migrations "
                "WHERE component IN ('channels', 'evaluations') ORDER BY component"
            )

        assert [tuple(row) for row in db.run(versions)] == [("channels", 1), ("evaluations", 1)]
        # Reopening is idempotent and preserves legacy payload text.
        assert PostgresEvaluationStore(db).list_traces("ws")[0].timestamp == 1
    finally:
        db.close()


@pytest.mark.parametrize("dry_run", [False, True])
def test_cli_postgres_uses_environment_closes_connection_and_never_migrates(
    monkeypatch: pytest.MonkeyPatch, capsys: Any, dry_run: bool
) -> None:
    import nexura_runtime.modules.optional_packages as optional

    calls: list[Any] = []

    class FakeDatabase:
        @classmethod
        def from_url(cls, url: str) -> Any:
            calls.append(url)
            return cls()

        def run(self, fn: Any) -> dict[str, str]:
            return {
                key: "current"
                for key in (
                    "channels",
                    "evaluations",
                    "channel_retention",
                    "evaluation_trace_retention",
                )
            }

        def close(self) -> None:
            calls.append("closed")

    class FakeChannels:
        def __init__(self, db: Any, **kw: Any) -> None:
            assert kw == {"migrate": False, "delivery_retry_window_seconds": AGE}

        def prune(self, age: float, **kw: Any) -> dict[str, int]:
            calls.append((age, kw))
            assert kw["dry_run"] is dry_run and kw["limit"] == 2
            return {
                "dedup_keys": 1 if kw.get("outbox") is False else 0,
                "outbox_receipts": 1 if kw.get("dedup") is False else 0,
            }

    class FakeEvaluations:
        def __init__(self, db: Any, **kw: Any) -> None:
            assert kw == {"migrate": False}

        def prune(self, age: float, **kw: Any) -> dict[str, int]:
            assert age == DAY and kw["dry_run"] is dry_run
            calls.append((age, kw))
            return {"eval_traces": 1}

    fake = SimpleNamespace(
        Database=FakeDatabase,
        migration_status=object(),
        PostgresChannelStore=FakeChannels,
        PostgresEvaluationStore=FakeEvaluations,
    )
    monkeypatch.setattr(optional, "require_package", lambda *a: fake)
    monkeypatch.setenv("NEXURA_DATABASE_URL", "postgresql://fake.invalid/local-test")
    assert prune_command(_args(None, dry_run=dry_run)) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["counts"] == {"dedup_keys": 1, "outbox_receipts": 1, "eval_traces": 1}
    assert calls[0] == "postgresql://fake.invalid/local-test" and calls[-1] == "closed"
    assert len({call[1]["now"] for call in calls[1:-1]}) == 1


def test_cli_dry_run_preserves_legacy_schema_and_payloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    monkeypatch.delenv("NEXURA_DATABASE_URL", raising=False)
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE eval_traces (run_id TEXT, workspace_id TEXT, trace_json TEXT)")
        conn.execute("CREATE TABLE eval_runs (run_id TEXT, workspace_id TEXT, created_at REAL)")
        conn.execute("INSERT INTO eval_runs VALUES ('known', 'ws', 1)")
        conn.execute("INSERT INTO eval_traces VALUES ('known', 'ws', 'untouched payload')")
        conn.execute("INSERT INTO eval_traces VALUES ('unknown', 'ws', 'untouched legacy payload')")
    before = path.read_bytes()
    assert prune_command(_args(path, dry_run=True)) == 0
    assert json.loads(capsys.readouterr().out)["counts"] == {
        "dedup_keys": 0,
        "outbox_receipts": 0,
        "eval_traces": 1,
    }
    assert path.read_bytes() == before


def test_cli_pending_postgres_migrations_close_without_pruning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    import nexura_runtime.modules.optional_packages as optional

    path = tmp_path / "service.sqlite3"
    channels = SqliteChannelStore(str(path))
    channels.record_receipt(_receipt("old", 1))
    calls: list[str] = []

    class FakeDatabase:
        @classmethod
        def from_url(cls, url: str) -> Any:
            return cls()

        def run(self, fn: Any) -> dict[str, str]:
            return {"evaluations": "behind"}

        def close(self) -> None:
            calls.append("closed")

    monkeypatch.setattr(
        optional,
        "require_package",
        lambda *a: SimpleNamespace(
            Database=FakeDatabase,
            migration_status=object(),
        ),
    )
    monkeypatch.setenv("NEXURA_DATABASE_URL", "postgresql://fake.invalid/local-test")
    try:
        assert prune_command(_args(path)) == 1
        assert "db migrate" in capsys.readouterr().err
        assert channels.get_receipt("old") is not None
        assert calls == ["closed"]
    finally:
        channels.close()


def test_blob_gc_collects_pruned_trace_only_after_its_grace(tmp_path: Path) -> None:
    path = tmp_path / "service.sqlite3"
    blobs = LocalBlobStore(tmp_path / "blobs")
    codec = BlobPayloads(blobs, 100, strict=True)
    store = SqliteEvaluationStore(str(path), blob_payloads=codec)
    try:
        codec.check_ready()
        store.save_trace(StoredTrace(run_id="orphan", timestamp=1, output="large" * 1000), "ws")
        (key,) = [item.key for item in blobs.list()]
        with sqlite3.connect(path) as conn:
            # The second reference table exists in a service DB, without requiring an async
            # StudioStore or a TestClient in this synchronous retention contract.
            conn.execute("CREATE TABLE nexura_studio_run_events (data_json TEXT)")
        assert key in sqlite_referenced_keys(path).keys
        assert store.prune(AGE, now=NOW) == {"eval_traces": 1}
        assert blobs.exists(key) and key not in sqlite_referenced_keys(path).keys
        recent = collect_blob_garbage(blobs, lambda: sqlite_referenced_keys(path))
        assert recent.deleted_keys == [] and blobs.exists(key)
        later = collect_blob_garbage(
            blobs, lambda: sqlite_referenced_keys(path), clock=lambda: time.time() + 3 * DAY
        )
        assert later.deleted_keys == [key] and not blobs.exists(key)
    finally:
        store.close()
