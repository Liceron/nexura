"""A6 S8: choosing where the service keeps workflow state (no database needed).

The default stays SQLite. A ``postgresql://`` URI (argument, ``--workflow-state-url`` or
``NEXURA_WORKFLOW_STATE_URL``) gives the service a ``PostgresWorkflowStore`` and serves the
effects API from its journal. Nothing here connects to a database: the Postgres store opens
its pool on first use. The end-to-end Postgres service run is in
``tests/postgres/test_postgres_workflow_store.py``.
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
from pathlib import Path

import pytest
from nexura_agents.contracts import MissingDependencyError
from nexura_postgres import PostgresWorkflowStore

from nexura_runtime.cli.main import main
from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.service.workflow_state import (
    WORKFLOW_STATE_URL_ENV,
    open_workflow_state_store,
    resolve_workflow_state_url,
)
from nexura_runtime.studio.store import StudioStore
from nexura_runtime.workflow import SQLiteWorkflowStore

URI = "postgresql://user:s3cret-pw@127.0.0.1:1/nexura"


def test_url_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(WORKFLOW_STATE_URL_ENV, raising=False)
    assert resolve_workflow_state_url() is None
    assert resolve_workflow_state_url(URI) == URI
    assert resolve_workflow_state_url("  postgres://h/db ") == "postgres://h/db"
    monkeypatch.setenv(WORKFLOW_STATE_URL_ENV, URI)
    assert resolve_workflow_state_url() == URI
    assert resolve_workflow_state_url("") is None  # an explicit empty value means SQLite
    monkeypatch.setenv(WORKFLOW_STATE_URL_ENV, "   ")
    assert resolve_workflow_state_url() is None


@pytest.mark.parametrize("url", ["mysql://user:s3cret-pw@h/db", "sqlite:///x.db", "x.db"])
def test_unsupported_urls_are_refused_without_echoing_them(url: str) -> None:
    with pytest.raises(ValueError, match="Unsupported workflow state URL scheme") as raised:
        resolve_workflow_state_url(url)
    assert "s3cret" not in str(raised.value)
    assert "x.db" not in str(raised.value)


def test_missing_package_names_pip_install(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "nexura_postgres", None)
    with pytest.raises(MissingDependencyError, match="pip install nexura-postgres"):
        open_workflow_state_store(URI)


def test_default_stays_sqlite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(WORKFLOW_STATE_URL_ENV, raising=False)
    app = create_api_app(db_path=tmp_path / "svc.sqlite3", profile="local-owner")
    try:
        assert isinstance(app.state.store.checkpoint_store, SQLiteWorkflowStore)
        assert app.state.workflow_state_store is app.state.store.checkpoint_store
        assert type(app.state.execution_store).__name__ == "SQLiteExecutionStore"
    finally:
        asyncio.run(app.state.store.close())
        asyncio.run(app.state.execution_store.close())


@pytest.mark.parametrize("source", ["argument", "environment"])
def test_postgres_url_selects_the_postgres_store_and_its_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    if source == "environment":
        monkeypatch.setenv(WORKFLOW_STATE_URL_ENV, URI)
        app = create_api_app(db_path=tmp_path / "svc.sqlite3", profile="local-owner")
    else:
        monkeypatch.delenv(WORKFLOW_STATE_URL_ENV, raising=False)
        app = create_api_app(
            db_path=tmp_path / "svc.sqlite3", profile="local-owner", workflow_state_url=URI
        )
    store = app.state.store.checkpoint_store
    try:
        assert isinstance(store, PostgresWorkflowStore)
        assert app.state.workflow_state_store is store
        assert app.state.execution_store is store.execution_store  # same pool
        # Studio run records stay in SQLite; no workflow checkpoint table is made there.
        with sqlite3.connect(tmp_path / "svc.sqlite3") as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master")}
        assert "nexura_studio_runs" in tables
        assert "nexura_workflow_checkpoints" not in tables
    finally:
        asyncio.run(app.state.store.close())


def test_explicit_empty_url_overrides_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(WORKFLOW_STATE_URL_ENV, URI)
    app = create_api_app(
        db_path=tmp_path / "svc.sqlite3", profile="local-owner", workflow_state_url=""
    )
    try:
        assert isinstance(app.state.store.checkpoint_store, SQLiteWorkflowStore)
    finally:
        asyncio.run(app.state.store.close())
        asyncio.run(app.state.execution_store.close())


def test_bad_url_fails_before_any_file_is_created(tmp_path: Path) -> None:
    db = tmp_path / "svc.sqlite3"
    with pytest.raises(ValueError, match="Unsupported workflow state URL scheme"):
        create_api_app(db_path=db, profile="local-owner", workflow_state_url="mysql://h/db")
    assert not db.exists()


def test_url_with_a_supplied_store_is_refused(tmp_path: Path) -> None:
    store = StudioStore(tmp_path / "svc.sqlite3")
    try:
        with pytest.raises(ValueError, match="checkpoint_store"):
            create_api_app(store=store, profile="local-owner", workflow_state_url=URI)
    finally:
        asyncio.run(store.close())


def test_studio_store_owns_and_closes_a_supplied_checkpoint_store(tmp_path: Path) -> None:
    closed: list[str] = []

    class Recording(PostgresWorkflowStore):
        async def close(self) -> None:
            closed.append("closed")
            await super().close()

    store = StudioStore(tmp_path / "svc.sqlite3", checkpoint_store=Recording(URI))
    asyncio.run(store.close())
    assert closed == ["closed"]


@pytest.mark.parametrize("command", ["serve", "worker"])
def test_cli_refuses_a_bad_url_with_exit_code_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], command: str
) -> None:
    code = main(
        [command, "--db", str(tmp_path / "svc.sqlite3"), "--workflow-state-url", "mysql://h/d"]
    )
    assert code == 2
    assert "Unsupported workflow state URL scheme" in capsys.readouterr().err
