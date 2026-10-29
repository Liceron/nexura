# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Service storage seams and read-only readiness (WP0/WP1)."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from nexura_agents.types import Message

from nexura_runtime.release.health import HealthChecker, create_health_router
from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.service.api.runs import _budget_manager
from nexura_runtime.service.storage_backend import (
    SQLiteIntegrationRegistrations,
    resolve_database_url,
)
from nexura_runtime.service.workspaces.repository import WorkspaceRepository
from nexura_runtime.testing.pytest_backend import shared_test_keys


def test_database_url_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NEXURA_DATABASE_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://ignored.example/db")
    assert resolve_database_url(None) is None
    monkeypatch.setenv("NEXURA_DATABASE_URL", "postgresql://env.example/db")
    assert resolve_database_url(None) == "postgresql://env.example/db"
    assert (
        resolve_database_url("postgresql://explicit.example/db")
        == "postgresql://explicit.example/db"
    )
    assert resolve_database_url("") is None


def test_postgres_service_fails_before_sqlite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shared_test_keys(monkeypatch)  # MVP WP9: Postgres mode needs the shared keys
    # Full Postgres mode (MVP WP8), not the development switch (which --service-backend=postgres
    # sets): the service goes straight to the database and never opens or creates SQLite.
    monkeypatch.delenv("NEXURA_ALLOW_PARTIAL_POSTGRES", raising=False)
    from nexura_runtime.service.storage_backend import ServiceStores

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Postgres service must not open SQLite")

    class Connecting(Exception):
        pass

    def connect(self: Any) -> Any:
        raise Connecting  # the first connection: every refusal and SQLite seam is behind us

    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(ServiceStores, "open_database", connect)
    path = tmp_path / "never-created.sqlite3"
    with pytest.raises(Connecting):
        create_api_app(db_path=path, database_url="postgresql://fixture.example/db")
    assert not path.exists() and not list(tmp_path.iterdir())


@pytest.mark.sqlite_only  # Inspects the SQLite file, schema and write counters explicitly.
async def test_readyz_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "service.sqlite3"
    app = create_api_app(db_path=path)
    statements: list[str] = []
    original = sqlite3.connect

    def tracked(*args: Any, **kwargs: Any) -> Any:
        conn = original(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    async with app.router.lifespan_context(app):
        with closing(original(path)) as observer:
            schema = observer.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall()
            version = observer.execute("PRAGMA data_version").fetchone()
            monkeypatch.setattr(sqlite3, "connect", tracked)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
            ) as client:
                for _ in range(3):
                    assert (await client.get("/readyz")).status_code == 200
            assert observer.execute("PRAGMA data_version").fetchone() == version
            assert (
                observer.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall() == schema
            )
        # MVP WP5: the probe reads the schema page, so an unreadable file is reported.
        assert statements == ["SELECT count(*) FROM sqlite_master"] * 3


async def test_readyz_backend_failure_is_503() -> None:
    class BrokenDatabase:
        def ping(self) -> bool:
            raise OSError("fixture unavailable")

    app = FastAPI()
    app.include_router(create_health_router(HealthChecker(database=BrokenDatabase())))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["probes"][1]["error"] == "fixture unavailable"


def test_uninitialized_health_probe_does_not_create_file(tmp_path: Path) -> None:
    path = tmp_path / "health.db"
    assert HealthChecker(db_path=path).check_database().state == "healthy"
    assert not path.exists()


async def test_app_ledger_shared_by_budgets_and_diagnostics(tmp_path: Path) -> None:
    app = create_api_app(db_path=tmp_path / "service.sqlite3")
    async with app.router.lifespan_context(app):
        manager = _budget_manager(app.state)
        assert manager.ledger is app.state.app_usage_ledger
        assert _budget_manager(app.state) is manager


async def test_session_delete_uses_public_store_and_durable_ownership(tmp_path: Path) -> None:
    path = tmp_path / "service.sqlite3"
    app = create_api_app(db_path=path)
    async with app.router.lifespan_context(app):
        await app.state.session_store.save("s1", [Message(role="user", content="fixture")], 0)
        app.state.workspace_repo.record_resource_ownership(
            "session", "s1", "default", "local-owner"
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            assert (await client.delete("/api/v1/sessions/s1")).status_code == 200
        assert await app.state.session_store.get("s1") is None
        assert app.state.workspace_repo.get_resource_ownership("session", "s1") is None
    restored = WorkspaceRepository(path)
    assert restored.get_resource_ownership("session", "s1") is None


def test_workspace_policy_method_persists(tmp_path: Path) -> None:
    path = tmp_path / "service.sqlite3"
    repo = WorkspaceRepository(path)
    repo.create_workspace("policy", "Policy", "owner", metadata={"kept": 1})
    repo.update_workspace_metadata("policy", {"require_promotion_approval": True})
    restored = WorkspaceRepository(path).get_workspace("policy")
    assert restored is not None
    assert restored.metadata == {"kept": 1, "require_promotion_approval": True}


def test_registration_store_survives_restart(tmp_path: Path) -> None:
    path = tmp_path / "service.sqlite3"
    store = SQLiteIntegrationRegistrations(path)
    assert store.list() == []
    assert not path.exists()
    store.save("w1", ["tool.a", "tool.b"], "mcp", "math_fixture", "math")
    assert SQLiteIntegrationRegistrations(path).list() == [
        ("w1", "tool.a", "mcp", "math_fixture", "math"),
        ("w1", "tool.b", "mcp", "math_fixture", "math"),
    ]


async def test_trace_get_is_unchanged_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WP1 review M1: traces never read the app usage ledger nor take its write lock."""
    from decimal import Decimal

    from nexura_runtime.service.storage_backend import app_usage_ledger
    from nexura_runtime.studio.traces import build_trace
    from nexura_runtime.workflow import WorkflowNode, WorkflowSpec

    path = tmp_path / "service.sqlite3"
    app = create_api_app(db_path=path)
    async with app.router.lifespan_context(app):
        store = app.state.store
        spec = WorkflowSpec(
            name="wf",
            entry_node="step",
            nodes={"step": WorkflowNode(id="step", handler="dummy")},
            terminal_nodes=["step"],
        )
        await store.record_run_start(run_id="r1", workflow_id="wf", spec=spec, initial_state={})
        app.state.workspace_repo.record_resource_ownership("run", "r1", "default", "local-owner")
        call = {
            "module_id": "nexura.models.openai",
            "attempt_id": "att_1",
            "model_id": "gpt-4o",
            "provider_call_id": "c1",
        }
        await store.record_run_event(
            run_id="r1", event_type="MODULE_INVOKE_STARTED", node_id="step", data=call
        )
        await store.record_run_event(
            run_id="r1",
            event_type="MODULE_INVOKE_COMPLETED",
            node_id="step",
            data={**call, "payload": {"usage": {"prompt_tokens": 5, "completion_tokens": 5}}},
        )
        # Budget spend lives in the app ledger; at HEAD traces never saw it.
        app_usage_ledger(app.state).record_commit(
            workspace_id="default",
            run_id="r1",
            amount_usd=Decimal("0.5"),
            tokens=10,
            usage_state="reported",
            provider_call_id="c1",
            model_id="gpt-4o",
        )
        run = await store.get_run("r1")
        # The app's price table prices the trace (live pass C2); the ledger is not read.
        pricing = app.state.pricing_table
        expected = build_trace(run, ledger=None, pricing_table=pricing)
        # The ledger would change the trace, so equality below is meaningful.
        assert build_trace(run, ledger=app_usage_ledger(app.state), pricing_table=pricing) != (
            expected
        )
        statements: list[str] = []
        original = sqlite3.connect

        def tracked(*args: Any, **kwargs: Any) -> Any:
            conn = original(*args, **kwargs)
            conn.set_trace_callback(statements.append)
            return conn

        with closing(original(path)) as observer:
            version = observer.execute("PRAGMA data_version").fetchone()
            monkeypatch.setattr(sqlite3, "connect", tracked)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
            ) as client:
                response = await client.get("/api/v1/runs/r1/trace")
            assert response.status_code == 200
            assert observer.execute("PRAGMA data_version").fetchone() == version
        assert response.json() == expected
        writes = ("BEGIN IMMEDIATE", "INSERT", "UPDATE", "DELETE", "CREATE", "REPLACE")
        assert not [sql for sql in statements if sql.lstrip().upper().startswith(writes)]
        assert getattr(app.state, "usage_ledger", None) is None


def test_partial_postgres_needs_the_explicit_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nexura_runtime.service.storage_backend import ServiceStores

    # SQLite means SQLite here, even under --service-backend=postgres.
    monkeypatch.delenv("NEXURA_DATABASE_URL", raising=False)
    monkeypatch.delenv("NEXURA_ALLOW_PARTIAL_POSTGRES", raising=False)
    for name in ("K_SERVICE", "CLOUD_RUN_JOB"):
        monkeypatch.delenv(name, raising=False)
    # MVP WP8: Postgres mode itself needs no switch; a SQLite-only feature does.
    stores = ServiceStores(tmp_path / "s.sqlite3", "postgresql://fixture.example/db")
    stores.require_service_support()
    assert stores.create("x", dict) == {}
    with pytest.raises(ValueError, match="NEXURA_ALLOW_PARTIAL_POSTGRES=1"):
        stores.require_sqlite_feature("repro_capture")
    assert not stores.partial
    monkeypatch.setenv("NEXURA_ALLOW_PARTIAL_POSTGRES", "true")  # only "1" counts
    with pytest.raises(ValueError):
        ServiceStores(
            tmp_path / "s.sqlite3", "postgresql://fixture.example/db"
        ).require_sqlite_feature("repro_capture")
    monkeypatch.setenv("NEXURA_ALLOW_PARTIAL_POSTGRES", "1")
    partial = ServiceStores(tmp_path / "s.sqlite3", "postgresql://fixture.example/db")
    partial.require_sqlite_feature("repro_capture")
    assert partial.partial and partial.create("x", dict) == {}
    monkeypatch.delenv("NEXURA_ALLOW_PARTIAL_POSTGRES")
    sqlite = ServiceStores(tmp_path / "s.sqlite3")
    sqlite.require_sqlite_feature("repro_capture")  # SQLite: no switch, never partial
    assert not sqlite.partial and sqlite.create("x", dict) == {}
    assert not (tmp_path / "s.sqlite3").exists()


@pytest.mark.parametrize("cloud_var", ["K_SERVICE", "CLOUD_RUN_JOB"])  # services and jobs
def test_partial_postgres_is_refused_on_cloud_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cloud_var: str
) -> None:
    from nexura_runtime.service.storage_backend import ServiceStores

    # SQLite means SQLite here, even under --service-backend=postgres.
    monkeypatch.delenv("NEXURA_DATABASE_URL", raising=False)
    monkeypatch.setenv("NEXURA_ALLOW_PARTIAL_POSTGRES", "1")
    monkeypatch.delenv("K_SERVICE", raising=False)
    monkeypatch.delenv("CLOUD_RUN_JOB", raising=False)
    monkeypatch.setenv(cloud_var, "nexura")
    with pytest.raises(RuntimeError, match=f"refused on Cloud Run \\({cloud_var} is set\\)"):
        ServiceStores(tmp_path / "s.sqlite3", "postgresql://fixture.example/db").create("x", dict)
    with pytest.raises(RuntimeError, match="refused on Cloud Run"):
        create_api_app(
            db_path=tmp_path / "s.sqlite3", database_url="postgresql://fixture.example/db"
        )
    assert not (tmp_path / "s.sqlite3").exists()
    # SQLite on Cloud Run is unaffected by this rule.
    assert ServiceStores(tmp_path / "s.sqlite3").create("x", dict) == {}


def test_partial_postgres_warns_at_startup_without_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: Any
) -> None:
    shared_test_keys(monkeypatch)  # MVP WP9: Postgres mode needs the shared keys
    monkeypatch.setenv("NEXURA_ALLOW_PARTIAL_POSTGRES", "1")
    monkeypatch.delenv("K_SERVICE", raising=False)
    url = "postgresql://u:fixture-secret@fixture-host.example/db?sslmode=require"

    class Unbindable:  # stops the app before it would connect to the fixture URL
        db_path = None

    # The switch alone (MVP WP8): nothing is partial, so no partial warning.
    with caplog.at_level("WARNING", logger="nexura_runtime.service"), pytest.raises(TypeError):
        create_api_app(
            db_path=tmp_path / "s.sqlite3", database_url=url, workspace_repo=Unbindable()
        )
    assert "PARTIAL POSTGRES MODE" not in caplog.text
    caplog.clear()
    # A SQLite-only feature under the switch: partial mode, warned at startup.
    with caplog.at_level("WARNING", logger="nexura_runtime.service"), pytest.raises(TypeError):
        create_api_app(
            db_path=tmp_path / "s.sqlite3",
            database_url=url,
            workspace_repo=Unbindable(),
            capture_failed_runs=True,
        )
    warning = "\n".join(r.getMessage() for r in caplog.records if r.levelname == "WARNING")
    assert "PARTIAL POSTGRES MODE" in warning and "never" in warning
    assert str(tmp_path / "s.sqlite3") in warning
    assert "repro capture" in warning
    assert "fixture-secret" not in caplog.text and "fixture-host" not in caplog.text


def _public_readyz_provider() -> Any:
    """The local owner, but never an operator: /readyz shows its public (minimal) body."""
    from nexura_runtime.service.api.auth_providers import LocalOwnerAuthProvider

    class NotOperator(LocalOwnerAuthProvider):
        def is_operator(self, request: Any) -> bool:
            return False

    return NotOperator()


async def test_sqlite_readyz_body_is_unchanged(tmp_path: Path) -> None:
    app = create_api_app(db_path=tmp_path / "s.sqlite3", auth_provider=_public_readyz_provider())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
    ) as client:
        response = await client.get("/readyz")
    assert response.json() == {"status": "healthy"}


async def test_partial_postgres_is_reported_on_readyz(
    service_database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from urllib.parse import urlsplit

    monkeypatch.setenv("NEXURA_ALLOW_PARTIAL_POSTGRES", "1")
    monkeypatch.delenv("K_SERVICE", raising=False)
    netloc = urlsplit(service_database_url).netloc
    # A SQLite-only feature under the switch makes the service partial (MVP WP8).
    app = create_api_app(
        db_path=tmp_path / "s.sqlite3",
        database_url=service_database_url,
        capture_failed_runs=True,
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            operator = await client.get("/readyz")  # local owner: the operator view
        assert operator.json()["storage"] == "partial-postgres"
        assert operator.json()["status"] == "healthy"
        assert netloc not in operator.text and "search_path" not in operator.text
    # Without one, the same switch changes nothing: full Postgres mode, no SQLite file.
    full = create_api_app(
        db_path=tmp_path / "full" / "s.sqlite3",
        database_url=service_database_url,
        blob_url=(tmp_path / "s.sqlite3.blobs").as_uri(),
    )
    async with full.router.lifespan_context(full):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=full), base_url="http://127.0.0.1"
        ) as client:
            operator = await client.get("/readyz")
        assert operator.json()["storage"] == "postgres"
        assert operator.json()["status"] == "healthy"
    assert not (tmp_path / "full" / "s.sqlite3").exists()
    public_app = create_api_app(
        db_path=tmp_path / "t.sqlite3",
        database_url=service_database_url,
        auth_provider=_public_readyz_provider(),
        # Same database, so the same blob store (MVP WP3: a database is bound to one store).
        blob_url=(tmp_path / "s.sqlite3.blobs").as_uri(),
    )
    async with public_app.router.lifespan_context(public_app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=public_app), base_url="http://127.0.0.1"
        ) as client:
            public = (await client.get("/readyz")).json()
        assert public == {"status": "healthy"}  # the public body stays minimal
