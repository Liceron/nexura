"""Round 2, m3: an approval whose decisions are not exact JSON (a tuple in ApproveWith
arguments) is consumed at once, without a claim.

Round 3 (F3-B1): that path no longer exists. Tuples are normalised to lists (an ordinary
claim), and a journaled resume refuses decisions with no exact JSON form before it takes
the run. Test 1 therefore uses a value with no JSON form (NaN) and expects that refusal
instead of a crash; its assertions (nothing paid, the run waiting) are unchanged. Test 3
covers the normalised tuple.

1. Safety: a crash after the batch checkpoint, before the approved call's intent, must not
   send the call unapproved on recover (no same-turn claim => asked again). Expected pass.
2. The fallback consumes BEFORE taking the run, the round-1 M1 pattern: when it races a
   normal resume of the same approval, the approval can be spent with nothing dispatched.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from nexura_agents import (
    Agent,
    AgentHooks,
    ApproveWith,
    FakeModel,
    MemoryCheckpointStore,
    Runner,
    ToolCall,
    tool,
)
from nexura_agents._agent_journal import AgentJournal
from nexura_agents.errors import ApprovalDecisionError


class Crash(BaseException):
    pass


def make_agent(paid: list[Any], first: bool) -> Agent:
    @tool(requires_approval=True)
    def pay(items: list[float]) -> str:
        """Pay for items."""
        paid.append(list(items))
        return "paid"

    responses: list[Any] = ["done"] * 3
    if first:
        responses.insert(0, [ToolCall(id="c1", name="pay", arguments={"items": [1]})])
    return Agent(name="A", instructions="i", model=FakeModel(responses=responses), tools=[pay])


async def test_fallback_consume_then_crash_does_not_send_unapproved() -> None:
    store, paid = MemoryCheckpointStore(), []
    runner = Runner(checkpoint_store=store)
    paused = await runner.run_async(make_agent(paid, True), "go")
    digest = paused.interruption.pending_digests()["c1"]

    class Hooks(AgentHooks):
        async def before_tool(self, ctx: Any, call: Any) -> None:
            raise Crash("after the batch checkpoint, before the intent")

    runner.hooks = Hooks()
    with pytest.raises(ApprovalDecisionError, match="representable as JSON"):
        await runner.resume_async(
            make_agent(paid, False),
            paused.interruption,
            approve={"c1": ApproveWith(arguments={"items": [1, float("nan")]}, digest=digest)},
        )
    result = await Runner(checkpoint_store=store).recover_async(
        make_agent(paid, False), paused.run_id
    )
    assert paid == [], (paid, result.status)
    assert result.status == "paused"


async def test_a_tuple_edit_is_normalised_and_sent_once_under_its_approval() -> None:
    """Round 3: the tuple of test 1's original setup is now plain JSON (a list). A crash after
    the batch checkpoint, before the call's intent: recovery sends it once under the claim."""
    store, paid = MemoryCheckpointStore(), []
    runner = Runner(checkpoint_store=store)
    paused = await runner.run_async(make_agent(paid, True), "go")
    digest = paused.interruption.pending_digests()["c1"]

    class Hooks(AgentHooks):
        async def before_tool(self, ctx: Any, call: Any) -> None:
            raise Crash("after the batch checkpoint, before the intent")

    runner.hooks = Hooks()
    with pytest.raises(Crash):
        await runner.resume_async(
            make_agent(paid, False),
            paused.interruption,
            approve={"c1": ApproveWith(arguments={"items": (1, 2)}, digest=digest)},
        )
    result = await Runner(checkpoint_store=store).recover_async(
        make_agent(paid, False), paused.run_id
    )
    assert result.status == "completed" and paid == [[1, 2]]


async def test_fallback_resume_racing_a_normal_resume_does_not_strand_the_run() -> None:
    store, paid = MemoryCheckpointStore(), []
    paused = await Runner(checkpoint_store=store).run_async(make_agent(paid, True), "go")
    digest = paused.interruption.pending_digests()["c1"]
    entered, gate = asyncio.Event(), asyncio.Event()
    original = AgentJournal.begin

    async def slow_begin(self: AgentJournal, *a: Any, **k: Any) -> None:
        await original(self, *a, **k)
        if not entered.is_set():
            entered.set()
            await gate.wait()  # the normal resume holds the run, before its claim

    AgentJournal.begin = slow_begin  # type: ignore[method-assign]
    try:
        normal = asyncio.create_task(
            Runner(checkpoint_store=store).resume_async(
                make_agent(paid, False), paused.interruption, approve=True
            )
        )
        await entered.wait()
        with pytest.raises(Exception):  # noqa: B017 - the busy fallback resume: any refusal
            await Runner(checkpoint_store=store).resume_async(
                make_agent(paid, False),
                paused.interruption,
                approve={"c1": ApproveWith(arguments={"items": (1, 2)}, digest=digest)},
            )
        gate.set()
        await asyncio.gather(normal, return_exceptions=True)
    finally:
        AgentJournal.begin = original  # type: ignore[method-assign]
    if paid:
        return
    again = await Runner(checkpoint_store=store).recover_async(
        make_agent(paid, False), paused.run_id
    )
    try:
        done = await Runner(checkpoint_store=store).resume_async(
            make_agent(paid, False), again.interruption, approve=True
        )
    except Exception as exc:
        pytest.fail(
            f"run stranded on a spent approval (fallback consumed before taking the run): {exc!r}"
        )
    assert done.status == "completed" and len(paid) == 1
