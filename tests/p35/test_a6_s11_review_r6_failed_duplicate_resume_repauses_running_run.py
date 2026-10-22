"""A6 S11 review repro R6: a refused duplicate resume rewrites the journal of a run that crashed
mid-dispatch.

After a resume dispatched the approved call and the process died inside it (intent, no
outcome, checkpoint tools_pending), a second resume with the same (spent) interruption
now takes the run, records a new claim, fails to consume, and commits ``journal.pause``
with the OLD approval pause reason. Before S11 the duplicate resume was refused before it
touched the run. The journal then says ``paused``/approval while the checkpoint is
tools_pending and an effect is pending: scanners that look for running runs with an
expired lease (service run_liveness, list_recoverable) no longer see it.
"""

from __future__ import annotations

from typing import Any

import pytest
from nexura_agents import Agent, FakeModel, MemoryCheckpointStore, Runner, ToolCall, tool
from nexura_agents.contracts.execution import ExecutionId
from nexura_agents.errors import ApprovalDecisionError


class Crash(BaseException):
    pass


def make_agent(paid: list[int], first: bool, crash: dict[str, bool]) -> Agent:
    @tool(requires_approval=True)
    def pay(amount: int) -> str:
        """Pay (needs approval)."""
        paid.append(amount)
        if crash.pop("now", False):
            raise Crash("died inside the payment")
        return "paid"

    responses: list[Any] = ["done"]
    if first:
        responses.insert(0, [ToolCall(id="c1", name="pay", arguments={"amount": 1})])
    return Agent(name="A", instructions="i", model=FakeModel(responses=responses), tools=[pay])


async def test_refused_duplicate_resume_leaves_the_journal_alone() -> None:
    store, paid = MemoryCheckpointStore(), []
    paused = await Runner(checkpoint_store=store).run_async(make_agent(paid, True, {}), "go")
    with pytest.raises(Crash):
        await Runner(checkpoint_store=store).resume_async(
            make_agent(paid, False, {"now": True}), paused.interruption, approve=True
        )
    journal = await store.open_execution_store()
    before = (await journal.get(ExecutionId("default", paused.run_id))).run
    with pytest.raises(ApprovalDecisionError):
        await Runner(checkpoint_store=store).resume_async(
            make_agent(paid, False, {}), paused.interruption, approve=True
        )
    after = (await journal.get(ExecutionId("default", paused.run_id))).run
    assert (after.status, after.revision) == (before.status, before.revision), (
        f"refused resume rewrote the journal: {before.status}@{before.revision} -> "
        f"{after.status}@{after.revision} (pause_reason={after.state.get('pause_reason')})"
    )


async def test_after_the_refused_resume_recover_still_finds_the_in_doubt_payment() -> None:
    store, paid = MemoryCheckpointStore(), []
    paused = await Runner(checkpoint_store=store).run_async(make_agent(paid, True, {}), "go")
    with pytest.raises(Crash):
        await Runner(checkpoint_store=store).resume_async(
            make_agent(paid, False, {"now": True}), paused.interruption, approve=True
        )
    with pytest.raises(ApprovalDecisionError):
        await Runner(checkpoint_store=store).resume_async(
            make_agent(paid, False, {}), paused.interruption, approve=True
        )
    journal = await store.open_execution_store()
    recoverable = [r.execution_id.run_id for r in await journal.list_recoverable()]
    result = await Runner(checkpoint_store=store).recover_async(
        make_agent(paid, False, {}), paused.run_id
    )
    assert result.status == "paused"  # added: the payment in doubt pauses, not repeated
    assert paid == [1]
    assert paused.run_id in recoverable, "the crashed run dropped out of list_recoverable"
