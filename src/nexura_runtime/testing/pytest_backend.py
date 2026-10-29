# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Backend selection and disposable direct-Postgres schemas (never a pooler DSN).

Load via ``pytest_plugins``. The optional pytest dependency is needed only by tests.
``backend`` parametrizes store contracts; ``service_database_url`` supplies the
schema URL. ``--service-backend`` switches service suites through the environment.
``isolated_postgres_database`` gives a test its own database and points
``NEXURA_TEST_POSTGRES_URI`` at it, so tests that write to ``public`` leave nothing behind.

Postgres is *required* (a missing URI or a run in which no test passed fails instead of
skipping) under ``--service-backend=postgres`` or ``NEXURA_REQUIRE_POSTGRES_TESTS=1``,
which the CI Postgres jobs set. Local runs without the URI still skip.
"""

from __future__ import annotations

import asyncio
import os
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import pytest

URI_ENV = "NEXURA_TEST_POSTGRES_URI"
REQUIRE_ENV = "NEXURA_REQUIRE_POSTGRES_TESTS"


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--service-backend", choices=("sqlite", "postgres"), default="sqlite")


def postgres_required(config: pytest.Config) -> bool:
    return bool(
        config.getoption("--service-backend") == "postgres" or os.environ.get(REQUIRE_ENV) == "1"
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "sqlite_only: verifies SQLite file/schema mechanics; inapplicable to Postgres"
    )
    config.addinivalue_line("markers", "pooled: requires NEXURA_TEST_POOLED_URI")
    if postgres_required(config) and not os.environ.get(URI_ENV):
        raise pytest.UsageError(
            f"{URI_ENV} is not set, but Postgres tests are required "
            f"(--service-backend=postgres or {REQUIRE_ENV}=1); refusing to skip them all"
        )


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    config = session.config
    if not postgres_required(config) or hasattr(config, "workerinput"):
        return  # xdist workers report to the controller, which runs this check
    reporter = config.pluginmanager.get_plugin("terminalreporter")
    passed = len(reporter.stats.get("passed", [])) if reporter is not None else 0
    if passed == 0 and exitstatus in (pytest.ExitCode.OK, pytest.ExitCode.NO_TESTS_COLLECTED):
        if reporter is not None:
            reporter.write_line(
                "Postgres tests are required but no test passed (all skipped or none ran)",
                red=True,
            )
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


def is_service_database_address(address: Any) -> bool:
    """True when ``address`` is the Postgres service database of ``--service-backend=postgres``.

    Tests that forbid every socket connect use this to let the service's own database through
    (and nothing else) when they run on the Postgres backend. Without ``NEXURA_DATABASE_URL``
    it is always False, so SQLite runs keep the full guard.
    """
    url = os.environ.get("NEXURA_DATABASE_URL")
    if not url or not isinstance(address, tuple) or len(address) < 2:
        return False
    parts = urlsplit(url)
    return bool((address[0], address[1]) == (parts.hostname, parts.port or 5432))


def _postgres_uri(request: pytest.FixtureRequest) -> str:
    uri = os.environ.get(URI_ENV)
    if uri:
        return uri
    if postgres_required(request.config):
        pytest.fail(f"{URI_ENV} is not configured, and Postgres tests are required")
    pytest.skip(f"{URI_ENV} is not configured")


@pytest.fixture(params=["sqlite", "postgres"])
def backend(request: pytest.FixtureRequest) -> str:
    if request.param == "postgres":
        _postgres_uri(request)
    return str(request.param)


def _admin(uri: str, sql: str) -> None:
    """Run one statement on a fresh asyncpg connection, off any running event loop."""

    async def execute() -> None:
        import asyncpg  # type: ignore[import-untyped]

        conn = await asyncpg.connect(uri, statement_cache_size=0)
        try:
            await conn.execute(sql)
        finally:
            await conn.close()

    errors: list[BaseException] = []

    def target() -> None:
        try:
            asyncio.run(execute())
        except BaseException as exc:  # re-raised in the caller's thread
            errors.append(exc)

    thread = threading.Thread(target=target)
    thread.start()
    thread.join()
    if errors:
        raise errors[0]


@pytest.fixture
def isolated_postgres_database(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[str | None]:
    """A database of this test's own (dropped afterwards); ``None`` without the URI."""
    uri = os.environ.get(URI_ENV)
    if not uri:
        yield None
        return
    name = "nexura_test_" + uuid.uuid4().hex
    parts = urlsplit(uri)
    dsn = urlunsplit(parts._replace(path="/" + name))
    _admin(uri, f'CREATE DATABASE "{name}"')
    monkeypatch.setenv(URI_ENV, dsn)
    try:
        yield dsn
    finally:
        _admin(uri, f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@contextmanager
def disposable_schema_url(uri: str) -> Iterator[str]:
    """A DSN whose ``search_path`` is a fresh schema of its own, dropped afterwards."""
    from nexura_runtime.modules.optional_packages import POSTGRES_PACKAGE, require_package

    postgres = require_package(POSTGRES_PACKAGE, "Postgres backend tests")

    name = "service_test_" + uuid.uuid4().hex
    parts = urlsplit(uri)
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query)
        if key not in {"search_path", "application_name"}
    ]
    # application_name tags this test's server sessions, so teardown can end the ones that
    # apps without a lifespan (or stores with their own pools) never closed.
    extra = [("search_path", name), ("application_name", name)]
    dsn = urlunsplit(parts._replace(query=urlencode([*query, *extra])))
    db = postgres.Database.from_url(uri)

    async def execute(conn: Any, sql: str) -> None:
        await conn.execute(sql)

    try:
        db.run(lambda conn: execute(conn, f'CREATE SCHEMA "{name}"'))
        yield dsn
    finally:
        try:
            # Apps that never ran their lifespan keep a pool on this URL; close it for them,
            # then end any other session of this test (for example workflow-store pools).
            postgres.Database._close_all_for_tests(dsn)

            async def terminate(conn: Any) -> None:
                await conn.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE application_name = $1 AND pid <> pg_backend_pid()",
                    name,
                )

            db.run(terminate)
            db.run(lambda conn: execute(conn, f'DROP SCHEMA IF EXISTS "{name}" CASCADE'))
        finally:
            db.close()


#: Test-only value of ``NEXURA_TRIGGER_SECRET_KEY`` set by the database fixtures.
TEST_TRIGGER_SECRET_KEY = "nexura-test-trigger-secret-key"


def shared_test_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the keys a Postgres service refuses to start without (MVP WP9), unless set already.

    ``NEXURA_MASTER_KEY`` gets a fresh random key and ``NEXURA_TRIGGER_SECRET_KEY`` a fixed
    test value, for this test only. A test of the refusal itself removes them.
    """
    if not os.environ.get("NEXURA_MASTER_KEY", "").strip():
        from nexura_runtime.secrets import generate_master_key

        monkeypatch.setenv("NEXURA_MASTER_KEY", generate_master_key())
    if not os.environ.get("NEXURA_TRIGGER_SECRET_KEY", "").strip():
        monkeypatch.setenv("NEXURA_TRIGGER_SECRET_KEY", TEST_TRIGGER_SECRET_KEY)


@pytest.fixture
def service_database_url(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[str]:
    """A disposable schema URL; the shared keys of a Postgres service are set for the test."""
    shared_test_keys(monkeypatch)
    with disposable_schema_url(_postgres_uri(request)) as dsn:
        yield dsn


@pytest.fixture
def second_service_database_url(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[str]:
    """Another, separate service database (schema) in the same test, for tests that need two
    deployments (for example a staging database next to prod's)."""
    shared_test_keys(monkeypatch)
    with disposable_schema_url(_postgres_uri(request)) as dsn:
        yield dsn


@pytest.fixture
def sqlite_repository_import() -> Iterator[None]:
    """TEST ONLY: file-backed workspace repositories may bind to Postgres in this test.

    See ``nexura_runtime.testing.sqlite_import``. ``--service-backend=postgres`` turns it on for
    every test; tests that check the refusal itself do not request it.
    """
    from nexura_runtime.testing.sqlite_import import allow_sqlite_repository_import

    with allow_sqlite_repository_import():
        yield


@pytest.fixture
def sqlite_studio_store() -> Iterator[None]:
    """TEST ONLY: a SQLite ``StudioStore`` passed to ``create_api_app`` is accepted in Postgres
    mode (MVP WP3). ``--service-backend=postgres`` turns it on for every test; tests that check
    the refusal itself do not request it."""
    from nexura_runtime.testing.sqlite_import import allow_sqlite_studio_store

    with allow_sqlite_studio_store():
        yield


@pytest.fixture(autouse=True)
def _service_backend(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    if request.config.getoption("--service-backend") != "postgres":
        return
    if request.node.get_closest_marker("sqlite_only") is not None:
        pytest.skip("SQLite file/schema mechanics do not apply to Postgres")
    url = request.getfixturevalue("service_database_url")
    monkeypatch.setenv("NEXURA_DATABASE_URL", url)
    # Every store is on Postgres (MVP WP8); the switch only lets the SQLite-only features
    # (repro capture, operator-token delivery tokens) that some tests use run next to it.
    monkeypatch.setenv("NEXURA_ALLOW_PARTIAL_POSTGRES", "1")
    # Tests that build a file-backed membership repository copy it into Postgres once
    # (test-only switch; the service refuses such a repository in Postgres mode).
    request.getfixturevalue("sqlite_repository_import")
    # Tests that pass their own SQLite StudioStore keep it (test-only; the service refuses one
    # in Postgres mode). Every other test's runs live in Postgres (MVP WP3).
    request.getfixturevalue("sqlite_studio_store")
