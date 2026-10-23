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
# A6 S11: the SDK budget scope of agent runs (the SDK's budget scope is not the run id).
AGENT_BUDGET_SCOPE = "durability-agent-budget"
BUDGET_MAX_TOKENS = 1_000_000
# The budget the handlers and tools of this process reserve against (None: no budget).
_BUDGET: dict[str, Any] = {}


def usage_ledger_path(db: str | Path) -> Path:
    """The SQLite usage ledger next to the test's database (shared by all its processes)."""
    return Path(db).with_name("usage-ledger.sqlite3")


def open_budget(config: dict[str, Any], scope: str, *, create: bool) -> Any:
    """The run budget of ``scope`` on the shared ledger: created by the process that starts
    the run, restored with ``manager.get_run_budget(scope)`` by every later one."""
    if not config.get("budget"):
        return None
    from nexura_agents.usage import BudgetConfig, BudgetManager, UsageLedger

    manager = BudgetManager(ledger=UsageLedger(str(usage_ledger_path(config["db"]))))
    if create:
        budget = manager.create_run_budget(
            WORKSPACE,
            scope,
            config=BudgetConfig(max_tokens=BUDGET_MAX_TOKENS),
            default_model="fake-model",
        )
    else:
        budget = manager.get_run_budget(scope)
        if budget is None:
            raise h.HarnessError(f"budget scope {scope!r} was not restored from the ledger")
    _BUDGET["budget"] = budget
    return budget


def ledger_reservations(db: str | Path, run_id: str) -> list[dict[str, Any]]:
    """Every reservation made for a run's steps (any status), with its settlement entry.

    Read in the test process from the shared usage ledger; each row has ``status``,
    ``step_key``, ``tracked``, ``estimated_cost_usd``, ``estimated_tokens`` and, when
    settled, ``settlement`` (``entry_type``, ``usage_state``, ``amount_usd``, ``tokens``,
    ``settled_by``).
    """
    from nexura_agents.usage import UsageLedger
    from nexura_agents.usage.linkage import frames_of

    ledger = UsageLedger(str(usage_ledger_path(db)))
    entries = {
        e.reservation_id: e
        for e in ledger.get_entries()
        if e.reservation_id and e.entry_type in ("reconcile", "release")
    }
    rows = []
    for res in ledger._reservations.values():
        frame = next((f for f in frames_of(res.metadata) if f.run_id == run_id), None)
        if frame is None:
            continue
        entry = entries.get(res.reservation_id)
        rows.append(
            {
                "status": res.status,
                "for": res.metadata.get("for"),
                "step_key": frame.step_key,
                "tracked": frame.tracked,
                "estimated_cost_usd": res.estimated_cost_usd,
                "estimated_tokens": res.estimated_tokens,
                "settlement": None
                if entry is None
                else {
                    "entry_type": entry.entry_type,
                    "usage_state": entry.usage_state,
                    "amount_usd": entry.amount_usd,
                    "tokens": entry.tokens,
                    "settled_by": entry.metadata.get("settled_by"),
                },
            }
        )
    return sorted(rows, key=lambda row: str(row["step_key"]))


def spend(name: str) -> Any:
    """Reserve a model call against the process's budget before an effect (a tool or
    handler that calls a model); returns the reservation to settle, or None."""
    budget = _BUDGET.get("budget")
    if budget is None:
        return None
    return budget.admit_provider_call("fake-model", estimated_tokens=100, metadata={"for": name})


def settle(reservation: Any) -> None:
    budget = _BUDGET.get("budget")
    if budget is None or reservation is None:
        return
    from nexura_agents import Usage

    budget.settle_provider_call(
        reservation, usage=Usage(prompt_tokens=60, completion_tokens=20, total_tokens=80)
    )


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

        async def consume_approval(self, approval_id: str, payload: str) -> bool:
            consumed: bool = await super().consume_approval(approval_id, payload)
            if consumed:
                h.point("after_consume_approval", approval_id=approval_id)
            return consumed

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
        reservation = spend("charge")
        h.effect("charge", extra={"runtime_key": getattr(ctx, "idempotency_key", None)})
        h.point("in_tool", run_id=ctx.run_id)
        time.sleep(delay)
        settle(reservation)
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

    @tool(requires_approval=True)
    def pay(ctx: RunContext[Any], order_id: str) -> str:
        """Pay an order (not idempotent; a person approves each call)."""
        reservation = spend("pay")
        h.effect("pay", extra={"approval": True})
        h.point("in_tool", run_id=ctx.run_id)
        settle(reservation)
        return f"paid {order_id}"

    @tool
    def hookpay(ctx: RunContext[Any], order_id: str) -> str:
        """Pay an order (not idempotent). Only a hook gates it (P39 G2, ``hook_gate``)."""
        h.effect("hookpay")
        h.point("in_tool", run_id=ctx.run_id)
        return f"paid {order_id}"

    return [charge, reserve, pay, hookpay]


def _agent_hooks(config: dict[str, Any]) -> Any:
    """``hook_gate``: tool names an approval hook gates in this process (P39 G2)."""
    from nexura_agents import AgentHooks

    names = set(config.get("hook_gate") or ())
    if not names:
        return None

    class GateNames(AgentHooks):
        def tool_needs_approval(self, ctx: Any, tool: Any, arguments: Any) -> bool | None:
            return True if tool.name in names else None

    return GateNames()


def _describe_agent(result: Any) -> dict[str, Any]:
    interruption = result.interruption
    budget = _BUDGET.get("budget")
    return {
        "interruption_id": interruption.interruption_id if interruption is not None else None,
        "interruption_json": interruption.to_json() if interruption is not None else None,
        "budget_max_tokens": budget.config.max_tokens if budget is not None else None,
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
    budget = open_budget(config, AGENT_BUDGET_SCOPE, create=config["mode"] == "run")
    # The run lease must be shorter than the takeover wait of the stale-owner row (K6,
    # claim_wait=10): a frozen owner stops renewing and its lease expires after this.
    runner = Runner(
        checkpoint_store=store,
        lease_ttl=float(config.get("lease_ttl", 2.0)),
        budget=budget,
        hooks=_agent_hooks(config),
    )
    if config["mode"] == "run":
        model = FakeModel(
            responses=[
                [ToolCall(id="c1", name=config["tool"], arguments={"order_id": "A1"})],
                "done",
            ]
        )
        agent = Agent(name="Billing", instructions="Bill orders.", model=model, tools=tools)
        return await outcome(lambda: runner.run_async(agent, "Bill A1"), _describe_agent)

    if config["mode"] == "resume":  # approve the stored interruption from this process
        from nexura_agents.durable import decode_checkpoint
        from nexura_agents.types import RunInterruption

        if config.get("interruption"):  # the one a person holds (a replay, for example)
            interruption = RunInterruption.from_json(config["interruption"])
        else:
            raw = await store.load(config["run_id"])
            if raw is None:
                raise h.HarnessError(f"no checkpoint for {config['run_id']}")
            _, interruption = decode_checkpoint(raw, config["run_id"])
        agent = Agent(
            name="Billing",
            instructions="Bill orders.",
            model=FakeModel(responses=["done"]),
            tools=tools,
        )
        return await outcome(
            lambda: runner.resume_async(agent, interruption, approve=True), _describe_agent
        )

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

    base = nexura_postgres.PostgresWorkflowStore
    return _point_workflow_store(base)(dsn=backend["dsn"])


def workflow_registry() -> Any:
    """``note`` (first node) and ``pay`` (second node): each performs one effect."""
    from nexura_runtime.workflow import ModuleRegistry

    registry = ModuleRegistry()

    async def note(state: dict[str, Any]) -> dict[str, Any]:
        reservation = spend("note")
        h.effect("note")
        h.point("in_note")
        settle(reservation)
        return {"noted": True}

    async def pay(state: dict[str, Any]) -> dict[str, Any]:
        reservation = spend("pay")
        h.effect("pay")
        h.point("in_pay")
        settle(reservation)
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


def composite_registry(effect: str | None) -> Any:
    """Handlers of the composite shapes (A6 S7): each performs one effect per item/branch.

    ``effect="idempotent"`` declares them idempotent and sends the step's idempotency key,
    so the effect service de-duplicates a repeat; undeclared they are non_idempotent.
    """
    from nexura_runtime.workflow import ModuleRegistry, step_idempotency_key

    registry = ModuleRegistry()
    keyed = effect == "idempotent"

    def pay_item(state: dict[str, Any]) -> dict[str, Any]:
        index = state["item_index"]
        h.effect(f"item{index}", key=step_idempotency_key() if keyed else None)
        h.point(f"in_item_{index}")
        return {"paid": index}

    def branch(name: str) -> Any:
        def handler(state: dict[str, Any]) -> dict[str, Any]:
            h.effect(f"branch_{name}", key=step_idempotency_key() if keyed else None)
            h.point(f"in_branch_{name}")
            return {name: True}

        return handler

    registry.register("pay_item", pay_item, effect=effect)
    for name in ("a", "b", "c"):
        registry.register(name, branch(name), effect=effect)
    return registry


def composite_spec(shape: str) -> Any:
    from nexura_runtime.workflow import WorkflowSpec

    if shape == "map":
        node = {
            "id": "m",
            "handler": "pay_item",
            "map_over": "items",
            "reducers": {"paid": "append"},
            "max_concurrency": 1,
        }
    elif shape == "parallel":
        node = {"id": "m", "handler": "", "parallel": ["a", "b", "c"], "max_concurrency": 1}
    else:  # a sub-workflow child running the map over the items
        node = {
            "id": "m",
            "handler": "",
            "subgraph": composite_spec("map").to_dict(),
            "output_mapping": {"paid": "paid"},
        }
    return WorkflowSpec.from_dict(
        {
            "name": f"durability-{shape}",
            "entry_node": "m",
            "terminal_nodes": ["m"],
            "nodes": {"m": node},
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
        "pending": getattr(result, "pending_effect", None),
        "state": result.state,
        "error": result.error,
    }


async def scenario_workflow(config: dict[str, Any]) -> dict[str, Any]:
    store = _open_workflow_store(config)
    try:
        return await _workflow_scenario(config, store)
    finally:
        await store.close()


async def _workflow_scenario(config: dict[str, Any], store: Any) -> dict[str, Any]:
    from nexura_runtime.workflow import WorkflowEngine

    shape = config.get("shape", "line")
    if shape == "line":
        spec, registry, initial = (
            workflow_spec(bool(config.get("gated"))),
            workflow_registry(),
            {},
        )
    else:
        spec, registry = composite_spec(shape), composite_registry(config.get("effect"))
        initial = {"items": [0, 1, 2]}
    run_id = config["run_id"]
    mode = config["mode"]
    # The service's convention: a workflow run's budget scope is its run id.
    budget = open_budget(config, run_id, create=mode == "run")
    engine = WorkflowEngine(spec, registry, store=store, workspace_id=WORKSPACE, budget=budget)
    result = await _workflow_mode(config, engine, store, initial)
    if budget is not None:
        result["budget_max_tokens"] = engine.budget.config.max_tokens  # restored policy
    return result


async def _workflow_mode(
    config: dict[str, Any], engine: Any, store: Any, initial: dict[str, Any]
) -> dict[str, Any]:
    run_id = config["run_id"]
    mode = config["mode"]
    if mode == "run":
        return await outcome(
            lambda: engine.run(initial, workflow_run_id=run_id), _describe_workflow
        )
    if mode == "resolve":
        return await outcome(
            lambda: engine.resolve_effect(
                run_id, config["resolution"], actor="ops", result=config.get("result")
            ),
            _describe_workflow,
        )
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

    backend = config["backend"]
    registry = workflow_registry()
    # Postgres: the workflow state, its journal and (A6 S10b) the trigger queue live there.
    app = create_api_app(
        registry=registry,
        db_path=config["db"],
        profile="local-owner",
        workflow_state_url=backend["dsn"] if backend["name"] == "postgres" else "",
    )
    store = app.state.trigger_manager.store
    try:
        return await _trigger_scenario(config, app, store)
    finally:
        await app.state.store.close()
        close = getattr(store, "close", None)
        if close is not None:
            await asyncio.to_thread(close)


async def _trigger_scenario(config: dict[str, Any], app: Any, store: Any) -> dict[str, Any]:
    from nexura_runtime.service.triggers.models import DurableTriggerJob
    from nexura_runtime.service.triggers.worker import TriggerWorker

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


# ------------------------------------------------------------------- service (A6 S9)


def service_registry(config: dict[str, Any]) -> Any:
    """``note`` then ``pay`` for the service: sync handlers (they run in a thread, so the
    run lease heartbeat keeps going while one blocks at a point). Each performs one effect,
    sending the step's idempotency key when it is declared idempotent."""
    from nexura_runtime.workflow import ModuleRegistry, step_idempotency_key

    registry = ModuleRegistry()

    def handler(name: str, effect: str | None) -> Any:
        def run(state: dict[str, Any]) -> dict[str, Any]:
            key = step_idempotency_key() if effect == "idempotent" else None
            h.effect(name, key=key)
            h.point(f"in_{name}")
            return {f"{name}d": True}

        return run

    for name in ("note", "pay"):
        effect = config.get(f"{name}_effect")
        registry.register(name, handler(name, effect), effect=effect)
    return registry


async def scenario_service(config: dict[str, Any]) -> dict[str, Any]:
    """One ``serve`` process on the shared database (and Postgres workflow state).

    Modes: ``start`` runs the workflow (the test kills it at a point), ``observe`` only
    starts the service and reads the run, ``recover`` and ``resolve`` continue it over REST.
    """
    from fastapi.testclient import TestClient

    from nexura_runtime.service.api.app import create_api_app

    backend = config["backend"]
    app = create_api_app(
        registry=service_registry(config),
        db_path=config["db"],
        profile="local-owner",
        workflow_state_url=backend["dsn"] if backend["name"] == "postgres" else "",
        workflow_lease_ttl=float(config["lease_seconds"]),
    )
    api = "/api/v1/runs"
    # A local client as Studio is: loopback host and the server's session token.
    headers = {"X-Workspace-ID": "default", "X-Nexura-Session": app.state.session_token}
    mode = config["mode"]
    result: dict[str, Any] = {"mode": mode}
    with TestClient(
        app,
        base_url="http://127.0.0.1",
        client=("127.0.0.1", 50000),
        raise_server_exceptions=False,
    ) as client:  # lifespan: startup
        if mode == "start":
            response = client.post(
                api, json={"spec": workflow_spec(gated=False).to_dict()}, headers=headers
            )
            result["response"] = response.json()
            return result
        run_id = config["run_id"]
        result["seen_status"] = client.get(f"{api}/{run_id}", headers=headers).json()["status"]
        if mode == "resolve":
            effect_id = config["effect_id"]
            resolved = client.post(
                f"{api}/{run_id}/effects/{effect_id}/resolve",
                json={"resolution": config["resolution"], "result": config.get("result")},
                headers=headers,
            )
            result["resolved"] = resolved.status_code
        if mode in ("recover", "resolve"):
            response = client.post(f"{api}/{run_id}/recover", headers=headers)
            body = response.json()
            result["code"] = response.status_code
            result["status"] = body.get("status")
            result["pause_reason"] = body.get("pause_reason")
            result["effect_id"] = (body.get("pending_effect") or {}).get("effect_id")
            result["state"] = body.get("current_state")
            result["detail"] = body.get("detail")
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
    "service": scenario_service,
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
