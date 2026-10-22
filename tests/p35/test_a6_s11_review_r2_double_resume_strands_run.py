"""A6 S11 review repro R2: two concurrent resumes of the same approval burn it and strand the run.

S11 moved consumption after the run is taken (claim for dispatch). When the run cannot be
taken, the losing resume consumes the approval anyway "to keep the old errors". If the
winner is between its claim commit and its own consume, the loser's consume succeeds, the
winner's fails, the winner re-pauses with the claim cleared, and the run is left paused
on a spent approval: resume refuses it, recover hands the same spent interruption back.
Before S11 (consume first, then take) the first resume always won and dispatched.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from nexura_agents import Agent, FakeModel, MemoryCheckpointStore, Runner, ToolCall, tool
from nexura_agents._agent_journal import AgentJournal
from nexura_agents.errors import ApprovalDecisionError


def make_agent(paid: list[int], first: bool) -> Agent:
    @tool(requires_approval=True)
    def pay(amount: int) -> str:
        """Pay (needs approval)."""
        paid.append(amount)
        return "paid"

    responses: list[Any] = ["done"]
    if first:
        responses.insert(0, [ToolCall(id="c1", name="pay", arguments={"amount": 1})])
    return Agent(name="A", instructions="i", model=FakeModel(responses=responses), tools=[pay])


async def test_concurrent_double_resume_does_not_strand_the_run() -> None:
    store, paid = MemoryCheckpointStore(), []
    paused = await Runner(checkpoint_store=store).run_async(make_agent(paid, True), "go")
    gate, entered = asyncio.Event(), asyncio.Event()
    # Hook a method that exists before and after S11: the winner has taken the run.
    original = AgentJournal.begin

    async def slow_begin(self: AgentJournal, *args: Any, **kwargs: Any) -> None:
        await original(self, *args, **kwargs)
        entered.set()
        await gate.wait()  # the winner holds the run, before its claim/consume

    AgentJournal.begin = slow_begin  # type: ignore[method-assign]
    try:
        first = asyncio.create_task(
            Runner(checkpoint_store=store).resume_async(
                make_agent(paid, False), paused.interruption, approve=True
            )
        )
        await entered.wait()
        # The double click: a second resume of the same approval while the first holds the run.
        with pytest.raises(Exception) as second:  # noqa: PT011 - either refusal is fine
            await Runner(checkpoint_store=store).resume_async(
                make_agent(paid, False), paused.interruption, approve=True
            )
        gate.set()
        outcome = await asyncio.gather(first, return_exceptions=True)
    finally:
        AgentJournal.begin = original  # type: ignore[method-assign]
    # Added when moved into tests/p35: the busy resume did not consume the approval, so the
    # first one finished (review M1).
    assert type(second.value).__name__ in ("RunRecoveryError", "ApprovalDecisionError")
    assert not isinstance(outcome[0], BaseException), outcome
    if paid == [1]:
        return  # one dispatch: fine
    # Nothing was paid. The run must still be continuable by someone.
    again = await Runner(checkpoint_store=store).recover_async(
        make_agent(paid, False), paused.run_id
    )
    assert again.status == "paused"
    try:
        done = await Runner(checkpoint_store=store).resume_async(
            make_agent(paid, False), again.interruption, approve=True
        )
    except ApprovalDecisionError as exc:
        pytest.fail(
            f"run stranded: approval burnt by the losing resume, recover hands back the spent "
            "interruption (same id: "
            f"{again.interruption.interruption_id == paused.interruption.interruption_id}) "
            f"and resume refuses it: {exc}"
        )
    assert done.status == "completed" and paid == [1]
