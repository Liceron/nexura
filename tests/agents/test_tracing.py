"""Opt-in OpenTelemetry tracing for agents and teams (GenAI semantic conventions)."""

from __future__ import annotations

import hmac
import subprocess
import sys
import textwrap
from collections.abc import Iterator
from typing import Any

import pytest
from nexura_agents import (
    Agent,
    FakeModel,
    ParallelAgent,
    Runner,
    SequentialAgent,
    ToolCall,
    Usage,
    tool,
)
from nexura_agents import tracing as nexura_tracing
from nexura_agents.errors import ModelRetry
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

USAGE = Usage(prompt_tokens=7, completion_tokens=3, total_tokens=10, reasoning_tokens=2)


@pytest.fixture
def exporter() -> Iterator[InMemorySpanExporter]:
    memory = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(memory))
    nexura_tracing.instrument(tracer_provider=provider)
    yield memory
    nexura_tracing.uninstrument()


def by_name(exporter: InMemorySpanExporter, prefix: str) -> list[ReadableSpan]:
    return [s for s in exporter.get_finished_spans() if s.name.startswith(prefix)]


def one(exporter: InMemorySpanExporter, prefix: str) -> ReadableSpan:
    found = by_name(exporter, prefix)
    assert len(found) == 1, [s.name for s in exporter.get_finished_spans()]
    return found[0]


def parent_id(span: ReadableSpan) -> int | None:
    return span.parent.span_id if span.parent else None


def calls(*names: str) -> list[ToolCall]:
    return [ToolCall(id=f"c{i}", name=n, arguments={"x": i}) for i, n in enumerate(names)]


def make_agent(*replies: Any, tools: list[Any] | None = None, name: str = "Helper") -> Agent:
    model = FakeModel(responses=list(replies), default_usage=USAGE)
    return Agent(name=name, instructions="Be brief.", model=model, tools=tools or [])


@tool
def lookup(x: int) -> str:
    return f"found {x}"


async def test_agent_chat_and_tool_spans_nest(exporter: InMemorySpanExporter) -> None:
    agent = make_agent(calls("lookup"), "all done", tools=[lookup])
    result = await Runner().run_async(agent, "hi")
    assert result.status == "completed"

    run = one(exporter, "invoke_agent")
    assert run.name == "invoke_agent Helper"
    assert run.attributes is not None
    assert run.attributes["gen_ai.operation.name"] == "invoke_agent"
    assert run.attributes["gen_ai.agent.name"] == "Helper"
    assert run.attributes["nexura.run_id"] == result.run_id
    assert run.attributes["nexura.run.status"] == "completed"
    assert run.attributes["gen_ai.usage.input_tokens"] == 14
    assert run.attributes["gen_ai.usage.output_tokens"] == 6
    assert run.attributes["nexura.usage.reasoning_tokens"] == 4
    assert parent_id(run) is None

    chats = by_name(exporter, "chat ")
    assert len(chats) == 2
    for chat in chats:
        assert parent_id(chat) == run.context.span_id
        assert chat.attributes is not None
        assert chat.attributes["gen_ai.operation.name"] == "chat"
        assert chat.attributes["gen_ai.request.model"] == "FakeModel"
        assert chat.attributes["gen_ai.usage.input_tokens"] == 7
        assert chat.attributes["gen_ai.usage.output_tokens"] == 3

    call = one(exporter, "execute_tool")
    assert call.name == "execute_tool lookup"
    assert call.attributes is not None
    assert call.attributes["gen_ai.operation.name"] == "execute_tool"
    assert call.attributes["gen_ai.tool.name"] == "lookup"
    expected_id = "call-" + hmac.digest(result.run_id.encode(), b"c0", "sha256").hex()[:16]
    assert call.attributes["gen_ai.tool.call.id"] == expected_id
    assert parent_id(call) == run.context.span_id
    assert call.status.status_code is not StatusCode.ERROR


async def test_finish_reason_is_recorded_when_known(exporter: InMemorySpanExporter) -> None:
    from nexura_agents.types import Message, ModelResponse

    reply = ModelResponse(
        message=Message(role="assistant", content="ok"), usage=USAGE, finish_reason="stop"
    )
    await Runner().run_async(make_agent(reply), "hi")
    chat = one(exporter, "chat ")
    assert chat.attributes is not None
    assert tuple(chat.attributes["gen_ai.response.finish_reasons"]) == ("stop",)


async def test_failed_run_marks_error_and_records_exception(
    exporter: InMemorySpanExporter,
) -> None:
    @tool
    def boom(x: int) -> str:
        raise RuntimeError("kaput")

    agent = make_agent(calls("boom"), "unreachable", tools=[boom])
    with pytest.raises(Exception, match="kaput"):
        await Runner().run_async(agent, "hi")
    run = one(exporter, "invoke_agent")
    assert run.status.status_code is StatusCode.ERROR
    assert run.attributes is not None
    assert run.attributes["nexura.run.status"] == "failed"
    assert any(e.name == "exception" for e in run.events)
    call = one(exporter, "execute_tool")
    assert call.status.status_code is StatusCode.ERROR
    assert any(e.name == "exception" for e in call.events)


async def test_model_failure_marks_chat_span(exporter: InMemorySpanExporter) -> None:
    class Broken(FakeModel):
        async def generate(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("provider down")

    agent = Agent(name="Helper", instructions="", model=Broken())
    with pytest.raises(Exception, match="provider down"):
        await Runner().run_async(agent, "hi")
    chat = one(exporter, "chat ")
    assert chat.status.status_code is StatusCode.ERROR
    assert chat.attributes is not None and chat.attributes["gen_ai.request.model"] == "Broken"
    assert one(exporter, "invoke_agent").status.status_code is StatusCode.ERROR


async def test_model_retry_is_not_an_error(exporter: InMemorySpanExporter) -> None:
    attempts: list[int] = []

    @tool
    def picky(x: int) -> str:
        attempts.append(x)
        if len(attempts) == 1:
            raise ModelRetry("try again")
        return "ok"

    agent = make_agent(calls("picky"), calls("picky"), "done", tools=[picky])
    await Runner().run_async(agent, "hi")
    spans = by_name(exporter, "execute_tool")
    assert len(spans) == 2
    assert all(s.status.status_code is not StatusCode.ERROR for s in spans)
    assert any(s.attributes and s.attributes.get("nexura.tool.retry_reason") for s in spans)


async def test_approval_pause_is_not_an_error_and_resume_gets_its_own_span(
    exporter: InMemorySpanExporter,
) -> None:
    @tool(requires_approval=True)
    def wire(x: int) -> str:
        return "sent"

    agent = make_agent(calls("wire"), "wired", tools=[wire])
    paused = await Runner().run_async(agent, "pay")
    assert paused.status == "paused" and paused.interruption is not None

    run = one(exporter, "invoke_agent")
    assert run.attributes is not None
    assert run.attributes["nexura.run.status"] == "paused"
    assert run.status.status_code is not StatusCode.ERROR
    pending = one(exporter, "execute_tool")
    assert pending.attributes is not None
    assert pending.attributes["nexura.tool.pending_approval"] is True
    assert pending.status.status_code is not StatusCode.ERROR
    assert parent_id(pending) == run.context.span_id

    exporter.clear()
    done = await Runner().resume_async(agent, paused.interruption, approve=True)
    assert done.status == "completed"
    resumed = one(exporter, "invoke_agent")
    assert resumed.attributes is not None
    assert resumed.attributes["nexura.run_id"] == paused.run_id
    assert resumed.attributes["nexura.run.status"] == "completed"
    assert resumed.attributes["nexura.run.resumed"] is True
    executed = one(exporter, "execute_tool")
    assert executed.attributes is not None
    assert "nexura.tool.pending_approval" not in executed.attributes
    assert parent_id(executed) == resumed.context.span_id


async def test_streaming_run_is_traced(exporter: InMemorySpanExporter) -> None:
    agent = make_agent("streamed")
    events = [e async for e in Runner().stream_async(agent, "hi")]
    assert events[-1].type.name == "RUN_COMPLETED"
    run = one(exporter, "invoke_agent")
    assert parent_id(one(exporter, "chat ")) == run.context.span_id


async def test_parallel_tool_calls_keep_the_run_as_parent(
    exporter: InMemorySpanExporter,
) -> None:
    agent = make_agent(calls("lookup", "lookup", "lookup"), "done", tools=[lookup])
    result = await Runner().run_async(agent, "hi")
    run = one(exporter, "invoke_agent")
    tool_spans = by_name(exporter, "execute_tool")
    assert len(tool_spans) == 3
    assert {parent_id(s) for s in tool_spans} == {run.context.span_id}
    assert {s.attributes["gen_ai.tool.call.id"] for s in tool_spans if s.attributes} == {
        "call-" + hmac.digest(result.run_id.encode(), call_id.encode(), "sha256").hex()[:16]
        for call_id in ("c0", "c1", "c2")
    }


async def test_agent_used_as_a_tool_nests_under_the_tool_span(
    exporter: InMemorySpanExporter,
) -> None:
    inner = make_agent("inner answer", name="Inner")
    outer = make_agent(
        [ToolCall(id="t1", name="ask_inner", arguments={"request": "q"})],
        "outer done",
        name="Outer",
        tools=[inner.as_tool(name="ask_inner", description="Ask the inner agent")],
    )
    await Runner().run_async(outer, "hi")
    outer_run = one(exporter, "invoke_agent Outer")
    tool_span = one(exporter, "execute_tool ask_inner")
    inner_run = one(exporter, "invoke_agent Inner")
    assert parent_id(tool_span) == outer_run.context.span_id
    assert parent_id(inner_run) == tool_span.context.span_id


async def test_user_span_is_the_parent_of_the_run() -> None:
    memory = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(memory))
    nexura_tracing.instrument(tracer_provider=provider)
    try:
        with provider.get_tracer("test").start_as_current_span("request") as root:
            await Runner().run_async(make_agent("hi"), "hi")
    finally:
        nexura_tracing.uninstrument()
    run = one(memory, "invoke_agent")
    assert parent_id(run) == root.get_span_context().span_id


async def test_team_span_wraps_member_agent_spans(exporter: InMemorySpanExporter) -> None:
    team = SequentialAgent(
        "pipeline", [make_agent("one", name="A"), make_agent("two", name="B")], runner=Runner()
    )
    result = await team.run_async("go")
    assert result.status == "completed"
    team_span = one(exporter, "invoke_team")
    assert team_span.name == "invoke_team pipeline"
    assert team_span.attributes is not None
    assert team_span.attributes["nexura.team.status"] == "completed"
    agents = by_name(exporter, "invoke_agent")
    assert len(agents) == 2
    assert {parent_id(a) for a in agents} == {team_span.context.span_id}


async def test_parallel_team_members_share_the_team_parent(
    exporter: InMemorySpanExporter,
) -> None:
    team = ParallelAgent(
        "fan", [make_agent("a", name="A"), make_agent("b", name="B"), make_agent("c", name="C")]
    )
    await team.run_async("go")
    team_span = one(exporter, "invoke_team")
    agents = by_name(exporter, "invoke_agent")
    assert len(agents) == 3
    assert {parent_id(a) for a in agents} == {team_span.context.span_id}
    for agent_span in agents:
        children = [
            c for c in by_name(exporter, "chat ") if parent_id(c) == agent_span.context.span_id
        ]
        assert len(children) == 1


async def test_nested_team_spans_and_pause_resume(exporter: InMemorySpanExporter) -> None:
    @tool(requires_approval=True)
    def wire(x: int) -> str:
        return "sent"

    inner = SequentialAgent("inner", [make_agent(calls("wire"), "ok", tools=[wire], name="Payer")])
    outer = SequentialAgent("outer", [inner])
    paused = await outer.run_async("go")
    assert paused.status == "paused" and paused.interruption is not None
    outer_span = one(exporter, "invoke_team outer")
    inner_span = one(exporter, "invoke_team inner")
    assert outer_span.attributes is not None
    assert outer_span.attributes["nexura.team.status"] == "paused"
    assert parent_id(inner_span) == outer_span.context.span_id
    assert parent_id(one(exporter, "invoke_agent")) == inner_span.context.span_id

    exporter.clear()
    done = await outer.resume_async(paused.interruption, approve=True)
    assert done.status == "completed"
    assert one(exporter, "invoke_team outer").attributes["nexura.run.resumed"] is True  # type: ignore[index]


# ---------------------------------------------------------------------- content capture


async def test_content_is_not_recorded_by_default(exporter: InMemorySpanExporter) -> None:
    agent = make_agent(calls("lookup"), "secret answer", tools=[lookup])
    await Runner().run_async(agent, "my private prompt")
    for span in exporter.get_finished_spans():
        assert not [e for e in span.events if e.name != "exception"], span.name


async def test_capture_content_records_redacted_truncated_events() -> None:
    memory = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(memory))
    nexura_tracing.instrument(tracer_provider=provider, capture_content=True)
    try:

        @tool
        def fetch(x: int, api_key: str) -> str:
            return "token=abc123 and " + "z" * 10_000

        agent = make_agent(
            [
                ToolCall(
                    id="c1", name="fetch", arguments={"x": 1, "api_key": "sk-supersecret1234567890"}
                )
            ],
            "final answer",
            tools=[fetch],
        )
        await Runner().run_async(agent, "use Bearer abcdef123456 please")
    finally:
        nexura_tracing.uninstrument()

    chats = by_name(memory, "chat ")
    prompt = next(e for e in chats[0].events if e.name == "gen_ai.content.prompt")
    assert prompt.attributes is not None
    text = str(prompt.attributes["gen_ai.prompt"])
    assert "use Bearer [REDACTED] please" in text and "abcdef123456" not in text
    completion = next(e for e in chats[-1].events if e.name == "gen_ai.content.completion")
    assert completion.attributes is not None
    assert "final answer" in str(completion.attributes["gen_ai.completion"])

    call = one(memory, "execute_tool")
    args = next(e for e in call.events if e.name == "gen_ai.tool.call.arguments")
    assert args.attributes is not None
    assert "sk-supersecret" not in str(args.attributes["gen_ai.tool.call.arguments"])
    assert "[REDACTED]" in str(args.attributes["gen_ai.tool.call.arguments"])
    result = next(e for e in call.events if e.name == "gen_ai.tool.call.result")
    assert result.attributes is not None
    value = str(result.attributes["gen_ai.tool.call.result"])
    assert "token=[REDACTED]" in value and "abc123" not in value
    assert value.endswith("[TRUNCATED]") and len(value) < 4100


# ---------------------------------------------------------------------- lifecycle


async def test_uninstrument_stops_spans() -> None:
    memory = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(memory))
    nexura_tracing.instrument(tracer_provider=provider)
    await Runner().run_async(make_agent("one"), "hi")
    emitted = len(memory.get_finished_spans())
    assert emitted > 0
    nexura_tracing.uninstrument()
    assert not nexura_tracing.is_instrumented()
    await Runner().run_async(make_agent("two"), "hi")
    assert len(memory.get_finished_spans()) == emitted


async def test_instrument_is_idempotent_and_never_duplicates_spans() -> None:
    memory = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(memory))
    try:
        nexura_tracing.instrument(tracer_provider=provider)
        nexura_tracing.instrument(tracer_provider=provider)
        assert nexura_tracing.is_instrumented()
        await Runner().run_async(make_agent("one"), "hi")
        assert len(by_name(memory, "invoke_agent")) == 1
        assert len(by_name(memory, "chat ")) == 1
    finally:
        nexura_tracing.uninstrument()
        nexura_tracing.uninstrument()  # also safe to repeat


async def test_instrument_does_not_set_the_global_provider() -> None:
    from opentelemetry import trace

    before = trace.get_tracer_provider()
    nexura_tracing.instrument(tracer_provider=TracerProvider())
    try:
        assert trace.get_tracer_provider() is before
    finally:
        nexura_tracing.uninstrument()


def run_python(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", "-c", textwrap.dedent(code)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_not_instrumented_path_imports_nothing_from_opentelemetry() -> None:
    proc = run_python(
        """
        import sys
        from nexura_agents import Agent, FakeModel, Runner
        agent = Agent(name="A", instructions="", model=FakeModel(responses=["hi"]))
        assert Runner().run(agent, "x").final_output == "hi"
        import nexura_agents.tracing as t
        assert not t.is_instrumented()
        leaked = sorted(m for m in sys.modules if m.split(".")[0] == "opentelemetry")
        assert not leaked, leaked
        print("clean")
        """
    )
    assert proc.returncode == 0, proc.stderr
    assert "clean" in proc.stdout


def test_instrument_without_opentelemetry_raises_a_helpful_import_error() -> None:
    proc = run_python(
        """
        import sys
        sys.modules["opentelemetry"] = None  # makes `import opentelemetry` fail
        from nexura_agents.tracing import instrument
        try:
            instrument()
        except ImportError as exc:
            assert 'nexura-agents[otel]' in str(exc), exc
            print("helpful")
        """
    )
    assert proc.returncode == 0, proc.stderr
    assert "helpful" in proc.stdout


async def test_payload_not_built_when_tracing_off(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}
    orig = nexura_tracing.messages_payload

    def spy(m: Any) -> Any:
        calls["n"] += 1
        return orig(m)

    monkeypatch.setattr(nexura_tracing, "messages_payload", spy)
    assert not nexura_tracing.is_instrumented()
    await Runner().run_async(
        Agent(name="A", instructions="x", model=FakeModel(responses=["hi"])), "hello"
    )
    assert calls["n"] == 0


async def test_payload_not_built_when_capture_content_off(
    exporter: InMemorySpanExporter, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"n": 0}
    orig = nexura_tracing.messages_payload

    def spy(m: Any) -> Any:
        calls["n"] += 1
        return orig(m)

    monkeypatch.setattr(nexura_tracing, "messages_payload", spy)
    await Runner().run_async(
        Agent(name="A", instructions="x", model=FakeModel(responses=["hi"])), "hello"
    )
    assert calls["n"] == 0


def test_redact_masks_secrets_in_json_text() -> None:
    out = nexura_tracing.redact(
        '{"password": "hunter2hunter2", "api_key": "abcd1234efgh", "t": "two words"}'
    )
    assert "hunter2" not in out and "abcd1234efgh" not in out
    assert "two words" in out
    assert "hunter2" not in nexura_tracing.redact('{"password": "pass word x"}')
    assert "pass word" not in nexura_tracing.redact('{"password": "pass word x"}')
    assert "abc" not in nexura_tracing.redact("token=abc")
