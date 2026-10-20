"""First-write session conflicts must have the same protocol error as later CAS."""

import asyncio
import os
import uuid

import pytest
from nexura_postgres import PostgresRepository
from nexura_runtime.errors import SessionConflictError


@pytest.mark.asyncio
async def test_concurrent_new_session_has_one_winner_and_one_cas_conflict() -> None:
    dsn = os.environ.get("NEXURA_TEST_POSTGRES_URI")
    if not dsn:
        pytest.skip("Disposable PostgreSQL required")
    repositories = [PostgresRepository(dsn), PostgresRepository(dsn)]
    session_id = uuid.uuid4().hex
    try:
        await repositories[0].init_db()
        outcomes = await asyncio.gather(
            *(repo.save_session("review", session_id, [], 0) for repo in repositories),
            return_exceptions=True,
        )
        assert outcomes.count(1) == 1
        assert sum(isinstance(result, SessionConflictError) for result in outcomes) == 1
    finally:
        await asyncio.gather(*(repo.close() for repo in repositories))
