"""SequentialAgent, ParallelAgent and LoopAgent: composition, approvals and MCP serving."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from nexura_agents import (
    Agent,
    AgentMCPServer,
    FakeModel,
    LoopAgent,
    MCPServer,
    ParallelAgent,
    Runner,
    SequentialAgent,
    Team,
    TeamInterruption,
    TeamResult,
    ToolCall,
    Usage,
    tool,
)
from nexura_agents.errors import (
    ApprovalDecisionError,
    EventLoopError,
    ModelTimeoutError,
    ProviderError,
    SDKValidationError,
    ToolError,
    UsageLimitExceededError,
)
from nexura_agents.types import ModelResponse
from pydantic import BaseModel

USAGE = Usage(prompt_tokens=3, completion_tokens=2, total_tokens=5)


def say(name: str, *replies: Any, **kwargs: Any) -> Agent:
    model = FakeModel(responses=list(replies), default_usage=USAGE)
    return Agent(name=name, instructions=f"You are {name}.", model=model, **kwargs)


def call(name: str, call_id: str = "c1", **arguments: Any) -> list[ToolCall]:
    return [ToolCall(id=call_id, name=name, arguments=arguments)]


class Slow(FakeModel):
    """Waits, then answers; records whether it was cancelled."""

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


# ---------------------------------------------------------------- validation


def test_construction_validation() -> None:
    a = say("A", "x")
    with pytest.raises(SDKValidationError, match="at least one"):
        SequentialAgent("T", [])
    with pytest.raises(SDKValidationError, match="Agent or Team"):
        SequentialAgent("T", [a, "nope"])  # type: ignore[list-item]
    with pytest.raises(SDKValidationError, match="Duplicate member name"):
        ParallelAgent("T", [a, say("A", "y")])
    with pytest.raises(SDKValidationError, match="name"):
        SequentialAgent("  ", [a])
    with pytest.raises(SDKValidationError, match="max_concurrency"):
        ParallelAgent("T", [a], max_concurrency=0)
    for bad in (0, 101, -1, True, 2.5):
        with pytest.raises(SDKValidationError, match="max_iterations"):
            LoopAgent("L", a, max_iterations=bad)  # type: ignore[arg-type]
    LoopAgent("L", a, max_iterations=1)
    LoopAgent("L", a, max_iterations=100)
    # Names must also be unique when members are teams.
    inner = SequentialAgent("A", [say("Z", "z")])
    with pytest.raises(SDKValidationError, match="Duplicate"):
        SequentialAgent("T", [a, inner])


async def test_team_base_is_abstract() -> None:
    from nexura_agents import Team

    with pytest.raises(TypeError):
        Team("x")  # type: ignore[abstract]


def test_sync_run_refuses_a_running_event_loop() -> None:
    team = SequentialAgent("T", [say("A", "x")])

    async def inside() -> None:
        team.run("go")

    with pytest.raises(EventLoopError):
        asyncio.run(inside())


def test_sync_run_works_outside_a_loop() -> None:
    result = SequentialAgent("T", [say("A", "x")]).run("go")
    assert result.status == "completed" and result.output == "x"


# ---------------------------------------------------------------- sequential


async def test_sequential_passes_each_output_to_the_next_and_sums_usage() -> None:
    first = say("First", "draft")
    second = say("Second", "polished")
    team = SequentialAgent("Pipe", [first, second])
    result = await team.run_async("topic")
    assert result.status == "completed" and result.output == "polished"
    assert [(s.path, s.member) for s in result.steps] == [("0", "First"), ("1", "Second")]
    assert result.interruption is None
    seen = second.model.calls[0]["messages"]  # type: ignore[attr-defined]
    assert seen[-1].content == "draft"
    assert result.usage.total_tokens == 10 and result.usage.prompt_tokens == 6


class Doc(BaseModel):
    title: str


async def test_sequential_serialises_models_and_values_and_custom_handoff() -> None:
    typed = say("Typed", '{"title": "T"}', output_type=Doc)
    plain = say("Plain", "ok")
    team = SequentialAgent("Pipe", [typed, plain])
    await team.run_async("x")
    assert plain.model.calls[0]["messages"][-1].content == '{"title":"T"}'  # type: ignore[attr-defined]

    seen: list[tuple[Any, Any]] = []

    def handoff(original: Any, previous: Any) -> str:
        seen.append((original, previous.final_output))
        return f"{original} / {previous.final_output}"

    a, b = say("A", "one"), say("B", "two")
    out = await SequentialAgent("P", [a, b], handoff=handoff).run_async("orig")
    assert seen == [("orig", "one")]
    assert b.model.calls[0]["messages"][-1].content == "orig / one"  # type: ignore[attr-defined]
    assert out.output == "two"


async def test_sequential_failure_propagates_original_error_and_stops() -> None:
    after = say("After", "never")
    broken = Agent(name="Broken", instructions="", model=Boom())
    with pytest.raises(ProviderError, match="model exploded"):
        await SequentialAgent("P", [broken, after]).run_async("x")
    assert after.model.calls == []  # type: ignore[attr-defined]


async def test_team_timeout_bounds_the_whole_team() -> None:
    slow = Agent(name="Slow", instructions="", model=Slow(0.3))
    with pytest.raises((ModelTimeoutError, asyncio.TimeoutError)):
        await SequentialAgent("P", [slow]).run_async("x", timeout=0.05)
    with pytest.raises(SDKValidationError):
        await SequentialAgent("P", [slow]).run_async("x", timeout=-1)


async def test_shared_runner_limits_cover_every_member_and_nested_teams() -> None:
    seen: list[str] = []

    from nexura_agents import AgentHooks

    class H(AgentHooks):
        async def before_model(self, ctx: Any, *args: Any, **kwargs: Any) -> None:
            seen.append(ctx.agent_name)

    runner = Runner(hooks=H())
    inner = SequentialAgent("Inner", [say("B", "b")])
    outer = SequentialAgent("Outer", [say("A", "a"), inner], runner=runner)
    await outer.run_async("go")
    assert seen == ["A", "B"]


# ---------------------------------------------------------------- parallel


async def test_parallel_runs_concurrently_keeps_declaration_order() -> None:
    slow = Agent(name="Slow", instructions="", model=Slow(0.1))
    fast = say("Fast", "f")
    team = ParallelAgent("P", [slow, fast])
    started = asyncio.get_running_loop().time()
    result = await team.run_async("q")
    assert asyncio.get_running_loop().time() - started < 0.19
    assert [s.member for s in result.steps] == ["Slow", "Fast"]
    assert [s.path for s in result.steps] == ["0", "1"]
    assert list(result.output) == ["Slow", "Fast"] and result.output["Fast"] == "f"
    assert result.usage.total_tokens == 10
    assert slow.model.calls[0]["messages"][-1].content == "q"  # type: ignore[attr-defined]
    assert fast.model.calls[0]["messages"][-1].content == "q"  # type: ignore[attr-defined]


async def test_parallel_combine_and_concurrency_limit() -> None:
    models = [Slow(0.05) for _ in range(4)]
    agents = [Agent(name=f"W{i}", instructions="", model=m) for i, m in enumerate(models)]
    result = await ParallelAgent(
        "P",
        agents,
        max_concurrency=2,
        combine=lambda by_name: sorted(by_name),
    ).run_async("q")
    assert result.output == ["W0", "W1", "W2", "W3"]

    # With a limit of one the members cannot overlap.
    started = asyncio.get_running_loop().time()
    await ParallelAgent("P2", agents, max_concurrency=1).run_async("q")
    assert asyncio.get_running_loop().time() - started >= 0.19


async def test_parallel_failure_cancels_siblings_and_raises_that_error() -> None:
    sibling = Slow(5)
    team = ParallelAgent(
        "P",
        [
            Agent(name="Sibling", instructions="", model=sibling),
            Agent(name="Broken", instructions="", model=Boom()),
        ],
    )
    started = asyncio.get_running_loop().time()
    with pytest.raises(ProviderError, match="model exploded"):
        await team.run_async("q")
    assert asyncio.get_running_loop().time() - started < 2
    assert sibling.cancelled and not sibling.finished


# ---------------------------------------------------------------- loop


async def test_loop_stops_on_until_and_feeds_back_the_last_output() -> None:
    agent = say("Writer", "v1", "v2", "v3", "v4")
    team = LoopAgent("L", agent, max_iterations=5, until=lambda r: r.final_output == "v3")
    result = await team.run_async("start")
    assert result.output == "v3"
    assert [s.path for s in result.steps] == ["iter-1", "iter-2", "iter-3"]
    prompts = [c["messages"][-1].content for c in agent.model.calls]  # type: ignore[attr-defined]
    assert prompts == ["start", "v1", "v2"]
    assert result.usage.total_tokens == 15


async def test_loop_caps_at_max_iterations_and_custom_feedback() -> None:
    agent = say("W", "a", "b", "c")
    result = await LoopAgent(
        "L", agent, max_iterations=2, feedback=lambda orig, last: f"{orig}+{last.final_output}"
    ).run_async("p")
    assert len(result.steps) == 2 and result.output == "b"
    prompts = [c["messages"][-1].content for c in agent.model.calls]  # type: ignore[attr-defined]
    assert prompts == ["p", "p+a"]
    once = await LoopAgent("L1", say("W", "z"), max_iterations=1).run_async("p")
    assert [s.path for s in once.steps] == ["iter-1"]


async def test_teams_nest() -> None:
    team = SequentialAgent(
        "Outer",
        [
            say("Intro", "start"),
            ParallelAgent("Fan", [say("L", "left"), say("R", "right")]),
            say("Wrap", "wrapped"),
        ],
    )
    result = await team.run_async("go")
    assert result.output == "wrapped"
    fan = result.steps[1].result
    assert isinstance(fan, TeamResult) and fan.output == {"L": "left", "R": "right"}
    assert [s.path for s in fan.steps] == ["1/0", "1/1"]
    assert result.usage.total_tokens == 20
    wrap = team.members[2]
    assert isinstance(wrap, Agent)
    assert wrap.model.calls[0]["messages"][-1].content == '{"L": "left", "R": "right"}'  # type: ignore[attr-defined]


# ---------------------------------------------------------------- approvals


class Shop:
    def __init__(self) -> None:
        self.refunds: list[tuple[str, int]] = []


def approving_agent(
    name: str,
    order: str,
    shop: Shop,
    call_id: str = "c1",
    *,
    delay: float = 0,
    resumed: bool = False,
) -> Agent:
    """An agent whose first step needs approval. ``resumed=True`` builds the copy a new
    process would have, whose model only holds what comes after the approval."""

    @tool(requires_approval=True)
    def refund(order_id: str, amount: int) -> str:
        """Refund an order."""
        shop.refunds.append((order_id, amount))
        return f"refunded {order_id}"

    replies: list[Any] = [f"{name} done"]
    if not resumed:
        replies.insert(0, call("refund", call_id, order_id=order, amount=10))
    return Agent(name=name, instructions="", model=Slow(delay, replies), tools=[refund])


async def test_sequential_pauses_then_resumes_without_rerunning_finished_members() -> None:
    shop = Shop()
    first = say("First", "prepared")
    gated = approving_agent("Gated", "A1", shop)
    last = say("Last", "all done")
    team = SequentialAgent("Pipe", [first, gated, last])

    paused = await team.run_async("go")
    assert paused.status == "paused" and paused.interruption is not None
    assert [s.path for s in paused.steps] == ["0", "1"]
    assert shop.refunds == [] and last.model.calls == []  # type: ignore[attr-defined]
    assert paused.usage.total_tokens == 10  # first run + gated run so far
    pending = paused.interruption.pending_calls()
    assert [(k, c.name) for k, c in pending] == [("1:c1", "refund")]
    digests = paused.interruption.pending_digests()
    assert set(digests) == {"1:c1"}

    final = await team.resume_async(paused.interruption, approve=digests)
    assert final.status == "completed" and final.output == "all done"
    assert shop.refunds == [("A1", 10)]
    assert len(first.model.calls) == 1  # type: ignore[attr-defined]  # not run again
    assert [s.path for s in final.steps] == ["0", "1", "2"]
    # First (5) + Gated before and after approval (10) + Last (5); the pause adds nothing twice.
    assert final.usage.total_tokens == 20
    assert last.model.calls[0]["messages"][-1].content == "Gated done"  # type: ignore[attr-defined]


async def test_json_round_trip_resumes_in_a_new_process() -> None:
    shop = Shop()
    team = SequentialAgent(
        "Pipe",
        [say("First", "prepared"), approving_agent("Gated", "A1", shop), say("Last", "fin")],
    )
    paused = await team.run_async(["go", "now"])
    saved = paused.interruption.to_json()  # type: ignore[union-attr]
    assert json.loads(saved)["schema_version"] == 1

    shop2 = Shop()
    fresh_first = say("First", "NEVER")
    fresh = SequentialAgent(
        "Pipe",
        [fresh_first, approving_agent("Gated", "A1", shop2, resumed=True), say("Last", "fin")],
    )
    restored = TeamInterruption.from_json(saved)
    assert restored.prompt == ["go", "now"]
    final = await fresh.resume_async(restored, approve=True)
    assert final.output == "fin" and shop2.refunds == [("A1", 10)]
    assert fresh_first.model.calls == []  # type: ignore[attr-defined]
    assert final.usage.total_tokens == 20
    assert shop.refunds == []


async def test_missing_unknown_and_conflicting_decisions_are_rejected() -> None:
    shop = Shop()
    team = ParallelAgent(
        "P", [approving_agent("A", "1", shop, "a1"), approving_agent("B", "2", shop, "b1")]
    )
    paused = await team.run_async("go")
    inter = paused.interruption
    assert inter is not None and set(inter.pending) == {"0", "1"}
    with pytest.raises(SDKValidationError, match="1:b1"):
        await team.resume_async(inter, approve=["0:a1"])
    with pytest.raises(SDKValidationError, match="missing"):
        await team.resume_async(inter)
    with pytest.raises(SDKValidationError, match="unknown"):
        await team.resume_async(inter, approve=["0:a1", "1:b1", "9:zz"])
    with pytest.raises(SDKValidationError, match="both"):
        await team.resume_async(inter, approve=["0:a1", "1:b1"], reject={"0:a1": "no"})
    with pytest.raises(ApprovalDecisionError, match="does not match"):
        await team.resume_async(inter, approve={"0:a1": "bad", "1:b1": "bad"})
    other = ParallelAgent("Other", [say("X", "x")])
    with pytest.raises(ApprovalDecisionError, match="belongs to team"):
        await other.resume_async(inter, approve=True)
    assert shop.refunds == []  # every refusal happened before anything executed

    final = await team.resume_async(inter, approve=["0:a1"], reject={"1:b1": "too much"})
    assert final.status == "completed" and shop.refunds == [("1", 10)]


async def test_parallel_pause_lets_running_members_finish_and_collects_all_paused() -> None:
    shop = Shop()
    quick = say("Quick", "q")
    team = ParallelAgent(
        "P",
        [approving_agent("A", "1", shop, "a1"), quick, approving_agent("B", "2", shop, "b1")],
    )
    paused = await team.run_async("go")
    assert paused.status == "paused"
    assert [s.member for s in paused.steps] == ["A", "Quick", "B"]
    inter = paused.interruption
    assert inter is not None
    assert set(inter.pending) == {"0", "2"} and set(inter.completed) == {"1"}
    final = await team.resume_async(inter, approve=True)
    assert final.output == {"A": "A done", "Quick": "q", "B": "B done"}
    assert len(quick.model.calls) == 1  # type: ignore[attr-defined]
    assert final.usage.total_tokens == 5 + 10 + 10


async def test_parallel_does_not_start_queued_members_after_a_pause() -> None:
    shop = Shop()
    queued = say("Queued", "later")
    team = ParallelAgent("P", [approving_agent("A", "1", shop, "a1"), queued], max_concurrency=1)
    paused = await team.run_async("go")
    assert paused.status == "paused" and queued.model.calls == []  # type: ignore[attr-defined]
    assert [s.member for s in paused.steps] == ["A"]
    final = await team.resume_async(paused.interruption, approve=True)  # type: ignore[arg-type]
    assert final.output == {"A": "A done", "Queued": "later"}


async def test_nested_teams_flatten_paths_and_resume_through_them() -> None:
    shop = Shop()
    inner = ParallelAgent("Inner", [approving_agent("A", "1", shop, "a1"), say("Fast", "f")])
    tail = say("Tail", "end")
    team = SequentialAgent("Outer", [say("Head", "h"), inner, tail])
    paused = await team.run_async("go")
    inter = paused.interruption
    assert inter is not None
    assert [k for k, _ in inter.pending_calls()] == ["1/0:a1"]
    assert set(inter.completed) == {"0", "1/1"}
    assert inter.nested_prompts == {"1": "h"}
    restored = TeamInterruption.from_json(inter.to_json())
    assert restored.nested_prompts == {"1": "h"}
    final = await team.resume_async(restored, approve=restored.pending_digests())
    assert final.output == "end" and shop.refunds == [("1", 10)]
    assert tail.model.calls[0]["messages"][-1].content == '{"A": "A done", "Fast": "f"}'  # type: ignore[attr-defined]
    assert final.usage.total_tokens == 5 + 10 + 5 + 5


async def test_resumed_team_can_pause_again() -> None:
    shop = Shop()

    @tool(requires_approval=True)
    def refund(order_id: str, amount: int) -> str:
        """Refund an order."""
        shop.refunds.append((order_id, amount))
        return "ok"

    agent = say(
        "Gated",
        call("refund", "r1", order_id="1", amount=1),
        call("refund", "r2", order_id="2", amount=2),
        "finished",
        tools=[refund],
    )
    team = SequentialAgent("S", [agent, say("After", "after")])
    first = await team.run_async("go")
    second = await team.resume_async(first.interruption, approve=True)  # type: ignore[arg-type]
    assert second.status == "paused" and shop.refunds == [("1", 1)]
    assert [k for k, _ in second.interruption.pending_calls()] == ["0:r2"]  # type: ignore[union-attr]
    third = await team.resume_async(second.interruption, approve=True)  # type: ignore[arg-type]
    assert third.output == "after" and shop.refunds == [("1", 1), ("2", 2)]
    assert third.usage.total_tokens == 15 + 5


async def test_loop_pause_resumes_and_keeps_counting_iterations() -> None:
    shop = Shop()
    attempts = {"n": 0}

    @tool(requires_approval=True)
    def refund(order_id: str, amount: int) -> str:
        """Refund an order."""
        shop.refunds.append((order_id, amount))
        return "ok"

    def handler(messages: Any, tools: Any) -> Any:
        last = messages[-1]
        if last.role == "tool":
            return "refund done"
        attempts["n"] += 1
        if attempts["n"] == 2:
            return call("refund", "r", order_id="x", amount=1)
        return f"try {attempts['n']}"

    agent = Agent(
        name="W",
        instructions="",
        model=FakeModel(handler=handler, default_usage=USAGE),
        tools=[refund],
    )
    seen: list[str] = []

    def until(result: Any) -> bool:
        seen.append(result.final_output)
        return result.final_output == "refund done"

    team = LoopAgent("L", agent, max_iterations=3, until=until)
    paused = await team.run_async("go")
    assert paused.status == "paused" and [s.path for s in paused.steps] == ["iter-1", "iter-2"]
    assert seen == ["try 1"]
    final = await team.resume_async(paused.interruption, approve=True)  # type: ignore[arg-type]
    assert [s.path for s in final.steps] == ["iter-1", "iter-2"]
    assert final.output == "refund done" and seen == ["try 1", "refund done"]
    assert shop.refunds == [("x", 1)]
    assert attempts["n"] == 2  # iteration 1 was not run again
    assert final.usage.total_tokens == 15


def test_sync_resume_wrapper() -> None:
    shop = Shop()
    team = SequentialAgent("S", [approving_agent("G", "1", shop)])
    paused = team.run("go")
    assert paused.interruption is not None
    done = team.resume(paused.interruption, approve=True)
    assert done.status == "completed" and shop.refunds == [("1", 10)]


# ---------------------------------------------------------------- as_tool


async def test_team_as_tool_passes_deps_and_adds_usage() -> None:
    team = SequentialAgent("Research Team", [say("A", "a"), say("B", '{"title": "T"}')])
    team_tool = team.as_tool(description="Researches.")
    assert team_tool.name == "research_team" and team_tool.description == "Researches."
    caller = Agent(
        name="Caller",
        instructions="",
        tools=[team_tool],
        model=FakeModel(
            responses=[call("research_team", "t", request="tides"), "done"], default_usage=USAGE
        ),
    )
    result = await caller.run_async("go")
    out = next(m.content for m in result.messages if m.role == "tool")
    assert out == '{"title": "T"}'
    assert result.usage.total_tokens == 5 * 2 + 10  # caller's two turns + the team's two runs


async def test_team_as_tool_raises_clearly_when_the_team_pauses() -> None:
    shop = Shop()
    last = say("Last", "never")
    team = SequentialAgent("T", [approving_agent("G", "1", shop), last])
    caller = Agent(
        name="Caller",
        instructions="",
        tools=[team.as_tool()],
        model=FakeModel(responses=[call("t", "t1", request="x"), "gave up"], default_usage=USAGE),
    )
    with pytest.raises(ToolError, match="need human approval") as excinfo:
        await caller.run_async("go")
    assert "Nothing after the pause ran" in str(excinfo.value)
    assert shop.refunds == [] and last.model.calls == []  # type: ignore[attr-defined]


# ---------------------------------------------------------------- MCP


async def test_team_served_over_mcp_and_paused_team_is_refused() -> None:
    shop = Shop()
    ok = SequentialAgent(
        "Pipeline", [say("A", "a"), say("B", "final")], description="Runs the pipeline."
    )
    gated = SequentialAgent("Gated Team", [approving_agent("G", "1", shop)])
    fan = ParallelAgent("Fan", [say("X", "x"), say("Y", "y")])
    server = AgentMCPServer("desk", agents=[ok, gated, fan])
    app = server.http_app()
    remote = MCPServer.http("http://testserver/mcp", transport=httpx.ASGITransport(app=app))
    async with remote:
        definitions = {d.name: d for d in await remote.list_tools()}
        assert set(definitions) == {"pipeline", "gated_team", "fan"}
        assert definitions["pipeline"].description == "Runs the pipeline."
        tools = {t.name: t for t in await remote.tools()}
        caller = Agent(
            name="Caller",
            instructions="",
            tools=list(tools.values()),
            model=FakeModel(
                responses=[
                    [
                        ToolCall(id="p", name="pipeline", arguments={"request": "go"}),
                        ToolCall(id="g", name="gated_team", arguments={"request": "go"}),
                        ToolCall(id="f", name="fan", arguments={"request": "go"}),
                    ],
                    "done",
                ]
            ),
        )
        result = await caller.run_async("help")
    out = {m.tool_call_id: m.content for m in result.messages if m.role == "tool"}
    assert out["p"] == "final"
    assert json.loads(out["f"] or "") == {"X": "x", "Y": "y"}
    assert "Stopped before refund" in (out["g"] or "")
    assert "need human approval" in (out["g"] or "") and "already ran" in (out["g"] or "")
    assert shop.refunds == []


# ---------------------------------------------------------------- review regressions


async def test_team_token_limit_covers_the_whole_team() -> None:
    from nexura_agents.limits import UsageLimits

    team = SequentialAgent(
        "T",
        [say("a", "x"), say("b", "y"), say("c", "z")],
        runner=Runner(usage_limits=UsageLimits(total_tokens_limit=8)),
    )
    with pytest.raises(UsageLimitExceededError):
        await team.run_async("go")


async def test_team_result_final_output_is_text_for_nested_members() -> None:
    inner = ParallelAgent("P", [say("a", "x"), say("b", "y")])
    seen: list[str] = []

    def handoff(original: Any, previous: Any) -> str:
        seen.append(previous.final_output)
        return "next"

    result = await SequentialAgent("S", [inner, say("c", "z")], handoff=handoff).run_async("go")
    assert seen == [json.dumps({"a": "x", "b": "y"})]
    assert result.final_output == "z"


def test_team_as_tool_accepts_runner_and_teams_have_repr() -> None:
    import inspect

    assert "runner" in inspect.signature(Team.as_tool).parameters
    team = SequentialAgent("S", [say("a", "x")])
    assert repr(team) == "SequentialAgent(name='S', members=['a'])"
    assert team.as_tool(runner=Runner()).name == "s"


async def test_mcp_paused_team_does_not_claim_nothing_ran() -> None:
    ran: list[str] = []

    @tool
    def send_email(to: str) -> str:
        """Send."""
        ran.append(to)
        return "sent"

    @tool(requires_approval=True)
    def refund(order: str) -> str:
        """Refund."""
        return "r"

    first = Agent(
        name="First",
        instructions="",
        tools=[send_email],
        model=FakeModel(responses=[call("send_email", "1", to="a@b"), "mailed"]),
    )
    second = Agent(
        name="Second",
        instructions="",
        tools=[refund],
        model=FakeModel(responses=[call("refund", "2", order="o"), "x"]),
    )
    app = AgentMCPServer("s", agents=[SequentialAgent("Flow", [first, second])]).http_app()
    remote = MCPServer.http("http://testserver/mcp", transport=httpx.ASGITransport(app=app))
    async with remote:
        t = (await remote.tools())[0]
        caller = Agent(
            name="C",
            instructions="",
            tools=[t],
            model=FakeModel(responses=[call("flow", "x", request="go"), "ok"]),
        )
        res = await caller.run_async("hi")
    msg = [m.content for m in res.messages if m.role == "tool"][0]
    assert ran == ["a@b"]
    assert "Nothing was executed" not in msg
    assert "already ran" in msg
