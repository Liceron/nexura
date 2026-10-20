"""Custom progress events from tools and hooks: ctx.emit (F54 part B)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from nexura_agents import Agent, AgentHooks, FakeModel, Runner, SequentialAgent, ToolCall, tool
from nexura_agents.errors import ToolExecutionError
from nexura_agents.events import RunEventType
from nexura_agents.run_context import RunContext


def calls(*names: str) -> list[ToolCall]:
    return [ToolCall(id=f"c{i}", name=n, arguments={}) for i, n in enumerate(names)]


async def events_of(agent: Any, prompt: str = "go", **kw: Any) -> list[Any]:
    return [e async for e in Runner(**kw).stream_async(agent, prompt)]


def custom(events: list[Any]) -> list[dict[str, Any]]:
    return [e.data for e in events if e.type == RunEventType.CUSTOM]


async def test_a_tool_reports_progress_while_it_runs() -> None:
    @tool
    async def ingest(ctx: RunContext[Any]) -> str:
        """Ingest files."""
        for done in (1, 2):
            ctx.emit("progress", done=done, total=2)
        return "ingested"

    model = FakeModel(responses=[calls("ingest"), "done"])
    events = await events_of(Agent(name="A", instructions="i", model=model, tools=[ingest]))
    assert custom(events) == [
        {
            "name": "progress",
            "data": {"done": 1, "total": 2},
            "agent": "A",
            "tool_call_id": "c0",
            "tool_name": "ingest",
        },
        {
            "name": "progress",
            "data": {"done": 2, "total": 2},
            "agent": "A",
            "tool_call_id": "c0",
            "tool_name": "ingest",
        },
    ]
    types = [e.type for e in events]
    # Emitted while the tool ran: after TOOL_STARTED, before TOOL_COMPLETED.
    first = types.index(RunEventType.CUSTOM)
    assert types.index(RunEventType.TOOL_STARTED) < first < types.index(RunEventType.TOOL_COMPLETED)


async def test_events_stream_live_not_after_the_batch() -> None:
    gate = asyncio.Event()

    @tool
    async def slow(ctx: RunContext[Any]) -> str:
        """Slow."""
        ctx.emit("started")
        await gate.wait()
        return "ok"

    model = FakeModel(responses=[calls("slow"), "done"])
    agent = Agent(name="A", instructions="i", model=model, tools=[slow])
    stream = Runner().stream_async(agent, "go")
    async for event in stream:
        if event.type == RunEventType.CUSTOM:
            assert event.data["name"] == "started"
            gate.set()  # only reachable if the event arrived while the tool was still waiting
            break
    rest = [e async for e in stream]
    assert rest[-1].type == RunEventType.RUN_COMPLETED


async def test_parallel_tools_interleave_and_are_tagged() -> None:
    @tool
    async def a(ctx: RunContext[Any]) -> str:
        """A."""
        ctx.emit("tick", n=1)
        await asyncio.sleep(0)
        ctx.emit("tick", n=2)
        return "a"

    @tool
    async def b(ctx: RunContext[Any]) -> str:
        """B."""
        ctx.emit("tick", n=1)
        return "b"

    model = FakeModel(responses=[calls("a", "b"), "done"])
    events = await events_of(Agent(name="A", instructions="i", model=model, tools=[a, b]))
    seen = [(d["tool_name"], d["data"]["n"]) for d in custom(events)]
    assert sorted(seen) == [("a", 1), ("a", 2), ("b", 1)]
    assert [n for t, n in seen if t == "a"] == [1, 2]  # per tool, in order


async def test_sync_tools_can_emit_from_their_thread() -> None:
    @tool
    def crunch(ctx: RunContext[Any]) -> str:
        """Crunch."""
        ctx.emit("step", n=1)
        return "ok"

    model = FakeModel(responses=[calls("crunch"), "done"])
    events = await events_of(Agent(name="A", instructions="i", model=model, tools=[crunch]))
    assert [d["data"] for d in custom(events)] == [{"n": 1}]


async def test_hooks_outside_tools_emit_at_the_next_step() -> None:
    class Hooks(AgentHooks):
        async def before_model(self, ctx: RunContext[Any], messages: Any) -> None:
            ctx.emit("thinking", turn=ctx.turn)

    agent = Agent(name="A", instructions="i", model=FakeModel(responses=["hi"]))
    events = await events_of(agent, hooks=Hooks())
    assert [d["data"] for d in custom(events)] == [{"turn": 1}]
    assert custom(events)[0]["tool_name"] is None
    assert events[-1].type == RunEventType.RUN_COMPLETED  # flushed before the end


async def test_run_async_ignores_events_and_results_are_unchanged() -> None:
    @tool
    async def noisy(ctx: RunContext[Any]) -> str:
        """Noisy."""
        ctx.emit("x")
        return "ok"

    model = FakeModel(responses=[calls("noisy"), "done"])
    result = await Runner().run_async(
        Agent(name="A", instructions="i", model=model, tools=[noisy]), "go"
    )
    assert result.output == "done"
    assert [e.type for e in result.events].count(RunEventType.CUSTOM) == 1


@pytest.mark.parametrize("name", ["", 3, None])
async def test_event_names_must_be_text(name: Any) -> None:
    @tool
    async def bad(ctx: RunContext[Any]) -> str:
        """Bad."""
        ctx.emit(name)
        return "ok"

    model = FakeModel(responses=[calls("bad"), "done"])
    with pytest.raises(ToolExecutionError, match="SDKValidationError: event name"):
        await Runner().run_async(Agent(name="A", instructions="i", model=model, tools=[bad]), "go")


async def test_team_streams_carry_member_custom_events() -> None:
    @tool
    async def work(ctx: RunContext[Any]) -> str:
        """Work."""
        ctx.emit("progress", pct=50)
        return "ok"

    member = Agent(
        name="Worker",
        instructions="i",
        model=FakeModel(responses=[calls("work"), "done"]),
        tools=[work],
    )
    team = SequentialAgent("Team", [member])
    events = [e async for e in team.stream_async("go")]
    found = [e for e in events if e.type == RunEventType.CUSTOM]
    assert [e.data["member"] for e in found] == ["Worker"]
    assert found[0].data["data"] == {"pct": 50}


async def test_closing_the_stream_during_a_tool_cancels_it() -> None:
    cancelled = asyncio.Event()

    @tool
    async def forever(ctx: RunContext[Any]) -> str:
        """Forever."""
        ctx.emit("started")
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return "never"

    model = FakeModel(responses=[calls("forever"), "done"])
    stream = Runner().stream_async(
        Agent(name="A", instructions="i", model=model, tools=[forever]), "go"
    )
    async for event in stream:
        if event.type == RunEventType.CUSTOM:
            break
    await stream.aclose()
    await asyncio.wait_for(cancelled.wait(), 2)
