# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Static regression gate for portable transaction pooling, including future ports.

The rules live in ``nexura_runtime.testing.pooling_rules`` so the enterprise suite runs the
same gate over ``enterprise/src`` (the enterprise tree is not part of the OSS checkout).
"""

from pathlib import Path

import pytest

from nexura_runtime.testing.pooling_rules import MUST_FLAG, MUST_PASS, scan, violations

ROOT = Path(__file__).resolve().parents[2]
ROOTS = [ROOT / "packages/nexura-postgres/src", ROOT / "src"]


def test_postgres_sources_follow_pooling_rules() -> None:
    assert scan(ROOTS) == {}


def test_missing_root_fails_instead_of_passing(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="does not exist"):
        scan([tmp_path / "absent"])


def test_new_pool_without_statement_cache_off_is_caught(tmp_path: Path) -> None:
    (tmp_path / "new_store.py").write_text(
        "import asyncpg\n\nasync def open(dsn):\n    return await asyncpg.create_pool(dsn)\n"
    )
    assert list(scan([tmp_path])) == [str(tmp_path / "new_store.py")]


def test_grep_gate_catches_forbidden_operations() -> None:
    for sql in [
        "SELECT pg_advisory_lock(1)",
        "LISTEN events",
        "NOTIFY events",
        "SET search_path=foo",
        "CREATE TEMP TABLE foo(id int)",
        "SELECT neon_x()",
    ]:
        assert violations(f"query = {sql!r}")
    assert violations("conn.prepare('SELECT 1')")
    assert not violations("query = 'SET LOCAL lock_timeout=100'")
    assert not violations("query = 'UPDATE jobs SET status=1'")
    assert not violations("query = 'SELECT pg_advisory_xact_lock(1)'")


@pytest.mark.parametrize("snippet", MUST_FLAG)
def test_negative_fixture_is_flagged(snippet: str) -> None:
    assert violations(snippet), snippet


@pytest.mark.parametrize("snippet", MUST_PASS)
def test_safe_fixture_is_not_flagged(snippet: str) -> None:
    assert violations(snippet) == [], snippet
