"""Unit and acceptance tests for Module B17: SQL Data Connector."""

from __future__ import annotations

import contextlib
import os
import tempfile
from collections.abc import Generator

import pytest
from nexura_runtime.contracts.context import ExecutionContext

from nexura_runtime.integrations.sql.connection import (
    SqlConnectionConfig,
    SqlConnectionManager,
)
from nexura_runtime.integrations.sql.errors import (
    ArbitraryDsnProhibitedError,
    ByteLimitExceededError,
    ConnectionNotFoundError,
    CrossWorkspaceConnectionError,
    QueryTimeoutError,
    SqlReadOnlyViolationError,
)
from nexura_runtime.integrations.sql.modules import (
    SqlExecuteModule,
    SqlQueryModule,
    SqlSchemaInspectModule,
)


@pytest.fixture
def temp_database_file() -> Generator[str, None, None]:
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    yield path
    if os.path.exists(path):
        os.remove(path)


@pytest.fixture
def sql_setup(temp_database_file: str) -> tuple[SqlConnectionManager, str]:
    manager = SqlConnectionManager()
    init_sql = """
    CREATE TABLE users (
        id INTEGER PRIMARY KEY,
        username TEXT NOT NULL UNIQUE,
        email TEXT NOT NULL,
        role TEXT NOT NULL
    );
    CREATE TABLE posts (
        id INTEGER PRIMARY KEY,
        user_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        content TEXT NOT NULL,
        FOREIGN KEY(user_id) REFERENCES users(id)
    );
    INSERT INTO users (id, username, email, role) VALUES
        (1, 'alice', 'alice@example.com', 'admin'),
        (2, 'bob', 'bob@example.com', 'member'),
        (3, 'charlie', 'charlie@example.com', 'member');
    INSERT INTO posts (id, user_id, title, content) VALUES
        (1, 1, 'Hello World', 'Welcome to the platform'),
        (2, 2, 'First Post', 'Bob writing first post');
    """

    write_cfg = SqlConnectionConfig(
        connection_id="test_db_rw",
        workspace_id="ws_finance",
        engine="sqlite",
        database_path=temp_database_file,
        read_only=False,
        pool_size=3,
    )
    manager.register_connection(write_cfg, initial_sql=init_sql)

    ro_cfg = SqlConnectionConfig(
        connection_id="test_db_ro",
        workspace_id="ws_finance",
        engine="sqlite",
        database_path=temp_database_file,
        read_only=True,
        statement_timeout_seconds=0.4,
        max_rows=5,
        max_bytes=100_000,
        pool_size=2,
    )
    manager.register_connection(ro_cfg)

    return manager, temp_database_file


@pytest.fixture
def context_finance() -> ExecutionContext:
    return ExecutionContext(
        workspace_id="ws_finance", run_id="run_test", node_id="node_1", approval_granted=True
    )


@pytest.fixture
def context_marketing() -> ExecutionContext:
    return ExecutionContext(
        workspace_id="ws_marketing", run_id="run_test", node_id="node_1", approval_granted=True
    )


class TestSqlAcceptanceRequirements:
    """Core acceptance test suite mapped to B17 specification."""

    @pytest.mark.asyncio
    async def test_injection_looking_values_stay_bound(
        self,
        sql_setup: tuple[SqlConnectionManager, str],
        context_finance: ExecutionContext,
    ) -> None:
        """Acceptance requirement: Injection-looking values stay bound."""
        manager, _ = sql_setup
        query_mod = SqlQueryModule(manager=manager)

        # 1. OR tautology injection attempt
        malicious_input = "' OR '1'='1"
        res = await query_mod.execute(
            context_finance,
            {
                "connection_id": "test_db_ro",
                "query": "SELECT * FROM users WHERE username = ?",
                "parameters": [malicious_input],
            },
            {},
        )
        assert res.status == "success"
        # Zero rows match the literal string "' OR '1'='1"
        assert res.outputs["row_count"] == 0

        # 2. DROP TABLE stacked injection attempt
        drop_attempt = "alice'; DROP TABLE users; --"
        res2 = await query_mod.execute(
            context_finance,
            {
                "connection_id": "test_db_ro",
                "query": "SELECT * FROM users WHERE username = :uname",
                "parameters": {"uname": drop_attempt},
            },
            {},
        )
        assert res2.status == "success"
        assert res2.outputs["row_count"] == 0

        # Verify valid query returns intended user
        res_legit = await query_mod.execute(
            context_finance,
            {
                "connection_id": "test_db_ro",
                "query": "SELECT id, username, email FROM users WHERE username = ?",
                "parameters": ["alice"],
            },
            {},
        )
        assert res_legit.outputs["row_count"] == 1
        assert res_legit.outputs["rows"][0]["username"] == "alice"

    @pytest.mark.asyncio
    async def test_database_read_only_role_rejects_writes_and_cte_writes(
        self,
        sql_setup: tuple[SqlConnectionManager, str],
        context_finance: ExecutionContext,
    ) -> None:
        """Acceptance requirement: Database read-only role rejects writes/CTEs with writes."""
        manager, _ = sql_setup
        query_mod = SqlQueryModule(manager=manager)

        # 1. Direct INSERT attempt on read-only connection
        with pytest.raises(SqlReadOnlyViolationError) as exc1:
            await query_mod.execute(
                context_finance,
                {
                    "connection_id": "test_db_ro",
                    "query": "INSERT INTO users (id, username, email, role) "
                    "VALUES (99, 'hacker', 'h@bad.com', 'admin')",
                },
                {},
            )
        assert "attempt to write a readonly database" in str(exc1.value).lower()

        # 2. Direct UPDATE attempt
        with pytest.raises(SqlReadOnlyViolationError):
            await query_mod.execute(
                context_finance,
                {
                    "connection_id": "test_db_ro",
                    "query": "UPDATE users SET role = 'superadmin' WHERE username = 'bob'",
                },
                {},
            )

        # 3. CTE with INSERT/write attempt
        with pytest.raises(SqlReadOnlyViolationError):
            await query_mod.execute(
                context_finance,
                {
                    "connection_id": "test_db_ro",
                    "query": (
                        "WITH bad_cte AS (SELECT 999 AS uid) "
                        "INSERT INTO users (id, username, email, role) "
                        "SELECT uid, 'injected', 'inj@test.com', 'guest' FROM bad_cte"
                    ),
                },
                {},
            )

    @pytest.mark.asyncio
    async def test_cross_workspace_connection_denied(
        self,
        sql_setup: tuple[SqlConnectionManager, str],
        context_marketing: ExecutionContext,
    ) -> None:
        """Acceptance requirement: Cross-workspace connection denied."""
        manager, _ = sql_setup
        query_mod = SqlQueryModule(manager=manager)

        # test_db_ro belongs to 'ws_finance'; invoking from 'ws_marketing' must fail
        with pytest.raises(CrossWorkspaceConnectionError) as exc:
            await query_mod.execute(
                context_marketing,
                {"connection_id": "test_db_ro", "query": "SELECT * FROM users"},
                {},
            )
        assert "belonging to workspace 'ws_finance' is denied" in str(exc.value)

    @pytest.mark.asyncio
    async def test_slow_queries_cancelled(
        self,
        sql_setup: tuple[SqlConnectionManager, str],
        context_finance: ExecutionContext,
    ) -> None:
        """Acceptance requirement: Slow queries cancelled."""
        manager, _ = sql_setup
        query_mod = SqlQueryModule(manager=manager)

        # Infinite/slow recursive query
        slow_query = (
            "WITH RECURSIVE inf(x) AS ("
            "VALUES(1) UNION ALL SELECT x + 1 FROM inf"
            ") SELECT count(*) FROM inf"
        )
        with pytest.raises(QueryTimeoutError) as exc:
            await query_mod.execute(
                context_finance,
                {"connection_id": "test_db_ro", "query": slow_query},
                {},
            )
        assert "exceeded statement timeout of 0.4 seconds" in str(exc.value)

    @pytest.mark.asyncio
    async def test_pool_connections_returned_after_failure(
        self,
        sql_setup: tuple[SqlConnectionManager, str],
        context_finance: ExecutionContext,
    ) -> None:
        """Acceptance requirement: Pool connections returned after failure."""
        manager, _ = sql_setup
        query_mod = SqlQueryModule(manager=manager)

        pool = await manager.get_pool("test_db_ro", "ws_finance")
        initial_checked_out = pool.checked_out_count
        assert initial_checked_out == 0

        # 1. Trigger syntax error failure
        with contextlib.suppress(Exception):
            await query_mod.execute(
                context_finance,
                {"connection_id": "test_db_ro", "query": "INVALID SYNTAX SQL !!!"},
                {},
            )

        assert pool.checked_out_count == 0

        # 2. Trigger read-only write failure
        with contextlib.suppress(SqlReadOnlyViolationError):
            await query_mod.execute(
                context_finance,
                {"connection_id": "test_db_ro", "query": "DELETE FROM users"},
                {},
            )

        assert pool.checked_out_count == 0

        # 3. Trigger statement timeout failure
        slow_loop = (
            "WITH RECURSIVE r(i) AS (VALUES(1) UNION ALL SELECT i+1 FROM r) SELECT count(*) FROM r"
        )
        with contextlib.suppress(QueryTimeoutError):
            await query_mod.execute(
                context_finance,
                {
                    "connection_id": "test_db_ro",
                    "query": slow_loop,
                },
                {},
            )

        assert pool.checked_out_count == 0

        # Verify pool is fully functional after repeated errors
        res = await query_mod.execute(
            context_finance,
            {"connection_id": "test_db_ro", "query": "SELECT count(*) AS total FROM users"},
            {},
        )
        assert res.status == "success"
        assert res.outputs["rows"][0]["total"] == 3
        assert pool.checked_out_count == 0


class TestSqlSecurityAndBoundaries:
    """Additional boundary tests for arbitrary DSNs, limits, and schema inspection."""

    @pytest.mark.asyncio
    async def test_arbitrary_dsn_prohibited_in_inputs(
        self,
        sql_setup: tuple[SqlConnectionManager, str],
        context_finance: ExecutionContext,
    ) -> None:
        manager, _ = sql_setup
        query_mod = SqlQueryModule(manager=manager)

        # Input attempting to provide direct DSN
        with pytest.raises(ArbitraryDsnProhibitedError) as exc1:
            await query_mod.execute(
                context_finance,
                {
                    "connection_id": "test_db_ro",
                    "query": "SELECT 1",
                    "dsn": "postgres://evil:pass@remote:5432/db",
                },
                {},
            )
        assert "Arbitrary database connection parameter 'dsn'" in str(exc1.value)

        # Input attempting to provide direct file path
        with pytest.raises(ArbitraryDsnProhibitedError) as exc2:
            await query_mod.execute(
                context_finance,
                {
                    "connection_id": "test_db_ro",
                    "query": "SELECT 1",
                    "database_path": "/etc/shadow",
                },
                {},
            )
        assert "Arbitrary database connection parameter 'database_path'" in str(exc2.value)

    @pytest.mark.asyncio
    async def test_unknown_connection_id_raises_error(
        self,
        sql_setup: tuple[SqlConnectionManager, str],
        context_finance: ExecutionContext,
    ) -> None:
        manager, _ = sql_setup
        query_mod = SqlQueryModule(manager=manager)

        with pytest.raises(ConnectionNotFoundError) as exc:
            await query_mod.execute(
                context_finance,
                {"connection_id": "non_existent_db", "query": "SELECT 1"},
                {},
            )
        assert "SQL connection 'non_existent_db' not found" in str(exc.value)

    @pytest.mark.asyncio
    async def test_schema_inspection_module(
        self,
        sql_setup: tuple[SqlConnectionManager, str],
        context_finance: ExecutionContext,
    ) -> None:
        manager, _ = sql_setup
        schema_mod = SqlSchemaInspectModule(manager=manager)

        res = await schema_mod.execute(
            context_finance,
            {"connection_id": "test_db_ro"},
            {},
        )
        assert res.status == "success"
        tables = res.outputs["tables"]
        table_names = [t["name"] for t in tables]
        assert "users" in table_names
        assert "posts" in table_names

        users_table = next(t for t in tables if t["name"] == "users")
        col_names = [c["name"] for c in users_table["columns"]]
        assert col_names == ["id", "username", "email", "role"]

        posts_table = next(t for t in tables if t["name"] == "posts")
        assert len(posts_table["foreign_keys"]) == 1
        fk = posts_table["foreign_keys"][0]
        assert fk["to_table"] == "users"
        assert fk["from_column"] == "user_id"

    @pytest.mark.asyncio
    async def test_sql_execute_module_mutations(
        self,
        sql_setup: tuple[SqlConnectionManager, str],
        context_finance: ExecutionContext,
    ) -> None:
        manager, _ = sql_setup
        exec_mod = SqlExecuteModule(manager=manager)
        query_mod = SqlQueryModule(manager=manager)

        # 1. Execute write on write connection
        res = await exec_mod.execute(
            context_finance,
            {
                "connection_id": "test_db_rw",
                "statement": "INSERT INTO users (id, username, email, role) VALUES (?, ?, ?, ?)",
                "parameters": [10, "diana", "diana@example.com", "member"],
            },
            {},
        )
        assert res.status == "success"
        assert res.outputs["rows_affected"] == 1

        # 2. Verify write persisted
        query_res = await query_mod.execute(
            context_finance,
            {
                "connection_id": "test_db_rw",
                "query": "SELECT * FROM users WHERE id = 10",
            },
            {},
        )
        assert query_res.outputs["row_count"] == 1
        assert query_res.outputs["rows"][0]["username"] == "diana"

        # 3. Attempt write on read-only connection fails
        with pytest.raises(SqlReadOnlyViolationError) as exc:
            await exec_mod.execute(
                context_finance,
                {
                    "connection_id": "test_db_ro",
                    "statement": "DELETE FROM users WHERE id = 10",
                },
                {},
            )
        assert "is configured as read-only" in str(exc.value)

    @pytest.mark.asyncio
    async def test_byte_limit_exceeded(
        self,
        sql_setup: tuple[SqlConnectionManager, str],
        context_finance: ExecutionContext,
    ) -> None:
        manager, _ = sql_setup
        query_mod = SqlQueryModule(manager=manager)

        with pytest.raises(ByteLimitExceededError) as exc:
            await query_mod.execute(
                context_finance,
                {"connection_id": "test_db_ro", "query": "SELECT * FROM users"},
                {"max_bytes": 10},
            )
        assert "exceeded byte limit of 10 bytes" in str(exc.value)
