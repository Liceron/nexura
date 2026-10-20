"""tool_use_behavior: a tool result can be the final output without another model call."""

from __future__ import annotations

from typing import Any

import pytest
from nexura_agents import (
    Agent,
    FakeModel,
    MemoryCheckpointStore,
    MemorySessionStore,
    ModelRetry,
    Runner,
    StopAtTools,
    ToolCall,
    tool,
)
from nexura_agents.errors import OutputValidationError, SDKValidationError
from nexura_agents.guardrails import GuardrailResult
from nexura_agents.hooks import AgentHooks
from nexura_agents.runner import RunEventType
from pydantic import BaseModel


@tool
def lookup(q: str) -> str:
    """Look up an order."""
    return f"order {q}: shipped"


@tool
def note(text: str) -> str:
    """Write a note."""
    return "noted"


def calls(*pairs: tuple[str, dict[str, Any]]) -> list[ToolCall]:
    return [ToolCall(id=f"c{i}", name=n, arguments=a) for i, (n, a) in enumerate(pairs)]


def agent(model: FakeModel, behavior: Any, tools: list[Any] | None = None, **kw: Any) -> Agent:
    return Agent(
        name="A",
        instructions="i",
        model=model,
        tools=tools or [lookup, note],
        tool_use_behavior=behavior,
        **kw,
    )


async def test_default_calls_the_model_again() -> None:
    model = FakeModel(responses=[calls(("lookup", {"q": "1"})), "It shipped."])
    result = await Runner().run_async(agent(model, "run_llm_again"), "where?")
    assert result.output == "It shipped."
    assert len(model.calls) == 2


async def test_stop_on_first_tool_returns_the_tool_result() -> None:
    model = FakeModel(responses=[calls(("lookup", {"q": "1"}))])
    result = await Runner().run_async(agent(model, "stop_on_first_tool"), "where?")
    assert result.status == "completed"
    assert result.output == "order 1: shipped"
    assert len(model.calls) == 1
    assert result.messages[-1].role == "tool"


async def test_stop_on_first_tool_uses_the_first_call_of_a_parallel_batch() -> None:
    model = FakeModel(responses=[calls(("note", {"text": "x"}), ("lookup", {"q": "2"}))])
    result = await Runner().run_async(
        agent(model, "stop_on_first_tool", parallel_tool_calls=True), "go"
    )
    assert result.output == "noted"
    tool_results = [m.content for m in result.messages if m.role == "tool"]
    assert tool_results == ["noted", "order 2: shipped"]  # every call in the batch still ran


async def test_stop_at_tools_only_stops_for_named_tools() -> None:
    model = FakeModel(
        responses=[calls(("note", {"text": "x"})), calls(("lookup", {"q": "3"})), "unused"]
    )
    result = await Runner().run_async(agent(model, StopAtTools(["lookup"])), "go")
    assert result.output == "order 3: shipped"
    assert len(model.calls) == 2


async def test_failed_call_does_not_end_the_run() -> None:
    attempts = {"n": 0}

    @tool(max_retries=2)
    def flaky(q: str) -> str:
        """Flaky."""
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise ModelRetry("try q=2")
        return f"ok {q}"

    model = FakeModel(responses=[calls(("flaky", {"q": "1"})), calls(("flaky", {"q": "2"}))])
    result = await Runner().run_async(agent(model, "stop_on_first_tool", tools=[flaky]), "go")
    assert result.output == "ok 2"
    assert len(model.calls) == 2


async def test_rejected_call_does_not_end_the_run() -> None:
    @tool(requires_approval=True)
    def refund(amount: int) -> str:
        """Refund."""
        return f"refunded {amount}"

    model = FakeModel(responses=[calls(("refund", {"amount": 5})), "I could not refund."])
    a = agent(model, StopAtTools(["refund"]), tools=[refund])
    runner = Runner()
    paused = await runner.run_async(a, "refund me")
    assert paused.interruption is not None
    done = await runner.resume_async(a, paused.interruption, reject={"c0": "too large"})
    assert done.output == "I could not refund."


async def test_approved_call_ends_the_run_on_resume() -> None:
    @tool(requires_approval=True)
    def refund(amount: int) -> str:
        """Refund."""
        return f"refunded {amount}"

    model = FakeModel(responses=[calls(("refund", {"amount": 5}))])
    a = agent(model, StopAtTools(["refund"]), tools=[refund])
    runner = Runner()
    paused = await runner.run_async(a, "refund me")
    assert paused.interruption is not None
    done = await runner.resume_async(
        a, paused.interruption, approve=paused.interruption.pending_digests()
    )
    assert done.output == "refunded 5"
    assert len(model.calls) == 1


async def test_output_type_parses_the_tool_result() -> None:
    class Order(BaseModel):
        id: str
        status: str

    @tool
    def fetch(id: str) -> dict[str, str]:
        """Fetch."""
        return {"id": id, "status": "shipped"}

    model = FakeModel(responses=[calls(("fetch", {"id": "7"}))])
    a = agent(model, "stop_on_first_tool", tools=[fetch], output_type=Order)
    result = await Runner().run_async(a, "go")
    assert result.output == Order(id="7", status="shipped")


async def test_tool_result_not_matching_output_type_fails_clearly() -> None:
    class Order(BaseModel):
        id: str

    model = FakeModel(responses=[calls(("lookup", {"q": "1"}))])
    a = agent(model, "stop_on_first_tool", output_type=Order)
    with pytest.raises(OutputValidationError, match="tool result chosen as the final output"):
        await Runner().run_async(a, "go")


async def test_session_and_events_match_a_normal_completion() -> None:
    model = FakeModel(responses=[calls(("lookup", {"q": "1"}))])
    store = MemorySessionStore()
    runner = Runner(session_store=store)
    events = [
        e
        async for e in runner.stream_async(agent(model, "stop_on_first_tool"), "go", session_id="s")
    ]
    kinds = [e.type for e in events]
    assert RunEventType.FINAL_OUTPUT in kinds and kinds[-1] == RunEventType.RUN_COMPLETED
    saved = await store.get("s")
    assert saved is not None
    assert saved.messages[-1].content == "order 1: shipped"


async def test_output_guardrail_sees_and_can_rewrite_the_tool_output() -> None:
    seen: list[Any] = []

    def redact(ctx: Any, output: Any) -> GuardrailResult:
        seen.append(output)
        return GuardrailResult(tripwire=False, replacement="[redacted]")

    model = FakeModel(responses=[calls(("lookup", {"q": "1"}))])
    a = agent(model, "stop_on_first_tool", output_guardrails=[redact])
    result = await Runner().run_async(a, "go")
    assert seen == ["order 1: shipped"]
    assert result.output == "[redacted]"
    assert result.messages[-1].content == "order 1: shipped"  # the tool message is not rewritten


async def test_handoff_in_the_batch_is_not_a_final_output() -> None:
    other_model = FakeModel(responses=["handled by B"])
    b = Agent(name="B", instructions="i", model=other_model)
    model = FakeModel(responses=[calls(("transfer_to_b", {}))])
    a = agent(model, "stop_on_first_tool", handoffs=[b])
    result = await Runner().run_async(a, "go")
    assert result.output == "handled by B"


async def test_checkpointed_run_cleans_up_after_a_tool_final_output() -> None:
    store = MemoryCheckpointStore()
    model = FakeModel(responses=[calls(("lookup", {"q": "1"}))])
    result = await Runner(checkpoint_store=store).run_async(
        agent(model, "stop_on_first_tool"), "go"
    )
    assert result.output == "order 1: shipped"
    assert await store.list_incomplete() == []


def test_invalid_behaviours_are_rejected() -> None:
    model = FakeModel(responses=["x"])
    with pytest.raises(SDKValidationError, match="tool_use_behavior must be"):
        agent(model, "stop")
    with pytest.raises(SDKValidationError, match="does not have: missing"):
        agent(model, StopAtTools(["lookup", "missing"]))
    with pytest.raises(SDKValidationError):
        StopAtTools("lookup")  # type: ignore[arg-type]
    with pytest.raises(SDKValidationError):
        StopAtTools([])


# ---------------------------------------------------------------- durable recovery (review fixes)


class Crash(BaseException):
    """Simulated process death."""


def _crashing_tools(crash_b: dict[str, bool]) -> list[Any]:
    @tool
    def a(x: str) -> str:
        """Tool a."""
        return f"a:{x}"

    @tool(idempotent=True)
    def b(x: str) -> str:
        """Tool b."""
        if crash_b["on"]:
            raise Crash
        return f"b:{x}"

    return [a, b]


def _two_calls() -> list[ToolCall]:
    return calls(("a", {"x": "1"}), ("b", {"x": "2"}))


async def _crash_in_b(behavior: Any) -> tuple[MemoryCheckpointStore, str]:
    store = MemoryCheckpointStore()
    first = Agent(
        name="A",
        instructions="i",
        model=FakeModel(responses=[_two_calls()]),
        tools=_crashing_tools({"on": True}),
        tool_use_behavior=behavior,
    )
    with pytest.raises(Crash):
        await Runner(checkpoint_store=store).run_async(first, "go")
    return store, next(iter(store._data))


@pytest.mark.parametrize("behavior", ["stop_on_first_tool", StopAtTools(["a"])])
async def test_recovery_mid_batch_counts_calls_finished_before_the_crash(behavior: Any) -> None:
    store, run_id = await _crash_in_b(behavior)
    model = FakeModel(responses=["model called again"])
    again = Agent(
        name="A",
        instructions="i",
        model=model,
        tools=_crashing_tools({"on": False}),
        tool_use_behavior=behavior,
    )
    result = await Runner(checkpoint_store=store).recover_async(again, run_id)
    assert result.output == "a:1"  # as in an uninterrupted run: first call in model order
    assert model.calls == []


class _CrashOnRunEnd(AgentHooks):
    """Crashes after the "completed" checkpoint, before the run reports completion."""

    armed = True

    async def on_run_end(self, ctx: Any, output: Any) -> None:
        if self.armed:
            raise Crash


@pytest.mark.parametrize("rewrite", [False, True])
async def test_completed_recovery_replays_the_output_the_caller_got(rewrite: bool) -> None:
    def redact(ctx: Any, output: Any) -> GuardrailResult:
        return GuardrailResult(tripwire=False, replacement="[redacted]")

    store, hooks = MemoryCheckpointStore(), _CrashOnRunEnd()
    a = Agent(
        name="A",
        instructions="i",
        model=FakeModel(responses=[_two_calls()]),
        tools=_crashing_tools({"on": False}),
        tool_use_behavior="stop_on_first_tool",
        output_guardrails=[redact] if rewrite else [],
    )
    with pytest.raises(Crash):
        await Runner(checkpoint_store=store, hooks=hooks).run_async(a, "go")
    run_id = next(iter(store._data))
    hooks.armed = False
    result = await Runner(checkpoint_store=store, hooks=hooks).recover_async(a, run_id)
    # Not the last tool message ("b:2"), and the guardrail rewrite is kept.
    assert result.output == ("[redacted]" if rewrite else "a:1")
