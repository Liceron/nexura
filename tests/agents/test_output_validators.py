"""Output validators: check the final answer and ask the model to try again (F28)."""

from __future__ import annotations

from typing import Any

import pytest
from nexura_agents import (
    Agent,
    FakeModel,
    ModelRetry,
    Runner,
    StopAtTools,
    ToolCall,
    keep_last_turns,
    tool,
)
from nexura_agents.errors import OutputValidationError, SDKValidationError
from nexura_agents.events import RunEventType
from nexura_agents.run_context import RunContext
from pydantic import BaseModel


class Quote(BaseModel):
    product: str
    price: float


def positive(quote: Quote) -> Quote:
    if quote.price <= 0:
        raise ModelRetry("price must be positive")
    return quote


async def test_validator_retry_then_success() -> None:
    model = FakeModel(
        responses=['{"product": "pen", "price": -1}', '{"product": "pen", "price": 2.5}']
    )
    agent = Agent(
        name="A", instructions="i", model=model, output_type=Quote, output_validators=[positive]
    )
    events = [e async for e in Runner().stream_async(agent, "quote")]
    done = [e for e in events if e.type == RunEventType.RUN_COMPLETED][0]
    assert done.data["output_value"] == Quote(product="pen", price=2.5)
    retries = [e for e in events if e.type == RunEventType.RETRY]
    assert retries[0].data == {"reason": "output_validator", "error": "price must be positive"}
    feedback = model.calls[1]["messages"][-1]
    assert feedback.role == "user"
    assert (
        feedback.content
        == "Your answer was not accepted: price must be positive\nPlease answer again."
    )


async def test_retries_exhausted_raise_output_validation_error() -> None:
    model = FakeModel(responses=['{"product": "pen", "price": -1}'], repeat_last=True)
    agent = Agent(
        name="A",
        instructions="i",
        model=model,
        output_type=Quote,
        output_validators=[positive],
        output_retries=2,
    )
    with pytest.raises(OutputValidationError, match="price must be positive") as info:
        await Runner().run_async(agent, "quote")
    assert info.value.attempts == 3
    assert len(model.calls) == 3


async def test_validators_chain_and_may_change_a_text_answer() -> None:
    seen: list[Any] = []

    def strip(text: str) -> str:
        return text.strip()

    async def with_ctx(ctx: RunContext[Any], text: str) -> str:
        seen.append((ctx.deps, text))
        return text.upper()

    model = FakeModel(responses=["  hello  "])
    agent = Agent(name="A", instructions="i", model=model, output_validators=[strip, with_ctx])
    result = await Runner().run_async(agent, "hi", deps="d")
    assert result.output == "HELLO"
    assert seen == [("d", "hello")]
    assert result.messages[-1].content == "HELLO"  # stored history matches the answer


async def test_other_validator_errors_fail_the_run() -> None:
    def broken(text: str) -> str:
        raise KeyError("bug")

    agent = Agent(
        name="A", instructions="i", model=FakeModel(responses=["x"]), output_validators=[broken]
    )
    with pytest.raises(KeyError):
        await Runner().run_async(agent, "hi")


async def test_validator_on_a_tool_result_output_cannot_retry() -> None:
    @tool
    def price(product: str) -> str:
        """Price."""
        return '{"product": "pen", "price": 0}'

    agent = Agent(
        name="A",
        instructions="i",
        model=FakeModel(
            responses=[[ToolCall(id="c1", name="price", arguments={"product": "pen"})]]
        ),
        tools=[price],
        output_type=Quote,
        tool_use_behavior=StopAtTools(["price"]),
        output_validators=[positive],
    )
    with pytest.raises(OutputValidationError, match="rejected the tool result"):
        await Runner().run_async(agent, "quote")


async def test_validator_feedback_stays_in_the_current_turn_when_trimming() -> None:
    model = FakeModel(
        responses=['{"product": "pen", "price": -1}', '{"product": "pen", "price": 1}']
    )
    agent = Agent(
        name="A",
        instructions="i",
        model=model,
        output_type=Quote,
        output_validators=[positive],
        history_processors=[keep_last_turns(1)],
    )
    await Runner().run_async(agent, "ORIGINAL QUESTION")
    retry_call = [m.content for m in model.calls[1]["messages"]]
    assert "ORIGINAL QUESTION" in retry_call


@pytest.mark.parametrize("bad", [positive, "x", [1]])
def test_output_validators_are_validated(bad: Any) -> None:
    with pytest.raises(SDKValidationError, match="output[ _]validator"):
        Agent(name="A", instructions="i", model=FakeModel(), output_validators=bad)


async def test_recovered_run_keeps_the_retry_budget_it_already_spent() -> None:
    from nexura_agents import MemoryCheckpointStore

    class Crash(BaseException):
        pass

    def reject_bad(text: str) -> str:
        if text != "good":
            raise ModelRetry("nope")
        return text

    calls = {"n": 0}

    def handler(messages: Any, tools: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 2:  # crash after one retry was spent
            raise Crash
        return "bad"

    store = MemoryCheckpointStore()
    first = Agent(
        name="A",
        instructions="i",
        model=FakeModel(handler=handler),
        output_retries=1,
        output_validators=[reject_bad],
    )
    with pytest.raises(Crash):
        await Runner(checkpoint_store=store).run_async(first, "go")
    (run_id,) = await store.list_incomplete()
    again = Agent(
        name="A",
        instructions="i",
        model=FakeModel(responses=["bad", "good"]),
        output_retries=1,
        output_validators=[reject_bad],
    )
    with pytest.raises(OutputValidationError):
        await Runner(checkpoint_store=store).recover_async(again, run_id)


async def test_cancel_interrupts_a_slow_output_validator() -> None:
    import asyncio
    import time

    from nexura_agents import CancelToken

    async def slow(output: Any) -> Any:
        await asyncio.sleep(3)
        return output

    agent = Agent(
        name="A", instructions="i", model=FakeModel(responses=["hi"]), output_validators=[slow]
    )
    token = CancelToken()
    asyncio.get_running_loop().call_later(0.1, token.cancel)
    started = time.monotonic()
    result = await Runner().run_async(agent, "x", cancel=token)
    assert result.status == "cancelled" and time.monotonic() - started < 1.5


async def test_a_resumed_run_keeps_the_retry_budget_it_already_spent() -> None:
    @tool(requires_approval=True)
    def act() -> str:
        """Act."""
        return "ok"

    def never(ctx: Any, output: Any) -> Any:
        raise ModelRetry("not good")

    agent = Agent(
        name="A",
        instructions="i",
        model=FakeModel(
            responses=["bad1", [ToolCall(id="c1", name="act", arguments={})], "bad2", "bad3"]
        ),
        tools=[act],
        output_retries=1,
        output_validators=[never],
    )
    runner = Runner()
    paused = await runner.run_async(agent, "go")  # the one retry is spent before the pause
    assert paused.status == "paused"
    events: list[Any] = []
    with pytest.raises(OutputValidationError):
        async for event in runner.resume_stream_async(agent, paused.interruption, approve=True):
            events.append(event)
    assert not [e for e in events if e.type == RunEventType.RETRY]
