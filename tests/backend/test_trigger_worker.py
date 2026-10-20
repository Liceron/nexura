"""Trigger worker: schedules and signed webhooks execute as real workflow runs (offline)."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from nexura_runtime.cli.main import main as cli_main
from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.service.triggers.models import DurableTriggerJob
from nexura_runtime.service.triggers.signatures import compute_webhook_signature
from nexura_runtime.service.triggers.store import SqliteTriggerStore
from nexura_runtime.service.triggers.worker import (
    TriggerWorker,
    check_release_pin,
)
from nexura_runtime.workflow import ModuleRegistry

T0 = 1_800_000_000.0
BASE = "/api/v1"


def _registry() -> ModuleRegistry:
    registry = ModuleRegistry()

    async def echo(state: dict[str, Any]) -> dict[str, Any]:
        return {"echoed": state.get("msg"), "seen_keys": sorted(state)}

    async def gated(state: dict[str, Any]) -> dict[str, Any]:
        return {"gated_done": True}

    registry.register("echo", echo)
    registry.register("gated", gated)
    return registry


def _spec(handler: str = "echo", *, approval: bool = False, name: str = "Trig") -> dict[str, Any]:
    return {
        "name": name,
        "entry_node": "n1",
        "terminal_nodes": ["n1"],
        "nodes": {"n1": {"id": "n1", "handler": handler, "requires_approval": approval}},
    }


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "worker.sqlite3")


@pytest.fixture
def app(db_path: str):
    return create_api_app(
        registry=_registry(),
        db_path=db_path,
        profile="local-owner",
        approval_required_handlers=frozenset({"gated"}),
    )


@pytest.fixture
async def client(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        headers={"X-Workspace-ID": "default"},
    ) as c:
        yield c


async def _create_workflow(client: httpx.AsyncClient, spec: dict[str, Any]) -> dict[str, Any]:
    res = await client.post(f"{BASE}/workflows", json={"spec": spec})
    assert res.status_code in (200, 201), res.text
    body: dict[str, Any] = res.json()
    return body


async def _schedule(client: httpx.AsyncClient, wf_id: str, **extra: Any) -> None:
    body = {
        "schedule_id": "sched-1",
        "workflow_id": wf_id,
        "interval_seconds": 60,
        "payload": {"msg": "from-schedule"},
        **extra,
    }
    res = await client.post(f"{BASE}/triggers/schedules", json=body)
    assert res.status_code == 200, res.text
    # Make the first fire deterministic relative to T0.
    return None


def _align_schedule(app, next_run_at: float) -> None:
    app.state.trigger_store.update_schedule_next_fire("sched-1", next_run_at=next_run_at)


async def _register_webhook(client: httpx.AsyncClient, wf_id: str, **extra: Any) -> None:
    res = await client.post(
        f"{BASE}/triggers/webhooks",
        json={"endpoint_id": "wh-1", "workflow_id": wf_id, "secret": "s3cret", **extra},
    )
    assert res.status_code == 200, res.text


async def _deliver(
    client: httpx.AsyncClient, payload: dict[str, Any], event_id: str, *, ts: float | None = None
) -> dict[str, Any]:
    ts = ts if ts is not None else time.time()
    # v2 signs the event id too, so a captured delivery cannot be replayed under a new id.
    sig = compute_webhook_signature("s3cret", ts, payload, event_id=event_id)
    res = await client.post(
        f"{BASE}/triggers/webhooks/wh-1/deliver",
        content=json.dumps(payload),
        headers={
            "Content-Type": "application/json",
            "X-Webhook-Signature": sig,
            "X-Webhook-Timestamp": str(ts),
            "X-Webhook-Event-ID": event_id,
        },
    )
    assert res.status_code == 200, res.text
    body: dict[str, Any] = res.json()
    return body


async def test_interval_schedule_creates_completed_run(app, client) -> None:
    wf = await _create_workflow(client, _spec())
    await _schedule(client, wf["workflow_id"])
    _align_schedule(app, T0 + 60)
    worker = TriggerWorker(app.state)

    assert await worker.run_once(now=T0) == []  # not due yet
    assert app.state.trigger_store.list_pending_jobs() == []

    outcomes = await worker.run_once(now=T0 + 60)
    assert len(outcomes) == 1
    out = outcomes[0]
    assert out.status == "completed" and out.error is None and out.run_id

    run = await app.state.store.get_run(out.run_id)
    assert run["status"] == "completed"
    assert run["input_state"] == {"msg": "from-schedule"}
    assert run["current_state"]["echoed"] == "from-schedule"
    assert app.state.workspace_repo.get_resource_workspace("run", out.run_id) == "default"
    events = [e["type"] for e in run.get("events", [])]
    assert "RUN_TRIGGERED" in events

    job = app.state.trigger_store.get_job(out.job_id)
    assert job.status == "completed" and job.run_id == out.run_id
    # Next tick before the next interval does not re-fire.
    assert await worker.run_once(now=T0 + 61) == []


async def test_signed_webhook_delivery_runs_with_payload(app, client) -> None:
    wf = await _create_workflow(client, _spec())
    await _register_webhook(client, wf["workflow_id"])
    delivered = await _deliver(client, {"msg": "hello-hook"}, "evt-1")
    assert delivered["is_duplicate"] is False

    jobs = (await client.get(f"{BASE}/triggers/jobs")).json()["jobs"]
    assert jobs[0]["run_id"] is None

    outcomes = await TriggerWorker(app.state).run_once()
    assert [o.status for o in outcomes] == ["completed"]
    run = await app.state.store.get_run(outcomes[0].run_id)
    assert run["input_state"] == {"msg": "hello-hook"}
    assert run["status"] == "completed"
    job = app.state.trigger_store.get_job(delivered["job_id"])
    assert job.run_id == outcomes[0].run_id


async def test_duplicate_webhook_event_runs_once(app, client) -> None:
    wf = await _create_workflow(client, _spec())
    await _register_webhook(client, wf["workflow_id"])
    ts = time.time()
    first = await _deliver(client, {"msg": "a"}, "evt-dup", ts=ts)
    second = await _deliver(client, {"msg": "a"}, "evt-dup", ts=ts)
    assert second["is_duplicate"] is True and second["job_id"] == first["job_id"]

    outcomes = await TriggerWorker(app.state).run_once()
    assert len(outcomes) == 1
    runs = await app.state.store.list_runs()
    assert len(runs) == 1


async def test_two_workers_race_each_job_runs_once(app, client) -> None:
    wf = await _create_workflow(client, _spec())
    store = app.state.trigger_store
    for i in range(6):
        store.enqueue_trigger_job(
            DurableTriggerJob(
                job_id=f"job_race_{i}",
                workspace_id="default",
                trigger_type="webhook",
                trigger_source_id="wh-x",
                workflow_id=wf["workflow_id"],
                release_id="current",
                payload={"msg": f"m{i}"},
                created_at=T0 + i,
            )
        )
    w1 = TriggerWorker(app.state, worker_id="w1")
    w2 = TriggerWorker(app.state, worker_id="w2")
    r1, r2 = await asyncio.gather(w1.run_once(now=T0), w2.run_once(now=T0))
    all_outcomes = r1 + r2
    assert sorted(o.job_id for o in all_outcomes) == [f"job_race_{i}" for i in range(6)]
    assert all(o.status == "completed" for o in all_outcomes)
    assert len({o.run_id for o in all_outcomes}) == 6
    assert len(await app.state.store.list_runs()) == 6
    assert store.list_pending_jobs() == []


def _enqueue(app, wf_id: str, *, ws: str = "default", release: str = "current") -> str:
    job = DurableTriggerJob(
        job_id=f"job_{wf_id}_{ws}_{release}".replace(":", "_"),
        workspace_id=ws,
        trigger_type="schedule",
        trigger_source_id="sched-x",
        workflow_id=wf_id,
        release_id=release,
        payload={"msg": "x"},
        created_at=T0,
    )
    app.state.trigger_store.enqueue_trigger_job(job)
    return job.job_id


async def test_foreign_workspace_workflow_fails_without_run(app, client) -> None:
    wf = await _create_workflow(client, _spec())
    app.state.workspace_repo.record_resource_ownership(
        "workflow", wf["workflow_id"], "ws_other", "someone"
    )
    job_id = _enqueue(app, wf["workflow_id"])
    outcomes = await TriggerWorker(app.state).run_once()
    assert outcomes[0].status == "failed" and outcomes[0].run_id is None
    assert "does not belong to workspace 'default'" in (outcomes[0].error or "")
    assert app.state.trigger_store.get_job(job_id).status == "failed"
    assert await app.state.store.list_runs() == []


async def test_missing_workflow_fails_without_run(app) -> None:
    _enqueue(app, "wf-missing")
    outcomes = await TriggerWorker(app.state).run_once()
    assert outcomes[0].status == "failed" and "not found" in (outcomes[0].error or "")
    assert await app.state.store.list_runs() == []


async def test_release_pin_matches_and_mismatches(app, client) -> None:
    wf = await _create_workflow(client, _spec())
    wf_id = wf["workflow_id"]
    rev, digest = wf["revision"], wf["spec_hash"]

    ok_jobs = [_enqueue(app, wf_id, release=r) for r in (f"rev:{rev}", f"sha256:{digest}")]
    worker = TriggerWorker(app.state)
    outcomes = await worker.run_once()
    assert {o.job_id: o.status for o in outcomes} == {j: "completed" for j in ok_jobs}

    # Edit the workflow: revision and spec hash change, so old pins are refused.
    updated = _spec(name="Trig changed")
    res = await client.put(
        f"{BASE}/workflows/{wf_id}", json={"spec": updated, "expected_revision": rev}
    )
    assert res.status_code == 200, res.text
    stale = [_enqueue(app, wf_id, release=r) for r in (f"rev:{rev}", digest, "garbage-pin")]
    before = len(await app.state.store.list_runs())
    outcomes = await worker.run_once()
    assert {o.job_id: o.status for o in outcomes} == {j: "failed" for j in stale}
    assert all(o.run_id is None and o.error for o in outcomes)
    assert any("no longer matches" in (o.error or "") for o in outcomes)
    assert any("Unrecognised release pin" in (o.error or "") for o in outcomes)
    assert len(await app.state.store.list_runs()) == before


def test_check_release_pin_rules() -> None:
    h = "ab" * 32
    assert check_release_pin("current", revision=2, spec_hash=h) is None
    assert check_release_pin("2", revision=2, spec_hash=h) is None
    assert check_release_pin("revision:2", revision=2, spec_hash=h) is None
    assert check_release_pin("rev:1", revision=2, spec_hash=h) is not None
    assert check_release_pin(h[:12], revision=2, spec_hash=h) is None
    assert check_release_pin("cd" * 32, revision=2, spec_hash=h) is not None


async def test_approval_gated_workflow_counts_as_started(app, client) -> None:
    wf = await _create_workflow(client, _spec("gated", approval=True))
    job_id = _enqueue(app, wf["workflow_id"])
    outcomes = await TriggerWorker(app.state).run_once()
    assert outcomes[0].status == "completed" and outcomes[0].run_id
    run = await app.state.store.get_run(outcomes[0].run_id)
    assert run["status"] == "pending_approval"
    assert app.state.trigger_store.get_job(job_id).run_id == outcomes[0].run_id


async def test_run_forever_stops_promptly_and_survives_errors(app, client) -> None:
    worker = TriggerWorker(app.state, poll_interval=30.0)
    calls = 0

    async def flaky(now: float | None = None):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("boom")
        return []

    worker.run_once = flaky  # type: ignore[method-assign]
    stop = asyncio.Event()
    worker.poll_interval = 0.01
    task = asyncio.create_task(worker.run_forever(stop))
    for _ in range(200):
        if calls >= 3:
            break
        await asyncio.sleep(0.01)
    assert calls >= 3  # iteration error did not kill the loop
    worker.poll_interval = 30.0
    await asyncio.sleep(0.05)
    started = time.monotonic()
    stop.set()
    await asyncio.wait_for(task, timeout=2.0)
    assert time.monotonic() - started < 1.0


async def test_lifespan_starts_and_stops_worker(db_path: str) -> None:
    app = create_api_app(
        registry=_registry(),
        db_path=db_path,
        profile="local-owner",
        run_trigger_worker=True,
        trigger_poll_interval=0.02,
    )
    async with app.router.lifespan_context(app):
        worker = app.state.trigger_worker
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            headers={"X-Workspace-ID": "default"},
        ) as client:
            wf = await _create_workflow(client, _spec())
            app.state.trigger_store.enqueue_trigger_job(
                DurableTriggerJob(
                    job_id="job_life",
                    workspace_id="default",
                    trigger_type="webhook",
                    trigger_source_id="wh",
                    workflow_id=wf["workflow_id"],
                    release_id="current",
                    payload={"msg": "life"},
                    created_at=T0,
                )
            )
            for _ in range(200):
                job = app.state.trigger_store.get_job("job_life")
                if job.status == "completed":
                    break
                await asyncio.sleep(0.02)
            assert job.status == "completed" and job.run_id
    assert worker is not None
    # Worker task finished on shutdown: no further jobs are processed.
    app2_pending = app.state.trigger_store.list_pending_jobs()
    assert app2_pending == []


async def test_sqlite_run_id_migration(tmp_path: Path) -> None:
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
            ('old', 'default', 'schedule', 's', 'wf', 'current', '{}', 'pending',
             NULL, NULL, 1.0, NULL, NULL);
        """
    )
    conn.commit()
    conn.close()
    store = SqliteTriggerStore(path)
    old = store.get_job("old")
    assert old is not None and old.run_id is None
    done = store.complete_job("old", run_id="run-1")
    assert done is not None and done.run_id == "run-1"
    store.close()
    SqliteTriggerStore(path).close()  # re-opening a migrated database is a no-op


async def test_cli_worker_once_processes_job(
    app, client, db_path: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    mod = tmp_path / "fake_handlers_for_worker.py"
    mod.write_text(
        "def register(registry):\n"
        "    async def echo(state):\n"
        "        return {'echoed': state.get('msg')}\n"
        "    registry.register('echo', echo)\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    wf = await _create_workflow(client, _spec())
    job_id = _enqueue(app, wf["workflow_id"])

    code = await asyncio.to_thread(
        cli_main,
        ["worker", "--db", db_path, "--once", "--handlers", "fake_handlers_for_worker:register"],
    )
    assert code == 0
    out = capsys.readouterr().out
    assert f"job={job_id} status=completed" in out

    job = app.state.trigger_store.get_job(job_id)
    assert job.status == "completed" and job.run_id
    run = await app.state.store.get_run(job.run_id)
    assert run["status"] == "completed"
    sys.modules.pop("fake_handlers_for_worker", None)


async def test_overdue_skip_schedule_does_not_burst(app, client) -> None:
    wf = await _create_workflow(client, _spec())
    await _schedule(client, wf["workflow_id"], missed_run_policy="skip")
    _align_schedule(app, T0)
    worker = TriggerWorker(app.state)
    total = 0
    for _ in range(5):
        total += len(await worker.run_once(now=T0 + 3607))
    assert total == 0
    assert app.state.trigger_store.get_schedule("sched-1").next_run_at > T0 + 3607


async def test_overdue_fire_once_schedule_fires_single_run(app, client) -> None:
    wf = await _create_workflow(client, _spec())
    await _schedule(client, wf["workflow_id"], missed_run_policy="fire_once")
    _align_schedule(app, T0)
    worker = TriggerWorker(app.state)
    total = 0
    for _ in range(5):
        total += len(await worker.run_once(now=T0 + 3607))
    assert total == 1


async def test_two_workers_cannot_double_tick_same_due_schedule(tmp_path: Path) -> None:
    db = str(tmp_path / "x.sqlite3")
    a = create_api_app(registry=_registry(), db_path=db, profile="local-owner")
    b = create_api_app(registry=_registry(), db_path=db, profile="local-owner")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a),
        base_url="http://testserver",
        headers={"X-Workspace-ID": "default"},
    ) as c:
        wf = await _create_workflow(c, _spec())
        await _schedule(c, wf["workflow_id"])
    _align_schedule(a, T0)
    snapshot = a.state.trigger_manager.store.list_schedules()  # both see the same due schedule
    for app_ in (a, b):
        app_.state.trigger_manager.store.list_schedules = lambda *x, _s=snapshot, **k: _s
    a.state.trigger_manager.tick_schedules(T0)
    b.state.trigger_manager.tick_schedules(T0)
    assert len(a.state.trigger_store.list_pending_jobs()) == 1


async def test_cancelled_worker_fails_job_instead_of_leaving_it_claimed(tmp_path: Path) -> None:
    registry = ModuleRegistry()

    async def slow(state: dict[str, Any]) -> dict[str, Any]:
        await asyncio.sleep(60)
        return {}

    registry.register("slow", slow)
    app = create_api_app(registry=registry, db_path=str(tmp_path / "s.db"), profile="local-owner")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        headers={"X-Workspace-ID": "default"},
    ) as c:
        wf = await _create_workflow(c, _spec("slow"))
    app.state.trigger_store.enqueue_trigger_job(
        DurableTriggerJob(
            job_id="j1",
            workspace_id="default",
            trigger_type="webhook",
            trigger_source_id="w",
            workflow_id=wf["workflow_id"],
            release_id="current",
            payload={},
        )
    )
    task = asyncio.create_task(TriggerWorker(app.state).run_once())
    await asyncio.sleep(0.5)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    job = app.state.trigger_store.get_job("j1")
    assert job.status == "failed"
    assert job.error_message == "worker cancelled"
    assert job.run_id is not None
