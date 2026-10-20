# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Postgres and MySQL connectors over a small driver protocol.

asyncpg is installed (it is in uv.lock), so Postgres has a real driver factory. No MySQL driver
is in uv.lock: the MySQL connector runs against any object implementing :class:`SqlDriver`
(an in-memory fake ships for tests) and the real driver is an optional extra the operator
installs and injects through ``driver_factory``. The DSN is a credential by reference.
"""

from __future__ import annotations

import asyncio
import inspect
import itertools
import os
import re
import ssl
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Protocol
from urllib.parse import parse_qsl, unquote, urlsplit

from pydantic import Field

from nexura_connectors._core import (
    Call,
    ConnectorModule,
    Op,
    PinnedHostnameSSLContext,
    StrictParams,
    resolve_validated_address,
    result_data,
)
from nexura_connectors.errors import (
    ConnectorDependencyError,
    ConnectorError,
    ConnectorTimeoutError,
    IndeterminateEffectError,
    InvalidConfigError,
    InvalidParamsError,
    UpstreamError,
)

DEFAULT_MAX_ROWS = 500
HARD_MAX_ROWS = 5000
_WRITE_WORDS = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate(?!\s*\()|grant|revoke|merge|copy|into|"
    r"lock|vacuum|reindex)\b",
    re.IGNORECASE,
)


def _words(text: str) -> frozenset[str]:
    return frozenset(text.split())


_COMMON_FUNCTIONS = _words(
    """
    abs ceil ceiling floor round trunc truncate sign sqrt cbrt power pow exp ln log log10 mod div
    pi random greatest least coalesce nullif cast convert
    count sum avg min max stddev stddev_pop stddev_samp variance var_pop var_samp bool_and bool_or
    every array_agg string_agg json_agg jsonb_agg json_object_agg jsonb_object_agg group_concat
    row_number rank dense_rank percent_rank cume_dist ntile lag lead first_value last_value
    nth_value percentile_cont percentile_disc mode
    lower upper length char_length character_length octet_length bit_length concat concat_ws
    substr substring left right trim ltrim rtrim btrim lpad rpad replace repeat reverse position
    strpos initcap overlay translate split_part format ascii chr md5 starts_with
    regexp_replace regexp_match regexp_matches regexp_split_to_array regexp_like
    to_char to_number to_date to_timestamp date_trunc date_part extract age
    now current_date current_time current_timestamp localtime localtimestamp make_date make_time
    make_interval date_format datediff dateadd date_add date_sub timestampdiff curdate curtime
    year month day hour minute second dayofweek dayofmonth dayofyear weekday
    json_extract json_unquote json_array json_object json_length json_valid json_keys json_type
    json_build_object jsonb_build_object json_build_array jsonb_build_array json_typeof
    jsonb_typeof json_array_length jsonb_array_length json_extract_path jsonb_extract_path
    json_extract_path_text jsonb_extract_path_text jsonb_pretty to_json to_jsonb
    array_length array_to_string string_to_array array_position array_cat array_append
    array_prepend cardinality unnest generate_series
    ifnull isnull if
    int integer bigint smallint numeric decimal real double float char varchar text date time
    timestamp timestamptz interval boolean bool bytea json jsonb uuid signed unsigned binary
    datetime
    """
)
#: Functions known to be free of side effects, per dialect. User-defined functions can never be
#: proven pure, so they are not here and a query that calls one is refused (use ``execute``).
READ_FUNCTION_ALLOWLIST: dict[str, frozenset[str]] = {
    "generic": _COMMON_FUNCTIONS,
    "postgres": _COMMON_FUNCTIONS
    | _words(
        "pg_typeof version current_schema current_database current_user session_user "
        "pg_get_viewdef pg_table_size pg_relation_size pg_size_pretty gen_random_uuid "
        "jsonb_array_elements jsonb_each jsonb_object_keys json_array_elements json_each "
        "json_object_keys jsonb_path_exists jsonb_path_query jsonb_set jsonb_strip_nulls "
        "to_tsvector to_tsquery plainto_tsquery ts_rank ts_headline array_remove array_replace "
        "array_dims array_upper array_lower bit_and bit_or bit_xor "
        "jsonb_array_elements_text json_array_elements_text jsonb_each_text json_each_text "
        "row_to_json array_to_json jsonb_insert jsonb_path_query_first jsonb_path_query_array "
        "jsonb_path_match jsonb_populate_record json_populate_record jsonb_populate_recordset "
        "json_populate_recordset jsonb_to_record json_to_record jsonb_to_recordset "
        "json_to_recordset jsonb_object jsonb_exists jsonb_typeof json_strip_nulls "
        "width_bucket sin cos tan asin acos atan atan2 sinh cosh tanh degrees radians cot "
        "gcd lcm scale trim_scale min_scale factorial sqrt "
        "corr covar_pop covar_samp regr_slope regr_intercept regr_count regr_r2 regr_avgx "
        "regr_avgy regr_sxx regr_syy regr_sxy "
        "make_timestamp make_timestamptz date_bin timezone clock_timestamp statement_timestamp "
        "transaction_timestamp timeofday isfinite justify_days justify_hours justify_interval "
        "encode decode sha224 sha256 sha384 sha512 to_hex quote_literal quote_ident "
        "quote_nullable convert_from convert_to "
        "regexp_count regexp_substr regexp_instr "
        "int4range int8range numrange tsrange tstzrange daterange isempty lower_inc upper_inc "
        "lower_inf upper_inf range_merge "
        "bit_length array_fill array_ndims array_positions string_to_table"
    ),
    "mysql": _COMMON_FUNCTIONS
    | _words(
        "database schema user current_user session_user version found_rows row_count "
        "uuid uuid_short sha1 sha2 hex unhex from_unixtime unix_timestamp str_to_date "
        "date_format time_format timediff addtime subtime last_day dayname monthname "
        "quarter week yearweek json_contains json_search json_table find_in_set field elt "
        "locate instr lcase ucase mid space strcmp soundex format_bytes "
        "substring_index json_arrayagg json_objectagg adddate subdate to_days from_days "
        "time_to_sec sec_to_time timestampadd period_diff period_add utc_timestamp utc_date "
        "utc_time sysdate bit_and bit_or bit_xor any_value json_merge_patch "
        "json_merge_preserve json_overlaps "
        "json_contains_path json_depth json_insert json_set json_replace json_remove json_quote "
        "json_pretty json_value json_storage_size sin cos tan asin acos atan atan2 degrees "
        "radians cot crc32 conv bin oct makedate maketime get_format convert_tz to_seconds "
        "date timestamp time dayofweek microsecond extract_value regexp_instr regexp_substr "
        "bit_count char_length ord reverse export_set make_set weight_string"
    ),
}
#: Reserved words that can never name a function, so "(" after them is plain grammar.
_NON_FUNCTION_WORDS = _words(
    """
    in exists values any all some not and or on using select from where union intersect except
    limit offset having when then else case end between distinct table array row window order
    fetch as with recursive lateral left right
    """
)
#: Keywords that are legal unquoted function names in some dialect, so "(" after them is grammar
#: only at the positions checked by :func:`_keyword_is_grammar`.
_POSITIONAL_WORDS = _words("filter over group by to is escape like ilike join")
_JOIN_MODIFIERS = _words("inner left right full cross natural outer")
#: Words after which an operand cannot end (an operator or clause keyword is expected next).
_OPERATOR_WORDS = _NON_FUNCTION_WORDS | _words(
    "by group partition filter over within to is escape like ilike join similar overlaps "
    "inner outer cross natural full"
)
_CALL_NAME = r"(?:[^\W\d][\w$]*|\"\"|``)"
_CALL_RE = re.compile(rf"({_CALL_NAME}(?:\s*\.\s*{_CALL_NAME})*)\s*\(")
_READ_START = re.compile(r"^\s*\(?\s*(select|with|show|explain|values|table)\b", re.IGNORECASE)


@dataclass
class SqlRows:
    """Rows returned by a driver; ``truncated`` is set when more than max_rows existed."""

    columns: list[str]
    rows: list[list[Any]]
    truncated: bool = False


class SqlDriver(Protocol):
    """Minimal async database driver interface that the connector depends on."""

    async def fetch(
        self,
        sql: str,
        args: list[Any],
        *,
        max_rows: int,
        timeout: float,
        read_only_sql: str | None = None,
    ) -> SqlRows:
        """Run a read-only statement inside a read-only transaction.

        ``read_only_sql`` is the statement that opens that transaction for the dialect (for
        example ``START TRANSACTION READ ONLY`` on MySQL); a driver that cannot open one itself
        must issue it before the query.
        """
        ...

    async def execute(self, sql: str, args: list[Any], *, timeout: float) -> int:
        """Run a write statement and return the affected row count."""
        ...

    async def close(self) -> None:
        """Release the connection."""
        ...


SqlDriverFactory = Callable[..., Awaitable[SqlDriver]]
"""Async ``factory(dsn, *, pinned_ip=..., server_name=...)`` returning a connected SqlDriver.

The connector resolves the DSN host once and validates every address. A factory that accepts the
keyword arguments ``pinned_ip`` (validated address) and ``server_name`` (original host name) is
called with them and MUST connect to ``pinned_ip`` (not re-resolve the name) while verifying TLS
against ``server_name``. A legacy ``factory(dsn)`` is still called with the DSN only and then
resolves the host again itself (documented residual time-of-check gap).
"""


_DOLLAR_TAG = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$")
_DSN_HOST_KEYS = {"host", "hostaddr", "hosts", "server", "unix_socket", "socket", "service"}


def _is_e_string(sql: str, quote_at: int) -> bool:
    """True when the quote at ``quote_at`` opens a Postgres E'...' escape string."""
    if quote_at < 1 or sql[quote_at - 1] not in "eE":
        return False
    return quote_at < 2 or not (sql[quote_at - 2].isalnum() or sql[quote_at - 2] in "_$")


def _mask(
    sql: str, *, backslash: bool, nested: bool, backtick: bool, dollar: bool, hash_comment: bool
) -> str:
    """Return a copy of ``sql`` with literals emptied and comments blanked, under one dialect guess.

    String literals, quoted identifiers and dollar-quoted bodies are removed (their delimiters
    stay) so their contents are data, not syntax. Raises InvalidParamsError when a literal or
    comment is unterminated or a MySQL executable comment is present.
    """
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if c == "-" and nxt == "-" or (c == "#" and hash_comment):
            end = sql.find("\n", i)
            i = n if end < 0 else end
            out.append(" ")
        elif c == "/" and nxt == "*":
            if sql.startswith(("/*!", "/*M!"), i):
                raise InvalidParamsError("executable SQL comments are not allowed")
            depth, i = 1, i + 2
            while depth:
                if i >= n:
                    raise InvalidParamsError("unterminated SQL comment")
                if nested and sql.startswith("/*", i):
                    depth, i = depth + 1, i + 2
                elif sql.startswith("*/", i):
                    depth, i = depth - 1, i + 2
                else:
                    i += 1
            out.append(" ")
        elif c in ("'", '"') or (c == "`" and backtick):
            escapes = (backslash and c != '"') or (c == "'" and _is_e_string(sql, i))
            i += 1
            while True:
                if i >= n:
                    raise InvalidParamsError("unterminated SQL literal or identifier")
                if escapes and sql[i] == "\\":
                    i += 2
                elif sql[i] == c:
                    if sql.startswith(c, i + 1):
                        i += 2
                        continue
                    i += 1
                    break
                else:
                    i += 1
            out.append(c + c)
        elif (
            c == "$"
            and dollar
            and (not out or not (out[-1][-1:].isalnum() or out[-1][-1:] in "_$"))
        ):
            m = _DOLLAR_TAG.match(sql, i)
            if not m:
                out.append(c)
                i += 1
                continue
            end = sql.find(m.group(0), m.end())
            if end < 0:
                raise InvalidParamsError("unterminated dollar-quoted string")
            i = end + len(m.group(0))
            out.append("''")
        else:
            out.append(c)
            i += 1
    return "".join(out)


_BOOLS = (False, True)
#: Lexer guesses per dialect as (backslash, nested, backtick, dollar, hash) choices.
_DIALECTS: dict[str, tuple[tuple[bool, ...], ...]] = {
    "generic": (_BOOLS, _BOOLS, _BOOLS, _BOOLS, _BOOLS),
    "postgres": (_BOOLS, (True,), (False,), (True,), (False,)),
    "mysql": (_BOOLS, (False,), (True,), (False,), (True,)),
}


def _masked_variants(sql: str, dialect: str = "generic") -> list[str]:
    """Mask ``sql`` under every lexer guess of ``dialect`` so differing parsers cannot disagree."""
    return [
        _mask(sql, backslash=b, nested=ne, backtick=bt, dollar=d, hash_comment=h)
        for b, ne, bt, d, h in itertools.product(*_DIALECTS[dialect])
    ]


def _single_statement(masked: str) -> str:
    """Return the masked text without trailing semicolons or reject extra statements."""
    body = masked.strip().rstrip(";").strip()
    if not body or ";" in body:
        raise InvalidParamsError("exactly one SQL statement is allowed")
    return body


def _prev_word(body: str, end: int) -> str:
    """Return the lower-cased word that ends at ``end`` (ignoring whitespace), or ''."""
    m = re.search(r"([\w$]+)\s*$", body[:end])
    return m.group(1).lower() if m else ""


def _ends_operand(body: str, end: int) -> bool:
    """True when the text before ``end`` ends an operand (value, name, literal or group)."""
    before = body[:end].rstrip()
    if not before:
        return False
    if before[-1] in ")'\"`":
        return True
    word = _prev_word(body, end)
    return bool(word) and word not in _OPERATOR_WORDS


def _closes_call(body: str, end: int) -> bool:
    """True when the text before ``end`` ends ``name(...)`` (a call or FILTER/WITHIN GROUP)."""
    before = body[:end].rstrip()
    if not before.endswith(")"):
        return False
    depth = 0
    for i in range(len(before) - 1, -1, -1):
        if before[i] == ")":
            depth += 1
        elif before[i] == "(":
            depth -= 1
            if depth == 0:
                name = _prev_word(body, i)
                return bool(name) and (name not in _OPERATOR_WORDS or name in ("filter", "group"))
    return False


def _keyword_is_grammar(word: str, body: str, start: int) -> bool:
    """True when ``word`` at ``start`` followed by "(" sits where the grammar makes it a keyword.

    FILTER and OVER must follow a call's closing parenthesis, WITHIN GROUP needs WITHIN, BY needs
    PARTITION/ORDER/GROUP, and the infix words (IS, ESCAPE, LIKE, JOIN) need an operand before
    them. Anywhere else the word is a function name and goes through the allow-list.
    """
    if word in ("filter", "over"):
        return _closes_call(body, start)
    prev = _prev_word(body, start)
    if word == "group":
        return prev == "within"
    if word == "to":
        return prev == "similar"
    if word == "by":
        return prev in ("partition", "order", "group")
    if word == "join":
        return prev in _JOIN_MODIFIERS or _ends_operand(body, start)
    return _ends_operand(body, start) or (word in ("like", "ilike") and prev == "not")


def _disallowed_function(body: str, dialect: str) -> str | None:
    """Return the first called function of masked ``body`` that is not on the dialect allow-list."""
    allowed = READ_FUNCTION_ALLOWLIST.get(dialect, READ_FUNCTION_ALLOWLIST["generic"])
    for m in _CALL_RE.finditer(body):
        name = re.sub(r"\s+", "", m.group(1)).lower()
        if name in _NON_FUNCTION_WORDS or (
            name in _POSITIONAL_WORDS and _keyword_is_grammar(name, body, m.start())
        ):
            continue
        previous = re.search(r"(\w+)\s*$", body[: m.start()])
        prev = previous.group(1).lower() if previous else ""
        if "." not in name and prev in ("as", "with", "recursive"):
            continue  # derived-table alias or CTE column list: a name, not a call
        if dialect == "postgres" and name.startswith("pg_catalog."):
            name = name[len("pg_catalog.") :]
        if name not in allowed:
            return "quoted identifier" if name.strip('`"') == "" else name
    return None


def assert_single_read_statement(sql: str, dialect: str = "generic") -> str:
    """Reject anything but one read-only statement; returns the original, unaltered SQL.

    Besides the statement-kind check, every function call must be on the dialect's allow-list
    (``READ_FUNCTION_ALLOWLIST``): a SELECT can still run ``dblink_exec`` or a user-defined
    function that writes, so such a call makes the operation a write.
    """
    for masked in _masked_variants(sql, dialect):
        body = _single_statement(masked)
        if not _READ_START.match(body) or _WRITE_WORDS.search(body):
            raise InvalidParamsError("query is not read-only; use the 'execute' operation")
        bad = _disallowed_function(body, dialect)
        if bad is not None:
            raise InvalidParamsError(
                f"query calls function {bad!r}, which is not on the read-only allow-list and may "
                "have side effects; use the 'execute' operation (it requires approval)"
            )
    return sql


def assert_single_statement(sql: str, dialect: str = "generic") -> str:
    """Reject multi-statement SQL for writes; returns the original, unaltered SQL."""
    for masked in _masked_variants(sql, dialect):
        _single_statement(masked)
    return sql


def dsn_host(dsn: str) -> str:
    """Return the single TCP host of a database URL; refuse multi-host and socket forms."""
    try:
        parts = urlsplit(dsn)
        port_ok = parts.port is None or parts.port > 0
    except ValueError:
        raise InvalidParamsError("credential is not a database URL") from None
    hostinfo = parts.netloc.rpartition("@")[2]
    if "," in hostinfo or "," in (parts.hostname or ""):
        raise InvalidParamsError("multi-host database URLs are not allowed")
    keys = {k.lower() for k, _ in parse_qsl(parts.query, keep_blank_values=True)}
    if keys & _DSN_HOST_KEYS:
        raise InvalidParamsError("host overrides in the database URL query are not allowed")
    host = parts.hostname
    if not host or not port_ok:
        raise InvalidParamsError("credential is not a database URL")
    if "/" in unquote(host) or "\\" in unquote(host) or unquote(host) != host:
        raise InvalidParamsError("unix-socket database hosts are not allowed")
    return host


class Query(StrictParams):
    """Parameters for a read-only query."""

    sql: str = Field(min_length=1, max_length=20000)
    args: list[Any] = Field(default_factory=list, max_length=100)
    max_rows: int = Field(default=DEFAULT_MAX_ROWS, ge=1, le=HARD_MAX_ROWS)


class Execute(StrictParams):
    """Parameters for a single write statement."""

    sql: str = Field(min_length=1, max_length=20000)
    args: list[Any] = Field(default_factory=list, max_length=100)


class AsyncpgDriver:
    """Real Postgres driver on asyncpg; read queries run in a read-only transaction."""

    def __init__(self, connection: Any) -> None:
        self._conn = connection

    async def fetch(
        self,
        sql: str,
        args: list[Any],
        *,
        max_rows: int,
        timeout: float,
        read_only_sql: str | None = None,
    ) -> SqlRows:
        """Fetch at most max_rows+1 rows through a server-side cursor in a READ ONLY transaction."""
        async with self._conn.transaction(readonly=True):
            cursor = await self._conn.cursor(sql, *args, timeout=timeout)
            records = await cursor.fetch(max_rows + 1)
        columns = list(records[0].keys()) if records else []
        rows = [list(r.values()) for r in records[:max_rows]]
        return SqlRows(columns, rows, truncated=len(records) > max_rows)

    async def execute(self, sql: str, args: list[Any], *, timeout: float) -> int:
        """Execute a write and parse the command tag."""
        tag: str = await self._conn.execute(sql, *args, timeout=timeout)
        tail = tag.rsplit(" ", 1)[-1]
        return int(tail) if tail.isdigit() else 0

    async def close(self) -> None:
        """Close the connection."""
        await self._conn.close()


def _pg_file(query: dict[str, str], key: str, env: str, default: str) -> str | None:
    """A libpq TLS file from the DSN, then the environment, then ``~/.postgresql/<default>``."""
    value = query.get(key) or os.environ.get(env)
    if value:
        return value
    try:
        path = Path.home() / ".postgresql" / default
    except RuntimeError:  # no home directory
        return None
    return str(path) if path.is_file() else None


def _asyncpg_ssl(dsn: str, server_name: str) -> ssl.SSLContext | None:
    """Build a verifying context bound to ``server_name`` when the DSN asks for verification.

    It honours the same inputs as asyncpg's own context for ``verify-ca`` and ``verify-full``:
    ``sslrootcert`` (required, as in asyncpg and libpq), ``sslcrl``, ``sslcert``, ``sslkey`` and
    ``sslpassword`` from the DSN, then ``PGSSLROOTCERT``, ``PGSSLCRL``, ``PGSSLCERT``,
    ``PGSSLKEY``, then ``~/.postgresql/``. A file that cannot be read fails the connection.
    """
    query = {k.lower(): v for k, v in parse_qsl(urlsplit(dsn).query, keep_blank_values=True)}
    mode = query.get("sslmode", "").lower().replace("_", "-")
    if mode not in {"verify-ca", "verify-full"}:
        return None
    context = PinnedHostnameSSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.server_name = server_name
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.verify_mode = ssl.CERT_REQUIRED
    context.check_hostname = mode == "verify-full"
    root = _pg_file(query, "sslrootcert", "PGSSLROOTCERT", "root.crt")
    if not root:
        raise InvalidConfigError(
            f"sslmode={mode} needs a root certificate: set sslrootcert in the DSN, "
            "PGSSLROOTCERT, or ~/.postgresql/root.crt"
        )
    context.load_verify_locations(cafile=root)
    crl = _pg_file(query, "sslcrl", "PGSSLCRL", "root.crl")
    if crl:
        context.load_verify_locations(cafile=crl)
        context.verify_flags |= ssl.VERIFY_CRL_CHECK_CHAIN
    cert = _pg_file(query, "sslcert", "PGSSLCERT", "postgresql.crt")
    if cert:
        key = _pg_file(query, "sslkey", "PGSSLKEY", "postgresql.key")
        password = query.get("sslpassword", "")
        context.load_cert_chain(cert, keyfile=key, password=lambda: password)
    return context


async def asyncpg_driver_factory(
    dsn: str, *, pinned_ip: str | None = None, server_name: str | None = None
) -> SqlDriver:
    """Open a real asyncpg connection to ``pinned_ip`` (TLS verified against ``server_name``)."""
    try:
        import asyncpg  # type: ignore[import-untyped]
    except ImportError:  # pragma: no cover - asyncpg is in uv.lock
        raise ConnectorDependencyError("asyncpg is not installed") from None
    if pinned_ip is None:
        connection = await asyncpg.connect(dsn, timeout=10)
    else:
        # explicit host overrides the DSN host; asyncpg then never resolves the name again
        extra: dict[str, Any] = {}
        context = _asyncpg_ssl(dsn, server_name or "") if server_name else None
        if context is not None:
            extra["ssl"] = context
        connection = await asyncpg.connect(dsn, host=pinned_ip, timeout=10, **extra)
    return AsyncpgDriver(connection)


async def missing_mysql_driver(dsn: str, **_: Any) -> SqlDriver:
    """Default MySQL factory: no driver is in uv.lock, so ask for an injected one."""
    raise ConnectorDependencyError(
        "no MySQL driver is installed; install an async MySQL driver (optional extra) and pass "
        "driver_factory=..., or use the in-memory fake for tests"
    )


@dataclass
class InMemorySqlDriver:
    """Test fake implementing SqlDriver: canned rows, records every statement."""

    columns: list[str] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)
    affected: int = 1
    delay: float = 0.0
    fail_with: Exception | None = None
    reads: list[tuple[str, list[Any]]] = field(default_factory=list)
    writes: list[tuple[str, list[Any]]] = field(default_factory=list)
    closed: bool = False
    read_only_begins: list[str | None] = field(default_factory=list)

    async def fetch(
        self,
        sql: str,
        args: list[Any],
        *,
        max_rows: int,
        timeout: float,
        read_only_sql: str | None = None,
    ) -> SqlRows:
        """Return canned rows, honouring max_rows and the timeout; records the read-only begin."""
        self.read_only_begins.append(read_only_sql)
        self.reads.append((sql, args))
        await self._maybe_wait(timeout)
        return SqlRows(self.columns, self.rows[:max_rows], truncated=len(self.rows) > max_rows)

    async def execute(self, sql: str, args: list[Any], *, timeout: float) -> int:
        """Record the write and return the canned affected count."""
        self.writes.append((sql, args))
        await self._maybe_wait(timeout)
        return self.affected

    async def _maybe_wait(self, timeout: float) -> None:
        if self.fail_with is not None:
            raise self.fail_with
        if self.delay:
            await asyncio.wait_for(asyncio.sleep(self.delay), timeout)

    async def close(self) -> None:
        """Mark the fake closed."""
        self.closed = True


def _accepts_pinning(factory: SqlDriverFactory) -> bool:
    """True when ``factory`` takes the ``pinned_ip`` and ``server_name`` keyword arguments."""
    try:
        params = inspect.signature(factory).parameters
    except (TypeError, ValueError):
        return False
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return True
    return "pinned_ip" in params and "server_name" in params


class _SqlConnector(ConnectorModule):
    """Shared SQL connector logic; the DSN is the credential."""

    CREDENTIAL_TYPE: ClassVar[str] = "database_dsn"
    DIALECT: ClassVar[str] = "generic"
    DEFAULT_PORT: ClassVar[int] = 5432
    READ_ONLY_SQL: ClassVar[str] = "BEGIN READ ONLY"
    OPS: ClassVar[dict[str, Op]] = {
        "query": Op("query", False, Query),
        "execute": Op("execute", True, Execute),
    }

    def __init__(self, driver_factory: SqlDriverFactory | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.driver_factory = driver_factory or self.default_factory()

    def default_factory(self) -> SqlDriverFactory:
        """Driver factory used when none is injected."""
        raise NotImplementedError

    async def run(self, op: Op, params: Any, call: Call) -> dict[str, Any]:
        """Validate SQL and destination, then run through the driver."""
        sql = (
            assert_single_read_statement(params.sql, self.DIALECT)
            if not op.write
            else assert_single_statement(params.sql, self.DIALECT)
        )
        host = dsn_host(call.secret)
        # the DSN host goes through the same outbound policy as HTTP URLs
        # resolve once, validate every address, then hand the validated IP to the driver
        port = urlsplit(call.secret).port or self.DEFAULT_PORT
        ip = await asyncio.to_thread(resolve_validated_address, host, port, call.http.allowed_hosts)
        timeout = call.http.timeout
        if _accepts_pinning(self.driver_factory):
            driver = await self.driver_factory(call.secret, pinned_ip=str(ip), server_name=host)
        else:
            driver = await self.driver_factory(call.secret)
        try:
            if op.write:
                try:
                    count = await driver.execute(sql, list(params.args), timeout=timeout)
                except TimeoutError:
                    raise IndeterminateEffectError("write timed out; outcome unknown") from None
                return result_data({"rows_affected": count})
            max_rows = min(params.max_rows, HARD_MAX_ROWS)
            try:
                out = await driver.fetch(
                    sql,
                    list(params.args),
                    max_rows=max_rows,
                    timeout=timeout,
                    read_only_sql=self.READ_ONLY_SQL,
                )
            except TimeoutError:
                raise ConnectorTimeoutError("query timed out") from None
            return result_data(
                {"columns": out.columns, "rows": out.rows, "row_count": len(out.rows)},
                truncated=out.truncated,
            )
        except ConnectorError:
            raise
        except Exception as exc:
            raise UpstreamError(f"database error ({type(exc).__name__})") from None
        finally:
            await driver.close()


class PostgresConnector(_SqlConnector):
    """Read-only queries and approved single-statement writes on Postgres."""

    MODULE_ID: ClassVar[str] = "nexura.connectors.postgres"
    DIALECT: ClassVar[str] = "postgres"
    TITLE: ClassVar[str] = "Postgres"
    DESCRIPTION: ClassVar[str] = (
        "Postgres via asyncpg. Credential is the DSN. Queries run read-only with a row cap."
    )

    def default_factory(self) -> SqlDriverFactory:
        """The real asyncpg factory."""
        return asyncpg_driver_factory


class MySqlConnector(_SqlConnector):
    """Read-only queries and approved single-statement writes on MySQL."""

    MODULE_ID: ClassVar[str] = "nexura.connectors.mysql"
    DIALECT: ClassVar[str] = "mysql"
    DEFAULT_PORT: ClassVar[int] = 3306
    READ_ONLY_SQL: ClassVar[str] = "START TRANSACTION READ ONLY"
    TITLE: ClassVar[str] = "MySQL"
    DESCRIPTION: ClassVar[str] = (
        "MySQL over the SqlDriver protocol. No MySQL driver is in uv.lock: install an async "
        "driver (optional extra) and inject driver_factory."
    )

    def default_factory(self) -> SqlDriverFactory:
        """A factory that explains the driver is an optional extra."""
        return missing_mysql_driver
