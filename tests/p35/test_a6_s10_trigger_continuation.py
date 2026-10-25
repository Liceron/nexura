"""A6 S10: a reclaimed trigger job continues the run it owns (founder decision 3).

In-process tests on one SQLite file. A worker that "died" is a job claimed by ``dead`` with
an expired queue lease and a run started for it; its crash inside a node is simulated with
a ``BaseException`` the engine does not catch (its run lease is given back, as after a
detected exit). The real-process rows (SIGKILL, SQLite and Postgres) are in
``tests/durability/test_durability_triggers.py``. All handlers are local fakes.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from nexura_runtime.cli.main import main as cli_main
from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.service.api.runs import start_workflow_run
from nexura_runtime.service.triggers.models import DurableTriggerJob
from nexura_runtime.service.triggers.worker import TriggerWorker
from nexura_runtime.workflow import ModuleRegistry, WorkflowSpec, step_idempotency_key

WS = "default"


class Crash(BaseException):
    """Stops the engine mid-node the way a killed process would (nothing catches it)."""


class Fakes:
    """``note`` then ``pay``. ``pay`` can crash once after its effect, fail, or block."""

    def __init__(self, pay_effect: str | None = None) -> None:
        self.calls: dict[str, int] = {"note": 0, "pay": 0}
        self.keys: list[str | None] = []
        self.crash_pay = False
        self.fail_pay = False
        self.gate: asyncio.Event | None = None
        self.registry = ModuleRegistry()

        async def note(state: dict[str, Any]) -> dict[str, Any]:
            self.calls["note"] += 1
            return {"noted": True}

        async def pay(state: dict[str, Any]) -> dict[str, Any]:
            self.calls["pay"] += 1
            self.keys.append(step_idempotency_key())
            if self.gate is not None:
                await self.gate.wait()
            if self.crash_pay:
                self.crash_pay = False
                raise Crash
            if self.fail_pay:
                raise RuntimeError("card declined")
            return {"paid": True}

        self.registry.register("note", note)
        self.registry.register("pay", pay, effect=pay_effect)


def _spec(gated: bool = False) -> WorkflowSpec:
    return WorkflowSpec.from_dict(
        {
            "name": "s10",
            "entry_node": "a",
            "terminal_nodes": ["b"],
            "nodes": {
                "a": {"id": "a", "handler": "note", "default_transition": "b"},
                "b": {"id": "b", "handler": "pay", "requires_approval": gated},
            },
        }
    )


class Env:
    def __init__(self, tmp_path: Path, fakes: Fakes, gated: bool = False) -> None:
        self.fakes = fakes
        self.gated = gated
        self.app = create_api_app(
            registry=fakes.registry, db_path=str(tmp_path / "svc.sqlite3"), profile="local-owner"
        )
        self.state = self.app.state
        self.jobs = self.state.trigger_store
        self.workflow_id = ""

    async def dead_worker_started(self, *, crash: bool = False, background: bool = False) -> str:
        """Job ``j1`` claimed by ``dead`` (queue lease long expired) and its run started."""
        workflow = await self.state.store.create_workflow(_spec(self.gated))
        self.workflow_id = workflow["workflow_id"]
        self.jobs.enqueue_trigger_job(
            DurableTriggerJob(
                job_id="j1",
                workspace_id=WS,
                trigger_type="schedule",
                trigger_source_id="s1",
                workflow_id=self.workflow_id,
                release_id="current",
            )
        )
        claimed = self.jobs.claim_pending_job("j1", "dead", lease_seconds=1, now=time.time() - 100)
        assert claimed is not None
        run_ids: list[str] = []

        def record(run_id: str) -> None:
            run_ids.append(run_id)
            assert self.jobs.set_job_run("j1", "dead", run_id)

        self.fakes.crash_pay = crash

        async def start() -> Any:
            return await start_workflow_run(
                self.state,
                spec=_spec(self.gated),
                workflow_id=self.workflow_id,
                workspace_id=WS,
                principal_id="trigger:schedule:s1",
                initial_state={},
                on_run_id=record,
                async_execution=background,
                trigger={"job_id": "j1", "attempt": 1},
            )

        if crash:
            with pytest.raises(Crash):
                await start()
        else:
            await start()
        return run_ids[0]

    def job(self) -> DurableTriggerJob:
        job = self.jobs.get_job("j1")
        assert job is not None
        return job  # type: ignore[no-any-return]

    async def run(self, run_id: str) -> dict[str, Any]:
        run = await self.state.store.get_run(run_id)
        assert run is not None
        return run  # type: ignore[no-any-return]

    async def event_types(self, run_id: str) -> list[str]:
        return [e["type"] for e in (await self.run(run_id))["events"]]


async def test_crash_mid_run_continues_the_same_run_and_pauses_on_the_unknown_effect(
    tmp_path: Path,
) -> None:
    env = Env(tmp_path, Fakes(pay_effect=None))  # undeclared: non_idempotent
    run_id = await env.dead_worker_started(crash=True)
    assert env.fakes.calls == {"note": 1, "pay": 1}
    assert (await env.run(run_id))["status"] == "running"

    outcomes = await TriggerWorker(env.state, worker_id="w2").run_once()

    assert [(o.status, o.run_id) for o in outcomes] == [("completed", run_id)]
    assert env.fakes.calls == {"note": 1, "pay": 1}  # nothing repeated
    run = await env.run(run_id)
    assert run["status"] == "paused" and run["pause_reason"] == "effect_unknown"
    assert run["pending_effect"]["node_id"] == "b"
    job = env.job()
    assert (job.status, job.run_id, job.attempts) == ("completed", run_id, 2)
    assert job.previous_run_ids == []
    recovery = [e for e in run["events"] if e["type"] == "RUN_RECOVERY_STARTED"]
    assert len(recovery) == 1 and recovery[0]["data"]["job_id"] == "j1"
    assert recovery[0]["data"]["attempt"] == 2
    assert len(await env.state.store.list_runs()) == 1  # no new run

    # A person resolves the effect and continues the run: it completes, pay still once.
    with TestClient(env.app) as client:
        headers = {"X-Workspace-ID": WS, "X-Nexura-Session": env.state.session_token}
        effect_id = run["pending_effect"]["effect_id"]
        resolved = client.post(
            f"/api/v1/runs/{run_id}/effects/{effect_id}/resolve",
            json={"resolution": "confirmed", "result": {"paid": True}},
            headers=headers,
        )
        assert resolved.status_code == 200, resolved.text
        recovered = client.post(f"/api/v1/runs/{run_id}/recover", headers=headers)
        assert recovered.status_code == 200, recovered.text
        assert recovered.json()["status"] == "completed"
    assert env.fakes.calls == {"note": 1, "pay": 1}


async def test_crash_mid_run_reruns_an_idempotent_step_with_the_same_key(tmp_path: Path) -> None:
    env = Env(tmp_path, Fakes(pay_effect="idempotent"))
    run_id = await env.dead_worker_started(crash=True)

    outcomes = await TriggerWorker(env.state).run_once()

    assert [(o.status, o.run_id) for o in outcomes] == [("completed", run_id)]
    assert env.fakes.calls == {"note": 1, "pay": 2}
    assert env.fakes.keys[0] is not None and env.fakes.keys == [env.fakes.keys[0]] * 2
    assert (await env.run(run_id))["status"] == "completed"
    assert env.job().run_id == run_id


async def test_a_completed_run_is_never_run_again(tmp_path: Path) -> None:
    """Killed after the run completed, before ``complete_job``: the job completes from it."""
    env = Env(tmp_path, Fakes())
    run_id = await env.dead_worker_started()
    assert (await env.run(run_id))["status"] == "completed"
    assert env.job().status == "claimed"
    events_before = await env.event_types(run_id)

    outcomes = await TriggerWorker(env.state).run_once()

    assert [(o.status, o.run_id) for o in outcomes] == [("completed", run_id)]
    assert env.fakes.calls == {"note": 1, "pay": 1}
    assert await env.event_types(run_id) == events_before  # not even a recovery
    job = env.job()
    assert (job.status, job.run_id, job.previous_run_ids) == ("completed", run_id, [])


async def test_a_failed_run_fails_the_job_without_running_again(tmp_path: Path) -> None:
    env = Env(tmp_path, Fakes(pay_effect="idempotent"))
    env.fakes.fail_pay = True
    run_id = await env.dead_worker_started()
    assert (await env.run(run_id))["status"] == "failed"

    outcomes = await TriggerWorker(env.state).run_once()

    assert [(o.status, o.run_id) for o in outcomes] == [("failed", run_id)]
    assert outcomes[0].error is not None and "card declined" in outcomes[0].error
    assert env.fakes.calls == {"note": 1, "pay": 1}
    job = env.job()
    assert job.status == "failed" and job.run_id == run_id


async def test_a_run_waiting_for_approval_counts_as_started(tmp_path: Path) -> None:
    env = Env(tmp_path, Fakes(), gated=True)
    run_id = await env.dead_worker_started()
    assert (await env.run(run_id))["status"] == "pending_approval"

    outcomes = await TriggerWorker(env.state).run_once()

    assert [(o.status, o.run_id) for o in outcomes] == [("completed", run_id)]
    assert (await env.run(run_id))["status"] == "pending_approval"
    assert env.fakes.calls == {"note": 1, "pay": 0}


async def test_the_run_lease_decides_the_worker_waits_for_a_live_run(tmp_path: Path) -> None:
    """The job lease expired, but the run's lease is live (another worker still executes
    it): the reclaiming worker does not start or recover anything; it waits, then
    completes the job from the run's result."""
    env = Env(tmp_path, Fakes())
    env.fakes.gate = asyncio.Event()
    run_id = await env.dead_worker_started(background=True)
    deadline = time.monotonic() + 5
    while env.fakes.calls["pay"] == 0:
        assert time.monotonic() < deadline, "pay never started"
        await asyncio.sleep(0.02)

    worker = TriggerWorker(env.state, worker_id="w2", poll_interval=0.05, lease_seconds=3)
    task = asyncio.create_task(worker.run_once())
    await asyncio.sleep(0.6)
    assert not task.done()
    job = env.job()
    assert (job.status, job.claimed_by, job.run_id) == ("claimed", "w2", run_id)
    assert (await env.run(run_id))["status"] == "running"
    assert "RUN_RECOVERY_STARTED" not in await env.event_types(run_id)

    env.fakes.gate.set()  # the other worker finishes the run
    outcomes = await asyncio.wait_for(task, 10)
    assert [(o.status, o.run_id) for o in outcomes] == [("completed", run_id)]
    assert env.fakes.calls == {"note": 1, "pay": 1}
    assert env.job().status == "completed"


async def test_an_exhausted_job_leaves_a_live_run_alone(tmp_path: Path) -> None:
    env = Env(tmp_path, Fakes())
    env.fakes.gate = asyncio.Event()
    run_id = await env.dead_worker_started(background=True)
    deadline = time.monotonic() + 5
    while env.fakes.calls["pay"] == 0:
        assert time.monotonic() < deadline, "pay never started"
        await asyncio.sleep(0.02)

    assert await TriggerWorker(env.state, max_attempts=1).run_once() == []
    job = env.job()
    assert job.status == "failed" and job.error_message == "worker lease expired 1 times"
    assert job.run_id == run_id
    assert (await env.run(run_id))["status"] == "running"  # its run lease is live

    env.fakes.gate.set()
    deadline = time.monotonic() + 10
    while (await env.run(run_id))["status"] == "running":
        assert time.monotonic() < deadline, "the run never finished"
        await asyncio.sleep(0.05)
    assert (await env.run(run_id))["status"] == "completed"


async def test_new_run_mode_was_removed_and_names_the_replacement(tmp_path: Path) -> None:
    """A6 S12: ``retry_mode="new_run"`` (a new run from the entry node, repeating effects)
    is gone. Asking for it is refused before anything runs, with a message that names what
    a reclaimed job does instead; the crashed run is then continued as usual."""
    env = Env(tmp_path, Fakes(pay_effect=None))
    old_run = await env.dead_worker_started(crash=True)
    with pytest.raises(ValueError, match="retry_mode='new_run' was removed") as refused:
        TriggerWorker(env.state, retry_mode="new_run")  # type: ignore[arg-type]
    assert "WorkflowEngine.recover" in str(refused.value)
    assert "fork" in str(refused.value)
    assert env.fakes.calls == {"note": 1, "pay": 1}  # refusing ran nothing

    outcomes = await TriggerWorker(env.state).run_once()
    assert [o.run_id for o in outcomes] == [old_run]  # the same run, continued
    assert env.fakes.calls == {"note": 1, "pay": 1}  # and nothing repeated
    job = env.job()
    assert job.run_id == old_run and job.previous_run_ids == []


async def test_cli_new_run_mode_is_refused_with_the_replacement(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The real worker refuses ``--retry-mode new_run``: no worker starts, exit code 1."""
    db = str(tmp_path / "cli-refused.sqlite3")
    args = ["worker", "--db", db, "--once", "--retry-mode", "new_run"]
    try:
        code = await asyncio.to_thread(cli_main, args)
    except SystemExit as exc:  # once the CLI drops the choice, argparse refuses it
        code = exc.code
    assert code not in (0, None)
    if code == 1:
        assert "retry_mode='new_run' was removed" in capsys.readouterr().err


def test_retry_mode_is_validated() -> None:
    with pytest.raises(ValueError, match="retry_mode"):
        TriggerWorker(object(), retry_mode="restart")  # type: ignore[arg-type]
    assert TriggerWorker(object()).retry_mode == "continue"


async def test_cli_worker_passes_the_retry_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
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
    assert await asyncio.to_thread(cli_main, ["worker", "--db", db, "--once"]) == 0
    assert captured["retry_mode"] == "continue"
    args = ["worker", "--db", db, "--once", "--retry-mode", "new_run"]
    assert await asyncio.to_thread(cli_main, args) == 0
    assert captured["retry_mode"] == "new_run"
    with pytest.raises(SystemExit):
        cli_main(["worker", "--retry-mode", "restart"])
