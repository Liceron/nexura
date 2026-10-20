"""Postgres and MySQL connector tests (in-memory SqlDriver fake; no database)."""

from __future__ import annotations

import pytest
from connector_helpers import invoke
from nexura_connectors import (
    InMemorySqlDriver,
    MySqlConnector,
    PostgresConnector,
)
from nexura_connectors.sql import AsyncpgDriver

PW = "pg_Password_Distinct_99"
DSN = f"postgresql://app:{PW}@8.8.8.8:5432/db"  # public IP literal: no DNS needed
CONNECTORS = [PostgresConnector, MySqlConnector]


def make(cls: type, driver: InMemorySqlDriver) -> tuple[PostgresConnector, InMemorySqlDriver]:
    async def factory(dsn: str) -> InMemorySqlDriver:
        assert dsn == DSN
        return driver

    return cls(driver_factory=factory), driver


async def run(cls: type, driver: InMemorySqlDriver, op: str, params: dict, **kw):  # type: ignore[no-untyped-def]
    mod, _ = make(cls, driver)
    kw.setdefault("secrets", {"cred": DSN})
    return await invoke(mod, op, params, **kw)


@pytest.mark.parametrize("cls", CONNECTORS)
async def test_manifest_marks_query_read_and_execute_write(cls: type) -> None:
    m = cls().manifest
    ops = {e["operation"]: e["write"] for e in (m.display.examples if m.display else [])}
    assert ops == {"query": False, "execute": True}
    assert m.required_credential_types == ["database_dsn"]


@pytest.mark.parametrize("cls", CONNECTORS)
async def test_query_returns_rows_with_cap_and_closes(cls: type) -> None:
    d = InMemorySqlDriver(columns=["id"], rows=[[1], [2], [3]])
    res = await run(
        cls, d, "query", {"sql": "SELECT id FROM t WHERE x = $1", "args": [1], "max_rows": 2}
    )
    assert res.outputs["data"] == {"columns": ["id"], "rows": [[1], [2]], "row_count": 2}
    assert res.outputs["truncated"] is True and d.closed
    assert d.reads == [("SELECT id FROM t WHERE x = $1", [1])]


@pytest.mark.parametrize("cls", CONNECTORS)
@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM t",
        "UPDATE t SET a=1",
        "SELECT 1; DROP TABLE t",
        "WITH x AS (DELETE FROM t RETURNING *) SELECT * FROM x",
        "SELECT * INTO new_t FROM t",
        "/* SELECT */ DROP TABLE t",
        "COPY t TO '/tmp/x'",
        "",
    ],
)
async def test_query_op_rejects_writes_and_multi_statement(cls: type, sql: str) -> None:
    d = InMemorySqlDriver()
    res = await run(cls, d, "query", {"sql": sql or " "}, approved=True)
    assert res.error_code == "INVALID_PARAMS"
    assert d.reads == [] and d.writes == [] and not d.closed


@pytest.mark.parametrize("cls", CONNECTORS)
async def test_execute_needs_approval_then_runs_once(cls: type) -> None:
    d = InMemorySqlDriver(affected=3)
    for approved in (None, False):
        res = await run(
            cls, d, "execute", {"sql": "UPDATE t SET a=$1", "args": [1]}, approved=approved
        )
        assert res.error_code == "APPROVAL_REQUIRED"
    assert d.writes == []
    res = await run(cls, d, "execute", {"sql": "UPDATE t SET a=$1", "args": [1]}, approved=True)
    assert res.outputs["data"] == {"rows_affected": 3} and res.outputs["write"] is True
    assert d.writes == [("UPDATE t SET a=$1", [1])]
    res = await run(cls, d, "execute", {"sql": "UPDATE t SET a=1; DROP TABLE t"}, approved=True)
    assert res.error_code == "INVALID_PARAMS" and len(d.writes) == 1


@pytest.mark.parametrize("cls", CONNECTORS)
async def test_credential_must_be_reference_and_resolve(cls: type) -> None:
    d = InMemorySqlDriver()
    mod, _ = make(cls, d)
    res = await invoke(mod, "query", {"sql": "SELECT 1"}, secrets={})
    assert res.error_code == "MISSING_CREDENTIAL"
    res = await invoke(
        mod, "query", {"sql": "SELECT 1"}, cfg={"credential_ref": DSN}, secrets={"x": DSN}
    )
    assert res.error_code == "MISSING_CREDENTIAL" and PW not in (res.error_message or "")
    res = await invoke(mod, "query", {"sql": "SELECT 1"}, secrets={"cred": "not-a-url"})
    assert res.error_code == "INVALID_PARAMS" and d.reads == []


@pytest.mark.parametrize("cls", CONNECTORS)
async def test_ssrf_dsn_host_blocked(cls: type) -> None:
    d = InMemorySqlDriver()
    for host in ("169.254.169.254", "127.0.0.1", "10.0.0.5", "192.168.0.2", "[::1]"):
        dsn = f"postgresql://app:{PW}@{host}:5432/db"
        res = await run(cls, d, "query", {"sql": "SELECT 1"}, secrets={"cred": dsn})
        assert res.error_code == "SSRF_BLOCKED", host
        assert PW not in (res.error_message or "")
    assert d.reads == []


@pytest.mark.parametrize("cls", CONNECTORS)
async def test_driver_errors_do_not_leak_dsn_and_are_not_retried(cls: type) -> None:
    d = InMemorySqlDriver(fail_with=RuntimeError(f"could not connect {DSN}"))
    res = await run(cls, d, "query", {"sql": "SELECT 1"})
    assert res.error_code == "UPSTREAM_ERROR" and PW not in f"{res.error_message} {res.metadata}"
    assert len(d.reads) == 1 and d.closed


@pytest.mark.parametrize("cls", CONNECTORS)
async def test_timeouts(cls: type) -> None:
    d = InMemorySqlDriver(delay=1.0)
    res = await run(cls, d, "query", {"sql": "SELECT 1"}, cfg={"timeout_seconds": 0.1})
    assert res.error_code == "TIMEOUT"
    d2 = InMemorySqlDriver(delay=1.0)
    res = await run(
        cls, d2, "execute", {"sql": "DELETE FROM t"}, cfg={"timeout_seconds": 0.1}, approved=True
    )
    assert res.status == "indeterminate" and len(d2.writes) == 1


async def test_row_cap_hard_limit_and_arg_cap() -> None:
    d = InMemorySqlDriver()
    res = await run(PostgresConnector, d, "query", {"sql": "SELECT 1", "max_rows": 10**6})
    assert res.error_code == "INVALID_PARAMS"
    res = await run(PostgresConnector, d, "query", {"sql": "SELECT 1", "args": list(range(101))})
    assert res.error_code == "INVALID_PARAMS" and d.reads == []


async def test_mysql_without_injected_driver_explains_optional_extra() -> None:
    res = await invoke(MySqlConnector(), "query", {"sql": "SELECT 1"}, secrets={"cred": DSN})
    assert res.error_code == "MISSING_DEPENDENCY"
    assert "optional extra" in (res.error_message or "") and PW not in (res.error_message or "")


async def test_postgres_default_factory_is_asyncpg() -> None:
    from nexura_connectors.sql import asyncpg_driver_factory

    assert PostgresConnector().driver_factory is asyncpg_driver_factory


async def test_asyncpg_driver_adapter_with_fake_connection() -> None:
    class Rec(dict):  # asyncpg Record stand-in
        pass

    class Cursor:
        async def fetch(self, n: int) -> list[Rec]:
            return [Rec(a=i) for i in range(n)]

    class Tx:
        async def __aenter__(self) -> None: ...
        async def __aexit__(self, *a: object) -> None: ...

    class Conn:
        readonly_seen = False

        def transaction(self, readonly: bool = False) -> Tx:
            self.readonly_seen = readonly
            return Tx()

        async def cursor(self, sql: str, *args: object, timeout: float) -> Cursor:
            return Cursor()

        async def execute(self, sql: str, *args: object, timeout: float) -> str:
            return "UPDATE 7"

        async def close(self) -> None: ...

    conn = Conn()
    drv = AsyncpgDriver(conn)
    out = await drv.fetch("SELECT 1", [], max_rows=2, timeout=1)
    assert out.rows == [[0], [1]] and out.truncated and conn.readonly_seen
    assert await drv.execute("UPDATE t", [], timeout=1) == 7


# --- review fixes: original SQL reaches the driver; tokenised validation; DSN host rules ---


@pytest.mark.parametrize("cls", CONNECTORS)
@pytest.mark.parametrize(
    "sql",
    [
        "select 'a--b' as v",
        "select 1 /* note */ as v -- trailing",
        'select "we;ird" from t',
        "select 'it''s; fine'",
    ],
)
async def test_query_runs_original_sql_and_literals_are_data(cls: type, sql: str) -> None:
    d = InMemorySqlDriver(columns=["x"], rows=[[1]])
    res = await run(cls, d, "query", {"sql": sql})
    assert res.status == "success"
    assert [s for s, _ in d.reads] == [sql]


@pytest.mark.parametrize("cls", CONNECTORS)
async def test_execute_runs_original_sql_with_literal_comment_markers(cls: type) -> None:
    sql = "update t set note='x /* y */ z -- q' where id = 1"
    d = InMemorySqlDriver()
    res = await run(cls, d, "execute", {"sql": sql}, approved=True)
    assert res.status == "success"
    assert [s for s, _ in d.writes] == [sql]


@pytest.mark.parametrize("cls", CONNECTORS)
@pytest.mark.parametrize(
    "sql",
    [
        "select 1; select 2",
        "select 1 /* ; */; select 2",
        "select 'a\\'; select 2 -- '",  # backslash dialects disagree: refused
        "select 1 /*! ; drop table t */",
        "select 'unterminated",
        "select 1 /* unterminated",
        "select 1 -- c\n; drop table t",
        "select `a`; drop table t",
    ],
)
async def test_query_rejects_ambiguous_or_multiple_statements(cls: type, sql: str) -> None:
    d = InMemorySqlDriver(columns=["x"], rows=[[1]])
    res = await run(cls, d, "query", {"sql": sql})
    assert res.status != "success"
    assert not d.reads


@pytest.mark.parametrize("cls", CONNECTORS)
@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://u:p_Distinct_Pw9@8.8.8.8:5432,127.0.0.1:5432/db",
        "postgresql://u:p_Distinct_Pw9@8.8.8.8/db?host=127.0.0.1",
        "postgresql://u:p_Distinct_Pw9@8.8.8.8/db?HOSTADDR=127.0.0.1",
        "postgresql://u:p_Distinct_Pw9@%2Fvar%2Frun%2Fpostgresql/db",
        "postgresql://u:p_Distinct_Pw9@8.8.8.8/db?host=/var/run/postgresql",
        "mysql://u:p_Distinct_Pw9@8.8.8.8/db?unix_socket=/tmp/mysql.sock",
        "mysql://u:p_Distinct_Pw9@8.8.8.8:3306,10.0.0.1:3306/db",
    ],
)
async def test_dsn_multihost_socket_and_override_refused(cls: type, dsn: str) -> None:
    opened: list[str] = []

    async def factory(s: str) -> InMemorySqlDriver:
        opened.append(s)
        return InMemorySqlDriver()

    res = await invoke(
        cls(driver_factory=factory), "query", {"sql": "select 1"}, secrets={"cred": dsn}
    )
    assert res.status != "success"
    assert not opened


@pytest.mark.parametrize("cls", CONNECTORS)
async def test_dsn_host_to_cgnat_metadata_refused(cls: type) -> None:
    dsn = "postgresql://u:p_Distinct_Pw9@100.100.100.200/db"
    opened: list[str] = []

    async def factory(s: str) -> InMemorySqlDriver:
        opened.append(s)
        return InMemorySqlDriver()

    res = await invoke(
        cls(driver_factory=factory), "query", {"sql": "select 1"}, secrets={"cred": dsn}
    )
    assert res.status != "success"
    assert not opened


async def test_dollar_quoting_is_data_on_postgres_only() -> None:
    sql = "select $q$ ; drop table t $q$"
    d = InMemorySqlDriver(columns=["x"], rows=[[1]])
    assert (await run(PostgresConnector, d, "query", {"sql": sql})).status == "success"
    assert [s for s, _ in d.reads] == [sql]
    d2 = InMemorySqlDriver(columns=["x"], rows=[[1]])
    assert (await run(MySqlConnector, d2, "query", {"sql": sql})).status != "success"
    assert not d2.reads


async def test_postgres_unterminated_dollar_quote_refused() -> None:
    d = InMemorySqlDriver(columns=["x"], rows=[[1]])
    res = await run(PostgresConnector, d, "query", {"sql": "select $x$ unterminated"})
    assert res.status != "success"
    assert not d.reads


# --- read classification: function allow-list and READ ONLY transaction ---


@pytest.mark.parametrize("cls", CONNECTORS)
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT dblink_exec('host=db','DROP TABLE users')",
        "SELECT purge_old_rows()",
        "select PG_NOTIFY('c','x')",
        "select nextval('s')",
        "select public.purge_old_rows()",
        'select "purge_old_rows"()',
        'select public . "f" ()',
        "select 1 from t where x in (select sleep(5))",
        "select pg_sleep (1)",
        "select get_lock('a', 1)",
        "select load_file('/etc/passwd')",
        "explain analyze select my_fn(1)",
        "with x as (select my_fn(1)) select * from x",
        "select count(*) filter (where my_fn(a)) from t",
        "select 1 where exists (select query_to_xml('delete from t', true, true, ''))",
    ],
)
async def test_query_refuses_unlisted_function_calls(cls: type, sql: str) -> None:
    d = InMemorySqlDriver(columns=["x"], rows=[[1]])
    res = await run(cls, d, "query", {"sql": sql})
    assert res.error_code == "INVALID_PARAMS"
    assert "execute" in res.error_message and "allow-list" in res.error_message
    assert d.reads == [] and d.writes == [] and not d.closed


@pytest.mark.parametrize("cls", CONNECTORS)
@pytest.mark.parametrize(
    "sql",
    [
        "select count(*), max(a), coalesce(b, 0), lower(c) from t where id in (1, 2)",
        "select cast(a as numeric(10, 2)), a::varchar(5) from t where exists (select 1)",
        "select * from (values (1), (2)) as v(a) where a = any(array[1, 2])",
        "with x(a) as (select 1) select * from x",
        "select row_number() over (partition by a order by b) from t",
        "select 'my_fn(1)' as s, \"my_fn\" from t -- other_fn(2)\n",
        "select now(), date_trunc('day', ts), extract(year from ts) from t",
        "select json_agg(t) from t, generate_series(1, 3) g",
        "select t.a from t join u on (t.id = u.id) where (t.a > 1)",
    ],
)
async def test_query_allows_listed_functions_and_syntax(cls: type, sql: str) -> None:
    d = InMemorySqlDriver(columns=["x"], rows=[[1]])
    res = await run(cls, d, "query", {"sql": sql})
    assert res.status == "success", res.error_message
    assert [s for s, _ in d.reads] == [sql]


async def test_postgres_pg_catalog_qualified_allowed_but_other_schema_refused() -> None:
    d = InMemorySqlDriver(columns=["x"], rows=[[1]])
    ok = await run(PostgresConnector, d, "query", {"sql": "select pg_catalog.lower('A')"})
    assert ok.status == "success"
    bad = await run(PostgresConnector, d, "query", {"sql": "select myschema.lower('A')"})
    assert bad.error_code == "INVALID_PARAMS"


async def test_dialect_specific_allow_list() -> None:
    d = InMemorySqlDriver(columns=["x"], rows=[[1]])
    assert (
        await run(PostgresConnector, d, "query", {"sql": "select gen_random_uuid()"})
    ).status == "success"
    assert (
        await run(MySqlConnector, d, "query", {"sql": "select gen_random_uuid()"})
    ).error_code == "INVALID_PARAMS"
    assert (
        await run(MySqlConnector, d, "query", {"sql": "select unix_timestamp()"})
    ).status == "success"
    assert (
        await run(PostgresConnector, d, "query", {"sql": "select unix_timestamp()"})
    ).error_code == "INVALID_PARAMS"


@pytest.mark.parametrize("cls", CONNECTORS)
async def test_execute_may_call_any_function_with_approval(cls: type) -> None:
    d = InMemorySqlDriver()
    sql = "select purge_old_rows()"
    res = await run(cls, d, "execute", {"sql": sql})
    assert res.error_code == "APPROVAL_REQUIRED" and d.writes == []
    res = await run(cls, d, "execute", {"sql": sql}, approved=True)
    assert res.status == "success" and d.writes == [(sql, [])] and d.reads == []


async def test_query_requests_read_only_transaction_per_dialect() -> None:
    pg, my = InMemorySqlDriver(), InMemorySqlDriver()
    await run(PostgresConnector, pg, "query", {"sql": "select 1"})
    await run(MySqlConnector, my, "query", {"sql": "select 1"})
    assert pg.read_only_begins == ["BEGIN READ ONLY"]
    assert my.read_only_begins == ["START TRANSACTION READ ONLY"]


async def test_mysql_backtick_quoted_function_refused() -> None:
    d = InMemorySqlDriver()
    res = await run(MySqlConnector, d, "query", {"sql": "select `f`()"})
    assert res.error_code == "INVALID_PARAMS" and d.reads == []


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT count(*) FILTER (WHERE x > 1) FROM t",
        "SELECT sum(x) OVER (PARTITION BY a ORDER BY b) FROM t",
        "SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY x) FROM t",
        "SELECT a FROM t WHERE s SIMILAR TO ('a') ESCAPE ('x')",
    ],
)
def test_positional_keywords_are_allowed(sql: str) -> None:
    from nexura_connectors.sql import assert_single_read_statement

    assert_single_read_statement(sql, "postgres")


@pytest.mark.parametrize("sql", ["SELECT filter(1)", "SELECT 1, over(2)", "SELECT within(1)"])
def test_keyword_named_call_in_other_position_is_refused(sql: str) -> None:
    from nexura_connectors.sql import assert_single_read_statement

    with pytest.raises(Exception, match="allow-list"):
        assert_single_read_statement(sql, "postgres")


def test_mysql_truncate_table_stays_a_write() -> None:
    from nexura_connectors.sql import assert_single_read_statement

    with pytest.raises(Exception, match="not read-only"):
        assert_single_read_statement("SELECT 1 FROM t WHERE TRUNCATE TABLE t", "mysql")
    with pytest.raises(Exception):  # noqa: B017
        assert_single_read_statement("WITH x AS (SELECT 1) TRUNCATE TABLE t", "mysql")
