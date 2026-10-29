# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Evaluation, Studio eval, channel and integration-registration store contracts (MVP WP8).

Every parametrised test runs the same assertions against the SQLite stores
(``SqliteEvaluationStore``, ``StudioEvalStore``, ``SqliteChannelStore``,
``SQLiteIntegrationRegistrations``) and the Postgres ones (``PostgresEvaluationStore``,
``PostgresStudioEvalStore``, ``PostgresChannelStore``, ``PostgresIntegrationRegistrations``;
skipped without ``NEXURA_TEST_POSTGRES_URI``). Each Postgres ``open_*`` call is a new instance
with a pool of its own, as another service instance has, so the concurrency tests are several
instances on one database. The guarantees across instances:

* a channel dedup key is recorded exactly once, whichever instance records it, and is a
  duplicate on every other (insert-and-detect-conflict);
* a Studio dataset save is a compare-and-set on its revision; Studio runs list in save order
  (database clock under the workspace lock), and retention keeps exactly the newest runs;
* evaluation runs with equal timestamps list in a stable order (insertion, newest first);
* a tool id is registered for one workspace only; a refused registration stores nothing;
* ``eval_traces.trace_json`` is ``TEXT`` holding the blob codec's text: a large trace goes to
  the blob store, and ``db blob-gc``'s reference scan finds it.

Everything is local: no model, no network beyond the test database.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nexura_runtime.blobs import BlobPayloads, LocalBlobStore
from nexura_runtime.blobs.payloads import EVAL_TRACE_NAMESPACE, MARKER_PREFIX, parse_marker
from nexura_runtime.blobs.retention import collect_blob_garbage
from nexura_runtime.evaluation import studio_store
from nexura_runtime.evaluation.errors import DatasetNotFoundError
from nexura_runtime.evaluation.models import (
    DatasetExample,
    EvaluationDataset,
    EvaluationSummary,
    StoredTrace,
)
from nexura_runtime.integrations.channels.models import OutboxReceipt
from nexura_runtime.service.storage_backend import IntegrationToolTakenError

DAY = 86_400.0


class _Backend:
    """Opens stores of one kind on this test's storage; each call is a new instance."""

    def __init__(self, backend: str, tmp_path: Path, request: pytest.FixtureRequest) -> None:
        self.backend = backend
        self.path = tmp_path / "service.sqlite3"
        self.url = request.getfixturevalue("service_database_url") if backend == "postgres" else ""
        self.databases: list[Any] = []
        self.opened: list[Any] = []

    def database(self) -> Any:
        from nexura_postgres import Database

        database = Database(self.url)  # a pool of its own, as another instance has
        self.databases.append(database)
        return database

    def evaluations(self, codec: Any = None) -> Any:
        if self.backend == "sqlite":
            from nexura_runtime.evaluation.store import SqliteEvaluationStore

            store: Any = SqliteEvaluationStore(str(self.path), blob_payloads=codec)
            self.opened.append(store)
            return store
        from nexura_postgres import PostgresEvaluationStore

        return PostgresEvaluationStore(self.database(), blob_payloads=codec)

    def studio(self) -> Any:
        if self.backend == "sqlite":
            from nexura_runtime.evaluation.studio_store import StudioEvalStore

            return StudioEvalStore(str(self.path))
        from nexura_postgres import PostgresStudioEvalStore

        return PostgresStudioEvalStore(self.database())

    def channels(self) -> Any:
        if self.backend == "sqlite":
            from nexura_runtime.integrations.channels.outbox import SqliteChannelStore

            store: Any = SqliteChannelStore(str(self.path))
            self.opened.append(store)
            return store
        from nexura_postgres import PostgresChannelStore

        return PostgresChannelStore(self.database())

    def registrations(self) -> Any:
        if self.backend == "sqlite":
            from nexura_runtime.service.storage_backend import SQLiteIntegrationRegistrations

            return SQLiteIntegrationRegistrations(self.path)
        from nexura_postgres import PostgresIntegrationRegistrations

        return PostgresIntegrationRegistrations(self.database())

    def sql(self, statement: str, *params: Any) -> list[tuple[Any, ...]]:
        """One statement on the Postgres schema (``$n`` placeholders)."""
        database = self.databases[0] if self.databases else self.database()

        async def run(conn: Any) -> Any:
            return await conn.fetch(statement, *params)

        return [tuple(row) for row in database.run(run)]

    def close(self) -> None:
        for store in self.opened:
            store.close()
        for database in self.databases:
            database.close()


@pytest.fixture
def stores(backend: str, tmp_path: Path, request: pytest.FixtureRequest) -> Iterator[_Backend]:
    opened = _Backend(backend, tmp_path, request)
    try:
        yield opened
    finally:
        opened.close()


@pytest.fixture
def pg(tmp_path: Path, request: pytest.FixtureRequest) -> Iterator[_Backend]:
    opened = _Backend("postgres", tmp_path, request)
    try:
        yield opened
    finally:
        opened.close()


def _race(workers: list[Callable[[], Any]]) -> list[Any]:
    """Run every worker in its own thread at once; results (or exceptions) in order."""
    barrier = threading.Barrier(len(workers))
    results: list[Any] = [None] * len(workers)

    def run(i: int) -> None:
        barrier.wait()
        try:
            results[i] = workers[i]()
        except Exception as exc:  # collected for the assertions
            results[i] = exc

    threads = [threading.Thread(target=run, args=(i,)) for i in range(len(workers))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert not any(t.is_alive() for t in threads)
    return results


def _dataset(dataset_id: str, version: str, created_at: float, text: str = "q") -> Any:
    return EvaluationDataset(
        dataset_id=dataset_id,
        version=version,
        description=f"about {text}",
        created_at=created_at,
        examples=[DatasetExample(example_id="e1", input_data={"q": text}, expected_output="a")],
        metadata={"k": text},
    )


def _summary(run_id: str, timestamp: float, **kw: Any) -> EvaluationSummary:
    return EvaluationSummary(
        run_id=run_id,
        dataset_id="d",
        dataset_version="1",
        timestamp=timestamp,
        total_examples=2,
        passed_examples=1,
        failed_examples=1,
        pass_rate=0.5,
        mean_score=0.25,
        total_latency_ms=12.5,
        total_cost_usd=Decimal("0.0042"),
        **kw,
    )


# ============================================================================ evaluations


def test_evaluation_datasets_contract(stores: _Backend) -> None:
    store = stores.evaluations()
    builtin = store.list_datasets("ws")[0]
    assert builtin["is_builtin"] is True
    assert store.get_dataset(builtin["dataset_id"]).dataset_id == builtin["dataset_id"]
    assert store.delete_dataset(builtin["dataset_id"], "ws") is False
    store.save_dataset(_dataset("d", "1", 100.0, "one"), "ws")
    store.save_dataset(_dataset("d", "2", 100.0, "two"), "ws")  # same time: insertion decides
    store.save_dataset(_dataset("e", "1", 50.0), "ws")
    store.save_dataset(_dataset("d", "9", 999.0), "other")
    latest = store.get_dataset("d", workspace_id="ws")
    assert latest.version == "2" and latest.examples[0].input_data == {"q": "two"}
    first = store.get_dataset("d", "1", "ws")
    assert first.description == "about one" and first.metadata == {"k": "one"}
    assert first.content_hash == _dataset("d", "1", 100.0, "one").content_hash
    listed = [(d["dataset_id"], d["version"]) for d in store.list_datasets("ws")[1:]]
    assert listed == [("d", "2"), ("d", "1"), ("e", "1")]
    assert [d["example_count"] for d in store.list_datasets("ws")[1:]] == [1, 1, 1]
    # A re-save of a version replaces it in place (upsert).
    store.save_dataset(_dataset("d", "1", 200.0, "again"), "ws")
    assert store.get_dataset("d", workspace_id="ws").examples[0].input_data == {"q": "again"}
    with pytest.raises(DatasetNotFoundError):
        store.get_dataset("d", "1", "nobody")
    assert store.delete_dataset("d", "ws") is True
    assert store.delete_dataset("d", "ws") is False
    assert [d["dataset_id"] for d in store.list_datasets("ws")[1:]] == ["e"]
    assert store.get_dataset("d", "9", "other").version == "9"  # another workspace untouched


def test_evaluation_runs_and_traces_contract(stores: _Backend) -> None:
    store = stores.evaluations()
    for run_id in ("r1", "r2", "r3"):
        store.save_run(_summary(run_id, 1000.0), "ws")  # equal timestamps
    store.save_run(_summary("r0", 900.0), "ws")
    store.save_run(_summary("x", 2000.0), "other")
    ids = [r["run_id"] for r in store.list_runs("ws")]
    assert ids == ["r3", "r2", "r1", "r0"]  # newest first, then newest insertion first
    assert [r["run_id"] for r in store.list_runs("ws", limit=2, offset=1)] == ["r2", "r1"]
    row = store.list_runs("ws")[0]
    assert row["total_cost_usd"] == pytest.approx(0.0042) and row["pass_rate"] == 0.5
    assert row["total_examples"] == 2 and row["created_at"] == 1000.0
    full = store.get_run("r2", "ws")
    assert full == _summary("r2", 1000.0)
    assert full is not None and full.total_cost_usd == Decimal("0.0042")
    assert store.get_run("r2", "other") is None
    # A second save of a run id replaces its summary only.
    store.save_run(_summary("r2", 1000.0, metadata={"v": 2}), "ws")
    assert store.get_run("r2", "ws").metadata == {"v": 2}  # type: ignore[union-attr]
    assert [r["run_id"] for r in store.list_runs("ws")] == ids
    # Traces: by run id in byte order, per workspace, replaced on re-import.
    for run_id in ("b", "B", "a"):
        store.save_trace(StoredTrace(run_id=run_id, timestamp=1.0, output=run_id), "ws")
    store.save_trace(StoredTrace(run_id="a", timestamp=2.0, output="new"), "ws")
    store.save_trace(StoredTrace(run_id="z", timestamp=1.0), "other")
    traces = store.list_traces("ws")
    assert [t.run_id for t in traces] == ["B", "a", "b"]
    assert traces[1].output == "new" and traces[1].timestamp == 2.0
    assert [t.run_id for t in store.list_traces("other")] == ["z"]
    reopened = stores.evaluations()  # another instance / process
    assert [t.run_id for t in reopened.list_traces("ws")] == ["B", "a", "b"]
    assert [r["run_id"] for r in reopened.list_runs("ws")] == ids


def test_eval_traces_go_to_the_blob_store(stores: _Backend, tmp_path: Path, caplog: Any) -> None:
    blobs = LocalBlobStore(tmp_path / "blobs")
    codec = BlobPayloads(blobs, 500)
    store = stores.evaluations(codec)
    big = "t" * 5000
    store.save_trace(StoredTrace(run_id="big", timestamp=1.0, output=big), "ws")
    store.save_trace(StoredTrace(run_id="small", timestamp=1.0, output="s"), "ws")
    if stores.backend == "postgres":
        raw = dict(stores.sql("SELECT run_id, trace_json FROM eval_traces"))
    else:
        import sqlite3

        with sqlite3.connect(stores.path) as conn:
            raw = dict(conn.execute("SELECT run_id, trace_json FROM eval_traces").fetchall())
    assert raw["big"].startswith(MARKER_PREFIX) and len(raw["big"]) < 1024
    assert not raw["small"].startswith(MARKER_PREFIX)
    (info,) = list(blobs.list())
    assert info.key.startswith("ws/" + EVAL_TRACE_NAMESPACE + "/")
    other = stores.evaluations(BlobPayloads(blobs, 500))  # another instance reads it
    assert [t.output for t in other.list_traces("ws")] == [big, "s"]
    blobs.delete(info.key)  # payload retention removed it: the trace is skipped and logged
    with caplog.at_level("WARNING"):
        assert [
            t.run_id for t in stores.evaluations(BlobPayloads(blobs, 500)).list_traces("ws")
        ] == ["small"]
    assert "payload blob is missing" in caplog.text


# ============================================================================ Studio evals


def test_studio_eval_datasets_contract(stores: _Backend) -> None:
    store = stores.studio()
    cases = [{"name": "c1", "input": "hi"}]
    made = store.save_dataset(
        "ws", "b", name="B", description="d", cases=cases, content_hash="h1", expected_revision=0
    )
    assert made["revision"] == 1 and made["case_count"] == 1 and made["cases"] == cases
    assert made["workflow_id"] is None and made["created_at"] == made["updated_at"]
    with pytest.raises(ValueError, match="conflict:1"):
        store.save_dataset(
            "ws", "b", name="B", description="d", cases=[], content_hash="h", expected_revision=0
        )
    again = store.save_dataset(
        "ws",
        "b",
        name="B2",
        description="d2",
        cases=cases * 2,
        content_hash="h2",
        expected_revision=1,
        workflow_id="wf",
    )
    assert again["revision"] == 2 and again["name"] == "B2" and again["workflow_id"] == "wf"
    assert again["created_at"] == made["created_at"]
    with pytest.raises(ValueError, match="conflict:2"):
        store.save_dataset(
            "ws", "b", name="B", description="", cases=[], content_hash="h", expected_revision=1
        )
    with pytest.raises(ValueError, match="conflict:0"):
        store.save_dataset(
            "ws", "new", name="N", description="", cases=[], content_hash="h", expected_revision=3
        )
    store.save_dataset(
        "ws", "A", name="A", description="", cases=[], content_hash="h", expected_revision=None
    )
    store.save_dataset(
        "other", "z", name="Z", description="", cases=[], content_hash="h", expected_revision=None
    )
    assert [d["dataset_id"] for d in store.list_datasets("ws")] == ["A", "b"]  # byte order
    assert [d["dataset_id"] for d in store.list_datasets("ws", "wf")] == ["b"]
    assert "cases" not in store.list_datasets("ws")[0]
    assert store.count_datasets("ws") == 2 and store.count_datasets("nobody") == 0
    assert store.get_dataset("other", "b") is None
    assert stores.studio().get_dataset("ws", "b")["revision"] == 2  # type: ignore[index]
    assert store.delete_dataset("ws", "b") is True and store.delete_dataset("ws", "b") is False


def _save_run(store: Any, ws: str, run_id: str, dataset_id: str = "d") -> dict[str, Any]:
    result: dict[str, Any] = store.save_run(
        ws,
        run_id=run_id,
        dataset_id=dataset_id,
        dataset_revision=1,
        dataset_hash="h",
        label=f"label {run_id}",
        target={"kind": "agent", "id": "a"},
        scorers=[{"type": "exact"}],
        summary={"passed": 1},
        report={"results": [run_id]},
    )
    return result


def test_studio_eval_runs_contract(stores: _Backend, monkeypatch: pytest.MonkeyPatch) -> None:
    store = stores.studio()
    saved = _save_run(store, "ws", "r1")
    assert saved["report"] == {"results": ["r1"]} and saved["target"]["kind"] == "agent"
    assert saved["label"] == "label r1" and saved["summary"] == {"passed": 1}
    for run_id, ds in (("r2", "d"), ("r3", "other"), ("r4", "d")):
        _save_run(store, "ws", run_id, ds)
    _save_run(store, "elsewhere", "r9")
    assert [r["run_id"] for r in store.list_runs("ws")] == ["r4", "r3", "r2", "r1"]
    assert [r["run_id"] for r in store.list_runs("ws", "d", 2)] == ["r4", "r2"]
    assert "report" not in store.list_runs("ws")[0]
    assert store.get_run("ws", "r9") is None and store.get_run("elsewhere", "r9") is not None
    assert store.delete_run("ws", "r2") is True and store.delete_run("ws", "r2") is False
    # Retention keeps the newest MAX_RUNS_PER_WORKSPACE runs of the workspace, others untouched.
    monkeypatch.setattr(studio_store, "MAX_RUNS_PER_WORKSPACE", 3)
    for i in range(5, 9):
        _save_run(stores.studio(), "ws", f"r{i}")
    assert [r["run_id"] for r in store.list_runs("ws")] == ["r8", "r7", "r6"]
    assert [r["run_id"] for r in store.list_runs("elsewhere")] == ["r9"]


# ============================================================================== channels


def _receipt(rid: str, created: float, **kw: Any) -> OutboxReceipt:
    values: dict[str, Any] = {
        "receipt_id": rid,
        "message_id": f"m-{rid}",
        "channel": "email",
        "recipient": "a@example.com",
        "status": "dry_run",
        "dry_run": True,
        "rendered_content": {"body": rid},
        "created_at": created,
        "metadata": {"rid": rid},
        "workspace_id": "ws",
    }
    values.update(kw)
    return OutboxReceipt(**values)


def test_channel_store_contract(stores: _Backend) -> None:
    store = stores.channels()
    assert store.is_duplicate("k") is False
    assert store.record_dedup("k") is True
    assert store.record_dedup("k", status="delivered") is False
    assert store.is_duplicate("k") is True
    assert stores.channels().is_duplicate("k") is True  # another instance
    assert stores.channels().record_dedup("k") is False
    store.record_receipt(_receipt("a", 10.0))
    store.record_receipt(_receipt("b", 10.0, channel="whatsapp", recipient="+911234567890"))
    store.record_receipt(_receipt("c", 5.0, dry_run=False, status="delivered"))
    store.record_receipt(_receipt("legacy", 1.0, workspace_id=None))
    store.record_receipt(_receipt("o", 2.0, workspace_id="other"))
    ids = [r.receipt_id for r in store.list_outbox(workspace_id="ws")]
    assert ids == ["c", "a", "b"]  # by time, then by insertion
    assert [r.receipt_id for r in store.list_outbox(workspace_id="ws", include_unscoped=True)] == [
        "legacy",
        "c",
        "a",
        "b",
    ]
    assert [r.receipt_id for r in store.list_outbox(channel="whatsapp")] == ["b"]
    assert [r.receipt_id for r in store.list_outbox(dry_run=False)] == ["c"]
    assert [r.receipt_id for r in store.list_outbox()] == ["legacy", "o", "c", "a", "b"]
    got = stores.channels().get_receipt("b")
    assert got == _receipt("b", 10.0, channel="whatsapp", recipient="+911234567890")
    assert store.get_receipt("nope") is None
    # Replacing a receipt (INSERT OR REPLACE) makes it the newest insertion.
    store.record_receipt(_receipt("a", 10.0, status="failed", attempt_count=3))
    assert [r.receipt_id for r in store.list_outbox(workspace_id="ws")] == ["c", "b", "a"]
    assert store.get_receipt("a").status == "failed"  # type: ignore[union-attr]
    store.record_receipt(_receipt("a2", 10.0, message_id="m-a"))
    assert store.get_receipt_by_message_id("m-a").receipt_id == "a2"  # type: ignore[union-attr]
    assert store.get_receipt_by_message_id("none") is None


# =================================================================== integration registrations


def test_integration_registrations_contract(stores: _Backend) -> None:
    store = stores.registrations()
    assert store.list() == []
    store.save("w1", [], "mcp", None, "x")  # nothing to save
    assert store.list() == []
    store.save("w1", ["tool.b", "tool.a"], "mcp", "math_fixture", "math")
    assert sorted(stores.registrations().list()) == [
        ("w1", "tool.a", "mcp", "math_fixture", "math"),
        ("w1", "tool.b", "mcp", "math_fixture", "math"),
    ]
    store.save("w1", ["tool.a"], "openapi", None, "pets")  # the workspace replaces its own
    assert ("w1", "tool.a", "openapi", None, "pets") in store.list()
    # Another workspace cannot take an id; a refused save stores nothing, not even free ids.
    with pytest.raises(IntegrationToolTakenError) as taken:
        store.save("w2", ["tool.free", "tool.b"], "mcp", None, "x")
    assert taken.value.module_id == "tool.b"
    assert {row[1] for row in store.list()} == {"tool.a", "tool.b"}
    store.save("w2", ["tool.free"], "mcp", None, "x")
    assert ("w2", "tool.free", "mcp", None, "x") in stores.registrations().list()


# ============================================================================ Postgres only


def test_postgres_schema_and_migrations(pg: _Backend) -> None:
    pg.evaluations()
    pg.studio()
    pg.channels()
    pg.registrations()
    components = dict(
        pg.sql(
            "SELECT component, max(version) FROM nexura_schema_migrations "
            "WHERE component = ANY($1::text[]) GROUP BY component",
            ["evaluations", "studio_evaluations", "channels", "integrations"],
        )
    )
    assert components == {
        "evaluations": 1,
        "studio_evaluations": 1,
        "channels": 1,
        "integrations": 1,
    }
    types = dict(
        pg.sql(
            "SELECT table_name || '.' || column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = ANY($1::text[])",
            [
                "eval_datasets",
                "eval_runs",
                "eval_traces",
                "studio_eval_datasets",
                "studio_eval_runs",
                "channel_dedup",
                "channel_outbox",
                "nexura_service_integration_registrations",
            ],
        )
    )
    assert types["eval_traces.trace_json"] == "text"  # the codec's text, never JSONB (WP2b)
    assert "jsonb" not in set(types.values()) and "json" not in set(types.values())
    assert types["channel_outbox.dry_run"] == "boolean"
    assert types["eval_runs.total_cost_usd"] == "text"
    identities = {
        row[0]
        for row in pg.sql(
            "SELECT table_name FROM information_schema.columns WHERE table_schema = "
            "current_schema() AND column_name = 'seq' AND is_identity = 'YES'"
        )
    }
    assert {
        "eval_datasets",
        "eval_runs",
        "studio_eval_runs",
        "channel_outbox",
        "nexura_service_integration_registrations",
    } <= identities
    (unique,) = pg.sql(
        "SELECT indexdef FROM pg_indexes WHERE schemaname = current_schema() "
        "AND indexname = 'nexura_service_integration_registrations_module'"
    )
    assert "UNIQUE" in unique[0] and "(module_id)" in unique[0]


def test_postgres_nul_is_stored_as_replacement_character(pg: _Backend) -> None:
    evals, studio, channels, regs = pg.evaluations(), pg.studio(), pg.channels(), pg.registrations()
    evals.save_dataset(_dataset("d", "v\x001", 1.0, "x\x00y"), "w\x00s")
    assert evals.get_dataset("d", "v\x001", "w\x00s").version == "v\ufffd1"
    evals.save_trace(StoredTrace(run_id="r\x00", timestamp=1.0, output="o\x00"), "w\x00s")
    (trace,) = evals.list_traces("w\x00s")
    assert trace.run_id == "r\x00" and trace.output == "o\x00"  # JSON keeps NUL escaped
    assert pg.sql("SELECT run_id, workspace_id FROM eval_traces") == [("r\ufffd", "w\ufffds")]
    saved = studio.save_dataset(
        "w",
        "d\x00",
        name="n\x00",
        description="",
        cases=[],
        content_hash="h",
        expected_revision=None,
    )
    assert saved["name"] == "n\ufffd" and studio.get_dataset("w", "d\x00") is not None
    assert channels.record_dedup("k\x00") is True and channels.record_dedup("k\x00") is False
    channels.record_receipt(_receipt("r\x00", 1.0, recipient="a\x00b"))
    assert channels.get_receipt("r\x00").recipient == "a\ufffdb"  # type: ignore[union-attr]
    regs.save("w\x00", ["t\x00"], "mcp", None, "p")
    assert regs.list() == [("w\ufffd", "t\ufffd", "mcp", None, "p")]


def test_postgres_channel_dedup_holds_across_instances(pg: _Backend) -> None:
    instances = [pg.channels() for _ in range(3)]
    for round_no in range(5):
        key = f"outbox:2:ws:order-{round_no}"
        results = _race([lambda s=s, k=key: s.record_dedup(k) for s in instances for _ in range(4)])
        assert sorted(results, key=str) == [False] * 11 + [True], results
        assert all(s.is_duplicate(key) for s in instances)
    assert pg.sql("SELECT count(*) FROM channel_dedup") == [(5,)]


def test_postgres_receipts_are_shared_across_instances(pg: _Backend) -> None:
    a, b = pg.channels(), pg.channels()
    _race(
        [lambda i=i: (a if i % 2 else b).record_receipt(_receipt(f"r{i}", 1.0)) for i in range(12)]
    )
    for store in (a, b):
        assert {r.receipt_id for r in store.list_outbox(workspace_id="ws")} == {
            f"r{i}" for i in range(12)
        }
    assert [r.receipt_id for r in a.list_outbox()] == [r.receipt_id for r in b.list_outbox()]


def test_postgres_studio_dataset_save_is_compare_and_set_across_instances(pg: _Backend) -> None:
    instances = [pg.studio() for _ in range(3)]

    def save(store: Any, expected: int | None, i: int) -> Any:
        return store.save_dataset(
            "ws",
            "d",
            name=f"n{i}",
            description="",
            cases=[{"name": f"c{i}", "input": i}],
            content_hash=f"h{i}",
            expected_revision=expected,
        )

    # Creation (expected revision 0): one creates, every other is told about revision 1.
    created = _race([lambda s=s, i=i: save(s, 0, i) for i, s in enumerate(instances * 2)])
    assert sum(isinstance(r, dict) for r in created) == 1
    assert sorted(str(r) for r in created if isinstance(r, Exception)) == ["conflict:1"] * 5
    for revision in (1, 2, 3):
        results = _race(
            [lambda s=s, i=i, r=revision: save(s, r, i) for i, s in enumerate(instances * 2)]
        )
        winners = [r for r in results if isinstance(r, dict)]
        assert len(winners) == 1 and winners[0]["revision"] == revision + 1
        assert (
            sorted(str(r) for r in results if isinstance(r, Exception))
            == [f"conflict:{revision + 1}"] * 5
        )
        stored = instances[0].get_dataset("ws", "d")
        assert stored["cases"] == winners[0]["cases"]  # the winner's write, whole
    # Unconditional saves (expected None) never lose a revision.
    _race([lambda s=s, i=i: save(s, None, i) for i, s in enumerate(instances * 2)])
    assert instances[1].get_dataset("ws", "d")["revision"] == 10


def test_postgres_studio_runs_list_in_save_order_whatever_the_instance_clocks(
    pg: _Backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    import nexura_postgres.evaluations as module

    a, b = pg.studio(), pg.studio()
    clock = iter(range(10_000, 0, -1))  # the store's view of the instance clock runs backwards
    monkeypatch.setattr(module, "time", SimpleNamespace(time=lambda: float(next(clock))))
    for i in range(8):
        _save_run(a if i % 2 else b, "ws", f"r{i}")
    expected = [f"r{i}" for i in reversed(range(8))]
    assert [r["run_id"] for r in a.list_runs("ws")] == expected
    assert [r["run_id"] for r in b.list_runs("ws")] == expected


def test_postgres_studio_run_retention_holds_across_instances(
    pg: _Backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(studio_store, "MAX_RUNS_PER_WORKSPACE", 5)
    instances = [pg.studio() for _ in range(3)]
    for round_no in range(3):
        _race(
            [
                lambda s=s, i=i, n=round_no: _save_run(s, "ws", f"x{n}-{i}")
                for i, s in enumerate(instances * 4)
            ]
        )
        kept = [r["run_id"] for r in instances[round_no].list_runs("ws", limit=100)]
        assert len(kept) == 5
        newest = [
            row[0]
            for row in pg.sql(
                "SELECT run_id FROM studio_eval_runs WHERE workspace_id = 'ws' "
                "ORDER BY created_at DESC, seq DESC"
            )
        ]
        assert kept == newest
        assert all(run.startswith(f"x{round_no}-") for run in kept)


def test_postgres_eval_run_order_is_stable_across_instances(pg: _Backend) -> None:
    a, b = pg.evaluations(), pg.evaluations()
    for i in range(6):
        (a if i % 2 else b).save_run(_summary(f"e{i}", 500.0), "ws")  # one timestamp for all
    expected = [f"e{i}" for i in reversed(range(6))]
    for _ in range(3):
        assert [r["run_id"] for r in a.list_runs("ws")] == expected
        assert [r["run_id"] for r in b.list_runs("ws")] == expected
    assert a.get_dataset("benchmark_customer_support").examples  # the built-in, no row


def test_postgres_a_tool_id_is_registered_once_across_instances(pg: _Backend) -> None:
    instances = [pg.registrations() for _ in range(2)]
    for round_no in range(4):
        ids = [f"mcp.r{round_no}.add", f"mcp.r{round_no}.sub"]
        orders = [ids, list(reversed(ids))]  # opposite lock orders must not deadlock
        results = _race(
            [
                lambda s=instances[w % 2], w=w, ids=orders[w % 2]: s.save(
                    f"w{w}", ids, "mcp", None, "p"
                )
                for w in range(8)
            ]
        )
        winners = [w for w, r in enumerate(results) if r is None]
        assert len(winners) == 1, results
        assert all(isinstance(r, IntegrationToolTakenError) for r in results if r is not None)
        rows = [row for row in instances[0].list() if row[1] in ids]
        assert {row[0] for row in rows} == {f"w{winners[0]}"} and len(rows) == 2


def test_postgres_eval_traces_are_found_by_blob_retention(pg: _Backend, tmp_path: Path) -> None:
    from nexura_postgres import PostgresStudioStore, referenced_blob_keys

    blobs = LocalBlobStore(tmp_path / "blobs")
    codec = BlobPayloads(blobs, 100, strict=True)
    evals = pg.evaluations(codec)
    # The run-event table exists in a service database too (both reference tables are scanned).
    PostgresStudioStore(pg.databases[0], checkpoint_store=object(), db_path=tmp_path / "x")
    codec.check_ready()  # binds this database to the store, as the service does at startup
    evals.save_trace(StoredTrace(run_id="t", timestamp=1.0, output="k" * 4000), "ws")
    (kept,) = [i.key for i in blobs.list()]
    orphan = blobs.put("ws", EVAL_TRACE_NAMESPACE, b"{}", "application/json").key
    database = pg.databases[0]
    scan = database.run(referenced_blob_keys)
    assert kept in scan.keys and "eval_traces" in scan.tables
    # The evaluation store registered this database with the codec: it is bound to the store.
    assert scan.bindings == {blobs.identity(create=False)}
    # Three days later (both blobs are past the one-day grace by then).
    report = collect_blob_garbage(
        blobs, lambda: database.run(referenced_blob_keys), clock=lambda: time.time() + 3 * DAY
    )
    assert report.deleted_keys == [orphan]
    assert blobs.exists(kept) and not blobs.exists(orphan)
    assert parse_marker(pg.sql("SELECT trace_json FROM eval_traces")[0][0])[0].key == kept  # type: ignore[index]
    assert [t.output for t in evals.list_traces("ws")] == ["k" * 4000]
