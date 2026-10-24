# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Event-gated cancellation after the session pop commits, with local stores/fakes."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from nexura_agents import Agent, FakeModel, MemorySessionStore, Message, Runner, SQLiteSessionStore
from nexura_agents.session import SessionSnapshot, retry_last_turn


class GatedSave:
    def __init__(self, store: MemorySessionStore | SQLiteSessionStore) -> None:
        self.store = store
        self.committed = asyncio.Event()
        self.release = asyncio.Event()
        self.armed = False

    async def get(self, session_id: str) -> SessionSnapshot | None:
        return await self.store.get(session_id)

    async def save(
        self,
        session_id: str,
        messages: list[Message],
        expected_revision: int,
        *,
        state: dict[str, Any] | None = None,
    ) -> SessionSnapshot:
        saved = await self.store.save(
            session_id, messages, expected_revision=expected_revision, state=state
        )
        if self.armed:
            self.armed = False
            self.committed.set()
            await self.release.wait()
        return saved

    async def close(self) -> None:
        await self.store.close()


@pytest.mark.parametrize("disk", [False, True])
@pytest.mark.parametrize("concurrent_save", [False, True])
async def test_cancel_during_committed_pop_restores_before_returning(
    tmp_path: Path, disk: bool, concurrent_save: bool
) -> None:
    underlying = SQLiteSessionStore(tmp_path / "session.sqlite") if disk else MemorySessionStore()
    try:
        store = GatedSave(underlying)
        original = [
            Message("system", "i"),
            Message("user", "first"),
            Message("assistant", "a"),
            Message("user", "second"),
            Message("assistant", "b"),
        ]
        state = {"saved": {"plan": ["one", "two"]}}
        await store.save("s", original, expected_revision=0, state=state)
        model = FakeModel(["must not run"])
        runner = Runner(session_store=store)
        agent = Agent("A", "i", model)
        store.armed = True
        task = asyncio.create_task(retry_last_turn(runner, agent, "s"))
        try:
            await asyncio.wait_for(store.committed.wait(), timeout=5)
            popped = await store.get("s")
            assert popped is not None and popped.messages == original[:3]
            task.cancel()
            await asyncio.sleep(0)
            # A second cancellation must not detach cleanup from the caller either.
            task.cancel()
            if concurrent_save:
                await underlying.save(
                    "s",
                    [Message("user", "newer work")],
                    expected_revision=popped.revision,
                    state={"newer": True},
                )
        finally:
            store.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        saved = await store.get("s")
        assert saved is not None
        if concurrent_save:
            assert saved.messages == [Message("user", "newer work")]
            assert saved.state == {"newer": True}
        else:
            assert saved.messages == original, "user turn lost after cancellation during the pop"
            assert saved.state == state
        assert model.calls == []
    finally:
        if isinstance(underlying, SQLiteSessionStore):
            underlying.close_sync()
