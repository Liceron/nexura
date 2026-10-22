"""A6 S11 round-2 review repro (minor 4): a claim of an earlier turn never approves a later
call that reuses its id. Copied from .autopilot/p35/s11_repros/round2/test_v1_b1_variants.py."""

from __future__ import annotations

from typing import Any

import pytest
from nexura_agents import (
    Agent,
    AgentHooks,
    FakeModel,
    MemoryCheckpointStore,
    Runner,
    ToolCall,
    tool,
)


class Crash(BaseException):
    pass


async def test_claim_from_an_earlier_turn_does_not_approve_a_later_call() -> None:
    """Turn 1: c1 = pay(1) approved and paid. Turn 2 reuses the id c1 (Gemini-style
    call_{name}_{index}) for pay(500), which was not gated when the model asked (dynamic
    rule) but is gated at recovery. Crash after turn 2's batch checkpoint, before the
    intent. Recover must not send it under turn 1's claim: it is asked for again."""
    store, paid = MemoryCheckpointStore(), []
    gate = {"on": True}

    @tool(requires_approval=lambda ctx, args: gate["on"])
    def pay(amount: int) -> str:
        """Pay."""
        paid.append(amount)
        return "paid"

    t1 = [ToolCall(id="c1", name="pay", arguments={"amount": 1})]
    t2 = [ToolCall(id="c1", name="pay", arguments={"amount": 500})]

    def agent(responses: list[Any]) -> Agent:
        return Agent(name="A", instructions="i", model=FakeModel(responses=responses), tools=[pay])

    class Hooks(AgentHooks):
        async def before_tool(self, ctx: Any, call: Any) -> None:
            if call.arguments.get("amount") == 500:
                raise Crash("after turn 2's batch checkpoint, before the intent")

    runner = Runner(checkpoint_store=store)
    paused = await runner.run_async(agent([t1, t2, "done"]), "go")
    gate["on"] = False  # turn 2's call is not gated when the model asks for it
    runner.hooks = Hooks()
    with pytest.raises(Crash):
        await runner.resume_async(agent([t2, "done"]), paused.interruption, approve=True)
    assert paid == [1]
    gate["on"] = True  # gated at recovery; only turn 1's claim mentions "c1"
    result = await Runner(checkpoint_store=store).recover_async(agent(["done"]), paused.run_id)
    assert 500 not in paid, (paid, result.status)
    assert result.status == "paused", result.status
