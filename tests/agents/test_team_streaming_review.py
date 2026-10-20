"""F54 review: terminal and failure events, unretrieved errors in team and workflow streams."""

from __future__ import annotations

import asyncio
import gc
from typing import Any

from nexura_agents import Agent, FakeModel, Runner, SequentialAgent
from nexura_agents.events import RunEventType as T

from nexura_runtime.workflow import (
    MemoryWorkflowStore,
    ModuleRegistry,
    WorkflowEngine,
    WorkflowNode,
    WorkflowSpec,
)


def ag(name: str, *replies: Any, model: Any = None) -> Agent:
    return Agent(name=name, instructions="", model=model or FakeModel(responses=list(replies)))


class Boom(FakeModel):
    async def generate(self, *a: Any, **k: Any) -> Any:
        raise RuntimeError("boom")


async def test_team_terminal_events_are_terminal() -> None:
    """is_terminal is True for a member's RUN_COMPLETED mid-team, False for TEAM_COMPLETED."""
    team = SequentialAgent("P", [ag("a", "x"), ag("b", "y")])
    events = [e async for e in team.stream_async("go")]
    first_terminal = next(i for i, e in enumerate(events) if e.is_terminal)
    assert events[-1].type == T.TEAM_COMPLETED
    assert events[-1].is_terminal, "TEAM_COMPLETED is not is_terminal"
    assert first_terminal == len(events) - 1, "member RUN_COMPLETED reports is_terminal mid-stream"


async def test_workflow_terminal_events_are_terminal() -> None:
    reg = ModuleRegistry()
    reg.register("one", lambda s: {"a": 1})
    spec = WorkflowSpec(
        name="w",
        entry_node="n",
        nodes={"n": WorkflowNode(id="n", handler="one")},
        terminal_nodes=["n"],
    )
    events = [e async for e in WorkflowEngine(spec, reg).stream({})]
    assert events[-1].type == T.WORKFLOW_COMPLETED
    assert events[-1].is_terminal


async def test_team_stream_uses_runner_stream_async() -> None:
    calls: list[str] = []

    class Counting(Runner):
        async def run_async(self, agent: Any, prompt: Any, **kw: Any) -> Any:
            calls.append(agent.name)
            return await super().run_async(agent, prompt, **kw)

    team = SequentialAgent("P", [ag("a", "x")], runner=Counting())
    await team.run_async("go")
    assert calls == ["a"]
    calls.clear()
    _ = [e async for e in team.stream_async("go")]
    # Documented: streaming goes through Runner.stream_async, not run_async.
    assert calls == []


async def test_nested_failure_is_tagged_and_ends_once() -> None:
    inner = SequentialAgent("Inner", [ag("bad", model=Boom())])
    outer = SequentialAgent("Outer", [inner])
    seen = []
    try:
        async for e in outer.stream_async("go"):
            seen.append(e)
    except Exception:
        pass
    failed = [e for e in seen if e.type == T.TEAM_FAILED]
    # The nested team reports its own failure, tagged with its path; one root failure ends it.
    assert [e.data["member_path"] for e in failed] == [["0"], []]
    assert [e for e in seen if e.is_terminal] == [failed[-1]]
    assert [e.data["member"] for e in seen if e.type == T.MEMBER_FAILED] == ["bad", "Inner"]


async def test_failed_member_gets_a_closing_event() -> None:
    team = SequentialAgent("P", [ag("bad", model=Boom())])
    seen = []
    try:
        async for e in team.stream_async("go"):
            seen.append(e)
    except Exception:
        pass
    started = [e for e in seen if e.type == T.MEMBER_STARTED]
    closed = [e for e in seen if e.type in (T.MEMBER_COMPLETED, T.MEMBER_FAILED)]
    assert len(started) == len(closed), "MEMBER_STARTED has no matching close on failure"


async def test_workflow_stream_does_one_extra_store_lookup() -> None:
    gets: list[str] = []

    class Store(MemoryWorkflowStore):
        async def get(self, run_id: str) -> Any:
            gets.append(run_id)
            return await super().get(run_id)

    reg = ModuleRegistry()
    reg.register("one", lambda s: {"a": 1})
    spec = WorkflowSpec(
        name="w",
        entry_node="n",
        nodes={"n": WorkflowNode(id="n", handler="one")},
        terminal_nodes=["n"],
    )
    eng = WorkflowEngine(spec, reg, store=Store())
    await eng.run({})
    run_gets = len(gets)
    gets.clear()
    _ = [e async for e in eng.stream({})]
    # Accepted: stream() fixes the run id up front (so WORKFLOW_STARTED can carry it), which
    # may cost one extra store lookup and never more (the atomic run claim added in the
    # final audit's B01 fix needs none).
    assert len(gets) <= run_gets + 1


async def test_workflow_unretrieved_exception_on_early_close() -> None:
    reg = ModuleRegistry()

    def bad(s: dict[str, Any]) -> dict[str, Any]:
        raise ValueError("x")

    reg.register("bad", bad)
    spec = WorkflowSpec(
        name="w",
        entry_node="n",
        nodes={"n": WorkflowNode(id="n", handler="bad")},
        terminal_nodes=["n"],
    )
    loop = asyncio.get_running_loop()
    problems: list[Any] = []
    loop.set_exception_handler(lambda loop, c: problems.append(c))
    stream = WorkflowEngine(spec, reg).stream({})
    await anext(stream)
    await anext(stream)  # NODE_STARTED
    await asyncio.sleep(0.05)  # run has failed in the background
    await stream.aclose()
    del stream
    gc.collect()
    await asyncio.sleep(0)
    assert not problems, problems


async def test_team_unretrieved_exception_on_early_close() -> None:
    team = SequentialAgent("P", [ag("bad", model=Boom())])
    loop = asyncio.get_running_loop()
    problems: list[Any] = []
    loop.set_exception_handler(lambda loop, c: problems.append(c))
    stream = team.stream_async("go")
    await anext(stream)
    await asyncio.sleep(0.1)
    await stream.aclose()
    del stream
    gc.collect()
    await asyncio.sleep(0)
    assert not problems, problems
