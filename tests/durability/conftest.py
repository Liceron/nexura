"""Fixtures for the A6 durability (kill/takeover) tests."""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from durability_harness import POSTGRES_ENV, Backend, EffectService, Harness

if sys.platform == "win32":  # pragma: no cover - SIGKILL/SIGSTOP and /proc are POSIX/Linux
    collect_ignore_glob = ["test_*.py"]


@pytest.fixture
def sqlite_backend() -> Backend:
    return Backend("sqlite")


@pytest.fixture
def postgres_backend() -> Backend:
    dsn = os.environ.get(POSTGRES_ENV)
    if not dsn:
        pytest.skip(f"{POSTGRES_ENV} is not set; Postgres durability rows need a disposable DB")
    return Backend("postgres", dsn)


@pytest.fixture
def backend(request: pytest.FixtureRequest) -> Backend:
    """Parametrize indirectly with ``"sqlite"`` or ``"postgres"``."""
    name = getattr(request, "param", "sqlite")
    fixture: Backend = request.getfixturevalue(f"{name}_backend")
    return fixture


@pytest.fixture
def harness(tmp_path: Path, backend: Backend) -> Iterator[Harness]:
    service = EffectService(tmp_path / "effect-ledger.sqlite3")
    instance = Harness(tmp_path, backend, service)
    try:
        yield instance
    finally:
        instance.close()
