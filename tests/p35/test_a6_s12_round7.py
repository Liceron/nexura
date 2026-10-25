"""S12 round 7: a re-run of an open step key remembers that the key already started (R6-1),
and a rejected saved-agent node is confirmed only when every tool call returned (L6-4).
Local fakes only (no network, no paid calls)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from nexura_agents import FakeModel
from nexura_agents.agent import Agent
from nexura_agents.contracts.context import ExecutionContext
from nexura_agents.contracts.execution import EffectStatus, ExecutionId
from nexura_agents.errors import WorkflowError
from nexura_agents.run_context import ModelRetry
from nexura_agents.tools import tool
from nexura_agents.types import Message, ToolCall

from nexura_runtime.agents_store import AgentNotFoundError
from nexura_runtime.runtime.agents import BuiltAgent, SavedAgentModule
from nexura_runtime.workflow import (
    ModuleRegistry,
    SQLiteWorkflowStore,
    WorkflowEngine,
    WorkflowNode,
    WorkflowSpec,
    approved_request,
    interrupt,
    request_approval,
)


async def effects(store: Any, run_id: str = "r") -> list[Any]:
    snap = await (await store.open_execution_store()).get(ExecutionId("default", run_id))
    return [] if snap is None else list(snap.effects.values())


def system_settled(found: list[Any]) -> list[Any]:
    return [e for e in found if e.resolved_by == "workflow:pre_dispatch"]


class Source:
    def __init__(self, agent: Agent[Any]) -> None:
        self.agent, self.revision, self.deleted = agent, 1, False

    def build_agent(self, ws: str, agent_id: str, ctx: ExecutionContext | None) -> Any:
        if self.deleted:
            raise AgentNotFoundError(f"Agent '{agent_id}' not found.")
        return BuiltAgent(agent=self.agent, agent_id=agent_id, revision=self.revision, name="a")


def saved(tmp_path: Path, source: Source) -> WorkflowEngine:
    registry = ModuleRegistry()
    registry.register("agent", SavedAgentModule(source))
    spec = WorkflowSpec(
        name="r7",
        entry_node="n",
        nodes={"n": WorkflowNode(id="n", handler="agent", config={"agent_id": "agt_1"})},
    )
    return WorkflowEngine(spec, registry, store=SQLiteWorkflowStore(tmp_path / "r7.db"))


def gated_writer(writes: list[str]) -> Agent[Any]:
    @tool(requires_approval=True)
    def write(note: str) -> str:
        """Writes a note (gated local fake write)."""
        writes.append(note)
        return "written"

    def model(messages: list[Message], tools: Any) -> Any:
        done = sum(1 for m in messages if m.role == "tool")
        if done == 0:
            return [ToolCall(id="c1", name="write", arguments={"note": "A"})]
        if done == 1:
            return [ToolCall(id="c2", name="write", arguments={"note": "B"})]
        return "finished"

    return Agent(name="a", instructions="x", model=FakeModel(handler=model), tools=[write])


async def assert_unknown_not_hidden(eng: WorkflowEngine) -> None:
    result = await eng.get_result("r")
    assert result.status == "paused" and result.pause_reason == "effect_unknown"
    found = await effects(eng.store)
    assert system_settled(found) == []
    assert [e.status for e in found] == [EffectStatus.INDETERMINATE]


# ------------------------------------------------------------------ R6-1 service shapes


@pytest.mark.parametrize("change", ["deleted", "edited"])
async def test_saved_agent_refused_on_rerun_after_a_write_pauses_unknown(
    tmp_path: Path, change: str
) -> None:
    """Every write gated: A approved and written under the open key, then the agent asks for
    B. The saved agent is deleted or edited while B waits; approving B is refused before the
    agent runs, but the key already wrote A, so a person settles it."""
    writes: list[str] = []
    source = Source(gated_writer(writes))
    eng = saved(tmp_path, source)
    first = await eng.run({"input": "go"}, workflow_run_id="r")
    second = await eng.resume("r", first.pending_approval.approval_id)
    assert second.pending_approval is not None and writes == ["A"]
    if change == "deleted":
        source.deleted = True
    else:
        source.revision = 2
    await eng.resume("r", second.pending_approval.approval_id)
    await assert_unknown_not_hidden(eng)
    assert writes == ["A"]
    done = await eng.resolve_effect("r", "confirmed", actor="operator", result={})
    assert done.status == "completed" and writes == ["A"]


async def test_plain_handler_rerun_refused_by_deadline_pauses_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[str] = []

    async def ask(state: dict[str, Any]) -> dict[str, Any]:
        if approved_request() is None:
            sent.append("before")
            request_approval({"send": 1})
        sent.append("after")
        return {}

    registry = ModuleRegistry()
    registry.register("ask", ask)
    spec = WorkflowSpec(name="d", entry_node="a", nodes={"a": WorkflowNode(id="a", handler="ask")})
    store = SQLiteWorkflowStore(tmp_path / "d.db")
    paused = await WorkflowEngine(spec, registry, store=store).run({}, workflow_run_id="r")
    original = WorkflowEngine._invoke

    async def late(self: Any, *args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(0.3)
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(WorkflowEngine, "_invoke", late)
    tight = WorkflowEngine(spec, registry, store=store, timeout=0.1)
    waiting = await tight.resume("r", paused.pending_approval.approval_id)
    assert waiting.pause_reason == "effect_unknown"
    await assert_unknown_not_hidden(WorkflowEngine(spec, registry, store=store))
    assert sent == ["before"]


async def test_interrupt_rerun_refused_before_invocation_pauses_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[str] = []

    async def ask(state: dict[str, Any]) -> dict[str, Any]:
        sent.append("sent")
        return {"answer": interrupt("which?")}

    registry = ModuleRegistry()
    registry.register("ask", ask)
    spec = WorkflowSpec(name="i", entry_node="a", nodes={"a": WorkflowNode(id="a", handler="ask")})
    store = SQLiteWorkflowStore(tmp_path / "i.db")
    paused = await WorkflowEngine(spec, registry, store=store).run({}, workflow_run_id="r")
    snap = await (await store.open_execution_store()).get(ExecutionId("default", "r"))
    assert "a#1" in snap.run.state["started_steps"]  # durable, saved with the pause
    original = WorkflowEngine._invoke

    async def late(self: Any, *args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(0.3)
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(WorkflowEngine, "_invoke", late)
    waiting = await WorkflowEngine(spec, registry, store=store, timeout=0.1).resume_interrupt(
        "r", paused.interrupt["interrupt_id"], "x"
    )
    assert waiting.pause_reason == "effect_unknown"
    await assert_unknown_not_hidden(WorkflowEngine(spec, registry, store=store))
    assert sent == ["sent"]


async def test_first_dispatch_refusal_is_still_not_applied(tmp_path: Path) -> None:
    """Control: a key dispatched for the first time and refused before it starts ran
    nothing, so the system still settles it (unchanged)."""
    source = Source(gated_writer([]))
    source.deleted = True
    eng = saved(tmp_path, source)
    with pytest.raises(WorkflowError, match="not found"):
        await eng.run({"input": "go"}, workflow_run_id="r")
    [effect] = await effects(eng.store)
    assert effect.resolved_by == "workflow:pre_dispatch"


# ------------------------------------------------------------------- L6-4 tool outcomes


async def test_rejection_after_a_tool_that_raised_waits_for_a_person(tmp_path: Path) -> None:
    attempts: list[str] = []

    @tool
    def write(note: str) -> str:
        """Writes, then asks the model to retry (local fake: it may have written)."""
        attempts.append(note)
        raise ModelRetry("try again later")

    @tool(requires_approval=True)
    def send(to: str) -> str:
        """Sends (gated)."""
        return "sent"

    def model(messages: list[Message], tools: Any) -> Any:
        if not any(m.role == "tool" for m in messages):
            return [ToolCall(id="c1", name="write", arguments={"note": "n1"})]
        return [ToolCall(id="c2", name="send", arguments={"to": "x"})]

    agent = Agent(name="a", instructions="x", model=FakeModel(handler=model), tools=[write, send])
    eng = saved(tmp_path, Source(agent))
    paused = await eng.run({"input": "go"}, workflow_run_id="r")
    assert paused.pending_approval is not None and attempts == ["n1"]
    waiting = await eng.resume("r", paused.pending_approval.approval_id, decision="reject")
    assert waiting.status == "paused" and waiting.pause_reason == "effect_unknown"
    snap = await (await eng.store.open_execution_store()).get(ExecutionId("default", "r"))
    [request] = snap.run.state["node_requests"].values()
    assert request["tool_results"] == [{"id": "c1", "tool": "write", "status": "raised"}]
    done = await eng.resolve_effect("r", "confirmed", actor="operator", result={})
    assert done.status == "rejected" and attempts == ["n1"]


async def test_earlier_attempt_statuses_carry_over_to_the_rejected_rerun(tmp_path: Path) -> None:
    """Three gated writes: A and B approved, C rejected. In the third run of the open key,
    A's result is replayed from history; its status comes from the earlier attempt."""
    writes: list[str] = []

    @tool(requires_approval=True)
    def write(note: str) -> str:
        """Writes a note (gated local fake write)."""
        writes.append(note)
        return "written"

    def model(messages: list[Message], tools: Any) -> Any:
        done = sum(1 for m in messages if m.role == "tool")
        note = "ABC"[done] if done < 3 else None
        if note is None:
            return "finished"
        return [ToolCall(id=f"c{done + 1}", name="write", arguments={"note": note})]

    agent = Agent(name="a", instructions="x", model=FakeModel(handler=model), tools=[write])
    eng = saved(tmp_path, Source(agent))
    pending = (await eng.run({"input": "go"}, workflow_run_id="r")).pending_approval
    for _ in range(2):
        pending = (await eng.resume("r", pending.approval_id)).pending_approval
    assert pending is not None and writes == ["A", "B"]
    rejected = await eng.resume("r", pending.approval_id, decision="reject")
    assert rejected.status == "rejected" and writes == ["A", "B"]
    [effect] = await effects(eng.store)
    assert effect.status == EffectStatus.CONFIRMED and system_settled([effect]) == []
    statuses = {r["id"]: r["status"] for r in effect.result["agent_tool_results"]}
    assert statuses == {"c1": "succeeded", "c2": "succeeded"}
