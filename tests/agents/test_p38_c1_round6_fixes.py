# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Returned failure statuses roll back; approval pauses can still resume, local fakes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from nexura_agents import (
    Agent,
    FakeModel,
    MemorySessionStore,
    Message,
    Runner,
    SQLiteSessionStore,
    ToolCall,
    tool,
)
from nexura_agents.session import retry_last_turn
from nexura_agents.types import RunResult


@pytest.mark.parametrize("disk", [False, True])
@pytest.mark.parametrize("newer_save", [False, True])
async def test_returned_failure_restores_without_overwriting_newer_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, disk: bool, newer_save: bool
) -> None:
    store = SQLiteSessionStore(tmp_path / "session.sqlite") if disk else MemorySessionStore()
    original = [Message("system", "i"), Message("user", "original"), Message("assistant", "old")]
    state = {"plan": ["retained"]}
    returned: RunResult[str] = RunResult("local-failed-run", "", [], status="failed")
    try:
        await store.save("s", original, expected_revision=0, state=state)
        runner = Runner(session_store=store)

        async def failed(*args: Any, **kwargs: Any) -> RunResult[str]:
            after = await store.get("s")
            assert after is not None and after.messages == original[:1]
            if newer_save:
                await store.save(
                    "s",
                    [Message("user", "newer")],
                    expected_revision=after.revision,
                    state={"newer": True},
                )
            return returned

        monkeypatch.setattr(runner, "run_async", failed)
        result = await retry_last_turn(runner, Agent("A", "i", FakeModel(["unused"])), "s")
        assert result is returned and result.status == "failed"
        saved = await store.get("s")
        assert saved is not None
        if newer_save:
            assert saved.messages == [Message("user", "newer")] and saved.state == {"newer": True}
        else:
            assert saved.messages == original and saved.state == state
    finally:
        if isinstance(store, SQLiteSessionStore):
            store.close_sync()


@pytest.mark.parametrize("disk", [False, True])
async def test_retry_approval_pause_keeps_session_revision_for_resume(
    tmp_path: Path, disk: bool
) -> None:
    store = SQLiteSessionStore(tmp_path / "session.sqlite") if disk else MemorySessionStore()
    original = [Message("system", "i"), Message("user", "original"), Message("assistant", "old")]
    state = {"plan": ["retained"]}
    calls: list[str] = []

    @tool(requires_approval=True)
    def reviewed() -> str:
        """A local approval-gated fake action."""
        calls.append("reviewed")
        return "accepted"

    agent = Agent(
        "A", "i", FakeModel([[ToolCall("approval", "reviewed", {})], "new reply"]), tools=[reviewed]
    )
    runner = Runner(session_store=store)
    try:
        await store.save("s", original, expected_revision=0, state=state)
        paused = await retry_last_turn(runner, agent, "s")
        assert paused.status == "paused" and paused.interruption is not None
        after = await store.get("s")
        assert after is not None and after.messages == original[:1]
        assert after.revision == paused.interruption.expected_revision
        assert calls == []
        done = await runner.resume_async(
            agent, paused.interruption, approve=paused.interruption.pending_digests()
        )
        assert done.status == "completed" and done.output == "new reply"
        saved = await store.get("s")
        assert saved is not None
        assert [m.content for m in saved.messages if m.role == "user"] == ["original"]
        assert saved.messages[-1].content == "new reply" and saved.state == state
        assert calls == ["reviewed"]
    finally:
        if isinstance(store, SQLiteSessionStore):
            store.close_sync()
