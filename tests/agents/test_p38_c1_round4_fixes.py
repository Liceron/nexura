# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Shared occurrence-bound decisions and durable recovery, using only local fakes."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

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
    SQLiteCheckpointStore,
    ToolCall,
    tool,
)
from nexura_agents.contracts.execution import ExecutionId
from nexura_agents.errors import ApprovalDecisionError


@contextmanager
def checkpoint_store(tmp_path: Path, disk: bool) -> Iterator[Any]:
    if disk:
        store = SQLiteCheckpointStore(tmp_path / "approval.sqlite")
        try:
            yield store
        finally:
            store.close()
    else:
        yield MemoryCheckpointStore()


@pytest.mark.parametrize("disk", [False, True])
@pytest.mark.parametrize("reuse_call_id", [False, True])
async def test_plain_approval_refuses_a_previous_occurrence_without_consuming_current(
    tmp_path: Path, disk: bool, reuse_call_id: bool
) -> None:
    applied: list[str] = []

    @tool(requires_approval=True, effect="non_idempotent")
    def replace_note(content: str) -> str:
        """Record a local fake replacement."""
        applied.append(content)
        return content

    ids = ["same"] * 3 if reuse_call_id else ["first", "second", "third"]
    values = ["first reviewed", "later reviewed", "first reviewed"]
    agent = Agent(
        "Plain",
        "local fake",
        FakeModel(
            [
                *(
                    Message(
                        "assistant",
                        tool_calls=[ToolCall(identifier, "replace_note", {"content": value})],
                    )
                    for identifier, value in zip(ids, values, strict=True)
                ),
                "done",
            ]
        ),
        tools=[replace_note],
    )
    with checkpoint_store(tmp_path, disk) as store:
        runner = Runner(checkpoint_store=store)
        first = await runner.run_async(agent, "go")
        assert first.interruption is not None
        old_review = first.interruption.pending_digests()
        second = await runner.resume_async(agent, first.interruption, approve=old_review)
        assert second.interruption is not None
        third = await runner.resume_async(
            agent, second.interruption, approve=second.interruption.pending_digests()
        )
        assert third.interruption is not None
        assert third.interruption.tool_scopes == {}
        current = third.interruption.pending_digests()
        assert current[ids[2]] != old_review[ids[0]]
        with pytest.raises(ApprovalDecisionError):
            await runner.resume_async(
                agent, third.interruption, approve={ids[2]: old_review[ids[0]]}
            )
        assert applied == values[:2]
        # Refusing the stale value must leave this occurrence's own grant usable.
        done = await runner.resume_async(agent, third.interruption, approve=current)
        assert done.status == "completed" and applied == values


def pending_agent(
    capability: str, files: ScopedFilesystem, applied: list[str], *, pending: bool
) -> Agent[str]:
    @tool(requires_approval=True, effect="non_idempotent")
    def replace_note(content: str) -> str:
        """Record a local fake replacement."""
        applied.append(content)
        return content

    arguments: dict[str, Any]
    if capability == "todo":
        name, arguments = "write_todos", {"items": [{"id": "one", "text": "reviewed"}]}
    elif capability.startswith("filesystem"):
        name, arguments = "fs_write", {"path": "note", "content": "reviewed"}
    else:
        name, arguments = "replace_note", {"content": "reviewed"}
    return Agent(
        "Recover",
        "local fake",
        FakeModel(
            [Message("assistant", tool_calls=[ToolCall("untrusted-id", name, arguments)])]
            if pending
            else ["done"]
        ),
        tools=[replace_note] if capability == "plain" else [],
        harness=Harness(
            todo=capability == "todo",
            filesystem=files if capability.startswith("filesystem") else None,
            require_write_approval=True,
        ),
    )


@pytest.mark.parametrize("disk", [False, True])
@pytest.mark.parametrize("capability", ["plain", "todo", "filesystem-memory", "filesystem-disk"])
async def test_recovered_pause_accepts_its_original_occurrence_decision(
    tmp_path: Path, disk: bool, capability: str
) -> None:
    applied: list[str] = []
    root = tmp_path / "workspace"
    root.mkdir()
    with (
        ScopedFilesystem(root if capability == "filesystem-disk" else None) as files,
        checkpoint_store(tmp_path, disk) as store,
    ):
        agent = pending_agent(capability, files, applied, pending=True)
        first = await Runner(checkpoint_store=store).run_async(agent, "go")
        assert first.interruption is not None
        reviewed = first.interruption.pending_digests()
        original_id = first.interruption.interruption_id
        # Close/reopen the disk store and capability as a new process would.
        if disk:
            store.close()
            store = SQLiteCheckpointStore(tmp_path / "approval.sqlite")
        try:
            if capability == "filesystem-disk":
                files.close()
                files = ScopedFilesystem(root)
            try:
                continued = pending_agent(capability, files, applied, pending=False)
                runner = Runner(checkpoint_store=store)
                recovered = await runner.recover_async(continued, first.run_id)
                assert recovered.interruption is not None
                restored = RunInterruption.from_json(recovered.interruption.to_json())
                assert restored.interruption_id == original_id
                assert restored.pending_digests() == reviewed
                done = await runner.resume_async(continued, restored, approve=reviewed)
                assert done.status == "completed"
                if capability == "todo":
                    assert done.state["nexura.todos"][0]["text"] == "reviewed"
                elif capability.startswith("filesystem"):
                    assert files.read("note") == "reviewed"
                else:
                    assert applied == ["reviewed"]
            finally:
                files.close()
        finally:
            if disk:
                store.close()


class Crash(BaseException):
    """Simulated process stop after a resume's journal claim commits."""


@pytest.mark.parametrize("capability", ["plain", "todo", "filesystem-memory"])
async def test_claim_recovery_keeps_its_decision_and_stable_effect_hash(
    tmp_path: Path, capability: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    applied: list[str] = []
    with ScopedFilesystem() as files, checkpoint_store(tmp_path, True) as store:
        first_agent = pending_agent(capability, files, applied, pending=True)
        runner = Runner(checkpoint_store=store)
        first = await runner.run_async(first_agent, "go")
        assert first.interruption is not None
        interruption = first.interruption
        reviewed = interruption.pending_digests()
        call = interruption.pending[0]
        effect_hash = interruption.action_digest(call, interruption.tool_scopes.get(call.id))
        assert reviewed[call.id] != effect_hash

        async def stop_before_consume(*args: Any) -> None:
            raise Crash()

        monkeypatch.setattr(runner, "_consume_approval", stop_before_consume)
        with pytest.raises(Crash):
            await runner.resume_async(first_agent, interruption, approve=reviewed)
        journal = await store.open_execution_store()
        snapshot = await journal.get(ExecutionId("default", first.run_id))
        assert snapshot is not None
        assert (
            snapshot.run.state["approval_claim"]["approve"][call.id]["digest"] == reviewed[call.id]
        )
        done = await Runner(checkpoint_store=store).recover_async(
            pending_agent(capability, files, applied, pending=False), first.run_id
        )
        assert done.status == "completed"
        snapshot = await journal.get(ExecutionId("default", first.run_id))
        assert snapshot is not None
        intents = [step.intent for step in snapshot.steps.values() if step.intent is not None]
        assert len(intents) == 1
        assert intents[0].approval_id == interruption.interruption_id
        assert intents[0].payload_digest == effect_hash
        if capability == "plain":
            assert applied == ["reviewed"]
        elif capability == "todo":
            assert done.state["nexura.todos"][0]["text"] == "reviewed"
        else:
            assert files.read("note") == "reviewed"
