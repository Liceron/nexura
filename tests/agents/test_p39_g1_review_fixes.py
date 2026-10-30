# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""P39 G1 review fixes beyond the moved repros (``test_p39_g1_review_repros.py``).

B1 (wrapper effect defaults), MIN1 (members with no hook methods), MIN2 (UsageLimits after
guardrail usage), MIN3 (usage of finished judges on cancel), MIN5 (unchecked answers on
cancel), MIN9 (decision-record filter) and the nits (a copied Tool for approval hooks, MCP
spec hint keys only). Offline: FakeModel, in-memory stores and an in-memory MCP server.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from nexura_agents import (
    Agent,
    AgentHooks,
    CancelToken,
    ComposedHooks,
    FakeModel,
    GuardrailResult,
    GuardrailTripwireTriggered,
    MCPServer,
    ModelRetry,
    Runner,
    ToolCall,
    Usage,
    UsageLimitExceededError,
    UsageLimits,
    tool,
)
from nexura_agents.contracts.execution import EffectClass, Unknown
from nexura_agents.errors import SDKValidationError
from nexura_agents.guardrails import decision_record
from nexura_agents.mcp import McpInMemoryTransport, McpServerSession
from nexura_agents.mcp.types import McpToolDefinition
from nexura_agents.module_tools import module_tool

from nexura_runtime.integrations.tools import tool_to_module
from nexura_runtime.modules.registry import LegacyModuleAdapter

TEN = Usage(prompt_tokens=7, completion_tokens=3, total_tokens=10)


def call(name: str, call_id: str = "c1", **arguments: Any) -> list[ToolCall]:
    return [ToolCall(call_id, name, arguments)]


# ---------------------------------------------------------------- B1: wrapper defaults


def test_tool_to_module_inherits_the_tools_declared_class() -> None:
    @tool
    def undeclared() -> str:
        """Undeclared."""
        return "x"

    @tool(effect="pure")
    def read() -> str:
        """Read."""
        return "x"

    @tool(idempotent=True)
    def upsert() -> str:
        """Upsert."""
        return "x"

    @tool(effect="reconcilable", reconcile=lambda ctx, intent: Unknown())
    def charge() -> str:
        """Charge."""
        return "x"

    expected = {
        undeclared: ("non_idempotent_write", EffectClass.NON_IDEMPOTENT),
        read: ("pure", EffectClass.PURE),
        upsert: ("idempotent_write", EffectClass.IDEMPOTENT),
        # a module tool cannot run the tool's reconcile: never repeated silently
        charge: ("non_idempotent_write", EffectClass.NON_IDEMPOTENT),
    }
    for wrapped, (policy, effect) in expected.items():
        module = tool_to_module(wrapped)
        assert module.manifest.effect_policy == policy, wrapped.name
        assert module_tool(module).effect_class() is effect, wrapped.name


def test_an_explicit_wrapper_policy_cannot_loosen_the_wrapped_tool() -> None:
    @tool
    def charge_card(amount: int) -> str:
        """Charge."""
        return "ok"

    @tool(effect="pure")
    def lookup() -> str:
        """Read."""
        return "x"

    loose = tool_to_module(charge_card, effect_policy="read")
    assert loose.manifest.effect_policy == "read"  # recorded as given
    assert module_tool(loose).effect_class() is EffectClass.NON_IDEMPOTENT  # the stricter
    strict = tool_to_module(lookup, effect_policy="non_idempotent_write")
    assert module_tool(strict).effect_class() is EffectClass.NON_IDEMPOTENT  # the stricter


def test_legacy_and_agent_wrappers_default_to_non_idempotent() -> None:
    from nexura_runtime.runtime.agents.coordinator import AgentCoordinator
    from nexura_runtime.runtime.agents.definition import AgentDefinition
    from nexura_runtime.runtime.agents.module import AgentCoordinatorModule

    adapter = LegacyModuleAdapter("pay", lambda inputs: {"paid": True})
    assert adapter.manifest.effect_policy == "non_idempotent_write"
    definition = AgentDefinition(name="support", model=FakeModel(responses=["x"]))
    assert definition.effect_policy == "non_idempotent_write"
    assert AgentDefinition.from_agent(
        Agent(name="a", instructions="i", model=FakeModel(responses=["x"]))
    ).effect_policy == ("non_idempotent_write")
    coordinator = AgentCoordinatorModule(AgentCoordinator(agents=[definition]))
    assert coordinator.manifest.effect_policy == "non_idempotent_write"
    assert module_tool(coordinator).effect_class() is EffectClass.NON_IDEMPOTENT


# ---------------------------------------------------------------- MAJ1 / MIN1


def test_only_a_plain_composition_is_flattened() -> None:
    class Policy(ComposedHooks):
        def tool_needs_approval(self, ctx: Any, tool: Any, arguments: Any) -> bool | None:
            return True

    a, b = AgentHooks(), AgentHooks()
    policy = Policy(a)
    composed = ComposedHooks(policy, ComposedHooks(b))
    assert composed.members == (policy, b)


@pytest.mark.parametrize("bad", [[AgentHooks()], (AgentHooks(),), {"x": 1}, set(), object(), 42])
def test_members_without_hook_methods_are_refused(bad: Any) -> None:
    with pytest.raises(SDKValidationError, match="at least one AgentHooks method"):
        ComposedHooks(AgentHooks(), bad)


def test_a_nested_list_in_runner_hooks_is_refused() -> None:
    with pytest.raises(SDKValidationError):
        Runner(hooks=[[AgentHooks()]])  # type: ignore[list-item]


# ---------------------------------------------------------------- MIN2 / MIN3 / MIN5


def judge(name: str = "judge") -> Any:
    def check(ctx: Any, value: Any) -> GuardrailResult:
        ctx.add_usage(TEN)
        return GuardrailResult.passed()

    check.__name__ = name
    return check


async def test_input_guardrail_usage_is_checked_before_the_model_call() -> None:
    model = FakeModel(["hi"], default_usage=TEN)
    agent = Agent(name="A", instructions="i", model=model, input_guardrails=[judge()])
    with pytest.raises(UsageLimitExceededError):
        await Runner(usage_limits=UsageLimits(total_tokens_limit=5)).run_async(agent, "go")
    assert model.calls == []  # stopped before the agent's model was called


async def test_output_guardrail_usage_within_the_limit_completes() -> None:
    agent = Agent(
        name="A",
        instructions="i",
        model=FakeModel(["hi"], default_usage=TEN),
        output_guardrails=[judge()],
    )
    result = await Runner(usage_limits=UsageLimits(total_tokens_limit=20)).run_async(agent, "go")
    assert result.usage.total_tokens == 20


async def test_a_cancelled_input_stage_keeps_the_usage_of_finished_judges() -> None:
    cancel = CancelToken()

    async def slow(ctx: Any, value: Any) -> GuardrailResult:
        cancel.cancel("stop")
        await asyncio.sleep(5)
        return GuardrailResult.passed()

    agent = Agent(
        name="A",
        instructions="i",
        model=FakeModel(["hi"], default_usage=TEN),
        input_guardrails=[judge(), slow],
    )
    result = await Runner().run_async(agent, "go", cancel=cancel)
    assert result.status == "cancelled" and result.usage.total_tokens == 10


async def test_a_tripping_judge_is_counted_on_the_run_span() -> None:
    from nexura_agents import tracing as nexura_tracing
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    def tripping_judge(ctx: Any, value: Any) -> GuardrailResult:
        ctx.add_usage(TEN)
        return GuardrailResult.tripped({"decision": "block"})

    memory = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(memory))
    nexura_tracing.instrument(tracer_provider=provider)
    try:
        agent = Agent(
            name="A",
            instructions="i",
            model=FakeModel(["hi"], default_usage=TEN),
            output_guardrails=[tripping_judge],
        )
        with pytest.raises(GuardrailTripwireTriggered):
            await Runner().run_async(agent, "go")
    finally:
        nexura_tracing.uninstrument()
    (run_span,) = [s for s in memory.get_finished_spans() if s.name == "invoke_agent A"]
    attributes = dict(run_span.attributes or {})
    assert attributes["gen_ai.usage.input_tokens"] == 14  # agent 7 + tripping judge 7


async def test_a_validator_retry_then_cancel_keeps_the_transcript_intact() -> None:
    cancel = CancelToken()
    seen: list[str] = []

    async def validator(ctx: Any, output: Any) -> Any:
        seen.append(output)
        if len(seen) == 1:
            raise ModelRetry("try again")
        cancel.cancel("stop")
        await asyncio.sleep(5)
        return output

    agent = Agent(
        name="A",
        instructions="i",
        model=FakeModel(["first answer", "second answer"]),
        output_validators=[validator],
        output_retries=2,
    )
    result = await Runner().run_async(agent, "go", cancel=cancel)
    assert result.status == "cancelled"
    texts = [m.content for m in result.messages]
    assert "second answer" not in texts  # unchecked: dropped
    assert "first answer" in texts  # earlier, rejected answer: part of the retry history
    assert result.messages[-1].role == "user"  # the retry feedback


# ---------------------------------------------------------------- MIN9


def test_decision_records_drop_values_that_look_detected() -> None:
    info = {
        "rule": "user@example.com",
        "backend": "sk-live0abcdefghijklmnop",
        "check": "card-4111111111111111",
        "error": "ghp_abcdefghijklmnopqrstuvwxyz012345",
        "categories": ["fraud", "anita@example.com", "9876543210", "x" * 40, "privacy"],
        "decision": "custom-decision",
    }
    record = decision_record("input", "g", "fail", info=info)
    assert record == {
        "stage": "input",
        "guardrail": "g",
        "action": "fail",
        "categories": ["fraud", "privacy"],
    }
    kept = decision_record(
        "input",
        "g",
        "fail",
        info={"decision": "review", "backend": "judge:ollama:qwen3:8b", "rule": "no-pii-v2"},
    )
    assert kept["decision"] == "review" and kept["backend"] == "judge:ollama:qwen3:8b"
    assert kept["rule"] == "no-pii-v2"


# ---------------------------------------------------------------- nits


async def test_an_approval_hook_gets_a_copy_of_the_tool() -> None:
    @tool(requires_approval=lambda ctx, args: args["amount"] > 100)
    def pay(amount: int) -> str:
        """Pay."""
        return "paid"

    class Meddle(AgentHooks):
        def tool_needs_approval(self, ctx: Any, tool: Any, arguments: Any) -> None:
            tool.requires_approval = False  # changes only the copy

    runner = Runner(hooks=[Meddle(), Meddle()])
    small = await runner.run_async(
        Agent(
            name="A",
            instructions="i",
            model=FakeModel([call("pay", amount=5), "ok"]),
            tools=[pay],
        ),
        "go",
    )
    assert small.status == "completed"
    big = await runner.run_async(
        Agent(
            name="A",
            instructions="i",
            model=FakeModel([call("pay", amount=500), "ok"]),
            tools=[pay],
        ),
        "go",
    )
    assert big.status == "paused"  # the real tool's gate is intact
    assert callable(pay.requires_approval)


async def test_only_the_four_spec_hint_keys_become_tags() -> None:
    client_side, server_side = McpInMemoryTransport.create_pair()
    server = McpServerSession(server_side)

    async def echo(args: dict[str, Any]) -> str:
        return "echo"

    server.register_tool("t", "t", {"type": "object"}, echo)
    server._tools["t"] = McpToolDefinition(
        name="t",
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
            "trustMeHint": True,
            "safeHint": True,
        },
    )
    task = asyncio.create_task(server.serve_loop())
    try:
        async with MCPServer(client_side, name="srv") as mcp:
            (built,) = await mcp.tools()
    finally:
        task.cancel()
    hints = sorted(t for t in built.tags if t.startswith("mcp.hint:"))
    assert hints == [
        "mcp.hint:destructiveHint=false",
        "mcp.hint:idempotentHint=true",
        "mcp.hint:openWorldHint=false",
        "mcp.hint:readOnlyHint=true",
    ]


# ---------------------------------------------------------------- review round 2


async def test_wrapping_a_gated_tool_keeps_its_approval_gate() -> None:
    """R2-2: module_tool(tool_to_module(t)) applies t.requires_approval (flag or rule)
    together with module_tool's own gates; any gate on either side gates the call."""
    sent: list[int] = []

    @tool(requires_approval=True)
    def wire(amount: int) -> str:
        """Wire money."""
        sent.append(amount)
        return "sent"

    @tool(requires_approval=lambda ctx, args: args["amount"] > 100)
    def refund(amount: int) -> str:
        """Refund."""
        sent.append(-amount)
        return "refunded"

    @tool
    def ping() -> str:
        """Ping."""
        return "pong"

    from nexura_agents import RunContext

    ctx = RunContext(deps=None, run_id="r", agent_name="a")
    gated_wire = module_tool(tool_to_module(wire), name="wire")
    gated_refund = module_tool(tool_to_module(refund), name="refund")
    assert gated_wire.needs_approval(ctx, {"amount": 1}) is True
    assert gated_refund.needs_approval(ctx, {"amount": 500}) is True
    assert gated_refund.needs_approval(ctx, {"amount": 5}) is False
    assert module_tool(tool_to_module(ping)).needs_approval(ctx, {}) is False
    # module_tool's own gate still applies on top of an ungated tool.
    assert module_tool(tool_to_module(ping), requires_approval=True).needs_approval(ctx, {})

    agent = Agent(
        name="A",
        instructions="i",
        model=FakeModel([call("wire", amount=50), "done"]),
        tools=[gated_wire],
    )
    runner = Runner()
    paused = await runner.run_async(agent, "pay")
    assert paused.status == "paused" and sent == []  # the first call waits for a person
    assert paused.interruption is not None
    done = await runner.resume_async(agent, paused.interruption, approve=True)
    assert done.status == "completed" and sent == [50]


def test_the_usage_budget_module_is_non_idempotent() -> None:
    """R2-3: reserve and record_usage are not idempotent; a repeat after a crash would add a
    second reservation or count usage twice."""
    from nexura_agents.usage.module import UsageBudgetModule

    module = UsageBudgetModule()
    assert module.manifest.effect_policy == "non_idempotent_write"
    assert module_tool(module).effect_class() is EffectClass.NON_IDEMPOTENT


@pytest.mark.parametrize(
    "value",
    [
        "4111-1111-1111-1111",  # card with hyphens
        "4111.1111.1111.1111",
        "4111-111111-11111",
        "123-456-7890",  # US phone
        "98765.43210",  # Indian mobile with a dot
        "+91-98765-43210",
        "080-2345-6789",  # landline
        "rule-for-123-456-789",  # 9 digits joined by separators
        "acct:5010-0123-4567-89",
    ],
)
def test_separator_formatted_numbers_are_dropped(value: str) -> None:
    """R2-1: digits are counted across single - . or space separators; 9 or more drop."""
    record = decision_record(
        "input", "g", "fail", info={"rule": value, "check": value, "categories": [value, "ok"]}
    )
    assert "rule" not in record and "check" not in record
    assert record["categories"] == ["ok"]


@pytest.mark.parametrize(
    "value",
    [
        "llama-guard3:1b",
        "judge:ollama:qwen3:8b",
        "gpt-4.1-mini",
        "v1.2.3",
        "rule-2026-10",
        "s1-s14",
    ],
)
def test_ordinary_identifiers_with_digits_are_kept(value: str) -> None:
    record = decision_record("input", "g", "fail", info={"backend": value})
    assert record["backend"] == value


class _UserHooks(AgentHooks):
    pass


def _gen() -> Any:
    yield AgentHooks()


@pytest.mark.parametrize(
    "bad",
    [{AgentHooks()}, {"a": AgentHooks()}, _gen(), _UserHooks, AgentHooks, object(), 42],
)
def test_runner_refuses_non_hooks_arguments_at_construction(bad: Any) -> None:
    """Nit: a set, dict, generator, class or plain object fails when the Runner is built,
    not on the run's first hook call."""
    with pytest.raises(SDKValidationError):
        Runner(hooks=bad)


def test_composed_hooks_refuses_a_class_member() -> None:
    with pytest.raises(SDKValidationError):
        ComposedHooks(AgentHooks(), _UserHooks)


def test_duck_typed_single_hooks_objects_are_still_accepted() -> None:
    class Legacy:
        async def before_model(self, ctx: Any, messages: Any) -> None:
            return None

    hooks: Any = Legacy()
    assert Runner(hooks=hooks).hooks is hooks
