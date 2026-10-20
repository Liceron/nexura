"""Unit and acceptance tests for B08: Sessions and Long-Term Memory."""

from __future__ import annotations

from pathlib import Path

import pytest
from nexura_runtime.contracts import ExecutionContext
from nexura_runtime.types import Message, Usage

from nexura_runtime.memory import (
    ContextTruncationPolicy,
    DuplicateSystemPromptError,
    FactNotFoundError,
    MemoryModule,
    MemoryStore,
    SessionAccessDeniedError,
)


@pytest.fixture
def temp_db(tmp_path: Path) -> Path:
    return tmp_path / "memory_test.db"


@pytest.fixture
def store(temp_db: Path) -> MemoryStore:
    return MemoryStore(temp_db)


@pytest.fixture
def context() -> ExecutionContext:
    return ExecutionContext(
        workspace_id="ws_fintech",
        principal_id="user_alice",
        run_id="run_mem_001",
        node_id="mem_node",
        attempt_id="att_1",
    )


# ---------------------------------------------------------------------------
# 1. Concurrent users cannot mix histories
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_concurrent_users_cannot_mix_histories(
    store: MemoryStore,
) -> None:
    ws = "ws_fintech"
    # Alice creates her session
    sess_alice = store.create_session(
        workspace_id=ws,
        principal_id="user_alice",
        session_id="sess_alice_private",
        initial_messages=[Message(role="user", content="Alice confidential notes")],
    )

    # Bob attempts to read Alice's session
    with pytest.raises(SessionAccessDeniedError) as exc_info:
        store.get_session(ws, "sess_alice_private", principal_id="user_bob")
    assert exc_info.value.details["principal_id"] == "user_bob"
    assert exc_info.value.details["owner_principal_id"] == "user_alice"

    # Bob attempts to append a turn to Alice's session
    with pytest.raises(SessionAccessDeniedError):
        store.append_turn(
            ws,
            "sess_alice_private",
            user_message=Message(role="user", content="Bob injection"),
            assistant_message=Message(role="assistant", content="Injected response"),
            expected_revision=sess_alice.revision,
            principal_id="user_bob",
        )

    # Bob creates his own session
    store.create_session(
        workspace_id=ws,
        principal_id="user_bob",
        session_id="sess_bob_private",
        initial_messages=[Message(role="user", content="Bob private notes")],
    )

    # Alice cannot read Bob's session
    with pytest.raises(SessionAccessDeniedError):
        store.get_session(ws, "sess_bob_private", principal_id="user_alice")

    # Verify Alice's history has only Alice's messages
    clean_alice = store.get_session(ws, "sess_alice_private", principal_id="user_alice")
    assert clean_alice is not None
    assert len(clean_alice.messages) == 1
    assert clean_alice.messages[0].content == "Alice confidential notes"


# ---------------------------------------------------------------------------
# 2. Duplicate system messages stay prevented
# ---------------------------------------------------------------------------
def test_duplicate_system_messages_stay_prevented(
    store: MemoryStore,
) -> None:
    ws = "ws_fintech"
    # 1. Reject multiple system prompts at session creation
    with pytest.raises(DuplicateSystemPromptError):
        store.create_session(
            workspace_id=ws,
            principal_id="user_alice",
            session_id="sess_double_sys",
            initial_messages=[
                Message(role="system", content="Prompt 1"),
                Message(role="system", content="Prompt 2"),
            ],
        )

    # 2. Reject appending a turn that includes a system message
    valid_sess = store.create_session(
        workspace_id=ws,
        principal_id="user_alice",
        session_id="sess_valid_sys",
        initial_messages=[Message(role="system", content="System Prompt Initial")],
    )

    with pytest.raises(DuplicateSystemPromptError):
        store.append_turn(
            ws,
            "sess_valid_sys",
            user_message=Message(role="system", content="System Prompt Duplicate"),
            assistant_message=Message(role="assistant", content="Resp"),
            expected_revision=valid_sess.revision,
            principal_id="user_alice",
        )

    # 3. Context builder injects facts without creating multiple system messages
    facts = [
        store.store_fact(ws, "user_alice", "country", "India"),
        store.store_fact(ws, "user_alice", "currency", "INR"),
    ]
    policy = ContextTruncationPolicy(max_messages=10, preserve_system_prompt=True)
    bounded = store.build_bounded_context(valid_sess, policy=policy, facts=facts)

    system_count = sum(1 for m in bounded if m.role == "system")
    assert system_count == 1, f"Expected exactly 1 system message, got {system_count}"
    assert "System Prompt Initial" in (bounded[0].content or "")
    assert "India" in (bounded[0].content or "")
    assert "INR" in (bounded[0].content or "")


# ---------------------------------------------------------------------------
# 3. Aborted runs follow explicit commit policy
# ---------------------------------------------------------------------------
def test_aborted_runs_follow_explicit_commit_policy(
    store: MemoryStore,
) -> None:
    ws = "ws_fintech"
    principal = "user_alice"
    session_id = "sess_tx_test"

    session = store.create_session(
        workspace_id=ws,
        principal_id=principal,
        session_id=session_id,
        initial_messages=[Message(role="system", content="Initial System")],
    )
    initial_rev = session.revision

    # 1. Staged transaction that gets aborted
    tx_abort = store.begin_transaction(ws, session_id, principal)
    tx_abort.stage_turn(
        Message(role="user", content="Uncommitted query"),
        Message(role="assistant", content="Uncommitted answer"),
    )
    # Simulate abort / crash before commit
    tx_abort.abort()

    # Verify session in storage remains untouched
    current = store.get_session(ws, session_id, principal)
    assert current is not None
    assert current.revision == initial_rev
    assert len(current.messages) == 1  # Only initial system message

    # Cannot commit an aborted transaction
    with pytest.raises(RuntimeError, match="Cannot commit an aborted transaction"):
        tx_abort.commit()

    # 2. Transaction that explicitly commits
    tx_commit = store.begin_transaction(ws, session_id, principal)
    tx_commit.stage_turn(
        Message(role="user", content="Committed query"),
        Message(role="assistant", content="Committed answer"),
    )
    committed_sess = tx_commit.commit()

    assert committed_sess.revision == initial_rev + 1
    assert len(committed_sess.messages) == 3

    # Storage reflects the committed turn
    refreshed = store.get_session(ws, session_id, principal)
    assert refreshed is not None
    assert refreshed.revision == initial_rev + 1
    assert len(refreshed.messages) == 3


# ---------------------------------------------------------------------------
# 4. Deleted memory is excluded from retrieval
# ---------------------------------------------------------------------------
def test_deleted_memory_excluded_from_retrieval(
    store: MemoryStore,
    temp_db: Path,
) -> None:
    ws = "ws_fintech"
    principal = "user_alice"

    fact_a = store.store_fact(ws, principal, "theme", "dark", category="ui")
    fact_b = store.store_fact(ws, principal, "email", "alice@example.com", category="profile")
    store.store_fact(ws, principal, "tier", "platinum", category="account")

    # Initial retrieval: all 3 facts returned
    all_facts = store.find_facts(ws, principal)
    assert len(all_facts) == 3

    # Soft delete fact_b
    deleted = store.delete_fact(ws, principal, fact_b.fact_id, hard_delete=False)
    assert deleted is True

    # find_facts excludes soft-deleted fact
    active_facts = store.find_facts(ws, principal)
    active_keys = [f.fact_key for f in active_facts]
    assert len(active_facts) == 2
    assert "email" not in active_keys
    assert "theme" in active_keys
    assert "tier" in active_keys

    # get_fact excludes deleted fact by default
    assert store.get_fact(ws, principal, fact_b.fact_id) is None
    # Can still find with include_deleted=True
    assert store.get_fact(ws, principal, fact_b.fact_id, include_deleted=True) is not None

    # Reopening database confirms deleted memory stays excluded across restarts
    reopened = MemoryStore(temp_db)
    dur_facts = reopened.find_facts(ws, principal)
    dur_keys = [f.fact_key for f in dur_facts]
    assert "email" not in dur_keys
    assert len(dur_facts) == 2

    # Hard delete fact_a purges it
    reopened.delete_fact(ws, principal, fact_a.fact_id, hard_delete=True)
    assert reopened.get_fact(ws, principal, fact_a.fact_id, include_deleted=True) is None

    # Deleting nonexistent fact raises FactNotFoundError
    with pytest.raises(FactNotFoundError):
        reopened.delete_fact(ws, principal, "ghost_fact_xyz")


# ---------------------------------------------------------------------------
# 5. Unknown usage stays unknown
# ---------------------------------------------------------------------------
def test_unknown_usage_stays_unknown() -> None:
    # When provider omits token usage, unknown usage must NOT be fabricated into zeros
    unreported_usage = Usage()
    assert unreported_usage.is_reported is False
    assert unreported_usage.prompt_tokens is None
    assert unreported_usage.completion_tokens is None
    assert unreported_usage.total_tokens is None

    # Accumulating with an unreported turn marks completion False without fabricating zeros
    accumulated = Usage(prompt_tokens=100, completion_tokens=50, total_tokens=150).add(None)
    assert accumulated.is_complete is False
    assert accumulated.total_tokens == 150

    # Accumulating two unreported turns leaves tokens None
    both_unreported = Usage().add(Usage())
    assert both_unreported.prompt_tokens is None
    assert both_unreported.total_tokens is None
    assert both_unreported.is_reported is False

    # Fully reported usage is complete and reported
    full_usage = Usage(prompt_tokens=100, completion_tokens=50, total_tokens=150)
    assert full_usage.is_complete is True
    assert full_usage.is_reported is True
    assert full_usage.total_tokens == 150


# ---------------------------------------------------------------------------
# 6. MemoryModule B00 Contract Integration
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_memory_module_b00_contract(
    store: MemoryStore,
    context: ExecutionContext,
) -> None:
    mod = MemoryModule(store=store)

    # 1. create_session
    res_create = await mod.execute(
        context,
        inputs={
            "operation": "create_session",
            "workspace_id": context.workspace_id,
            "principal_id": context.principal_id,
            "session_id": "sess_mod_test",
        },
        config={},
    )
    assert res_create.status == "success"
    assert res_create.outputs["revision"] == 1

    # 2. append_turn
    res_turn = await mod.execute(
        context,
        inputs={
            "operation": "append_turn",
            "workspace_id": context.workspace_id,
            "principal_id": context.principal_id,
            "session_id": "sess_mod_test",
            "user_message": {"role": "user", "content": "Hello module"},
            "assistant_message": {"role": "assistant", "content": "Hello user"},
            "expected_revision": 1,
        },
        config={},
    )
    assert res_turn.status == "success"
    assert res_turn.outputs["revision"] == 2
    assert res_turn.outputs["messages_count"] == 2

    # 3. store_fact
    res_fact = await mod.execute(
        context,
        inputs={
            "operation": "store_fact",
            "workspace_id": context.workspace_id,
            "principal_id": context.principal_id,
            "fact_key": "user_role",
            "fact_value": "analyst",
        },
        config={},
    )
    assert res_fact.status == "success"
    f_id = res_fact.outputs["fact_id"]

    # 4. find_facts
    res_find = await mod.execute(
        context,
        inputs={
            "operation": "find_facts",
            "workspace_id": context.workspace_id,
            "principal_id": context.principal_id,
        },
        config={},
    )
    assert res_find.status == "success"
    assert res_find.outputs["facts_count"] == 1

    # 5. delete_fact
    res_del = await mod.execute(
        context,
        inputs={
            "operation": "delete_fact",
            "workspace_id": context.workspace_id,
            "principal_id": context.principal_id,
            "fact_id": f_id,
        },
        config={},
    )
    assert res_del.status == "success"
