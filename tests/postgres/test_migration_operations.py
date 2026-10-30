# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Operator and failure contracts, using explicitly labelled local fake connections."""

from typing import Any

import pytest
from nexura_postgres import migrations
from nexura_postgres.db import Database

from nexura_runtime.cli.main import main


@pytest.mark.parametrize(
    "states, expected",
    [
        ({"studio": "current"}, 0),
        ({"studio": "behind"}, 1),
        ({"studio": "Database schema is newer"}, 1),
        ({"studio": "Migration checksum mismatch"}, 1),
    ],
)
def test_db_status_exit_codes(
    states: dict[str, str],
    expected: int,
    monkeypatch: pytest.MonkeyPatch,
    capsys: Any,
) -> None:
    class FakeDatabase:
        closed = False

        def run(self, fn: Any, **kwargs: Any) -> Any:
            assert fn is migrations.migration_status
            return states

        def close(self) -> None:
            self.closed = True

    database = FakeDatabase()
    monkeypatch.setattr(Database, "from_url", lambda url: database)
    assert main(["db", "status", "--database-url", "postgresql://fixture.example/db"]) == expected
    assert database.closed
    assert "shared service ledger" in capsys.readouterr().out


def test_db_migrate_closes_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeDatabase:
        closed = False

        def run(self, fn: Any, **kwargs: Any) -> Any:
            raise RuntimeError("fixture migration failure")

        def close(self) -> None:
            self.closed = True

    database = FakeDatabase()
    monkeypatch.setattr(Database, "from_url", lambda url: database)
    with pytest.raises(RuntimeError, match="fixture migration failure"):
        main(["db", "migrate", "--database-url", "postgresql://fixture.example/db"])
    assert database.closed


async def test_status_detects_behind_ahead_and_checksum(monkeypatch: pytest.MonkeyPatch) -> None:
    from test_db_foundations import MIGRATIONS, FakeConnection, FakeLedger

    monkeypatch.setattr(migrations, "COMPONENTS", {"studio": MIGRATIONS})
    ledger = FakeLedger()
    conn = FakeConnection(ledger)
    assert await migrations.migration_status(conn) == {"studio": "behind"}
    await migrations.migrate_all(conn)
    assert await migrations.migration_status(conn) == {"studio": "current"}
    ledger.rows["studio"][0]["checksum"] = "corrupt"
    assert "checksum mismatch" in (await migrations.migration_status(conn))["studio"]
    monkeypatch.setattr(migrations, "COMPONENTS", {})
    assert "newer" in (await migrations.migration_status(conn))["studio"]


async def test_migration_failure_rolls_back_ledger() -> None:
    from test_db_foundations import MIGRATIONS, FakeConnection, FakeLedger

    class BrokenConnection(FakeConnection):
        async def execute(self, sql: str, *args: Any, **kwargs: Any) -> None:
            if sql == "BROKEN DDL":
                raise RuntimeError("fixture DDL failure")
            await super().execute(sql, *args, **kwargs)

    ledger = FakeLedger()
    with pytest.raises(RuntimeError, match="fixture DDL failure"):
        await migrations.apply_migrations(
            BrokenConnection(ledger), "studio", (*MIGRATIONS, (2, "second", ("BROKEN DDL",)))
        )
    assert not ledger.rows
    await migrations.apply_migrations(FakeConnection(ledger), "studio", MIGRATIONS)
    assert len(ledger.rows["studio"]) == 1


def test_backend_fixture(backend: str) -> None:
    assert backend in {"sqlite", "postgres"}


def test_password_in_argv_is_warned(monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    class FakeDatabase:
        def run(self, fn: Any, **kwargs: Any) -> Any:
            return {}

        def close(self) -> None:
            pass

    monkeypatch.setattr(Database, "from_url", lambda url: FakeDatabase())
    assert (
        main(["db", "status", "--database-url", "postgresql://u:fixture-pw@fixture.example/db"])
        == 0
    )
    err = capsys.readouterr().err
    assert "NEXURA_DATABASE_URL" in err
    assert "fixture-pw" not in err
    assert main(["db", "status", "--database-url", "postgresql://u@fixture.example/db"]) == 0
    assert "warning" not in capsys.readouterr().err


def test_register_is_idempotent_and_refuses_redefinition(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(migrations, "COMPONENTS", {})
    first = ((1, "one", ("SELECT 1",)),)
    migrations.register("fixture", first)
    migrations.register("fixture", first)
    assert {"fixture": first} == migrations.COMPONENTS
    with pytest.raises(ValueError, match="already registered differently"):
        migrations.register("fixture", ((1, "one", ("SELECT 2",)),))
    with pytest.raises(ValueError, match="contiguous"):
        migrations.register("gap", ((2, "two", ()),))


def test_cli_loads_migration_entry_points(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib.metadata

    from nexura_runtime.cli import database

    loaded: list[str] = []

    class Entry:
        def load(self) -> None:
            loaded.append("fixture")

    def entry_points(*, group: str) -> list[Entry]:
        assert group == "nexura.migrations"
        return [Entry()]

    monkeypatch.setattr(importlib.metadata, "entry_points", entry_points)
    database.load_migration_components()
    assert loaded == ["fixture"]


def test_workspaces_component_is_registered_on_import() -> None:
    import nexura_postgres
    from nexura_postgres.workspaces import MIGRATIONS

    assert nexura_postgres.migrations.COMPONENTS["workspaces"] == MIGRATIONS
