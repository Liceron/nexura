"""A6 S11 round-3 review (F3-B1): one approved call is never paid twice.

Adapted from ``.autopilot/p35/s11_repros/round3/test_w1_marker_claim_two_live_approvals.py``.
The marker-claim path it attacked was removed (lead direction): decisions are normalised to
JSON at resume (tuples become lists), and a journaled resume refuses decisions that are not
exactly representable before it takes the run or writes anything. Every claim is exact, so
recovery always continues under the original approval and never issues a second one for a
call. The repro's safety assertion (``c1`` paid at most once) is kept in each variant.
"""

from __future__ import annotations

from typing import Any

import pytest
from nexura_agents import (
    Agent,
    ApproveWith,
    FakeModel,
    MemoryCheckpointStore,
    Runner,
    ToolCall,
    tool,
)
from nexura_agents.contracts.execution import ExecutionId, StepIntent
from nexura_agents.errors import ApprovalDecisionError
from nexura_agents.execution_store import ExecutionStep
from nexura_agents.types import RunInterruption


class Crash(BaseException):
    pass


def make_agent(paid: list[Any], responses: list[Any]) -> Agent:
    @tool(requires_approval=True)
    def pay(items: list[float]) -> str:
        """Pay for items."""
        paid.append(list(items))
        return "paid"

    return Agent(name="A", instructions="i", model=FakeModel(responses=responses), tools=[pay])


C1 = [ToolCall(id="c1", name="pay", arguments={"items": [1]})]
C2 = [ToolCall(id="c2", name="pay", arguments={"items": [2]})]


async def test_non_json_decisions_are_refused_before_anything_and_nothing_is_paid() -> None:
    store, paid = MemoryCheckpointStore(), []
    paused = await Runner(checkpoint_store=store).run_async(make_agent(paid, [C1]), "go")
    i1 = paused.interruption
    digest = i1.pending_digests()["c1"]
    journal = await store.open_execution_store()
    before = (await journal.get(ExecutionId("default", paused.run_id))).run
    with pytest.raises(ApprovalDecisionError, match="representable as JSON"):
        await Runner(checkpoint_store=store).resume_async(
            make_agent(paid, ["done"]),
            i1,
            approve={"c1": ApproveWith(arguments={"items": [float("nan")]}, digest=digest)},
        )
    after = (await journal.get(ExecutionId("default", paused.run_id))).run
    assert paid == []
    assert (after.revision, after.fencing_token, after.status) == (
        before.revision,
        before.fencing_token,
        before.status,
    )  # nothing taken or written
    # Nothing was consumed: the same approval with JSON decisions pays c1 once.
    done = await Runner(checkpoint_store=store).resume_async(
        make_agent(paid, ["done"]),
        i1,
        approve={"c1": ApproveWith(arguments={"items": [1.5]}, digest=digest)},
    )
    assert done.status == "completed"
    assert paid == [[1.5]]
    assert paid.count([1]) <= 1  # the repro's "never paid twice"


async def test_a_crash_between_the_claim_and_the_consume_continues_under_the_same_approval() -> (
    None
):
    """The repro's scenario with a tuple edit (now normalised): recover continues under I1
    (no second approval exists), c1 is paid once, and I1 cannot be used again."""
    store, paid = MemoryCheckpointStore(), []
    runner = Runner(checkpoint_store=store)
    paused = await runner.run_async(make_agent(paid, [C1]), "go")
    i1 = paused.interruption
    digest = i1.pending_digests()["c1"]

    async def die(*args: Any) -> None:
        raise Crash("claim committed, approval not consumed yet")

    runner._consume_approval = die  # type: ignore[method-assign]
    with pytest.raises(Crash):
        await runner.resume_async(
            make_agent(paid, ["done"]),
            i1,
            approve={"c1": ApproveWith(arguments={"items": (1,)}, digest=digest)},
        )
    recovered = await Runner(checkpoint_store=store).recover_async(
        make_agent(paid, [C2]), paused.run_id
    )
    assert recovered.status == "paused"  # c1 paid under I1; the model then asks for c2
    assert paid == [[1]]
    assert [c.id for c in recovered.interruption.pending] == ["c2"]
    with pytest.raises(ApprovalDecisionError):
        await Runner(checkpoint_store=store).resume_async(
            make_agent(paid, ["done"]), i1, approve=True
        )
    assert paid.count([1]) <= 1, f"c1 paid twice: {paid}"


def test_an_approval_of_an_already_dispatched_call_is_refused() -> None:
    """Defence in depth: an approval pause whose pending call already has an intent for
    that turn is refused; an effect_unknown or re-approval pause of it is not."""
    from nexura_agents.runner import _REAPPROVE_REASON, _UNKNOWN_REASON, _refuse_claimed

    call = ToolCall(id="c1", name="pay", arguments={"items": [1]})
    steps = {"turn:1/call:c1": ExecutionStep(intent=StepIntent("turn:1/call:c1", "d", "nx-k"))}

    def interruption(reason: str | None, turn: int = 1) -> RunInterruption:
        return RunInterruption(
            run_id="r",
            agent_name="A",
            messages=[],
            pending=[call],
            turn=turn,
            reason=reason,
            interruption_id="i2",
        )

    with pytest.raises(ApprovalDecisionError, match="already dispatched"):
        _refuse_claimed({}, steps, "paused", interruption(None))
    _refuse_claimed({}, steps, "paused", interruption(_UNKNOWN_REASON))
    _refuse_claimed({}, steps, "paused", interruption(_REAPPROVE_REASON))
    _refuse_claimed({}, steps, "paused", interruption(None, turn=2))  # another turn's call
