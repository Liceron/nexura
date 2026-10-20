"""Session management: delete, list, pop_last_turn, retry_last_turn."""

import asyncio

import pytest
from nexura_agents import Agent, FakeModel, MemorySessionStore, Runner, SQLiteSessionStore
from nexura_agents.errors import SDKValidationError, SessionConflictError
from nexura_agents.session import (
    SessionSnapshot,
    delete_session,
    list_sessions,
    pop_last_turn,
    retry_last_turn,
)
from nexura_agents.types import Message, ToolCall


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    if request.param == "memory":
        yield MemorySessionStore()
    else:
        s = SQLiteSessionStore(tmp_path / "s.db")
        yield s
        s.close_sync()


TURNS = [
    Message(role="system", content="s"),
    Message(role="user", content="u1"),
    Message(role="assistant", content="a1"),
    Message(role="user", content="u2"),
    Message(role="assistant", tool_calls=[ToolCall(id="c", name="f", arguments={})]),
    Message(role="tool", tool_call_id="c", content="r", name="f"),
    Message(role="assistant", content="a2"),
]


def test_delete_and_list(store):
    async def go():
        assert await list_sessions(store) == []
        await store.save("b", TURNS[:2], expected_revision=0)
        await store.save("a", TURNS[:2], expected_revision=0)
        assert await list_sessions(store) == ["a", "b"]
        assert await delete_session(store, "a") is True
        assert await delete_session(store, "a") is False
        assert await store.get("a") is None
        assert await list_sessions(store) == ["b"]
        # A deleted id is created again with expected_revision=0, but its revision keeps
        # counting, so a snapshot taken before the delete cannot overwrite it.
        snap = await store.save("a", TURNS[:2], expected_revision=0)
        assert snap.revision == 2

    asyncio.run(go())


def test_pop_with_tool_calls_keeps_state(store):
    async def go():
        await store.save("s", TURNS, expected_revision=0, state={"k": 1})
        snap = await pop_last_turn(store, "s")
        assert snap is not None and snap.revision == 2
        assert [m.content for m in snap.messages] == ["s", "u1", "a1"]
        stored = await store.get("s")
        assert stored.messages == snap.messages
        assert stored.state == {"k": 1}
        # Pop again removes the first turn; the system message stays.
        snap = await pop_last_turn(store, "s")
        assert [m.role for m in snap.messages] == ["system"]

    asyncio.run(go())


def test_pop_empty_and_system_only(store):
    async def go():
        assert await pop_last_turn(store, "missing") is None
        await store.save("s", TURNS[:1], expected_revision=0)
        assert await pop_last_turn(store, "s") is None
        assert (await store.get("s")).revision == 1

    asyncio.run(go())


class RacyStore:
    """Wraps a store; saves a competing update between get and save of the pop."""

    def __init__(self, inner):
        self.inner = inner
        self.race = True

    async def get(self, session_id):
        snap = await self.inner.get(session_id)
        if self.race and snap is not None:
            self.race = False
            await self.inner.save(session_id, snap.messages, snap.revision)
        return snap

    async def save(self, session_id, messages, expected_revision, state=None):
        return await self.inner.save(session_id, messages, expected_revision, state=state)

    async def close(self):
        pass


def test_pop_conflict_loses_nothing(store):
    async def go():
        await store.save("s", TURNS, expected_revision=0)
        racy = RacyStore(store)
        with pytest.raises(SessionConflictError):
            await pop_last_turn(racy, "s")
        stored = await store.get("s")
        assert stored.messages == TURNS and stored.revision == 2

    asyncio.run(go())


def test_retry_makes_new_reply_and_keeps_earlier_turns(store):
    async def go():
        model = FakeModel(responses=["one", "two", "two again"])
        agent = Agent(name="A", instructions="Be brief.", model=model)
        runner = Runner(session_store=store)
        await runner.run_async(agent, "first", session_id="s")
        await runner.run_async(agent, "second", session_id="s")
        result = await retry_last_turn(runner, agent, "s")
        assert result.output == "two again"
        msgs = (await store.get("s")).messages
        assert [m.content for m in msgs if m.role != "system"] == [
            "first",
            "one",
            "second",
            "two again",
        ]

    asyncio.run(go())


def test_retry_errors(store):
    async def go():
        agent = Agent(name="A", instructions="x", model=FakeModel(responses=["ok"]))
        with pytest.raises(SDKValidationError, match="no user turn"):
            await retry_last_turn(Runner(session_store=store), agent, "nope")
        with pytest.raises(SDKValidationError, match="session_store"):
            await retry_last_turn(Runner(), agent, "nope")

    asyncio.run(go())


class MinimalStore:
    """A custom store with only get/save/close."""

    def __init__(self):
        self.snaps: dict[str, SessionSnapshot] = {}

    async def get(self, session_id):
        return self.snaps.get(session_id)

    async def save(self, session_id, messages, expected_revision):
        self.snaps[session_id] = SessionSnapshot(session_id, list(messages), expected_revision + 1)
        return self.snaps[session_id]

    async def close(self):
        pass


def test_custom_store_without_optional_methods():
    async def go():
        custom = MinimalStore()
        with pytest.raises(SDKValidationError, match="delete"):
            await delete_session(custom, "s")
        with pytest.raises(SDKValidationError, match="list_sessions"):
            await list_sessions(custom)
        # pop works with any store that has get/save.
        await custom.save("s", TURNS, 0)
        snap = await pop_last_turn(custom, "s")
        assert [m.content for m in snap.messages] == ["s", "u1", "a1"]

    asyncio.run(go())
