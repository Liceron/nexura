"""Workflow test fixtures: a Postgres workflow store in its own throwaway schema (A6 S8).

``NEXURA_TEST_POSTGRES_URI`` must name a disposable database; without it the Postgres
parameters skip. Each test gets a fresh schema (dropped afterwards), so fixed run ids such
as ``r1`` do not collide between tests.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Iterator
from typing import Any

import pytest

POSTGRES_ENV = "NEXURA_TEST_POSTGRES_URI"


def postgres_uri() -> str:
    uri = os.environ.get(POSTGRES_ENV)
    if not uri:
        pytest.skip(f"{POSTGRES_ENV} is not configured")
    return uri


def in_schema(uri: str, schema: str) -> str:
    """``uri`` with ``search_path`` set to ``schema`` (asyncpg passes unknown query
    parameters to the server as settings)."""
    return f"{uri}{'&' if '?' in uri else '?'}search_path={schema}"


async def _admin(uri: str, statement: str) -> None:
    import asyncpg  # type: ignore[import-untyped]

    conn = await asyncpg.connect(uri)
    try:
        await conn.execute(statement)
    finally:
        await conn.close()


class PostgresSchema:
    """A throwaway schema; ``store()`` opens a ``PostgresWorkflowStore`` in it."""

    def __init__(self, uri: str) -> None:
        self.uri = uri
        self.name = f"wf_test_{uuid.uuid4().hex[:12]}"
        self.dsn = in_schema(uri, self.name)
        self._stores: list[Any] = []

    def store(self, **kwargs: Any) -> Any:
        from nexura_postgres import PostgresWorkflowStore

        opened = PostgresWorkflowStore(self.dsn, **kwargs)
        self._stores.append(opened)
        return opened

    def create(self) -> None:
        asyncio.run(_admin(self.uri, f'CREATE SCHEMA "{self.name}"'))

    def drop(self) -> None:
        for opened in self._stores:
            opened.close_sync()
        asyncio.run(_admin(self.uri, f'DROP SCHEMA IF EXISTS "{self.name}" CASCADE'))


@pytest.fixture
def postgres_schema() -> Iterator[PostgresSchema]:
    schema = PostgresSchema(postgres_uri())
    schema.create()
    try:
        yield schema
    finally:
        schema.drop()


@pytest.fixture
def postgres_workflow_store(postgres_schema: PostgresSchema) -> Any:
    return postgres_schema.store()
