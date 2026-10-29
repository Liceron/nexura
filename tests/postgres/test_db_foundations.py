# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""WP0 foundation contracts. Fakes model transactions, never a live provider."""

from __future__ import annotations

import ast
import asyncio
import copy
import threading
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from nexura_postgres.db import Database, normalize_url, pool_options
from nexura_postgres.migrations import apply_migrations, checksum, migrate_all

MIGRATIONS = ((1, "first", ("CREATE TABLE example (id TEXT)",)),)


class FakeLedger:
    def __init__(self) -> None:
        self.rows: dict[str, list[dict[str, Any]]] = {}
        self.statements: list[str] = []
        self.lock = asyncio.Lock()


class FakeConnection:
    def __init__(self, ledger: FakeLedger) -> None:
        self.ledger = ledger
        self.locked = False
        self.transaction_depth = 0
        self.snapshot: dict[str, Any] = {}

    def transaction(self) -> Any:
        connection = self

        class Transaction:
            async def __aenter__(self) -> None:
                connection.transaction_depth += 1

            async def __aexit__(self, *exc: Any) -> None:
                connection.transaction_depth -= 1
                if connection.locked:
                    if exc[0] is not None:
                        connection.ledger.rows = connection.snapshot
                    connection.locked = False
                    connection.ledger.lock.release()

        return Transaction()

    async def execute(self, sql: str, *args: Any, **kwargs: Any) -> None:
        assert self.transaction_depth, "DDL and writes must be transactional"
        if "pg_advisory_xact_lock" in sql:
            assert args == (734658110,)
            await self.ledger.lock.acquire()
            self.snapshot = copy.deepcopy(self.ledger.rows)
            self.locked = True
        elif sql.startswith("INSERT INTO nexura_schema_migrations"):
            component, version, name, digest = args
            self.ledger.rows.setdefault(component, []).append(
                {"version": version, "name": name, "checksum": digest}
            )
        self.ledger.statements.append(sql)
        await asyncio.sleep(0)  # force competing first uses to interleave

    async def fetchval(self, sql: str) -> bool:
        assert "to_regclass" in sql
        return True

    async def fetch(self, sql: str, component: str | None = None, **kwargs: Any) -> Any:
        if component is None:
            return [
                {"component": name, **row}
                for name, rows in self.ledger.rows.items()
                for row in rows
            ]
        return copy.deepcopy(self.ledger.rows.get(component, []))


async def test_concurrent_first_uses_apply_once() -> None:
    ledger = FakeLedger()
    await asyncio.gather(
        *(apply_migrations(FakeConnection(ledger), "example", MIGRATIONS) for _ in range(2))
    )
    assert ledger.statements.count("CREATE TABLE example (id TEXT)") == 1
    assert len(ledger.rows["example"]) == 1
    assert ledger.rows["example"][0]["checksum"] == checksum(MIGRATIONS[0])


async def test_newer_database_refused_before_ddl() -> None:
    ledger = FakeLedger()
    conn = FakeConnection(ledger)
    await apply_migrations(conn, "example", MIGRATIONS)
    with pytest.raises(RuntimeError, match="newer"):
        await apply_migrations(conn, "example", ())
    assert ledger.statements.count("CREATE TABLE example (id TEXT)") == 1


async def test_checksum_mismatch_refused_before_ddl() -> None:
    ledger = FakeLedger()
    conn = FakeConnection(ledger)
    await apply_migrations(conn, "example", MIGRATIONS)
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        await apply_migrations(conn, "example", ((1, "first", ("ALTER TABLE example",)),))
    assert "ALTER TABLE example" not in ledger.statements


async def test_all_components_share_one_lock() -> None:
    ledger = FakeLedger()
    await migrate_all(FakeConnection(ledger), {"a": MIGRATIONS, "b": MIGRATIONS})
    assert set(ledger.rows) == {"a", "b"}
    assert sum("pg_advisory_xact_lock" in sql for sql in ledger.statements) == 1


@pytest.mark.parametrize("migrations", [((2, "gap", ()),), ((1, "a", ()), (1, "b", ()))])
async def test_invalid_migration_sequence_refused(migrations: Any) -> None:
    with pytest.raises(ValueError, match="contiguous"):
        await apply_migrations(FakeConnection(FakeLedger()), "example", migrations)


def test_url_normalization_keeps_ssl_and_never_logs_credentials(caplog: Any) -> None:
    url = "postgres://user:secret@fixture-pooler.example/db?sslmode=require&channel_binding=require"
    with caplog.at_level("INFO"):
        result = normalize_url(url)
    assert result.startswith("postgresql://")
    assert parse_qs(urlsplit(result).query) == {"sslmode": ["require"]}
    assert "channel_binding" in caplog.text
    assert "secret" not in caplog.text
    assert "fixture-pooler" not in caplog.text
    assert normalize_url(result) == result


@pytest.mark.parametrize("url", [None, "", "sqlite:///db", "postgresql:///db"])
def test_bad_urls_rejected(url: Any) -> None:
    with pytest.raises(ValueError, match="PostgreSQL URL"):
        normalize_url(url)


def test_socket_host_option_is_a_host() -> None:
    url = "postgresql://user@/db?host=/cloudsql/project:region:instance"
    assert normalize_url(url) == url


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql:///db",  # local Unix socket, accepted by asyncpg before WP0
        "dbname=db user=fixture",  # keyword DSN
        "postgresql://user@fixture.example/db?search_path=s1,s2",
    ],
)
def test_existing_store_dsns_pass_through_unchanged(dsn: str) -> None:
    assert normalize_url(dsn, strict=False) == dsn


def test_existing_store_dsns_still_drop_channel_binding() -> None:
    dsn = "postgres://user@fixture.example/db?channel_binding=require&sslmode=require"
    assert (
        normalize_url(dsn, strict=False) == "postgresql://user@fixture.example/db?sslmode=require"
    )


def test_pool_defaults_and_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NEXURA_DATABASE_POOL_MAX_SIZE", raising=False)
    options = pool_options()
    assert {
        key: options[key]
        for key in (
            "min_size",
            "max_size",
            "statement_cache_size",
            "command_timeout",
            "max_inactive_connection_lifetime",
        )
    } == dict(
        min_size=0,
        max_size=5,
        statement_cache_size=0,
        command_timeout=30,
        max_inactive_connection_lifetime=60,
    )
    monkeypatch.setenv("NEXURA_DATABASE_POOL_MAX_SIZE", "9")
    assert pool_options()["max_size"] == 9
    monkeypatch.setenv("NEXURA_DATABASE_POOL_MAX_SIZE", "0")
    with pytest.raises(ValueError, match="positive"):
        pool_options()


async def test_shared_pool_bridge_transaction_and_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncpg

    calls: list[Any] = []

    class Connection:
        depth = 0

        def transaction(self) -> Any:
            return self

        async def __aenter__(self) -> Connection:
            self.depth += 1
            return self

        async def __aexit__(self, *exc: Any) -> None:
            self.depth -= 1

        async def fetchval(self, sql: str, **kwargs: Any) -> int:
            assert self.depth == 2  # acquisition plus transaction
            assert sql == "SELECT 1"
            return 1

    class Pool:
        closed = False

        def acquire(self) -> Connection:
            return Connection()

        async def close(self) -> None:
            self.closed = True

    pool = Pool()

    async def create_pool(url: str, **options: Any) -> Pool:
        calls.append((url, options, threading.get_ident()))
        await options["reset"](None)
        return pool

    monkeypatch.setattr(asyncpg, "create_pool", create_pool)
    db = Database.from_url("postgresql://user@fake/db")
    shared = Database.from_url("postgres://user@fake/db")
    assert db is shared
    try:
        assert await asyncio.gather(
            *(db.arun(lambda conn: conn.fetchval("SELECT 1")) for _ in range(2))
        ) == [1, 1]
        assert db.ping()
        assert await db.arun(lambda conn: conn.fetchval("SELECT 1")) == 1
        assert len(calls) == 1
        assert calls[0][1]["statement_cache_size"] == 0
        assert calls[0][2] != threading.get_ident()
        db.close()
        assert not pool.closed
        assert shared.ping()
    finally:
        shared.close()
    assert pool.closed
    assert not db._bridge.thread.is_alive()
    with pytest.raises(RuntimeError, match="closed"):
        db.ping()


def test_all_six_existing_pools_disable_statement_cache() -> None:
    root = Path(__file__).resolve().parents[2] / "packages/nexura-postgres/src/nexura_postgres"
    names = {
        "repository",
        "pgvector",
        "execution_store",
        "checkpoint_store",
        "workflow_store",
        "trigger_store",
    }
    for name in names:
        tree = ast.parse((root / f"{name}.py").read_text())
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "create_pool"
        ]
        assert len(calls) == 1, name
        options = {kw.arg: ast.unparse(kw.value) for kw in calls[0].keywords}
        assert options["statement_cache_size"] == "0", name
        assert options["reset"] == "noop_reset", name
        assert ast.unparse(calls[0].args[0]).startswith("normalize_url("), name
        assert ast.unparse(calls[0].args[0]).endswith("strict=False)"), name


async def test_real_concurrent_migrations(service_database_url: str) -> None:
    """Direct-Postgres contract; skipped cleanly without an explicitly supplied URI."""
    db = Database.from_url(service_database_url)
    try:
        await asyncio.gather(
            *(db.arun(lambda conn: apply_migrations(conn, "example", MIGRATIONS)) for _ in range(2))
        )
        assert (
            await db.arun(
                lambda conn: conn.fetchval(
                    "SELECT count(*) FROM nexura_schema_migrations WHERE component='example'"
                )
            )
            == 1
        )
    finally:
        await db.aclose()


async def test_real_newer_checksum_and_failed_migration_refused(
    service_database_url: str,
) -> None:
    """Direct Postgres: refusals happen before any DDL, and a failed migration rolls back."""
    db = Database.from_url(service_database_url)

    async def tables(conn: Any) -> set[str]:
        rows = await conn.fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"
        )
        return {row["tablename"] for row in rows}

    try:
        await db.arun(lambda conn: apply_migrations(conn, "example", MIGRATIONS))
        with pytest.raises(RuntimeError, match="newer"):
            await db.arun(lambda conn: apply_migrations(conn, "example", ()))
        changed = ((1, "first", ("CREATE TABLE changed (id TEXT)",)),)
        with pytest.raises(RuntimeError, match="checksum mismatch"):
            await db.arun(lambda conn: apply_migrations(conn, "example", changed))
        broken = (
            (1, "first", ("CREATE TABLE half_done (id TEXT)",)),
            (2, "second", ("CREATE TABLE broken (",)),
        )
        with pytest.raises(Exception, match="syntax"):
            await db.arun(lambda conn: apply_migrations(conn, "broken", broken))
        assert await db.arun(tables) == {"example", "nexura_schema_migrations"}
        assert (
            await db.arun(
                lambda conn: conn.fetchval(
                    "SELECT count(*) FROM nexura_schema_migrations WHERE component='broken'"
                )
            )
            == 0
        )
    finally:
        await db.aclose()


async def test_real_pool_holds_no_named_prepared_statements(service_database_url: str) -> None:
    """``statement_cache_size=0``: parameterised queries leave no named server statement."""
    db = Database.from_url(service_database_url)

    async def work(conn: Any) -> int:
        for value in range(3):
            assert await conn.fetchval("SELECT $1::int + 1", value) == value + 1
        return int(await conn.fetchval("SELECT count(*) FROM pg_prepared_statements"))

    try:
        assert await db.arun(work) == 0
        assert db.ping()
    finally:
        await db.aclose()


def test_real_db_cli_migrate_and_status(
    service_database_url: str, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    import json

    from nexura_postgres import migrations

    from nexura_runtime.cli.main import main

    monkeypatch.setattr(migrations, "COMPONENTS", {"wp0_example": MIGRATIONS})
    argv = ["--database-url", service_database_url]
    assert main(["db", "status", *argv]) == 1
    assert json.loads(capsys.readouterr().out)["components"] == {"wp0_example": "behind"}
    assert main(["db", "migrate", *argv]) == 0
    assert json.loads(capsys.readouterr().out)["components"] == {"wp0_example": "current"}
    assert main(["db", "status", *argv]) == 0
    assert json.loads(capsys.readouterr().out)["components"] == {"wp0_example": "current"}
    monkeypatch.setattr(migrations, "COMPONENTS", {})
    assert main(["db", "status", *argv]) == 1
    assert "newer" in json.loads(capsys.readouterr().out)["components"]["wp0_example"]


def test_real_service_probe_and_readiness(service_database_url: str) -> None:
    from nexura_runtime.release.health import HealthChecker
    from nexura_runtime.service.storage_backend import ServiceStores

    stores = ServiceStores(":memory:", service_database_url)
    try:
        assert stores.ping()
        assert HealthChecker(database=stores).check_database().state == "healthy"
    finally:
        stores.close()


async def test_real_apply_and_migrate_all_race_applies_once(service_database_url: str) -> None:
    """Review M2: first-use ``apply_migrations`` racing startup ``migrate_all`` on a fresh
    schema (the ledger table does not exist yet) applies every migration exactly once."""
    slow = (
        (1, "first", ("SELECT pg_sleep(0.3)", "CREATE TABLE raced (id TEXT)")),
        (2, "second", ("ALTER TABLE raced ADD COLUMN extra TEXT",)),
    )
    db = Database.from_url(service_database_url)
    try:
        await asyncio.gather(
            *(db.arun(lambda conn: apply_migrations(conn, "raced", slow)) for _ in range(2)),
            *(db.arun(lambda conn: migrate_all(conn, {"raced": slow})) for _ in range(2)),
        )
        rows = await db.arun(
            lambda conn: conn.fetch(
                "SELECT version FROM nexura_schema_migrations WHERE component='raced' "
                "ORDER BY version"
            )
        )
        assert [row["version"] for row in rows] == [1, 2]
    finally:
        await db.aclose()


@pytest.mark.parametrize(
    "url, warned",
    [
        ("postgresql://u:secret@remote.example/db", True),
        ("postgresql://u:secret@remote.example/db?sslmode=disable", True),
        ("postgresql://u:secret@remote.example/db?sslmode=verify-full", False),
        ("postgresql://u:secret@127.0.0.1/db", False),
    ],
)
def test_remote_url_without_tls_is_warned(url: str, warned: bool, caplog: Any) -> None:
    with caplog.at_level("WARNING"):
        normalize_url(url)
    assert ("sslmode=verify-full" in caplog.text) is warned
    assert "secret" not in caplog.text and "remote.example" not in caplog.text
