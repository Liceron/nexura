"""Postgres repository and the execution journal, on an explicitly configured disposable database.

The S3/S4 round trip of the legacy ``DurableExecutionEngine`` was removed with that engine
(A6 S12). Effect resolution on Postgres is covered by
``tests/p35/test_a6_execution_store.py::test_effect_resolution_is_once_fenced_and_audited``
(postgres parameter) and the workflow rows of ``tests/durability`` on Postgres.
"""

import os
import uuid
from urllib.parse import urlsplit, urlunsplit

import pytest
from _legacy_journal import seed_legacy
from nexura_agents.errors import StorageError
from nexura_agents.execution_store import ExecutionConflictError
from nexura_postgres import PostgresRepository
from nexura_postgres.execution_store import PostgresExecutionStore


async def test_postgres_plain_repository_runs_do_not_touch_the_journal() -> None:
    uri = os.environ.get("NEXURA_TEST_POSTGRES_URI")
    if not uri:
        pytest.skip("NEXURA_TEST_POSTGRES_URI is not configured")
    repo = PostgresRepository(uri)
    try:
        await repo.init_db()
        run_id = uuid.uuid4().hex
        created = await repo.create_run("s34", run_id, initial_state={"step": 0})
        updated = await repo.update_run("s34", run_id, created.revision, "step1", {"step": 1})
        assert updated.revision == created.revision + 1 and updated.status == "step1"
        assert getattr(repo, "_execution_store", None) is None
    finally:
        await repo.close()


@pytest.mark.parametrize("status", ["paused", "completed"])
async def test_postgres_legacy_bridge_read_update_and_resolution(status: str) -> None:
    """Lead-run profile: create/drop a disposable database using asyncpg only."""
    uri = os.environ.get("NEXURA_TEST_POSTGRES_URI")
    if not uri:
        pytest.skip("NEXURA_TEST_POSTGRES_URI is not configured (needs CREATE DATABASE)")
    import asyncpg

    database = "s12_round2_" + uuid.uuid4().hex
    admin = await asyncpg.connect(uri)
    await admin.execute(f'CREATE DATABASE "{database}"')
    parts = urlsplit(uri)
    dsn = urlunsplit(parts._replace(path="/" + database))
    store = PostgresExecutionStore(dsn)
    repo = PostgresRepository(dsn)
    try:
        historical = await seed_legacy(store, status)
        identity = historical.run.execution_id
        await store.close()
        store = PostgresExecutionStore(dsn)
        record = await repo.get_run("ws", "legacy-run")
        assert record is not None and record.status == status
        assert record.input_data == {"amount": 5}
        assert (await repo.get_run_events("ws", "legacy-run"))[-1].event_type == f"run_{status}"
        with pytest.raises(StorageError, match="removed DurableExecutionEngine"):
            await repo.update_run("ws", "legacy-run", record.revision, "running", {"bad": True})
        assert await store.get(identity) == historical
        with pytest.raises(ExecutionConflictError, match="durable_graph writes were removed"):
            await store.create(
                identity, owner="bad", definition_digest="legacy-digest", kind="durable_graph"
            )
        assert await store.get(identity) == historical
        if status == "paused":
            lease = await store.claim(identity, "ops", 30, definition_digest="legacy-digest")
            assert lease is not None
            before = await store.get(identity)
            with pytest.raises(ExecutionConflictError, match="durable_graph writes were removed"):
                await store.commit(lease, state={"bad": True}, status="completed")
            assert await store.get(identity) == before
            resolved = await store.resolve_effect(
                lease,
                next(iter(historical.effects)),
                "confirmed",
                actor="ops",
                reason="Checked fake ledger",
                result={"paid": True},
            )
            assert resolved.status.value == "resolved" and resolved.resolved_by == "ops"
            snapshot = await store.get(identity)
            assert snapshot.events[-1].payload["reason"] == "Checked fake ledger"
    finally:
        await repo.close()
        await store.close()
        await admin.execute(f'DROP DATABASE "{database}" WITH (FORCE)')
        await admin.close()
