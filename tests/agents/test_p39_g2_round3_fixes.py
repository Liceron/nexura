"""P39 G2 review round 2 fixes (R2-M1, R2-M2, R2-m1, R2-m2) for the coordinator ledger.

- R2-M1: a coordinator node visited again in a workflow cycle runs its tools fresh (the
  workflow step is part of every key); an edited agent never replays an old result.
- R2-M2: with ``replay_model_responses=True`` the re-run replays the recorded model
  responses (the model is not called again) and follows the recorded path; without it, a
  diverging re-run fails closed (positional and repeated-call backstops).
- R2-m1: without an approval database, one coordinator object still never repeats a call.
- R2-m2: replayed steps are reported (event sink, no execution); a completed run visit's
  entries are deleted; ``purge`` removes old rows.

Offline: scripted fake providers, SQLite in ``tmp_path``.
"""

from __future__ import annotations

import importlib
import time
from pathlib import Path
from typing import Any

import pytest
from nexura_agents import ToolCall, tool
from nexura_agents.contracts.context import ExecutionContext


def _provider(responses: list[Any]) -> Any:
    cls = importlib.import_module("nexura_runtime.providers.fake").StreamingFakeProvider
    return cls(canned_responses=responses)


def _tc(name: str, cid: str = "c", **args: Any) -> Any:
    from nexura_agents.types import Message, ModelResponse

    return ModelResponse(
        message=Message(role="assistant", tool_calls=[ToolCall(id=cid, name=name, arguments=args)])
    )


def _final(text: str = "done") -> Any:
    from nexura_agents.types import Message, ModelResponse

    return ModelResponse(message=Message(role="assistant", content=text))


def _approve(manager: Any, approval_id: str, workspace: str = "ws") -> None:
    from nexura_runtime.runtime.approvals import ApprovalDecisionRecord

    req = manager.get_request(workspace, approval_id)
    manager.submit_decision(
        workspace,
        approval_id,
        ApprovalDecisionRecord(
            approval_id=approval_id,
            workspace_id=workspace,
            decided_by_principal_id="reviewer",
            decided_by_role="admin",
            decision="approved",
            expected_payload_digest=req.payload_digest,
        ),
    )


class Sink:
    def __init__(self) -> None:
        self.events: list[Any] = []

    def emit(self, event: Any) -> None:
        self.events.append(event)


def _ctx(sink: Any = None, attempt: str = "att_1", step_key: str | None = None) -> Any:
    return ExecutionContext(
        workspace_id="ws",
        principal_id="p",
        run_id="r",
        node_id="n",
        attempt_id=attempt,
        step_key=step_key,
        event_sink=sink,
    )


def _setup(tmp_path: Path, tools: list[Any], responses: list[Any], **kwargs: Any) -> Any:
    from nexura_runtime.runtime.agents.coordinator import AgentCoordinator
    from nexura_runtime.runtime.agents.definition import AgentDefinition
    from nexura_runtime.runtime.approvals import ApprovalGate, ApprovalPolicyManager

    provider = _provider(responses)
    agent = AgentDefinition(name="worker", model=provider, tools=tools)
    manager = ApprovalPolicyManager(tmp_path / "ledger.db")
    coord = AgentCoordinator([agent], approval_gate=ApprovalGate(manager), **kwargs)
    return coord, manager, provider, agent


def _rows(manager: Any) -> list[tuple[str, str]]:
    with manager._connection() as conn:
        return [
            (str(r[0]), str(r[1]))
            for r in conn.execute("SELECT kind, status FROM coordinator_call_ledger").fetchall()
        ]


# ---------------------------------------------------------------- R2-M2: model replay


async def test_model_replay_follows_the_recorded_path_without_calling_the_model(
    tmp_path: Path,
) -> None:
    from nexura_runtime.runtime.approvals import ApprovalRequiredError

    sent: list[str] = []

    @tool
    def notify() -> str:
        """SMS (ungated, not idempotent)."""
        sent.append("notify")
        return "ok"

    @tool(requires_approval=True)
    def refund() -> str:
        """Refund."""
        sent.append("refund")
        return "refunded"

    # The model is asked three times in all: the re-run replays turns 1 and 2.
    coord, manager, provider, _ = _setup(
        tmp_path,
        [notify, refund],
        [_tc("notify", "a"), _tc("refund", "b"), _final()],
        replay_model_responses=True,
    )
    sink = Sink()
    with pytest.raises(ApprovalRequiredError) as paused:
        await coord.run_async("worker", "go", context=_ctx(sink))
    _approve(manager, paused.value.approval_id)
    done = await coord.run_async("worker", "go", context=_ctx(sink))
    assert done.status == "completed" and sent == ["notify", "refund"]
    assert provider._canned_responses == []  # exactly three model calls
    replayed = [e.payload["event"] for e in sink.events if "event" in e.payload]
    assert replayed == [
        "coordinator.model_replayed",
        "coordinator.tool_replayed",
        "coordinator.model_replayed",
    ]
    assert all(e.payload["executed"] is False for e in sink.events if "event" in e.payload)


async def test_without_model_replay_a_repeated_call_elsewhere_is_not_resent(
    tmp_path: Path,
) -> None:
    """The re-run re-asks the model: turn 1 matches and is replayed, turn 2's approved
    refund runs, then the model asks for the same notify again in turn 3. An earlier
    attempt already sent it: fail closed, nothing sent twice."""
    from nexura_runtime.runtime.agents.errors import CoordinatorCallOutcomeUnknownError
    from nexura_runtime.runtime.approvals import ApprovalRequiredError

    sent: list[str] = []

    @tool
    def notify() -> str:
        """SMS."""
        sent.append("notify")
        return "ok"

    @tool(requires_approval=True)
    def refund() -> str:
        """Refund."""
        sent.append("refund")
        return "refunded"

    coord, manager, _, _ = _setup(
        tmp_path,
        [notify, refund],
        [_tc("notify", "a"), _tc("refund", "b"), _tc("notify", "a"), _tc("refund", "b")]
        + [_tc("notify", "c"), _final()],
    )
    with pytest.raises(ApprovalRequiredError) as paused:
        await coord.run_async("worker", "go", context=_ctx())
    _approve(manager, paused.value.approval_id)
    with pytest.raises(CoordinatorCallOutcomeUnknownError, match="already sent"):
        await coord.run_async("worker", "go", context=_ctx())
    assert sent == ["notify", "refund"]


# ---------------------------------------------------------------- R2-M1: step identity


async def test_a_coordinator_node_in_a_workflow_cycle_runs_its_tools_on_each_visit(
    tmp_path: Path,
) -> None:
    from nexura_runtime.runtime.agents.coordinator import AgentCoordinator
    from nexura_runtime.runtime.agents.definition import AgentDefinition
    from nexura_runtime.runtime.agents.module import AgentCoordinatorModule
    from nexura_runtime.runtime.approvals import ApprovalGate, ApprovalPolicyManager
    from nexura_runtime.workflow import (
        ModuleRegistry,
        SQLiteWorkflowStore,
        WorkflowEngine,
        WorkflowNode,
        WorkflowSpec,
    )

    polled: list[int] = []

    @tool(effect="pure")
    def poll_status() -> str:
        """Current job status."""
        polled.append(1)
        return f"status #{len(polled)}"

    agent = AgentDefinition(
        name="worker",
        model=_provider(
            [_tc("poll_status", "a"), _final("one"), _tc("poll_status", "b")] + [_final("two")]
        ),
        tools=[poll_status],
    )
    coordinator = AgentCoordinator(
        [agent], approval_gate=ApprovalGate(ApprovalPolicyManager(tmp_path / "approvals.db"))
    )
    registry = ModuleRegistry()
    registry.register("coord", AgentCoordinatorModule(coordinator))

    def count(state: dict[str, Any]) -> dict[str, Any]:
        visits = int(state.get("visits", 0)) + 1
        return {"visits": visits, "_branch": "again" if visits < 2 else "stop"}

    registry.register("count", count)
    spec = WorkflowSpec(
        name="g2-cycle",
        entry_node="coord",
        nodes={
            "coord": WorkflowNode(
                id="coord",
                handler="coord",
                input_mapping={"prompt": "prompt"},
                default_transition="count",
            ),
            "count": WorkflowNode(
                id="count", handler="count", transitions={"again": "coord", "stop": "end"}
            ),
        },
        terminal_nodes=["end"],
    )
    engine = WorkflowEngine(spec, registry, store=SQLiteWorkflowStore(tmp_path / "wf.db"))
    result = await engine.run({"prompt": "check the job"})
    assert result.status == "completed", result
    assert polled == [1, 1]  # the second visit called the tool again (no stale replay)


async def test_the_step_is_part_of_the_approval_id_and_the_marker(tmp_path: Path) -> None:
    from nexura_runtime.runtime.agents.coordinator import (
        _coordinator_approval_id,
        _coordinator_run_marker,
    )

    first = _ctx(step_key="node:n#1")
    again = _ctx(step_key="node:n#2")
    same = _ctx(step_key="node:n#1", attempt="att_9")
    ids = [_coordinator_approval_id(c, "w", 0, 1, 0, "pay", {"a": 1}) for c in (first, again)]
    assert ids[0] != ids[1]
    assert _coordinator_approval_id(same, "w", 0, 1, 0, "pay", {"a": 1}) == ids[0]
    assert _coordinator_run_marker(first) != _coordinator_run_marker(again)
    assert _coordinator_run_marker(_ctx(attempt="att_1")) != _coordinator_run_marker(
        _ctx(attempt="att_3")
    )


async def test_an_edited_agent_never_replays_an_old_result(tmp_path: Path) -> None:
    from nexura_runtime.runtime.approvals import ApprovalRequiredError

    looked: list[int] = []

    @tool(effect="pure")
    def lookup() -> str:
        """Look up."""
        looked.append(1)
        return f"fresh #{len(looked)}"

    @tool(requires_approval=True)
    def refund() -> str:
        """Refund."""
        return "refunded"

    coord, manager, _, agent = _setup(
        tmp_path,
        [lookup, refund],
        [_tc("lookup", "a"), _tc("refund", "b"), _tc("lookup", "a"), _tc("refund", "b")]
        + [_final()],
    )
    with pytest.raises(ApprovalRequiredError):
        await coord.run_async("worker", "go", context=_ctx())
    agent.instructions = "Edited after the pause."
    from nexura_runtime.runtime.approvals.errors import ApprovalIdentityMismatchError

    with pytest.raises(ApprovalIdentityMismatchError):  # bound to the old definition
        await coord.run_async("worker", "go", context=_ctx())
    assert looked == [1, 1]  # the lookup ran again for the edited agent, not replayed


# ---------------------------------------------------------------- R2-m2: cleanup


async def test_a_completed_run_visit_leaves_only_its_marker_and_purge_removes_it(
    tmp_path: Path,
) -> None:
    from nexura_runtime.runtime.agents.call_ledger import purge_coordinator_ledger
    from nexura_runtime.runtime.approvals import ApprovalRequiredError

    @tool
    def notify() -> str:
        """SMS."""
        return "ok"

    @tool(requires_approval=True)
    def refund() -> str:
        """Refund."""
        return "refunded"

    coord, manager, _, _ = _setup(
        tmp_path,
        [notify, refund],
        [_tc("notify", "a"), _tc("refund", "b"), _final()],
        replay_model_responses=True,
    )
    with pytest.raises(ApprovalRequiredError) as paused:
        await coord.run_async("worker", "go", context=_ctx())
    kinds = sorted(_rows(manager))
    assert ("call", "done") in kinds and ("model", "done") in kinds
    _approve(manager, paused.value.approval_id)
    assert (await coord.run_async("worker", "go", context=_ctx())).status == "completed"
    assert _rows(manager) == [("run", "done")]  # only the completion marker
    assert purge_coordinator_ledger(manager, 3600) == 0  # still fresh
    time.sleep(0.01)
    assert purge_coordinator_ledger(manager, 0) == 1
    assert _rows(manager) == []


def test_the_memory_ledger_purges_too() -> None:
    from nexura_runtime.runtime.agents.call_ledger import Entry, MemoryCallLedger

    ledger = MemoryCallLedger()
    ledger.finish("ws", "e1", Entry("s", "call", "r", "a1", "t", "d"), "x")
    assert ledger.same_call_elsewhere("ws", "s", "t", "d", "a2")
    assert not ledger.same_call_elsewhere("ws", "s", "t", "d", "a1")
    time.sleep(0.01)
    assert ledger.purge(0) == 1 and ledger.get("ws", "e1") is None


async def test_a_different_call_at_a_position_that_already_ran_fails_closed(
    tmp_path: Path,
) -> None:
    """Attempt 1 looked up order o1, then paused on the refund. The re-run's model asks
    for order o2 at the same position: replaying o1's recorded result for o2 would be
    wrong, and running it would leave the run on a different path. Fail closed."""
    from nexura_runtime.runtime.agents.errors import CoordinatorCallOutcomeUnknownError
    from nexura_runtime.runtime.approvals import ApprovalRequiredError

    looked: list[str] = []

    @tool(effect="pure")
    def lookup(order: str) -> str:
        """Look up an order."""
        looked.append(order)
        return f"order {order}"

    @tool(requires_approval=True)
    def refund() -> str:
        """Refund."""
        return "refunded"

    coord, manager, _, _ = _setup(
        tmp_path,
        [lookup, refund],
        [_tc("lookup", "a", order="o1"), _tc("refund", "b"), _tc("lookup", "a", order="o2")],
    )
    with pytest.raises(ApprovalRequiredError) as paused:
        await coord.run_async("worker", "go", context=_ctx())
    _approve(manager, paused.value.approval_id)
    with pytest.raises(CoordinatorCallOutcomeUnknownError, match="diverged"):
        await coord.run_async("worker", "go", context=_ctx())
    assert looked == ["o1"]
