# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Numbered, checksum-verified migrations for new service components.

Every entry point takes the one shared transaction-level advisory lock
(``MIGRATION_LOCK``) before it reads or writes the ledger, so concurrent first uses and
``migrate_all`` on other instances apply each migration exactly once. Callers cannot pick
another lock. All pending migrations of one call run in one transaction: a statement that
cannot run inside a transaction (``CREATE INDEX CONCURRENTLY``) is not supported. Waiting
for the lock and each statement are bounded by ``MIGRATION_TIMEOUT`` seconds, not by the
pool's 30-second ``command_timeout``; callers that use ``Database.run`` pass
``timeout=MIGRATION_TIMEOUT + 30``.

Existing execution/workflow/trigger ledgers deliberately remain independent.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

Migration = tuple[int, str, tuple[str, ...]]
MIGRATION_LOCK = 734658110
MIGRATION_TIMEOUT = 300.0
COMPONENTS: dict[str, tuple[Migration, ...]] = {}
LEDGER_DDL = """CREATE TABLE IF NOT EXISTS nexura_schema_migrations (
    component TEXT NOT NULL, version INT NOT NULL, name TEXT NOT NULL,
    checksum TEXT NOT NULL, applied_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (component, version))"""


def register(component: str, migrations: Sequence[Migration]) -> None:
    """Add ``component`` to ``COMPONENTS`` (used by ``migrate_all`` and ``db migrate|status``).

    Registering the same migrations again is a no-op; different ones for a registered
    component are refused, so two modules can never fight over one ledger entry.
    """
    validate_migrations(migrations)
    existing = COMPONENTS.get(component)
    if existing is not None and tuple(existing) != tuple(migrations):
        raise ValueError(f"Migration component {component!r} is already registered differently")
    COMPONENTS[component] = tuple(migrations)


def checksum(migration: Migration) -> str:
    return hashlib.sha256(json.dumps(migration, ensure_ascii=True).encode()).hexdigest()


def validate_migrations(migrations: Sequence[Migration]) -> None:
    if [m[0] for m in migrations] != list(range(1, len(migrations) + 1)):
        raise ValueError("Migration versions must be contiguous starting at 1")


def check_applied(component: str, migrations: Sequence[Migration], rows: Any) -> int:
    validate_migrations(migrations)
    if [row["version"] for row in rows] != list(range(1, len(rows) + 1)):
        raise RuntimeError(f"Non-contiguous migration ledger for {component}")
    if len(rows) > len(migrations):
        raise RuntimeError(f"Database schema for {component} is newer than this application")
    for row, migration in zip(rows, migrations, strict=False):
        if row["checksum"] != checksum(migration) or row["name"] != migration[1]:
            raise RuntimeError(f"Migration checksum mismatch for {component} v{migration[0]}")
    return len(rows)


async def _apply(conn: Any, component: str, migrations: Sequence[Migration]) -> None:
    rows = await conn.fetch(
        "SELECT version, name, checksum FROM nexura_schema_migrations "
        "WHERE component=$1 ORDER BY version",
        component,
        timeout=MIGRATION_TIMEOUT,
    )
    applied = check_applied(component, migrations, rows)
    for migration in migrations[applied:]:
        version, name, statements = migration
        for sql in statements:
            await conn.execute(sql, timeout=MIGRATION_TIMEOUT)
        await conn.execute(
            "INSERT INTO nexura_schema_migrations "
            "(component, version, name, checksum, applied_at) "
            "VALUES ($1,$2,$3,$4,EXTRACT(EPOCH FROM clock_timestamp()))",
            component,
            version,
            name,
            checksum(migration),
            timeout=MIGRATION_TIMEOUT,
        )


async def apply_migrations(conn: Any, component: str, migrations: Sequence[Migration]) -> None:
    """Apply one component's pending migrations under the shared ``MIGRATION_LOCK``."""
    await migrate_all(conn, {component: migrations})


async def migrate_all(
    conn: Any,
    components: Mapping[str, Sequence[Migration]] | None = None,
) -> None:
    """Apply every registered component (or ``components``) in one transaction and lock."""
    selected = COMPONENTS if components is None else components
    for migrations in selected.values():
        validate_migrations(migrations)
    async with conn.transaction():
        # The lock comes first: it serialises the ledger DDL and every ledger read.
        await conn.execute(
            "SELECT pg_advisory_xact_lock($1)", MIGRATION_LOCK, timeout=MIGRATION_TIMEOUT
        )
        await conn.execute(LEDGER_DDL, timeout=MIGRATION_TIMEOUT)
        for component, migrations in selected.items():
            await _apply(conn, component, migrations)


async def migration_status(conn: Any) -> dict[str, str]:
    """Read-only operator status, including unknown (ahead) components."""
    exists = await conn.fetchval("SELECT to_regclass('nexura_schema_migrations')")
    rows = (
        await conn.fetch(
            "SELECT component, version, name, checksum FROM nexura_schema_migrations "
            "ORDER BY component, version"
        )
        if exists
        else []
    )
    result: dict[str, str] = {}
    for component in sorted(set(COMPONENTS) | {row["component"] for row in rows}):
        migrations = COMPONENTS.get(component, ())
        try:
            applied = check_applied(
                component, migrations, [row for row in rows if row["component"] == component]
            )
            result[component] = "current" if applied == len(migrations) else "behind"
        except RuntimeError as exc:
            result[component] = str(exc)
    return result
