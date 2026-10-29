"""Trigger job leases: heartbeat renewal, expiry recovery, retries, and concurrency (offline)."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from nexura_runtime.cli.main import main as cli_main
from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.service.triggers.models import DurableTriggerJob
from nexura_runtime.service.triggers.store import (
    MemoryTriggerStore,
    SqliteTriggerStore,
    TriggerStore,
)
from nexura_runtime.service.triggers.worker import TriggerWorker
from nexura_runtime.workflow import ModuleRegistry, WorkflowSpec

BASE = "/api/v1"


def _spec(handler: str) -> dict[str, Any]:
    return {
        "name": "Lease",
        "entry_node": "n1",
        "terminal_nodes": ["n1"],
        "nodes": {"n1": {"id": "n1", "handler": handler}},
    }


def _job(job_id: str, workflow_id: str = "wf", **extra: Any) -> DurableTriggerJob:
    return DurableTriggerJob(
        job_id=job_id,
        workspace_id="default",
        trigger_type="webhook",
        trigger_source_id="w",
        workflow_id=workflow_id,
        release_id="current",
        **extra,
    )


class Env:
    """App with 'echo', 'slow' (blocks on ``gate``) and 'counted' (tracks concurrency)."""

    def __init__(self, db_path: str) -> None:
        self.gate = asyncio.Event()
        self.running = 0
        self.peak = 0
        self.slow_started = 0
        registry = ModuleRegistry()

        async def echo(state: dict[str, Any]) -> dict[str, Any]:
            return {"ok": True}

        async def slow(state: dict[str, Any]) -> dict[str, Any]:
            self.slow_started += 1
            await self.gate.wait()
            return {"slow": True}

        async def counted(state: dict[str, Any]) -> dict[str, Any]:
            self.running += 1
            self.peak = max(self.peak, self.running)
            await asyncio.sleep(0.15)
            self.running -= 1
            return {}

        registry.register("echo", echo)
        registry.register("slow", slow)
        registry.register("counted", counted)
        self.app = create_api_app(registry=registry, db_path=db_path, profile="local-owner")
        self.workflows: dict[str, str] = {}

    async def workflow(self, handler: str) -> str:
        if handler not in self.workflows:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.app),
                base_url="http://testserver",
                headers={"X-Workspace-ID": "default"},
            ) as c:
                res = await c.post(f"{BASE}/workflows", json={"spec": _spec(handler)})
                assert res.status_code in (200, 201), res.text
                self.workflows[handler] = res.json()["workflow_id"]
        return self.workflows[handler]

    @property
    def store(self) -> TriggerStore:
        return self.app.state.trigger_store  # type: ignore[no-any-return]

    def worker(self, **kwargs: Any) -> TriggerWorker:
        return TriggerWorker(self.app.state, **kwargs)


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(str(tmp_path / "lease.sqlite3"))


async def _until(predicate: Any, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition not reached"
        await asyncio.sleep(0.02)


async def test_heartbeat_renewal_keeps_long_job_owned(env: Env) -> None:
    wf = await env.workflow("slow")
    env.store.enqueue_trigger_job(_job("j1", wf))
    worker = env.worker(lease_seconds=0.6)  # heartbeat every 0.2s
    task = asyncio.create_task(worker.run_once())
    await _until(lambda: env.slow_started == 1)
    first = env.store.get_job("j1")
    assert first is not None and first.lease_expires_at is not None

    await asyncio.sleep(1.5)  # more than two full lease periods
    mid = env.store.get_job("j1")
    assert mid is not None and mid.status == "claimed" and mid.claimed_by == worker.worker_id
    assert mid.lease_expires_at is not None and mid.lease_expires_at > time.time()
    assert mid.lease_expires_at > first.lease_expires_at
    other = env.worker(lease_seconds=0.6)
    assert env.store.reclaim_expired_jobs("other", lease_seconds=0.6) == []
    assert await other.run_once() == []

    env.gate.set()
    outcomes = await task
    assert [o.status for o in outcomes] == ["completed"]
    done = env.store.get_job("j1")
    assert done is not None and done.status == "completed"
    assert done.lease_expires_at is None and done.attempts == 1
    assert env.slow_started == 1  # never retried


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_expired_lease_reclaimed_exactly_once_by_racing_workers(kind: str, tmp_path: Path) -> None:
    """The job keeps its run for the new owner to continue (A6 S10; the ``keep_run=False``
    mode of the old ``retry_mode="new_run"`` was removed by A6 S12)."""
    path = str(tmp_path / "race.sqlite3")
    stores: list[TriggerStore]
    if kind == "memory":
        shared = MemoryTriggerStore()
        stores = [shared, shared]
    else:
        stores = [SqliteTriggerStore(path), SqliteTriggerStore(path)]
    stores[0].enqueue_trigger_job(_job("j1"))
    claimed = stores[0].claim_pending_job("j1", "dead", lease_seconds=10, now=1000.0)
    assert claimed is not None and claimed.attempts == 1
    assert claimed.lease_expires_at == 1010.0
    stores[0].set_job_run("j1", "dead", "run-old")

    barrier = threading.Barrier(2)
    results: dict[str, list[DurableTriggerJob]] = {}

    def race(name: str, store: TriggerStore) -> None:
        barrier.wait()
        results[name] = store.reclaim_expired_jobs(name, lease_seconds=10, now=2000.0)

    threads = [
        threading.Thread(target=race, args=("w1", stores[0])),
        threading.Thread(target=race, args=("w2", stores[1])),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    winners = [name for name, jobs in results.items() if jobs]
    assert len(winners) == 1
    job = results[winners[0]][0]
    assert job.claimed_by == winners[0] and job.attempts == 2 and job.status == "claimed"
    assert job.lease_expires_at == 2010.0
    assert job.run_id == "run-old" and job.previous_run_ids == []
    stored = stores[0].get_job("j1")
    assert stored is not None and stored.claimed_by == winners[0]
    # A non-expired claim is never taken.
    assert stores[1].reclaim_expired_jobs("w3", lease_seconds=10, now=2005.0) == []


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_attempts_exhausted_fails_job(kind: str, tmp_path: Path) -> None:
    store: TriggerStore = (
        MemoryTriggerStore() if kind == "memory" else SqliteTriggerStore(str(tmp_path / "a.db"))
    )
    store.enqueue_trigger_job(_job("j1"))
    assert store.claim_pending_job("j1", "w0", lease_seconds=5, now=100.0) is not None
    store.set_job_run("j1", "w0", "run-1")
    t = 100.0
    for attempt in (2, 3):
        t += 10
        got = store.reclaim_expired_jobs("wN", lease_seconds=5, max_attempts=3, now=t)
        assert [g.status for g in got] == ["claimed"] and got[0].attempts == attempt
        store.set_job_run("j1", "wN", f"run-{attempt}")
    t += 10
    got = store.reclaim_expired_jobs("wN", lease_seconds=5, max_attempts=3, now=t)
    assert len(got) == 1 and got[0].status == "failed"
    assert got[0].error_message == "worker lease expired 3 times"
    assert got[0].run_id == "run-3" and got[0].previous_run_ids == ["run-1", "run-2"]
    assert store.reclaim_expired_jobs("wN", lease_seconds=5, max_attempts=3, now=t + 99) == []


async def test_worker_fails_exhausted_job_and_interrupts_its_run(env: Env) -> None:
    wf = await env.workflow("echo")
    state = env.app.state
    spec = WorkflowSpec.from_dict(_spec("echo"))
    await state.store.record_run_start("run-dead", wf, spec, {})
    env.store.enqueue_trigger_job(_job("j1", wf))
    env.store.claim_pending_job("j1", "dead", lease_seconds=1, now=time.time() - 100)
    env.store.set_job_run("j1", "dead", "run-dead")
    # Burn the remaining attempts so the next expiry is the last allowed one.
    env.store.reclaim_expired_jobs("x", lease_seconds=1, max_attempts=9, now=time.time() - 50)
    env.store.reclaim_expired_jobs("x", lease_seconds=1, max_attempts=9, now=time.time() - 40)
    assert await env.worker(max_attempts=3).run_once() == []
    job = env.store.get_job("j1")
    assert job is not None and job.status == "failed"
    assert job.error_message == "worker lease expired 3 times"


async def test_old_run_marked_interrupted_and_new_run_linked(env: Env) -> None:
    wf = await env.workflow("echo")
    state = env.app.state
    await state.store.record_run_start("run-dead", wf, WorkflowSpec.from_dict(_spec("echo")), {})
    assert (await state.store.get_run("run-dead"))["status"] == "running"
    env.store.enqueue_trigger_job(_job("j1", wf))
    env.store.claim_pending_job("j1", "dead", lease_seconds=5, now=time.time() - 100)
    env.store.set_job_run("j1", "dead", "run-dead")

    outcomes = await env.worker().run_once()
    assert [o.status for o in outcomes] == ["completed"]
    new_run = outcomes[0].run_id
    assert new_run and new_run != "run-dead"

    old = await state.store.get_run("run-dead")
    assert old["status"] == "interrupted" and "lease expired" in old["error"]
    assert (await state.store.get_run(new_run))["status"] == "completed"
    job = env.store.get_job("j1")
    assert job is not None and job.status == "completed"
    assert job.run_id == new_run and job.previous_run_ids == ["run-dead"]
    assert job.attempts == 2


async def test_stale_owner_cannot_complete_a_reclaimed_job(env: Env) -> None:
    env.store.enqueue_trigger_job(_job("j1"))
    env.store.claim_pending_job("j1", "old", lease_seconds=1, now=1.0)
    env.store.reclaim_expired_jobs("new", lease_seconds=60, now=500.0)
    stale = env.store.complete_job("j1", error_message="late", expected_worker="old")
    assert stale is not None and stale.status == "claimed" and stale.claimed_by == "new"
    assert not env.store.renew_job_lease("j1", "old", lease_seconds=60)
    assert not env.store.set_job_run("j1", "old", "run-x")
    done = env.store.complete_job("j1", expected_worker="new")
    assert done is not None and done.status == "completed"


async def test_lost_lease_cancels_local_run_without_failing_new_owner(env: Env) -> None:
    wf = await env.workflow("slow")
    env.store.enqueue_trigger_job(_job("j1", wf))
    worker = env.worker(lease_seconds=0.3)
    task = asyncio.create_task(worker.run_once())
    await _until(lambda: env.slow_started == 1)
    # Another worker takes over (as if our heartbeat had stalled).
    env.store.reclaim_expired_jobs("thief", lease_seconds=60, now=time.time() + 1000)
    await asyncio.gather(task, return_exceptions=True)
    job = env.store.get_job("j1")
    assert job is not None and job.status == "claimed" and job.claimed_by == "thief"


async def test_schedule_ticks_continue_while_long_job_runs(env: Env) -> None:
    wf = await env.workflow("slow")
    env.store.enqueue_trigger_job(_job("j1", wf))
    worker = env.worker(poll_interval=0.05)
    ticks: list[float | None] = []
    original = worker.manager.tick_schedules

    def counting(now: float | None = None) -> Any:
        ticks.append(now)
        return original(now)

    worker.manager.tick_schedules = counting  # type: ignore[method-assign]
    stop = asyncio.Event()
    loop_task = asyncio.create_task(worker.run_forever(stop))
    await _until(lambda: env.slow_started == 1)
    before = len(ticks)
    await asyncio.sleep(0.5)
    assert len(ticks) >= before + 3  # ticking while the job is still running
    assert worker.active_job_ids == ["j1"]
    job = env.store.get_job("j1")
    assert job is not None and job.status == "claimed"
    env.gate.set()
    await _until(lambda: (env.store.get_job("j1") or job).status == "completed")
    stop.set()
    await asyncio.wait_for(loop_task, timeout=5)


async def test_concurrency_limit_respected(env: Env) -> None:
    wf = await env.workflow("counted")
    for i in range(5):
        env.store.enqueue_trigger_job(_job(f"j{i}", wf, created_at=100.0 + i))
    outcomes = await env.worker(max_concurrent_jobs=2).run_once()
    assert [o.status for o in outcomes] == ["completed"] * 5
    assert env.peak == 2

    env.peak = 0
    for i in range(5, 8):
        env.store.enqueue_trigger_job(_job(f"j{i}", wf, created_at=200.0 + i))
    await env.worker(max_concurrent_jobs=4).run_once()
    assert env.peak == 3


async def test_shutdown_grace_then_cancel_fails_job(env: Env) -> None:
    wf = await env.workflow("slow")
    env.store.enqueue_trigger_job(_job("j1", wf))
    worker = env.worker(poll_interval=0.05, shutdown_grace_seconds=0.2)
    stop = asyncio.Event()
    loop_task = asyncio.create_task(worker.run_forever(stop))
    await _until(lambda: env.slow_started == 1)
    started = time.monotonic()
    stop.set()
    await asyncio.wait_for(loop_task, timeout=5)
    assert 0.15 <= time.monotonic() - started < 3
    job = env.store.get_job("j1")
    assert job is not None and job.status == "failed" and job.error_message == "worker cancelled"
    assert job.run_id is not None
    run = await env.app.state.store.get_run(job.run_id)
    assert run["status"] == "interrupted"


def test_migration_of_old_trigger_db(tmp_path: Path) -> None:
    path = str(tmp_path / "old.sqlite3")
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE durable_trigger_jobs (
            job_id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, trigger_type TEXT NOT NULL,
            trigger_source_id TEXT NOT NULL, workflow_id TEXT NOT NULL, release_id TEXT NOT NULL,
            payload_json TEXT NOT NULL, status TEXT NOT NULL, claimed_by TEXT, claimed_at REAL,
            created_at REAL NOT NULL, completed_at REAL, error_message TEXT);
        INSERT INTO durable_trigger_jobs VALUES
            ('stuck', 'default', 'schedule', 's', 'wf', 'current', '{"a": 1}', 'claimed',
             'old-worker', 1000.0, 1.0, NULL, NULL),
            ('waiting', 'default', 'schedule', 's', 'wf', 'current', '{}', 'pending',
             NULL, NULL, 2.0, NULL, NULL);
        """
    )
    conn.commit()
    conn.close()

    store = SqliteTriggerStore(path)
    columns = {
        r[1] for r in sqlite3.connect(path).execute("PRAGMA table_info(durable_trigger_jobs)")
    }
    assert {"run_id", "lease_expires_at", "attempts", "previous_run_ids_json"} <= columns
    stuck = store.get_job("stuck")
    assert stuck is not None and stuck.attempts == 0 and stuck.lease_expires_at is None
    assert stuck.previous_run_ids == [] and stuck.payload == {"a": 1}

    # A pre-lease claim is treated as expiring one lease after it was claimed.
    assert store.reclaim_expired_jobs("w", lease_seconds=300, now=1100.0) == []
    got = store.reclaim_expired_jobs("w", lease_seconds=300, now=1400.0)
    assert [j.job_id for j in got] == ["stuck"] and got[0].attempts == 1
    assert got[0].lease_expires_at == 1700.0
    claimed = store.claim_pending_job("waiting", "w", lease_seconds=30, now=1500.0)
    assert claimed is not None and claimed.attempts == 1
    store.close()
    SqliteTriggerStore(path).close()  # reopening a migrated database is a no-op


async def test_cli_worker_lease_flags_parse(
    env: Env, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, Any] = {}

    class FakeWorker:
        worker_id = "fake"

        def __init__(self, app_state: Any, **kwargs: Any) -> None:
            captured.update(kwargs)

        async def run_once(self) -> list[Any]:
            return []

    monkeypatch.setattr("nexura_runtime.service.triggers.worker.TriggerWorker", FakeWorker)
    db = str(tmp_path / "cli.sqlite3")
    code = await asyncio.to_thread(
        cli_main,
        [
            "worker", "--db", db, "--once", "--lease-seconds", "12.5",
            "--max-attempts", "5", "--max-concurrent-jobs", "2",
        ],
    )  # fmt: skip
    assert code == 0
    assert captured["lease_seconds"] == 12.5
    assert captured["max_attempts"] == 5 and captured["max_concurrent_jobs"] == 2

    captured.clear()
    assert await asyncio.to_thread(cli_main, ["worker", "--db", db, "--once"]) == 0
    assert captured["lease_seconds"] == 300.0
    assert captured["max_attempts"] == 3 and captured["max_concurrent_jobs"] == 4


async def test_cli_worker_rejects_invalid_lease(env: Env, tmp_path: Path, capsys: Any) -> None:
    db = str(tmp_path / "cli2.sqlite3")
    code = await asyncio.to_thread(
        cli_main, ["worker", "--db", db, "--once", "--lease-seconds", "0"]
    )
    assert code == 1
    assert "lease_seconds" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        cli_main(["worker", "--lease-seconds", "abc"])


async def test_backlogged_reclaims_not_double_started(env: Env) -> None:
    await env.workflow("slow")
    wf = await env.workflow("slow")
    for i in (1, 2):
        env.store.enqueue_trigger_job(_job(f"j{i}", wf))
        env.store.claim_pending_job(f"j{i}", "dead", lease_seconds=1, now=time.time() - 100)
    worker = env.worker(lease_seconds=0.3, poll_interval=0.5, max_jobs_per_tick=1, max_attempts=9)
    stop = asyncio.Event()
    task = asyncio.create_task(worker.run_forever(stop))
    await asyncio.sleep(2.5)
    env.gate.set()
    await asyncio.sleep(0.3)
    stop.set()
    await task
    assert env.slow_started == 2
    job = env.store.get_job("j2")
    assert job is not None and job.attempts == 2


async def test_run_once_survives_lost_lease_and_keeps_sibling_outcomes(env: Env) -> None:
    wf_slow = await env.workflow("slow")
    wf_echo = await env.workflow("echo")
    env.store.enqueue_trigger_job(_job("j1", wf_slow))
    env.store.enqueue_trigger_job(_job("j2", wf_echo))
    worker = env.worker(lease_seconds=0.3)
    task = asyncio.create_task(worker.run_once())
    await _until(lambda: env.slow_started == 1)

    def sibling_done() -> bool:
        job = env.store.get_job("j2")
        return job is not None and job.status == "completed"

    # The thief reclaims every job whose lease looks expired; the sibling must have finished
    # first (its run takes a few database round trips when runs live in Postgres, MVP WP3).
    await _until(sibling_done)
    env.store.reclaim_expired_jobs("thief", lease_seconds=60, now=time.time() + 1000)
    outcomes = await asyncio.wait_for(task, 5)
    assert [o.job_id for o in outcomes] == ["j2"]


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), 0.0, -1.0])
def test_non_finite_lease_rejected(bad: float) -> None:
    with pytest.raises(ValueError):
        TriggerWorker(object(), lease_seconds=bad)
