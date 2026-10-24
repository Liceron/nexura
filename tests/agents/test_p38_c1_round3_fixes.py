# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Aggregate-only plan notifications and run-bound filesystem decisions, local fakes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from _c1_platform import requires_landlock
from nexura_agents import (
    Agent,
    FakeModel,
    Harness,
    MemoryCheckpointStore,
    Message,
    RunContext,
    Runner,
    ScopedFilesystem,
    ToolCall,
)
from nexura_agents.errors import ApprovalDecisionError
from nexura_agents.harness import todo_tools


async def test_todo_notifications_have_only_trusted_aggregate_fields() -> None:
    private = "PRIVATE-ID-AND-CALL-CORRELATION"
    changes = [
        [
            {"id": private, "text": private},
            {"id": "b", "text": "b", "status": "in_progress"},
            {"id": "c", "text": "c", "status": "completed"},
        ],
        [{"id": private, "text": private, "status": "completed"}],
        [],
    ]
    model = FakeModel(
        [
            *(
                Message(
                    "assistant",
                    tool_calls=[ToolCall(private + str(i), "write_todos", {"items": items})],
                )
                for i, items in enumerate(changes)
            ),
            "done",
        ]
    )
    result = await Agent(private, "local fake", model, harness=Harness(todo=True)).run_async("go")
    events = [
        event.data
        for event in result.events
        if event.type.value == "custom" and event.data.get("name") == "nexura.todos_updated"
    ]
    assert private not in str(events)
    assert all(set(event) == {"name", "data"} for event in events)
    assert [event["data"] for event in events] == [
        {
            "version": 1,
            "count": 3,
            "status_counts": {"pending": 1, "in_progress": 1, "completed": 1},
        },
        {
            "version": 2,
            "count": 1,
            "status_counts": {"pending": 0, "in_progress": 0, "completed": 1},
        },
        {
            "version": 3,
            "count": 0,
            "status_counts": {"pending": 0, "in_progress": 0, "completed": 0},
        },
    ]
    assert result.state["nexura.todos"] == []


async def test_todo_version_survives_json_replay_without_reverting_later_plan() -> None:
    writer = todo_tools()[0]
    ctx: RunContext[Any] = RunContext(deps=None, run_id="run", agent_name="local fake")
    ctx.idempotency_key = "first-call"
    original = {"items": [{"id": "one", "text": "PRIVATE", "status": "pending"}]}
    await writer.execute(original, ctx)
    assert ctx._events.take_pending()[0]["data"]["version"] == 1
    restored: RunContext[Any] = RunContext(
        deps=None, run_id="run", agent_name="local fake", state=json.loads(json.dumps(ctx.state))
    )
    restored.idempotency_key = "first-call"
    await writer.execute(original, restored)
    assert restored._events.take_pending()[0]["data"]["version"] == 1
    restored.idempotency_key = "second-call"
    await writer.execute({"items": [{"id": "two", "text": "NEW", "status": "completed"}]}, restored)
    assert restored._events.take_pending()[0]["data"]["version"] == 2
    restored.idempotency_key = "first-call"
    await writer.execute(original, restored)
    assert restored.state["nexura.todos"] == [{"id": "two", "text": "NEW", "status": "completed"}]
    assert restored._events.take_pending()[0]["data"] == {
        "version": 2,
        "count": 1,
        "status_counts": {"pending": 0, "in_progress": 0, "completed": 1},
    }


@pytest.mark.parametrize("disk", [False, pytest.param(True, marks=requires_landlock)])
async def test_edit_approval_requires_this_run_and_leaves_its_own_grant_usable(
    tmp_path: Path,
    disk: bool,
) -> None:
    with ScopedFilesystem(tmp_path if disk else None) as files:
        files.write("note", "original")

        def build() -> Agent[str]:
            return Agent(
                "A",
                "local fake",
                FakeModel(
                    [
                        Message(
                            "assistant",
                            tool_calls=[
                                ToolCall(
                                    "edit",
                                    "fs_edit",
                                    {"path": "note", "old": "original", "new": "approved"},
                                )
                            ],
                        ),
                        "done",
                    ]
                ),
                harness=Harness(filesystem=files, require_write_approval=True),
            )

        runner = Runner(checkpoint_store=MemoryCheckpointStore())
        first_agent, second_agent = build(), build()
        first = await runner.run_async(first_agent, "first")
        second = await runner.run_async(second_agent, "second")
        assert first.interruption is not None and second.interruption is not None
        first_review = first.interruption.pending_digests()
        second_review = second.interruption.pending_digests()
        assert first_review != second_review
        await runner.resume_async(first_agent, first.interruption, approve=first_review)
        files.write("note", "original")
        with pytest.raises(ApprovalDecisionError):
            await runner.resume_async(second_agent, second.interruption, approve=first_review)
        assert files.read("note") == "original"
        done = await runner.resume_async(second_agent, second.interruption, approve=second_review)
        assert done.status == "completed" and files.read("note") == "approved"
