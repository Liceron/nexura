"""Branch a conversation: fork_session copies a session, whole or up to a message (F45)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from nexura_agents import (
    Agent,
    FakeModel,
    MemorySessionStore,
    Runner,
    SQLiteSessionStore,
    ToolCall,
    fork_session,
    tool,
)
from nexura_agents.errors import SDKValidationError, SessionConflictError
from nexura_agents.run_context import RunContext
from nexura_agents.types import Message


@pytest.fixture(params=["memory", "sqlite"])
def store(request: Any, tmp_path: Path) -> Any:
    if request.param == "memory":
        return MemorySessionStore()
    return SQLiteSessionStore(tmp_path / "sessions.db")


@tool
def lookup(city: str) -> str:
    """Look up a city."""
    return f"sunny in {city}"


async def chat(store: Any, session_id: str, *turns: tuple[str, Any]) -> FakeModel:
    model = FakeModel(responses=[reply for _, reply in turns])
    agent = Agent(name="Chat", instructions="i", model=model, tools=[lookup])
    runner = Runner(session_store=store)
    for text, _ in turns:
        await runner.run_async(agent, text, session_id=session_id)
    return model


def texts(messages: list[Message]) -> list[str | None]:
    return [m.content for m in messages if m.role != "system"]


async def test_a_fork_continues_from_a_point_and_leaves_the_source_alone(store: Any) -> None:
    await chat(store, "main", ("I'm Asha.", "Hi Asha."), ("I like tea.", "Noted."))
    # Stored messages start with the system message, so at=3 keeps it and the first turn.
    snapshot = await fork_session(store, "main", "branch", at=3)
    assert texts(snapshot.messages) == ["I'm Asha.", "Hi Asha."]
    assert snapshot.revision == 1

    model = await chat(store, "branch", ("I like coffee.", "Coffee it is."))
    assert texts(model.calls[0]["messages"])[-3:] == ["I'm Asha.", "Hi Asha.", "I like coffee."]
    main = await store.get("main")
    branch = await store.get("branch")
    assert texts(main.messages) == ["I'm Asha.", "Hi Asha.", "I like tea.", "Noted."]
    assert texts(branch.messages) == ["I'm Asha.", "Hi Asha.", "I like coffee.", "Coffee it is."]


async def test_without_at_the_whole_session_is_copied(store: Any) -> None:
    await chat(store, "main", ("hello", "hi"))
    snapshot = await fork_session(store, "main", "copy")
    assert texts(snapshot.messages) == ["hello", "hi"]


async def test_a_cut_inside_a_tool_exchange_is_refused_with_the_nearest_points(
    store: Any,
) -> None:
    call = [ToolCall(id="c1", name="lookup", arguments={"city": "Pune"})]
    await chat(store, "main", ("weather?", call), ("thanks", "welcome"))
    # Messages: system, user, assistant(call), tool, ...; cutting after the call but
    # before its result would leave the provider an unanswered tool call.
    with pytest.raises(SDKValidationError, match=r"at=3 would separate a tool call.*\[2, 4\]"):
        await fork_session(store, "main", "branch", at=3)
    assert await store.get("branch") is None
    snapshot = await fork_session(store, "main", "branch", at=4)
    assert [m.role for m in snapshot.messages] == ["system", "user", "assistant", "tool"]


async def test_a_fork_never_overwrites_an_existing_session(store: Any) -> None:
    await chat(store, "main", ("a", "b"))
    await chat(store, "other", ("c", "d"))
    with pytest.raises(SessionConflictError):
        await fork_session(store, "main", "other")
    assert texts((await store.get("other")).messages) == ["c", "d"]


@pytest.mark.parametrize("at", [-1, 4, True, 1.5, "1"])
async def test_out_of_range_or_non_integer_points_are_refused(store: Any, at: Any) -> None:
    await chat(store, "main", ("a", "b"))
    with pytest.raises(SDKValidationError, match="at must be an integer from 0 to 3"):
        await fork_session(store, "main", "branch", at=at)


async def test_same_id_and_missing_source_are_refused(store: Any) -> None:
    with pytest.raises(SDKValidationError, match="does not exist"):
        await fork_session(store, "nope", "branch")
    await chat(store, "main", ("a", "b"))
    with pytest.raises(SDKValidationError, match="new session id"):
        await fork_session(store, "main", "main")


async def test_saved_run_state_is_copied(store: Any) -> None:
    @tool
    def remember(ctx: RunContext[Any], item: str) -> str:
        """Remember an item."""
        ctx.state.setdefault("cart", []).append(item)
        return "ok"

    model = FakeModel(
        responses=[[ToolCall(id="c1", name="remember", arguments={"item": "tea"})], "added"]
    )
    agent = Agent(name="Shop", instructions="i", model=model, tools=[remember])
    await Runner(session_store=store).run_async(agent, "add tea", session_id="main")
    snapshot = await fork_session(store, "main", "branch")
    assert snapshot.state == {"cart": ["tea"]}
    assert (await store.get("branch")).state == {"cart": ["tea"]}


async def test_stores_without_state_still_fork() -> None:
    class PlainStore(MemorySessionStore):
        async def save(  # type: ignore[override]
            self, session_id: str, messages: list[Message], expected_revision: int
        ) -> Any:
            return await super().save(session_id, messages, expected_revision)

    store = PlainStore()
    await store.save("main", [Message(role="user", content="hi")], 0)
    snapshot = await fork_session(store, "main", "branch")
    assert texts(snapshot.messages) == ["hi"]
