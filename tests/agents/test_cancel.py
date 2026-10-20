"""Cancelling a run with CancelToken (F32)."""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

import pytest
from nexura_agents import (
    Agent,
    CancelToken,
    FakeModel,
    MemoryCheckpointStore,
    Runner,
    ToolCall,
    tool,
)
from nexura_agents.events import RunEventType
from nexura_agents.types import ModelResponse


def call(name: str, **arguments: Any) -> list[ToolCall]:
    return [ToolCall(id="c1", name=name, arguments=arguments)]


class SlowModel:
    """A model whose call takes a long time (so cancel must interrupt it)."""

    def __init__(self, delay: float = 5.0) -> None:
        self.delay = delay
        self.calls = 0

    async def generate(self, messages: Any, tools: Any = None, **kw: Any) -> ModelResponse:
        from nexura_agents import Message

        self.calls += 1
        await asyncio.sleep(self.delay)
        return ModelResponse(message=Message(role="assistant", content="late"))


async def test_cancel_before_start_returns_a_cancelled_result() -> None:
    token = CancelToken()
    token.cancel("user left")
    model = FakeModel(responses=["never"])
    result = await Runner().run_async(
        Agent(name="A", instructions="i", model=model), "hi", cancel=token
    )
    assert result.status == "cancelled"
    assert model.calls == []
    cancelled = [e for e in result.events if e.type == RunEventType.RUN_CANCELLED]
    assert cancelled[0].data["reason"] == "user left"


async def test_cancel_interrupts_a_long_model_call() -> None:
    token = CancelToken()
    model = SlowModel()
    agent = Agent(name="A", instructions="i", model=model)  # type: ignore[arg-type]
    asyncio.get_running_loop().call_later(0.1, token.cancel)
    started = time.monotonic()
    result = await Runner().run_async(agent, "hi", cancel=token)
    assert time.monotonic() - started < 2
    assert result.status == "cancelled" and model.calls == 1


async def test_cancel_between_steps_after_a_tool() -> None:
    from nexura_agents.hooks import AgentHooks

    token = CancelToken()

    class StopAfterTool(AgentHooks):
        async def after_tool(self, ctx: Any, call: Any, result: Any) -> None:
            token.cancel("enough")

    @tool
    def lookup(q: str) -> str:
        """Lookup."""
        return "found"

    model = FakeModel(responses=[call("lookup", q="x"), "never reached"])
    agent = Agent(name="A", instructions="i", model=model, tools=[lookup])
    result = await Runner(hooks=StopAfterTool()).run_async(agent, "go", cancel=token)
    assert result.status == "cancelled"
    assert len(model.calls) == 1  # the next model call never started
    assert [m.content for m in result.messages if m.role == "tool"] == ["found"]
    assert result.turns_count == 1


async def test_cancel_interrupts_an_async_tool_and_keeps_the_checkpoint() -> None:
    token = CancelToken()
    store = MemoryCheckpointStore()

    @tool
    async def slow(x: str) -> str:
        """Slow."""
        token.cancel()
        await asyncio.sleep(5)
        return "late"

    agent = Agent(
        name="A", instructions="i", model=FakeModel(responses=[call("slow", x="a")]), tools=[slow]
    )
    result = await Runner(checkpoint_store=store).run_async(agent, "go", cancel=token)
    assert result.status == "cancelled"
    assert await store.list_incomplete() == [result.run_id]  # recoverable later


def test_cancel_from_another_thread_with_the_sync_runner() -> None:
    token = CancelToken()
    agent = Agent(name="A", instructions="i", model=SlowModel())  # type: ignore[arg-type]
    timer = threading.Timer(0.1, token.cancel)
    timer.start()
    try:
        result = Runner().run(agent, "hi", cancel=token)
    finally:
        timer.cancel()
    assert result.status == "cancelled"


async def test_streaming_ends_with_run_cancelled() -> None:
    token = CancelToken()
    agent = Agent(name="A", instructions="i", model=SlowModel())  # type: ignore[arg-type]
    asyncio.get_running_loop().call_later(0.1, token.cancel)
    events = [e async for e in Runner().stream_async(agent, "hi", cancel=token)]
    assert events[0].type == RunEventType.RUN_STARTED
    assert events[-1].type == RunEventType.RUN_CANCELLED


async def test_one_token_can_stop_several_runs() -> None:
    token = CancelToken()
    agent = Agent(name="A", instructions="i", model=SlowModel())  # type: ignore[arg-type]
    asyncio.get_running_loop().call_later(0.1, token.cancel)
    results = await asyncio.gather(
        *(Runner().run_async(agent, f"q{i}", cancel=token) for i in range(3))
    )
    assert [r.status for r in results] == ["cancelled"] * 3


async def test_cancelling_the_task_itself_still_raises() -> None:
    agent = Agent(name="A", instructions="i", model=SlowModel())  # type: ignore[arg-type]
    task = asyncio.ensure_future(Runner().run_async(agent, "hi", cancel=CancelToken()))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_uncancelled_token_changes_nothing() -> None:
    result = await Runner().run_async(
        Agent(name="A", instructions="i", model=FakeModel(responses=["ok"])),
        "hi",
        cancel=CancelToken(),
    )
    assert result.status == "completed" and result.output == "ok"


def test_cancel_is_idempotent_and_keeps_the_first_reason() -> None:
    token = CancelToken()
    token.cancel("first")
    token.cancel("second")
    assert token.cancelled and token.reason == "first"


# ---------------------------------------------------------------- review fixes


async def test_cancelling_the_task_also_stops_the_running_tool() -> None:
    finished: list[str] = []

    @tool
    async def slow(x: str) -> str:
        """Slow."""
        await asyncio.sleep(0.3)
        finished.append("finished")
        return "late"

    agent = Agent(
        name="A", instructions="i", model=FakeModel(responses=[call("slow", x="a")]), tools=[slow]
    )
    task = asyncio.ensure_future(Runner().run_async(agent, "go", cancel=CancelToken()))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.5)
    assert finished == []  # the tool did not keep running in the background


async def test_a_hanging_before_tool_hook_is_cancellable() -> None:
    from nexura_agents.hooks import AgentHooks

    token = CancelToken()

    class SlowHook(AgentHooks):
        async def before_tool(self, ctx: Any, call: Any) -> None:
            token.cancel()
            await asyncio.sleep(3)

    @tool
    def lookup(q: str) -> str:
        """Lookup."""
        return "found"

    agent = Agent(
        name="A",
        instructions="i",
        model=FakeModel(responses=[call("lookup", q="x")]),
        tools=[lookup],
    )
    started = time.monotonic()
    result = await Runner(hooks=SlowHook()).run_async(agent, "go", cancel=token)
    assert result.status == "cancelled"
    assert time.monotonic() - started < 1.5


async def test_cancelled_run_span_is_ended_with_status_cancelled() -> None:
    from nexura_agents import tracing as nexura_tracing
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    memory = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(memory))
    nexura_tracing.instrument(tracer_provider=provider)
    try:
        token = CancelToken()

        @tool
        async def slow(x: str) -> str:
            """Slow."""
            token.cancel()
            await asyncio.sleep(5)
            return "late"

        agent = Agent(
            name="A",
            instructions="i",
            model=FakeModel(responses=[call("slow", x="a")]),
            tools=[slow],
        )
        result = await Runner().run_async(agent, "go", cancel=token)
    finally:
        nexura_tracing.uninstrument()
    assert result.status == "cancelled"
    spans = [s for s in memory.get_finished_spans() if s.name.startswith("invoke_agent")]
    assert spans and spans[0].attributes is not None
    assert spans[0].attributes["nexura.run.status"] == "cancelled"


# ---------------------------------------------------------------- F36: resume and recover


async def test_cancel_applies_to_resume() -> None:
    @tool(requires_approval=True)
    async def charge(amount: int) -> str:
        """Charge."""
        await asyncio.sleep(5)
        return "charged"

    agent = Agent(
        name="A",
        instructions="i",
        model=FakeModel(responses=[call("charge", amount=5)]),
        tools=[charge],
    )
    runner = Runner()
    paused = await runner.run_async(agent, "go")
    assert paused.interruption is not None
    token = CancelToken()
    asyncio.get_running_loop().call_later(0.1, token.cancel)
    started = time.monotonic()
    result = await runner.resume_async(
        agent, paused.interruption, approve=paused.interruption.pending_digests(), cancel=token
    )
    assert result.status == "cancelled" and time.monotonic() - started < 2


async def test_cancel_applies_to_recover() -> None:
    class Crash(BaseException):
        pass

    def crash(messages: Any, tools: Any) -> Any:
        raise Crash

    store = MemoryCheckpointStore()
    first = Agent(name="A", instructions="i", model=FakeModel(handler=crash))
    with pytest.raises(Crash):
        await Runner(checkpoint_store=store).run_async(first, "go")
    (run_id,) = await store.list_incomplete()
    token = CancelToken()
    asyncio.get_running_loop().call_later(0.1, token.cancel)
    again = Agent(name="A", instructions="i", model=SlowModel())  # type: ignore[arg-type]
    result = await Runner(checkpoint_store=store).recover_async(again, run_id, cancel=token)
    assert result.status == "cancelled"
    assert await store.list_incomplete() == [run_id]  # still recoverable later


def test_sync_resume_and_recover_accept_cancel() -> None:
    import inspect

    for method in (Runner.resume_async, Runner.recover_async):
        assert "cancel" in inspect.signature(method).parameters
