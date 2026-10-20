"""Regressions from the F54 part B review: late thread emits, event data snapshots."""

from __future__ import annotations

import asyncio
import threading
from typing import Any

from nexura_agents import Agent, AgentHooks, CancelToken, FakeModel, Runner, ToolCall, tool
from nexura_agents.events import RunEventType
from nexura_agents.run_context import RunContext


def calls(*names: str) -> list[ToolCall]:
    return [ToolCall(id=f"c{i}", name=n, arguments={}) for i, n in enumerate(names)]


def custom(events: list[Any]) -> list[dict[str, Any]]:
    return [e.data for e in events if e.type == RunEventType.CUSTOM]


async def test_thread_emit_racing_detach_is_not_lost() -> None:
    release = threading.Event()
    started = threading.Event()

    @tool
    def spawn(ctx: RunContext[Any]) -> str:
        """Spawn."""
        loop = asyncio.get_event_loop_policy()  # noqa: F841

        def work() -> None:
            ctx.emit("late")

        # patch call_soon_threadsafe only for the worker thread
        real_loop = ctx._events._loop
        orig = real_loop.call_soon_threadsafe

        def slow(cb: Any, *a: Any) -> Any:
            if threading.current_thread().name == "emitter":
                started.set()
                release.wait(5)
            return orig(cb, *a)

        real_loop.call_soon_threadsafe = slow  # type: ignore[method-assign]
        t = threading.Thread(target=work, name="emitter")
        t.start()
        started.wait(5)
        return "ok"

    model = FakeModel(responses=[calls("spawn"), "done"])
    agent = Agent(name="A", instructions="i", model=model, tools=[spawn])
    events = []
    async for e in Runner().stream_async(agent, "go"):
        events.append(e)
        if e.type == RunEventType.TOOL_COMPLETED:
            release.set()
            await asyncio.sleep(0.2)
    assert [d["name"] for d in custom(events)] == ["late"]


async def test_emitted_data_is_snapshotted() -> None:
    @tool
    async def t(ctx: RunContext[Any]) -> str:
        """T."""
        items = [1]
        ctx.emit("p", items=items)
        items.append(2)
        return "ok"

    model = FakeModel(responses=[calls("t"), "done"])
    agent = Agent(name="A", instructions="i", model=model, tools=[t])
    events = [e async for e in Runner().stream_async(agent, "go")]
    assert custom(events)[0]["data"]["items"] == [1]


async def test_cancel_token_leaves_no_tasks_and_keeps_events() -> None:
    token = CancelToken()

    @tool
    async def slow(ctx: RunContext[Any]) -> str:
        """Slow."""
        ctx.emit("first")
        await asyncio.sleep(0.05)
        token.cancel("stop")
        await asyncio.sleep(5)
        return "x"

    model = FakeModel(responses=[calls("slow"), "done"])
    agent = Agent(name="A", instructions="i", model=model, tools=[slow])
    before = {t for t in asyncio.all_tasks()}
    events = [e async for e in Runner().stream_async(agent, "go", cancel=token)]
    await asyncio.sleep(0.05)
    leftover = {t for t in asyncio.all_tasks()} - before - {asyncio.current_task()}
    assert not leftover
    assert [d["name"] for d in custom(events)] == ["first"]
    assert events[-1].type == RunEventType.RUN_CANCELLED or "cancel" in events[-1].type.value


async def test_events_before_tool_failure_are_delivered_before_failure() -> None:
    @tool
    async def bad(ctx: RunContext[Any]) -> str:
        """Bad."""
        ctx.emit("before_fail")
        raise RuntimeError("boom")

    model = FakeModel(responses=[calls("bad"), "done"])
    agent = Agent(name="A", instructions="i", model=model, tools=[bad])
    events = []
    try:
        async for e in Runner().stream_async(agent, "go"):
            events.append(e)
    except Exception:
        pass
    types = [e.type for e in events]
    assert RunEventType.CUSTOM in types
    assert types.index(RunEventType.CUSTOM) < types.index(RunEventType.RUN_FAILED)


async def test_pause_flushes_hook_events() -> None:
    class H(AgentHooks):
        async def on_agent_start(self, ctx: Any, agent: Any) -> None:
            ctx.emit("start")

    @tool(requires_approval=True)
    async def danger(ctx: RunContext[Any]) -> str:
        """D."""
        return "x"

    model = FakeModel(responses=[calls("danger"), "done"])
    agent = Agent(name="A", instructions="i", model=model, tools=[danger])
    events = [e async for e in Runner(hooks=H()).stream_async(agent, "go")]
    assert RunEventType.RUN_PAUSED in [e.type for e in events]
    assert [d["name"] for d in custom(events)] == ["start"]
