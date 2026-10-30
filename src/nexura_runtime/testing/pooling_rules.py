# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Static gate for transaction-pooler safety (MVP Postgres design §2b-5).

The service database may sit behind PgBouncer or Neon's pooler in transaction mode, where
consecutive transactions can run on different server sessions. So Postgres code must not
keep session state, prepared statements, listeners or session advisory locks.

``violations(source)`` returns one message per rule broken in a Python source. It checks
string literals (docstrings excepted; ``+`` concatenations of literals are folded, and a
literal that is only a dangerous keyword is flagged to catch ``"{} x".format("SET")``) and
asyncpg calls: ``.prepare``, listeners, and every ``create_pool``/``asyncpg.connect`` must
pass ``statement_cache_size=0`` (or ``**pool_options()``) and no session hooks
(``server_settings``, ``init``, ``setup``). ``scan`` fails loudly on a missing root.
``MUST_FLAG`` and ``MUST_PASS`` are the gate's own regression fixtures.

Limits: statement keywords (``SET``, ``LISTEN``, ``PREPARE`` …) are matched in upper case,
and SQL assembled at run time from non-literal parts cannot be seen. Review still applies.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable
from pathlib import Path

__all__ = ["MUST_FLAG", "MUST_PASS", "scan", "violations"]

_I = re.IGNORECASE
# Statement keywords are matched in upper case, as every SQL literal in this repository is
# written, so English messages such as "Set NEXURA_..." or "listens on" never match.
_STATEMENT = r"(?:^|;)\s*"
SQL_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "session advisory lock",
        re.compile(r"\bpg_(?:try_)?advisory_(?:un)?lock(?:_shared|_all)?\s*\(", _I),
    ),
    ("LISTEN/UNLISTEN", re.compile(_STATEMENT + r"(?:UN)?LISTEN\s+(?:\*|[\w\"?]+)\s*(?:;|$)")),
    ("NOTIFY", re.compile(_STATEMENT + r"NOTIFY\s+[\w\"?]+\s*(?:,|;|$)")),
    ("NOTIFY", re.compile(r"\bpg_notify\s*\(", _I)),
    ("temporary object", re.compile(r"\bTEMP(?:ORARY)?\s+(?:TABLE|VIEW|SEQUENCE)\b", _I)),
    ("Neon-only function", re.compile(r"\bneon_\w+\s*\(", _I)),
    (
        "session SET",
        re.compile(
            _STATEMENT
            + r"SET\s+(?!LOCAL\b|TRANSACTION\b)"
            + r"(?:(?i:SESSION|ROLE|TIME\s+ZONE)\b|[\w.]+\s*(?:=|(?i:TO)\b))"
        ),
    ),
    ("session RESET", re.compile(_STATEMENT + r"RESET\s+(?:ALL|[\w.]+)\s*(?:;|$)")),
    ("prepared statement", re.compile(_STATEMENT + r"(?:PREPARE\s+\w+\b|DEALLOCATE\b)")),
    ("cursor WITH HOLD", re.compile(r"\bWITH\s+HOLD\b", _I)),
    ("ALTER ROLE/DATABASE SET", re.compile(r"\bALTER\s+(?:ROLE|USER|DATABASE)\b[^;]*\bSET\b", _I)),
)
SET_CONFIG = re.compile(r"\bset_config\s*\(([^)]*)\)", _I)
BARE_KEYWORDS = {"SET", "RESET", "LISTEN", "UNLISTEN", "NOTIFY", "PREPARE", "DEALLOCATE"}
FORBIDDEN_METHODS = {
    "prepare": "explicit prepared statement",
    "add_listener": "LISTEN listener",
    "add_log_listener": "session log listener",
}
SESSION_HOOKS = {"server_settings", "init", "setup"}


def _string_checks(text: str) -> list[str]:
    found = [name for name, pattern in SQL_RULES if pattern.search(text)]
    for match in SET_CONFIG.finditer(text):
        if match[1].rsplit(",", 1)[-1].strip().lower() != "true":
            found.append("session-level set_config (is_local must be true)")
    if text.strip() in BARE_KEYWORDS:
        found.append(f"bare SQL keyword literal {text.strip()!r}")
    return found


def _fold(node: ast.AST) -> str | None:
    """The literal value of a ``+`` chain of string literals, else ``None``."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _fold(node.left), _fold(node.right)
        if left is not None and right is not None:
            return left + right
    return None


def _asyncpg_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "asyncpg":
            names.update(alias.asname or alias.name for alias in node.names)
    return names


def _is_pool_constructor(call: ast.Call, imported: set[str]) -> bool:
    func = call.func
    if isinstance(func, ast.Attribute):
        if func.attr == "create_pool":
            return True
        return (
            func.attr == "connect"
            and isinstance(func.value, ast.Name)
            and func.value.id == "asyncpg"
        )
    return (
        isinstance(func, ast.Name)
        and func.id in imported
        and func.id
        in {
            "create_pool",
            "connect",
        }
    )


def _passes_pool_options(call: ast.Call) -> bool:
    for keyword in call.keywords:
        if (
            keyword.arg == "statement_cache_size"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value == 0
        ):
            return True
        if keyword.arg is None and isinstance(keyword.value, ast.Call):
            target = keyword.value.func
            name = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")
            if name == "pool_options":
                return True
    return False


def violations(source: str) -> list[str]:
    tree = ast.parse(source)
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    imported = _asyncpg_names(tree)
    fstring_parts = {
        id(part)
        for node in ast.walk(tree)
        if isinstance(node, ast.JoinedStr)
        for part in node.values
    }
    errors: list[str] = []
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        if isinstance(node, ast.JoinedStr):
            text = "".join(
                part.value
                if isinstance(part, ast.Constant) and isinstance(part.value, str)
                else "?"
                for part in node.values
            )
            errors += [f"line {line}: {msg} (f-string)" for msg in _string_checks(text)]
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docstrings and id(node) not in fstring_parts:
                errors += [f"line {line}: {msg}" for msg in _string_checks(node.value)]
        elif isinstance(node, ast.BinOp) and (folded := _fold(node)) is not None:
            errors += [f"line {line}: {msg} (concatenated)" for msg in _string_checks(folded)]
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute) and node.func.attr in FORBIDDEN_METHODS:
                errors.append(f"line {line}: {FORBIDDEN_METHODS[node.func.attr]}")
            if _is_pool_constructor(node, imported):
                hooks = sorted(SESSION_HOOKS & {k.arg for k in node.keywords if k.arg})
                if hooks:
                    errors.append(f"line {line}: session hook {', '.join(hooks)} on pool")
                if not _passes_pool_options(node):
                    errors.append(f"line {line}: pool without statement_cache_size=0")
    return errors


def scan(roots: Iterable[Path]) -> dict[str, list[str]]:
    """Violations per file under ``roots``. A missing root is an error, never a silent pass.

    This module is skipped: it holds the patterns and fixtures themselves.
    """
    failures: dict[str, list[str]] = {}
    for root in roots:
        if not root.is_dir():
            raise FileNotFoundError(f"pooling-rules root does not exist: {root}")
        for path in sorted(root.rglob("*.py")):
            if path.resolve() == Path(__file__).resolve():
                continue
            if found := violations(path.read_text(encoding="utf-8")):
                failures[str(path)] = found
    return failures


#: Each snippet breaks a rule and must be flagged.
MUST_FLAG: tuple[str, ...] = (
    "q = 'SELECT pg_advisory_lock(1)'",
    "q = 'SELECT pg_try_advisory_lock_shared(1)'",
    "q = 'SELECT pg_advisory_unlock_all()'",
    "q = 'LISTEN events'",
    "q = 'UNLISTEN *'",
    "q = 'NOTIFY events'",
    "q = \"SELECT pg_notify('c', 'x')\"",
    "q = 'SET search_path=foo'",
    "q = 'SET SESSION statement_timeout=1'",
    "q = 'BEGIN; SET role x'",
    "q = 'RESET ALL'",
    "q = 'CREATE TEMP TABLE foo(id int)'",
    "q = 'CREATE TEMPORARY VIEW v AS SELECT 1'",
    "q = 'SELECT neon_x()'",
    "q = \"SELECT set_config('search_path', 'x', false)\"",
    "q = \"SELECT set_config('search_path', $1, FALSE)\"",
    "q = 'PREPARE s AS SELECT 1'",
    "q = 'DEALLOCATE ALL'",
    "q = 'DECLARE c CURSOR WITH HOLD FOR SELECT 1'",
    "q = 'ALTER ROLE app SET search_path = x'",
    "q = 'ALTER DATABASE db SET work_mem = 1'",
    "q = 'SET' + ' search_path=x'",
    "q = '{} search_path'.format('SET')",
    "q = f'SET search_path = {schema}'",
    "q = f'SELECT 1; LISTEN {channel}'",
    "conn.prepare('SELECT 1')",
    "conn.add_listener('events', callback)",
    "conn.add_log_listener(callback)",
    "pool = asyncpg.create_pool(dsn)",
    "pool = asyncpg.create_pool(dsn, statement_cache_size=100)",
    "pool = asyncpg.create_pool(dsn, statement_cache_size=0, server_settings={'a': 'b'})",
    "pool = asyncpg.create_pool(dsn, statement_cache_size=0, init=prepare_session)",
    "pool = asyncpg.create_pool(dsn, statement_cache_size=0, setup=on_acquire)",
    "conn = asyncpg.connect(dsn)",
    "from asyncpg import create_pool\npool = create_pool(dsn)",
    "from asyncpg import connect\nconn = connect(dsn)",
)

#: Each snippet is safe and must not be flagged.
MUST_PASS: tuple[str, ...] = (
    "q = 'SET LOCAL lock_timeout = 100'",
    "q = 'BEGIN; SET LOCAL statement_timeout = 1'",
    "q = 'UPDATE jobs SET status = 1'",
    "q = 'INSERT INTO t VALUES (1) ON CONFLICT (id) DO UPDATE SET v = 2'",
    "q = 'SELECT pg_advisory_xact_lock(1)'",
    "q = \"SELECT set_config('search_path', 'x', true)\"",
    "q = 'SELECT temp FROM readings'",
    "message = 'the server is listening on port 80'",
    "message = 'could not prepare the request'",
    "message = 'Set NEXURA_DATABASE_URL or --database-url'",
    "message = 'notify the operator, then retry'",
    "message = 'binary not found; set it to a path'",
    "q = f'UPDATE {table} SET updated_at = ?'",
    "q = 'SET TRANSACTION ISOLATION LEVEL REPEATABLE READ'",
    "pool = asyncpg.create_pool(dsn, statement_cache_size=0, reset=noop_reset)",
    "pool = asyncpg.create_pool(url, **pool_options())",
    "conn = sqlite3.connect(path)",
    'def f():\n    """SET search_path and LISTEN are forbidden; this is documentation."""',
)
