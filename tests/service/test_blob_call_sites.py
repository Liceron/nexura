# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Large payloads leave the database (MVP WP2b): the codec, its two call sites, config wiring.

Call sites: Studio run events (``nexura_studio_run_events.data_json``; the Runs and Traces
pages) and imported eval traces (``eval_traces.trace_json``). The app-level tests run on SQLite
and on real Postgres (``backend``; the Postgres half needs ``NEXURA_TEST_POSTGRES_URI``). Since
WP3 the run events of the Postgres half live in Postgres (``PostgresStudioStore``), and since WP8
the eval traces too (``PostgresEvaluationStore``, ``trace_json`` kept as ``TEXT``). The blob
wiring is the same in both modes.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

import httpx
import pytest
from _service_state import store_sql
from fastapi import FastAPI

from nexura_runtime.blobs import (
    BlobIntegrityError,
    BlobPayloads,
    BlobStoreError,
    LocalBlobStore,
)
from nexura_runtime.blobs.payloads import (
    MARKER_PREFIX,
    RUN_EVENT_NAMESPACE,
    decode_json,
    parse_marker,
)
from nexura_runtime.evaluation.models import StoredTrace
from nexura_runtime.evaluation.store import SqliteEvaluationStore
from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.service.storage_backend import ServiceStores
from nexura_runtime.studio.store import StudioStore
from nexura_runtime.testing.blob_store import FakeGCSClient, allow_local_blobs_on_cloud_run
from nexura_runtime.workflow import WorkflowNode, WorkflowSpec

BIG = "x" * 20_000  # well above the 8 KiB default


@pytest.fixture(autouse=True)
def _not_on_cloud_run(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("K_SERVICE", "CLOUD_RUN_JOB", "NEXURA_BLOB_URL", "NEXURA_BLOB_OFFLOAD_BYTES"):
        monkeypatch.delenv(name, raising=False)


# -- The codec ---------------------------------------------------------------------------


def _codec(tmp_path: Path, threshold: int = 100) -> BlobPayloads:
    return BlobPayloads(LocalBlobStore(tmp_path / "blobs"), threshold)


def test_small_payloads_stay_inline_and_large_ones_become_references(tmp_path: Path) -> None:
    codec = _codec(tmp_path)
    small = json.dumps({"a": 1})
    assert codec.encode("ws", RUN_EVENT_NAMESPACE, small) == small
    large = json.dumps({"text": "y" * 500})
    stored = codec.encode("ws", RUN_EVENT_NAMESPACE, large)
    assert stored.startswith(MARKER_PREFIX) and len(stored) < 700
    ref, preview = parse_marker(stored) or (None, "")
    assert ref is not None and ref.key.startswith("ws/run-events/")
    assert preview == large[:256]
    assert codec.decode(stored, "ws") == large
    assert codec.decode(small, "ws") == small  # inline rows read as before


def test_threshold_zero_offloads_nothing_but_still_reads_references(tmp_path: Path) -> None:
    writer = _codec(tmp_path)
    stored = writer.encode("ws", RUN_EVENT_NAMESPACE, json.dumps({"t": "z" * 500}))
    reader = BlobPayloads(writer.store, 0)
    big = json.dumps({"t": "z" * 50_000})
    assert reader.encode("ws", RUN_EVENT_NAMESPACE, big) == big
    assert json.loads(reader.decode(stored, "ws")) == {"t": "z" * 500}


def test_a_payload_that_looks_like_a_reference_reads_back_as_itself(tmp_path: Path) -> None:
    codec = _codec(tmp_path, threshold=0)
    victim = codec.store.put("victim-ws", RUN_EVENT_NAMESPACE, b'{"secret": 1}', "application/json")
    crafted = json.dumps({"$nexura_blob": victim.to_dict(), "preview": ""})
    stored = codec.encode("attacker-ws", RUN_EVENT_NAMESPACE, crafted)
    assert stored != crafted  # forced into the blob store, never stored inline
    assert codec.decode(stored, "attacker-ws") == crafted
    assert "secret" not in codec.decode(stored, "attacker-ws")


def test_a_reference_is_followed_only_within_its_own_workspace(tmp_path: Path) -> None:
    codec = _codec(tmp_path)
    stored = codec.encode("ws-a", RUN_EVENT_NAMESPACE, json.dumps({"t": "q" * 500}))
    # Read as another workspace (for example a forged legacy row): the reference is not followed.
    assert codec.decode(stored, "ws-b") == stored
    assert "q" * 300 not in codec.decode(stored, "ws-b")


def test_a_missing_blob_reads_as_unavailable_with_its_preview(tmp_path: Path) -> None:
    codec = _codec(tmp_path)
    text = json.dumps({"t": "m" * 500})
    stored = codec.encode("ws", RUN_EVENT_NAMESPACE, text)
    ref, _ = parse_marker(stored) or (None, "")
    assert ref is not None and codec.store.delete(ref)
    assert decode_json(codec, stored, "ws") == {"t": "m" * 500}  # this process cached it
    codec.clear_cache()  # as another instance would read it
    value = decode_json(codec, stored, "ws")
    assert value["payload_unavailable"] is True and value["reason"] == "missing"
    assert value["preview"] == text[:256] and value["size"] == len(text)


def test_an_unavailable_store_keeps_the_payload_inline(tmp_path: Path, caplog: Any) -> None:
    class Down(LocalBlobStore):
        def put(self, *args: Any, **kwargs: Any) -> Any:
            raise BlobStoreError("store is down")

    codec = BlobPayloads(Down(tmp_path / "blobs"), 100)
    text = json.dumps({"t": "d" * 500})
    with caplog.at_level("WARNING"):
        assert codec.encode("ws", RUN_EVENT_NAMESPACE, text) == text
    assert "inline" in caplog.text
    crafted = json.dumps({"$nexura_blob": {}, "preview": ""})
    with pytest.raises(BlobStoreError):  # a reference look-alike can never be stored inline
        codec.encode("ws", RUN_EVENT_NAMESPACE, crafted)


# -- StudioStore and the eval store on SQLite ----------------------------------------------


def _spec() -> WorkflowSpec:
    return WorkflowSpec(name="big", entry_node="n", nodes={"n": WorkflowNode("n", "echo")})


def _service_spec() -> dict[str, Any]:
    return {
        "name": "big",
        "entry_node": "n",
        "nodes": {"n": {"id": "n", "handler": "synthetic_order_lookup", "transitions": {}}},
    }


def _raw_events(db: Path, run_id: str) -> list[str]:
    with closing(sqlite3.connect(db)) as conn:
        return [
            row[0]
            for row in conn.execute(
                "SELECT data_json FROM nexura_studio_run_events WHERE run_id=? ORDER BY event_seq",
                (run_id,),
            )
        ]


def _raw_events_of(app: FastAPI, run_id: str) -> list[str]:
    """The stored ``data_json`` texts of a service run, from Postgres or the SQLite file."""
    database = getattr(app.state.store, "database", None)
    if database is None:
        return _raw_events(Path(app.state.store.db_path), run_id)

    async def fetch(conn: Any) -> Any:
        return await conn.fetch(
            "SELECT data_json FROM nexura_studio_run_events WHERE run_id=$1 ORDER BY event_seq",
            run_id,
        )

    return [row["data_json"] for row in database.run(fetch)]


async def test_studio_store_offloads_large_run_events(tmp_path: Path) -> None:
    db = tmp_path / "s.sqlite3"
    codec = _codec(tmp_path, threshold=8192)
    store = StudioStore(db, blob_payloads=codec)
    try:
        await store.record_run_start("run-1", None, _spec(), {"doc": BIG}, journal_workspace="ws-1")
        await store.record_run_event("run-1", "NODE_DONE", node_id="n", data={"out": BIG})
        await store.record_run_event("run-1", "SMALL", data={"ok": True})
        raw = _raw_events(db, "run-1")
        assert raw[0].startswith(MARKER_PREFIX) and raw[1].startswith(MARKER_PREFIX)
        assert json.loads(raw[2]) == {"ok": True}
        assert all(len(value) < 1024 for value in raw)
        keys = sorted(info.key for info in codec.store.list())
        assert len(keys) == 2 and all(k.startswith("ws-1/run-events/") for k in keys)
        run = await store.get_run("run-1")
        assert run is not None
        assert run["events"][0]["data"]["initial_state"] == {"doc": BIG}
        assert run["events"][1]["data"] == {"out": BIG}
        assert run["events"][2]["data"] == {"ok": True}
        assert run["input_state"] == {"doc": BIG}  # run columns are unchanged (stay in the DB)
        assert "_journal_workspace" not in run
    finally:
        await store.close()


async def test_inline_rows_written_before_offloading_still_read(tmp_path: Path) -> None:
    db = tmp_path / "s.sqlite3"
    legacy = StudioStore(db)  # no codec: the pre-WP2b behaviour
    await legacy.record_run_start("run-old", None, _spec(), {"doc": BIG})
    await legacy.record_run_event("run-old", "NODE_DONE", data={"out": BIG})
    await legacy.close()
    assert not _raw_events(db, "run-old")[0].startswith(MARKER_PREFIX)
    store = StudioStore(db, blob_payloads=_codec(tmp_path, threshold=8192))
    try:
        run = await store.get_run("run-old")
        assert run is not None
        assert run["events"][0]["data"]["initial_state"] == {"doc": BIG}
        assert run["events"][1]["data"] == {"out": BIG}
    finally:
        await store.close()


async def test_tampered_run_event_blob_is_reported_not_shown(tmp_path: Path, caplog: Any) -> None:
    db = tmp_path / "s.sqlite3"
    codec = _codec(tmp_path, threshold=8192)
    store = StudioStore(db, blob_payloads=codec)
    try:
        await store.record_run_start("run-t", None, _spec(), {"doc": BIG}, journal_workspace="w")
        await store.record_run_event("run-t", "SMALL", data={"ok": True})
        (info,) = list(codec.store.list())
        path = tmp_path / "blobs" / info.key
        path.write_bytes(path.read_bytes().replace(b"xxxx", b"yyyy", 1))
        codec.clear_cache()  # another instance, which never saw the original bytes
        with pytest.raises(BlobIntegrityError):
            codec.decode(_raw_events(db, "run-t")[0], "w")
        with caplog.at_level("ERROR"):
            run = await store.get_run("run-t")
        assert run is not None
        data = run["events"][0]["data"]
        assert data["payload_unavailable"] is True and data["reason"] == "integrity"
        assert "doc" not in data and "integrity check" in caplog.text
        assert run["events"][1]["data"] == {"ok": True}  # the rest of the run still reads
    finally:
        await store.close()


async def test_event_for_a_missing_run_leaves_only_an_unreferenced_blob(tmp_path: Path) -> None:
    db = tmp_path / "s.sqlite3"
    codec = _codec(tmp_path, threshold=8192)
    store = StudioStore(db, blob_payloads=codec)
    try:
        await store.record_run_event("no-such-run", "X", data={"out": BIG})
        assert _raw_events(db, "no-such-run") == []
        assert [i.key.split("/")[0] for i in codec.store.list()] == ["default"]
    finally:
        await store.close()


def test_eval_store_offloads_large_traces(tmp_path: Path) -> None:
    codec = _codec(tmp_path, threshold=8192)
    store = SqliteEvaluationStore(str(tmp_path / "e.sqlite3"), blob_payloads=codec)
    try:
        big = StoredTrace(run_id="r-big", timestamp=1.0, output=BIG)
        small = StoredTrace(run_id="r-small", timestamp=2.0, output="tiny")
        store.save_trace(big, "ws-1")
        store.save_trace(small, "ws-1")
        with closing(sqlite3.connect(tmp_path / "e.sqlite3")) as conn:
            raw = dict(conn.execute("SELECT run_id, trace_json FROM eval_traces"))
        assert raw["r-big"].startswith(MARKER_PREFIX) and len(raw["r-big"]) < 1024
        assert json.loads(raw["r-small"])["output"] == "tiny"
        assert {t.run_id: t.output for t in store.list_traces("ws-1")} == {
            "r-big": BIG,
            "r-small": "tiny",
        }
        assert store.list_traces("ws-2") == []
        # Payload retention deleted the big trace's blob: it is skipped, the rest still reads.
        for info in codec.store.list():
            codec.store.delete(info.key)
        codec.clear_cache()
        assert [t.run_id for t in store.list_traces("ws-1")] == ["r-small"]
    finally:
        store.close()


# -- Configuration ---------------------------------------------------------------------


def test_offload_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NEXURA_DATABASE_URL", raising=False)
    db = tmp_path / "s.sqlite3"
    sqlite_only = ServiceStores(db)
    assert sqlite_only.blob_offload_bytes() == 0  # a plain SQLite service stays one file
    codec = sqlite_only.blob_payloads()
    assert isinstance(codec.store, LocalBlobStore)
    assert codec.store.root == (tmp_path / "s.sqlite3.blobs").resolve()
    assert ServiceStores(db, "postgresql://h.example/db").blob_offload_bytes() == 8192
    assert ServiceStores(db, blob_url=(tmp_path / "b").as_uri()).blob_offload_bytes() == 8192
    monkeypatch.setenv("NEXURA_BLOB_OFFLOAD_BYTES", "1000")
    assert ServiceStores(db).blob_offload_bytes() == 1000
    monkeypatch.setenv("NEXURA_BLOB_OFFLOAD_BYTES", "-1")
    with pytest.raises(ValueError, match="NEXURA_BLOB_OFFLOAD_BYTES"):
        ServiceStores(db).blob_offload_bytes()
    monkeypatch.setenv("NEXURA_BLOB_OFFLOAD_BYTES", "lots")
    with pytest.raises(ValueError, match="NEXURA_BLOB_OFFLOAD_BYTES"):
        ServiceStores(db).blob_offload_bytes()


def test_gs_url_selects_the_gcs_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import nexura_runtime.blobs.url as blob_url

    monkeypatch.setenv("NEXURA_BLOB_URL", "gs://nexura-blobs/prod")
    stores = ServiceStores(tmp_path / "s.sqlite3")
    real_open = blob_url.open_blob_store
    monkeypatch.setattr(
        "nexura_runtime.service.storage_backend.open_blob_store",
        lambda location: real_open(location, client=FakeGCSClient()),
    )
    codec = stores.blob_payloads()
    assert codec.store.scheme == "gs" and codec.store.describe() == "gs://nexura-blobs/prod"
    assert codec.threshold == 8192


def test_bad_blob_url_fails_before_anything_is_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NEXURA_BLOB_URL", "s3://bucket/x")
    with pytest.raises(ValueError, match="blob URL"):
        create_api_app(db_path=tmp_path / "s.sqlite3")
    assert not (tmp_path / "s.sqlite3").exists()


@pytest.mark.parametrize("cloud_var", ["K_SERVICE", "CLOUD_RUN_JOB"])
def test_local_blob_folder_is_refused_on_cloud_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cloud_var: str
) -> None:
    monkeypatch.delenv("NEXURA_DATABASE_URL", raising=False)
    monkeypatch.setenv("NEXURA_ALLOW_PARTIAL_POSTGRES", "1")
    monkeypatch.setenv(cloud_var, "nexura")
    db = tmp_path / "s.sqlite3"
    # An explicit file:// URL is refused, by the factory and by the app, before any file exists.
    explicit = (tmp_path / "blobs").as_uri()
    with pytest.raises(
        RuntimeError, match=f"local blob folder is refused on Cloud Run \\({cloud_var}"
    ):
        ServiceStores(db, blob_url=explicit).blob_payloads()
    with pytest.raises(RuntimeError, match="NEXURA_BLOB_URL=gs://"):
        create_api_app(db_path=db, blob_url=explicit)
    assert not db.exists()
    # Postgres mode with the default folder is refused too (ahead of the partial-mode refusal,
    # which is checked first by require_service_support and also applies here).
    with pytest.raises(RuntimeError, match="local blob folder is refused"):
        ServiceStores(db, "postgresql://h.example/db").require_blob_support()
    # A gs:// store is accepted on Cloud Run.
    ServiceStores(db, "postgresql://h.example/db", "gs://nexura-blobs/x").require_blob_support()
    # A plain SQLite service keeps its local default, like its local database file.
    assert ServiceStores(db).blob_payloads().store.scheme == "file"
    # The test-only opt-in (a module flag, no environment variable) lifts the refusal.
    with allow_local_blobs_on_cloud_run():
        assert ServiceStores(db, blob_url=explicit).blob_payloads().store.scheme == "file"
    with pytest.raises(RuntimeError, match="refused on Cloud Run"):
        ServiceStores(db, blob_url=explicit).blob_payloads()


def test_cloud_run_refusal_has_no_environment_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("K_SERVICE", "nexura")
    for name in (
        "NEXURA_ALLOW_LOCAL_BLOBS",
        "NEXURA_TEST_LOCAL_BLOBS",
        "NEXURA_ALLOW_LOCAL_BLOBS_ON_CLOUD_RUN",
    ):
        monkeypatch.setenv(name, "1")
    with pytest.raises(RuntimeError, match="refused on Cloud Run"):
        ServiceStores(tmp_path / "s.sqlite3", blob_url=(tmp_path / "b").as_uri()).blob_payloads()


# -- The service, on SQLite and on real Postgres --------------------------------------------


@pytest.fixture
def make_app(
    backend: str, tmp_path: Path, request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Callable[..., FastAPI]]:
    if backend == "postgres":
        url: str | None = request.getfixturevalue("service_database_url")
        monkeypatch.setenv("NEXURA_ALLOW_PARTIAL_POSTGRES", "1")
    else:
        monkeypatch.delenv("NEXURA_DATABASE_URL", raising=False)
        url = None

    def make(**kwargs: Any) -> FastAPI:
        kwargs.setdefault("blob_url", (tmp_path / "blobs").as_uri())
        return create_api_app(db_path=tmp_path / "s.sqlite3", database_url=url, **kwargs)

    yield make


@pytest.fixture
async def client_for() -> AsyncIterator[Callable[[FastAPI], Any]]:
    opened: list[Any] = []

    async def open_client(app: FastAPI) -> httpx.AsyncClient:
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        )
        opened.append((lifespan, client))
        return client

    yield open_client
    for lifespan, client in reversed(opened):
        await client.aclose()
        await lifespan.__aexit__(None, None, None)


async def test_service_run_payloads_go_to_the_blob_store(
    make_app: Callable[..., FastAPI], client_for: Any, tmp_path: Path
) -> None:
    app = make_app()
    client = await client_for(app)
    started = await client.post(
        "/api/v1/runs", json={"spec": _service_spec(), "initial_state": {"doc": BIG}}
    )
    assert started.status_code == 201, started.text
    run_id = started.json()["run_id"]
    got = await client.get(f"/api/v1/runs/{run_id}")
    assert got.status_code == 200, got.text
    events = got.json()["events"]
    assert events[0]["type"] == "RUN_STARTED"
    # The API shortens long strings for display (run_diagnostics, 8000 characters).
    assert events[0]["data"]["initial_state"]["doc"].startswith("x" * 8000)
    full = await app.state.store.get_run(run_id)
    assert full["events"][0]["data"]["initial_state"] == {"doc": BIG}
    raw = _raw_events_of(app, run_id)
    offloaded = [value for value in raw if value.startswith(MARKER_PREFIX)]
    assert offloaded and all(len(value) < 1024 for value in offloaded)
    assert all(len(value.encode()) <= 8192 for value in raw)  # nothing large stays inline
    keys = [info.key for info in app.state.blob_store.list()]
    assert keys and all(key.startswith("default/run-events/") for key in keys)
    assert app.state.blob_store.describe() == (tmp_path / "blobs").as_uri()
    # A deleted payload blob (retention) leaves a readable run with the preview.
    for key in keys:
        app.state.blob_store.delete(key)
    app.state.blob_payloads.clear_cache()  # as another instance would read it
    again = (await client.get(f"/api/v1/runs/{run_id}")).json()["events"][0]["data"]
    assert again["payload_unavailable"] is True and again["preview"]
    assert again["reason"] == "missing"


async def test_service_eval_traces_go_to_the_blob_store(
    make_app: Callable[..., FastAPI], client_for: Any, tmp_path: Path, backend: str
) -> None:
    app = make_app()
    client = await client_for(app)
    trace = {"run_id": "imported-1", "timestamp": 1.0, "output": BIG}
    imported = await client.post("/api/v1/evaluations/traces", json=trace)
    assert imported.status_code == 200, imported.text
    # The service's eval_traces table: the SQLite file, or Postgres (MVP WP8) on that backend.
    ((stored,),) = store_sql(app.state.eval_store, "SELECT trace_json FROM eval_traces")
    assert stored.startswith(MARKER_PREFIX)
    if backend == "postgres":
        assert not (tmp_path / "s.sqlite3").exists()  # no SQLite file in Postgres mode
    (info,) = list(app.state.blob_store.list())
    assert info.key.startswith("default/eval-traces/")
    assert [t.output for t in app.state.eval_store.list_traces("default")] == [BIG]


async def test_service_with_a_gcs_store(
    make_app: Callable[..., FastAPI],
    client_for: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import nexura_runtime.blobs.url as blob_url

    fake = FakeGCSClient()
    real_open = blob_url.open_blob_store
    monkeypatch.setattr(
        "nexura_runtime.service.storage_backend.open_blob_store",
        lambda location: real_open(location, client=fake),
    )
    app = make_app(blob_url="gs://nexura-blobs/prod")
    client = await client_for(app)
    started = await client.post(
        "/api/v1/runs", json={"spec": _service_spec(), "initial_state": {"doc": BIG}}
    )
    assert started.status_code == 201, started.text
    run_id = started.json()["run_id"]
    assert (await client.get(f"/api/v1/runs/{run_id}")).status_code == 200
    run = await app.state.store.get_run(run_id)
    assert run["events"][0]["data"]["initial_state"] == {"doc": BIG}
    # The identity and (Postgres, two-way binding) owner objects sit beside the prefix.
    metadata = {"prod.nexura-store-id", "prod.nexura-store-owner"}
    names = [n for n in fake.buckets["nexura-blobs"] if n not in metadata]
    assert names and all(name.startswith("prod/default/run-events/") for name in names)
    # The store's identity is recorded in every reference.
    identity = fake.raw("nexura-blobs", "prod.nexura-store-id").decode()
    raw = _raw_events_of(app, run_id)[0]
    assert json.loads(raw)["store"] == identity


# -- Read cost and resilience (review H1, L3, M3) --------------------------------------


def _gcs_studio(tmp_path: Path, fake: FakeGCSClient) -> tuple[StudioStore, BlobPayloads]:
    from nexura_runtime.blobs.gcs import GCSBlobStore

    codec = BlobPayloads(GCSBlobStore("bkt", "p", client=fake), 8192)
    return StudioStore(tmp_path / "s.sqlite3", blob_payloads=codec), codec


async def test_repeated_run_reads_download_each_payload_once(tmp_path: Path) -> None:
    fake = FakeGCSClient()
    store, codec = _gcs_studio(tmp_path, fake)
    try:
        await store.record_run_start("r", None, _spec(), {}, journal_workspace="ws")
        for i in range(30):
            await store.record_run_event("r", f"E{i}", data={"out": BIG})
        codec.clear_cache()  # a fresh instance: nothing cached yet
        fake.downloads = 0
        for _ in range(10):
            run = await store.get_run("r")
            assert run is not None and run["events"][-1]["data"] == {"out": BIG}
        assert fake.downloads == 30  # was 300: one download per payload, then the cache
        codec.clear_cache()
        fake.downloads = 0
        status = await store.get_run("r", payloads=False)
        assert status is not None and status["status"] == "running"
        assert fake.downloads == 0  # status-only reads never touch the blob store
        assert status["events"][-1]["data"]["payload_deferred"] is True
        live = await store.get_run("r", payloads_after_seq=29)  # what a stream has not sent
        assert fake.downloads == 2  # events 30 and 31 only
        assert live is not None and live["events"][-1]["data"] == {"out": BIG}
        loaded = await store.load_payloads(status)
        assert loaded["events"][1]["data"] == {"out": BIG} and fake.downloads == 30
    finally:
        await store.close()


@pytest.mark.parametrize("code", [503, 403])
async def test_a_failing_blob_store_never_fails_status_or_run_reads(
    tmp_path: Path, code: int, caplog: Any
) -> None:
    fake = FakeGCSClient()
    store, codec = _gcs_studio(tmp_path, fake)
    try:
        await store.record_run_start("r", None, _spec(), {"doc": BIG}, journal_workspace="ws")
        codec.clear_cache()
        fake.fail_downloads = code  # 5xx after retries, or an IAM mistake
        status = await store.get_run("r", payloads=False)
        assert status is not None and status["status"] == "running"
        run = await store.get_run("r")
        assert run is not None
        data = run["events"][0]["data"]
        assert data["payload_unavailable"] is True and data["reason"] == "store_error"
        assert data["preview"].startswith('{"workflow_id"')
        fake.fail_downloads = None  # once the store recovers, the payload reads again
        again = await store.get_run("r")
        assert again is not None and again["events"][0]["data"]["initial_state"] == {"doc": BIG}
    finally:
        await store.close()


async def test_service_control_paths_work_while_the_blob_store_fails(
    make_app: Callable[..., FastAPI], client_for: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import nexura_runtime.blobs.url as blob_url

    fake = FakeGCSClient()
    real_open = blob_url.open_blob_store
    monkeypatch.setattr(
        "nexura_runtime.service.storage_backend.open_blob_store",
        lambda location: real_open(location, client=fake),
    )
    app = make_app(blob_url="gs://nexura-blobs/prod")
    client = await client_for(app)
    started = await client.post(
        "/api/v1/runs", json={"spec": _service_spec(), "initial_state": {"doc": BIG}}
    )
    assert started.status_code == 201, started.text
    run_id = started.json()["run_id"]
    app.state.blob_payloads.clear_cache()
    fake.fail_downloads = 503
    got = await client.get(f"/api/v1/runs/{run_id}")
    assert got.status_code == 200, got.text
    assert got.json()["events"][0]["data"]["reason"] == "store_error"
    cancel = await client.post(f"/api/v1/runs/{run_id}/cancel", json={})
    assert cancel.status_code == 409, cancel.text  # already finished: a normal answer, no 500
    stream = await client.get(f"/api/v1/runs/{run_id}/events")
    assert stream.status_code == 200 and "store_error" in stream.text


async def test_run_read_downloads_nothing_before_authorisation(
    make_app: Callable[..., FastAPI], client_for: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi import HTTPException

    import nexura_runtime.blobs.url as blob_url
    from nexura_runtime.service.api import runs as runs_api

    fake = FakeGCSClient()
    real_open = blob_url.open_blob_store
    monkeypatch.setattr(
        "nexura_runtime.service.storage_backend.open_blob_store",
        lambda location: real_open(location, client=fake),
    )
    app = make_app(blob_url="gs://nexura-blobs/prod")
    client = await client_for(app)
    started = await client.post(
        "/api/v1/runs", json={"spec": _service_spec(), "initial_state": {"doc": BIG}}
    )
    run_id = started.json()["run_id"]
    app.state.blob_payloads.clear_cache()
    fake.downloads = 0

    def foreign(request: Any, rid: str) -> None:
        raise HTTPException(status_code=403, detail={"code": "CROSS_WORKSPACE_ACCESS_DENIED"})

    monkeypatch.setattr(runs_api, "_assert_run_ownership", foreign)
    assert (await client.get(f"/api/v1/runs/{run_id}")).status_code == 403
    assert (await client.get(f"/api/v1/runs/{run_id}/events")).status_code == 403
    assert fake.downloads == 0


async def test_eval_trace_routes_do_blob_io_off_the_event_loop(
    make_app: Callable[..., FastAPI], client_for: Any
) -> None:
    import threading

    app = make_app()
    client = await client_for(app)
    loop_thread = threading.get_ident()
    seen: list[int] = []
    store = app.state.eval_store
    save, list_traces = store.save_trace, store.list_traces

    def save_spy(*args: Any) -> Any:
        seen.append(threading.get_ident())
        return save(*args)

    def list_spy(*args: Any) -> Any:
        seen.append(threading.get_ident())
        return list_traces(*args)

    store.save_trace, store.list_traces = save_spy, list_spy
    trace = {"run_id": "imported-1", "timestamp": 1.0, "output": BIG}
    assert (await client.post("/api/v1/evaluations/traces", json=trace)).status_code == 200
    online = await client.post(
        "/api/v1/evaluations/online",
        json={"example": {"input_data": {}, "expected_output": "unused"}},
    )
    assert online.status_code == 200, online.text
    assert len(seen) == 2 and loop_thread not in seen


# -- Follow-up N2, N3, N4 ----------------------------------------------------------------


def test_cache_limit_counts_utf8_bytes(tmp_path: Path) -> None:
    fake = FakeGCSClient()
    from nexura_runtime.blobs.gcs import GCSBlobStore

    codec = BlobPayloads(GCSBlobStore("bkt", "p", client=fake), 100, cache_bytes=4000)
    wide = json.dumps({"t": "字" * 1500}, ensure_ascii=False)  # 1500 characters, 4.5 kB
    assert len(wide) < 4000 < len(wide.encode())
    stored = codec.encode("ws", RUN_EVENT_NAMESPACE, wide)
    assert codec._cached_bytes == 0  # over the byte limit: not cached
    fake.downloads = 0
    assert codec.decode(stored, "ws") == wide and fake.downloads == 1
    narrow = json.dumps({"t": "a" * 1500})
    codec.encode("ws", RUN_EVENT_NAMESPACE, narrow)
    assert codec._cached_bytes == len(narrow.encode())


def test_cache_is_keyed_by_the_recorded_hash_and_size(tmp_path: Path) -> None:
    codec = _codec(tmp_path)
    text = json.dumps({"t": "h" * 500})
    stored = codec.encode("ws", RUN_EVENT_NAMESPACE, text)
    assert codec.decode(stored, "ws") == text  # served from the cache
    edited = json.loads(stored)
    edited["$nexura_blob"]["sha256"] = "0" * 64  # someone edited the row
    forged = json.dumps(edited, ensure_ascii=False)
    with pytest.raises(BlobIntegrityError):
        codec.decode(forged, "ws")  # a cache miss: the store's bytes are checked again


async def test_strict_mode_checks_the_store_at_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    import nexura_runtime.blobs.url as blob_url

    monkeypatch.delenv("NEXURA_DATABASE_URL", raising=False)
    fake = FakeGCSClient()
    real_open = blob_url.open_blob_store
    monkeypatch.setattr(
        "nexura_runtime.service.storage_backend.open_blob_store",
        lambda location: real_open(location, client=fake),
    )
    fake.fail_downloads = 403  # IAM: the identity object cannot be read
    app = create_api_app(db_path=tmp_path / "s.sqlite3", blob_url="gs://nexura-blobs/prod")
    with pytest.raises(RuntimeError, match=r"gs://nexura-blobs/prod is not usable"):
        async with app.router.lifespan_context(app):
            pass
    fake.fail_downloads = None
    ok = create_api_app(db_path=tmp_path / "s.sqlite3", blob_url="gs://nexura-blobs/prod")
    async with ok.router.lifespan_context(ok):
        identity = fake.raw("nexura-blobs", "prod.nexura-store-id").decode()
    from nexura_runtime.blobs.binding import SQLiteBlobBinding

    assert SQLiteBlobBinding(tmp_path / "s.sqlite3").read() == identity  # bound at startup
    # A database bound to another store stops the start too.
    other = FakeGCSClient()
    monkeypatch.setattr(
        "nexura_runtime.service.storage_backend.open_blob_store",
        lambda location: real_open(location, client=other),
    )
    wrong = create_api_app(db_path=tmp_path / "s.sqlite3", blob_url="gs://nexura-blobs/prod")
    with pytest.raises(RuntimeError, match="bound to blob store"):
        async with wrong.router.lifespan_context(wrong):
            pass


async def test_strict_mode_refuses_an_open_local_root_at_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    url = request.getfixturevalue("service_database_url")
    monkeypatch.setenv("NEXURA_ALLOW_PARTIAL_POSTGRES", "1")
    root = tmp_path / "blobs"
    root.mkdir()
    root.chmod(0o777)
    app = create_api_app(db_path=tmp_path / "s.sqlite3", database_url=url, blob_url=root.as_uri())
    with pytest.raises(RuntimeError, match="group/other write"):
        async with app.router.lifespan_context(app):
            pass


async def test_plain_sqlite_is_not_checked_at_startup(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.delenv("NEXURA_DATABASE_URL", raising=False)
    root = tmp_path / "blobs"
    root.mkdir()
    root.chmod(0o777)  # would be refused at the first offload, quietly, not at startup
    app = create_api_app(db_path=tmp_path / "s.sqlite3", blob_url=root.as_uri())
    async with app.router.lifespan_context(app):
        assert app.state.blob_payloads.strict is False
    assert list(root.iterdir()) == []


def test_strict_write_failures_are_logged_as_errors(tmp_path: Path, caplog: Any) -> None:
    class Down(LocalBlobStore):
        def put(self, *args: Any, **kwargs: Any) -> Any:
            raise BlobStoreError("store is down")

    text = json.dumps({"t": "e" * 500})
    for strict, level in ((True, "ERROR"), (False, "WARNING")):
        caplog.clear()
        codec = BlobPayloads(Down(tmp_path / f"b{strict}"), 100, strict=strict)
        with caplog.at_level("WARNING"):
            assert codec.encode("ws", RUN_EVENT_NAMESPACE, text) == text
        assert codec.inline_fallbacks == 1
        assert [r.levelname for r in caplog.records if "stored inline" in r.getMessage()] == [level]
