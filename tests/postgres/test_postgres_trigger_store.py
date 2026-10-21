"""A6 S10b: PostgresTriggerStore specifics and the service's trigger queue on Postgres.

Needs ``NEXURA_TEST_POSTGRES_URI`` (a disposable database); each test works in its own
schema, dropped afterwards. The contract shared with the memory and SQLite stores is in
``tests/backend/test_trigger_store_contract.py``; this file covers what only Postgres has:
webhook secrets encrypted in the database, versioned migrations under concurrent first use,
closing and reopening the store's private loop, and ``create_api_app(workflow_state_url=...)``
end to end (webhook delivered, job queued in Postgres, run by a worker). Every handler is a
local fake.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from nexura_postgres import PostgresTriggerStore
from nexura_postgres.trigger_store import MIGRATIONS

from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.service.triggers.errors import TriggerError
from nexura_runtime.service.triggers.models import DurableTriggerJob, WebhookConfig
from nexura_runtime.service.triggers.secretbox import ENV_KEY
from nexura_runtime.service.triggers.signatures import compute_webhook_signature
from nexura_runtime.service.triggers.worker import TriggerWorker
from nexura_runtime.workflow import ModuleRegistry


async def _admin(uri: str, statement: str, *args: Any) -> Any:
    import asyncpg  # type: ignore[import-untyped]

    conn = await asyncpg.connect(uri)
    try:
        return await conn.fetch(statement, *args)
    finally:
        await conn.close()


class Schema:
    def __init__(self, uri: str) -> None:
        self.uri = uri
        self.name = f"trig_pg_{uuid.uuid4().hex[:12]}"
        self.dsn = f"{uri}{'&' if '?' in uri else '?'}search_path={self.name}"

    def fetch(self, statement: str, *args: Any) -> Any:
        return asyncio.run(_admin(self.dsn, statement, *args))


@pytest.fixture
def schema() -> Iterator[Schema]:
    uri = os.environ.get("NEXURA_TEST_POSTGRES_URI")
    if not uri:
        pytest.skip("NEXURA_TEST_POSTGRES_URI is not configured")
    made = Schema(uri)
    asyncio.run(_admin(uri, f'CREATE SCHEMA "{made.name}"'))
    try:
        yield made
    finally:
        asyncio.run(_admin(uri, f'DROP SCHEMA IF EXISTS "{made.name}" CASCADE'))


@pytest.fixture(autouse=True)
def _no_env_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_KEY, raising=False)


def _webhook(secret: str = "pg-secret-value") -> WebhookConfig:
    return WebhookConfig(
        endpoint_id="ep", workspace_id="ws", workflow_id="wf", release_id="current", secret=secret
    )


def test_constructor_validation() -> None:
    with pytest.raises(ValueError, match="DSN"):
        PostgresTriggerStore("")
    with pytest.raises(ValueError, match="max_pool_size"):
        PostgresTriggerStore("postgresql://h/db", max_pool_size=0)
    with pytest.raises(ValueError, match="call_timeout"):
        PostgresTriggerStore("postgresql://h/db", call_timeout=0)


def test_webhook_secrets_are_encrypted_in_the_database(schema: Schema, tmp_path: Path) -> None:
    key_file = tmp_path / "svc.sqlite3.trigger.key"
    store = PostgresTriggerStore(schema.dsn, secret_key_file=key_file)
    try:
        store.save_webhook_config(_webhook())
        rows = schema.fetch("SELECT secret FROM nexura_trigger_webhooks")
        assert len(rows) == 1
        assert rows[0]["secret"].startswith("enc:") and "pg-secret-value" not in rows[0]["secret"]
        assert key_file.stat().st_mode & 0o777 == 0o600
        # Another process with the same key reads it; one with another key cannot.
        same = PostgresTriggerStore(schema.dsn, secret_key_file=key_file)
        other = PostgresTriggerStore(schema.dsn, secret_key_file=tmp_path / "other.key")
        try:
            got = same.get_webhook_config("ep")
            assert got is not None and got.secret == "pg-secret-value"
            with pytest.raises(Exception):  # noqa: B017 - authentication failure of the cipher
                other.get_webhook_config("ep")
        finally:
            same.close()
            other.close()
    finally:
        store.close()


def test_the_environment_key_is_shared_across_hosts(
    schema: Schema, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV_KEY, "one key for every host")
    first, second = PostgresTriggerStore(schema.dsn), PostgresTriggerStore(schema.dsn)
    try:
        first.save_webhook_config(_webhook())
        got = second.get_webhook_config("ep")
        assert got is not None and got.secret == "pg-secret-value"
    finally:
        first.close()
        second.close()


def test_without_a_key_webhooks_are_refused_but_jobs_work(schema: Schema) -> None:
    store = PostgresTriggerStore(schema.dsn)
    try:
        with pytest.raises(TriggerError, match="NEXURA_TRIGGER_SECRET_KEY"):
            store.save_webhook_config(_webhook())
        assert store.get_webhook_config("ep") is None
        job = DurableTriggerJob(
            job_id="j1",
            workspace_id="ws",
            trigger_type="schedule",
            trigger_source_id="s",
            workflow_id="wf",
            release_id="current",
        )
        store.enqueue_trigger_job(job)
        assert store.claim_pending_job("j1", "w1") is not None
    finally:
        store.close()


def test_concurrent_first_use_migrates_once_and_newer_versions_are_refused(
    schema: Schema,
) -> None:
    stores = [PostgresTriggerStore(schema.dsn) for _ in range(4)]
    errors: list[BaseException] = []
    barrier = threading.Barrier(len(stores))

    def first_use(store: PostgresTriggerStore) -> None:
        try:
            barrier.wait()
            store.init_db()
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    threads = [threading.Thread(target=first_use, args=(s,)) for s in stores]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    try:
        assert not errors, errors
        versions = schema.fetch("SELECT version FROM nexura_trigger_migrations ORDER BY version")
        assert [r["version"] for r in versions] == [v for v, _, _ in MIGRATIONS]
        stores[0].init_db()  # again: a no-op
        schema.fetch(
            "INSERT INTO nexura_trigger_migrations (version, name, applied_at) "
            "VALUES (999, 'from the future', 0)"
        )
        newer = PostgresTriggerStore(schema.dsn)
        try:
            with pytest.raises(TriggerError, match="version 999"):
                newer.list_pending_jobs()
        finally:
            newer.close()
    finally:
        for store in stores:
            store.close()


def test_closed_store_reopens_and_works_from_async_code(schema: Schema) -> None:
    store = PostgresTriggerStore(schema.dsn)
    try:
        assert store.list_pending_jobs() == []
        store.close()
        store.close()  # idempotent
        assert store.list_schedules() == []  # reopened on use

        async def from_a_running_loop() -> int:
            # The worker calls the synchronous store from its own event loop.
            return len(store.list_pending_jobs())

        assert asyncio.run(from_a_running_loop()) == 0
    finally:
        store.close()


def test_leases_use_the_database_clock(schema: Schema) -> None:
    store = PostgresTriggerStore(schema.dsn)
    try:
        store.enqueue_trigger_job(
            DurableTriggerJob(
                job_id="j1",
                workspace_id="ws",
                trigger_type="schedule",
                trigger_source_id="s",
                workflow_id="wf",
                release_id="current",
            )
        )
        claimed = store.claim_pending_job("j1", "w1", lease_seconds=30)
        db_now = schema.fetch("SELECT EXTRACT(EPOCH FROM clock_timestamp())::float8 AS now")
        assert claimed is not None and claimed.claimed_at is not None
        assert 0 <= db_now[0]["now"] - claimed.claimed_at < 5
        assert claimed.lease_expires_at == pytest.approx(claimed.claimed_at + 30)
    finally:
        store.close()


# ------------------------------------------------------------------ the service


def _registry(applied: list[str]) -> ModuleRegistry:
    registry = ModuleRegistry()

    def echo(state: dict[str, Any]) -> dict[str, Any]:
        applied.append(str(state.get("msg")))
        return {"echoed": state.get("msg")}

    registry.register("echo", echo, effect="pure")
    return registry


_SPEC = {
    "name": "pg-trigger",
    "entry_node": "n1",
    "terminal_nodes": ["n1"],
    "nodes": {"n1": {"id": "n1", "handler": "echo"}},
}


async def test_service_keeps_its_trigger_queue_in_postgres(schema: Schema, tmp_path: Path) -> None:
    db = tmp_path / "svc.sqlite3"
    applied: list[str] = []
    app = create_api_app(
        registry=_registry(applied),
        db_path=db,
        profile="local-owner",
        workflow_state_url=schema.dsn,
    )
    store = app.state.trigger_store
    assert isinstance(store, PostgresTriggerStore)
    assert app.state.trigger_manager.store is store
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            headers={"X-Workspace-ID": "default"},
        ) as client:
            created = await client.post("/api/v1/workflows", json={"spec": _SPEC})
            assert created.status_code in (200, 201), created.text
            wf_id = created.json()["workflow_id"]
            registered = await client.post(
                "/api/v1/triggers/webhooks",
                json={"endpoint_id": "orders", "workflow_id": wf_id, "secret": "s3cret"},
            )
            assert registered.status_code == 200, registered.text
            payload = {"msg": "hello from postgres"}
            ts = time.time()
            for _ in range(2):  # the second delivery of the same event is de-duplicated
                delivered = await client.post(
                    "/api/v1/triggers/webhooks/orders/deliver",
                    content=json.dumps(payload),
                    headers={
                        "Content-Type": "application/json",
                        "X-Webhook-Signature": compute_webhook_signature(
                            "s3cret", ts, payload, event_id="evt-1"
                        ),
                        "X-Webhook-Timestamp": str(ts),
                        "X-Webhook-Event-ID": "evt-1",
                    },
                )
                assert delivered.status_code == 200, delivered.text
            listed = await client.get("/api/v1/triggers/jobs")
            assert listed.status_code == 200, listed.text

        outcomes = await TriggerWorker(app.state).run_once()
        assert [o.status for o in outcomes] == ["completed"]
        assert applied == ["hello from postgres"]
        run = await app.state.store.get_run(outcomes[0].run_id)
        assert run is not None and run["status"] == "completed"
    finally:
        await app.state.store.close()
        store.close()

    jobs = await _admin(schema.dsn, "SELECT status, run_id, attempts FROM nexura_trigger_jobs")
    assert [(r["status"], r["run_id"], r["attempts"]) for r in jobs] == [
        ("completed", outcomes[0].run_id, 1)
    ]
    receipts = await _admin(schema.dsn, "SELECT endpoint_id, event_id FROM nexura_trigger_receipts")
    assert [(r["endpoint_id"], r["event_id"]) for r in receipts] == [("orders", "evt-1")]
    secrets = await _admin(schema.dsn, "SELECT secret FROM nexura_trigger_webhooks")
    assert secrets[0]["secret"].startswith("enc:")
    with sqlite3.connect(db) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
    assert "durable_trigger_jobs" not in tables  # the queue is not in SQLite
    # Webhook secrets use the key file of the service's SQLite database.
    assert Path(f"{db}.trigger.key").exists()
