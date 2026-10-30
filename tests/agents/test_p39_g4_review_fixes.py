# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""P39 G4 review fixes: no unchecked reply text reaches a consumer, a store or a trace.

The independent review's repros as tests: each asserted a
leak; here each asserts the fixed behaviour. Offline only: FakeModel (local fake models).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator, Sequence
from typing import Any

import pytest
from nexura_agents import (
    Agent,
    AgentHooks,
    FakeModel,
    GuardrailResult,
    GuardrailTripwireTriggered,
    OutputValidationError,
    RunEvent,
    RunEventType,
    Runner,
    StreamGuard,
    ToolCall,
    Usage,
    UsageLimits,
    redact_output_pii,
    set_default_hooks,
    tool,
)
from nexura_agents.durable import MemoryCheckpointStore
from nexura_agents.errors import SDKValidationError, UsageLimitExceededError
from nexura_agents.guardrails_builtin import detect_pii
from nexura_agents.session import MemorySessionStore
from nexura_agents.stream_guard import MIN_OVERLAP_CHARS, TextGate, visible_length
from nexura_agents.types import Message, ModelResponse
from pydantic import BaseModel

T = RunEventType
SECRET = "SECRET-TOKEN-XYZ"
KEY = "sk-" + "A1b2C3d4E5" * 5 + "ZZZZTAI"  # a fake key shaped like an API key (60 chars)


@pytest.fixture(autouse=True)
def _no_default_hooks() -> Iterator[None]:
    from nexura_agents import policy

    policy._unlock_for_tests()
    set_default_hooks(None)
    yield
    policy._unlock_for_tests()
    set_default_hooks(None)


async def collect(stream: AsyncIterator[RunEvent]) -> tuple[list[RunEvent], BaseException | None]:
    events: list[RunEvent] = []
    try:
        async for event in stream:
            events.append(event)
    except Exception as exc:
        return events, exc
    return events, None


def shown(events: Sequence[RunEvent]) -> str:
    return "".join(e.data["text"] for e in events if e.type == T.TEXT_DELTA)


def finals(events: Sequence[RunEvent]) -> list[str]:
    return [e.data["output"] for e in events if e.type == T.FINAL_OUTPUT]


def with_secret(events: Sequence[RunEvent], secret: str) -> list[str]:
    return [e.type.value for e in events if secret in json.dumps(e.data, default=str)]


def block_secret(ctx: Any, output: Any) -> GuardrailResult:
    return GuardrailResult(tripwire=SECRET in str(output), info={"reason": "secret"})


def luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def card_with_luhn_prefix() -> str:
    """A valid card whose 13-digit prefix is also Luhn-valid (the review's 4111111111119007)."""
    for tail in range(10_000):
        card = f"411111111111{tail:04d}"
        if luhn_ok(card) and luhn_ok(card[:13]):
            return card
    raise AssertionError("no such card")


# ------------------------------------------------------------------------------- B1


@pytest.mark.parametrize("offset", [-45, -40, -30, -20, -10, -3, 0, 3, 10])
async def test_b1_key_across_a_window_end_is_never_shown_or_stored(offset: int) -> None:
    prefix = 400 + offset  # the key starts near the end of the first default window
    reply = "p" * (prefix - 1) + " " + KEY + " and the rest of the reply. " + "q" * 300
    store = MemorySessionStore()
    agent = Agent(
        name="A",
        instructions="i",
        model=FakeModel([reply], stream=True, chunk_size=4),
        output_guardrails=[redact_output_pii(["secret"])],
        stream_guard="chunked",  # defaults: 400 / 120
    )
    events, error = await collect(
        Runner(session_store=store).stream_async(agent, "go", session_id="s", stream_text=True)
    )
    assert error is None
    text = shown(events)
    for piece in (KEY, KEY[-12:], KEY[-25:], KEY[10:30]):
        assert piece not in text
        assert all(piece not in f for f in finals(events))
    snapshot = await store.get("s")
    assert snapshot is not None
    assert all(KEY[-12:] not in (m.content or "") for m in snapshot.messages)
    assert text == finals(events)[0] == reply.replace(KEY, "[REDACTED:secret]")


@pytest.mark.parametrize("offset", [-16, -13, -10, -5, -1, 0, 1, 5])
async def test_b1_card_with_a_luhn_valid_prefix_across_a_window_end(offset: int) -> None:
    card = card_with_luhn_prefix()
    assert [f.kind for f in detect_pii(card[:13])] == ["card"]  # the trap: a valid prefix
    reply = "p" * (399 + offset) + " " + card + " is on file. " + "q" * 300
    agent = Agent(
        name="A",
        instructions="i",
        model=FakeModel([reply], stream=True, chunk_size=4),
        output_guardrails=[redact_output_pii(["card"])],
        stream_guard="chunked",
    )
    events, error = await collect(Runner().stream_async(agent, "go", stream_text=True))
    assert error is None
    text = shown(events)
    digits = "".join(ch for ch in text if ch.isdigit())
    assert digits == "", text[max(0, text.find("[") - 4) : text.find("[") + 30]
    assert text == finals(events)[0] == reply.replace(card, "[REDACTED:card]")


async def test_b1_email_across_a_window_end() -> None:
    email = "john.doe.customer@example-corp.com"
    reply = "p" * 384 + " " + email + " and the rest of the reply. " + "q" * 300
    agent = Agent(
        name="A",
        instructions="i",
        model=FakeModel([reply], stream=True, chunk_size=4),
        output_guardrails=[redact_output_pii(["email"])],
        stream_guard="chunked",
    )
    events, error = await collect(Runner().stream_async(agent, "go", stream_text=True))
    assert error is None
    assert "example-corp" not in shown(events) and "corp.com" not in shown(events)


async def test_b1_final_check_runs_on_the_raw_reply() -> None:
    """The whole-answer check sees the raw reply, not text already rewritten by windows."""
    seen: list[str] = []

    def record(ctx: Any, output: Any) -> GuardrailResult:
        seen.append(str(output))
        text = str(output)
        return (
            GuardrailResult.rewritten(text.replace(SECRET, "[x]"))
            if SECRET in text
            else (GuardrailResult.passed())
        )

    reply = "Hello " * 20 + SECRET + " and more " * 20
    agent = Agent(
        name="A",
        instructions="i",
        model=FakeModel([reply], stream=True, chunk_size=5),
        output_guardrails=[record],
        stream_guard=StreamGuard(mode="chunked", chunk_chars=40, overlap_chars=40),
    )
    events, error = await collect(Runner().stream_async(agent, "go", stream_text=True))
    assert error is None
    assert seen[-1] == reply  # the final pass got the raw answer
    assert all("[x]" not in window for window in seen)  # every window was raw too


def test_b1_gate_holds_back_from_a_rewrite_that_reaches_the_window_end() -> None:
    gate = TextGate("chunked", StreamGuard(mode="chunked", chunk_chars=10, overlap_chars=32))
    gate.add("a" * 40 + "sk-partial")
    window = gate.raw[0:50]
    checked = "a" * 40 + "[REDACTED]"
    show = gate.accept(0, 50, checked)
    assert show == "a" * 18  # raw text before the held 32 visible characters
    assert gate.raw[gate.done :] == "a" * 22 + "sk-partial"  # held back RAW, not rewritten
    del window


# ------------------------------------------------------------------------------- M1


def turn_with_text(text: str) -> ModelResponse:
    return ModelResponse(
        message=Message(
            role="assistant",
            content=text,
            tool_calls=[ToolCall(id="c1", name="lookup", arguments={"order": "A1"})],
        )
    )


@tool
def lookup(order: str) -> str:
    """Look up an order."""
    return "shipped"


@pytest.mark.parametrize("mode", ["auto", "hold", "chunked"])
@pytest.mark.parametrize("stream_text", [True, False])
async def test_m1_text_before_tool_calls_is_checked_before_it_is_stored(
    mode: str, stream_text: bool
) -> None:
    checkpoints = MemoryCheckpointStore()
    seen_in_checkpoint: list[str] = []

    @tool
    async def lookup_and_peek(order: str) -> str:
        """Look up an order."""
        seen_in_checkpoint.append(repr(checkpoints.__dict__))
        return "shipped"

    turn = ModelResponse(
        message=Message(
            role="assistant",
            content=f"Using the API key {KEY} to look it up.",
            tool_calls=[ToolCall(id="c1", name="lookup_and_peek", arguments={"order": "A1"})],
        )
    )
    sessions = MemorySessionStore()
    agent = Agent(
        name="A",
        instructions="i",
        model=FakeModel([turn, "It shipped."], stream=True, chunk_size=4),
        tools=[lookup_and_peek],
        output_guardrails=[redact_output_pii(["secret"])],
        stream_guard=StreamGuard(mode=mode, chunk_chars=8, overlap_chars=64),  # type: ignore[arg-type]
    )
    events, error = await collect(
        Runner(session_store=sessions, checkpoint_store=checkpoints).stream_async(
            agent, "go", session_id="s", stream_text=stream_text
        )
    )
    assert error is None
    assert with_secret(events, KEY) == []
    assert seen_in_checkpoint and KEY not in seen_in_checkpoint[0]
    snapshot = await sessions.get("s")
    assert snapshot is not None
    assert all(KEY not in (m.content or "") for m in snapshot.messages)
    assistant = [m for m in snapshot.messages if m.role == "assistant" and m.tool_calls]
    assert assistant[0].content == "Using the API key [REDACTED:secret] to look it up."
    if mode == "chunked" and stream_text:
        assert "Using the API key [REDACTED:secret] to look it up." in shown(events)


async def test_m1_a_trip_on_text_before_tool_calls_stops_the_run_before_any_tool() -> None:
    ran: list[str] = []

    @tool
    def charge(order: str) -> str:
        """Charge."""
        ran.append(order)
        return "charged"

    turn = ModelResponse(
        message=Message(
            role="assistant",
            content=f"Charging with {SECRET}.",
            tool_calls=[ToolCall(id="c1", name="charge", arguments={"order": "A1"})],
        )
    )
    store = MemorySessionStore()
    agent = Agent(
        name="A",
        instructions="i",
        model=FakeModel([turn, "done"], stream=True),
        tools=[charge],
        output_guardrails=[block_secret],
    )
    events, error = await collect(
        Runner(session_store=store).stream_async(agent, "go", session_id="s", stream_text=True)
    )
    assert isinstance(error, GuardrailTripwireTriggered)
    assert ran == [] and await store.get("s") is None
    tripped = next(e for e in events if e.type == T.GUARDRAIL_TRIPPED).data
    assert tripped["phase"] == "chunk" and tripped["shown"] is False
    assert with_secret(events, SECRET) == []


class Refund(BaseModel):
    n: int


async def test_m1_typed_agent_text_before_tool_calls_is_withheld() -> None:
    def ok(ctx: Any, output: Any) -> GuardrailResult:
        return GuardrailResult.passed()

    store = MemorySessionStore()
    agent = Agent(
        name="A",
        instructions="i",
        model=FakeModel([turn_with_text(f"key {SECRET}"), '{"n": 1}'], stream=True),
        tools=[lookup],
        output_type=Refund,
        output_guardrails=[ok],
    )
    events, error = await collect(
        Runner(session_store=store).stream_async(agent, "go", session_id="s", stream_text=True)
    )
    assert error is None
    snapshot = await store.get("s")
    assert snapshot is not None
    assert all(SECRET not in (m.content or "") for m in snapshot.messages)
    assert with_secret(events, SECRET) == []


async def test_m1_unguarded_agents_keep_text_before_tool_calls() -> None:
    agent = Agent(
        name="A",
        instructions="i",
        model=FakeModel([turn_with_text(f"key {SECRET}"), "ok"]),
        tools=[lookup],
    )
    result = await Runner().run_async(agent, "go")
    assert any(m.content == f"key {SECRET}" for m in result.messages)


# ------------------------------------------------------------------------------- M2


@pytest.mark.parametrize("behavior", ["stop_on_first_tool", "stop_at"])
@pytest.mark.parametrize("guard", ["block", "rewrite"])
async def test_m2_a_final_tool_result_is_not_echoed_before_the_output_guardrails(
    behavior: str, guard: str
) -> None:
    from nexura_agents import StopAtTools

    @tool
    def fetch(order: str) -> str:
        """Fetch."""
        return f"result {SECRET}"

    def check(ctx: Any, output: Any) -> GuardrailResult:
        if guard == "block":
            return block_secret(ctx, output)
        return GuardrailResult.rewritten(str(output).replace(SECRET, "[x]"))

    agent = Agent(
        name="A",
        instructions="i",
        model=FakeModel(
            [[ToolCall(id="c1", name="fetch", arguments={"order": "A1"})]], stream=True
        ),
        tools=[fetch],
        output_guardrails=[check],
        tool_use_behavior="stop_on_first_tool"
        if behavior == "stop_on_first_tool"
        else StopAtTools(["fetch"]),
    )
    events, error = await collect(Runner().stream_async(agent, "go", stream_text=True))
    completed = next(e for e in events if e.type == T.TOOL_COMPLETED).data
    assert "result" not in completed and completed["result_withheld"] is True
    if guard == "block":
        assert isinstance(error, GuardrailTripwireTriggered)
        assert with_secret(events, SECRET) == []
    else:
        assert error is None
        assert finals(events) == ["result [x]"]
        assert [k for k in with_secret(events, SECRET) if k != "run_completed"] == []


async def test_m2_results_that_cannot_end_the_run_are_still_reported() -> None:
    @tool
    def fetch(order: str) -> str:
        """Fetch."""
        return "plain"

    agent = Agent(
        name="A",
        instructions="i",
        model=FakeModel([[ToolCall(id="c1", name="fetch", arguments={"order": "A1"})], "ok"]),
        tools=[fetch],
        output_guardrails=[block_secret],
    )
    result = await Runner().run_async(agent, "go")
    completed = next(e for e in result.events if e.type == T.TOOL_COMPLETED).data
    assert completed["result"] == "plain"


# ------------------------------------------------------------------------------- M3


class Out(BaseModel):
    n: int


async def test_m3_retry_and_failure_never_echo_the_unchecked_typed_reply() -> None:
    bad = json.dumps({"n": SECRET})
    agent = Agent(
        name="A",
        instructions="i",
        model=FakeModel([bad, bad], stream=True, chunk_size=5),
        output_type=Out,
        output_guardrails=[block_secret],
        output_retries=1,
    )
    store = MemorySessionStore()
    events, error = await collect(
        Runner(session_store=store).stream_async(agent, "go", session_id="s", stream_text=True)
    )
    assert isinstance(error, OutputValidationError)
    assert SECRET not in str(error) and error.raw_output is None
    assert with_secret(events, SECRET) == []
    retry = next(e for e in events if e.type == T.RETRY).data["error"]
    assert (
        "n: Input should be a valid integer" in retry
        and "input" not in retry.lower().split("input should")[0]
    )


async def test_m3_the_retried_reply_is_not_kept_in_the_transcript() -> None:
    bad = json.dumps({"n": SECRET})
    seen: list[list[Message]] = []

    def capture(messages: list[Message], tools: Any) -> str:
        seen.append(list(messages))
        return bad if len(seen) == 1 else '{"n": 3}'

    def ok(ctx: Any, output: Any) -> GuardrailResult:
        return GuardrailResult.passed()

    agent = Agent(
        name="A",
        instructions="i",
        model=FakeModel(handler=capture),
        output_type=Out,
        output_guardrails=[ok],
        output_retries=1,
    )
    result = await Runner().run_async(agent, "go")
    assert result.output == Out(n=3)
    assert all(SECRET not in (m.content or "") for m in seen[1])
    assert all(SECRET not in (m.content or "") for m in result.messages)


async def test_m3_dict_schema_errors_leave_values_out() -> None:
    schema = {
        "type": "object",
        "properties": {"kind": {"enum": ["a", "b"]}, "n": {"type": "integer", "maximum": 3}},
        "additionalProperties": False,
    }
    agent = Agent(
        name="A",
        instructions="i",
        model=FakeModel([json.dumps({"kind": SECRET, "n": 987654, SECRET: 1})]),
        output_type=schema,
        output_guardrails=[block_secret],
        output_retries=0,
    )
    events, error = await collect(Runner().stream_async(agent, "go"))
    assert isinstance(error, OutputValidationError)
    assert SECRET not in str(error) and "987654" not in str(error)
    assert with_secret(events, SECRET) == []


async def test_m3_unguarded_agents_keep_raw_output_on_the_error() -> None:
    agent = Agent(
        name="A",
        instructions="i",
        model=FakeModel([json.dumps({"n": "x"})]),
        output_type=Out,
        output_retries=0,
    )
    with pytest.raises(OutputValidationError) as info:
        await Runner().run_async(agent, "go")
    assert info.value.raw_output == '{"n": "x"}'


# ------------------------------------------------------------------------------- M4


async def test_m4_usage_limits_stop_window_checks_before_more_text_is_shown() -> None:
    calls = 0

    def judge(ctx: Any, output: Any) -> GuardrailResult:
        nonlocal calls
        calls += 1
        ctx.add_usage(Usage(prompt_tokens=900, completion_tokens=100, total_tokens=1000))
        return GuardrailResult.passed()

    agent = Agent(
        name="A",
        instructions="i",
        model=FakeModel(["w" * 20_000], stream=True, chunk_size=50),
        output_guardrails=[judge],
        stream_guard="chunked",
    )
    events, error = await collect(
        Runner(usage_limits=UsageLimits(total_tokens_limit=1500)).stream_async(
            agent, "go", stream_text=True
        )
    )
    assert isinstance(error, UsageLimitExceededError)
    assert calls == 2  # the second window went over the cap
    assert len(shown(events)) <= 400  # nothing after the window that went over
    assert events[-1].type == T.RUN_FAILED


async def test_m4_max_windows_holds_the_rest_until_the_final_check() -> None:
    calls = 0

    def count(ctx: Any, output: Any) -> GuardrailResult:
        nonlocal calls
        calls += 1
        return GuardrailResult.passed()

    reply = "v" * 2_000
    agent = Agent(
        name="A",
        instructions="i",
        model=FakeModel([reply], stream=True, chunk_size=10),
        output_guardrails=[count],
        stream_guard=StreamGuard(mode="chunked", chunk_chars=50, overlap_chars=32, max_windows=3),
    )
    events, error = await collect(Runner().stream_async(agent, "go", stream_text=True))
    assert error is None
    assert calls == 3 + 1  # three windows, then the whole answer
    pieces = [e.data["text"] for e in events if e.type == T.TEXT_DELTA]
    assert len(pieces) == 4 and "".join(pieces) == reply
    assert len(pieces[-1]) > 1_000  # the rest arrived only after the final check


def test_m4_m5_stream_guard_validation() -> None:
    for kwargs in (
        {"overlap_chars": 0},
        {"overlap_chars": MIN_OVERLAP_CHARS - 1},
        {"max_windows": 0},
    ):
        with pytest.raises(SDKValidationError):
            StreamGuard(mode="chunked", **kwargs)
    assert StreamGuard(overlap_chars=MIN_OVERLAP_CHARS).overlap_chars == 32


# ------------------------------------------------------------------------------- M5


async def test_m5_invisible_padding_does_not_stretch_a_card_past_the_overlap() -> None:
    zw = "​" * 12
    card = zw.join("4111111111111111")  # 196 code points, 16 visible digits
    reply = "p" * 249 + " " + card + " is on file. " + "q" * 600
    agent = Agent(
        name="A",
        instructions="i",
        model=FakeModel([reply], stream=True, chunk_size=7),
        output_guardrails=[redact_output_pii(["card"])],
        stream_guard="chunked",
    )
    events, error = await collect(Runner().stream_async(agent, "go", stream_text=True))
    digits = "".join(ch for ch in shown(events) if ch.isdigit())
    assert digits == "", (digits, error)


def test_m5_visible_length_ignores_invisible_characters_and_folds_nfkc() -> None:
    assert visible_length("ab​c‍") == 3
    assert visible_length("a   \n  b") == 3  # a whitespace run counts once
    assert visible_length("ﬁ") == 2  # the "fi" ligature folds to two letters
    assert visible_length("é") == 1  # a combining accent adds nothing


# ------------------------------------------------------------------------------- minor


async def test_m1_minor_a_list_attribute_for_output_guardrails_fails_closed() -> None:
    class Policy(AgentHooks):
        output_guardrails = [block_secret]  # type: ignore[assignment]  # the mistake

    agent = Agent(name="A", instructions="i", model=FakeModel([SECRET], stream=True))
    events, error = await collect(
        Runner(hooks=[Policy()]).stream_async(agent, "go", stream_text=True)
    )
    assert isinstance(error, SDKValidationError)
    assert shown(events) == "" and events[-1].type == T.RUN_FAILED


async def test_m2_minor_a_hook_that_adds_guardrails_mid_turn_fails_closed() -> None:
    class Late(AgentHooks):
        def __init__(self) -> None:
            self.asked = 0

        def output_guardrails(self, ctx: Any, agent: Any) -> Sequence[Any]:
            self.asked += 1
            return [] if self.asked == 1 else [block_secret]

    agent = Agent(name="A", instructions="i", model=FakeModel(["blocked words"], stream=True))
    events, error = await collect(
        Runner(hooks=[Late()]).stream_async(agent, "go", stream_text=True)
    )
    assert isinstance(error, GuardrailTripwireTriggered) and error.guardrail == "stream_guard"
    tripped = next(e for e in events if e.type == T.GUARDRAIL_TRIPPED).data
    assert tripped["shown"] is True  # a client must discard what it showed
    assert not finals(events)


async def test_m3_minor_a_raising_output_guardrails_hook_ends_with_run_failed() -> None:
    class Boom(AgentHooks):
        def output_guardrails(self, ctx: Any, agent: Any) -> Sequence[Any]:
            raise RuntimeError("policy store down")

    agent = Agent(name="A", instructions="i", model=FakeModel(["secret"], stream=True))
    events, error = await collect(Runner(hooks=Boom()).stream_async(agent, "go", stream_text=True))
    assert isinstance(error, RuntimeError)
    assert events[-1].type == T.RUN_FAILED and shown(events) == ""


async def test_m5_minor_traces_record_only_checked_text() -> None:
    from nexura_agents import tracing as nexura_tracing
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    def scrub(ctx: Any, output: Any) -> GuardrailResult:
        return GuardrailResult.rewritten(str(output).replace(SECRET, "[x]"))

    memory = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(memory))
    nexura_tracing.instrument(tracer_provider=provider, capture_content=True)
    try:
        agent = Agent(
            name="A",
            instructions="i",
            model=FakeModel([turn_with_text(f"pre {SECRET}"), f"final {SECRET}"]),
            tools=[lookup],
            output_guardrails=[scrub],
        )
        result = await Runner().run_async(agent, "go")
    finally:
        nexura_tracing.uninstrument()
    assert result.output == "final [x]"
    dumped = json.dumps(
        [
            {
                "attrs": dict(span.attributes or {}),
                "events": [dict(e.attributes or {}) for e in span.events],
            }
            for span in memory.get_finished_spans()
        ],
        default=str,
    )
    assert SECRET not in dumped
    assert "final [x]" in dumped  # the checked answer is recorded on the run span
