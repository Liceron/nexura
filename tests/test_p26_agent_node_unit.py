"""P26 units: request_approval()/approved_request() in the engine, module_tool(context=...),
the saved-agent node outside the service, and AgentSpec helpers. Offline (FakeModel only)."""

from __future__ import annotations

from typing import Any

import pytest
from nexura_agents import FakeModel
from nexura_agents.agent import Agent
from nexura_agents.contracts.base import BaseModule
from nexura_agents.contracts.context import ExecutionContext
from nexura_agents.contracts.manifest import ModuleManifest
from nexura_agents.contracts.result import ModuleResult
from nexura_agents.errors import WorkflowError
from nexura_agents.json_schema import validate_instance
from nexura_agents.module_tools import module_tool
from nexura_agents.runner import Runner
from nexura_agents.types import Message, ToolCall

from nexura_runtime.agents_spec import (
    DEFAULT_AGENT_MODEL,
    AgentSpecError,
    parse_agent_spec,
    schema_example,
)
from nexura_runtime.agents_store import (
    AgentNameTakenError,
    AgentNotFoundError,
    AgentRevisionConflictError,
    AgentStore,
)
from nexura_runtime.runtime.agents import BuiltAgent, SavedAgentModule
from nexura_runtime.service.api.chat_models import DEFAULT_CHAT_MODEL
from nexura_runtime.workflow import (
    MemoryWorkflowStore,
    ModuleRegistry,
    WorkflowEngine,
    WorkflowNode,
    WorkflowSpec,
    approved_request,
    request_approval,
)


def _engine(registry: ModuleRegistry, nodes: dict[str, WorkflowNode], entry: str) -> WorkflowEngine:
    spec = WorkflowSpec(name="t", entry_node=entry, nodes=nodes)
    return WorkflowEngine(spec, registry, store=MemoryWorkflowStore())


# ------------------------------------------------------------ engine: request_approval


async def test_node_request_pauses_on_the_approval_path_and_hands_back_details() -> None:
    seen: list[Any] = []
    effects: list[str] = []

    async def ask(state: dict[str, Any]) -> dict[str, Any]:
        granted = approved_request()
        seen.append(granted)
        if granted is None:
            request_approval({"send": state["to"]})
        effects.append(granted["send"])
        return {"sent": granted["send"]}

    async def after(state: dict[str, Any]) -> dict[str, Any]:
        seen.append(approved_request())  # a later node never sees the grant
        return {}

    reg = ModuleRegistry()
    reg.register("ask", ask)
    reg.register("after", after)
    nodes = {
        "a": WorkflowNode(id="a", handler="ask", default_transition="b"),
        "b": WorkflowNode(id="b", handler="after"),
    }
    eng = _engine(reg, nodes, "a")
    paused = await eng.run({"to": "x@example.com"})
    assert paused.status == "paused" and paused.interrupt is None
    pending = paused.pending_approval
    assert pending is not None and pending.node_id == "a"
    assert pending.details == {"send": "x@example.com", "node_request": True}
    assert pending.payload_digest and effects == []
    done = await eng.resume(paused.workflow_run_id, pending.approval_id)
    assert done.status == "completed" and done.state["sent"] == "x@example.com"
    assert effects == ["x@example.com"]
    assert seen == [None, {"send": "x@example.com", "node_request": True}, None]

    # Reject: terminal, the node does not run again.
    again = await eng.run({"to": "y@example.com"})
    assert again.pending_approval is not None
    rejected = await eng.resume(
        again.workflow_run_id, again.pending_approval.approval_id, decision="reject"
    )
    assert rejected.status == "rejected" and effects == ["x@example.com"]


async def test_node_request_inside_a_sub_workflow_pauses_the_parent() -> None:
    effects: list[str] = []

    async def ask(state: dict[str, Any]) -> dict[str, Any]:
        granted = approved_request()
        if granted is None:
            request_approval({"what": "inner write"})
        effects.append(granted["what"])
        return {"done": True}

    reg = ModuleRegistry()
    reg.register("ask", ask)
    inner = WorkflowSpec(
        name="inner", entry_node="i", nodes={"i": WorkflowNode(id="i", handler="ask")}
    )
    nodes = {"outer": WorkflowNode(id="outer", handler="", subgraph=inner)}
    eng = _engine(reg, nodes, "outer")
    paused = await eng.run({})
    assert paused.status == "paused" and paused.pending_approval is not None
    assert paused.pending_approval.node_id == "outer/i" and effects == []
    done = await eng.resume(paused.workflow_run_id, paused.pending_approval.approval_id)
    assert done.status == "completed", done.error
    assert effects == ["inner write"]


async def test_request_approval_outside_a_run_or_in_parallel_is_refused() -> None:
    with pytest.raises(WorkflowError):
        request_approval({"x": 1})

    async def ask(state: dict[str, Any]) -> dict[str, Any]:
        request_approval({"x": 1})
        return {}

    async def other(state: dict[str, Any]) -> dict[str, Any]:
        return {"o": 1}

    reg = ModuleRegistry()
    reg.register("ask", ask)
    reg.register("other", other)
    nodes = {"p": WorkflowNode(id="p", handler="", parallel=["ask", "other"])}
    with pytest.raises(WorkflowError):
        await _engine(reg, nodes, "p").run({})


async def test_request_approval_details_must_be_json_and_bounded() -> None:
    async def bad(state: dict[str, Any]) -> dict[str, Any]:
        request_approval({"obj": object()})
        return {}

    async def huge(state: dict[str, Any]) -> dict[str, Any]:
        request_approval({"x": "y" * 600_000})
        return {}

    for handler in (bad, huge):
        reg = ModuleRegistry()
        reg.register("h", handler)
        with pytest.raises(WorkflowError):
            await _engine(reg, {"h": WorkflowNode(id="h", handler="h")}, "h").run({})


# ------------------------------------------------------- module_tool(context=...)


class _Probe(BaseModule):
    def __init__(self) -> None:
        self.seen: list[ExecutionContext] = []
        self._manifest = ModuleManifest(
            module_id="t.probe",
            version="1.0.0",
            kind="integration",
            title="probe",
            description="d",
            input_schema={"type": "object"},
            effect_policy="read",
        )

    @property
    def manifest(self) -> ModuleManifest:
        return self._manifest

    async def invoke(
        self, context: ExecutionContext, inputs: dict[str, Any], config: dict[str, Any]
    ) -> ModuleResult:
        self.seen.append(context)
        return ModuleResult.success({"ok": True})


async def test_module_tool_context_carries_the_run_but_never_its_approval() -> None:
    probe = _Probe()
    parent = ExecutionContext(
        workspace_id="wsX",
        principal_id="p1",
        run_id="wf-run",
        node_id="node-1",
        approval_granted=True,
    )
    tool = module_tool(probe, name="probe", context=parent)

    def model(messages: list[Message], tools: Any) -> Any:
        if messages[-1].role == "tool":
            return "done"
        return [ToolCall(id="c1", name="probe", arguments={})]

    agent = Agent(name="a", instructions="x", model=FakeModel(handler=model), tools=[tool])
    result = await Runner().run_async(agent, "go")
    assert result.status == "completed"
    ctx = probe.seen[-1]
    assert (ctx.workspace_id, ctx.principal_id, ctx.run_id) == ("wsX", "p1", "wf-run")
    assert ctx.node_id == "node-1/a:probe" and ctx.attributes["tool_call_id"] == "c1"
    assert ctx.approval_granted is False
    # Without a context the old behaviour stays: the agent run id and the default workspace.
    plain = module_tool(probe, name="probe")
    agent = Agent(name="a", instructions="x", model=FakeModel(handler=model), tools=[plain])
    await Runner().run_async(agent, "go")
    assert probe.seen[-1].workspace_id == "default" and probe.seen[-1].run_id != "wf-run"


# ------------------------------------------------------------- the node itself


class _Source:
    def __init__(self, agent: Agent[Any]) -> None:
        self.agent = agent
        self.asked: list[tuple[str, str]] = []

    def build_agent(
        self, workspace_id: str, agent_id: str, context: ExecutionContext | None
    ) -> BuiltAgent:
        self.asked.append((workspace_id, agent_id))
        if agent_id != "agt_1":
            from nexura_runtime.agents_store import AgentNotFoundError

            raise AgentNotFoundError(f"Agent '{agent_id}' not found.")
        return BuiltAgent(agent=self.agent, agent_id=agent_id, revision=1, name="a")


async def test_saved_agent_node_in_a_plain_engine_reads_the_runs_workspace() -> None:
    agent = Agent(name="a", instructions="x", model=FakeModel(handler=lambda m, t: "pong"))
    source = _Source(agent)
    reg = ModuleRegistry()
    reg.register("agent", SavedAgentModule(source))
    nodes = {
        "n": WorkflowNode(
            id="n", handler="agent", config={"agent_id": "agt_1", "output_key": "reply"}
        )
    }
    spec = WorkflowSpec(name="t", entry_node="n", nodes=nodes)
    eng = WorkflowEngine(spec, reg, store=MemoryWorkflowStore(), workspace_id="wsQ")
    done = await eng.run({"input": "ping"})
    assert done.status == "completed" and done.state["reply"] == "pong"
    assert source.asked == [("wsQ", "agt_1")]
    unbound = ModuleRegistry()
    unbound.register("agent", SavedAgentModule())
    with pytest.raises(WorkflowError, match="no agent store"):
        await WorkflowEngine(spec, unbound, store=MemoryWorkflowStore()).run({"input": "x"})


async def test_saved_agent_node_outside_a_workflow_cannot_ask_for_approval() -> None:
    from nexura_agents.tools import tool

    @tool(requires_approval=True)
    def send(to: str) -> str:
        return "sent"

    def model(messages: list[Message], tools: Any) -> Any:
        return [ToolCall(id="c1", name="send", arguments={"to": "x"})]

    agent = Agent(name="a", instructions="x", model=FakeModel(handler=model), tools=[send])
    node = SavedAgentModule(_Source(agent))
    ctx = ExecutionContext(run_id="r", node_id="n")
    result = await node.execute(ctx, {"input": "go"}, {"agent_id": "agt_1"})
    assert result.status == "failure" and result.error_code == "APPROVAL_UNAVAILABLE"


# ------------------------------------------------------------------ spec helpers


def test_default_model_is_the_offline_fixture() -> None:
    assert DEFAULT_AGENT_MODEL == DEFAULT_CHAT_MODEL


def test_parse_errors_carry_field_errors() -> None:
    with pytest.raises(AgentSpecError) as err:
        parse_agent_spec({"name": "ok", "tools": ["a b"], "limits": {"max_tool_calls": -1}})
    fields = {e["field"] for e in err.value.field_errors}
    assert fields == {"tools", "limits.max_tool_calls"}
    with pytest.raises(AgentSpecError):
        parse_agent_spec(["not", "an", "object"])


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "object", "properties": {"a": {"type": "string", "minLength": 2}}},
        {"type": "array", "items": {"type": "integer", "minimum": 3}, "minItems": 2},
        {"type": ["null", "number"], "maximum": -5},
        {
            "$defs": {"x": {"enum": ["k"]}},
            "type": "object",
            "properties": {"x": {"$ref": "#/$defs/x"}},
            "required": ["x"],
        },
        {"type": "boolean"},
    ],
)
def test_schema_example_matches_its_schema(schema: dict[str, Any]) -> None:
    assert validate_instance(schema, schema_example(schema)) == []


def test_store_conflicts_and_isolation(tmp_path: Any) -> None:
    store = AgentStore(tmp_path / "a.db")
    spec = parse_agent_spec({"name": "one"})
    rec = store.create("w1", spec)
    with pytest.raises(AgentNameTakenError):
        store.create("w1", spec)
    store.create("w2", spec)  # same name, other workspace
    with pytest.raises(AgentNotFoundError):
        store.get("w2", rec["agent_id"])
    updated = store.update(
        "w1",
        rec["agent_id"],
        parse_agent_spec({"name": "one", "description": "d"}),
        expected_revision=1,
    )
    assert updated["revision"] == 2
    with pytest.raises(AgentRevisionConflictError) as conflict:
        store.update("w1", rec["agent_id"], spec, expected_revision=1)
    assert conflict.value.current_revision == 2
    with pytest.raises(AgentNotFoundError):
        store.delete("w2", rec["agent_id"])
    store.delete("w1", rec["agent_id"], expected_revision=2)
    assert store.list_agents("w1") == [] and len(store.list_agents("w2")) == 1
