"""P38 C2: tracing follows the pinned OpenTelemetry GenAI conventions (real OTel SDK).

Spans go to the SDK's ``InMemorySpanExporter`` and metrics to its ``InMemoryMetricReader``;
models are local fakes. Covers the spec attribute names and values, span names and kinds,
opt-in structured content (off by default), the deprecated aliases kept for one release,
``error.type``, and the ``nexura.approval.*`` / ``nexura.budget.*`` / ``nexura.effect.*``
events. It imports only the agents package and the OpenTelemetry SDK, so the agents-wheel
check (scripts/test_agents_wheel.py) runs it too.
"""

from __future__ import annotations

import contextlib
import hmac
import json
import subprocess
import sys
import textwrap
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import pytest
from nexura_agents import (
    Agent,
    FakeModel,
    MemoryCheckpointStore,
    Message,
    ModelResponse,
    Runner,
    ToolCall,
    Usage,
    tool,
)
from nexura_agents import tracing as nexura_tracing
from nexura_agents.errors import ProviderError
from nexura_agents.models.resilience import RetryModel
from nexura_agents.types import response_identity
from nexura_agents.usage import BudgetConfig, BudgetManager
from nexura_agents.usage.errors import UsageError
from nexura_agents.usage.models import ModelPricing
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind

SECRET = "sk-c2SECRETtoken0123456789abcdef"
USAGE = Usage(
    prompt_tokens=100,
    completion_tokens=20,
    total_tokens=120,
    cache_read_input_tokens=40,
    cache_creation_input_tokens=10,
    reasoning_tokens=5,
)


class Small(FakeModel):
    model = "small-model"


class Telemetry:
    def __init__(self, capture: bool) -> None:
        self.spans = InMemorySpanExporter()
        self.metrics = InMemoryMetricReader()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(self.spans))
        nexura_tracing.instrument(
            tracer_provider=provider,
            meter_provider=MeterProvider(metric_readers=[self.metrics]),
            capture_content=capture,
        )

    def finished(self) -> list[ReadableSpan]:
        return list(self.spans.get_finished_spans())

    def one(self, name: str) -> ReadableSpan:
        found = [s for s in self.finished() if s.name == name]
        assert len(found) == 1, [s.name for s in self.finished()]
        return found[0]

    def points(self, name: str) -> list[Any]:
        data = self.metrics.get_metrics_data()
        if data is None:
            return []
        return [
            point
            for resource in data.resource_metrics
            for scope in resource.scope_metrics
            for metric in scope.metrics
            if metric.name == name
            for point in metric.data.data_points
        ]

    def dump(self) -> str:
        out = []
        for s in self.finished():
            out.append(s.name + (s.status.description or ""))
            out.append(json.dumps(dict(s.attributes or {}), default=str))
            for e in s.events:
                out.append(e.name + json.dumps(dict(e.attributes or {}), default=str))
        return "\n".join(out)


@pytest.fixture
def otel() -> Iterator[Telemetry]:
    telemetry = Telemetry(capture=False)
    try:
        yield telemetry
    finally:
        nexura_tracing.uninstrument()


@pytest.fixture
def otel_capture() -> Iterator[Telemetry]:
    telemetry = Telemetry(capture=True)
    try:
        yield telemetry
    finally:
        nexura_tracing.uninstrument()


@tool
def lookup(city: str) -> str:
    """Look up the weather."""
    return json.dumps({"city": city, "sky": "rainy"})


@tool
def broken() -> str:
    """Always fails."""
    raise RuntimeError("down")


def reply(text: str = "It is rainy.") -> ModelResponse:
    return ModelResponse(
        message=Message(role="assistant", content=text),
        usage=USAGE,
        finish_reason="stop",
        response_id="resp_c2_1",
        response_model="small-model-2026-10",
    )


def tool_agent() -> Agent:
    calls = [ToolCall(id="call_1", name="lookup", arguments={"city": "Paris"})]
    model = Small(responses=[calls, reply()], default_usage=USAGE)
    return Agent(name="Weather", instructions="Answer briefly.", model=model, tools=[lookup])


# ---------------------------------------------------------------- spans and attributes


async def test_spec_names_kinds_and_attributes(otel: Telemetry) -> None:
    result = await Runner().run_async(tool_agent(), f"Weather in Paris? key {SECRET}")
    assert result.status == "completed"

    run = otel.one("invoke_agent Weather")
    assert run.kind == SpanKind.INTERNAL
    assert run.attributes["gen_ai.operation.name"] == "invoke_agent"
    assert run.attributes["gen_ai.agent.name"] == "Weather"

    chats = [s for s in otel.finished() if s.name == "chat small-model"]
    assert len(chats) == 2
    last = chats[-1]
    assert last.kind == SpanKind.CLIENT
    attrs = last.attributes
    assert attrs["gen_ai.operation.name"] == "chat"
    assert attrs["gen_ai.provider.name"] == "fake"
    assert attrs["gen_ai.request.model"] == "small-model"
    assert attrs["gen_ai.response.id"] == "resp_c2_1"
    assert attrs["gen_ai.response.model"] == "small-model-2026-10"
    assert tuple(attrs["gen_ai.response.finish_reasons"]) == ("stop",)
    assert attrs["gen_ai.usage.input_tokens"] == 100
    assert attrs["gen_ai.usage.output_tokens"] == 20
    assert attrs["gen_ai.usage.cache_read.input_tokens"] == 40
    assert attrs["gen_ai.usage.cache_write.input_tokens"] == 10
    assert attrs["gen_ai.usage.reasoning.output_tokens"] == 5
    # The first reply came from the default usage without an id: nothing is invented.
    assert "gen_ai.response.id" not in chats[0].attributes

    call = otel.one("execute_tool lookup")
    assert call.kind == SpanKind.INTERNAL
    assert call.attributes["gen_ai.operation.name"] == "execute_tool"
    assert call.attributes["gen_ai.tool.name"] == "lookup"
    run_id = str(dict(run.attributes or {})["nexura.run_id"])
    expected_id = "call-" + hmac.digest(run_id.encode(), b"call_1", "sha256").hex()[:16]
    assert call.attributes["gen_ai.tool.call.id"] == expected_id

    # Run totals carry the spec usage names too.
    assert run.attributes["gen_ai.usage.reasoning.output_tokens"] == 10


async def test_deprecated_aliases_are_still_emitted(otel: Telemetry) -> None:
    await Runner().run_async(tool_agent(), "hi")
    chat = [s for s in otel.finished() if s.name.startswith("chat ")][-1]
    assert chat.attributes["gen_ai.system"] == chat.attributes["gen_ai.provider.name"] == "fake"
    run = otel.one("invoke_agent Weather")
    assert run.attributes["nexura.usage.reasoning_tokens"] == 10
    assert otel.points("gen_ai.client.token.usage")
    assert {
        p.attributes["gen_ai.operation.name"]
        for p in otel.points("gen_ai.client.operation.duration")
    } >= {"chat", "invoke_agent", "execute_tool"}


async def test_capture_is_off_by_default_and_nothing_leaks(otel: Telemetry) -> None:
    await Runner().run_async(tool_agent(), f"Weather in Paris? api_key={SECRET}")
    for span in otel.finished():
        attrs = span.attributes or {}
        for key in (
            "gen_ai.input.messages",
            "gen_ai.output.messages",
            "gen_ai.system_instructions",
            "gen_ai.tool.call.arguments",
            "gen_ai.tool.call.result",
        ):
            assert key not in attrs, (span.name, key)
        names = {e.name for e in span.events}
        assert not names & {"gen_ai.content.prompt", "gen_ai.content.completion"}
    dump = otel.dump()
    assert SECRET not in dump
    assert "Paris" not in dump and "Answer briefly" not in dump and "rainy" not in dump


async def test_capture_records_structured_messages(otel_capture: Telemetry) -> None:
    await Runner().run_async(tool_agent(), "Weather in Paris?")
    chats = [s for s in otel_capture.finished() if s.name.startswith("chat ")]
    first, last = chats[0].attributes, chats[-1].attributes

    assert json.loads(first["gen_ai.system_instructions"]) == [
        {"type": "text", "content": "Answer briefly."}
    ]
    assert json.loads(first["gen_ai.input.messages"]) == [
        {"role": "user", "parts": [{"type": "text", "content": "Weather in Paris?"}]}
    ]
    assert json.loads(first["gen_ai.output.messages"]) == [
        {
            "role": "assistant",
            "parts": [
                {
                    "type": "tool_call",
                    "id": "call_1",
                    "name": "lookup",
                    "arguments": {"city": "Paris"},
                }
            ],
        }
    ]

    history = json.loads(last["gen_ai.input.messages"])
    assert [m["role"] for m in history] == ["user", "assistant", "tool"]
    response_part = history[2]["parts"][0]
    assert response_part["type"] == "tool_call_response"
    assert response_part["id"] == "call_1"
    assert response_part["response"] == {"city": "Paris", "sky": "rainy"}
    output = json.loads(last["gen_ai.output.messages"])
    # The pinned schema deprecates finish_reason on output messages; it is on the span.
    assert output == [{"role": "assistant", "parts": [{"type": "text", "content": "It is rainy."}]}]
    assert tuple(last["gen_ai.response.finish_reasons"]) == ("stop",)

    call = otel_capture.one("execute_tool lookup").attributes
    assert json.loads(call["gen_ai.tool.call.arguments"]) == {"city": "Paris"}
    assert json.loads(call["gen_ai.tool.call.result"]) == {"city": "Paris", "sky": "rainy"}

    # Deprecated content events stay for one release when capture is on.
    events = {e.name for s in chats for e in s.events}
    assert {"gen_ai.content.prompt", "gen_ai.content.completion"} <= events


async def test_captured_content_is_redacted(otel_capture: Telemetry) -> None:
    @tool
    def call_api(token: str, query: str) -> str:
        """Call an API."""
        return f"Authorization: Bearer {SECRET}"

    calls = [ToolCall(id="c9", name="call_api", arguments={"token": SECRET, "query": "q"})]
    agent = Agent(
        name="Api",
        instructions="Use the API.",
        model=Small(responses=[calls, "done"], default_usage=USAGE),
        tools=[call_api],
    )
    await Runner().run_async(agent, f"use api_key={SECRET}")
    dump = otel_capture.dump()
    assert SECRET not in dump
    assert "[REDACTED]" in dump
    call = otel_capture.one("execute_tool call_api").attributes
    assert json.loads(call["gen_ai.tool.call.arguments"]) == {"token": "[REDACTED]", "query": "q"}


async def test_long_text_is_truncated_keeping_json(otel_capture: Telemetry) -> None:
    agent = Agent(name="Long", instructions="i", model=Small(responses=["ok"], default_usage=USAGE))
    await Runner().run_async(agent, "x" * (nexura_tracing.MAX_CONTENT_CHARS + 500))
    chat = next(s for s in otel_capture.finished() if s.name.startswith("chat "))
    history = json.loads(chat.attributes["gen_ai.input.messages"])
    text = history[0]["parts"][0]["content"]
    assert text.endswith("[TRUNCATED]")
    assert len(text) < nexura_tracing.MAX_CONTENT_CHARS + 20


# ---------------------------------------------------------------- errors


class Failing:
    model = "flaky-model"

    async def generate(self, messages: Any, tools: Any = None, *, timeout: Any = None) -> Any:
        raise RuntimeError(f"upstream 500 Authorization: Bearer {SECRET}")


async def test_error_type_on_failed_operations(otel: Telemetry) -> None:
    agent = Agent(name="Down", instructions="i", model=Failing())
    with pytest.raises(ProviderError):
        await Runner().run_async(agent, "hi")
    chat = otel.one("chat flaky-model")
    assert chat.attributes["error.type"] == "RuntimeError"
    run = otel.one("invoke_agent Down")
    assert run.attributes["error.type"] == "ProviderError"
    durations = otel.points("gen_ai.client.inference.duration")
    assert [p.attributes.get("error.type") for p in durations] == ["RuntimeError"]
    assert SECRET not in otel.dump()

    tool_agent_ = Agent(
        name="Tools",
        instructions="i",
        model=Small(responses=[[ToolCall(id="b1", name="broken", arguments={})], "x"]),
        tools=[broken],
    )
    with contextlib.suppress(Exception):
        await Runner().run_async(tool_agent_, "go")
    failed = otel.one("execute_tool broken")
    assert failed.attributes["error.type"]
    tool_points = otel.points("gen_ai.execute_tool.duration")
    assert any(p.attributes.get("error.type") for p in tool_points)


# ---------------------------------------------------------------- metrics


async def test_spec_metrics(otel: Telemetry) -> None:
    await Runner().run_async(tool_agent(), "hi")

    inference = otel.points("gen_ai.client.inference.duration")
    assert len(inference) == 2  # one point per (attribute set); two replies, two models seen
    assert {p.attributes["gen_ai.provider.name"] for p in inference} == {"fake"}
    assert {p.attributes["gen_ai.operation.name"] for p in inference} == {"chat"}
    assert "small-model-2026-10" in {p.attributes.get("gen_ai.response.model") for p in inference}

    used = otel.points("gen_ai.client.inference.usage.input_tokens")
    assert sum(p.value for p in used) == 200
    assert {p.attributes["gen_ai.token.modality"] for p in used} == {"unknown"}
    assert sum(p.value for p in otel.points("gen_ai.client.inference.usage.output_tokens")) == 40
    cache = otel.points("gen_ai.client.inference.usage.cache_read.input_tokens")
    assert sum(p.value for p in cache) == 80
    written = otel.points("gen_ai.client.inference.usage.cache_write.input_tokens")
    assert sum(p.value for p in written) == 20
    thinking = otel.points("gen_ai.client.inference.usage.reasoning.output_tokens")
    assert sum(p.value for p in thinking) == 10
    per_call = otel.points("gen_ai.client.inference.operation.input_tokens")
    assert sum(p.count for p in per_call) == 2

    agent_points = otel.points("gen_ai.invoke_agent.duration")
    assert [p.attributes for p in agent_points] == [{"gen_ai.agent.name": "Weather"}]
    tool_points = otel.points("gen_ai.execute_tool.duration")
    assert [dict(p.attributes) for p in tool_points] == [{"gen_ai.tool.name": "lookup"}]
    # Metric attributes never carry ids or content.
    for name in ("gen_ai.client.inference.duration", "gen_ai.invoke_agent.duration"):
        for point in otel.points(name):
            assert "gen_ai.response.id" not in point.attributes


# ---------------------------------------------------------------- provider names


def test_provider_name_mapping() -> None:
    assert nexura_tracing.provider_name(Small(responses=["x"])) == "fake"
    assert nexura_tracing.provider_name(RetryModel(Small(responses=["x"]))) == "fake"

    class LiteLLMModel:  # same class name as the adapter, without importing litellm
        def __init__(self, model: str) -> None:
            self.model = model

    assert nexura_tracing.provider_name(LiteLLMModel("bedrock/anthropic.claude")) == "aws.bedrock"
    assert nexura_tracing.provider_name(LiteLLMModel("gemini/gemini-2.5")) == "gcp.gemini"
    assert nexura_tracing.provider_name(LiteLLMModel("gpt-4o")) == "openai"

    class Custom:
        gen_ai_provider_name = "acme"

    assert nexura_tracing.provider_name(Custom()) == "acme"
    assert nexura_tracing.provider_name(object()) is None


def test_response_identity_only_takes_text() -> None:
    assert response_identity({"id": "r1", "model": "m"}) == {
        "response_id": "r1",
        "response_model": "m",
    }
    assert response_identity({"id": 5, "model": ""}) == {
        "response_id": None,
        "response_model": None,
    }
    assert response_identity(
        {"responseId": "g1", "modelVersion": "gemini-x"}, "responseId", "modelVersion"
    ) == {"response_id": "g1", "response_model": "gemini-x"}
    assert response_identity(None) == {"response_id": None, "response_model": None}


async def test_openai_adapter_reports_response_id_and_model(otel: Telemetry) -> None:
    import httpx
    from nexura_agents.models.openai import OpenAIChatModel

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-c2",
                "model": "gpt-test-2026-10-01",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "hello"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
            },
        )

    model = OpenAIChatModel(
        "gpt-test",
        api_key="test-key-not-real",
        base_url="https://api.openai.com/v1",  # the mock transport answers; nothing is sent
        transport=httpx.MockTransport(handler),
    )
    agent = Agent(name="Mocked", instructions="i", model=model)
    await Runner().run_async(agent, "hi")
    chat = otel.one("chat gpt-test")
    assert chat.attributes["gen_ai.provider.name"] == "openai"
    assert chat.attributes["gen_ai.response.id"] == "chatcmpl-c2"
    assert chat.attributes["gen_ai.response.model"] == "gpt-test-2026-10-01"


# ---------------------------------------------------------------- nexura.* events


@tool(requires_approval=True)
def send_refund(order_id: str, amount: int) -> str:
    """Refund an order."""
    return f"refunded {amount} for {order_id}"


def refund_agent() -> Agent:
    calls = [ToolCall(id="r1", name="send_refund", arguments={"order_id": "A1", "amount": 40})]
    return Agent(
        name="Support",
        instructions="Help.",
        model=Small(responses=[calls, "Refund sent."], default_usage=USAGE),
        tools=[send_refund],
    )


def events(span: ReadableSpan, name: str) -> list[dict[str, Any]]:
    return [dict(e.attributes or {}) for e in span.events if e.name == name]


async def test_approval_requested_and_decided(otel: Telemetry) -> None:
    agent = refund_agent()
    runner = Runner()
    paused = await runner.run_async(agent, "Refund A1")
    assert paused.status == "paused"
    interruption = paused.interruption
    first = otel.one("invoke_agent Support")
    (requested,) = events(first, "nexura.approval.requested")
    assert requested == {
        "nexura.approval.id": interruption.interruption_id,
        "nexura.approval.kind": "tool_call",
        "nexura.approval.reason": "approval",
        "nexura.approval.pending_count": 1,
        "nexura.approval.tools": ("send_refund",),
    }

    otel.spans.clear()
    done = await runner.resume_async(agent, interruption, approve=interruption.pending_digests())
    assert done.status == "completed"
    resumed = otel.one("invoke_agent Support")
    (decided,) = events(resumed, "nexura.approval.decided")
    assert decided["nexura.approval.id"] == interruption.interruption_id
    assert decided["nexura.approval.decision"] == "approve"
    assert decided["nexura.approval.approved_count"] == 1
    assert decided["nexura.approval.rejected_count"] == 0


async def test_rejected_approval_and_invalid_decision(otel: Telemetry) -> None:
    from nexura_agents.errors import ApprovalDecisionError

    agent = refund_agent()
    runner = Runner()
    paused = await runner.run_async(agent, "Refund A1")
    interruption = paused.interruption
    otel.spans.clear()
    # A decision that fails its checks records no decided event.
    with pytest.raises(ApprovalDecisionError):
        await runner.resume_async(agent, interruption, approve={"r1": "0" * 64})
    assert all(not events(s, "nexura.approval.decided") for s in otel.finished()), otel.dump()
    await runner.resume_async(agent, interruption, reject={"r1": "duplicate"})
    decided = [e for s in otel.finished() for e in events(s, "nexura.approval.decided")]
    assert [d["nexura.approval.decision"] for d in decided] == ["reject"]
    assert "duplicate" not in otel.dump()  # the reason text is never recorded


def run_budget(**limits: Any) -> Any:
    """A real ``RunBudget`` (in-memory ledger) with zero pricing for the fake model."""
    manager = BudgetManager()
    manager.pricing_table.add_model_pricing(
        ModelPricing(
            model_id="small-model",
            input_cost_per_million=Decimal("0"),
            output_cost_per_million=Decimal("0"),
        )
    )
    return manager.create_run_budget("w", "r", config=BudgetConfig(**limits))


def budget_events(otel: Telemetry) -> list[tuple[str, dict[str, Any]]]:
    found = [
        (e.name, dict(e.attributes or {}))
        for s in otel.finished()
        for e in s.events
        if e.name.startswith("nexura.budget.")
    ]
    found += [
        (s.name, dict(s.attributes or {}))
        for s in otel.finished()
        if s.name.startswith("nexura.budget.")
    ]
    return found


async def test_budget_reserve_and_settle(otel: Telemetry) -> None:
    budget = run_budget(max_tokens=100_000)
    agent = Agent(name="B", instructions="i", model=Small(responses=["ok"], default_usage=USAGE))
    with warnings_ignored():
        result = await Runner(budget=budget).run_async(agent, "hi")
    assert result.status == "completed"
    found = budget_events(otel)
    kinds = [name for name, _ in found]
    assert kinds == ["nexura.budget.reserve", "nexura.budget.settle"]
    reserve, settle = found[0][1], found[1][1]
    assert reserve["nexura.budget.model"] == "small-model"
    assert reserve["nexura.budget.method"] == "generate"
    assert reserve["nexura.budget.reservation_id"] == settle["nexura.budget.reservation_id"]
    assert reserve["nexura.budget.estimated_tokens"] > 0
    assert settle["nexura.budget.outcome"] == "settled"
    assert settle["nexura.budget.input_tokens"] == 100
    # The reservation is recorded on the model call it pays for.
    chat = otel.one("chat small-model")
    assert [e.name for e in chat.events] == ["nexura.budget.reserve", "nexura.budget.settle"]


async def test_budget_refusal_is_recorded_and_still_raises(otel: Telemetry) -> None:
    agent = Agent(name="B", instructions="i", model=Small(responses=["ok"], default_usage=USAGE))
    with pytest.raises(UsageError), warnings_ignored():
        await Runner(budget=run_budget(max_tokens=10)).run_async(agent, "hi")
    found = budget_events(otel)
    assert [name for name, _ in found] == ["nexura.budget.refuse"]
    refused = found[0][1]
    assert refused["error.type"] == "BudgetExceededError"
    assert refused["nexura.budget.model"] == "small-model"
    assert "nexura.budget.reservation_id" not in refused


async def test_durable_effect_intent_and_outcome(otel: Telemetry) -> None:
    @tool
    def charge(amount: int) -> str:
        """Charge a card (not idempotent)."""
        return "charged"

    agent = Agent(
        name="Billing",
        instructions="i",
        model=Small(
            responses=[[ToolCall(id="k1", name="charge", arguments={"amount": 5})], "ok"],
            default_usage=USAGE,
        ),
        tools=[charge],
    )
    result = await Runner(checkpoint_store=MemoryCheckpointStore()).run_async(agent, "go")
    assert result.status == "completed"
    run = otel.one("invoke_agent Billing")
    names = [e.name for e in run.events if e.name.startswith("nexura.effect.")]
    assert names == ["nexura.effect.intent", "nexura.effect.outcome"]
    intent = events(run, "nexura.effect.intent")[0]
    outcome = events(run, "nexura.effect.outcome")[0]
    assert intent["nexura.effect.class"] == "non_idempotent"
    assert intent["nexura.effect.status"] == "pending"
    assert outcome["nexura.effect.status"] == "confirmed"
    assert intent["nexura.effect.id"] == outcome["nexura.effect.id"]
    assert intent["nexura.run_id"] == result.run_id
    assert "charged" not in otel.dump()  # effect results are never recorded


def test_events_without_a_span_become_standalone_spans(otel: Telemetry) -> None:
    nexura_tracing.budget_event("reserve", "m", method="generate", reservation_id="r")
    (span,) = otel.finished()
    assert span.name == "nexura.budget.reserve"
    assert span.attributes["nexura.budget.reservation_id"] == "r"


def test_event_helpers_drop_non_plain_values(otel: Telemetry) -> None:
    nexura_tracing.record_event(
        "nexura.approval.requested", {"ok": "x", "obj": object(), "nan": float("nan")}
    )
    (span,) = otel.finished()
    assert dict(span.attributes) == {"ok": "x"}


def test_helpers_do_nothing_while_tracing_is_off() -> None:
    assert not nexura_tracing.is_instrumented()
    nexura_tracing.record_event("nexura.approval.requested", {"a": 1})
    nexura_tracing.budget_event("refuse", "m", error_type="UsageError")
    nexura_tracing.approval_requested(None, "a1", kind="tool_call", tools=["t"])
    nexura_tracing.approval_decided(None, "a1", kind="tool_call", decision="approve")
    nexura_tracing.effects_committed([object()], run_id="r")


def test_no_opentelemetry_installed_never_raises() -> None:
    script = textwrap.dedent(
        """
        import asyncio, sys

        class Block:
            def find_spec(self, name, path=None, target=None):
                if name == "opentelemetry" or name.startswith("opentelemetry."):
                    raise ImportError("blocked for the test")
                return None

        sys.meta_path.insert(0, Block())
        from nexura_agents import Agent, FakeModel, MemoryCheckpointStore, Runner, ToolCall, tool
        from nexura_agents import tracing

        @tool
        def charge(amount: int) -> str:
            "Charge."
            return "ok"

        class Allowance:
            async def admit_provider_call_async(self, model_id, **kw):
                return "r1"
            async def settle_provider_call_async(self, reservation, **kw):
                return None
            def record_turn(self):
                return None
            def record_tool_call(self, name):
                return None

        agent = Agent(
            name="A",
            instructions="i",
            model=FakeModel(
                responses=[[ToolCall(id="c", name="charge", arguments={"amount": 1})], "done"]
            ),
            tools=[charge],
        )
        runner = Runner(checkpoint_store=MemoryCheckpointStore(), budget=Allowance())
        result = asyncio.run(runner.run_async(agent, "go"))
        assert result.status == "completed", result.status
        tracing.record_event("nexura.budget.reserve", {"a": 1})
        try:
            tracing.instrument()
        except ImportError:
            print("ok")
        """
    )
    done = subprocess.run(
        [sys.executable, "-W", "ignore", "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip().endswith("ok")


@contextlib.contextmanager
def warnings_ignored() -> Iterator[None]:
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        yield


def test_pinned_spec_reference() -> None:
    assert len(nexura_tracing.SEMCONV_GENAI_COMMIT) == 40
    assert nexura_tracing.SEMCONV_GENAI_REPOSITORY.endswith("semantic-conventions-genai")
    assert nexura_tracing.ALIASES_REMOVED_IN == "0.5.0"


# ---------------------------------------------------------------- review fixes (P38 C2 review)

# A valid JSON document nested deeper than Python's recursion limit: json.loads raises
# RecursionError on it. Tool results are often untrusted (web pages, API bodies).
DEEP_JSON = "[" * 100000 + "]" * 100000


@tool
def fetch_page() -> str:
    """Return untrusted JSON text."""
    return DEEP_JSON


def deep_agent() -> Agent:
    calls = [ToolCall(id="c1", name="fetch_page", arguments={})]
    return Agent(
        name="Fetcher",
        instructions="i",
        model=FakeModel(responses=[calls, "done"]),
        tools=[fetch_page],
    )


async def test_r1_untraced_control_completes() -> None:
    assert not nexura_tracing.is_instrumented()
    assert (await Runner().run_async(deep_agent(), "go")).status == "completed"


async def test_r1_tracing_with_capture_off_never_fails_the_run(otel: Telemetry) -> None:
    result = await Runner().run_async(deep_agent(), "go")
    assert result.status == "completed"
    call = otel.one("execute_tool fetch_page")
    assert "gen_ai.tool.call.result" not in call.attributes


async def test_r1_tracing_with_capture_on_never_fails_the_run(otel_capture: Telemetry) -> None:
    result = await Runner().run_async(deep_agent(), "go")
    assert result.status == "completed"
    # Too large to parse: recorded as (truncated) text instead of failing.
    recorded = json.loads(
        otel_capture.one("execute_tool fetch_page").attributes["gen_ai.tool.call.result"]
    )
    assert isinstance(recorded, str) and recorded.endswith("[TRUNCATED]")
    last_chat = [s for s in otel_capture.finished() if s.name.startswith("chat ")][-1]
    history = json.loads(last_chat.attributes["gen_ai.input.messages"])
    assert history[-1]["parts"][0]["type"] == "tool_call_response"


def test_r1_structured_parsing_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    nested = "[" * 5000 + "]" * 5000  # below the size limit
    # Parsed or not, recording it is bounded: redaction stops at depth 12.
    assert isinstance(nexura_tracing._json_attribute(nexura_tracing._structured(nested)), str)
    assert nexura_tracing._structured('{"a": 1}') == {"a": 1}
    assert nexura_tracing._structured("plain text") == "plain text"
    too_big = "[" + "1," * nexura_tracing.MAX_MESSAGES_CHARS + "1]"
    assert nexura_tracing._structured(too_big) == too_big  # not parsed at all

    def deep(_: str) -> Any:
        raise RecursionError("maximum recursion depth exceeded while decoding")

    monkeypatch.setattr(nexura_tracing.json, "loads", deep)
    assert nexura_tracing._structured("[[1]]") == "[[1]]"


def test_r2_input_messages_keep_the_newest_messages(otel_capture: Telemetry) -> None:
    history = [Message(role="user", content=f"msg-{i}") for i in range(150)]
    chat = nexura_tracing.start_chat(nexura_tracing.NOOP, FakeModel(responses=["x"]))
    nexura_tracing.record_prompt(chat, history)
    chat.end()
    (span,) = otel_capture.finished()
    texts = [m["parts"][0]["content"] for m in json.loads(span.attributes["gen_ai.input.messages"])]
    assert texts[-1] == "msg-149"
    assert texts[0] == "msg-50" and len(texts) == nexura_tracing.MAX_HISTORY_MESSAGES


def test_r2_oldest_messages_are_dropped_to_fit(otel_capture: Telemetry) -> None:
    history = [Message(role="user", content=f"m{i} " + "w" * 3990) for i in range(40)]
    chat = nexura_tracing.start_chat(nexura_tracing.NOOP, FakeModel(responses=["x"]))
    nexura_tracing.record_prompt(chat, history)
    chat.end()
    (span,) = otel_capture.finished()
    value = span.attributes["gen_ai.input.messages"]
    assert len(value) <= nexura_tracing.MAX_MESSAGES_CHARS
    texts = [m["parts"][0]["content"] for m in json.loads(value)]
    assert texts[-1].startswith("m39 ") and not texts[0].startswith("m0 ")


def test_m3_capture_processes_each_message_once(
    otel_capture: Telemetry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bounding the history is linear: each message is redacted and serialized once."""
    calls = 0
    original = nexura_tracing._prepared

    def counted(value: Any) -> Any:
        nonlocal calls
        calls += 1
        return original(value)

    monkeypatch.setattr(nexura_tracing, "_prepared", counted)
    history = [Message(role="user", content="word " * 800) for _ in range(100)]
    chat = nexura_tracing.start_chat(nexura_tracing.NOOP, FakeModel(responses=["x"]))
    nexura_tracing.record_prompt(chat, history)
    chat.end()
    assert calls == 100


def test_r3_presigned_attachment_url_is_redacted(otel_capture: Telemetry) -> None:
    from nexura_agents.attachments import Attachment

    signature = "c2REVIEWsignature0123456789abcdef"
    url = (
        "https://bucket.s3.amazonaws.com/report.pdf?X-Amz-Algorithm=AWS4-HMAC-SHA256"
        f"&X-Amz-Signature={signature}"
    )
    inline = Attachment.from_bytes(b"%PDF-1.7 secret body", mime_type="application/pdf")
    message = Message(
        role="user",
        content="summarise",
        attachments=[Attachment.from_url(url, mime_type="application/pdf"), inline],
    )
    chat = nexura_tracing.start_chat(nexura_tracing.NOOP, FakeModel(responses=["x"]))
    nexura_tracing.record_prompt(chat, [message])
    chat.end()
    (span,) = otel_capture.finished()
    parts = json.loads(span.attributes["gen_ai.input.messages"])[0]["parts"]
    assert parts[1]["uri"] == "https://bucket.s3.amazonaws.com/report.pdf?..."
    dump = otel_capture.dump()
    assert signature not in dump
    assert inline.data not in dump  # neither the attribute nor the deprecated event


async def test_m1_token_metrics_carry_the_response_model(otel: Telemetry) -> None:
    await Runner().run_async(tool_agent(), "hi")
    for name in (
        "gen_ai.client.inference.usage.input_tokens",
        "gen_ai.client.inference.operation.input_tokens",
    ):
        models = {p.attributes.get("gen_ai.response.model") for p in otel.points(name)}
        assert "small-model-2026-10" in models, (name, models)


def test_m2_provider_names_follow_the_endpoint() -> None:
    class OpenAIProvider:  # same class names as the adapters, without network clients
        def __init__(self, base_url: str) -> None:
            self.base_url = base_url

    class VertexAIProvider:
        pass

    assert nexura_tracing.provider_name(OpenAIProvider("https://api.openai.com/v1")) == "openai"
    azure = OpenAIProvider("https://acme.openai.azure.com/openai/v1")
    assert nexura_tracing.provider_name(azure) == "azure.ai.openai"
    assert nexura_tracing.provider_name(OpenAIProvider("https://api.groq.com/openai/v1")) == "groq"
    assert nexura_tracing.provider_name(OpenAIProvider("http://localhost:11434/v1")) is None
    assert nexura_tracing.provider_name(VertexAIProvider()) == "gcp.vertex_ai"


async def test_m4_finish_reasons_use_the_convention_values(otel: Telemetry) -> None:
    calls = [ToolCall(id="t1", name="lookup", arguments={"city": "Pune"})]
    first = ModelResponse(
        message=Message(role="assistant", tool_calls=calls), usage=USAGE, finish_reason="tool_calls"
    )
    agent = Agent(name="F", instructions="i", model=Small(responses=[first, "ok"]), tools=[lookup])
    await Runner().run_async(agent, "go")
    chat = next(s for s in otel.finished() if s.name.startswith("chat "))
    assert tuple(chat.attributes["gen_ai.response.finish_reasons"]) == ("tool_call",)
    assert tuple(chat.attributes["nexura.response.finish_reasons"]) == ("tool_calls",)

    with pytest.raises(ProviderError):
        await Runner().run_async(Agent(name="Down", instructions="i", model=Failing()), "hi")
    failed = otel.one("chat flaky-model")
    assert tuple(failed.attributes["gen_ai.response.finish_reasons"]) == ("error",)


async def test_m6_decided_once_per_consumed_approval(otel: Telemetry) -> None:
    agent = refund_agent()
    runner = Runner(checkpoint_store=MemoryCheckpointStore())
    paused = await runner.run_async(agent, "Refund A1")
    interruption = paused.interruption
    done = await runner.resume_async(agent, interruption, approve=interruption.pending_digests())
    assert done.status == "completed"
    decided = [e for s in otel.finished() for e in events(s, "nexura.approval.decided")]
    assert len(decided) == 1 and decided[0]["nexura.approval.decision"] == "approve"
    # The same approval again: refused, and no second decided event.
    with contextlib.suppress(Exception):
        await runner.resume_async(agent, interruption, approve=interruption.pending_digests())
    decided = [e for s in otel.finished() for e in events(s, "nexura.approval.decided")]
    assert len(decided) == 1


# ---------------------------------------------------------------- re-review fixes (n1, n2)


def test_n1_data_url_attachment_is_not_copied(otel_capture: Telemetry) -> None:
    """An Attachment built directly with a data: URL carries its bytes in ``url``."""
    from nexura_agents.attachments import Attachment

    payload = "c2DATAURLpayload0123456789abcdef"
    message = Message(
        role="user",
        content="see file",
        attachments=[
            Attachment(mime_type="application/pdf", url=f"data:application/pdf;base64,{payload}")
        ],
    )
    chat = nexura_tracing.start_chat(nexura_tracing.NOOP, FakeModel(responses=["x"]))
    nexura_tracing.record_prompt(chat, [message])
    chat.end()
    (span,) = otel_capture.finished()
    assert payload not in span.attributes["gen_ai.input.messages"]
    assert payload not in otel_capture.dump()  # nor in the deprecated event
    part = json.loads(span.attributes["gen_ai.input.messages"])[0]["parts"][1]
    assert part["type"] == "uri" and part["uri"] == "[OMITTED non-http URL]"
    assert nexura_tracing._safe_url("gs://bucket/a.pdf") == "gs://bucket/a.pdf"
    assert nexura_tracing._safe_url("file:///etc/passwd") == "[OMITTED non-http URL]"


class Stop(BaseException):
    """Simulated process death: nothing in the runner catches it."""


def decided_events(otel: Telemetry) -> list[dict[str, Any]]:
    return [e for s in otel.finished() for e in events(s, "nexura.approval.decided")]


@pytest.mark.parametrize("window", ["claimed_not_consumed", "consumed_and_traced"])
async def test_n2_recovered_claim_records_decided_exactly_once(
    otel: Telemetry, window: str
) -> None:
    """A resume that stops after its journal claim: recover() applies the decision and the
    decided event appears exactly once, whichever side consumed the approval."""
    store = MemoryCheckpointStore()
    runner = Runner(checkpoint_store=store)
    paused = await runner.run_async(refund_agent(), "Refund A1")
    interruption = paused.interruption
    if window == "claimed_not_consumed":

        async def stop(*args: Any) -> bool:
            raise Stop("claimed, not consumed")

        store.consume_approval = stop  # type: ignore[method-assign]
    else:
        original = runner._claim_approval

        async def claim_then_stop(*args: Any) -> None:
            await original(*args)  # records the claim, consumes, records the event
            raise Stop("consumed, nothing dispatched")

        runner._claim_approval = claim_then_stop  # type: ignore[method-assign]
    with pytest.raises(Stop):
        await runner.resume_async(
            refund_agent(), interruption, approve=interruption.pending_digests()
        )
    if window == "claimed_not_consumed":
        del store.consume_approval
        assert decided_events(otel) == []
    else:
        assert len(decided_events(otel)) == 1

    after = Agent(
        name="Support",
        instructions="Help.",
        model=Small(responses=["Refund sent."], default_usage=USAGE),
        tools=[send_refund],
    )
    done = await Runner(checkpoint_store=store).recover_async(after, paused.run_id)
    assert done.status == "completed", done.status
    decided = decided_events(otel)
    assert len(decided) == 1, decided
    assert decided[0]["nexura.approval.id"] == interruption.interruption_id
    assert decided[0]["nexura.approval.decision"] == "approve"
