"""R3-1 reviewer regression: decisions are bound to an approval occurrence."""

from __future__ import annotations

from typing import Any, cast

import pytest
from nexura_agents import (
    Agent,
    FakeModel,
    Harness,
    MemoryCheckpointStore,
    Message,
    RunInterruption,
    Runner,
    ScopedFilesystem,
    ToolCall,
)
from nexura_agents.errors import ApprovalDecisionError


def call(identifier: str, name: str, **arguments: Any) -> Message:
    return Message("assistant", tool_calls=[ToolCall(identifier, name, arguments)])


@pytest.mark.parametrize("capability", ["filesystem", "todo"])
@pytest.mark.parametrize("reuse_call_id", [False, True])
async def test_decision_cannot_authorize_a_later_occurrence_in_same_run(
    capability: str, reuse_call_id: bool
) -> None:
    with ScopedFilesystem() as fs:
        plans: list[dict[str, Any]]
        if capability == "filesystem":
            name, plans = (
                "fs_write",
                [
                    {"path": "note", "content": "first reviewed"},
                    {"path": "note", "content": "later reviewed"},
                    {"path": "note", "content": "first reviewed"},
                ],
            )
        else:
            name, plans = (
                "write_todos",
                [
                    {"items": [{"id": "one", "text": "first reviewed"}]},
                    {"items": [{"id": "two", "text": "later reviewed"}]},
                    {"items": [{"id": "one", "text": "first reviewed"}]},
                ],
            )
        ids = ["c0"] * 3 if reuse_call_id else ["c0", "c1", "c2"]
        model = FakeModel([*(call(ids[i], name, **p) for i, p in enumerate(plans)), "done"])
        agent = Agent(
            "A",
            "local fake",
            model,
            harness=Harness(filesystem=fs, todo=True, require_write_approval=True),
        )
        runner = Runner(checkpoint_store=MemoryCheckpointStore())
        first = await runner.run_async(agent, "go")
        first.interruption = cast(RunInterruption, first.interruption)
        first_review = first.interruption.pending_digests()
        old_digest = first_review["c0"]
        second = await runner.resume_async(agent, first.interruption, approve=first_review)
        second.interruption = cast(RunInterruption, second.interruption)
        third = await runner.resume_async(
            agent, second.interruption, approve=second.interruption.pending_digests()
        )
        third.interruption = cast(RunInterruption, third.interruption)
        assert third.status == "paused" and third.interruption.run_id == first.interruption.run_id
        assert third.interruption.interruption_id != first.interruption.interruption_id
        if capability == "filesystem":
            assert fs.read("note") == "later reviewed"
        else:
            assert third.interruption.state["nexura.todos"][0]["text"] == "later reviewed"
        # Reused ids accept the exact old mapping; unique ids accept its unchanged digest.
        with pytest.raises(ApprovalDecisionError):
            completed = await runner.resume_async(
                agent, third.interruption, approve={ids[2]: old_digest}
            )
            if capability == "filesystem":
                assert fs.read("note") == "first reviewed"
            else:
                assert completed.state["nexura.todos"][0]["text"] == "first reviewed"
