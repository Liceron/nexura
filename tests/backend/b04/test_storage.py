"""Acceptance tests for B04: Storage, migrations, and concurrency."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from nexura_postgres import PostgresRepository
from nexura_runtime.contracts.errors import MissingDependencyError
from nexura_runtime.errors import SessionConflictError, StorageError
from nexura_runtime.session import serialize_session
from nexura_runtime.types import Message

from nexura_runtime.storage import (
    SQLiteRepository,
    get_storage_repository,
)


@pytest.fixture
def temp_db(tmp_path: Path) -> Path:
    return tmp_path / "test_storage.db"


@pytest.mark.asyncio
async def test_reopen_old_sqlite_fixtures_after_migration(temp_db: Path) -> None:
    """Acceptance case: Reopen old SQLite fixtures after migration."""
    # 1. Create legacy schema and populate data as existing 0.3 system did
    legacy_conn = sqlite3.connect(str(temp_db))
    legacy_conn.execute("""
        CREATE TABLE nexura_sessions (
            session_id TEXT PRIMARY KEY,
            revision INTEGER NOT NULL,
            data TEXT NOT NULL,
            updated_at REAL NOT NULL
        );
    """)
    legacy_conn.execute("""
        CREATE TABLE nexura_workflow_checkpoints (
            workflow_run_id TEXT PRIMARY KEY,
            spec_version INTEGER NOT NULL,
            spec_hash TEXT NOT NULL,
            current_node_id TEXT,
            state_json TEXT NOT NULL,
            step_count INTEGER NOT NULL,
            status TEXT NOT NULL,
            approval_id TEXT,
            approval_json TEXT,
            error TEXT,
            updated_at REAL NOT NULL
        );
    """)

    # Populate legacy session with Indic Hindi text
    msg = Message(role="user", content="नमस्ते, खाता शेष क्या है?")
    raw_session_data = serialize_session("sess-legacy-1", 3, [msg])
    legacy_conn.execute(
        "INSERT INTO nexura_sessions (session_id, revision, data, updated_at) VALUES (?, ?, ?, ?)",
        ("sess-legacy-1", 3, raw_session_data, time.time()),
    )

    # Populate legacy workflow checkpoint
    state_payload = {"account_id": "ACC-9988", "balance": 15000}
    legacy_conn.execute(
        "INSERT INTO nexura_workflow_checkpoints "
        "(workflow_run_id, spec_version, spec_hash, current_node_id, state_json, step_count, "
        "status, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "wf-legacy-run-1",
            1,
            "hash123",
            "node_check",
            json.dumps(state_payload),
            2,
            "completed",
            time.time(),
        ),
    )
    legacy_conn.commit()

    legacy_conn.close()

    # 2. Open via unified SQLiteRepository (which automatically executes run_sqlite_migrations)
    repo = SQLiteRepository(temp_db)

    # Verify session migration to workspace 'default'
    snapshot = await repo.get_session("default", "sess-legacy-1")
    assert snapshot is not None
    assert snapshot.session_id == "sess-legacy-1"
    assert snapshot.revision == 3
    assert len(snapshot.messages) == 1
    assert snapshot.messages[0].content == "नमस्ते, खाता शेष क्या है?"

    # Verify checkpoint migration to runs in workspace 'default'
    run_rec = await repo.get_run("default", "wf-legacy-run-1")
    assert run_rec is not None
    assert run_rec.run_id == "wf-legacy-run-1"
    assert run_rec.status == "completed"
    assert run_rec.state["account_id"] == "ACC-9988"
    assert run_rec.state["balance"] == 15000

    await repo.close()


@pytest.mark.asyncio
async def test_cas_concurrency_across_independent_connections(temp_db: Path) -> None:
    """Acceptance case: Two independent connections cannot win the same CAS."""
    repo1 = SQLiteRepository(temp_db)
    repo2 = SQLiteRepository(temp_db)

    # Create run on repo1
    run_id = "run-concurrent-cas"
    await repo1.create_run(
        workspace_id="ws_finance",
        run_id=run_id,
        initial_state={"status_text": "initialized"},
    )

    # Both read revision 1
    rec1 = await repo1.get_run("ws_finance", run_id)
    rec2 = await repo2.get_run("ws_finance", run_id)
    assert rec1 is not None and rec2 is not None
    assert rec1.revision == 1
    assert rec2.revision == 1

    # repo1 performs CAS update 1 -> 2
    updated1 = await repo1.update_run(
        workspace_id="ws_finance",
        run_id=run_id,
        expected_revision=1,
        status="step1_done",
        state_update={"step1": True},
    )
    assert updated1.revision == 2
    assert updated1.status == "step1_done"

    # repo2 attempts CAS update using stale expected_revision=1
    with pytest.raises(SessionConflictError) as exc_info:
        await repo2.update_run(
            workspace_id="ws_finance",
            run_id=run_id,
            expected_revision=1,
            status="step2_done",
            state_update={"step2": True},
        )
    assert exc_info.value.expected_revision == 1
    assert exc_info.value.actual_revision == 2

    # Verify session save CAS behaves identically across independent instances
    sess_id = "sess-cas"
    msg1 = Message(role="user", content="Hello")
    rev1 = await repo1.save_session("ws_finance", sess_id, [msg1], expected_revision=0)
    assert rev1 == 1

    msg2a = Message(role="assistant", content="Response A")
    msg2b = Message(role="assistant", content="Response B")

    # repo1 advances to rev 2
    rev2 = await repo1.save_session("ws_finance", sess_id, [msg1, msg2a], expected_revision=1)
    assert rev2 == 2

    # repo2 tries to advance with expected_revision=1 -> must raise SessionConflictError
    with pytest.raises(SessionConflictError):
        await repo2.save_session("ws_finance", sess_id, [msg1, msg2b], expected_revision=1)

    await repo1.close()
    await repo2.close()


@pytest.mark.asyncio
async def test_partial_transaction_failure_leaves_no_phantom_completion(temp_db: Path) -> None:
    """Acceptance case: Partial transaction failure leaves no phantom completion."""
    repo = SQLiteRepository(temp_db)
    run_id = "run-tx-failure"
    await repo.create_run("ws_ops", run_id, initial_state={"step": 0})

    # Intentionally trigger an error during the transaction by inserting an event with
    # non-serializable content or patching conn.execute on event insert
    class UnserializableObject:
        pass

    bad_events: list[dict[str, Any]] = [
        {"type": "valid_event", "data": "ok"},
        {"type": "invalid_event", "data": UnserializableObject()},  # canonical_json will fail
    ]

    with pytest.raises(TypeError):
        await repo.update_run(
            workspace_id="ws_ops",
            run_id=run_id,
            expected_revision=1,
            status="completed",
            state_update={"step": 1},
            events=bad_events,
        )

    # Verify atomic rollback: the run status must NOT be 'completed' and revision must still be 1
    run_after = await repo.get_run("ws_ops", run_id)
    assert run_after is not None
    assert run_after.status == "running"
    assert run_after.revision == 1
    assert run_after.state == {"step": 0}

    # Verify no orphan events were persisted
    events = await repo.get_run_events("ws_ops", run_id)
    assert len(events) == 0

    await repo.close()


@pytest.mark.asyncio
async def test_workspace_isolation(temp_db: Path) -> None:
    """Verify strict workspace partition isolation."""
    repo = SQLiteRepository(temp_db)

    # Same run_id in two workspaces
    await repo.create_run("ws_alpha", "run_shared_id", initial_state={"tenant": "alpha"})
    await repo.create_run("ws_beta", "run_shared_id", initial_state={"tenant": "beta"})

    run_a = await repo.get_run("ws_alpha", "run_shared_id")
    run_b = await repo.get_run("ws_beta", "run_shared_id")
    assert run_a is not None and run_b is not None
    assert run_a.state["tenant"] == "alpha"
    assert run_b.state["tenant"] == "beta"

    # Updating alpha leaves beta unaffected
    await repo.update_run(
        "ws_alpha",
        "run_shared_id",
        expected_revision=1,
        status="alpha_complete",
    )

    run_a_updated = await repo.get_run("ws_alpha", "run_shared_id")
    run_b_unchanged = await repo.get_run("ws_beta", "run_shared_id")
    assert run_a_updated is not None and run_a_updated.status == "alpha_complete"
    assert run_a_updated.revision == 2
    assert run_b_unchanged is not None and run_b_unchanged.status == "running"
    assert run_b_unchanged.revision == 1

    await repo.close()


@pytest.mark.asyncio
async def test_postgres_repository_mock_and_dependency_guards() -> None:
    """Verify PostgreSQL repository error handling and transaction semantics."""
    # When asyncpg is not installed, factory raises MissingDependencyError
    with (
        patch.dict("sys.modules", {"asyncpg": None}),
        pytest.raises(MissingDependencyError) as exc_info,
    ):
        get_storage_repository("postgresql://postgres:secret@localhost:5432/nexura_test")
    # asyncpg is a dependency of the nexura-postgres package (no longer a runtime extra).
    assert exc_info.value.dependency_name == "asyncpg"
    assert "pip install nexura-postgres" in str(exc_info.value)

    # Test PostgresRepository protocol operations using an injected mock asyncpg pool
    mock_pool = MagicMock()
    mock_conn = MagicMock()

    class MockTransactionContext:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
            return None

    class MockAcquireContext:
        async def __aenter__(self) -> MagicMock:
            return mock_conn

        async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
            return None

    mock_pool.acquire.return_value = MockAcquireContext()
    mock_conn.transaction.return_value = MockTransactionContext()

    pg_repo = PostgresRepository(pool=mock_pool)

    # 1. Test get_session not found
    mock_conn.fetchrow = AsyncMock(return_value=None)
    sess = await pg_repo.get_session("ws_1", "missing_sess")
    assert sess is None

    # 2. Test CAS conflict on save_session
    mock_conn.fetchrow = AsyncMock(return_value={"revision": 2})
    with pytest.raises(SessionConflictError) as exc_cas:
        await pg_repo.save_session(
            "ws_1",
            "sess_1",
            [Message(role="user", content="Hi")],
            expected_revision=1,
        )
    assert exc_cas.value.expected_revision == 1
    assert exc_cas.value.actual_revision == 2

    # 3. Test update_run not found
    mock_conn.fetchrow = AsyncMock(return_value=None)
    with pytest.raises(StorageError):
        await pg_repo.update_run("ws_1", "unknown_run", expected_revision=1, status="done")


@pytest.mark.asyncio
async def test_postgres_disposable_service_integration() -> None:
    """Acceptance case: PostgreSQL integration tests use a disposable service."""
    pg_dsn = os.getenv("NEXURA_TEST_POSTGRES_URI")
    if not pg_dsn:
        pytest.skip("Disposable PostgreSQL service not configured (set NEXURA_TEST_POSTGRES_URI)")

    # If configured, run live integration against the disposable service
    try:
        import asyncpg  # type: ignore[import-untyped]  # noqa: F401
    except ImportError:
        pytest.skip("asyncpg extra not installed in current environment")

    repo = PostgresRepository(dsn=pg_dsn)
    await repo.init_db()

    run_id = f"test-pg-run-{int(time.time())}"
    rec = await repo.create_run("ws_pg_test", run_id, initial_state={"step": 0})
    assert rec.revision == 1

    updated = await repo.update_run(
        "ws_pg_test",
        run_id,
        expected_revision=1,
        status="step1",
        state_update={"step": 1},
        events=[{"type": "step_finished", "step": 1}],
    )
    assert updated.revision == 2

    events = await repo.get_run_events("ws_pg_test", run_id)
    assert len(events) == 1
    assert events[0].event_type == "step_finished"

    await repo.close()
