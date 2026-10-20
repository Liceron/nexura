"""Agent loop features: deps, dynamic instructions, typed output, retries, parallel
tools, guardrails, hooks, usage limits, approvals with pause/resume, streaming,
provider context and model resilience. All offline (fake models / fake providers)."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest
from nexura_runtime.errors import (
    ApprovalDecisionError,
    GuardrailTripwireTriggered,
    ModelRetry,
    OutputValidationError,
    ProviderRateLimitError,
    ToolExecutionError,
    ToolValidationError,
    UsageLimitExceededError,
)
from nexura_runtime.events import RunEventType
from nexura_runtime.guardrails import GuardrailResult
from nexura_runtime.hooks import AgentHooks
from nexura_runtime.limits import UsageLimits
from nexura_runtime.models.resilience import FallbackModel, RetryModel
from nexura_runtime.providers.fake import StreamingFakeProvider
from nexura_runtime.providers.types import StreamDelta
from nexura_runtime.run_context import RunContext
from nexura_runtime.types import Message, ModelResponse, RunInterruption, Usage
from pydantic import BaseModel

from nexura_runtime import Agent, FakeModel, Runner, ToolCall, tool


def call(name: str, call_id: str = "c1", **arguments: Any) -> list[ToolCall]:
    return [ToolCall(id=call_id, name=name, arguments=arguments)]


@dataclass
class Shop:
    orders: dict[str, str]
    refunds: list[str]


# --------------------------------------------------------------------------- deps


async def test_deps_reach_tools_and_dynamic_instructions() -> None:
    @tool
    def order_status(ctx: RunContext[Shop], order_id: str) -> str:
        """Look up an order."""
        return ctx.deps.orders[order_id]

    model = FakeModel(responses=[call("order_status", order_id="A1"), "It shipped."])
    agent = Agent(
        name="Support",
        instructions=lambda ctx: f"Store has {len(ctx.deps.orders)} orders.",
        model=model,
        tools=[order_status],
    )
    result = await Runner().run_async(agent, "Status of A1?", deps=Shop({"A1": "shipped"}, []))

    assert result.output == "It shipped."
    assert model.calls[0]["messages"][0].content == "Store has 1 orders."
    assert result.messages[3].content == "shipped"
    # The context parameter is hidden from the model's tool schema.
    assert "ctx" not in order_status.to_schema()["function"]["parameters"]["properties"]


async def test_tool_needing_context_fails_clearly_without_runner() -> None:
    @tool
    def needs_ctx(ctx: RunContext[None], x: int) -> int:
        return x

    with pytest.raises(ToolValidationError, match="RunContext"):
        await needs_ctx.execute({"x": 1})


# --------------------------------------------------------------------------- typed output


class Refund(BaseModel):
    order_id: str
    amount: int


async def test_output_type_is_validated_and_retried_with_feedback() -> None:
    model = FakeModel(responses=["sure!", '```json\n{"order_id": "A1", "amount": 450}\n```'])
    agent = Agent(name="R", instructions="Decide.", model=model, output_type=Refund)
    result = await Runner().run_async(agent, "Refund A1")

    assert result.output == Refund(order_id="A1", amount=450)
    assert '"amount"' in model.calls[0]["messages"][0].content  # schema in system prompt
    retry_events = [e for e in result.events if e.type == RunEventType.RETRY]
    assert retry_events[0].data["reason"] == "output_validation"
    assert "did not match" in model.calls[1]["messages"][-1].content


async def test_output_type_failure_after_retries_raises() -> None:
    agent = Agent(
        name="R",
        instructions="x",
        model=FakeModel(responses=["no", "still no"]),
        output_type=Refund,
        output_retries=1,
    )
    with pytest.raises(OutputValidationError) as exc:
        await Runner().run_async(agent, "Refund")
    assert exc.value.raw_output == "still no"
    assert exc.value.attempts == 2


# --------------------------------------------------------------------------- tool retries


async def test_model_retry_sends_feedback_then_succeeds() -> None:
    attempts: list[int] = []

    @tool
    def lookup(order_id: str) -> str:
        attempts.append(1)
        if not order_id.startswith("ORD-"):
            raise ModelRetry("Order IDs look like ORD-123.")
        return "found"

    model = FakeModel(
        responses=[
            call("lookup", "c1", order_id="123"),
            call("lookup", "c2", order_id="ORD-123"),
            "Found it.",
        ]
    )
    agent = Agent(name="A", instructions="", model=model, tools=[lookup])
    result = await Runner().run_async(agent, "find 123")

    assert len(attempts) == 2
    assert result.output == "Found it."
    assert "ORD-123" in model.calls[1]["messages"][-1].content
    assert [e.data["reason"] for e in result.events if e.type == RunEventType.RETRY] == [
        "model_retry"
    ]


async def test_model_retry_beyond_limit_fails_the_run() -> None:
    @tool
    def flaky() -> str:
        raise ModelRetry("try again")

    model = FakeModel(responses=[call("flaky", "c1"), call("flaky", "c2"), "never"])
    agent = Agent(name="A", instructions="", model=model, tools=[flaky])
    with pytest.raises(ToolExecutionError):
        await Runner().run_async(agent, "go")


async def test_invalid_arguments_feedback_only_when_tool_opts_in() -> None:
    @tool(max_retries=1)
    def add(a: int, b: int) -> int:
        return a + b

    model = FakeModel(responses=[call("add", "c1", a="x", b=1), call("add", "c2", a=2, b=1), "3"])
    result = await Runner().run_async(
        Agent(name="A", instructions="", model=model, tools=[add]), ""
    )
    assert result.output == "3"
    assert "Invalid arguments" in model.calls[1]["messages"][-1].content

    @tool
    def strict(a: int) -> int:
        return a

    strict_model = FakeModel(responses=[call("strict", a="x")])
    with pytest.raises(ToolValidationError):
        await Runner().run_async(
            Agent(name="A", instructions="", model=strict_model, tools=[strict]), ""
        )


# --------------------------------------------------------------------------- parallel


async def test_parallel_tool_calls_overlap_and_keep_order() -> None:
    @tool
    async def slow(label: str) -> str:
        await asyncio.sleep(0.2)
        return label

    batch = [
        ToolCall(id="a", name="slow", arguments={"label": "first"}),
        ToolCall(id="b", name="slow", arguments={"label": "second"}),
        ToolCall(id="c", name="slow", arguments={"label": "third"}),
    ]
    agent = Agent(
        name="P",
        instructions="",
        model=FakeModel(responses=[batch, "done"]),
        tools=[slow],
        parallel_tool_calls=True,
    )
    t0 = time.perf_counter()
    result = await Runner().run_async(agent, "go")
    assert time.perf_counter() - t0 < 0.45  # three 0.2s calls overlapped
    assert [m.content for m in result.messages if m.role == "tool"] == ["first", "second", "third"]
    types = [e.type for e in result.events]
    first_done = types.index(RunEventType.TOOL_COMPLETED)
    assert types[:first_done].count(RunEventType.TOOL_STARTED) == 3


# --------------------------------------------------------------------------- guardrails


async def test_input_guardrail_stops_before_any_model_call() -> None:
    def no_card_numbers(ctx: RunContext[Any], text: str) -> GuardrailResult:
        return GuardrailResult(tripwire=any(ch.isdigit() for ch in text) and len(text) > 15)

    model = FakeModel(responses=["unreachable"])
    agent = Agent(name="G", instructions="", model=model, input_guardrails=[no_card_numbers])
    with pytest.raises(GuardrailTripwireTriggered) as exc:
        await Runner().run_async(agent, "my card is 4111111111111111")
    assert exc.value.stage == "input"
    assert model.calls == []


async def test_async_output_guardrail_sees_typed_output() -> None:
    async def cap_refund(ctx: RunContext[Any], output: Refund) -> GuardrailResult:
        return GuardrailResult(tripwire=output.amount > 1000, info={"amount": output.amount})

    agent = Agent(
        name="G",
        instructions="",
        model=FakeModel(responses=['{"order_id": "A", "amount": 5000}']),
        output_type=Refund,
        output_guardrails=[cap_refund],
    )
    with pytest.raises(GuardrailTripwireTriggered) as exc:
        await Runner().run_async(agent, "refund")
    assert exc.value.info == {"amount": 5000}


# --------------------------------------------------------------------------- hooks


async def test_hooks_fire_in_order_and_can_veto_a_tool() -> None:
    log: list[str] = []

    class Audit(AgentHooks):
        async def on_run_start(self, ctx: RunContext[Any]) -> None:
            log.append("start")

        async def before_model(self, ctx: RunContext[Any], messages: list[Message]) -> None:
            log.append(f"model{ctx.turn}")

        async def before_tool(self, ctx: RunContext[Any], call: ToolCall) -> None:
            log.append(f"tool:{call.name}")
            if call.arguments.get("amount", 0) > 100:
                raise ModelRetry("Amounts over 100 need a manager.")

        async def after_tool(self, ctx: RunContext[Any], call: ToolCall, result: str) -> None:
            log.append(f"done:{call.name}")

        async def on_run_end(self, ctx: RunContext[Any], output: Any) -> None:
            log.append("end")

    @tool
    def pay(amount: int) -> str:
        return f"paid {amount}"

    model = FakeModel(responses=[call("pay", "c1", amount=500), call("pay", "c2", amount=50), "ok"])
    agent = Agent(name="H", instructions="", model=model, tools=[pay])
    await Runner(hooks=Audit()).run_async(agent, "pay")
    assert log == ["start", "model1", "tool:pay", "model2", "tool:pay", "done:pay", "model3", "end"]


# --------------------------------------------------------------------------- usage limits


async def test_usage_limits_stop_runaway_loops_and_fail_closed_on_unknown_usage() -> None:
    @tool
    def ping() -> str:
        return "pong"

    looping = FakeModel.endless("ping", default_usage=Usage(1, 1, 2, True))
    agent = Agent(name="L", instructions="", model=looping, tools=[ping], max_turns=50)
    with pytest.raises(UsageLimitExceededError) as exc:
        await Runner(usage_limits=UsageLimits(request_limit=3)).run_async(agent, "go")
    assert exc.value.limit == "request_limit"

    def unknown() -> Agent:  # fresh model per run; FakeModel reports no usage by default
        return Agent(name="U", instructions="", model=FakeModel(responses=["hi"]))

    with pytest.raises(UsageLimitExceededError, match="not reported"):
        await Runner(usage_limits=UsageLimits(total_tokens_limit=100)).run_async(unknown(), "x")
    allowed = Runner(usage_limits=UsageLimits(total_tokens_limit=100, allow_unknown_usage=True))
    assert (await allowed.run_async(unknown(), "x")).output == "hi"


# --------------------------------------------------------------------------- approvals


def _refund_agent(model: FakeModel, shop: Shop) -> Agent:
    @tool(requires_approval=lambda ctx, args: args["amount"] > 100)
    def refund(ctx: RunContext[Shop], order_id: str, amount: int) -> str:
        ctx.deps.refunds.append(f"{order_id}:{amount}")
        return "refunded"

    @tool
    def note(text: str) -> str:
        return "noted"

    return Agent(name="Refunds", instructions="", model=model, tools=[refund, note])


async def test_approval_pauses_before_effect_and_resumes_after_restart() -> None:
    shop = Shop({}, [])
    batch = [
        ToolCall(id="r1", name="refund", arguments={"order_id": "A1", "amount": 450}),
        ToolCall(id="n1", name="note", arguments={"text": "customer upset"}),
    ]
    agent = _refund_agent(FakeModel(responses=[batch, "Refund issued."]), shop)

    paused = await Runner().run_async(agent, "refund A1", deps=shop)
    assert paused.status == "paused"
    assert shop.refunds == []
    assert paused.interruption is not None
    assert [c.id for c in paused.interruption.pending] == ["r1"]

    # Persist and reload, as a different process would.
    reviewed = paused.interruption.pending_digests()
    restored = RunInterruption.from_json(paused.interruption.to_json())
    done = await Runner().resume_async(agent, restored, approve=reviewed, deps=shop)
    assert done.status == "completed" and done.output == "Refund issued."
    assert shop.refunds == ["A1:450"]
    tool_msgs = {m.tool_call_id: m.content for m in done.messages if m.role == "tool"}
    assert tool_msgs == {"r1": "refunded", "n1": "noted"}  # every call got a result


async def test_rejected_call_is_reported_to_the_model_and_never_runs() -> None:
    shop = Shop({}, [])
    model = FakeModel(
        responses=[call("refund", "r1", order_id="A1", amount=900), "Escalated instead."]
    )
    agent = _refund_agent(model, shop)
    paused = await Runner().run_async(agent, "refund", deps=shop)
    assert paused.interruption is not None

    with pytest.raises(ApprovalDecisionError):
        await Runner().resume_async(agent, paused.interruption, deps=shop)  # no decision
    with pytest.raises(ApprovalDecisionError):
        await Runner().resume_async(
            agent, paused.interruption, approve=["r1"], reject={"r1": "x"}, deps=shop
        )

    done = await Runner().resume_async(
        agent, paused.interruption, reject={"r1": "over policy limit"}, deps=shop
    )
    assert shop.refunds == []
    assert "over policy limit" in model.calls[1]["messages"][-1].content
    assert done.output == "Escalated instead."


async def test_small_amounts_skip_approval() -> None:
    shop = Shop({}, [])
    agent = _refund_agent(
        FakeModel(responses=[call("refund", order_id="A2", amount=40), "ok"]), shop
    )
    result = await Runner().run_async(agent, "refund", deps=shop)
    assert result.status == "completed" and shop.refunds == ["A2:40"]


# --------------------------------------------------------------------------- sub-agents


async def test_sub_agent_usage_counts_toward_the_parent_run() -> None:
    specialist = Agent(
        name="Specialist",
        instructions="",
        model=FakeModel(responses=["policy says yes"], default_usage=Usage(10, 5, 15, True)),
        description="Answers policy questions.",
    )
    parent_model = FakeModel(
        responses=[call("specialist", request="allowed?"), "Yes."],
        default_usage=Usage(1, 1, 2, True),
    )
    parent = Agent(name="Lead", instructions="", model=parent_model, tools=[specialist.as_tool()])
    result = await Runner().run_async(parent, "can we?")
    assert result.output == "Yes."
    assert result.usage.total_tokens == 2 + 2 + 15


# --------------------------------------------------------------------------- providers


async def test_provider_models_stream_text_and_get_budget_context() -> None:
    from nexura_runtime.usage.manager import BudgetManager
    from nexura_runtime.usage.models import BudgetConfig, ModelPricing
    from nexura_runtime.usage.pricing import PricingTable

    deltas = [
        StreamDelta(kind="text_delta", text="Hel"),
        StreamDelta(kind="text_delta", text="lo"),
        StreamDelta(kind="usage_delta", usage=Usage(4, 2, 6, True)),
        StreamDelta(kind="finish", finish_reason="stop"),
    ]
    provider = StreamingFakeProvider("fake-stream", queued_deltas=[deltas])
    pricing = PricingTable(version="t", date="2026-10-08", source="test")
    pricing.add_model_pricing(
        ModelPricing(
            model_id="fake-stream",
            input_cost_per_million=Decimal("1"),
            output_cost_per_million=Decimal("2"),
            effective_date="2026-10-08",
        )
    )
    budget = BudgetManager(pricing_table=pricing).create_run_budget(
        workspace_id="ws", run_id="r", config=BudgetConfig(max_cost_usd=Decimal("1"))
    )
    agent = Agent(name="S", instructions="", model=provider)
    events = [e async for e in Runner(budget=budget).stream_async(agent, "hi", stream_text=True)]

    assert [e.data["text"] for e in events if e.type == RunEventType.TEXT_DELTA] == ["Hel", "lo"]
    completed = next(e for e in events if e.type == RunEventType.RUN_COMPLETED)
    assert completed.data["output"] == "Hello"
    assert budget.get_status().total_spent_usd > 0  # admitted and settled via context


async def test_retry_and_fallback_models_drive_agents() -> None:
    class Flaky(FakeModel):
        def __init__(self, failures: int, reply: str) -> None:
            super().__init__(responses=[reply])
            self.failures = failures

        async def generate(self, messages: Any, tools: Any = None, **kw: Any) -> ModelResponse:
            if self.failures:
                self.failures -= 1
                raise ProviderRateLimitError("slow down")
            return await super().generate(messages, tools, timeout=kw.get("timeout"))

    retried = RetryModel(Flaky(2, "after retries"), max_attempts=3, base_delay=0.001)
    assert (await Runner().run_async(Agent("R", "", retried), "x")).output == "after retries"

    primary = Flaky(5, "unused")
    backup = FakeModel(responses=["from backup"])
    fallback = FallbackModel(RetryModel(primary, max_attempts=2, base_delay=0.001), backup)
    result = await Runner().run_async(Agent("F", "", fallback), "x")
    assert result.output == "from backup" and fallback.last_used is backup


async def test_approval_binds_to_exact_arguments() -> None:
    shop = Shop({}, [])
    model = FakeModel(responses=[call("refund", "r1", order_id="A1", amount=450), "done"])
    agent = _refund_agent(model, shop)
    paused = await Runner().run_async(agent, "refund", deps=shop)
    assert paused.interruption is not None
    reviewed = paused.interruption.pending_digests()

    # Someone edits the stored interruption after the reviewer saw it.
    tampered = RunInterruption.from_json(
        paused.interruption.to_json().replace('"amount": 450', '"amount": 45000')
    )
    with pytest.raises(ApprovalDecisionError, match="arguments changed|differs"):
        await Runner().resume_async(agent, tampered, approve=reviewed, deps=shop)
    assert shop.refunds == []

    done = await Runner().resume_async(agent, paused.interruption, approve=reviewed, deps=shop)
    assert done.output == "done" and shop.refunds == ["A1:450"]


async def test_approve_true_approves_everything_not_rejected() -> None:
    shop = Shop({}, [])
    batch = [
        ToolCall(id="r1", name="refund", arguments={"order_id": "A1", "amount": 450}),
        ToolCall(id="r2", name="refund", arguments={"order_id": "B2", "amount": 900}),
    ]
    agent = _refund_agent(FakeModel(responses=[batch, "Partly done."]), shop)
    runner = Runner()
    paused = await runner.run_async(agent, "refund both", deps=shop)
    assert paused.interruption is not None
    done = await runner.resume_async(
        agent, paused.interruption, approve=True, reject={"r2": "too large"}, deps=shop
    )
    assert shop.refunds == ["A1:450"]
    assert done.output == "Partly done."
