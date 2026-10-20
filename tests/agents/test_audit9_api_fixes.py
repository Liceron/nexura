"""Regression tests for audit 9 API findings: retry-feedback pop, Team runner/cancel, typing."""

from __future__ import annotations

import inspect
import subprocess
import sys
import textwrap

import pytest
from nexura_agents import (
    Agent,
    CancelToken,
    GroupChat,
    LoopAgent,
    MaxTurns,
    MemorySessionStore,
    ModelRetry,
    ParallelAgent,
    Runner,
    SequentialAgent,
    Team,
)
from nexura_agents.events import RunEventType
from nexura_agents.models.fake import FakeModel
from nexura_agents.session import pop_last_turn, retry_last_turn


def _ag(name: str, *responses: str) -> Agent:
    return Agent(
        name=name,
        model=FakeModel(list(responses) or ["ok"] * 5, repeat_last=True),
        instructions="x",
    )


def _validated_agent() -> Agent:
    def reject_first(text: str) -> str:
        if "attempt1" in text:
            raise ModelRetry("try again")
        return text

    return Agent(
        name="A",
        instructions="i",
        model=FakeModel(responses=["attempt1", "attempt2", "attempt3"]),
        output_validators=[reject_first],
    )


async def test_pop_last_turn_removes_the_real_user_turn_after_validator_retry():
    store = MemorySessionStore()
    runner = Runner(session_store=store)
    await runner.run_async(_validated_agent(), "ORIGINAL QUESTION", session_id="s")
    snap = await pop_last_turn(store, "s")
    assert snap is not None
    assert "ORIGINAL QUESTION" not in [m.content for m in snap.messages if m.role == "user"]


async def test_retry_last_turn_reruns_the_real_user_input_after_validator_retry():
    store = MemorySessionStore()
    runner = Runner(session_store=store)
    agent = _validated_agent()
    await runner.run_async(agent, "ORIGINAL QUESTION", session_id="s")
    result = await retry_last_turn(runner, agent, "s")
    assert result.status == "completed"
    snapshot = await store.get("s")
    assert snapshot is not None
    users = [m.content for m in snapshot.messages if m.role == "user"]
    assert users.count("ORIGINAL QUESTION") == 1


def test_team_run_stream_param_parity():
    run = set(inspect.signature(Team.run_async).parameters)
    assert set(inspect.signature(Team.stream_async).parameters) - run == set()
    assert {"runner", "cancel"} <= run
    assert {"runner", "cancel"} <= set(inspect.signature(Team.run).parameters)
    assert {"runner", "cancel"} <= set(inspect.signature(Team.resume_async).parameters)
    assert "cancel" in inspect.signature(Team.resume_stream_async).parameters


def test_team_run_accepts_runner():
    seen: list[str] = []

    class Spy(Runner):
        async def run_async(self, agent, prompt, **kwargs):  # type: ignore[no-untyped-def]
            seen.append(agent.name)
            return await super().run_async(agent, prompt, **kwargs)

    team = SequentialAgent("s", [_ag("a")])
    team.run("hi", runner=Spy())
    assert seen == ["a"]


def _teams() -> dict[str, Team]:
    return {
        "seq": SequentialAgent("s", [_ag("a"), SequentialAgent("inner", [_ag("i1"), _ag("i2")])]),
        "par": ParallelAgent("p", [_ag("a"), SequentialAgent("in", [_ag("x")])]),
        "loop": LoopAgent("l", SequentialAgent("in2", [_ag("a")]), max_iterations=2),
        "chat": GroupChat("g", [_ag("a"), _ag("b")], max_turns=3, terminate=MaxTurns(3)),
        "chat_mgr": GroupChat("g", [_ag("a"), _ag("b")], select=_ag("m", "a"), max_turns=3),
    }


@pytest.mark.parametrize("name", ["seq", "par", "loop", "chat", "chat_mgr"])
def test_pre_cancelled_team_returns_cancelled_result(name):
    token = CancelToken()
    token.cancel("stop")
    result = _teams()[name].run("hi", cancel=token)
    assert result.status == "cancelled"
    assert result.output is None
    assert result.interruption is None


async def test_cancel_during_run_stops_later_members():
    token = CancelToken()
    calls: list[str] = []

    def cancelling(messages, tools):  # type: ignore[no-untyped-def]
        calls.append("first")
        token.cancel("mid")
        from nexura_agents import Message

        return Message(role="assistant", content="done")

    first = Agent(name="first", model=FakeModel(handler=cancelling), instructions="x")
    second = _ag("second")
    result = await SequentialAgent("s", [first, second]).run_async("hi", cancel=token)
    assert result.status == "cancelled"
    assert [s.member for s in result.steps][0] == "first"
    assert all(s.member != "second" or s.result.status == "cancelled" for s in result.steps)
    assert second.model.calls == []  # type: ignore[attr-defined]


async def test_stream_cancelled_team_ends_with_one_terminal_event():
    token = CancelToken()
    token.cancel()
    events = [e async for e in _teams()["seq"].stream_async("hi", cancel=token)]
    terminal = [e for e in events if e.is_terminal]
    assert len(terminal) == 1 and events[-1].is_terminal
    assert terminal[0].type == RunEventType.TEAM_COMPLETED
    assert terminal[0].data["result"].status == "cancelled"


def test_typed_results_preserved_through_clone_override_retry(tmp_path):
    pytest.importorskip("mypy")
    f = tmp_path / "t.py"
    f.write_text(
        textwrap.dedent(
            """
        from pydantic import BaseModel
        from nexura_agents import Agent, Runner
        from nexura_agents.session import retry_last_turn
        from nexura_agents.models.fake import FakeModel
        class Out(BaseModel):
            a: int
        ag = Agent(name="a", model=FakeModel(["x"]), instructions="x", output_type=Out)
        reveal_type(ag)
        reveal_type(ag.clone(name="b"))
        with ag.override(name="c") as o:
            reveal_type(o)
        async def f(r: Runner):
            reveal_type(await retry_last_turn(r, ag, "s"))
    """
        )
    )
    out = subprocess.run(
        [sys.executable, "-m", "mypy", "--no-incremental", str(f)], capture_output=True, text=True
    ).stdout
    lines = [line for line in out.splitlines() if "Revealed" in line]
    assert len(lines) == 4, out
    assert all("Out" in line for line in lines), lines
