# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""MVP Postgres WP8: the last stores in the service, full Postgres mode, and two instances.

* A Postgres ``database_url`` now runs the whole service without the partial-mode switch and
  without any SQLite file: a full lifecycle (workflow, run, approval, chat, knowledge,
  evaluations, Studio evals, channel delivery, integration registration) runs with
  ``sqlite3.connect`` forbidden for every file, and nothing is created on disk but the blob
  folder.
* Two ``create_api_app`` instances on one schema (each with its own registry and its own,
  never-created SQLite path): a channel dedup key delivered through one is a duplicate through
  the other; integration registrations made through A are restored into B's registry when B
  restarts (not before: the registry is per process), and an id claimed through A is refused
  through B at once; evaluation and Studio eval runs list in the same stable order on both.
* Routes that wait on a lock of these stores keep the event loop free.
* The two SQLite-only features are refused in Postgres mode unless the development switch is
  set; the knowledge index build lock is per app (WP7 review R2-L2).

Postgres tests skip without ``NEXURA_TEST_POSTGRES_URI``; every model is a local fake.
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
import threading
import time
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

import httpx
import pytest
from _local_env import local_headers
from nexura_agents import FakeModel

from nexura_runtime.modules.registry import VersionedModuleRegistry
from nexura_runtime.secrets.keys import generate_master_key
from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.studio.builtin_modules import get_default_registry
from nexura_runtime.workflow import ModuleRegistry, WorkflowEngine, WorkflowSpec

API = "/api/v1"
H = local_headers("default", "admin")
GATED = {
    "name": "wp8",
    "entry_node": "lookup",
    "nodes": {
        "lookup": {
            "id": "lookup",
            "handler": "synthetic_order_lookup",
            "default_transition": "ticket",
        },
        "ticket": {
            "id": "ticket",
            "handler": "synthetic_ticket_creator",
            "requires_approval": True,
        },
    },
    "terminal_nodes": ["ticket"],
}


@pytest.fixture
def full_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Full Postgres mode: no development switch, keys from the environment, no Cloud Run."""
    monkeypatch.delenv("NEXURA_ALLOW_PARTIAL_POSTGRES", raising=False)
    monkeypatch.setenv("NEXURA_MASTER_KEY", generate_master_key())
    monkeypatch.setenv("NEXURA_TRIGGER_SECRET_KEY", "wp8-test-trigger-key")
    for name in ("K_SERVICE", "CLOUD_RUN_JOB", "NEXURA_WORKFLOW_STATE_URL", "NEXURA_BLOB_URL"):
        monkeypatch.delenv(name, raising=False)


def _registry() -> ModuleRegistry:
    """The default handlers on a versioned registry of this instance alone (another process)."""
    registry = ModuleRegistry(versioned_registry=VersionedModuleRegistry())
    registry.include(get_default_registry())
    return registry


def _app(tmp_path: Path, name: str, url: str, **kw: Any) -> Any:
    kw.setdefault("registry", _registry())
    return create_api_app(
        db_path=tmp_path / name / "svc.sqlite3",  # never created in full Postgres mode
        database_url=url,
        blob_url=(tmp_path / "blobs").as_uri(),  # one database, one blob store
        profile="local-owner",
        **kw,
    )


async def _client(stack: AsyncExitStack, app: Any) -> httpx.AsyncClient:
    await stack.enter_async_context(app.router.lifespan_context(app))
    return await stack.enter_async_context(
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1")
    )


@pytest.fixture
async def two(
    service_database_url: str, tmp_path: Path, full_mode: None
) -> AsyncIterator[tuple[Any, Any, httpx.AsyncClient, httpx.AsyncClient]]:
    apps = [_app(tmp_path, name, service_database_url) for name in ("a", "b")]
    async with AsyncExitStack() as stack:
        a, b = [await _client(stack, app) for app in apps]
        yield apps[0], apps[1], a, b
    assert not (tmp_path / "a").exists() and not (tmp_path / "b").exists()


# ============================================================================ full lifecycle


async def test_full_postgres_lifecycle_creates_no_sqlite_file(
    service_database_url: str,
    tmp_path: Path,
    full_mode: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[str] = []
    original = sqlite3.connect

    def guarded(path: Any, *args: Any, **kwargs: Any) -> Any:
        if str(path) != ":memory:":
            opened.append(str(path))  # recorded too, in case a caller swallows the error
            raise AssertionError(f"File SQLite opened in full Postgres mode: {path}")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", guarded)
    app = _app(
        tmp_path,
        "svc",
        service_database_url,
        chat_models={"local": FakeModel(["hello there"], repeat_last=True, stream=True)},
    )
    assert app.state.service_stores.storage_mode == "postgres"
    # Built (and migrated) when the app is created, never by a first request on the loop.
    assert type(app.state.studio_eval_store).__name__ == "PostgresStudioEvalStore"
    async with AsyncExitStack() as stack:
        c = await _client(stack, app)
        # Workflow, run, approval.
        made = await c.post(
            f"{API}/workflows", json={"workflow_id": "wf", "spec": GATED}, headers=H
        )
        assert made.status_code == 201, made.text
        run = await c.post(
            f"{API}/runs",
            json={"workflow_id": "wf", "initial_state": {"order_id": "ORD-DELHI-501"}},
            headers=H,
        )
        assert run.status_code == 201, run.text
        body = run.json()
        assert body["status"] == "pending_approval", body
        decided = await c.post(
            f"{API}/runs/{body['run_id']}/decide",
            json={"approval_id": body["pending_approval"]["approval_id"], "decision": "approve"},
            headers=H,
        )
        assert decided.status_code == 200, decided.text
        final = (await c.get(f"{API}/runs/{body['run_id']}", headers=H)).json()
        assert final["status"] == "completed"
        # Chat and knowledge.
        chat = await c.post(
            f"{API}/sessions/s1/chat/stream", json={"message": "hi", "model": "local"}, headers=H
        )
        assert chat.status_code == 200 and "chat_completed" in chat.text
        doc = await c.post(
            f"{API}/knowledge/documents/upload",
            json={"filename": "n.md", "content": "Herons wade in marshes."},
            headers=H,
        )
        assert doc.status_code == 201, doc.text
        hits = await c.post(f"{API}/knowledge/retrieve", json={"query": "herons"}, headers=H)
        assert hits.json()["citations"]
        # Evaluations and Studio evals.
        trace = {"run_id": "imported", "timestamp": 1.0, "output": "x" * 20_000}
        assert (await c.post(f"{API}/evaluations/traces", json=trace, headers=H)).status_code == 200
        online = await c.post(
            f"{API}/evaluations/online",
            json={"example": {"input_data": {}, "expected_output": "unused"}},
            headers=H,
        )
        assert online.status_code == 200, online.text
        dataset = {
            "dataset_id": "qa",
            "examples": [{"input_data": {"q": "x"}, "expected_output": "y"}],
        }
        assert (
            await c.post(f"{API}/evaluations/datasets", json=dataset, headers=H)
        ).status_code == 200
        evaluated = await c.post(
            f"{API}/evaluations/run",
            json={"dataset_id": "benchmark_customer_support", "graders": ["exact_match"]},
            headers=H,
        )
        assert evaluated.status_code == 200, evaluated.text
        runs = (await c.get(f"{API}/evaluations/runs", headers=H)).json()["runs"]
        assert {r["run_id"] for r in runs} == {online.json()["run_id"], evaluated.json()["run_id"]}
        studio = await c.put(
            f"{API}/evaluations/studio/datasets/ds1",
            json={"name": "D", "cases": [{"name": "c", "input": "hi", "expected_output": "hi"}]},
            headers=H,
        )
        assert studio.status_code == 200, studio.text
        studio_run = await c.post(
            f"{API}/evaluations/studio/runs",
            json={"dataset_id": "ds1", "target": {"kind": "agent", "id": "local-fake"}},
            headers=H,
        )
        assert studio_run.status_code == 200, studio_run.text
        # Channel delivery (dry-run outbox) with a dedup key.
        send = {
            "channel": "email",
            "recipient": "a@example.com",
            "subject_or_template": "s",
            "body": "b",
            "dedup_key": "order-1",
        }
        first = await c.post(f"{API}/triggers/outbox/test-dispatch", json=send, headers=H)
        again = await c.post(f"{API}/triggers/outbox/test-dispatch", json=send, headers=H)
        assert (first.json()["is_duplicate"], again.json()["is_duplicate"]) == (False, True)
        outbox = (await c.get(f"{API}/triggers/outbox", headers=H)).json()["outbox"]
        assert [r["status"] for r in outbox] == ["dry_run", "deduplicated"]
        # Integration registration.
        reg = await c.post(
            f"{API}/integrations/register",
            json={"kind": "mcp", "server_name_or_prefix": "calc", "fixture_id": "math_fixture"},
            headers=H,
        )
        assert reg.status_code == 200 and "mcp.calc.math_add" in reg.json()["registered_modules"]
        ready = (await c.get("/readyz")).json()
        assert ready["status"] == "healthy" and ready["storage"] == "postgres"
    assert opened == []
    assert sorted(p.name for p in tmp_path.iterdir()) == ["blobs"]  # no SQLite file anywhere
    stores = app.state
    for store in (
        stores.eval_store,
        stores.studio_eval_store,
        stores.channel_store,
        stores.integration_registrations,
    ):
        assert store.database is stores.service_stores.database or store.database is not None
        assert type(store).__module__.startswith("nexura_postgres.")


def test_sqlite_only_features_are_refused_in_postgres_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, full_mode: None
) -> None:
    from nexura_runtime.service.api.operator_token import (
        OperatorTokenAuthProvider,
        generate_operator_token,
    )
    from nexura_runtime.service.storage_backend import ServiceStores

    def never(self: Any) -> Any:
        raise AssertionError("refused before any connection")

    monkeypatch.setattr(ServiceStores, "open_database", never)
    url = "postgresql://fixture.example/db?sslmode=require"
    with pytest.raises(ValueError, match="NEXURA_CAPTURE_FAILED_RUNS"):
        create_api_app(db_path=tmp_path / "s.sqlite3", database_url=url, capture_failed_runs=True)
    with pytest.raises(ValueError, match="serve --auth token"):
        create_api_app(
            db_path=tmp_path / "s.sqlite3",
            database_url=url,
            auth_provider=OperatorTokenAuthProvider(generate_operator_token()),
        )
    assert not list(tmp_path.iterdir())


# ============================================================================ two instances


async def test_a_dedup_key_delivered_on_one_instance_is_a_duplicate_on_the_other(
    two: tuple[Any, Any, httpx.AsyncClient, httpx.AsyncClient],
) -> None:
    _, _, a, b = two
    for round_no in range(3):
        send = {
            "channel": "email",
            "recipient": "a@example.com",
            "subject_or_template": "s",
            "body": "b",
            "dedup_key": f"order-{round_no}",
        }
        url = f"{API}/triggers/outbox/test-dispatch"
        results = await asyncio.gather(
            *[(a, b)[i % 2].post(url, json=send, headers=H) for i in range(8)]
        )
        assert all(r.status_code == 200 for r in results), [r.text for r in results]
        duplicates = sorted(r.json()["is_duplicate"] for r in results)
        assert duplicates == [False] + [True] * 7
    for client in (a, b):
        outbox = (await client.get(f"{API}/triggers/outbox", headers=H)).json()["outbox"]
        assert len(outbox) == 24  # the receipts are shared too
        assert sorted(r["status"] for r in outbox).count("dry_run") == 3


async def test_registrations_are_restored_on_another_instance_after_it_restarts(
    two: tuple[Any, Any, httpx.AsyncClient, httpx.AsyncClient],
    service_database_url: str,
    tmp_path: Path,
) -> None:
    app_a, app_b, a, b = two
    alpha = local_headers("ws-alpha", "admin")
    beta = local_headers("ws-beta", "admin")
    mcp = await a.post(
        f"{API}/integrations/register",
        json={"kind": "mcp", "server_name_or_prefix": "calc", "fixture_id": "math_fixture"},
        headers=alpha,
    )
    assert mcp.status_code == 200, mcp.text
    api = await a.post(
        f"{API}/integrations/register",
        json={"kind": "openapi", "server_name_or_prefix": "pets", "fixture_id": "petstore"},
        headers=alpha,
    )
    assert api.status_code == 200, api.text
    ids = mcp.json()["registered_modules"] + api.json()["registered_modules"]
    assert "mcp.calc.math_add" in ids and len(api.json()["registered_modules"]) >= 1
    # B runs in another process: its registry does not see A's registration until it restarts.
    assert not any(app_b.state.registry.has_handler(mid) for mid in ids)
    # Ownership is shared at once: B refuses the same ids for another workspace.
    taken = await b.post(
        f"{API}/integrations/register",
        json={"kind": "mcp", "server_name_or_prefix": "calc", "fixture_id": "math_fixture"},
        headers=beta,
    )
    assert taken.status_code == 403 and "CROSS_WORKSPACE_DENIED" in taken.text
    # B restarts (a new instance on the same database): every registration is restored.
    restarted = _app(tmp_path, "b2", service_database_url)
    async with AsyncExitStack() as stack:
        b2 = await _client(stack, restarted)
        for mid in ids:
            module = restarted.state.registry.get(mid)
            assert getattr(module, "_workspace_id", None) == "ws-alpha"
        listed = (await b2.get(f"{API}/modules", headers=alpha)).json()["items"]
        assert set(ids) <= {m["module_id"] for m in listed}
        hidden = (await b2.get(f"{API}/modules", headers=beta)).json()["items"]
        assert not set(ids) & {m["module_id"] for m in hidden}
        spec = WorkflowSpec.from_dict(
            {
                "name": "mcp",
                "entry_node": "add",
                "nodes": {
                    "add": {
                        "id": "add",
                        "handler": "mcp.calc.math_add",
                        "input_mapping": {"x": "n1", "y": "n2"},
                        "output_mapping": {"result": "sum"},
                    }
                },
                "terminal_nodes": ["add"],
            }
        )
        result = await WorkflowEngine(
            spec=spec, registry=restarted.state.registry, workspace_id="ws-alpha"
        ).run(initial_state={"n1": 15, "n2": 27})
        assert result.status == "completed" and "42" in str(result.state.get("sum"))
    assert not (tmp_path / "b2").exists()


async def test_one_tool_id_raced_on_two_instances_is_registered_once(
    two: tuple[Any, Any, httpx.AsyncClient, httpx.AsyncClient],
) -> None:
    app_a, app_b, a, b = two
    body = {"kind": "mcp", "fixture_id": "math_fixture"}
    for round_no in range(3):
        prefix = f"race{round_no}"
        results = await asyncio.gather(
            a.post(
                f"{API}/integrations/register",
                json={**body, "server_name_or_prefix": prefix},
                headers=local_headers(f"w{round_no}-a", "admin"),
            ),
            b.post(
                f"{API}/integrations/register",
                json={**body, "server_name_or_prefix": prefix},
                headers=local_headers(f"w{round_no}-b", "admin"),
            ),
        )
        codes = sorted(r.status_code for r in results)
        assert codes[0] == 200 and codes[1] in (403, 409), [r.text for r in results]
        rows = [r for r in app_a.state.integration_registrations.list() if prefix in r[1]]
        assert len({row[0] for row in rows}) == 1  # one workspace holds every id


async def test_a_registration_raced_past_the_in_memory_checks_is_stored_once(
    two: tuple[Any, Any, httpx.AsyncClient, httpx.AsyncClient],
) -> None:
    """Both instances pass their in-memory checks before either stores: the shared store decides,
    the loser gets 409 and registers nothing in its own registry."""
    app_a, app_b, a, b = two
    for round_no in range(3):
        barrier = threading.Barrier(2, timeout=10)
        originals = {}
        for app in (app_a, app_b):
            store = app.state.integration_registrations
            originals[app] = store.save

            def gated(*args: Any, _save: Any = store.save, _gate: Any = barrier) -> Any:
                _gate.wait()  # both requests reached the save (in worker threads)
                return _save(*args)

            store.save = gated
        prefix = f"gate{round_no}"
        try:
            results = await asyncio.gather(
                *[
                    client.post(
                        f"{API}/integrations/register",
                        json={
                            "kind": "mcp",
                            "fixture_id": "math_fixture",
                            "server_name_or_prefix": prefix,
                        },
                        headers=local_headers(f"g{round_no}-{name}", "admin"),
                    )
                    for client, name in ((a, "a"), (b, "b"))
                ]
            )
        finally:
            for app, save in originals.items():
                app.state.integration_registrations.save = save
        codes = [r.status_code for r in results]
        assert sorted(codes) == [200, 409], [r.text for r in results]
        loser = (app_a, app_b)[codes.index(409)]
        detail = results[codes.index(409)].json()["detail"]
        assert detail["code"] == "INTEGRATION_TOOL_ID_CONFLICT"
        assert not loser.state.registry.has_handler(f"mcp.{prefix}.math_add")
        rows = [r for r in app_a.state.integration_registrations.list() if f".{prefix}." in r[1]]
        assert {row[0] for row in rows} == {f"g{round_no}-" + ("b" if loser is app_a else "a")}


async def test_a_storage_error_fails_the_registration_and_registers_nothing(
    two: tuple[Any, Any, httpx.AsyncClient, httpx.AsyncClient],
) -> None:
    app_a, _, a, _ = two
    store = app_a.state.integration_registrations

    def broken(*args: Any) -> None:
        raise RuntimeError("fixture storage failure")

    store.save = broken
    with pytest.raises(RuntimeError, match="fixture storage failure"):
        await a.post(
            f"{API}/integrations/register",
            json={"kind": "mcp", "server_name_or_prefix": "broken", "fixture_id": "math_fixture"},
            headers=H,
        )
    del store.save
    assert not app_a.state.registry.has_handler("mcp.broken.math_add")
    assert (
        app_a.state.workspace_repo.get_resource_workspace("integration_tool", "mcp.broken.math_add")
        is None
    )
    assert not [r for r in store.list() if ".broken." in r[1]]


async def test_eval_run_lists_are_stable_across_instances(
    two: tuple[Any, Any, httpx.AsyncClient, httpx.AsyncClient],
) -> None:
    _, _, a, b = two
    made = []
    for i in range(6):
        res = await (a, b)[i % 2].post(
            f"{API}/evaluations/run",
            json={"dataset_id": "benchmark_customer_support", "graders": ["exact_match"]},
            headers=H,
        )
        assert res.status_code == 200, res.text
        made.append(res.json()["run_id"])
    for client in (a, b):
        listed = (await client.get(f"{API}/evaluations/runs", headers=H)).json()["runs"]
        assert [r["run_id"] for r in listed] == list(reversed(made))
    case = {"name": "c", "input": "hi", "expected_output": "hi"}
    put = await a.put(
        f"{API}/evaluations/studio/datasets/ds", json={"name": "D", "cases": [case]}, headers=H
    )
    assert put.status_code == 200, put.text
    studio_made = []
    for i in range(6):
        res = await (b, a)[i % 2].post(
            f"{API}/evaluations/studio/runs",
            json={"dataset_id": "ds", "target": {"kind": "agent", "id": "local-fake"}},
            headers=H,
        )
        assert res.status_code == 200, res.text
        studio_made.append(res.json()["run_id"])
    for client in (a, b):
        listed = (await client.get(f"{API}/evaluations/studio/runs", headers=H)).json()["runs"]
        assert [r["run_id"] for r in listed] == list(reversed(studio_made))
    # A revision written through B is the base a stale save through A conflicts with.
    edit = {"name": "D", "cases": [case], "expected_revision": 1}
    assert (
        await b.put(f"{API}/evaluations/studio/datasets/ds", json=edit, headers=H)
    ).status_code == 200
    stale = await a.put(f"{API}/evaluations/studio/datasets/ds", json=edit, headers=H)
    assert stale.status_code == 409, stale.text


# ===================================================================== event loop stays free


class _Holder:
    """Holds one transaction (and the locks it took) on a pool of its own until released."""

    def __init__(self, url: str, statements: list[tuple[str, tuple[Any, ...]]]) -> None:
        from nexura_postgres import Database

        self.database = Database(url)
        self.taken = threading.Event()
        self.release = threading.Event()

        async def hold(conn: Any) -> None:
            for sql, args in statements:
                await conn.execute(sql, *args)
            self.taken.set()
            await asyncio.to_thread(self.release.wait, 30)
            raise RuntimeError("rolled back on purpose")

        self.future = self.database.submit(hold)
        assert self.taken.wait(10)

    def close(self) -> None:
        self.release.set()
        with contextlib.suppress(RuntimeError):  # the deliberate rollback
            self.future.result(30)
        self.database.close()


async def _waits_but_loop_is_free(
    client: httpx.AsyncClient, holder: _Holder, call: Callable[[], Any]
) -> Any:
    pending = asyncio.ensure_future(call())
    started = time.monotonic()
    try:
        await asyncio.sleep(0.4)
        assert not pending.done()  # waiting for the held lock
        assert (await client.get("/healthz")).status_code == 200
        # Timed from before the sleep: a call blocking the loop delays the sleep too.
        assert time.monotonic() - started < 2.0, "the event loop was blocked"
    finally:
        holder.close()
    return await pending


async def test_routes_waiting_on_a_lock_keep_the_event_loop_free(
    two: tuple[Any, Any, httpx.AsyncClient, httpx.AsyncClient], service_database_url: str
) -> None:
    from nexura_postgres.evaluations import DATASET_LOCK_SQL, RUNS_LOCK_SQL
    from nexura_postgres.integrations import TOOL_LOCK_SQL

    _, _, a, _ = two
    case = {"name": "c", "input": "hi", "expected_output": "hi"}
    put = _Holder(service_database_url, [(DATASET_LOCK_SQL, ("default", "held"))])
    saved = await _waits_but_loop_is_free(
        a,
        put,
        lambda: a.put(
            f"{API}/evaluations/studio/datasets/held",
            json={"name": "D", "cases": [case]},
            headers=H,
        ),
    )
    assert saved.status_code == 200, saved.text
    run = _Holder(service_database_url, [(RUNS_LOCK_SQL, ("default",))])
    ran = await _waits_but_loop_is_free(
        a,
        run,
        lambda: a.post(
            f"{API}/evaluations/studio/runs",
            json={"dataset_id": "held", "target": {"kind": "agent", "id": "local-fake"}},
            headers=H,
        ),
    )
    assert ran.status_code == 200, ran.text
    # A dedup key inserted by an uncommitted transaction elsewhere: the insert waits for it.
    key = "outbox:7:default:held"
    dedup = _Holder(
        service_database_url,
        [("INSERT INTO channel_dedup (key, processed_at, status) VALUES ($1, 0, 'x')", (key,))],
    )
    send = {
        "channel": "email",
        "recipient": "a@example.com",
        "subject_or_template": "s",
        "body": "b",
        "dedup_key": "held",
    }
    sent = await _waits_but_loop_is_free(
        a, dedup, lambda: a.post(f"{API}/triggers/outbox/test-dispatch", json=send, headers=H)
    )
    assert sent.json()["is_duplicate"] is False  # the holder rolled back: this one recorded it
    tool = _Holder(service_database_url, [(TOOL_LOCK_SQL, ("mcp.held.math_add",))])
    registered = await _waits_but_loop_is_free(
        a,
        tool,
        lambda: a.post(
            f"{API}/integrations/register",
            json={"kind": "mcp", "server_name_or_prefix": "held", "fixture_id": "math_fixture"},
            headers=H,
        ),
    )
    assert registered.status_code == 200, registered.text


# ============================================================ knowledge build lock (R2-L2)


def test_the_knowledge_build_lock_is_per_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from starlette.requests import Request

    from nexura_runtime.service.api import knowledge

    monkeypatch.delenv("NEXURA_DATABASE_URL", raising=False)
    apps = [create_api_app(db_path=tmp_path / f"{n}.sqlite3") for n in ("one", "two")]
    for app in apps:
        app.state.hybrid_knowledge_index = None
    assert apps[0].state.knowledge_build_lock is not apps[1].state.knowledge_build_lock

    def request(app: Any) -> Request:
        return Request({"type": "http", "app": app, "headers": []})

    def build_in_thread(app: Any) -> threading.Thread:
        thread = threading.Thread(target=knowledge._index, args=(request(app),), daemon=True)
        thread.start()
        return thread

    # Another app building (its lock held) does not make this app wait.
    with apps[0].state.knowledge_build_lock:
        thread = build_in_thread(apps[1])
        thread.join(5)
        assert not thread.is_alive()
    assert apps[1].state.hybrid_knowledge_index is not None
    # The app's own lock does: a concurrent build waits for it, and the index is built once.
    calls: list[int] = []
    stores = apps[0].state.service_stores
    real = stores.knowledge_index

    def counted(*args: Any) -> Any:
        calls.append(1)
        time.sleep(0.2)
        return real(*args)

    monkeypatch.setattr(stores, "knowledge_index", counted)
    with apps[0].state.knowledge_build_lock:
        threads = [build_in_thread(apps[0]) for _ in range(6)]
        time.sleep(0.3)
        assert all(t.is_alive() for t in threads) and calls == []
    for thread in threads:
        thread.join(10)
    assert calls == [1] and apps[0].state.hybrid_knowledge_index is not None


def test_sqlite_default_keeps_the_wp8_stores_in_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient

    monkeypatch.delenv("NEXURA_DATABASE_URL", raising=False)
    db = tmp_path / "svc.sqlite3"
    app = create_api_app(db_path=db, profile="local-owner")
    assert app.state.service_stores.storage_mode is None
    assert type(app.state.eval_store).__name__ == "SqliteEvaluationStore"
    assert type(app.state.channel_store).__name__ == "SqliteChannelStore"
    assert type(app.state.integration_registrations).__name__ == "SQLiteIntegrationRegistrations"
    with TestClient(app, base_url="http://127.0.0.1") as c:
        send = {
            "channel": "email",
            "recipient": "a@example.com",
            "subject_or_template": "s",
            "body": "b",
            "dedup_key": "k",
        }
        assert (
            c.post(f"{API}/triggers/outbox/test-dispatch", json=send, headers=H).status_code == 200
        )
        case = {"name": "c", "input": "hi", "expected_output": "hi"}
        put = c.put(
            f"{API}/evaluations/studio/datasets/d", json={"name": "D", "cases": [case]}, headers=H
        )
        assert put.status_code == 200, put.text
        assert "storage" not in c.get("/readyz").json()
    assert type(app.state.studio_eval_store).__name__ == "StudioEvalStore"
    with sqlite3.connect(db) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"eval_traces", "studio_eval_runs", "channel_dedup", "channel_outbox"} <= tables


async def test_a_restored_mcp_tool_runs_after_a_restart_on_sqlite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Restored MCP tools get the fixture's own definition (MVP WP8), so they run; before, a
    stand-in definition was restored and the manifest-digest check refused the first call."""
    monkeypatch.delenv("NEXURA_DATABASE_URL", raising=False)
    db = tmp_path / "svc.sqlite3"
    alpha = local_headers("ws-alpha", "admin")
    first = create_api_app(db_path=db, registry=_registry(), profile="local-owner")
    async with AsyncExitStack() as stack:
        c = await _client(stack, first)
        reg = await c.post(
            f"{API}/integrations/register",
            json={"kind": "mcp", "server_name_or_prefix": "calc", "fixture_id": "math_fixture"},
            headers=alpha,
        )
        assert reg.status_code == 200, reg.text
    restarted = create_api_app(db_path=db, registry=_registry(), profile="local-owner")
    spec = WorkflowSpec.from_dict(
        {
            "name": "mcp",
            "entry_node": "add",
            "nodes": {
                "add": {
                    "id": "add",
                    "handler": "mcp.calc.math_add",
                    "input_mapping": {"x": "n1", "y": "n2"},
                    "output_mapping": {"result": "sum"},
                }
            },
            "terminal_nodes": ["add"],
        }
    )
    result = await WorkflowEngine(
        spec=spec, registry=restarted.state.registry, workspace_id="ws-alpha"
    ).run(initial_state={"n1": 15, "n2": 27})
    assert result.status == "completed" and "42" in str(result.state.get("sum"))
