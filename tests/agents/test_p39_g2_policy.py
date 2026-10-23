"""P39 G2: default hooks, inheritance, the coverage gaps and hook gates after a crash.

- ``set_default_hooks`` applies to every ``Runner``; a runner's own ``hooks=`` compose
  with it (the default first) and never replace it; ``Runner(default_hooks=False)`` (for
  tests) leaves out only the process default, never hooks inherited from an enclosing run.
- Inheritance: a nested ``Runner()``, ``Agent.as_tool`` sub-runs, teams, handoffs, the
  runtime's ``AgentCoordinator`` and ``AgentDefinition.as_tool``, and ``AgentMCPServer``.
- Gaps: coordinator and MCP plain tools go through guardrails, hooks and approval gates;
  handoff and knowledge tools have an origin; a ``ToolModule`` workflow node honours the
  wrapped tool's ``requires_approval``.
- MIN7: a hook's approval gate is recorded in the run's journal, so a resume or recovery
  by a runner without that hook still waits for a person (fail closed).

All offline: ``FakeModel``, in-memory stores; crashes are a ``BaseException``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
from nexura_agents import (
    Agent,
    AgentHooks,
    AgentMCPServer,
    FakeModel,
    GuardrailResult,
    MemoryCheckpointStore,
    ResolvedOutcome,
    RunContext,
    RunInterruption,
    Runner,
    RunResult,
    SequentialAgent,
    Tool,
    ToolCall,
    default_hooks,
    handoff,
    set_default_hooks,
    tool,
)
from nexura_agents.contracts.execution import ExecutionId, NotApplied
from nexura_agents.errors import SDKValidationError
from nexura_agents.mcp.serve import ApprovalRequiredOverMCP, ToolGuardrailRejected
from nexura_agents.policy import active_hooks, effective_hooks


class Crash(BaseException):
    """Simulated process death: the runner keeps the checkpoint."""


@pytest.fixture(autouse=True)
def _no_default_hooks() -> Iterator[None]:
    from nexura_agents import policy

    policy._unlock_for_tests()  # also clears a locked default left by another test
    yield
    policy._unlock_for_tests()


def call(name: str, call_id: str = "c1", **arguments: Any) -> list[ToolCall]:
    return [ToolCall(id=call_id, name=name, arguments=arguments)]


def held(result: RunResult[Any]) -> RunInterruption:
    assert result.interruption is not None
    return result.interruption


def agent_with(tools: list[Any], responses: list[Any], name: str = "A") -> Agent:
    return Agent(
        name=name, instructions="Be helpful.", model=FakeModel(responses=responses), tools=tools
    )


class Recorder(AgentHooks):
    """Records tool calls and run starts, under a label."""

    def __init__(self, label: str, log: list[str]) -> None:
        self.label = label
        self.log = log

    async def on_run_start(self, ctx: RunContext[Any]) -> None:
        self.log.append(f"{self.label}:run:{ctx.agent_name}")

    async def before_tool(self, ctx: RunContext[Any], call: ToolCall) -> str | None:
        self.log.append(f"{self.label}:tool:{call.name}")
        return None


class GateNames(AgentHooks):
    """Requires approval for the named tools (deterministic, name-based)."""

    def __init__(self, *names: str) -> None:
        self.names = set(names)

    def tool_needs_approval(
        self, ctx: RunContext[Any], tool: Tool, arguments: Mapping[str, Any]
    ) -> bool | None:
        return True if tool.name in self.names else None


class Answer(AgentHooks):
    """Answers every tool call itself (``before_tool`` takes over)."""

    def __init__(self, text: str) -> None:
        self.text = text

    async def before_tool(self, ctx: RunContext[Any], call: ToolCall) -> str | None:
        return self.text


# ---------------------------------------------------------------- the default policy


async def test_the_default_hooks_apply_to_every_runner() -> None:
    log: list[str] = []
    policy = Recorder("policy", log)
    set_default_hooks(policy)
    assert default_hooks() is policy

    @tool
    def lookup() -> str:
        """Look up."""
        return "found"

    await agent_with([lookup], [call("lookup"), "ok"]).run_async("go")  # Runner() inside
    await Runner().run_async(agent_with([lookup], [call("lookup"), "ok"]), "go")
    assert log == ["policy:run:A", "policy:tool:lookup"] * 2


async def test_explicit_hooks_compose_with_the_default_and_never_replace_it() -> None:
    log: list[str] = []
    policy, mine = Recorder("policy", log), Recorder("mine", log)
    set_default_hooks(policy)
    ran: list[str] = []

    @tool
    def lookup() -> str:
        """Look up."""
        ran.append("lookup")
        return "found"

    runner = Runner(hooks=mine)
    assert runner.hooks is mine  # the runner's own hooks are unchanged
    await runner.run_async(agent_with([lookup], [call("lookup"), "ok"]), "go")
    # The policy is first (outermost), the developer's hooks still run.
    assert log == ["policy:run:A", "mine:run:A", "policy:tool:lookup", "mine:tool:lookup"]

    # A list does not drop it either, and the policy's takeover wins over the developer's.
    set_default_hooks([Answer("blocked by policy")])
    result = await Runner(hooks=[Answer("mine")]).run_async(
        agent_with([lookup], [call("lookup"), "ok"]), "go"
    )
    tool_results = [m.content for m in result.messages if m.role == "tool"]
    assert tool_results == ["blocked by policy"] and ran == ["lookup"]


async def test_a_developer_hook_cannot_lift_the_policy_approval_gate() -> None:
    class NeverGate(AgentHooks):
        def tool_needs_approval(self, ctx: Any, tool: Any, arguments: Any) -> bool:
            return False

    set_default_hooks(GateNames("pay"))
    ran: list[str] = []

    @tool
    def pay() -> str:
        """Pay."""
        ran.append("pay")
        return "paid"

    result = await Runner(hooks=[NeverGate()]).run_async(
        agent_with([pay], [call("pay"), "ok"]), "go"
    )
    assert result.status == "paused" and ran == []


async def test_the_same_hooks_object_is_used_once() -> None:
    log: list[str] = []
    policy = Recorder("policy", log)
    set_default_hooks(policy)

    @tool
    def lookup() -> str:
        """Look up."""
        return "found"

    await Runner(hooks=[policy]).run_async(agent_with([lookup], [call("lookup"), "ok"]), "go")
    assert log == ["policy:run:A", "policy:tool:lookup"]
    assert Runner(hooks=policy).effective_hooks() is policy  # a single object, as is


def test_set_default_hooks_validates_like_runner_hooks() -> None:
    for bad in ("x", {AgentHooks()}, object(), AgentHooks):
        with pytest.raises(SDKValidationError):
            set_default_hooks(bad)  # type: ignore[arg-type]
    set_default_hooks([])
    assert default_hooks() is None


async def test_the_test_only_opt_out_skips_the_default_but_not_inherited_hooks() -> None:
    log: list[str] = []
    set_default_hooks(Recorder("policy", log))

    @tool
    def lookup() -> str:
        """Look up."""
        return "found"

    await Runner(default_hooks=False).run_async(agent_with([lookup], [call("lookup"), "ok"]), "go")
    assert log == []

    @tool
    async def nested() -> str:
        """Run a nested agent with the opt-out."""
        inner = agent_with([lookup], [call("lookup"), "inner done"], name="Inner")
        result = await Runner(default_hooks=False).run_async(inner, "go")
        return str(result.output)

    await Runner().run_async(agent_with([nested], [call("nested"), "ok"]), "go")
    # The nested run's opt-out cannot drop the policy inherited from the enclosing run.
    assert "policy:run:Inner" in log and "policy:tool:lookup" in log
    with pytest.raises(SDKValidationError):
        Runner(default_hooks="no")  # type: ignore[arg-type]


async def test_runs_in_progress_keep_their_hooks_and_nothing_leaks_to_the_caller() -> None:
    log: list[str] = []
    seen: list[Any] = []

    @tool
    def lookup() -> str:
        """Look up."""
        seen.append(active_hooks.get())
        return "found"

    mine = Recorder("mine", log)
    runner = Runner(hooks=mine)
    events = runner.stream_async(agent_with([lookup], [call("lookup"), "ok"]), "go")
    async for _ in events:
        assert active_hooks.get() is None  # the consumer is outside the run
    assert seen == [mine]
    assert effective_hooks() is None


# ---------------------------------------------------------------- inheritance


async def test_a_nested_runner_inherits_the_enclosing_runs_hooks() -> None:
    log: list[str] = []

    @tool
    def lookup() -> str:
        """Look up."""
        return "found"

    @tool
    async def nested() -> str:
        """Run a nested agent."""
        inner = agent_with([lookup], [call("lookup"), "inner done"], name="Inner")
        return str((await Runner().run_async(inner, "go")).output)

    await Runner(hooks=Recorder("mine", log)).run_async(
        agent_with([nested], [call("nested"), "ok"]), "go"
    )
    assert log == ["mine:run:A", "mine:tool:nested", "mine:run:Inner", "mine:tool:lookup"]


async def test_as_tool_sub_runs_keep_the_callers_hooks_and_gates() -> None:
    log: list[str] = []
    ran: list[str] = []

    @tool
    def pay() -> str:
        """Pay."""
        ran.append("pay")
        return "paid"

    @tool
    def lookup() -> str:
        """Look up."""
        return "found"

    payer = agent_with([pay, lookup], [call("lookup", "l1"), call("pay", "p1"), "x"], "Payer")
    boss = agent_with([payer.as_tool("payer")], [call("payer", request="pay A1"), "ok"], "Boss")
    from nexura_agents.errors import ToolError

    # The caller's gate reached the sub-run: it paused, which a sub-agent tool call cannot
    # wait for (documented as_tool behaviour), so the call fails before anything is paid.
    with pytest.raises(ToolError, match="need human"):
        await Runner(hooks=[Recorder("mine", log), GateNames("pay")]).run_async(boss, "go")
    assert "mine:tool:lookup" in log and "mine:run:Payer" in log
    assert ran == []
    assert boss.tools_by_name["payer"].origin == "agent:Payer"


async def test_handoff_targets_run_under_the_same_hooks() -> None:
    log: list[str] = []
    set_default_hooks(Recorder("policy", log))

    @tool
    def refund() -> str:
        """Refund."""
        return "refunded"

    refunds = agent_with([refund], [call("refund", "r1"), "refunded"], "Refunds")
    triage = Agent(
        name="Triage",
        instructions="Route.",
        model=FakeModel(responses=[call("transfer_to_refunds", "h1", reason="refund")]),
        handoffs=[handoff(refunds)],
    )
    result = await Runner().run_async(triage, "refund me")
    assert result.status == "completed"
    assert log == ["policy:run:Triage", "policy:tool:refund"]
    transfer = triage.handoffs[0].tool
    assert transfer.origin == "handoff" and "handoff:Refunds" in transfer.tags


async def test_team_members_inherit_the_default_and_the_enclosing_hooks() -> None:
    log: list[str] = []

    @tool
    def lookup() -> str:
        """Look up."""
        return "found"

    team = SequentialAgent(
        name="Pipeline",
        agents=[
            agent_with([lookup], [call("lookup", "a1"), "one"], "One"),
            agent_with([lookup], [call("lookup", "b1"), "two"], "Two"),
        ],
    )
    set_default_hooks(Recorder("policy", log))
    await team.run_async("go")
    assert log == ["policy:run:One", "policy:tool:lookup", "policy:run:Two", "policy:tool:lookup"]


# ---------------------------------------------------------------- AgentCoordinator


def _coordinator_agent(responses: list[Any], tools: list[Tool], **kwargs: Any) -> Any:
    from nexura_runtime.runtime.agents.definition import AgentDefinition

    return AgentDefinition(
        name="worker", model=FakeModel(responses=responses), tools=tools, **kwargs
    )


async def test_coordinator_tools_go_through_hooks_and_guardrails() -> None:
    from nexura_runtime.runtime.agents.coordinator import AgentCoordinator

    log: list[str] = []
    ran: list[str] = []
    seen_ctx: list[Any] = []

    def no_secret(ctx: RunContext[Any], arguments: dict[str, Any]) -> GuardrailResult:
        seen_ctx.append(ctx.tool)
        if "secret" in json.dumps(arguments):
            return GuardrailResult.rejected("no secrets to tools")
        return GuardrailResult.passed()

    def scrub(ctx: RunContext[Any], result: Any) -> GuardrailResult:
        return GuardrailResult.rewritten(str(result).replace("4111", "****"))

    @tool(input_guardrails=[no_secret], output_guardrails=[scrub])
    def send(text: str) -> str:
        """Send text."""
        ran.append(text)
        return f"sent 4111 {text}"

    worker = _coordinator_agent(
        [call("send", "s1", text="hello"), call("send", "s2", text="secret"), "done"], [send]
    )
    set_default_hooks(Recorder("policy", log))
    result = await AgentCoordinator([worker], hooks=Recorder("mine", log)).run_async("worker", "go")
    assert result.status == "completed" and ran == ["hello"]
    tool_msgs = [m.content for m in result.messages if m.role == "tool"]
    assert tool_msgs[0] == "sent **** hello"
    assert tool_msgs[1] is not None and "no secrets to tools" in tool_msgs[1]
    assert log == ["policy:tool:send", "mine:tool:send"]  # the rejected call never reached them
    assert seen_ctx[0].name == "send" and seen_ctx[0].origin.startswith("python:")


async def test_coordinator_before_tool_can_answer_and_after_tool_can_rewrite() -> None:
    from nexura_runtime.runtime.agents.coordinator import AgentCoordinator

    ran: list[str] = []

    class Rewrite(AgentHooks):
        async def after_tool(self, ctx: Any, call: Any, result: str) -> str | None:
            return f"[checked] {result}"

    @tool
    def act() -> str:
        """Act."""
        ran.append("act")
        return "acted"

    worker = _coordinator_agent([call("act"), "done"], [act])
    result = await AgentCoordinator([worker], hooks=[Answer("skipped")]).run_async("worker", "go")
    assert ran == [] and [m.content for m in result.messages if m.role == "tool"] == ["skipped"]
    worker = _coordinator_agent([call("act"), "done"], [act])
    result = await AgentCoordinator([worker], hooks=[Rewrite()]).run_async("worker", "go")
    assert [m.content for m in result.messages if m.role == "tool"] == ["[checked] acted"]


async def test_coordinator_approval_gates_from_the_tool_or_a_hook_fail_closed() -> None:
    from nexura_runtime.runtime.agents.coordinator import AgentCoordinator
    from nexura_runtime.runtime.agents.errors import AgentApprovalRequiredError

    ran: list[str] = []

    @tool(requires_approval=True)
    def wire() -> str:
        """Wire money (gated by the tool)."""
        ran.append("wire")
        return "wired"

    @tool
    def pay() -> str:
        """Pay (gated by the default policy)."""
        ran.append("pay")
        return "paid"

    with pytest.raises(AgentApprovalRequiredError):
        await AgentCoordinator([_coordinator_agent([call("wire"), "x"], [wire])]).run_async(
            "worker", "go"
        )
    set_default_hooks(GateNames("pay"))
    with pytest.raises(AgentApprovalRequiredError):
        await AgentCoordinator([_coordinator_agent([call("pay"), "x"], [pay])]).run_async(
            "worker", "go"
        )
    assert ran == []
    # The test-only opt-out leaves the process default out.
    done = await AgentCoordinator(
        [_coordinator_agent([call("pay"), "ok"], [pay])], default_hooks=False
    ).run_async("worker", "go")
    assert done.status == "completed" and ran == ["pay"]


async def test_coordinator_approval_gate_is_asked_for_a_hook_gated_call() -> None:
    from nexura_runtime.runtime.agents.coordinator import AgentCoordinator

    asked: list[tuple[str, Any]] = []

    class Gate:
        def check_or_request_approval(self, context: Any, **kwargs: Any) -> Any:
            asked.append((kwargs["action_name"], kwargs["payload"]))
            raise RuntimeError("pending approval")

    @tool
    def pay(amount: int) -> str:
        """Pay."""
        return "paid"

    worker = _coordinator_agent([call("pay", amount=5), "x"], [pay])
    coordinator = AgentCoordinator(
        [worker],
        approval_gate=Gate(),  # type: ignore[arg-type]
        hooks=GateNames("pay"),
    )
    with pytest.raises(RuntimeError, match="pending approval"):
        await coordinator.run_async("worker", "go")
    assert asked == [("pay", {"amount": 5})]


async def test_coordinator_handoffs_skip_tool_hooks_and_nested_runs_inherit() -> None:
    from nexura_runtime.runtime.agents.coordinator import AgentCoordinator
    from nexura_runtime.runtime.agents.definition import AgentDefinition, HandoffTarget

    log: list[str] = []

    @tool
    def lookup() -> str:
        """Look up."""
        return "found"

    @tool
    async def nested() -> str:
        """Run an SDK agent from a coordinator tool."""
        inner = agent_with([lookup], [call("lookup"), "inner"], name="Inner")
        return str((await Runner().run_async(inner, "go")).output)

    first = AgentDefinition(
        name="first",
        model=FakeModel(responses=[call("transfer_to_second", "h1", reason="go on")]),
        handoffs=[HandoffTarget(target="second")],
    )
    second = AgentDefinition(
        name="second", model=FakeModel(responses=[call("nested"), "done"]), tools=[nested]
    )
    result = await AgentCoordinator([first, second], hooks=Recorder("mine", log)).run_async(
        "first", "go"
    )
    assert result.status == "completed" and result.final_agent_name == "second"
    assert log == ["mine:tool:nested", "mine:run:Inner", "mine:tool:lookup"]


async def test_agent_definition_subagent_tools_get_hooks_guardrails_and_gates() -> None:
    from nexura_runtime.runtime.agents.errors import AgentApprovalRequiredError

    log: list[str] = []
    ran: list[str] = []

    @tool
    def lookup() -> str:
        """Look up."""
        ran.append("lookup")
        return "found"

    @tool
    def pay() -> str:
        """Pay."""
        ran.append("pay")
        return "paid"

    sub = _coordinator_agent([call("lookup"), call("pay", "c2"), "x"], [lookup, pay])
    delegate = sub.as_tool()
    assert delegate.origin == "agent:worker"
    set_default_hooks([Recorder("policy", log), GateNames("pay")])
    with pytest.raises(AgentApprovalRequiredError):
        await delegate.arun(query="go")
    assert ran == ["lookup"] and log == ["policy:tool:lookup"]


# ---------------------------------------------------------------- AgentMCPServer


async def test_mcp_plain_tools_run_the_runners_and_the_default_hooks() -> None:
    log: list[str] = []
    ran: list[str] = []

    class Rewrite(AgentHooks):
        async def after_tool(self, ctx: Any, call: Any, result: str) -> str | None:
            assert ctx.tool is not None and ctx.tool.name == "echo"
            return result.upper()

    @tool
    def echo(text: str) -> str:
        """Echo."""
        ran.append(text)
        return text

    set_default_hooks(Recorder("policy", log))
    server = AgentMCPServer(
        "desk", tools=[echo], runner=Runner(hooks=[Recorder("mine", log), Rewrite()])
    )
    handler = server.session._tool_handlers["echo"]
    assert await handler({"text": "hi"}) == "HI"
    assert log == ["policy:tool:echo", "mine:tool:echo"] and ran == ["hi"]

    answered = AgentMCPServer("desk", tools=[echo], runner=Runner(hooks=Answer("cached")))
    assert await answered.session._tool_handlers["echo"]({"text": "x"}) == "cached"
    assert ran == ["hi"]


async def test_mcp_plain_tool_hook_gates_and_guardrails_refuse() -> None:
    ran: list[str] = []

    def block(ctx: RunContext[Any], arguments: dict[str, Any]) -> GuardrailResult:
        return GuardrailResult.rejected("not here")

    @tool
    def pay() -> str:
        """Pay."""
        ran.append("pay")
        return "paid"

    @tool(input_guardrails=[block])
    def guarded() -> str:
        """Guarded."""
        ran.append("guarded")
        return "ok"

    set_default_hooks(GateNames("pay"))
    server = AgentMCPServer("desk", tools=[pay, guarded])  # no runner: Runner() defaults
    with pytest.raises(ApprovalRequiredOverMCP):
        await server.session._tool_handlers["pay"]({})
    with pytest.raises(ToolGuardrailRejected, match="not here"):
        await server.session._tool_handlers["guarded"]({})
    assert ran == []
    reply = await server.session.handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "pay"}}
    )
    assert reply is not None and reply["result"]["isError"] is True and ran == []


async def test_mcp_agent_calls_inherit_the_default_policy() -> None:
    log: list[str] = []

    @tool
    def lookup() -> str:
        """Look up."""
        return "found"

    set_default_hooks(Recorder("policy", log))
    server = AgentMCPServer(
        "desk",
        agents=[
            Agent(
                name="Desk",
                description="Answers.",
                instructions="",
                model=FakeModel(responses=[call("lookup"), "answer"]),
                tools=[lookup],
            )
        ],
    )
    assert await server.session._tool_handlers["desk"]({"request": "hi"}) == "answer"
    assert log == ["policy:run:Desk", "policy:tool:lookup"]


# ---------------------------------------------------------------- origins


def test_knowledge_tools_have_a_knowledge_origin() -> None:
    from nexura_runtime.knowledge.hybrid import create_hybrid_search_tool
    from nexura_runtime.knowledge.retrieval.tool import create_retrieval_tool
    from nexura_runtime.service.api.sessions import knowledge_search_tool

    rag = create_retrieval_tool(object(), collection_id="policies")  # type: ignore[arg-type]
    assert rag.origin == "knowledge:policies" and "knowledge:policies" in rag.tags
    hybrid = create_hybrid_search_tool(object(), workspace_id="w", collection_id="docs")  # type: ignore[arg-type]
    assert hybrid.origin == "knowledge:docs"
    studio = knowledge_search_tool(None, "w", ["a", "b"])
    assert studio.origin == "knowledge:a,b" and {"knowledge:a", "knowledge:b"} <= studio.tags


# ---------------------------------------------------------------- ToolModule as a node


def _tool_node_engine(tmp_path: Path, module: Any) -> Any:
    from nexura_runtime.workflow import (
        ModuleRegistry,
        SQLiteWorkflowStore,
        WorkflowEngine,
        WorkflowNode,
        WorkflowSpec,
    )

    registry = ModuleRegistry()
    registry.register("pay_node", module)
    node = WorkflowNode(id="pay", handler="pay_node", input_mapping={"amount": "amount"})
    return WorkflowEngine(
        WorkflowSpec(name="g2", entry_node="pay", nodes={"pay": node}),
        registry,
        store=SQLiteWorkflowStore(tmp_path / "g2.db"),
    )


async def test_a_tool_module_node_pauses_for_the_wrapped_tools_approval(tmp_path: Path) -> None:
    from nexura_runtime.integrations.tools import tool_to_module

    paid: list[int] = []

    @tool(requires_approval=lambda ctx, args: args["amount"] > 100)
    def pay(amount: int) -> str:
        """Pay."""
        paid.append(amount)
        return f"paid {amount}"

    engine = _tool_node_engine(tmp_path, tool_to_module(pay))
    small = await engine.run({"amount": 5})
    assert small.status == "completed" and paid == [5]  # the rule says no gate

    paused = await engine.run({"amount": 500})
    assert paused.status == "paused" and paid == [5]
    pending = paused.pending_approval
    assert pending is not None
    assert pending.details["tool_requires_approval"] == "pay"
    assert pending.details["arguments"] == {"amount": 500}
    done = await engine.resume(paused.workflow_run_id, pending.approval_id)
    assert done.status == "completed" and paid == [5, 500]

    again = await engine.run({"amount": 900})
    rejected = await engine.resume(
        again.workflow_run_id, again.pending_approval.approval_id, decision="reject"
    )
    assert rejected.status != "completed" and paid == [5, 500]


async def test_a_tool_module_with_a_flag_gate_runs_once_when_the_node_is_approved(
    tmp_path: Path,
) -> None:
    from nexura_runtime.integrations.tools import tool_to_module
    from nexura_runtime.workflow import (
        ModuleRegistry,
        SQLiteWorkflowStore,
        WorkflowEngine,
        WorkflowNode,
        WorkflowSpec,
    )

    paid: list[int] = []

    @tool(requires_approval=True)
    def pay(amount: int) -> str:
        """Pay."""
        paid.append(amount)
        return "paid"

    registry = ModuleRegistry()
    registry.register("pay_node", tool_to_module(pay))
    node = WorkflowNode(
        id="pay", handler="pay_node", input_mapping={"amount": "amount"}, requires_approval=True
    )
    engine = WorkflowEngine(
        WorkflowSpec(name="g2", entry_node="pay", nodes={"pay": node}),
        registry,
        store=SQLiteWorkflowStore(tmp_path / "g2b.db"),
    )
    paused = await engine.run({"amount": 7})
    assert paused.status == "paused" and paid == []
    assert paused.pending_approval is not None
    done = await engine.resume(paused.workflow_run_id, paused.pending_approval.approval_id)
    # The node's own approval covers the call: one approval, not two.
    assert done.status == "completed" and paid == [7]


async def test_a_gated_tool_module_outside_a_workflow_is_refused() -> None:
    from nexura_agents.contracts.context import ExecutionContext
    from nexura_agents.contracts.errors import ContractError

    from nexura_runtime.integrations.tools import tool_to_module

    paid: list[int] = []

    @tool(requires_approval=True)
    def pay(amount: int) -> str:
        """Pay."""
        paid.append(amount)
        return "paid"

    module = tool_to_module(pay)
    with pytest.raises(ContractError) as refused:
        await module.execute(ExecutionContext(run_id="r", node_id="n"), {"amount": 1}, {})
    assert refused.value.code == "APPROVAL_REQUIRED" and paid == []
    granted = ExecutionContext(run_id="r", node_id="n", approval_granted=True)
    result = await module.execute(granted, {"amount": 1}, {})
    assert result.status == "success" and paid == [1]


# ---------------------------------------------------------------- MIN7: gates after a crash


async def _paused_with_hook_then_claim_lost(
    store: MemoryCheckpointStore, tools: list[Any], name: str = "pay"
) -> RunResult[Any]:
    """A hook gates ``name``; the approval's resume crashes before the call; the claim is
    lost (as for a batch checkpointed without a recorded claim)."""

    class CrashBeforeTool(GateNames):
        async def before_tool(self, ctx: Any, call: Any) -> None:
            raise Crash("after the batch checkpoint, before the call")

    runner = Runner(checkpoint_store=store, hooks=GateNames(name))
    paused = await runner.run_async(agent_with(tools, [call(name), "x"]), "go")
    assert paused.status == "paused"
    runner.hooks = CrashBeforeTool(name)
    with pytest.raises(Crash):
        await runner.resume_async(agent_with(tools, ["x"]), held(paused), approve=True)
    journal = await store.open_execution_store()
    eid = ExecutionId("default", paused.run_id)
    lease = await journal.claim(
        eid, "tester", 5, definition_digest=(await journal.get(eid)).run.definition_digest
    )
    assert lease is not None
    state = dict((await journal.get(eid)).run.state)
    assert state["hook_gates"] == ["turn:1/call:c1"]  # recorded by the pause
    state["approval_claim"] = None
    await journal.commit(lease, state=state, status="running")
    await journal.release(lease)
    return paused


async def test_a_hook_gate_holds_when_the_recovery_runner_has_no_hook() -> None:
    """MIN7: the recovery worker lacks the gating hook; the call still waits for a person."""
    count: dict[str, int] = {}
    store = MemoryCheckpointStore()

    @tool
    def pay() -> str:
        """Pay (gated by a hook only)."""
        count["pay"] = count.get("pay", 0) + 1
        return "paid"

    paused = await _paused_with_hook_then_claim_lost(store, [pay])
    bare = Runner(checkpoint_store=store)  # no hook, no default
    result = await bare.recover_async(agent_with([pay], ["done"]), paused.run_id)
    assert result.status == "paused" and count == {}
    assert held(result).reason == "effect_not_applied"
    again = await bare.recover_async(agent_with([pay], ["done"]), paused.run_id)
    assert again.status == "paused" and count == {}
    final = await bare.resume_async(agent_with([pay], ["done"]), held(again), approve=True)
    assert final.status == "completed" and count == {"pay": 1}  # a person approved: once


async def test_a_hook_gate_holds_for_a_not_applied_outcome_resumed_without_the_hook() -> None:
    """Site ``_apply_outcomes``: a person says the crashed call did not happen; the resume
    runner has no hook, yet the call is sent again only after a fresh approval."""
    count: dict[str, int] = {}
    store = MemoryCheckpointStore()

    @tool
    def pay() -> str:
        """Pay (gated by a hook only)."""
        count["pay"] = count.get("pay", 0) + 1
        if count["pay"] == 1:
            raise Crash
        return "paid"

    hooked = Runner(checkpoint_store=store, hooks=GateNames("pay"))
    first = await hooked.run_async(agent_with([pay], [call("pay"), "x"]), "go")
    with pytest.raises(Crash):
        await hooked.resume_async(agent_with([pay], ["x"]), held(first), approve=True)
    bare = Runner(checkpoint_store=store)
    paused = await bare.recover_async(agent_with([pay], ["done"]), first.run_id)
    assert held(paused).pause_reason == "effect_unknown"
    again = await bare.resume_async(
        agent_with([pay], ["done"]), held(paused), outcomes={"c1": ResolvedOutcome.not_applied()}
    )
    assert again.status == "paused" and count["pay"] == 1
    assert held(again).reason == "effect_not_applied"
    final = await bare.resume_async(agent_with([pay], ["done"]), held(again), approve=True)
    assert final.status == "completed" and count["pay"] == 2


async def test_a_hook_gate_holds_for_reconcile_not_applied_without_the_hook() -> None:
    """Site ``_settle_in_doubt``: its approval was used by the first try."""
    count: dict[str, int] = {}
    store = MemoryCheckpointStore()

    def reconcile(ctx: Any, intent: Any) -> Any:
        return NotApplied()

    @tool(effect="reconcilable", reconcile=reconcile)
    def charge() -> str:
        """Charge."""
        count["charge"] = count.get("charge", 0) + 1
        if count["charge"] == 1:
            raise Crash
        return "charged"

    hooked = Runner(checkpoint_store=store, hooks=GateNames("charge"))
    first = await hooked.run_async(agent_with([charge], [call("charge"), "x"]), "go")
    with pytest.raises(Crash):
        await hooked.resume_async(agent_with([charge], ["x"]), held(first), approve=True)
    again = await Runner(checkpoint_store=store).recover_async(
        agent_with([charge], ["done"]), first.run_id
    )
    assert again.status == "paused" and count["charge"] == 1
    assert held(again).reason == "effect_not_applied"


async def test_a_pause_lost_before_its_journal_commit_records_the_gate_on_recovery() -> None:
    """The paused checkpoint was saved, then the process died before the journal's pause
    commit (which carries the hook gate). Recovery without the hook writes the pause and
    records every call it asks about, so a later lost claim still cannot send it."""
    count: dict[str, int] = {}

    class DieAfterPausedCheckpoint(MemoryCheckpointStore):
        armed = True

        async def save(self, run_id: str, checkpoint_json: str, owner: str | None = None) -> None:
            await super().save(run_id, checkpoint_json, owner=owner)
            if self.armed and json.loads(checkpoint_json)["phase"] == "paused":
                self.armed = False
                raise Crash("after the paused checkpoint, before the journal pause")

    @tool
    def pay() -> str:
        """Pay."""
        count["pay"] = count.get("pay", 0) + 1
        return "paid"

    store = DieAfterPausedCheckpoint()
    with pytest.raises(Crash):
        await Runner(checkpoint_store=store, hooks=GateNames("pay")).run_async(
            agent_with([pay], [call("pay"), "x"]), "go"
        )
    run_id = next(iter(store._data))
    journal = await store.open_execution_store()
    eid = ExecutionId("default", run_id)
    assert "hook_gates" not in (await journal.get(eid)).run.state  # lost with the commit
    bare = Runner(checkpoint_store=store)
    paused = await bare.recover_async(agent_with([pay], ["x"]), run_id)
    assert paused.status == "paused" and count == {}
    assert (await journal.get(eid)).run.state["hook_gates"] == ["turn:1/call:c1"]


async def test_hook_gates_of_earlier_turns_are_dropped() -> None:
    store = MemoryCheckpointStore()

    @tool
    def pay() -> str:
        """Pay."""
        return "paid"

    runner = Runner(checkpoint_store=store, hooks=GateNames("pay"))
    agent = agent_with([pay], [call("pay", "p1"), call("pay", "p2"), "done"])
    first = await runner.run_async(agent, "go")
    second = await runner.resume_async(agent, held(first), approve=True)
    assert second.status == "paused"
    journal = await store.open_execution_store()
    state = (await journal.get(ExecutionId("default", first.run_id))).run.state
    assert state["hook_gates"] == ["turn:2/call:p2"]
    done = await runner.resume_async(agent, held(second), approve=True)
    assert done.status == "completed"


async def test_a_recorded_gate_never_blocks_an_approved_resume() -> None:
    """The recorded gate makes recovery stricter only; an approved call still runs once."""
    count: dict[str, int] = {}
    store = MemoryCheckpointStore()

    @tool
    def pay() -> str:
        """Pay."""
        count["pay"] = count.get("pay", 0) + 1
        return "paid"

    paused = await Runner(checkpoint_store=store, hooks=GateNames("pay")).run_async(
        agent_with([pay], [call("pay"), "x"]), "go"
    )
    done = await Runner(checkpoint_store=store).resume_async(
        agent_with([pay], ["done"]), held(paused), approve=True
    )
    assert done.status == "completed" and count == {"pay": 1}
