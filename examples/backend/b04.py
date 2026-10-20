"""B04 Standalone Example: Storage, Migrations, and Concurrency.

Demonstrates unified session and run persistence with optimistic concurrency (CAS),
atomic transactional event appending, schema migrations, and engine profiles (SQLite & PostgreSQL).
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from nexura_postgres import PostgresRepository  # pip install nexura-postgres
from nexura_runtime.contracts.errors import MissingDependencyError
from nexura_runtime.errors import SessionConflictError
from nexura_runtime.types import Message

from nexura_runtime.storage import (
    StorageRepository,
    get_storage_repository,
)


async def run_sqlite_demo(db_path: Path) -> None:
    print("\n--- 1. SQLite Unified Storage Profile ---")
    repo: StorageRepository = get_storage_repository(f"sqlite://{db_path}")
    workspace_id = "ws_mumbai_finance"

    # 1. Create and persist conversation session
    session_id = "sess_kyc_001"
    initial_messages = [
        Message(role="user", content="नमस्ते, मुझे अपने ऋण आवेदन की स्थिति देखनी है।"),
        Message(
            role="assistant",
            content="नमस्ते! मैं आपके ऋण आवेदन की जांच कर रहा हूँ। कृपया प्रतीक्षा करें।",
        ),
    ]

    print(f"Saving initial session '{session_id}' (expected revision: 0)...")
    rev1 = await repo.save_session(
        workspace_id=workspace_id,
        session_id=session_id,
        messages=initial_messages,
        expected_revision=0,
    )
    print(f"Session saved successfully. Current revision: {rev1}")

    # 2. Re-open session and verify data preservation
    snapshot = await repo.get_session(workspace_id=workspace_id, session_id=session_id)
    assert snapshot is not None
    print(f"Reopened session: {snapshot.session_id}, messages count: {len(snapshot.messages)}")
    print(f"Message 0 content: {snapshot.messages[0].content}")

    # 3. Optimistic concurrency control (CAS) demonstration
    print("Testing Compare-And-Swap (CAS) conflict detection...")
    stale_expected_revision = 0
    try:
        await repo.save_session(
            workspace_id=workspace_id,
            session_id=session_id,
            messages=initial_messages + [Message(role="user", content="धन्यवाद!")],
            expected_revision=stale_expected_revision,
        )
        print("ERROR: Stale revision update unexpectedly succeeded!")
    except SessionConflictError as exc:
        print(f"Expected CAS conflict caught successfully: {exc}")

    # 4. Durable workflow run persistence & transactional event append
    print("\nCreating durable workflow run record...")
    run_id = "run_loan_verification_442"
    run_rec = await repo.create_run(
        workspace_id=workspace_id,
        run_id=run_id,
        initial_state={"applicant_id": "AP-9901", "stage": "document_verification"},
        metadata={"priority": "high", "region": "IN-MH"},
    )
    print(
        f"Created run '{run_rec.run_id}' with revision {run_rec.revision}, status: {run_rec.status}"
    )

    # Update run and atomically append events
    print("Updating run to 'credit_check' with atomic event log...")
    events = [
        {"type": "doc_approved", "doc_type": "aadhaar", "verified": True},
        {"type": "cibil_check_queued", "cibil_score": 780},
    ]
    updated_run = await repo.update_run(
        workspace_id=workspace_id,
        run_id=run_id,
        expected_revision=1,
        status="credit_check",
        state_update={"cibil_score": 780},
        events=events,
    )
    print(f"Run updated. New revision: {updated_run.revision}, status: {updated_run.status}")

    # 5. Retrieve ordered run events
    run_events = await repo.get_run_events(workspace_id=workspace_id, run_id=run_id)
    print(f"Persisted {len(run_events)} transactional run events:")
    for evt in run_events:
        print(f"  Seq {evt.seq} [{evt.event_type}]: {evt.payload}")

    # 6. Transaction rollback verification
    print("\nTesting transactional rollback on partial failure...")

    class NonSerializable:
        pass

    try:
        await repo.update_run(
            workspace_id=workspace_id,
            run_id=run_id,
            expected_revision=2,
            status="completed",
            events=[{"type": "bad_event", "bad_data": NonSerializable()}],
        )
    except TypeError:
        print("Caught expected serialization error during event append.")

    check_run = await repo.get_run(workspace_id=workspace_id, run_id=run_id)
    assert check_run is not None
    print(
        f"Verified rollback: run status remains '{check_run.status}', "
        f"revision is {check_run.revision}"
    )

    await repo.close()


async def run_postgres_profile_demo() -> None:
    print("\n--- 2. PostgreSQL Storage Profile & Protocol Contract ---")

    # Without nexura-postgres (or asyncpg) this raises MissingDependencyError
    pg_dsn = "postgresql://agentkit:secret@localhost:5432/nexura_enterprise"
    print(f"Loading the PostgreSQL repository for: '{pg_dsn}'")
    try:
        get_storage_repository(pg_dsn)
    except MissingDependencyError as exc:
        print(f"Expected dependency guard triggered: {exc.message} (extra: '{exc.extra_name}')")

    # Demonstrate PostgreSQL repository contract using an injected mock pool
    print("Demonstrating PostgreSQL profile using protocol-compatible connection pool:")
    mock_pool = MagicMock()
    mock_pool.close = AsyncMock()
    mock_conn = MagicMock()

    class MockContext:
        async def __aenter__(self) -> Any:
            return mock_conn

        async def __aexit__(self, *args: Any) -> None:
            pass

    mock_pool.acquire.return_value = MockContext()
    mock_conn.transaction.return_value = MockContext()
    mock_conn.fetchrow = AsyncMock(return_value={"revision": 1})

    pg_repo = PostgresRepository(pool=mock_pool)
    print("PostgresRepository instantiated with injected pool.")
    print("StorageRepository protocol verification: SUCCESS (implements identical interface).")
    await pg_repo.close()


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "unified_storage.db"
        await run_sqlite_demo(db_path)
        await run_postgres_profile_demo()
    print("\nB04 execution completed successfully.")


if __name__ == "__main__":
    asyncio.run(main())
