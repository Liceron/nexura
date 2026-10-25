"""S12 round 6: a node that asked for approval already ran (R5-1); mapped module nodes are
checked per item (R5-2); a module cannot claim "not dispatched" after it started (Low).
Local fakes only (no network, no paid calls)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from nexura_agents import FakeModel
from nexura_agents.agent import Agent
from nexura_agents.contracts import BaseModule, ModuleManifest, ModuleResult
from nexura_agents.contracts.context import ExecutionContext
from nexura_agents.contracts.execution import EffectStatus, ExecutionId
from nexura_agents.errors import WorkflowError
from nexura_agents.policy import _unlock_for_tests
from nexura_agents.tools import tool
from nexura_agents.types import Message, ToolCall
from nexura_guardrails import GuardrailPolicy, Rule, When

from nexura_runtime.runtime.agents import BuiltAgent, SavedAgentModule
from nexura_runtime.workflow import (
    ModuleRegistry,
    SQLiteWorkflowStore,
    WorkflowEngine,
    WorkflowNode,
    WorkflowSpec,
    approved_request,
    request_approval,
)


@pytest.fixture(autouse=True)
def _clean() -> Any:
    _unlock_for_tests()
    yield
    _unlock_for_tests()


async def effects(store: Any, run_id: str = "r") -> list[Any]:
    snap = await (await store.open_execution_store()).get(ExecutionId("default", run_id))
    return sorted(snap.effects.values(), key=lambda e: str(e.intent.step_key))


def no_pre_dispatch(found: list[Any]) -> bool:
    return all(e.resolved_by != "workflow:pre_dispatch" for e in found)


def saved_agent_engine(tmp_path: Path, model: Any, tools: list[Any]) -> WorkflowEngine:
    agent = Agent(name="a", instructions="x", model=FakeModel(handler=model), tools=tools)

    class Source:
        def build_agent(self, ws: str, agent_id: str, ctx: ExecutionContext | None) -> Any:
            return BuiltAgent(agent=agent, agent_id=agent_id, revision=1, name="a")

    registry = ModuleRegistry()
    registry.register("agent", SavedAgentModule(Source()))
    spec = WorkflowSpec(
        name="r6",
        entry_node="n",
        nodes={"n": WorkflowNode(id="n", handler="agent", config={"agent_id": "agt_1"})},
    )
    return WorkflowEngine(spec, registry, store=SQLiteWorkflowStore(tmp_path / "r6.db"))


# ------------------------------------------------------------------ R5-1: saved agents


async def test_saved_agent_ungated_write_then_rejection_confirms_what_ran(tmp_path: Path) -> None:
    writes: list[str] = []

    @tool
    def write(note: str) -> str:
        """Writes a note (local fake external write)."""
        writes.append(note)
        return "written"

    @tool(requires_approval=True)
    def send(to: str) -> str:
        """Sends (gated)."""
        return "sent"

    def model(messages: list[Message], tools: Any) -> Any:
        if not any(m.role == "tool" for m in messages):
            return [ToolCall(id="c1", name="write", arguments={"note": "n1"})]
        return [ToolCall(id="c2", name="send", arguments={"to": "x"})]

    eng = saved_agent_engine(tmp_path, model, [write, send])
    paused = await eng.run({"input": "go"}, workflow_run_id="r")
    assert paused.pending_approval is not None and writes == ["n1"]
    [open_effect] = await effects(eng.store)
    assert open_effect.status == EffectStatus.PENDING  # never settled while it waits
    rejected = await eng.resume("r", paused.pending_approval.approval_id, decision="reject")
    assert rejected.status == "rejected" and writes == ["n1"]
    [effect] = await effects(eng.store)
    assert effect.status == EffectStatus.CONFIRMED and no_pre_dispatch([effect])
    # Round 7 (L6-4): each call also records how it ended.
    assert effect.result["agent_tool_results"] == [
        {"id": "c1", "tool": "write", "status": "succeeded"}
    ]
    assert effect.result["rejected_approval_id"] == paused.pending_approval.approval_id


async def test_saved_agent_every_write_gated_a_approved_b_rejected(tmp_path: Path) -> None:
    writes: list[str] = []

    @tool(requires_approval=True)
    def write(note: str) -> str:
        """Writes a note (local fake external write, gated)."""
        writes.append(note)
        return "written"

    def model(messages: list[Message], tools: Any) -> Any:
        done = sum(1 for m in messages if m.role == "tool")
        if done == 0:
            return [ToolCall(id="c1", name="write", arguments={"note": "A"})]
        if done == 1:
            return [ToolCall(id="c2", name="write", arguments={"note": "B"})]
        return "finished"

    eng = saved_agent_engine(tmp_path, model, [write])
    first = await eng.run({"input": "go"}, workflow_run_id="r")
    assert first.pending_approval is not None and writes == []
    second = await eng.resume("r", first.pending_approval.approval_id)
    assert second.status == "paused" and second.pending_approval is not None
    assert writes == ["A"]
    rejected = await eng.resume("r", second.pending_approval.approval_id, decision="reject")
    assert rejected.status == "rejected" and writes == ["A"]
    found = await effects(eng.store)
    assert [e.status for e in found] == [EffectStatus.CONFIRMED] and no_pre_dispatch(found)
    assert found[0].result["agent_tool_results"] == [
        {"id": "c1", "tool": "write", "status": "succeeded"}
    ]


async def test_saved_agent_refusal_before_its_run_is_still_not_applied(tmp_path: Path) -> None:
    """A refusal returned before the module marks its start ran nothing."""
    eng = saved_agent_engine(tmp_path, lambda m, t: "x", [])
    with pytest.raises(WorkflowError, match="no 'input'"):
        await eng.run({"other": 1}, workflow_run_id="r")
    assert (await eng.get_result("r")).status == "failed"
    [effect] = await effects(eng.store)
    assert effect.status == EffectStatus.RESOLVED and effect.resolution.value == "not_applied"
    assert effect.resolved_by == "workflow:pre_dispatch"


# ------------------------------------------------------------ R5-1: plain handlers


def plain_engine(tmp_path: Path, handler: Any, effect: str | None = None) -> WorkflowEngine:
    registry = ModuleRegistry()
    if effect is None:
        registry.register("h", handler)
    else:
        registry.register("h", handler, effect=effect)
    spec = WorkflowSpec(
        name="p",
        entry_node="a",
        nodes={
            "a": WorkflowNode(id="a", handler="h", default_transition="b"),
            "b": WorkflowNode(id="b", handler="h"),
        },
    )
    return WorkflowEngine(spec, registry, store=SQLiteWorkflowStore(tmp_path / "p.db"))


@pytest.mark.parametrize("settled", ["not_applied", "confirmed"])
async def test_unsafe_node_request_rejection_waits_for_a_person_then_ends_rejected(
    tmp_path: Path, settled: str
) -> None:
    sent: list[str] = []

    async def ask(state: dict[str, Any]) -> dict[str, Any]:
        sent.append("before")  # what the node may have done before asking is unknown
        if approved_request() is None:
            request_approval({"send": "x"})
        return {}

    eng = plain_engine(tmp_path, ask)
    paused = await eng.run({}, workflow_run_id="r")
    assert paused.pending_approval is not None
    waiting = await eng.resume("r", paused.pending_approval.approval_id, decision="reject")
    assert waiting.status == "paused" and waiting.pause_reason == "effect_unknown"
    assert no_pre_dispatch(await effects(eng.store))
    done = await eng.resolve_effect("r", settled, actor="reviewer", result={})
    assert done.status == "rejected" and sent == ["before"]


async def test_pure_node_request_rejection_ends_rejected_at_once(tmp_path: Path) -> None:
    async def ask(state: dict[str, Any]) -> dict[str, Any]:
        if approved_request() is None:
            request_approval({"why": "check"})
        return {}

    eng = plain_engine(tmp_path, ask, effect="pure")
    paused = await eng.run({}, workflow_run_id="r")
    rejected = await eng.resume("r", paused.pending_approval.approval_id, decision="reject")
    assert rejected.status == "rejected"


async def test_misuse_after_a_write_raises_pauses_and_fails_after_settlement(
    tmp_path: Path,
) -> None:
    writes: list[int] = []

    async def pay(state: dict[str, Any]) -> dict[str, Any]:
        writes.append(1)
        request_approval({"o": object()})
        return {}

    eng = plain_engine(tmp_path, pay)
    with pytest.raises(WorkflowError, match="JSON-serializable"):
        await eng.run({}, workflow_run_id="r")
    paused = await eng.get_result("r")
    assert paused.status == "paused" and paused.pause_reason == "effect_unknown"
    assert no_pre_dispatch(await effects(eng.store))
    done = await eng.resolve_effect("r", "confirmed", actor="reviewer", result={})
    assert done.status == "failed" and "JSON-serializable" in (done.error or "")
    assert writes == [1]  # node b never ran; nothing was sent again


async def test_crash_while_pausing_for_a_node_request_recovers_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Crash(BaseException):
        pass

    sent: list[str] = []

    async def ask(state: dict[str, Any]) -> dict[str, Any]:
        sent.append("x")
        if approved_request() is None:
            request_approval({"send": "x"})
        return {}

    eng = plain_engine(tmp_path, ask)

    async def crash(*args: Any, **kwargs: Any) -> Any:
        raise Crash

    with monkeypatch.context() as patched:
        patched.setattr(WorkflowEngine, "_pause_for_node_request", crash)
        with pytest.raises(Crash):
            await eng.run({}, workflow_run_id="r")
    recovered = await plain_engine(tmp_path, ask).recover("r")
    assert recovered.status == "paused" and recovered.pause_reason == "effect_unknown"
    assert sent == ["x"]


# ------------------------------------------------- Low: "not dispatched" is internal


class FlagAfterWrite(BaseModule):
    """A third-party module that writes, then claims the old internal flag."""

    def __init__(self, wrote: list[int]) -> None:
        self.wrote = wrote
        self._manifest = ModuleManifest(
            module_id="fake.flag",
            version="1.0.0",
            kind="integration",
            title="flag",
            description="local fake",
            effect_policy="non_idempotent_write",
        )

    @property
    def manifest(self) -> ModuleManifest:
        return self._manifest

    async def invoke(self, context: Any, inputs: dict[str, Any], config: dict[str, Any]) -> Any:
        self.wrote.append(1)
        return ModuleResult.failure("X", "refused", metadata={"workflow_not_dispatched": True})


async def test_module_cannot_claim_not_dispatched_after_it_started(tmp_path: Path) -> None:
    wrote: list[int] = []
    registry = ModuleRegistry()
    registry.register("m", FlagAfterWrite(wrote))
    spec = WorkflowSpec(name="f", entry_node="m", nodes={"m": WorkflowNode(id="m", handler="m")})
    eng = WorkflowEngine(spec, registry, store=SQLiteWorkflowStore(tmp_path / "f.db"))
    paused = await eng.run({}, workflow_run_id="r")
    assert paused.status == "paused" and paused.pause_reason == "effect_unknown"
    assert wrote == [1] and no_pre_dispatch(await effects(eng.store))


# ----------------------------------------------------- R5-2: mapped module nodes per item


class Charge(BaseModule):
    def __init__(self, charged: list[Any]) -> None:
        self.charged = charged
        self._manifest = ModuleManifest(
            module_id="fake.charge",
            version="1.0.0",
            kind="integration",
            title="charge",
            description="local fake charge",
            input_schema={
                "type": "object",
                "properties": {"amount": {"type": "integer"}},
                "required": ["amount"],
            },
            effect_policy="non_idempotent_write",
        )

    @property
    def manifest(self) -> ModuleManifest:
        return self._manifest

    async def invoke(self, context: Any, inputs: dict[str, Any], config: dict[str, Any]) -> Any:
        self.charged.append(inputs["amount"])
        return ModuleResult.success({f"receipt_{inputs['amount']}": "r"})  # one key per item


def mapped(tmp_path: Path, charged: list[Any], action: str) -> WorkflowEngine:
    registry = ModuleRegistry()
    registry.register("charge", Charge(charged))
    node = WorkflowNode(
        id="charge",
        handler="charge",
        map_over="amounts",
        map_as="amt",
        input_mapping={"amount": "amt"},
    )
    spec = WorkflowSpec(name="m", entry_node="charge", nodes={"charge": node})
    policy = GuardrailPolicy(
        rules=[
            Rule(
                "big",
                When(stage="tool_input", tools="charge", args={"$.amount": {"gt": 100}}),
                action=action,  # type: ignore[arg-type]
            )
        ]
    )
    store = SQLiteWorkflowStore(tmp_path / "m.db")
    return WorkflowEngine(spec, registry, store=store, guardrail_policy=policy)


async def test_mapped_block_names_the_item_and_dispatches_no_item(tmp_path: Path) -> None:
    charged: list[Any] = []
    eng = mapped(tmp_path, charged, "block")
    with pytest.raises(WorkflowError, match=r"rule 'big' \(item 1\)"):
        await eng.run({"amounts": [5, 500]}, workflow_run_id="r")
    assert charged == []
    assert await effects(eng.store) == []
    done = await mapped(tmp_path / "ok", [], "block").run({"amounts": [5, 6]})
    assert done.status == "completed"


async def test_mapped_approval_gates_the_step_then_runs_every_item(tmp_path: Path) -> None:
    charged: list[Any] = []
    eng = mapped(tmp_path, charged, "require_approval")
    paused = await eng.run({"amounts": [5, 500]}, workflow_run_id="r")
    assert paused.status == "paused" and paused.pending_approval is not None and charged == []
    done = await eng.resume("r", paused.pending_approval.approval_id)
    assert done.status == "completed" and sorted(charged) == [5, 500]
    unchecked: list[Any] = []
    assert (
        await mapped(tmp_path / "s", unchecked, "require_approval").run({"amounts": [5]})
    ).status == ("completed")
