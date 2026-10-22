"""A6 S11 review repro R1: a call a human REJECTED in a resume is executed after a crash in the
same batch.

The resume approves c1 and rejects c2 (both gated pays). The crash comes after c1 ran and
before c2's rejection was recorded. recover() rebuilds fresh decisions: the claim's
"reject" map (recorded in the journal by S11) is never read, c2 has no intent, so it is
treated as "never dispatched, just run" (K1) and pays.
"""

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


def make_agent(counter: list[int], first: bool) -> Agent:
    @tool(requires_approval=True)
    def pay(amount: int) -> str:
        """Pay (needs approval)."""
        counter.append(amount)
        return "paid"

    responses: list[Any] = ["done"]
    if first:
        responses.insert(
            0,
            [
                ToolCall(id="c1", name="pay", arguments={"amount": 1}),
                ToolCall(id="c2", name="pay", arguments={"amount": 2}),
            ],
        )
    return Agent(name="A", instructions="i", model=FakeModel(responses=responses), tools=[pay])


async def test_rejected_call_is_not_executed_after_crash() -> None:
    store, paid = MemoryCheckpointStore(), []
    runner = Runner(checkpoint_store=store)
    paused = await runner.run_async(make_agent(paid, True), "go")
    assert paused.status == "paused"
    digests = paused.interruption.pending_digests()

    class Hooks(AgentHooks):
        async def after_tool(self, ctx: Any, call: Any, result: str) -> None:
            if call.id == "c1":
                raise Crash("after c1 ran, before c2's rejection is recorded")

    runner.hooks = Hooks()
    with pytest.raises(Crash):
        await runner.resume_async(
            make_agent(paid, False),
            paused.interruption,
            approve={"c1": digests["c1"]},
            reject={"c2": "do NOT pay 2"},
        )
    assert paid == [1]
    result = await Runner(checkpoint_store=store).recover_async(
        make_agent(paid, False), paused.run_id
    )
    if result.status == "paused":
        from nexura_agents.durable import ResolvedOutcome

        result = await Runner(checkpoint_store=store).resume_async(
            make_agent(paid, False),
            result.interruption,
            outcomes={"c1": ResolvedOutcome(result="paid", resolution="confirmed")},
        )
    # Expected: c2 stays rejected (the claim recorded it). Actual: c2 pays.
    assert 2 not in paid, f"rejected call executed after recover: paid={paid}, {result.status}"


async def test_rejected_call_is_not_executed_by_recover_alone() -> None:
    """Same, but c1 finished cleanly (outcome committed): recover() itself pays c2."""
    store, paid = MemoryCheckpointStore(), []
    runner = Runner(checkpoint_store=store)
    paused = await runner.run_async(make_agent(paid, True), "go")
    digests = paused.interruption.pending_digests()
    original = Runner._checkpoint

    async def crash_after_c1(self: Runner, state: Any, phase: str) -> None:
        await original(self, state, phase)
        if any(m.role == "tool" and m.tool_call_id == "c1" for m in state.messages):
            raise Crash("after c1's result was checkpointed, before c2's rejection")

    Runner._checkpoint = crash_after_c1  # type: ignore[method-assign]
    try:
        with pytest.raises(Crash):
            await runner.resume_async(
                make_agent(paid, False),
                paused.interruption,
                approve={"c1": digests["c1"]},
                reject={"c2": "do NOT pay 2"},
            )
    finally:
        Runner._checkpoint = original  # type: ignore[method-assign]
    assert paid == [1]
    result = await Runner(checkpoint_store=store).recover_async(
        make_agent(paid, False), paused.run_id
    )
    assert 2 not in paid, f"recover() executed the rejected call: paid={paid}, {result.status}"
