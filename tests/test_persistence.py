"""Tests for local conversation persistence (Task 2).

Covers:
- Two session store instances + reopen durability + CAS conflict.
- No duplicate system prompts across multi-turn runs.
- Failure leaves history unchanged (uncommitted).
- Concurrent session isolation.
- Explicit session_id requirements and rejection of ambiguous explicit history.
- Round-tripping nested tool calls and Indic text in versioned JSON.
- CAS failure surfaces as failure before RUN_COMPLETED event is emitted.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from nexura_runtime.errors import SessionConflictError
from nexura_runtime.session import (
    MemorySessionStore,
    SessionSnapshot,
    SQLiteSessionStore,
    deserialize_session,
    serialize_session,
)

from nexura_runtime import (
    Agent,
    FakeModel,
    Message,
    RunEventType,
    Runner,
    SDKValidationError,
    ToolCall,
    ToolExecutionError,
    tool,
)


@pytest.mark.asyncio
async def test_session_serialization_nested_tool_calls_and_indic_text() -> None:
    session_id = "test-session-indic"
    revision = 3
    messages = [
        Message(role="system", content="आप एक सहायक हैं।"),
        Message(role="user", content="कृपया मेरा ऑर्डर ट्रैक करें।"),
        Message(
            role="assistant",
            content=None,
            tool_calls=[
                ToolCall(
                    id="call_indic_99",
                    name="track_order",
                    arguments={"order_id": "ORD-दिल्ली-8899", "metadata": {"priority": "उच्च"}},
                    raw_arguments=None,
                )
            ],
        ),
        Message(
            role="tool",
            content='{"status": "पारगमन में", "eta": "2 दिन"}',
            tool_call_id="call_indic_99",
            name="track_order",
        ),
    ]

    serialized = serialize_session(session_id, revision, messages)
    assert "आप एक सहायक हैं।" in serialized
    assert "ORD-दिल्ली-8899" in serialized
    assert "पारगमन में" in serialized

    out_id, out_rev, out_msgs = deserialize_session(serialized)
    assert out_id == session_id
    assert out_rev == revision
    assert len(out_msgs) == 4
    assert out_msgs[0].content == "आप एक सहायक हैं।"
    assert out_msgs[2].tool_calls is not None
    assert out_msgs[2].tool_calls[0].arguments["metadata"]["priority"] == "उच्च"
    assert out_msgs[3].content == '{"status": "पारगमन में", "eta": "2 दिन"}'


@pytest.mark.asyncio
async def test_sqlite_two_instances_reopen_durability_and_cas_conflict(tmp_path: Path) -> None:
    db_file = tmp_path / "sessions.db"

    # Instance 1: write initial conversation
    store1 = SQLiteSessionStore(db_file)
    messages1 = [
        Message(role="user", content="Turn 1 question"),
        Message(role="assistant", content="Turn 1 answer"),
    ]
    snap1 = await store1.save("sess_100", messages1, expected_revision=0)
    assert snap1.revision == 1

    # Instance 2: open same db file, verify it reads persisted state
    store2 = SQLiteSessionStore(db_file)
    snap2 = await store2.get("sess_100")
    assert snap2 is not None
    assert snap2.revision == 1
    assert len(snap2.messages) == 2
    assert snap2.messages[0].content == "Turn 1 question"

    # Store 1 performs an update: revision becomes 2
    messages2 = messages1 + [
        Message(role="user", content="Turn 2 question"),
        Message(role="assistant", content="Turn 2 answer"),
    ]
    snap1_updated = await store1.save("sess_100", messages2, expected_revision=1)
    assert snap1_updated.revision == 2

    # Store 2 attempts to save with stale expected_revision=1 -> CAS conflict
    with pytest.raises(SessionConflictError) as exc_info:
        await store2.save("sess_100", messages1, expected_revision=1)

    assert exc_info.value.session_id == "sess_100"
    assert exc_info.value.expected_revision == 1
    assert exc_info.value.actual_revision == 2

    # Clean up stores
    await store1.close()
    await store2.close()

    # Instance 3: Reopen database and verify revision 2 is preserved
    store3 = SQLiteSessionStore(db_file)
    snap3 = await store3.get("sess_100")
    assert snap3 is not None
    assert snap3.revision == 2
    assert len(snap3.messages) == 4
    await store3.close()


@pytest.mark.asyncio
async def test_no_duplicate_system_prompts_across_turns(tmp_path: Path) -> None:
    db_file = tmp_path / "session_turns.db"
    store = SQLiteSessionStore(db_file)

    model = FakeModel(
        responses=[
            "Hello, I am ready to help.",
            "I remember our chat. Here is answer 2.",
        ]
    )

    agent = Agent(
        name="AssistantBot",
        instructions="You are a helpful assistant.",
        model=model,
    )

    runner = Runner(session_store=store)

    # Turn 1
    res1 = await runner.run_async(agent, "Hi", session_id="user_session_1")
    assert res1.status == "completed"
    assert len(res1.messages) == 3  # system, user, assistant
    system_count_1 = sum(1 for m in res1.messages if m.role == "system")
    assert system_count_1 == 1

    # Turn 2 on same session
    res2 = await runner.run_async(agent, "Second question", session_id="user_session_1")
    assert res2.status == "completed"
    # Should have system, user1, assistant1, user2, assistant2
    assert len(res2.messages) == 5
    system_count_2 = sum(1 for m in res2.messages if m.role == "system")
    assert system_count_2 == 1  # No duplicate system prompt!

    # Verify store snapshot also contains exactly 1 system prompt
    snap = await store.get("user_session_1")
    assert snap is not None
    assert snap.revision == 2
    assert sum(1 for m in snap.messages if m.role == "system") == 1

    await store.close()


@pytest.mark.asyncio
async def test_failure_leaves_history_unchanged(tmp_path: Path) -> None:
    db_file = tmp_path / "failures.db"
    store = SQLiteSessionStore(db_file)

    @tool
    def exploding_tool() -> str:
        raise RuntimeError("Simulated crash during tool execution")

    # Turn 1 succeeds
    model1 = FakeModel(["Initial answer"])
    agent1 = Agent(name="Bot1", instructions="inst", model=model1)
    runner = Runner(session_store=store)
    await runner.run_async(agent1, "Start", session_id="fail_sess")

    snap1 = await store.get("fail_sess")
    assert snap1 is not None
    assert snap1.revision == 1
    assert len(snap1.messages) == 3

    # Turn 2 fails due to tool error
    model2 = FakeModel(
        responses=[
            [ToolCall(id="c_err", name="exploding_tool", arguments={})],
        ]
    )
    agent2 = Agent(name="Bot2", instructions="inst", model=model2, tools=[exploding_tool])

    with pytest.raises(ToolExecutionError):
        await runner.run_async(agent2, "Trigger tool crash", session_id="fail_sess")

    # Verify persisted session history is completely unchanged (no partial commit)
    snap2 = await store.get("fail_sess")
    assert snap2 is not None
    assert snap2.revision == 1
    assert len(snap2.messages) == 3
    assert snap2.messages[-1].content == "Initial answer"

    await store.close()


@pytest.mark.asyncio
async def test_cas_failure_surfaces_as_failure_before_run_completed(tmp_path: Path) -> None:
    db_file = tmp_path / "cas_event.db"
    store = SQLiteSessionStore(db_file)

    # Pre-populate session at revision 1
    await store.save("sess_conflict", [Message(role="user", content="init")], expected_revision=0)

    # Concurrently advance store behind runner's back so runner's CAS fails
    class SabotagingStore:
        def __init__(self, real_store: SQLiteSessionStore) -> None:
            self.real_store = real_store

        async def get(self, session_id: str) -> SessionSnapshot | None:
            return await self.real_store.get(session_id)

        async def save(
            self, session_id: str, messages: list[Message], expected_revision: int
        ) -> SessionSnapshot:
            # Advance revision in real store before attempting save
            await self.real_store.save(
                session_id,
                [Message(role="user", content="interfering run")],
                expected_revision=expected_revision,
            )
            # Now attempting save with expected_revision will fail with SessionConflictError
            return await self.real_store.save(session_id, messages, expected_revision)

        async def close(self) -> None:
            await self.real_store.close()

    sabotage = SabotagingStore(store)
    model = FakeModel(["Final output"])
    agent = Agent(name="ConflictBot", instructions="inst", model=model)
    runner = Runner(session_store=sabotage)

    events = []
    with pytest.raises(SessionConflictError):
        async for event in runner.stream_async(agent, "Hi", session_id="sess_conflict"):
            events.append(event)

    event_types = [e.type for e in events]
    # RUN_COMPLETED must NEVER be emitted if CAS fails!
    assert RunEventType.RUN_COMPLETED not in event_types
    # RUN_FAILED must be emitted
    assert RunEventType.RUN_FAILED in event_types

    await store.close()


@pytest.mark.asyncio
async def test_concurrent_session_isolation(tmp_path: Path) -> None:
    db_file = tmp_path / "concurrent_sessions.db"
    store = SQLiteSessionStore(db_file)

    def model_handler(messages: list[Message], tools: Any) -> str:
        user_msg = next((m.content for m in messages if m.role == "user"), "")
        return f"Response to {user_msg}"

    model = FakeModel(handler=model_handler)
    agent = Agent(name="ConcurrentAgent", instructions="inst", model=model)
    runner = Runner(session_store=store)

    async def run_for_session(idx: int) -> None:
        session_id = f"session_{idx}"
        res = await runner.run_async(agent, f"User {idx} message", session_id=session_id)
        assert f"User {idx} message" in res.final_output

    tasks = [run_for_session(i) for i in range(10)]
    await asyncio.gather(*tasks)

    # Verify each session is isolated and has revision 1
    for i in range(10):
        snap = await store.get(f"session_{i}")
        assert snap is not None
        assert snap.revision == 1
        assert any(m.content == f"User {i} message" for m in snap.messages)

    await store.close()


@pytest.mark.asyncio
async def test_session_store_argument_validation() -> None:
    store = MemorySessionStore()
    model = FakeModel(["Answer"])
    agent = Agent(name="ValidationBot", instructions="inst", model=model)
    runner = Runner()

    # 1. Store without session_id raises SDKValidationError
    with pytest.raises(SDKValidationError, match="Explicit session_id is required"):
        await runner.run_async(agent, "Hi", session_store=store)

    # 2. Both explicit history and store raises SDKValidationError
    with pytest.raises(
        SDKValidationError,
        match="Cannot provide both explicit history and a session store",
    ):
        await runner.run_async(
            agent,
            "Hi",
            session_id="s1",
            session_store=store,
            history=[Message(role="user", content="prior")],
        )
