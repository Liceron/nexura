# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""MVP Postgres WP5: service wiring of secrets, the usage ledger and the readiness probe.

* With ``NEXURA_DATABASE_URL`` (or ``database_url=``) and ``NEXURA_MASTER_KEY`` the service keeps
  credentials in ``PostgresSecretStore`` and spend, reservations and budget policies in
  ``PostgresUsageLedger``, both on the app's one shared database; none of their tables reaches
  the SQLite file. Without the URL, SQLite exactly as before.
* Two service instances on one database (each with its own SQLite file, as two Cloud Run
  instances): a credential created through one is listed, revoked and deleted through the other,
  and a revocation is honoured at once by the instance that did not make it; a run budget
  registered through one is read through the other.
* Run diagnostics read only the run's ledger entries and never wait for the budget lock.
* ``/readyz`` on both backends: healthy, unhealthy when the database is down (503, and the
  operator body never carries the database password), and no row or table is written by any
  number of probes.

Postgres tests skip without ``NEXURA_TEST_POSTGRES_URI``. Credentials are fixtures; nothing calls a
model or the network beyond the test database.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from collections.abc import Iterator
from contextlib import ExitStack, closing
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from _local_env import local_headers
from fastapi.testclient import TestClient
from nexura_agents.usage.ledger import UsageLedger

from nexura_runtime.credentials.errors import CredentialAccessDeniedError
from nexura_runtime.secrets.keys import generate_master_key
from nexura_runtime.secrets.store import EncryptedSecretStore
from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.service.api.runs import _budget_manager
from nexura_runtime.service.storage_backend import ServiceStores, app_usage_ledger

API = "/api/v1"
WP5_SQLITE_TABLES = {"nexura_secrets", "usage_entries", "reservations", "budget_policies"}
SECRET = "sk-test-WP5-wiring-0123456789abcdef"


def _spec() -> dict[str, Any]:
    return {
        "spec_version": 1,
        "name": "wp5",
        "entry_node": "n",
        "nodes": {"n": {"id": "n", "handler": "synthetic_order_lookup", "transitions": {}}},
    }


def _sqlite_tables(path: Path) -> set[str]:
    if not path.exists():
        return set()
    with closing(sqlite3.connect(path)) as conn:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


@pytest.fixture
def partial(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXURA_ALLOW_PARTIAL_POSTGRES", "1")
    monkeypatch.setenv("NEXURA_MASTER_KEY", generate_master_key())
    monkeypatch.delenv("NEXURA_INSECURE_ALLOW_PLAINTEXT_SECRETS", raising=False)
    for name in ("K_SERVICE", "CLOUD_RUN_JOB", "NEXURA_WORKFLOW_STATE_URL"):
        monkeypatch.delenv(name, raising=False)


def _exercise(client: TestClient, headers: dict[str, str]) -> dict[str, Any]:
    """A credential and a run with a budget, through the API."""
    made = client.post(
        f"{API}/credentials",
        headers=headers,
        json={"credential_id": "openai", "credential_type": "api_key", "secret": SECRET},
    )
    assert made.status_code == 201, made.text
    assert SECRET not in made.text
    verified = client.post(f"{API}/credentials/openai/verify", headers=headers)
    assert verified.status_code == 200, verified.text
    assert verified.json()["status"] != "revoked" and SECRET not in verified.text
    run = client.post(
        f"{API}/runs",
        headers=headers,
        json={"spec": _spec(), "budget_config": {"max_cost_usd": "2.50"}},
    )
    assert run.status_code in (200, 201), run.text
    run_id = run.json()["run_id"]
    diagnostics = client.get(f"{API}/runs/{run_id}/diagnostics", headers=headers)
    assert diagnostics.status_code == 200, diagnostics.text
    return {"run_id": run_id}


def _schema_rows(database: Any) -> dict[str, int]:
    """Row count of every table in the test schema (and so the table list)."""

    async def run(conn: Any) -> dict[str, int]:
        names = [
            r[0]
            for r in await conn.fetch(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = current_schema() AND table_type = 'BASE TABLE'"
            )
        ]
        counts = {}
        for name in names:
            counts[name] = await conn.fetchval(f'SELECT count(*) FROM "{name}"')
        return counts

    result: dict[str, int] = database.run(run)
    return result


# ===================================================================================== wiring


@pytest.mark.parametrize("source", ["argument", "environment"])
def test_postgres_mode_keeps_secrets_and_usage_in_postgres(
    service_database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    partial: None,
    source: str,
) -> None:
    from nexura_postgres import PostgresSecretStore, PostgresUsageLedger

    db = tmp_path / "svc.sqlite3"
    if source == "environment":
        monkeypatch.setenv("NEXURA_DATABASE_URL", service_database_url)
        app = create_api_app(db_path=db, profile="local-owner")
    else:
        monkeypatch.delenv("NEXURA_DATABASE_URL", raising=False)
        app = create_api_app(db_path=db, database_url=service_database_url, profile="local-owner")
    stores = app.state.service_stores
    assert isinstance(app.state.secret_store, PostgresSecretStore)
    assert app.state.secret_store.database is stores.database
    with TestClient(app) as client:
        made = _exercise(client, local_headers("default", "admin"))
        ledger = app_usage_ledger(app.state)
        assert isinstance(ledger, PostgresUsageLedger) and ledger.database is stores.database
        assert _budget_manager(app.state).ledger is ledger
        policy = ledger.budget_policy("run", made["run_id"])
        assert policy is not None
        assert Decimal(str(json.loads(policy)["config"]["max_cost_usd"])) == Decimal("2.50")
        rows = _schema_rows(stores.database)
        assert rows["nexura_secrets"] == 1 and rows["nexura_usage_budget_policies"] >= 1
    assert not (_sqlite_tables(db) & WP5_SQLITE_TABLES)  # nothing of WP5's reached SQLite


def test_sqlite_default_is_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NEXURA_DATABASE_URL", raising=False)
    monkeypatch.setenv("NEXURA_MASTER_KEY", generate_master_key())
    db = tmp_path / "svc.sqlite3"
    app = create_api_app(db_path=db, profile="local-owner")
    assert type(app.state.secret_store) is EncryptedSecretStore
    with TestClient(app) as client:
        _exercise(client, local_headers("default", "admin"))
        assert type(app_usage_ledger(app.state)) is UsageLedger
    assert _sqlite_tables(db) >= WP5_SQLITE_TABLES


def test_partial_mode_messages_name_what_is_still_pending(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(
        "NEXURA_ALLOW_PARTIAL_POSTGRES", raising=False
    )  # the refusal, not the dev switch
    for name in ("K_SERVICE", "CLOUD_RUN_JOB"):
        monkeypatch.delenv(name, raising=False)
    stores = ServiceStores(tmp_path / "x.sqlite3", "postgresql://fixture.example/db")
    # MVP WP8 ported evaluations, channels and integrations: Postgres mode needs no switch, and
    # nothing is pending. Only the two SQLite-only features are refused, by name.
    stores.require_service_support()
    assert not stores.partial and stores.storage_mode == "postgres"
    for feature, named in (
        ("repro_capture", "NEXURA_CAPTURE_FAILED_RUNS"),
        ("delivery_tokens", "serve --auth token"),
    ):
        with pytest.raises(ValueError) as refused:
            stores.require_sqlite_feature(feature)
        assert named in str(refused.value)
        assert "NEXURA_ALLOW_PARTIAL_POSTGRES=1" in str(refused.value)
        for ported in ("evaluations", "channels", "integrations", "knowledge", "WP8", "pending"):
            assert ported not in str(refused.value)
    assert not stores.partial and not (tmp_path / "x.sqlite3").exists()
    monkeypatch.setenv("NEXURA_ALLOW_PARTIAL_POSTGRES", "1")
    allowed = ServiceStores(tmp_path / "x.sqlite3", "postgresql://fixture.example/db")
    allowed.require_sqlite_feature("delivery_tokens")
    assert allowed.partial and allowed.storage_mode == "partial-postgres"
    warning = allowed.partial_warning()
    on_postgres, on_sqlite = warning.split("uses Postgres")
    assert "secrets" in on_postgres and "usage ledger" in on_postgres
    assert "secrets" not in on_sqlite and "usage" not in on_sqlite
    assert "organisations" in on_postgres and "publications" in on_postgres
    assert "organisations" not in on_sqlite and "publications" not in on_sqlite
    assert "knowledge index" in on_postgres and "knowledge" not in on_sqlite
    for ported in ("evaluations", "channels", "integrations"):
        assert ported in on_postgres and ported not in on_sqlite
    assert "delivery tokens" in on_sqlite


# ============================================================================ two instances


@pytest.fixture
def two_instances(
    service_database_url: str, tmp_path: Path, partial: None
) -> Iterator[tuple[TestClient, TestClient]]:
    def make(name: str) -> Any:
        return create_api_app(
            db_path=tmp_path / name / "svc.sqlite3",  # each instance has its own file
            database_url=service_database_url,
            blob_url=(tmp_path / "blobs").as_uri(),
            profile="local-owner",
        )

    with ExitStack() as stack:
        a = stack.enter_context(TestClient(make("a")))
        b = stack.enter_context(TestClient(make("b")))
        yield a, b
    for name in ("a", "b"):
        assert not (_sqlite_tables(tmp_path / name / "svc.sqlite3") & WP5_SQLITE_TABLES)


def test_two_instances_share_credentials_and_revocation(
    two_instances: tuple[TestClient, TestClient],
) -> None:
    a, b = two_instances
    h = local_headers("default", "admin")
    # Both instances are running before the credential exists (no startup warm-up helps).
    made = a.post(
        f"{API}/credentials",
        headers=h,
        json={"credential_id": "stripe", "credential_type": "api_key", "secret": SECRET},
    )
    assert made.status_code == 201, made.text
    seen = b.get(f"{API}/credentials/stripe", headers=h)
    assert seen.status_code == 200 and SECRET not in seen.text, seen.text
    assert "stripe" in [
        c["credential_id"] for c in b.get(f"{API}/credentials", headers=h).json()["items"]
    ]
    duplicate = b.post(
        f"{API}/credentials",
        headers=h,
        json={"credential_id": "stripe", "credential_type": "api_key", "secret": SECRET},
    )
    assert duplicate.status_code == 409, duplicate.text
    store_a = a.app.state.credential_store  # type: ignore[attr-defined]
    assert store_a.resolve("default", "stripe").reveal() == SECRET

    # Revoked through B: A refuses it at once, through the API and in runs.
    assert b.post(f"{API}/credentials/stripe/revoke", headers=h).status_code == 200
    assert a.post(f"{API}/credentials/stripe/verify", headers=h).json()["status"] == "revoked"
    with pytest.raises(CredentialAccessDeniedError):
        store_a.resolve("default", "stripe")
    assert store_a.for_workspace("default").resolve("stripe") is None

    # Deleted through A: gone on B.
    assert a.delete(f"{API}/credentials/stripe", headers=h).status_code in (200, 204)
    assert b.get(f"{API}/credentials/stripe", headers=h).status_code == 404
    assert b.delete(f"{API}/credentials/stripe", headers=h).status_code == 404


def test_two_instances_share_run_budgets_and_spend(
    two_instances: tuple[TestClient, TestClient],
) -> None:
    a, b = two_instances
    h = local_headers("default", "admin")
    run = a.post(
        f"{API}/runs", headers=h, json={"spec": _spec(), "budget_config": {"max_cost_usd": "0.50"}}
    )
    assert run.status_code in (200, 201), run.text
    run_id = run.json()["run_id"]
    budget_b = _budget_manager(b.app.state).get_run_budget(run_id)  # type: ignore[attr-defined]
    assert budget_b is not None and budget_b.config.max_cost_usd == Decimal("0.50")
    ledger_a = app_usage_ledger(a.app.state)  # type: ignore[attr-defined]
    ledger_b = app_usage_ledger(b.app.state)  # type: ignore[attr-defined]
    assert ledger_a is not ledger_b
    ledger_a.reserve(
        workspace_id="default",
        run_id=run_id,
        estimated_cost_usd=Decimal("0.40"),
        config=budget_b.config,
    )
    from nexura_agents.usage.errors import BudgetExceededError

    with pytest.raises(BudgetExceededError):
        ledger_b.reserve(
            workspace_id="default",
            run_id=run_id,
            estimated_cost_usd=Decimal("0.20"),
            config=budget_b.config,
        )
    assert ledger_b.get_status("default", run_id).total_reserved_usd == Decimal("0.40000000")


# ============================================================================= diagnostics


class _HeldLedgerLock:
    """Holds the usage ledger's global lock in another transaction until ``release``."""

    def __init__(self, database: Any) -> None:
        import asyncio
        from concurrent.futures import Future

        from nexura_postgres.usage import LEDGER_LOCK

        self.taken: Future[None] = Future()
        self.done: Future[None] = Future()

        async def hold(conn: Any) -> None:
            await conn.execute("SELECT pg_advisory_xact_lock($1)", LEDGER_LOCK)
            self.taken.set_result(None)
            await asyncio.wait_for(asyncio.wrap_future(self.done), 60)

        self.outer = database.submit(hold)
        self.taken.result(30)

    def release(self) -> None:
        self.done.set_result(None)
        self.outer.result(30)


@pytest.fixture
def pg_app(service_database_url: str, tmp_path: Path, partial: None) -> Iterator[TestClient]:
    app = create_api_app(
        db_path=tmp_path / "svc.sqlite3", database_url=service_database_url, profile="local-owner"
    )
    with TestClient(app) as client:
        yield client


def test_diagnostics_read_only_the_run_and_never_wait_for_the_budget_lock(
    pg_app: TestClient, service_database_url: str
) -> None:
    from nexura_postgres import Database

    h = local_headers("default", "admin")
    state = pg_app.app.state  # type: ignore[attr-defined]
    run = pg_app.post(f"{API}/runs", headers=h, json={"spec": _spec()})
    assert run.status_code in (200, 201), run.text
    run_id = run.json()["run_id"]
    # Spend of this run and of 200 other runs, written by another instance's ledger.
    from nexura_postgres import PostgresUsageLedger

    other_db = Database(service_database_url)
    try:
        other = PostgresUsageLedger(other_db)
        other.record_commit(
            workspace_id="default", run_id=run_id, amount_usd=Decimal("0.01"), tokens=3
        )
        for i in range(200):
            other.record_commit(
                workspace_id="default", run_id=f"other-{i}", amount_usd=Decimal("0")
            )
        held = _HeldLedgerLock(other_db)
        try:
            started = time.monotonic()
            response = pg_app.get(f"{API}/runs/{run_id}/diagnostics", headers=h)
            elapsed = time.monotonic() - started
        finally:
            held.release()
    finally:
        other_db.close()
    assert response.status_code == 200, response.text
    assert elapsed < 10, "diagnostics waited for the budget lock"
    ledger = state.app_usage_ledger
    # The app's one ledger answered with one filtered read: nothing else was loaded.
    assert ledger._seen == 0 and ledger._entries == []
    assert [e.run_id for e in ledger.get_entries(run_id=run_id)] == [run_id]


# ================================================================================== readyz


def test_postgres_readyz_healthy_and_writes_nothing(pg_app: TestClient) -> None:
    stores = pg_app.app.state.service_stores  # type: ignore[attr-defined]
    before = _schema_rows(stores.database)
    for _ in range(5):  # the local owner is an operator: no cache, every call probes
        response = pg_app.get("/readyz")
        assert response.status_code == 200, response.text
        probe = {p["name"]: p for p in response.json()["probes"]}["database"]
        assert probe["state"] == "healthy"
    assert _schema_rows(stores.database) == before  # no table and no row added
    assert "_nexura_health_ping" not in before


def test_postgres_readyz_reports_a_database_that_is_down(
    pg_app: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nexura_postgres import Database

    stores = pg_app.app.state.service_stores  # type: ignore[attr-defined]
    password = "wp5-never-shown-" + os.urandom(6).hex()
    # Nothing listens on port 1: connection refused, as for a stopped database.
    down = Database(f"postgresql://nexura:{password}@127.0.0.1:1/nexura")
    real = stores.database
    monkeypatch.setattr(stores, "database", down)
    try:
        response = pg_app.get("/readyz")
        public = TestClient(pg_app.app).get("/readyz", headers={"x-forwarded-for": "203.0.113.9"})
    finally:
        monkeypatch.setattr(stores, "database", real)
        down.close()
    assert response.status_code == 503, response.text
    probe = {p["name"]: p for p in response.json()["probes"]}["database"]
    assert probe["state"] == "unhealthy" and probe["error"]
    assert password not in response.text and password not in public.text
    assert pg_app.get("/readyz").status_code == 200  # back once the database is


def test_sqlite_readyz_healthy_unhealthy_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("NEXURA_DATABASE_URL", raising=False)
    data = tmp_path / "data"
    db = data / "svc.sqlite3"
    app = create_api_app(db_path=db, profile="local-owner")

    def snapshot() -> tuple[list[str], dict[str, int]]:
        with closing(sqlite3.connect(db)) as conn:
            names = sorted(r[0] for r in conn.execute("SELECT name FROM sqlite_master"))
            tables = [
                r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            ]
            return names, {
                t: conn.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0] for t in tables
            }

    with TestClient(app) as client:
        before = snapshot()
        for _ in range(5):  # the local owner is an operator: no cache, every call probes
            assert client.get("/readyz").status_code == 200
        assert snapshot() == before  # no table and no row added
        # The volume goes away (the app's open handles keep working, new opens fail).
        data.rename(tmp_path / "data-gone")
        try:
            down = client.get("/readyz")
        finally:
            (tmp_path / "data-gone").rename(data)
        assert down.status_code == 503, down.text
        probe = {p["name"]: p for p in down.json()["probes"]}["database"]
        assert probe["state"] == "unhealthy"
        assert client.get("/readyz").status_code == 200


def test_sqlite_probe_reads_the_file(tmp_path: Path) -> None:
    """``SELECT 1`` alone never reads the file; the probe must report a corrupt database."""
    from nexura_runtime.service.storage_backend import SQLiteProbe

    corrupt = tmp_path / "corrupt.sqlite3"
    corrupt.write_bytes(b"this is not a database" * 400)
    with pytest.raises(sqlite3.DatabaseError):
        SQLiteProbe(corrupt).ping()
    good = tmp_path / "good.sqlite3"
    with closing(sqlite3.connect(good)) as conn, conn:
        conn.execute("CREATE TABLE t (a)")
    assert SQLiteProbe(good).ping() is True
    assert SQLiteProbe(tmp_path / "missing.sqlite3").ping() is True  # historical: not created
    assert not (tmp_path / "missing.sqlite3").exists()


# ============================================================ fix round (review WP5 M1, M2)


def _race_requests(workers: list[Any]) -> list[Any]:
    import threading

    barrier = threading.Barrier(len(workers))
    results: list[Any] = [None] * len(workers)

    def run(index: int, work: Any) -> None:
        barrier.wait()
        results[index] = work()

    threads = [threading.Thread(target=run, args=(i, w)) for i, w in enumerate(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    return results


def test_same_credential_created_on_two_instances_at_once(
    two_instances: tuple[TestClient, TestClient],
) -> None:
    """M2: one 201 and the rest 409 ``CREDENTIAL_ALREADY_EXISTS``; the stored value is the
    winner's, never silently replaced by a loser's."""
    a, b = two_instances
    h = local_headers("default", "admin")
    store_a = a.app.state.credential_store  # type: ignore[attr-defined]
    for round_ in range(3):
        name = f"twin-{round_}"
        bodies = [
            {"credential_id": name, "credential_type": "api_key", "secret": f"{SECRET}-{i}"}
            for i in range(4)
        ]
        responses = _race_requests(
            [
                lambda c=c, j=j: c.post(f"{API}/credentials", headers=h, json=j)
                for c, j in zip((a, b, a, b), bodies, strict=True)
            ]
        )
        codes = sorted(r.status_code for r in responses)
        assert codes == [201, 409, 409, 409], [r.text for r in responses]
        assert all("CREDENTIAL_ALREADY_EXISTS" in r.text for r in responses if r.status_code == 409)
        winner = next(i for i, r in enumerate(responses) if r.status_code == 201)
        assert store_a.resolve("default", name).reveal() == f"{SECRET}-{winner}"
        assert all(SECRET not in r.text for r in responses)


def test_a_create_racing_a_create_on_the_other_instance_gets_409(
    two_instances: tuple[TestClient, TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    """M2, interleaved deterministically: A's route has checked that the name is free when B
    creates it; A's write must then be refused (409), and B's value stays."""
    from nexura_runtime.credentials.errors import CredentialNotFoundError

    a, b = two_instances
    h = local_headers("default", "admin")
    store_a = a.app.state.credential_store  # type: ignore[attr-defined]
    original = store_a.get_metadata
    fired: list[Any] = []

    def interleaved(workspace_id: str, credential_id: str) -> Any:
        try:
            return original(workspace_id, credential_id)
        except CredentialNotFoundError:
            if not fired:  # B creates the name between A's check and A's write
                fired.append(
                    b.post(
                        f"{API}/credentials",
                        headers=h,
                        json={
                            "credential_id": "dup",
                            "credential_type": "api_key",
                            "secret": f"{SECRET}-from-b",
                        },
                    )
                )
            raise

    monkeypatch.setattr(store_a, "get_metadata", interleaved)
    response = a.post(
        f"{API}/credentials",
        headers=h,
        json={"credential_id": "dup", "credential_type": "api_key", "secret": f"{SECRET}-a"},
    )
    assert fired and fired[0].status_code == 201, fired
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "CREDENTIAL_ALREADY_EXISTS"
    assert store_a.resolve("default", "dup").reveal() == f"{SECRET}-from-b"


def test_postgres_mode_without_a_master_key_is_refused(
    service_database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    partial: None,
) -> None:
    """L3, superseded by MVP WP9: Postgres mode without a master key used to start with
    credentials kept in one process (and a warning). A shared database cannot serve that, so
    every profile, local ones included, is now refused at startup, before any connection or
    file, and the insecure plaintext opt-out does not lift it."""
    from nexura_runtime.service.storage_backend import SharedKeyMissingError

    monkeypatch.delenv("NEXURA_MASTER_KEY")
    monkeypatch.setenv("NEXURA_INSECURE_ALLOW_PLAINTEXT_SECRETS", "I_UNDERSTAND_THIS_IS_UNSAFE")
    opened: list[str] = []
    monkeypatch.setattr(ServiceStores, "open_database", lambda self: opened.append("x"))
    with pytest.raises(SharedKeyMissingError, match="NEXURA_MASTER_KEY") as refused:
        create_api_app(
            db_path=tmp_path / "svc.sqlite3",
            database_url=service_database_url,
            profile="local-owner",
        )
    assert refused.value.missing == ["NEXURA_MASTER_KEY"]
    assert service_database_url not in str(refused.value)
    assert not opened and not list(tmp_path.iterdir())


def test_run_routes_wait_for_the_budget_lock_off_the_event_loop(pg_app: TestClient) -> None:
    """M3: the run routes read the run budget's status; with the ledger lock held by another
    transaction they wait in a worker thread, and the service keeps answering."""
    import threading

    h = local_headers("default", "admin")
    state = pg_app.app.state  # type: ignore[attr-defined]
    run = pg_app.post(
        f"{API}/runs", headers=h, json={"spec": _spec(), "budget_config": {"max_cost_usd": "1"}}
    )
    assert run.status_code in (200, 201), run.text
    run_id = run.json()["run_id"]
    calls = {
        "GET /runs/{id}": lambda: pg_app.get(f"{API}/runs/{run_id}", headers=h),
        "POST /runs": lambda: pg_app.post(
            f"{API}/runs", headers=h, json={"spec": _spec(), "budget_config": {"max_tokens": 9}}
        ),
    }
    for name, call in calls.items():
        held = _HeldLedgerLock(state.service_stores.database)
        outcome: list[Any] = []
        try:
            worker = threading.Thread(target=lambda c=call, o=outcome: o.append(c()))
            worker.start()
            worker.join(0.5)
            assert worker.is_alive() and not outcome, f"{name} did not wait for the lock"
            started = time.monotonic()
            assert pg_app.get("/healthz").status_code == 200
            assert time.monotonic() - started < 2, f"{name} blocked the event loop"
        finally:
            held.release()
        worker.join(30)
        assert outcome and outcome[0].status_code in (200, 201), name
        assert "budget_status" in outcome[0].json(), name
