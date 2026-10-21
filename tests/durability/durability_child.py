"""Child process entry point for the A6 durability harness.

Run as ``python durability_child.py <config.json>`` by ``durability_harness.Harness``.
Each scenario runs one Nexura path against the test's SQLite file (or Postgres) and
writes a JSON result. Only the operation under test is wrapped by :func:`outcome`, so its
exception becomes part of the result; any other error is a harness bug and makes the
child exit non-zero (the parent then raises ``HarnessError``).
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from collections.abc import Awaitable, Callable, Coroutine
from pathlib import Path
from typing import Any

import durability_harness as h
from nexura_agents import RunContext

TRIGGER_JOB_ID = "job-1"
WORKSPACE = "durability"


async def outcome(
    operation: Callable[[], Awaitable[Any]], describe: Callable[[Any], dict[str, Any]]
) -> dict[str, Any]:
    """Run the operation under test; report its result or the exception it raised."""
    try:
        value = await operation()
    except Exception as exc:
        return {"error_type": type(exc).__name__, "error": str(exc)}
    return describe(value)


# ------------------------------------------------------------------------- SDK agents


def _point_checkpoint_store(base: type) -> type:
    """A checkpoint store that marks a point right after each committed save."""

    class PointCheckpointStore(base):  # type: ignore[misc]
        async def save(self, run_id: str, checkpoint_json: str, owner: str | None = None) -> None:
            await super().save(run_id, checkpoint_json, owner=owner)
            envelope = json.loads(checkpoint_json)
            phase = envelope["phase"]
            if phase == "tools_pending":
                pending = json.loads(envelope["interruption"]).get("pending") or []
                name = "after_tools_pending" if pending else "after_tool_result"
            else:
                name = f"after_{phase}"
            h.point(name, run_id=run_id)

    return PointCheckpointStore


def _open_checkpoint_store(config: dict[str, Any]) -> Any:
    backend = config["backend"]
    if backend["name"] == "sqlite":
        from nexura_agents import SQLiteCheckpointStore

        return _point_checkpoint_store(SQLiteCheckpointStore)(config["db"])
    import nexura_postgres

    base = nexura_postgres.PostgresCheckpointStore
    return _point_checkpoint_store(base)(dsn=backend["dsn"])


def _agent_tools(config: dict[str, Any]) -> list[Any]:
    from nexura_agents import tool

    delay = float(config.get("tool_delay", 0))

    @tool
    def charge(ctx: RunContext[Any], order_id: str) -> str:
        """Charge a card (not idempotent)."""
        h.effect("charge", extra={"runtime_key": getattr(ctx, "idempotency_key", None)})
        h.point("in_tool", run_id=ctx.run_id)
        time.sleep(delay)
        return f"charged {order_id}"

    @tool(idempotent=True)
    def reserve(ctx: RunContext[Any], order_id: str) -> str:
        """Reserve stock (idempotent: the remote de-duplicates on the key)."""
        runtime_key = getattr(ctx, "idempotency_key", None)
        h.effect(
            "reserve",
            key=f"{ctx.run_id}:{ctx.tool_call_id}",
            extra={"runtime_key": runtime_key},
        )
        h.point("in_tool", run_id=ctx.run_id)
        time.sleep(delay)
        return f"reserved {order_id}"

    return [charge, reserve]


def _describe_agent(result: Any) -> dict[str, Any]:
    interruption = result.interruption
    return {
        "status": result.status,
        "final_output": result.final_output,
        "reason": interruption.reason if interruption is not None else None,
        "pending": [call.name for call in interruption.pending] if interruption else [],
        "run_id": result.run_id,
    }


async def scenario_agent(config: dict[str, Any]) -> dict[str, Any]:
    from nexura_agents import Agent, FakeModel, Runner, RunRecoveryError, ToolCall

    store = _open_checkpoint_store(config)
    tools = _agent_tools(config)
    # The run lease must be shorter than the takeover wait of the stale-owner row (K6,
    # claim_wait=10): a frozen owner stops renewing and its lease expires after this.
    runner = Runner(checkpoint_store=store, lease_ttl=float(config.get("lease_ttl", 2.0)))
    if config["mode"] == "run":
        model = FakeModel(
            responses=[
                [ToolCall(id="c1", name=config["tool"], arguments={"order_id": "A1"})],
                "done",
            ]
        )
        agent = Agent(name="Billing", instructions="Bill orders.", model=model, tools=tools)
        return await outcome(lambda: runner.run_async(agent, "Bill A1"), _describe_agent)

    h.point("ready")  # a "go" block here lines up racing recoverers
    agent = Agent(
        name="Billing",
        instructions="Bill orders.",
        model=FakeModel(responses=["done"]),
        tools=tools,
    )
    deadline = time.monotonic() + float(config.get("claim_wait", 0))

    async def recover() -> Any:
        while True:
            try:
                return await runner.recover_async(
                    agent, config["run_id"], on_uncertain=config.get("on_uncertain", "pause")
                )
            except RunRecoveryError as exc:
                if "already being recovered" not in str(exc) or time.monotonic() > deadline:
                    raise
                await asyncio.sleep(0.1)

    return await outcome(recover, _describe_agent)


# --------------------------------------------------------------------- WorkflowEngine


def _point_workflow_store(base: type) -> type:
    """A workflow store that marks a point right after each committed save or claim."""

    class PointWorkflowStore(base):  # type: ignore[misc]
        async def save(self, checkpoint: Any) -> None:
            await super().save(checkpoint)
            cursor = checkpoint.current_node_id or "end"
            h.point(f"after_save_{checkpoint.status}_{cursor}", step=checkpoint.step_count)

        async def claim_approval(self, workflow_run_id: str, approval_id: str) -> bool:
            claimed: bool = await super().claim_approval(workflow_run_id, approval_id)
            if claimed:
                h.point("after_claim_approval", approval_id=approval_id)
            return claimed

    return PointWorkflowStore


def _open_workflow_store(config: dict[str, Any]) -> Any:
    backend = config["backend"]
    if backend["name"] == "sqlite":
        from nexura_runtime.workflow import SQLiteWorkflowStore

        return _point_workflow_store(SQLiteWorkflowStore)(config["db"])
    import nexura_postgres

    # A6 S8 adds this store; confirm its constructor when it lands.
    base = nexura_postgres.PostgresWorkflowStore  # type: ignore[attr-defined]
    return _point_workflow_store(base)(dsn=backend["dsn"])


def workflow_registry() -> Any:
    """``note`` (first node) and ``pay`` (second node): each performs one effect."""
    from nexura_runtime.workflow import ModuleRegistry

    registry = ModuleRegistry()

    async def note(state: dict[str, Any]) -> dict[str, Any]:
        h.effect("note")
        h.point("in_note")
        return {"noted": True}

    async def pay(state: dict[str, Any]) -> dict[str, Any]:
        h.effect("pay")
        h.point("in_pay")
        return {"paid": True}

    registry.register("note", note)
    registry.register("pay", pay)
    return registry


def workflow_spec(gated: bool) -> Any:
    from nexura_runtime.workflow import WorkflowSpec

    return WorkflowSpec.from_dict(
        {
            "name": "durability",
            "entry_node": "a",
            "terminal_nodes": ["b"],
            "nodes": {
                "a": {"id": "a", "handler": "note", "default_transition": "b"},
                "b": {"id": "b", "handler": "pay", "requires_approval": gated},
            },
        }
    )


def _describe_workflow(result: Any) -> dict[str, Any]:
    pending = result.pending_approval
    return {
        "status": result.status,
        "current_node_id": result.current_node_id,
        "approval_id": pending.approval_id if pending is not None else None,
        "pause_reason": getattr(result, "pause_reason", None),
        "pending_effect": getattr(result, "pending_effect", None) is not None,
        "state": result.state,
        "error": result.error,
    }


async def scenario_workflow(config: dict[str, Any]) -> dict[str, Any]:
    from nexura_runtime.workflow import WorkflowEngine

    store = _open_workflow_store(config)
    engine = WorkflowEngine(
        workflow_spec(bool(config.get("gated"))),
        workflow_registry(),
        store=store,
        workspace_id=WORKSPACE,
    )
    run_id = config["run_id"]
    mode = config["mode"]
    if mode == "run":
        return await outcome(lambda: engine.run({}, workflow_run_id=run_id), _describe_workflow)
    if mode == "resume":
        approval_id = config.get("approval_id")
        if approval_id is None:
            checkpoint = await store.get(run_id)
            approval_id = checkpoint.pending_approval.approval_id
        return await outcome(lambda: engine.resume(run_id, approval_id), _describe_workflow)
    if mode == "fork":
        return await outcome(lambda: engine.fork(run_id, -1), _describe_workflow)
    if mode == "recover":
        return await outcome(
            lambda: engine.recover(run_id, on_uncertain="pause"), _describe_workflow
        )
    raise h.HarnessError(f"unknown workflow mode {mode!r}")


# ---------------------------------------------------------------------- trigger worker


async def scenario_trigger(config: dict[str, Any]) -> dict[str, Any]:
    from nexura_runtime.service.api.app import create_api_app
    from nexura_runtime.service.triggers.models import DurableTriggerJob
    from nexura_runtime.service.triggers.worker import TriggerWorker

    if config["backend"]["name"] != "sqlite":
        # A6 S10b: wire PostgresTriggerStore (and the Postgres workflow store) in here.
        raise h.HarnessError("trigger scenarios support SQLite only until A6 S10b")
    registry = workflow_registry()
    app = create_api_app(registry=registry, db_path=config["db"], profile="local-owner")
    store = app.state.trigger_manager.store
    if config.get("setup"):
        workflow = await app.state.store.create_workflow(workflow_spec(gated=False))
        store.enqueue_trigger_job(
            DurableTriggerJob(
                job_id=TRIGGER_JOB_ID,
                workspace_id="default",
                trigger_type="webhook",
                trigger_source_id="durability",
                workflow_id=workflow["workflow_id"],
                release_id="current",
                payload={},
            )
        )

    original_complete = store.complete_job

    def complete_job(job_id: str, error_message: str | None = None, **kwargs: Any) -> Any:
        if error_message is None:
            # The run finished; the job is not completed yet.
            h.point("before_complete_job", run_id=kwargs.get("run_id"))
        return original_complete(job_id, error_message=error_message, **kwargs)

    store.complete_job = complete_job

    worker = TriggerWorker(
        app.state, worker_id=config["name"], lease_seconds=float(config["lease_seconds"])
    )

    async def work() -> Any:
        deadline = time.monotonic() + float(config.get("work_seconds", 15))
        while time.monotonic() < deadline:
            await worker.run_once()
            job = store.get_job(TRIGGER_JOB_ID)
            if job is not None and job.status in ("completed", "failed"):
                return job
            await asyncio.sleep(0.1)
        return store.get_job(TRIGGER_JOB_ID)

    result = await outcome(work, lambda job: {"job": job.model_dump(mode="json")})
    job = store.get_job(TRIGGER_JOB_ID)
    runs: dict[str, Any] = {}
    if job is not None:
        for run_id in [*job.previous_run_ids, job.run_id]:
            if run_id:
                record = await app.state.store.get_run(run_id)
                runs[run_id] = record.get("status") if record else None
    result["runs"] = runs
    return result


# ---------------------------------------------------- legacy DurableExecutionEngine


def _legacy_spec() -> Any:
    from nexura_runtime.runtime.execution import DurableNodeSpec, DurableWorkflowSpec

    return DurableWorkflowSpec(
        workflow_id="durability-legacy",
        entry_node="transfer",
        terminal_nodes=["transfer"],
        nodes={
            "transfer": DurableNodeSpec(
                node_id="transfer", handler="transfer", effect_policy="non_idempotent_write"
            )
        },
    )


async def scenario_legacy(config: dict[str, Any]) -> dict[str, Any]:
    from nexura_runtime.runtime.execution import DurableExecutionEngine, FencingConflictError

    backend = config["backend"]
    repository: Any
    if backend["name"] == "sqlite":
        from nexura_runtime.storage.repository import SQLiteRepository

        repository = SQLiteRepository(config["db"])
    else:
        from nexura_postgres import PostgresRepository

        repository = PostgresRepository(dsn=backend["dsn"])
    engine = DurableExecutionEngine(repository)

    async def transfer(state: dict[str, Any]) -> dict[str, Any]:
        h.effect("transfer")
        h.point("in_effect")
        return {"done": True}

    engine.register_handler("transfer", transfer)
    run_id = config["run_id"]
    spec = _legacy_spec()
    try:
        if config.get("init"):
            if backend["name"] != "sqlite":
                await repository.init_db()
            await engine.init_run(WORKSPACE, run_id, spec)

        async def execute() -> Any:
            deadline = time.monotonic() + 10
            while True:
                try:
                    await engine.claim_run(WORKSPACE, run_id, config["name"], lease_duration=0.5)
                    break
                except FencingConflictError:
                    if time.monotonic() > deadline:
                        raise
                    await asyncio.sleep(0.1)
            return await engine.execute_step(WORKSPACE, run_id, config["name"], spec)

        return await outcome(execute, lambda state: {"status": state.status})
    finally:
        await repository.close()


# ------------------------------------------------------------------------ self-test


async def scenario_selftest(config: dict[str, Any]) -> dict[str, Any]:
    if config.get("crash"):
        raise RuntimeError("selftest child crashed on purpose")
    h.point("ready")
    h.point("after")
    return {"ok": True}


SCENARIOS: dict[str, Callable[[dict[str, Any]], Coroutine[Any, Any, dict[str, Any]]]] = {
    "agent": scenario_agent,
    "workflow": scenario_workflow,
    "trigger": scenario_trigger,
    "legacy": scenario_legacy,
    "selftest": scenario_selftest,
}


def main() -> None:
    config = json.loads(Path(sys.argv[1]).read_text())
    context = h.ChildContext(config)
    h.set_child_context(context)
    result = asyncio.run(SCENARIOS[config["scenario"]](config))
    context.write_result(result)


if __name__ == "__main__":
    main()
