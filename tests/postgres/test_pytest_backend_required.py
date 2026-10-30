# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Review M4: a Postgres CI run cannot pass by skipping every Postgres test."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

SAMPLE = """
import pytest

def test_backend(backend):
    assert backend in {"sqlite", "postgres"}

def test_skipped():
    pytest.skip("fixture skip")
"""
ONLY_SKIPS = """
import pytest

def test_skipped():
    pytest.skip("fixture skip")
"""


def _run(tmp_path: Path, source: str, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
    (tmp_path / "test_sample.py").write_text(source)
    child = {
        k: v
        for k, v in os.environ.items()
        if k not in {"NEXURA_TEST_POSTGRES_URI", "NEXURA_REQUIRE_POSTGRES_TESTS"}
    }
    child.update(env)
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "nexura_runtime.testing.pytest_backend",
            "-p",
            "no:cacheprovider",
            "test_sample.py",
            *args,
        ],
        cwd=tmp_path,
        env=child,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_local_run_without_uri_still_skips(tmp_path: Path) -> None:
    result = _run(tmp_path, SAMPLE)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed, 2 skipped" in result.stdout


@pytest.mark.parametrize(
    "args, env",
    [((), {"NEXURA_REQUIRE_POSTGRES_TESTS": "1"}), (("--service-backend=postgres",), {})],
)
def test_required_run_without_uri_fails(
    tmp_path: Path, args: tuple[str, ...], env: dict[str, str]
) -> None:
    result = _run(tmp_path, SAMPLE, *args, **env)
    assert result.returncode == pytest.ExitCode.USAGE_ERROR, result.stdout + result.stderr
    assert "NEXURA_TEST_POSTGRES_URI is not set" in result.stderr


def test_required_run_where_nothing_passed_fails(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        ONLY_SKIPS,
        NEXURA_REQUIRE_POSTGRES_TESTS="1",
        NEXURA_TEST_POSTGRES_URI="postgresql://fixture@127.0.0.1:9/never-contacted",
    )
    assert result.returncode == pytest.ExitCode.TESTS_FAILED, result.stdout + result.stderr
    assert "no test passed" in result.stdout


def test_network_guard_exception_is_only_the_service_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nexura_runtime.testing.pytest_backend import is_service_database_address

    monkeypatch.delenv("NEXURA_DATABASE_URL", raising=False)
    assert not is_service_database_address(("127.0.0.1", 5432))  # SQLite runs: full guard
    monkeypatch.setenv("NEXURA_DATABASE_URL", "postgresql://u@127.0.0.1:55432/db?search_path=s")
    assert is_service_database_address(("127.0.0.1", 55432))
    assert not is_service_database_address(("127.0.0.1", 443))
    assert not is_service_database_address(("93.184.216.34", 55432))
    assert not is_service_database_address("/tmp/socket")
