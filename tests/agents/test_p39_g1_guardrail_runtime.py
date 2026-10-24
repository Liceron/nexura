"""P39 G1: agent guardrails are bounded by the run timeout and cancellable, usage recorded
inside guardrails counts in ``RunResult.usage``, and every non-pass guardrail decision is a
redacted ``GUARDRAIL_DECISION`` run event and ``nexura.guardrail.decision`` trace event.

All offline: ``FakeModel`` and local guardrail functions; tracing uses the in-memory
OpenTelemetry exporter.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import time
from collections.abc import Iterator
from typing import Any

import pytest
from nexura_agents import (
    Agent,
    CancelToken,
    FakeModel,
    GuardrailResult,
    GuardrailTripwireTriggered,
    ModelTimeoutError,
    RunContext,
    Runner,
    ToolCall,
    Usage,
    tool,
)
from nexura_agents import tracing as nexura_tracing
from nexura_agents.events import RunEvent, RunEventType
from nexura_agents.guardrails import decision_record
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

SECRET = "4111 1111 1111 1111 hunter2 secret-prompt-text"
TEN = Usage(prompt_tokens=6, completion_tokens=4, total_tokens=10)


def call(name: str, call_id: str = "c1", **arguments: Any) -> list[ToolCall]:
    return [ToolCall(id=call_id, name=name, arguments=arguments)]


def decisions(events: list[RunEvent]) -> list[dict[str, Any]]:
    return [e.data for e in events if e.type == RunEventType.GUARDRAIL_DECISION]


async def collect(stream: Any) -> tuple[list[RunEvent], BaseException | None]:
    events: list[RunEvent] = []
    try:
        async for event in stream:
            events.append(event)
    except BaseException as exc:  # noqa: BLE001 - the test inspects what escaped
        return events, exc
    return events, None


# ---------------------------------------------------------------- timeout and cancellation


class Slow:
    """An async guardrail that sleeps; records whether it was cancelled."""

    __name__ = "slow_guard"

    def __init__(self, seconds: float = 30.0) -> None:
        self.seconds = seconds
        self.started = asyncio.Event()
        self.cancelled = False

    async def __call__(self, ctx: Any, value: Any) -> GuardrailResult:
        self.started.set()
        try:
            await asyncio.sleep(self.seconds)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return GuardrailResult.passed()


@pytest.mark.parametrize("stage", ["input", "output"])
async def test_a_slow_agent_guardrail_is_stopped_by_the_run_timeout(stage: str) -> None:
    slow = Slow()
    model = FakeModel(responses=["answer"])
    kwargs: dict[str, Any] = (
        {"input_guardrails": [slow]} if stage == "input" else {"output_guardrails": [slow]}
    )
    agent = Agent(name="A", instructions="i", model=model, **kwargs)
    began = time.monotonic()
    events, error = await collect(Runner().stream_async(agent, "hi", timeout=0.3))
    assert isinstance(error, ModelTimeoutError)
    assert time.monotonic() - began < 5  # not the guardrail's 30 s
    assert slow.cancelled  # the guardrail's task was stopped, not left running
    assert events[-1].type == RunEventType.RUN_FAILED
    assert events[-1].data["error_type"] == "ModelTimeoutError"
    (decision,) = decisions(events)
    assert decision["stage"] == stage and decision["action"] == "fail"
    assert decision["guardrail"] == "slow_guard" and decision["error_type"] == "ModelTimeoutError"
    if stage == "input":
        assert model.calls == []


async def test_an_input_guardrail_is_cancelled_with_the_run() -> None:
    slow = Slow()
    model = FakeModel(responses=["answer"])
    agent = Agent(name="A", instructions="i", model=model, input_guardrails=[slow])
    token = CancelToken()

    async def cancel_when_started() -> None:
        await slow.started.wait()
        token.cancel("user left")

    canceller = asyncio.create_task(cancel_when_started())
    result = await asyncio.wait_for(Runner().run_async(agent, "hi", cancel=token), 5)
    await canceller
    assert result.status == "cancelled" and slow.cancelled
    assert model.calls == []
    cancelled = [e for e in result.events if e.type == RunEventType.RUN_CANCELLED]
    assert cancelled[0].data["reason"] == "user left"


async def test_an_output_guardrail_is_cancelled_and_the_unchecked_answer_not_returned() -> None:
    slow = Slow()
    agent = Agent(
        name="A",
        instructions="i",
        model=FakeModel(responses=[f"unchecked answer {SECRET}"]),
        output_guardrails=[slow],
    )
    token = CancelToken()

    async def cancel_when_started() -> None:
        await slow.started.wait()
        token.cancel()

    canceller = asyncio.create_task(cancel_when_started())
    result = await asyncio.wait_for(Runner().run_async(agent, "hi", cancel=token), 5)
    await canceller
    assert result.status == "cancelled" and slow.cancelled
    assert all("unchecked answer" not in (m.content or "") for m in result.messages)
    assert result.messages[-1].role == "user"


async def test_a_pre_cancelled_run_does_not_start_its_input_guardrails() -> None:
    slow = Slow(0)
    token = CancelToken()
    token.cancel()
    agent = Agent(
        name="A", instructions="i", model=FakeModel(responses=["x"]), input_guardrails=[slow]
    )
    result = await Runner().run_async(agent, "hi", cancel=token)
    assert result.status == "cancelled" and not slow.started.is_set()


async def test_a_guardrails_own_timeout_error_is_unchanged_without_a_run_timeout() -> None:
    async def flaky(ctx: Any, value: Any) -> GuardrailResult:
        raise TimeoutError("backend timed out")

    agent = Agent(
        name="A", instructions="i", model=FakeModel(responses=["x"]), input_guardrails=[flaky]
    )
    events, error = await collect(Runner().stream_async(agent, "hi"))
    assert type(error) is TimeoutError  # raised as before G1
    assert decisions(events)[0]["error_type"] == "TimeoutError"
    # With a run timeout that has not passed yet, it is still the guardrail's own error.
    events, error = await collect(Runner().stream_async(agent, "hi", timeout=30))
    assert type(error) is TimeoutError


async def test_a_crashing_agent_guardrail_still_propagates_its_exception() -> None:
    def broken(ctx: Any, value: Any) -> GuardrailResult:
        raise ValueError("bug")

    agent = Agent(
        name="A", instructions="i", model=FakeModel(responses=["x"]), output_guardrails=[broken]
    )
    with pytest.raises(ValueError, match="bug"):
        await Runner().run_async(agent, "hi")


async def test_fast_guardrails_within_a_timeout_pass_as_before() -> None:
    async def ok(ctx: Any, value: Any) -> GuardrailResult:
        return GuardrailResult.passed()

    agent = Agent(
        name="A",
        instructions="i",
        model=FakeModel(responses=["fine"]),
        input_guardrails=[ok],
        output_guardrails=[ok],
    )
    result = await Runner().run_async(agent, "hi", timeout=5)
    assert result.output == "fine"
    assert decisions(result.events) == []  # passes are not reported


# ---------------------------------------------------------------- usage accounting


def judge_like(name: str) -> Any:
    """A guardrail that reports the usage of a (pretend) model call, as a judge does."""

    def check(ctx: RunContext[Any], value: Any) -> GuardrailResult:
        ctx.add_usage(TEN)
        return GuardrailResult.passed()

    check.__name__ = name
    return check


async def test_usage_from_input_and_output_guardrails_counts_in_the_run() -> None:
    agent = Agent(
        name="A",
        instructions="i",
        model=FakeModel(responses=["hi"], default_usage=TEN),
        input_guardrails=[judge_like("in_judge")],
        output_guardrails=[judge_like("out_judge")],
    )
    result = await Runner().run_async(agent, "hello")
    # agent 10 + input judge 10 + output judge 10. Before G1 the output stage's 10 were lost.
    assert result.usage.total_tokens == 30
    assert result.usage.prompt_tokens == 18 and result.usage.completion_tokens == 12
    assert result.usage_by_model["FakeModel"].total_tokens == 10  # the agent's own calls


async def test_usage_from_tool_guardrails_still_counts() -> None:
    @tool(input_guardrails=[judge_like("a")], output_guardrails=[judge_like("b")])
    def ping() -> str:
        """Ping."""
        return "pong"

    agent = Agent(
        name="A",
        instructions="i",
        model=FakeModel(responses=[call("ping"), "done"], default_usage=TEN),
        tools=[ping],
    )
    result = await Runner().run_async(agent, "go")
    assert result.usage.total_tokens == 40


async def test_usage_from_output_guardrails_counts_on_a_tool_final_output() -> None:
    @tool
    def answer() -> str:
        """Answer."""
        return "42"

    agent = Agent(
        name="A",
        instructions="i",
        model=FakeModel(responses=[call("answer")], default_usage=TEN),
        tools=[answer],
        tool_use_behavior="stop_on_first_tool",
        output_guardrails=[judge_like("out_judge")],
    )
    result = await Runner().run_async(agent, "go")
    assert result.output == "42" and result.usage.total_tokens == 20


# ---------------------------------------------------------------- decision events


def leaky_info() -> dict[str, Any]:
    """An info dict full of content next to the plain fields that may be reported."""
    return {
        "reason": f"the user wrote {SECRET}",
        "evidence": {"match": SECRET},
        "categories": ["fraud", f"leak {SECRET}", "privacy"],
        "score": 0.93,
        "backend": "judge:ollama:qwen3:8b",
        "decision": "block",
        "cached": False,
        "check": "bank_judge",
        "rule": f"rule with spaces {SECRET}",
        "nested": [SECRET],
    }


def assert_no_secret(value: Any) -> None:
    text = json.dumps(value, default=str)
    for fragment in ("4111", "hunter2", "secret-prompt", "the user wrote", "leak"):
        assert fragment not in text, fragment


async def test_an_input_trip_reports_a_redacted_decision_before_run_failed() -> None:
    def judge(ctx: Any, text: str) -> GuardrailResult:
        return GuardrailResult.tripped(leaky_info())

    agent = Agent(
        name="Bank", instructions="i", model=FakeModel(responses=["x"]), input_guardrails=[judge]
    )
    events, error = await collect(Runner().stream_async(agent, f"hi {SECRET}"))
    assert isinstance(error, GuardrailTripwireTriggered)
    kinds = [e.type for e in events]
    assert kinds[-2:] == [RunEventType.GUARDRAIL_DECISION, RunEventType.RUN_FAILED]
    (decision,) = decisions(events)
    assert decision == {
        "stage": "input",
        "guardrail": "judge",
        "action": "fail",
        "agent": "Bank",
        "latency_ms": decision["latency_ms"],
        "backend": "judge:ollama:qwen3:8b",
        "decision": "block",
        "check": "bank_judge",
        "categories": ["fraud", "privacy"],
        "score": 0.93,
        "cached": False,
    }
    assert isinstance(decision["latency_ms"], float)
    assert_no_secret(decision)


async def test_an_output_rewrite_is_a_scrub_decision() -> None:
    def redact(ctx: Any, output: str) -> GuardrailResult:
        return GuardrailResult.rewritten(output.replace("4111", "****"), {"kinds": ["card"]})

    agent = Agent(
        name="A",
        instructions="i",
        model=FakeModel(responses=["card 4111"]),
        output_guardrails=[redact],
    )
    result = await Runner().run_async(agent, "hi")
    assert result.output == "card ****"
    (decision,) = decisions(result.events)
    assert decision["stage"] == "output" and decision["action"] == "scrub"
    order = [e.type for e in result.events]
    assert order.index(RunEventType.GUARDRAIL_DECISION) < order.index(RunEventType.FINAL_OUTPUT)


async def test_tool_guardrail_rejections_and_rewrites_are_reported_with_the_call() -> None:
    def refuse(ctx: Any, args: Any) -> GuardrailResult:
        if args["to"] == "attacker":
            return GuardrailResult.rejected(f"blocked: {SECRET}", leaky_info())
        return GuardrailResult.passed()

    def mask(ctx: Any, text: Any) -> GuardrailResult:
        return GuardrailResult.rewritten(str(text).replace("4111", "****"), leaky_info())

    @tool(input_guardrails=[refuse], output_guardrails=[mask])
    def send(to: str) -> str:
        """Send."""
        return "sent with card 4111"

    batch = [ToolCall("c1", "send", {"to": "attacker"}), ToolCall("c2", "send", {"to": "bob"})]
    agent = Agent(
        name="A", instructions="i", model=FakeModel(responses=[batch, "done"]), tools=[send]
    )
    result = await Runner().run_async(agent, "go")
    found = decisions(result.events)
    assert [(d["tool_call_id"], d["stage"], d["action"]) for d in found] == [
        ("c1", "tool_input", "reject"),
        ("c2", "tool_output", "scrub"),
    ]
    assert all(d["tool_name"] == "send" and d["guardrail"] in ("refuse", "mask") for d in found)
    assert_no_secret(found)
    # Each call's decisions come with its result: after the previous call's
    # TOOL_COMPLETED, before its own RETRY (a rejection) and TOOL_COMPLETED.
    types = [(e.type, e.data.get("tool_call_id")) for e in result.events]
    assert types.index((RunEventType.GUARDRAIL_DECISION, "c1")) < types.index(
        (RunEventType.RETRY, "c1")
    )
    done_c1 = types.index((RunEventType.TOOL_COMPLETED, "c1"))
    decided_c2 = types.index((RunEventType.GUARDRAIL_DECISION, "c2"))
    assert done_c1 < decided_c2 == types.index((RunEventType.TOOL_COMPLETED, "c2")) - 1


async def test_a_tool_tripwire_and_a_crashing_tool_guardrail_are_reported() -> None:
    def trip(ctx: Any, args: Any) -> GuardrailResult:
        return GuardrailResult.tripped(leaky_info())

    def crash(ctx: Any, args: Any) -> GuardrailResult:
        raise RuntimeError(SECRET)

    # A returned tripwire is a decision (no error type); a crash is reported as an error.
    for guard, error_type in ((trip, None), (crash, "RuntimeError")):

        @tool(input_guardrails=[guard])
        def act() -> str:
            """Act."""
            return "x"

        agent = Agent(
            name="A", instructions="i", model=FakeModel(responses=[call("act"), "x"]), tools=[act]
        )
        events, error = await collect(Runner().stream_async(agent, "go"))
        assert error is not None
        (decision,) = decisions(events)
        assert decision["action"] == "fail" and decision["stage"] == "tool_input"
        assert decision.get("error_type") == error_type
        kinds = [e.type for e in events]
        assert kinds.index(RunEventType.GUARDRAIL_DECISION) < kinds.index(RunEventType.RUN_FAILED)
        assert_no_secret(decision)


def test_decision_record_keeps_only_plain_short_tokens() -> None:
    record = decision_record(
        "tool_output",
        "g",
        "reject",
        info={
            "categories": "fraud",  # a string, not a list: dropped
            "score": float("nan"),
            "cached": "yes",
            "error": "BudgetExceededError",
            "backend": "x" * 200,
        },
        latency_ms=float("inf"),
    )
    assert record == {
        "stage": "tool_output",
        "guardrail": "g",
        "action": "reject",
        "error_type": "BudgetExceededError",
    }
    assert decision_record("input", "g", "fail", info="not a mapping") == {
        "stage": "input",
        "guardrail": "g",
        "action": "fail",
    }


# ---------------------------------------------------------------- tracing


@pytest.fixture
def exporter() -> Iterator[InMemorySpanExporter]:
    memory = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(memory))
    nexura_tracing.instrument(tracer_provider=provider)
    yield memory
    nexura_tracing.uninstrument()


async def test_decisions_are_trace_events_on_the_run_and_tool_spans(
    exporter: InMemorySpanExporter,
) -> None:
    def redact(ctx: Any, output: str) -> GuardrailResult:
        return GuardrailResult.rewritten("[redacted]", leaky_info())

    def refuse(ctx: Any, args: Any) -> GuardrailResult:
        return GuardrailResult.rejected(f"no {SECRET}", leaky_info())

    @tool(input_guardrails=[refuse])
    def send(to: str) -> str:
        """Send."""
        return "sent"

    agent = Agent(
        name="A",
        instructions="i",
        model=FakeModel(responses=[call("send", to=SECRET), f"final {SECRET}"]),
        tools=[send],
        output_guardrails=[redact],
    )
    result = await Runner().run_async(agent, f"hi {SECRET}")
    spans = exporter.get_finished_spans()
    found = {
        span.name: [e for e in span.events if e.name == "nexura.guardrail.decision"]
        for span in spans
    }
    (run_event,) = found["invoke_agent A"]
    (tool_event,) = found["execute_tool send"]
    on_run: dict[str, Any] = dict(run_event.attributes or {})
    on_tool: dict[str, Any] = dict(tool_event.attributes or {})
    assert on_run["nexura.guardrail.stage"] == "output"
    assert on_run["nexura.guardrail.action"] == "scrub"
    assert on_run["nexura.guardrail.check"] == "redact"
    assert on_run["nexura.guardrail.source"] == "live"
    assert tuple(on_run["nexura.guardrail.categories"]) == ("fraud", "privacy")
    assert on_tool["nexura.guardrail.stage"] == "tool_input"
    assert on_tool["nexura.guardrail.action"] == "reject"
    assert on_tool["gen_ai.tool.name"] == "send"
    tool_span = next(span for span in spans if span.name == "execute_tool send")
    assert tool_span.attributes is not None
    assert on_tool["gen_ai.tool.call.id"] == tool_span.attributes["gen_ai.tool.call.id"]
    expected_id = "call-" + hmac.digest(result.run_id.encode(), b"c1", "sha256").hex()[:16]
    assert on_tool["gen_ai.tool.call.id"] == expected_id
    assert_no_secret([on_run, on_tool])
