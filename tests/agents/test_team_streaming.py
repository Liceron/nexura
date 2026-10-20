"""Team.stream_async: team and member events, tagging, failure, early close and parity."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from nexura_agents import (
    Agent,
    FakeModel,
    LoopAgent,
    ParallelAgent,
    SequentialAgent,
    Team,
    TeamResult,
    ToolCall,
    Usage,
    tool,
)
from nexura_agents.events import RunEvent, RunEventType
from nexura_agents.types import ModelResponse

USAGE = Usage(prompt_tokens=3, completion_tokens=2, total_tokens=5)
T = RunEventType


class Slow(FakeModel):
    def __init__(self, delay: float, responses: Any = None) -> None:
        super().__init__(responses=responses, default_usage=USAGE)
        self.delay = delay
        self.cancelled = False
        self.finished = False

    async def generate(self, *args: Any, **kwargs: Any) -> ModelResponse:
        try:
            await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        self.finished = True
        return await super().generate(*args, **kwargs)


class Boom(FakeModel):
    async def generate(self, *args: Any, **kwargs: Any) -> ModelResponse:
        await asyncio.sleep(0.01)
        raise RuntimeError("model exploded")


def say(name: str, *replies: Any, delay: float = 0.0) -> Agent:
    return Agent(name=name, instructions="", model=Slow(delay, list(replies)))


async def collect(team: Team, prompt: str = "go") -> list[RunEvent]:
    return [event async for event in team.stream_async(prompt)]


def summary(events: list[RunEvent]) -> list[tuple[str, str | None]]:
    return [(e.type.value, e.data.get("member")) for e in events]


def of(events: list[RunEvent], kind: RunEventType) -> list[RunEvent]:
    return [e for e in events if e.type == kind]


def same(a: TeamResult, b: TeamResult) -> None:
    assert (a.name, a.status, a.output, a.usage) == (b.name, b.status, b.output, b.usage)
    assert [(s.path, s.member, s.result.final_output) for s in a.steps] == [
        (s.path, s.member, s.result.final_output) for s in b.steps
    ]


async def test_sequential_order_and_result_match_run() -> None:
    def build() -> SequentialAgent:
        return SequentialAgent("Pipe", [say("A", "one"), say("B", "two")])

    events = await collect(build())
    plain = await build().run_async("go")

    assert events[0].type == T.TEAM_STARTED and events[0].data["team"] == "Pipe"
    assert events[0].data["member_path"] == []
    assert events[-1].type == T.TEAM_COMPLETED
    final = events[-1].data["result"]
    assert isinstance(final, TeamResult)
    same(final, plain)

    marks = [(e.type.value, e.data.get("member")) for e in events if e.data.get("member")]
    kinds = [m for m in marks if m[0] in ("member_started", "member_completed")]
    assert kinds == [
        ("member_started", "A"),
        ("member_completed", "A"),
        ("member_started", "B"),
        ("member_completed", "B"),
    ]
    # A's run events all come between A's member events.
    positions = {
        (e.type, e.data.get("member")): i
        for i, e in enumerate(events)
        if e.type in (T.MEMBER_STARTED, T.MEMBER_COMPLETED)
    }
    for i, e in enumerate(events):
        if e.type == T.RUN_STARTED:
            member = e.data["member"]
            assert (
                positions[(T.MEMBER_STARTED, member)] < i < positions[(T.MEMBER_COMPLETED, member)]
            )
    run_started = of(events, T.RUN_STARTED)
    assert [e.data["member_path"] for e in run_started] == [["0"], ["1"]]
    assert all(e.data["team"] == "Pipe" for e in run_started)
    assert of(events, T.MEMBER_COMPLETED)[0].data["status"] == "completed"


async def test_parallel_events_interleave_and_result_matches() -> None:
    def build() -> ParallelAgent:
        return ParallelAgent("Par", [say("Slow", "s", delay=0.08), say("Fast", "f", delay=0.0)])

    events = await collect(build())
    plain = await build().run_async("go")
    same(events[-1].data["result"], plain)

    completed = [e.data["member"] for e in of(events, T.MEMBER_COMPLETED)]
    assert completed == ["Fast", "Slow"]  # arrival order, not declaration order
    started = [e.data["member"] for e in of(events, T.MEMBER_STARTED)]
    assert started == ["Slow", "Fast"]
    # Fast finishes before Slow's run completes: events interleave.
    kinds = summary(events)
    assert kinds.index(("member_completed", "Fast")) < kinds.index(("run_completed", "Slow"))
    assert events[-1].data["result"].output == {"Slow": "s", "Fast": "f"}


async def test_loop_iterations_have_paths_and_result_matches() -> None:
    def build() -> LoopAgent:
        return LoopAgent(
            "Loop",
            say("Worker", "draft", "better", "best"),
            max_iterations=3,
            until=lambda r: r.final_output == "better",
        )

    events = await collect(build())
    plain = await build().run_async("go")
    same(events[-1].data["result"], plain)
    assert [e.data["member_path"] for e in of(events, T.MEMBER_STARTED)] == [
        ["iter-1"],
        ["iter-2"],
    ]
    assert events[-1].data["result"].output == "better"


def nested() -> SequentialAgent:
    inner = ParallelAgent("Inner", [say("X", "x"), say("Y", "y")])
    return SequentialAgent("Outer", [say("First", "f"), inner])


async def test_nested_team_events_carry_full_path() -> None:
    events = await collect(nested())
    starts = [(e.data["team"], e.data["member_path"]) for e in of(events, T.TEAM_STARTED)]
    assert starts == [("Outer", []), ("Inner", ["1"])]
    paths = sorted(e.data["member_path"] for e in of(events, T.RUN_STARTED))
    assert paths == [["0"], ["1", "0"], ["1", "1"]]
    assert events[-1].type == T.TEAM_COMPLETED and events[-1].data["team"] == "Outer"
    same(events[-1].data["result"], await nested().run_async("go"))


async def test_member_failure_yields_team_failed_then_raises() -> None:
    bad = Agent(name="Bad", instructions="", model=Boom())
    team = SequentialAgent("Pipe", [say("Ok", "fine"), bad, say("Never", "x")])
    seen: list[RunEvent] = []
    with pytest.raises(Exception, match="model exploded"):
        async for event in team.stream_async("go"):
            seen.append(event)
    assert seen[-1].type == T.TEAM_FAILED
    assert seen[-1].data["error_type"] == "RuntimeError" or "exploded" in seen[-1].data["error"]
    assert ("member_completed", "Ok") in summary(seen)
    assert ("member_started", "Never") not in summary(seen)
    assert any(e.type == T.RUN_FAILED and e.data["member"] == "Bad" for e in seen)


async def test_parallel_failure_cancels_siblings() -> None:
    slow = Slow(5.0, ["late"])
    team = ParallelAgent(
        "Par",
        [
            Agent(name="Slow", instructions="", model=slow),
            Agent(name="Bad", instructions="", model=Boom()),
        ],
    )
    seen = []
    with pytest.raises(Exception, match="model exploded"):
        async for event in team.stream_async("go"):
            seen.append(event)
    assert seen[-1].type == T.TEAM_FAILED
    assert slow.cancelled and not slow.finished


async def test_early_close_cancels_members() -> None:
    slow_a, slow_b = Slow(5.0, ["a"]), Slow(5.0, ["b"])
    team = ParallelAgent(
        "Par",
        [
            Agent(name="A", instructions="", model=slow_a),
            Agent(name="B", instructions="", model=slow_b),
        ],
    )
    stream = team.stream_async("go")
    first = await anext(stream)
    assert first.type == T.TEAM_STARTED
    await asyncio.sleep(0.1)  # both members are inside their model call
    await stream.aclose()
    assert slow_a.cancelled and slow_b.cancelled
    assert not slow_a.finished and not slow_b.finished


async def test_early_close_sequential_stops_later_members() -> None:
    first = say("A", "one", delay=0.2)
    second = say("B", "two")
    team = SequentialAgent("Pipe", [first, second])
    stream = team.stream_async("go")
    await anext(stream)
    await asyncio.sleep(0.05)
    await stream.aclose()
    assert first.model.cancelled  # type: ignore[attr-defined]
    assert second.model.calls == []  # type: ignore[attr-defined]


async def test_paused_team_streams_team_paused_and_resumes() -> None:
    refunds: list[str] = []

    def gated(resumed: bool) -> Agent:
        @tool(requires_approval=True)
        def refund(order_id: str) -> str:
            """Refund."""
            refunds.append(order_id)
            return "ok"

        replies: list[Any] = ["done"]
        if not resumed:
            replies.insert(0, [ToolCall(id="c1", name="refund", arguments={"order_id": "A1"})])
        return Agent(name="Gated", instructions="", model=Slow(0, replies), tools=[refund])

    team = SequentialAgent("Pipe", [say("First", "ready"), gated(False)])
    events = await collect(team)
    assert events[-1].type == T.TEAM_PAUSED
    paused = events[-1].data["result"]
    assert paused.status == "paused" and paused.interruption is not None
    assert any(e.type == T.RUN_PAUSED and e.data["member"] == "Gated" for e in events)

    resumed_team = SequentialAgent("Pipe", [say("First", "ready"), gated(True)])
    stream = resumed_team.resume_stream_async(paused.interruption, approve=True)
    resumed = [e async for e in stream]
    assert resumed[-1].type == T.TEAM_COMPLETED
    assert refunds == ["A1"]
    restored = [e for e in resumed if e.type == T.MEMBER_COMPLETED and e.data.get("restored")]
    assert [e.data["member"] for e in restored] == ["First"]


async def test_stream_validates_timeout_eagerly() -> None:
    team = SequentialAgent("Pipe", [say("A", "x")])
    with pytest.raises(Exception, match="timeout"):
        team.stream_async("go", timeout=-1)


async def test_runner_override_is_used() -> None:
    from nexura_agents import Runner

    seen: list[str] = []

    class Spy(Runner):
        def stream_async(self, agent: Agent, prompt: Any, **kwargs: Any) -> Any:
            seen.append(agent.name)
            return super().stream_async(agent, prompt, **kwargs)

    team = SequentialAgent("Pipe", [say("A", "x")])
    events = [e async for e in team.stream_async("go", runner=Spy())]
    assert events[-1].type == T.TEAM_COMPLETED and seen == ["A"]
