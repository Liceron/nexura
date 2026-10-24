"""Round-5 platform/cancellation audit, with local stores and fake models only."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, cast

import pytest
from nexura_agents import (
    Agent,
    CancelToken,
    FakeModel,
    MemorySessionStore,
    Message,
    Runner,
    Skill,
    SQLiteSessionStore,
)
from nexura_agents.session import SessionSnapshot, retry_last_turn
from nexura_agents.skills import SkillError
from nexura_agents.types import ModelResponse


@pytest.mark.parametrize("disk", [False, True])
@pytest.mark.parametrize("newer_save", [False, True])
async def test_cancel_token_during_retry_restores_turn_without_overwriting_newer_work(
    tmp_path: Path, disk: bool, newer_save: bool
) -> None:
    store = SQLiteSessionStore(tmp_path / "sessions.sqlite") if disk else MemorySessionStore()
    original = [
        Message("system", "i"),
        Message("user", "first"),
        Message("assistant", "a"),
        Message("user", "second"),
        Message("assistant", "b"),
    ]
    state = {"plan": ["retain this"]}
    entered = asyncio.Event()
    gate = asyncio.Event()

    class SlowFake(FakeModel):
        async def generate(self, *args: Any, **kwargs: Any) -> ModelResponse:
            entered.set()
            await gate.wait()
            return await super().generate(*args, **kwargs)

    try:
        await store.save("s", original, expected_revision=0, state=state)
        runner = Runner(session_store=store)
        token = CancelToken()
        task = asyncio.create_task(
            retry_last_turn(runner, Agent("A", "i", SlowFake(["unused"])), "s", cancel=token)
        )
        await asyncio.wait_for(entered.wait(), 5)
        popped = cast(SessionSnapshot, await store.get("s"))
        assert popped.messages == original[:3]
        if newer_save:
            await store.save(
                "s",
                [Message("user", "newer")],
                expected_revision=popped.revision,
                state={"newer": True},
            )
        token.cancel()
        result = await asyncio.wait_for(task, 5)
        assert result.status == "cancelled"
        saved = cast(SessionSnapshot, await store.get("s"))
        if newer_save:
            assert saved.messages == [Message("user", "newer")] and saved.state == {"newer": True}
        else:
            assert saved.messages == original, "CancelToken cancellation lost the popped user turn"
            assert saved.state == state
    finally:
        if isinstance(store, SQLiteSessionStore):
            store.close_sync()


@pytest.mark.parametrize("swap", ["hardlink", "hardlink_after_open"])
def test_portable_loader_fstat_rejects_hardlink_races(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, swap: str
) -> None:
    root = tmp_path / "review"
    root.mkdir()
    target = root / "SKILL.md"
    target.write_text("---\nname: review\ndescription: review\n---\nbody")
    outside = tmp_path / "outside"
    outside.write_text("PRIVATE OUTSIDE")
    original_open = os.open

    def raced_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if Path(path) == target:
            if swap == "hardlink":
                target.unlink()
                os.link(outside, target)
            else:
                # Preserve inode identity but change link count after the last lstat.
                os.link(target, tmp_path / "second-link")
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", raced_open)
    with pytest.raises(SkillError, match="singly linked|identity"):
        Skill.from_directory(root, format="agent-skills")
