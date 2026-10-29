# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""MVP Postgres WP9: what Cloud Run statelessness still needed in the open-source service.

* **Startup refusal.** A Postgres ``database_url`` refuses to start without
  ``NEXURA_MASTER_KEY`` or ``NEXURA_TRIGGER_SECRET_KEY`` (a key made up per instance would make
  the shared rows unreadable on every other instance), before any connection or file, with a
  message naming each missing variable. SQLite mode is unchanged.
* **No trigger key file (WP8 review M1).** In Postgres mode webhook secrets use only the
  variable: no ``<db_path>.trigger.key`` file is ever written, even when the variable vanishes
  after startup (the store then refuses instead).
* **No second lifespan (WP8 review L3).** A Postgres-mode app object started again after its
  shutdown fails with a clear ``ServiceShutDownError``, not a closed-pool error in a store.
* **The trigger tick** (``run_trigger_tick``): one bounded pass that fires a due schedule and
  runs the job, returns within its budget while a long job keeps running in the background,
  and returns ``busy`` while another pass holds the store's lease. The in-process poller keeps
  working next to it. On both backends (the Postgres ones skip without
  ``NEXURA_TEST_POSTGRES_URI``); every handler is a local function, nothing calls a model.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from _local_env import local_headers

from nexura_runtime.secrets.keys import generate_master_key
from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.service.storage_backend import (
    ServiceShutDownError,
    ServiceStores,
    SharedKeyMissingError,
)
from nexura_runtime.service.triggers.errors import TriggerError
from nexura_runtime.service.triggers.models import WebhookConfig
from nexura_runtime.service.triggers.tick import (
    LEASE_MARGIN_SECONDS,
    TICK_LEASE,
    check_tick_budget,
    run_trigger_tick,
)
from nexura_runtime.workflow import ModuleRegistry

API = "/api/v1"
H = local_headers("default", "admin")
FIXTURE_URL = "postgresql://fixture.example/db?sslmode=require"
KEYS = ("NEXURA_MASTER_KEY", "NEXURA_TRIGGER_SECRET_KEY")


@pytest.fixture
def full_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Full Postgres mode, no Cloud Run, no development switch; the keys are set per test."""
    monkeypatch.delenv("NEXURA_ALLOW_PARTIAL_POSTGRES", raising=False)
    for name in ("K_SERVICE", "CLOUD_RUN_JOB", "NEXURA_WORKFLOW_STATE_URL", "NEXURA_BLOB_URL"):
        monkeypatch.delenv(name, raising=False)


def _keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXURA_MASTER_KEY", generate_master_key())
    monkeypatch.setenv("NEXURA_TRIGGER_SECRET_KEY", "wp9-test-trigger-key")


# --------------------------------------------------------------------------- startup refusal


@pytest.mark.parametrize(
    "unset", [("NEXURA_MASTER_KEY",), ("NEXURA_TRIGGER_SECRET_KEY",), KEYS], ids=str
)
@pytest.mark.parametrize("how", ["missing", "blank"])
def test_postgres_mode_refuses_to_start_without_the_shared_keys(
    unset: tuple[str, ...],
    how: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_mode: None,
) -> None:
    _keys(monkeypatch)
    for name in unset:
        if how == "missing":
            monkeypatch.delenv(name)
        else:
            monkeypatch.setenv(name, "  ")  # whitespace is no key
    # Not lifted by the plaintext opt-out (it keeps credentials in one process).
    monkeypatch.setenv("NEXURA_INSECURE_ALLOW_PLAINTEXT_SECRETS", "I_UNDERSTAND_THIS_IS_UNSAFE")
    opened: list[str] = []
    monkeypatch.setattr(ServiceStores, "open_database", lambda self: opened.append("x"))
    with pytest.raises(SharedKeyMissingError) as refused:
        create_api_app(db_path=tmp_path / "svc.sqlite3", database_url=FIXTURE_URL)
    message = str(refused.value)
    assert refused.value.missing == list(unset)
    for name in KEYS:
        assert (name in message) == (name in unset), (name, message)
    assert "every instance" in message and "fixture.example" not in message
    assert not opened and not list(tmp_path.iterdir())  # before any connection or file


def test_postgres_mode_starts_with_both_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, full_mode: None
) -> None:
    _keys(monkeypatch)

    class Connecting(Exception):
        pass

    def connect(self: Any) -> Any:
        raise Connecting  # past every refusal, at the first connection

    monkeypatch.setattr(ServiceStores, "open_database", connect)
    with pytest.raises(Connecting):
        create_api_app(db_path=tmp_path / "svc.sqlite3", database_url=FIXTURE_URL)
    assert not list(tmp_path.iterdir())


@pytest.mark.sqlite_only  # SQLite mode itself: the key file next to the database file.
async def test_sqlite_mode_is_unchanged_without_the_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in (*KEYS, "NEXURA_DATABASE_URL", "DATABASE_URL", "NEXURA_WORKFLOW_STATE_URL"):
        monkeypatch.delenv(name, raising=False)
    app = create_api_app(db_path=tmp_path / "s.sqlite3", registry=_registry())
    assert app.state.secret_store is None  # in-process credentials, as before
    async with _client(app) as client:
        workflow = await _workflow(client)
        res = await client.post(
            f"{API}/triggers/webhooks",
            json={"endpoint_id": "wh", "workflow_id": workflow, "secret": "s3cret"},
        )
        assert res.status_code == 200, res.text
    key_file = tmp_path / "s.sqlite3.trigger.key"
    assert key_file.exists() and key_file.stat().st_mode & 0o077 == 0


# --------------------------------------------------------------------------- M1 and L3


async def test_postgres_mode_never_writes_a_trigger_key_file(
    service_database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, full_mode: None
) -> None:
    """WP8 review M1: webhook secrets use NEXURA_TRIGGER_SECRET_KEY only; a second instance
    with the same key reads them; without the key the store refuses and writes no file."""
    _keys(monkeypatch)
    app_a = _pg_app(tmp_path / "a", service_database_url)
    app_b = _pg_app(tmp_path / "b", service_database_url)
    assert app_a.state.trigger_store._secret_key_file is None
    async with _client(app_a) as a, _client(app_b):
        workflow = await _workflow(a)
        res = await a.post(
            f"{API}/triggers/webhooks",
            json={"endpoint_id": "wh", "workflow_id": workflow, "secret": "s3cret-shared"},
        )
        assert res.status_code == 200, res.text
        stored = app_b.state.trigger_store.get_webhook_config("wh")
        assert stored is not None and stored.secret == "s3cret-shared"
        # The variable gone after startup: the next instance's store refuses, writes nothing.
        monkeypatch.delenv("NEXURA_TRIGGER_SECRET_KEY")
        app_c = _pg_app(tmp_path / "c", service_database_url, check_keys=False)
        with pytest.raises(TriggerError, match="NEXURA_TRIGGER_SECRET_KEY"):
            app_c.state.trigger_store.save_webhook_config(
                WebhookConfig(
                    endpoint_id="wh2",
                    workspace_id="default",
                    workflow_id=workflow,
                    release_id="current",
                    secret="x",
                )
            )
        app_c.state.trigger_store.close()
    leftovers = [p for p in tmp_path.rglob("*") if p.is_file() and "blobs" not in p.parts]
    assert leftovers == [], leftovers  # no key file, no SQLite file (only the blob folder)
    assert not list(tmp_path.rglob("*.trigger.key"))


async def test_a_postgres_app_refuses_a_second_lifespan_clearly(
    service_database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, full_mode: None
) -> None:
    """WP8 review L3: a clear error instead of a closed-pool RuntimeError in a store."""
    _keys(monkeypatch)
    app = _pg_app(tmp_path, service_database_url)
    async with app.router.lifespan_context(app):
        pass
    with pytest.raises(ServiceShutDownError, match="already been shut down.*create_api_app"):
        async with app.router.lifespan_context(app):
            pass
    # A new app on the same database starts normally.
    again = _pg_app(tmp_path, service_database_url)
    async with _client(again) as client:
        assert (await client.get("/healthz")).status_code == 200


# --------------------------------------------------------------------------- the trigger tick


def _registry(gate: asyncio.Event | None = None) -> ModuleRegistry:
    registry = ModuleRegistry()

    async def echo(state: dict[str, Any]) -> dict[str, Any]:
        return {"echoed": state.get("msg")}

    async def slow(state: dict[str, Any]) -> dict[str, Any]:
        assert gate is not None
        await gate.wait()
        return {"slow_done": True}

    registry.register("echo", echo)
    registry.register("slow", slow)
    return registry


def _pg_app(
    root: Path, url: str, *, registry: ModuleRegistry | None = None, check_keys: bool = True
) -> Any:
    if not check_keys:
        # Only to build an instance whose key vanished after the refusal check (M1 above).
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(ServiceStores, "require_shared_keys", lambda self: None)
            return _pg_app(root, url, registry=registry)
    return create_api_app(
        db_path=root / "never.sqlite3",
        database_url=url,
        blob_url=(root.parent / "blobs").as_uri(),
        registry=registry or _registry(),
    )


@asynccontextmanager
async def _client(app: Any) -> AsyncIterator[httpx.AsyncClient]:
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1", headers=H
        ) as client,
    ):
        yield client


async def _workflow(client: httpx.AsyncClient, handler: str = "echo") -> str:
    spec = {
        "name": f"wp9-{handler}",
        "entry_node": "n1",
        "terminal_nodes": ["n1"],
        "nodes": {"n1": {"id": "n1", "handler": handler}},
    }
    res = await client.post(f"{API}/workflows", json={"spec": spec})
    assert res.status_code in (200, 201), res.text
    return str(res.json()["workflow_id"])


async def _due_schedule(app: Any, client: httpx.AsyncClient, workflow: str, sid: str) -> None:
    res = await client.post(
        f"{API}/triggers/schedules",
        json={"schedule_id": sid, "workflow_id": workflow, "interval_seconds": 3600},
    )
    assert res.status_code == 200, res.text
    # Due now: one fire (next_run_at exactly one interval back would be a catch-up).
    app.state.trigger_store.update_schedule_next_fire(sid, next_run_at=time.time() - 1)


def _jobs(app: Any, sid: str) -> list[Any]:
    store = app.state.trigger_store
    jobs = [store.get_job(j.job_id) for j in store.list_pending_jobs(limit=500)]
    return [j for j in jobs if j is not None and j.trigger_source_id == sid]


def _all_jobs(app: Any, sid: str) -> list[dict[str, Any]]:
    """Every job of a schedule, finished ones too (the API lists pending and claimed only)."""
    store = app.state.trigger_store
    if hasattr(store, "_call"):  # PostgresTriggerStore

        async def op(conn: Any) -> list[dict[str, Any]]:
            rows = await conn.fetch(
                "SELECT job_id, status FROM nexura_trigger_jobs WHERE trigger_source_id = $1",
                sid,
            )
            return [dict(r) for r in rows]

        result: list[dict[str, Any]] = store._call(op)
        return result
    rows = store._conn.execute(
        "SELECT job_id, status FROM durable_trigger_jobs WHERE trigger_source_id = ?", (sid,)
    ).fetchall()
    return [dict(r) for r in rows]


@pytest.fixture(params=["sqlite", "postgres"])
def make_app(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Callable[..., Any]:
    """One app factory per backend; Postgres apps share one schema (two instances)."""
    monkeypatch.delenv("NEXURA_ALLOW_PARTIAL_POSTGRES", raising=False)
    for name in ("K_SERVICE", "CLOUD_RUN_JOB", "NEXURA_WORKFLOW_STATE_URL", "NEXURA_BLOB_URL"):
        monkeypatch.delenv(name, raising=False)
    if request.param == "postgres":
        url = request.getfixturevalue("service_database_url")
        _keys(monkeypatch)
        counter = iter(range(100))
        return lambda registry=None: _pg_app(tmp_path / f"i{next(counter)}", url, registry=registry)
    if request.config.getoption("--service-backend") == "postgres":
        pytest.skip("the SQLite parameter runs in the SQLite suite")
    for name in ("NEXURA_DATABASE_URL", "DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)
    return lambda registry=None: create_api_app(
        db_path=tmp_path / "shared.sqlite3", registry=registry or _registry()
    )


async def test_a_tick_fires_a_due_schedule_and_runs_the_job(make_app: Callable[..., Any]) -> None:
    app = make_app()
    async with _client(app) as client:
        workflow = await _workflow(client)
        await _due_schedule(app, client, workflow, "s-due")
        summary = await run_trigger_tick(app.state, budget_seconds=20)
        assert summary["status"] == "ok", summary
        assert summary["enqueued"] == 1 and summary["started"] == 1, summary
        assert summary["completed"] == 1 and summary["failed"] == 0, summary
        assert summary["running"] == 0 and summary["reclaimed"] == 0, summary
        assert set(summary) == {
            "status",
            "enqueued",
            "reclaimed",
            "started",
            "completed",
            "failed",
            "running",
            "elapsed_seconds",
            "budget_seconds",
        }
        assert _jobs(app, "s-due") == []  # nothing pending
        runs = await client.get(f"{API}/runs", params={"workflow_id": workflow})
        assert runs.status_code == 200, runs.text
        mine = runs.json()["items"]
        assert len(mine) == 1 and mine[0]["status"] == "completed", mine
        assert [j["status"] for j in _all_jobs(app, "s-due")] == ["completed"]
        # Not due again: the next tick has nothing to do.
        again = await run_trigger_tick(app.state, budget_seconds=20)
        assert again["status"] == "ok" and again["enqueued"] == 0 and again["started"] == 0
        # The lease is released after each pass.
        store = app.state.trigger_store
        assert store.acquire_lease(TICK_LEASE, "probe", ttl_seconds=1)
        store.release_lease(TICK_LEASE, "probe")


async def test_a_tick_keeps_the_event_loop_free_while_claims_wait(
    make_app: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """WP9 review L3: a pass's reclaim and claim store calls run in a worker thread, so a slow
    database round trip (here 0.4 s per call) never stalls the event loop: a loop heartbeat
    keeps ticking while the pass claims, and the job still runs."""
    app = make_app()
    async with _client(app) as client:
        workflow = await _workflow(client)
        await _due_schedule(app, client, workflow, "s-slow-claim")
        store = app.state.trigger_store
        slow = 0.4
        for name in ("list_pending_jobs", "claim_pending_job", "reclaim_expired_jobs"):
            original = getattr(store, name)

            def delayed(*args: Any, _original: Any = original, **kwargs: Any) -> Any:
                time.sleep(slow)  # a slow round trip, blocking whichever thread calls it
                return _original(*args, **kwargs)

            monkeypatch.setattr(store, name, delayed)
        gaps: list[float] = []
        stop = asyncio.Event()

        async def heartbeat() -> None:
            loop = asyncio.get_running_loop()
            last = loop.time()
            while not stop.is_set():
                await asyncio.sleep(0.02)
                now = loop.time()
                gaps.append(now - last)
                last = now

        beat = asyncio.create_task(heartbeat())
        summary = await run_trigger_tick(app.state, budget_seconds=20)
        stop.set()
        await beat
        assert summary["status"] == "ok" and summary["completed"] == 1, summary
        assert gaps and max(gaps) < slow * 0.75, max(gaps)


async def test_a_tick_is_bounded_and_leaves_a_long_job_running(
    make_app: Callable[..., Any],
) -> None:
    gate = asyncio.Event()
    app = make_app(_registry(gate))
    async with _client(app) as client:
        workflow = await _workflow(client, "slow")
        await _due_schedule(app, client, workflow, "s-slow")
        started = time.monotonic()
        summary = await run_trigger_tick(app.state, budget_seconds=0.5)
        elapsed = time.monotonic() - started
        assert summary["status"] == "ok" and summary["started"] == 1, summary
        assert summary["running"] == 1 and summary["completed"] == 0, summary
        assert elapsed < 3.0, elapsed  # the budget, not the job, ends the pass
        assert summary["elapsed_seconds"] < 3.0
        worker = app.state.trigger_tick_worker
        assert len(worker.active_job_ids) == 1  # still running, under its heartbeat
        # The lease is free again: the next pass is not "busy" and does not run the job again.
        nxt = await run_trigger_tick(app.state, budget_seconds=0.2)
        assert nxt["status"] == "ok" and nxt["started"] == 0, nxt
        gate.set()
        for _ in range(200):
            if not worker.active_job_ids:
                break
            await asyncio.sleep(0.05)
        assert not worker.active_job_ids
        assert [j["status"] for j in _all_jobs(app, "s-slow")] == ["completed"]


async def test_a_second_tick_during_a_pass_is_busy_on_any_instance(
    make_app: Callable[..., Any],
) -> None:
    """While one pass runs (here: waiting for a slow job inside its budget), a tick on another
    instance (or the same one) answers busy and starts nothing."""
    gate = asyncio.Event()
    app_a, app_b = make_app(_registry(gate)), make_app(_registry(gate))
    async with _client(app_a) as a, _client(app_b):
        workflow = await _workflow(a, "slow")
        await _due_schedule(app_a, a, workflow, "s-during")
        first = asyncio.create_task(run_trigger_tick(app_a.state, budget_seconds=30))
        for _ in range(200):  # the pass has claimed the job and waits for it
            worker = getattr(app_a.state, "trigger_tick_worker", None)
            if worker is not None and worker.active_job_ids:
                break
            await asyncio.sleep(0.02)
        others = await asyncio.gather(
            run_trigger_tick(app_b.state, budget_seconds=5),
            run_trigger_tick(app_a.state, budget_seconds=5),
        )
        assert [o["status"] for o in others] == ["busy", "busy"], others
        gate.set()
        summary = await first
        assert summary["status"] == "ok" and summary["completed"] == 1, summary
        assert [j["status"] for j in _all_jobs(app_a, "s-during")] == ["completed"]


async def test_shutdown_ends_jobs_a_tick_left_running(make_app: Callable[..., Any]) -> None:
    """A job still running after its pass gets the worker's grace period at shutdown, then is
    cancelled and failed ("worker cancelled"); it is not left claimed."""
    gate = asyncio.Event()  # never set: the job outlives the app
    app = make_app(_registry(gate))
    async with _client(app) as client:
        workflow = await _workflow(client, "slow")
        await _due_schedule(app, client, workflow, "s-shutdown")
        summary = await run_trigger_tick(app.state, budget_seconds=0.3)
        assert summary["running"] == 1, summary
        worker = app.state.trigger_tick_worker
        worker.shutdown_grace_seconds = 0.2
    assert worker.active_job_ids == []
    store_app = make_app()  # the first app's stores are closed: read through a new instance
    async with _client(store_app):
        jobs = _all_jobs(store_app, "s-shutdown")
    assert [j["status"] for j in jobs] == ["failed"], jobs
    job = store_app.state.trigger_store.get_job(jobs[0]["job_id"])
    assert job is not None and job.error_message == "worker cancelled", job


async def test_shutdown_fits_cloud_runs_ten_second_window(
    make_app: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """WP9 review L5: Cloud Run stops a container 10 s after SIGTERM. There, the poller's and
    the tick's grace and the waits of the shutdown phases are capped, so a job a tick left
    running is failed ("worker cancelled") before the kill instead of being cut off; outside
    Cloud Run the defaults are unchanged."""
    from nexura_runtime.service.cloud import CLOUD_RUN_SHUTDOWN_PHASE_SECONDS, shutdown_grace
    from nexura_runtime.testing.blob_store import allow_local_blobs_on_cloud_run

    assert shutdown_grace(20.0) == 20.0  # make_app removed K_SERVICE
    monkeypatch.setenv("K_SERVICE", "nexura-ent-api")
    assert shutdown_grace(20.0) == CLOUD_RUN_SHUTDOWN_PHASE_SECONDS
    assert shutdown_grace(1.0) == 1.0
    assert 3 * CLOUD_RUN_SHUTDOWN_PHASE_SECONDS < 10.0
    gate = asyncio.Event()  # never set: the job outlives the app
    with allow_local_blobs_on_cloud_run():  # the test's blob folder stands in for a bucket
        app = make_app(_registry(gate))
    started = 0.0
    async with _client(app) as client:
        workflow = await _workflow(client, "slow")
        await _due_schedule(app, client, workflow, "s-cloud-run")
        summary = await run_trigger_tick(app.state, budget_seconds=0.3)
        assert summary["running"] == 1, summary
        worker = app.state.trigger_tick_worker
        assert worker.shutdown_grace_seconds == CLOUD_RUN_SHUTDOWN_PHASE_SECONDS
        started = asyncio.get_running_loop().time()
    assert asyncio.get_running_loop().time() - started < 10.0
    assert worker.active_job_ids == []
    monkeypatch.delenv("K_SERVICE")
    store_app = make_app()
    async with _client(store_app):
        jobs = _all_jobs(store_app, "s-cloud-run")
    assert [j["status"] for j in jobs] == ["failed"], jobs


async def test_a_tick_is_busy_while_another_pass_holds_the_lease(
    make_app: Callable[..., Any],
) -> None:
    app = make_app()
    async with _client(app) as client:
        workflow = await _workflow(client)
        await _due_schedule(app, client, workflow, "s-busy")
        store = app.state.trigger_store
        assert store.acquire_lease(TICK_LEASE, "other-instance/pass", ttl_seconds=60)
        busy = await run_trigger_tick(app.state, budget_seconds=5)
        assert busy["status"] == "busy" and set(busy) == {
            "status",
            "elapsed_seconds",
            "budget_seconds",
        }
        assert _jobs(app, "s-busy") == []  # nothing fired while busy
        sched = store.get_schedule("s-busy")
        assert sched is not None and sched.next_run_at < time.time()  # still due
        # A pass whose instance died: once its lease expires, the next tick takes over.
        far = time.time() + 60 + LEASE_MARGIN_SECONDS
        assert store.acquire_lease(TICK_LEASE, "taker", ttl_seconds=1, now=far)
        store.release_lease(TICK_LEASE, "taker")
        assert store.release_lease(TICK_LEASE, "other-instance/pass") is False
        ok = await run_trigger_tick(app.state, budget_seconds=20)
        assert ok["status"] == "ok" and ok["completed"] == 1, ok


async def test_concurrent_ticks_and_the_poller_run_a_due_fire_once(
    make_app: Callable[..., Any],
) -> None:
    """Two instances (Postgres; one file on SQLite) tick together while one also runs the
    in-process poller: the due fire becomes one job and one run."""
    from nexura_runtime.service.triggers.worker import TriggerWorker

    app_a, app_b = make_app(), make_app()
    async with _client(app_a) as a, _client(app_b):
        workflow = await _workflow(a)
        for rnd in range(3):
            sid = f"s-race-{rnd}"
            await _due_schedule(app_a, a, workflow, sid)
            stop = asyncio.Event()
            poller = asyncio.create_task(
                TriggerWorker(app_b.state, poll_interval=0.01).run_forever(stop)
            )
            results = await asyncio.gather(
                *(run_trigger_tick(app.state, budget_seconds=10) for app in (app_a, app_b) * 2)
            )
            for _ in range(200):
                jobs = _all_jobs(app_a, sid)
                if jobs and all(j["status"] in ("completed", "failed") for j in jobs):
                    break
                await asyncio.sleep(0.05)
            stop.set()
            await poller
            jobs = _all_jobs(app_a, sid)
            assert len(jobs) == 1, (rnd, jobs, results)
            assert jobs[0]["status"] == "completed", jobs
            fired = sum(r.get("enqueued", 0) for r in results)
            assert fired <= 1, results  # the poller may have fired it instead
            runs = await a.get(f"{API}/runs", params={"workflow_id": workflow})
            assert runs.json()["total"] == rnd + 1, runs.text  # one run per due fire


def test_the_tick_budget_is_checked() -> None:
    assert check_tick_budget("45") == 45.0
    for bad in (0, -1, 301, float("nan"), float("inf"), "x", None):
        with pytest.raises(ValueError, match="budget"):
            check_tick_budget(bad)  # type: ignore[arg-type]
