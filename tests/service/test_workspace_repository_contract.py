# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Workspace and resource-ownership contract on both backends (MVP Postgres WP2).

Every test runs against SQLite (``bind_db_path``) and Postgres (``bind_database``, skipped
without ``NEXURA_TEST_POSTGRES_URI``). The same assertions on both prove the Postgres
repository keeps the SQLite behaviour, including iteration order. Postgres-only tests at the
end cover what SQLite cannot do: a second instance sees writes at once (read-through).
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from nexura_runtime.service.workspaces.repository import (
    UNOWNED_WORKSPACE,
    WorkspaceExistsError,
    WorkspaceRepository,
)
from nexura_runtime.testing.pytest_backend import shared_test_keys

Opener = Callable[[], WorkspaceRepository]


@pytest.fixture
def open_repo(backend: str, tmp_path: Path, request: pytest.FixtureRequest) -> Iterator[Opener]:
    """Open a repository on this test's storage; each call is a new "instance"."""
    if backend == "sqlite":
        path = tmp_path / "workspaces.sqlite3"
        yield lambda: WorkspaceRepository(path)
        return
    from nexura_postgres import Database

    database = Database.from_url(request.getfixturevalue("service_database_url"))

    def open_postgres() -> WorkspaceRepository:
        repo = WorkspaceRepository()
        repo.bind_database(database)
        return repo

    try:
        yield open_postgres
    finally:
        database.close()


def _ids(workspaces: list[Any]) -> list[str]:
    return [ws.workspace_id for ws in workspaces]


def test_default_workspace_and_creation_order(open_repo: Opener) -> None:
    repo = open_repo()
    assert _ids(repo.list_workspaces()) == ["default"]
    default = repo.get_workspace("default")
    assert default is not None and default.owner_id == "local-owner"
    for ws_id in ("b", "a", "c"):
        repo.create_workspace(ws_id, ws_id.upper(), "owner", metadata={"k": ws_id})
    assert _ids(repo.list_workspaces()) == ["default", "b", "a", "c"]
    assert _ids(open_repo().list_workspaces()) == ["default", "b", "a", "c"]
    created = open_repo().get_workspace("a")
    assert created is not None
    assert (created.name, created.owner_id, created.metadata) == ("A", "owner", {"k": "a"})


def test_duplicate_and_invalid_workspace_ids_are_refused(open_repo: Opener) -> None:
    repo = open_repo()
    repo.create_workspace("ws", "WS", "owner")
    with pytest.raises(WorkspaceExistsError):
        repo.create_workspace("ws", "Again", "other")
    with pytest.raises(WorkspaceExistsError):
        open_repo().create_workspace("ws", "Again", "other")
    for bad in ("", "a\x00b", "line\nbreak"):
        with pytest.raises(ValueError, match="Invalid workspace id"):
            repo.create_workspace(bad, "x", "owner")
    stored = open_repo().get_workspace("ws")
    assert stored is not None and stored.name == "WS"


def test_metadata_update_merges_and_persists(open_repo: Opener) -> None:
    repo = open_repo()
    repo.create_workspace("ws", "WS", "owner", metadata={"kept": 1, "flag": False})
    updated = repo.update_workspace_metadata("ws", {"flag": True})
    assert updated.metadata == {"kept": 1, "flag": True}
    reopened = open_repo().get_workspace("ws")
    assert reopened is not None and reopened.metadata == {"kept": 1, "flag": True}
    with pytest.raises(KeyError):
        repo.update_workspace_metadata("missing", {"flag": True})


def test_resource_ownership_round_trip_and_order(open_repo: Opener) -> None:
    repo = open_repo()
    repo.record_resource_ownership("run", "r2", "ws", "alice")
    repo.record_resource_ownership("run", "r1", "ws", "bob")
    repo.record_resource_ownership("session", "s1", "ws", "alice")
    repo.record_resource_ownership("run", "r3", "other", "alice")
    # Re-recording moves ownership but keeps the record's position (SQLite dict semantics).
    repo.record_resource_ownership("run", "r2", "ws", "carol")
    # The live order is the SQLite repository's in-memory order. A reopened SQLite file
    # reloads in rowid order, where INSERT OR REPLACE moved r2 last; Postgres keeps the live
    # order on every instance. Callers do not depend on the order across a restart.
    assert repo.list_workspace_resource_ids("run", "ws") == ["r2", "r1"]
    for view in (repo, open_repo()):
        assert view.get_resource_ownership("run", "r2") == ("ws", "carol")
        assert view.get_resource_workspace("run", "r3") == "other"
        assert view.get_resource_ownership("run", "nope") is None
        assert sorted(view.list_workspace_resource_ids("run", "ws")) == ["r1", "r2"]
        assert view.list_owned_resource_ids("run", "ws", "bob") == ["r1"]
        assert view.list_owned_resource_ids("run", "ws", "carol") == ["r2"]
    assert repo.delete_resource_ownership("run", "r1") is True
    assert repo.delete_resource_ownership("run", "r1") is False
    assert open_repo().get_resource_ownership("run", "r1") is None


def test_fail_closed_unowned_resources(open_repo: Opener) -> None:
    repo = open_repo()
    assert repo.get_resource_workspace("run", "unknown") is None
    repo.fail_closed_unowned = True
    assert repo.get_resource_workspace("run", "unknown") == UNOWNED_WORKSPACE
    assert repo.get_resource_workspace("credential", "unknown") is None
    repo.record_resource_ownership("run", "known", "ws", "alice")
    assert repo.get_resource_workspace("run", "known") == "ws"


def test_open_source_membership_answers(open_repo: Opener) -> None:
    repo = open_repo()
    repo.create_workspace("ws", "WS", "someone")
    member = repo.get_member("ws", "local-owner")
    assert member is not None and member.role == "admin"
    assert repo.get_member("missing", "local-owner") is None
    assert repo.get_member("ws", "someone") is None
    assert repo.is_removed_member("ws", "local-owner") is False
    assert _ids(repo.list_workspaces_for_principal("local-owner")) == ["default", "ws"]
    assert repo.list_workspaces_for_principal("someone") == []


def test_records_made_before_binding_are_kept(backend: str, open_repo: Opener) -> None:
    first = open_repo()
    first.create_workspace("stored", "Stored", "owner")
    first.record_resource_ownership("run", "r", "stored", "alice")
    fresh = WorkspaceRepository()
    fresh.create_workspace("memory", "Memory", "owner")
    fresh.record_resource_ownership("run", "r", "memory", "bob")  # the stored row wins
    fresh.record_resource_ownership("run", "m", "memory", "bob")
    if backend == "sqlite":
        assert first.db_path is not None
        fresh.bind_db_path(first.db_path)
    else:
        assert isinstance(first, WorkspaceRepository) and first.database_bound
        fresh.bind_database(first._pg.database)
    for view in (fresh, open_repo()):
        assert {"default", "stored", "memory"} <= set(_ids(view.list_workspaces()))
        assert view.get_resource_ownership("run", "r") == ("stored", "alice")
        assert view.get_resource_ownership("run", "m") == ("memory", "bob")


def test_repository_binds_to_postgres_only_once(service_database_url: str, tmp_path: Path) -> None:
    from nexura_postgres import Database

    database = Database.from_url(service_database_url)
    try:
        repo = WorkspaceRepository()
        repo.bind_database(database)
        with pytest.raises(RuntimeError, match="already bound to Postgres"):
            repo.bind_database(database)
        with pytest.raises(RuntimeError, match="already bound to Postgres"):
            repo.bind_db_path(tmp_path / "other.sqlite3")
    finally:
        database.close()


# ---- Postgres only: what a cached SQLite repository cannot do ------------------------------


@pytest.fixture
def two_postgres_instances(service_database_url: str) -> Iterator[tuple[Any, Any]]:
    from nexura_postgres import Database

    database = Database.from_url(service_database_url)
    a, b = WorkspaceRepository(), WorkspaceRepository()
    a.bind_database(database)
    b.bind_database(database)
    try:
        yield a, b
    finally:
        database.close()


def test_postgres_second_instance_sees_writes_immediately(
    two_postgres_instances: tuple[Any, Any],
) -> None:
    a, b = two_postgres_instances
    a.create_workspace("ws", "WS", "owner")
    a.record_resource_ownership("run", "r1", "ws", "alice")
    assert b.get_workspace("ws") is not None
    assert b.get_resource_workspace("run", "r1") == "ws"
    a.update_workspace_metadata("ws", {"require_promotion_approval": True})
    ws = b.get_workspace("ws")
    assert ws is not None and ws.metadata == {"require_promotion_approval": True}
    assert b.delete_resource_ownership("run", "r1") is True
    assert a.get_resource_ownership("run", "r1") is None


def test_postgres_concurrent_create_has_one_winner(
    two_postgres_instances: tuple[Any, Any],
) -> None:
    outcomes: list[str] = []

    def create(repo: WorkspaceRepository, owner: str) -> None:
        try:
            repo.create_workspace("contested", "Contested", owner)
            outcomes.append(owner)
        except WorkspaceExistsError:
            outcomes.append("exists")

    threads = [
        threading.Thread(target=create, args=(repo, owner))
        for repo, owner in zip(two_postgres_instances, ("a", "b"), strict=True)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes)[1] == "exists" and outcomes.count("exists") == 1
    winner = two_postgres_instances[0].get_workspace("contested")
    assert winner is not None and winner.owner_id in {"a", "b"}


def test_postgres_concurrent_metadata_updates_both_land(
    two_postgres_instances: tuple[Any, Any],
) -> None:
    a, b = two_postgres_instances
    a.create_workspace("ws", "WS", "owner")
    threads = [
        threading.Thread(target=repo.update_workspace_metadata, args=("ws", {key: True}))
        for repo, key in ((a, "first"), (b, "second"))
        for _ in range(5)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    ws = a.get_workspace("ws")
    assert ws is not None and ws.metadata == {"first": True, "second": True}


# ---- SQLite default stays as it was ----------------------------------------------------------


@pytest.mark.sqlite_only  # asserts the SQLite binding, which --service-backend=postgres replaces
def test_sqlite_app_binds_workspaces_to_the_service_file(tmp_path: Path) -> None:
    from nexura_runtime.service.api.app import create_api_app

    path = tmp_path / "service.sqlite3"
    app = create_api_app(db_path=path)
    repo = app.state.workspace_repo
    assert isinstance(repo, WorkspaceRepository)
    assert repo.db_path == path.resolve() and repo.database_bound is False
    repo.create_workspace("ws", "WS", "owner")
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute(
            "SELECT name FROM nexura_service_workspaces WHERE workspace_id='ws'"
        ).fetchone() == ("WS",)


async def test_postgres_app_keeps_workspaces_out_of_sqlite(
    service_database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nexura_runtime.service.api.app import create_api_app

    monkeypatch.setenv("NEXURA_ALLOW_PARTIAL_POSTGRES", "1")
    path = tmp_path / "service.sqlite3"
    app = create_api_app(db_path=path, database_url=service_database_url)
    async with app.router.lifespan_context(app):
        repo = app.state.workspace_repo
        assert repo.database_bound and repo.db_path is None
        repo.create_workspace("ws", "WS", "owner")
        repo.record_resource_ownership("run", "r1", "ws", "alice")
        with closing(sqlite3.connect(path)) as conn:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
        assert not tables & {"nexura_service_workspaces", "nexura_service_resource_ownership"}
        other = WorkspaceRepository()
        other.bind_database(app.state.service_stores.open_database())
        assert other.get_resource_workspace("run", "r1") == "ws"


# ---- Postgres mode never keeps workspaces per instance (review M1) ---------------------------


def test_postgres_mode_refuses_a_sqlite_loaded_repository_before_connecting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review M4: a file-backed repository is refused (a stale file would undo removals)."""
    shared_test_keys(monkeypatch)  # MVP WP9: Postgres mode needs the shared keys
    from nexura_runtime.service import storage_backend
    from nexura_runtime.service.api.app import create_api_app
    from nexura_runtime.service.workspaces import repository

    # The test-only switch off (--service-backend=postgres turns it on for every test).
    monkeypatch.setattr(repository, "_TEST_SQLITE_IMPORT", False)

    monkeypatch.setenv("NEXURA_ALLOW_PARTIAL_POSTGRES", "1")
    monkeypatch.delenv("K_SERVICE", raising=False)
    monkeypatch.delenv("CLOUD_RUN_JOB", raising=False)
    opened: list[str] = []
    monkeypatch.setattr(
        storage_backend.ServiceStores, "open_database", lambda self: opened.append("x")
    )
    with pytest.raises(ValueError, match="db import-sqlite") as refused:
        create_api_app(
            db_path=tmp_path / "service.sqlite3",
            database_url="postgresql://fixture.example/db?sslmode=require",
            workspace_repo=WorkspaceRepository(tmp_path / "bound.sqlite3"),
        )
    assert "loaded from the SQLite file" in str(refused.value)
    assert not opened  # refused before any connection
    with pytest.raises(ValueError, match="loaded from the SQLite file"):
        WorkspaceRepository(tmp_path / "bound.sqlite3").bind_database(object())


@pytest.mark.parametrize("cloud_var", ["K_SERVICE", "CLOUD_RUN_JOB"])
def test_test_only_sqlite_import_is_ignored_on_cloud_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cloud_var: str
) -> None:
    from nexura_runtime.testing.sqlite_import import allow_sqlite_repository_import

    monkeypatch.delenv("K_SERVICE", raising=False)
    monkeypatch.delenv("CLOUD_RUN_JOB", raising=False)
    monkeypatch.setenv(cloud_var, "nexura")
    with (
        allow_sqlite_repository_import(),
        pytest.raises(ValueError, match="loaded from the SQLite file"),
    ):
        WorkspaceRepository(tmp_path / "bound.sqlite3").bind_database(object())


@pytest.mark.sqlite_only  # checks the switch's default; --service-backend=postgres turns it on
def test_test_only_sqlite_import_is_not_an_environment_switch() -> None:
    import inspect

    from nexura_runtime.service.workspaces import repository
    from nexura_runtime.testing import sqlite_import

    assert repository._TEST_SQLITE_IMPORT is False
    source = inspect.getsource(sqlite_import)
    assert "os.environ" not in source and "getenv" not in source
    assert "_TEST_SQLITE_IMPORT = " not in inspect.getsource(repository).replace(
        "_TEST_SQLITE_IMPORT = False", ""
    )


async def test_test_only_import_moves_a_sqlite_loaded_repository(
    service_database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sqlite_repository_import: None,
) -> None:
    from nexura_runtime.service.api.app import create_api_app

    monkeypatch.setenv("NEXURA_ALLOW_PARTIAL_POSTGRES", "1")
    monkeypatch.delenv("K_SERVICE", raising=False)
    monkeypatch.delenv("CLOUD_RUN_JOB", raising=False)
    file = tmp_path / "bound.sqlite3"
    seeded = WorkspaceRepository(file)
    seeded.create_workspace("from-file", "From file", "owner")
    seeded.record_resource_ownership("run", "r-file", "from-file", "alice")
    repo = WorkspaceRepository(file)
    app = create_api_app(
        db_path=tmp_path / "service.sqlite3", database_url=service_database_url, workspace_repo=repo
    )
    async with app.router.lifespan_context(app):
        assert app.state.workspace_repo is repo and repo.database_bound
        repo.create_workspace("after", "After", "owner")
        other = WorkspaceRepository()
        other.bind_database(app.state.service_stores.open_database())
        assert other.get_resource_workspace("run", "r-file") == "from-file"
        assert other.get_workspace("after") is not None
        assert WorkspaceRepository(file).get_workspace("after") is None  # file no longer used


def test_postgres_mode_refuses_a_repository_that_cannot_bind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nexura_runtime.service.api.app import bind_workspaces_to_postgres

    class Custom:
        db_path = None

    opened: list[bool] = []

    class Stores:
        def open_database(self) -> Any:
            opened.append(True)
            return object()

    with pytest.raises(TypeError, match="cannot bind to Postgres"):
        bind_workspaces_to_postgres(Custom(), Stores())
    assert not opened  # refused before any connection


def test_sqlite_mode_binding_is_unchanged(tmp_path: Path) -> None:
    """Without a URL a pre-bound repository is honoured and an unbound one gets the file."""
    from nexura_runtime.service.api.app import create_api_app

    own = WorkspaceRepository(tmp_path / "own.sqlite3")
    app = create_api_app(db_path=tmp_path / "service.sqlite3", workspace_repo=own)
    assert app.state.workspace_repo is own and own.db_path == (tmp_path / "own.sqlite3").resolve()
