"""A6 S4: REST effect inspection and resolution over the service's execution journal.

All effects are local fake handlers; no network or paid provider is involved.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from _local_env import local_headers
from fastapi.testclient import TestClient
from nexura_agents.contracts.errors import IndeterminateEffectError
from nexura_agents.contracts.execution import EffectStatus, ExecutionId
from nexura_agents.execution_store import SQLiteExecutionStore
from starlette.requests import Request

from nexura_runtime.runtime.execution import (
    DurableExecutionEngine,
    DurableNodeSpec,
    DurableWorkflowSpec,
    NodeIntent,
)
from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.service.auth.models import Principal
from nexura_runtime.service.extensions import Identity
from nexura_runtime.service.workspaces.repository import WorkspaceRepository

API = "/api/v1"
SPEC = DurableWorkflowSpec(
    workflow_id="fake-payment",
    entry_node="pay",
    terminal_nodes=["pay"],
    nodes={"pay": DurableNodeSpec(node_id="pay", handler="fake-pay")},
)


async def _unknown_effect(db: Path, workspace_id: str, run_id: str) -> str:
    """Leave a run paused with an indeterminate effect, as a crash mid-dispatch does."""
    store = SQLiteExecutionStore(db)
    try:
        engine = DurableExecutionEngine(store, {"fake-pay": lambda _: {"paid": True}})
        await engine.init_run(workspace_id, run_id, SPEC)
        state = await engine.claim_run(workspace_id, run_id, "crashed-worker")
        # The crashed worker had committed its dispatch intent before dying.
        state.pre_dispatch_intent = NodeIntent(
            node_id="pay", effect_policy="non_idempotent_write", dispatched=True
        )
        records = await engine._records()
        await records.update_run(
            workspace_id, run_id, state.revision, "running", state.model_dump()
        )
        with pytest.raises(IndeterminateEffectError):
            await engine.execute_step(workspace_id, run_id, "crashed-worker", SPEC)
        snapshot = await store.get(ExecutionId(workspace_id, run_id))
        (effect,) = snapshot.effects.values()
        assert effect.status == EffectStatus.INDETERMINATE
        return effect.effect_id
    finally:
        await store.close()


def _local(tmp_path: Path) -> tuple[TestClient, Path]:
    db = tmp_path / "svc.sqlite3"
    repo = WorkspaceRepository()
    repo.create_workspace("wsA", "A", owner_id="local-owner")
    repo.create_workspace("wsB", "B", owner_id="local-owner")
    app = create_api_app(db_path=str(db), workspace_repo=repo, profile="local-owner")
    return TestClient(app, raise_server_exceptions=False), db


def test_list_and_resolve_once_then_the_run_continues(tmp_path: Path) -> None:
    client, db = _local(tmp_path)
    effect_id = asyncio.run(_unknown_effect(db, "wsA", "run-1"))
    headers = local_headers("wsA")
    with client:
        listed = client.get(f"{API}/runs/run-1/effects", headers=headers)
        assert listed.status_code == 200, listed.text
        (effect,) = listed.json()["effects"]
        assert effect["effect_id"] == effect_id
        assert effect["status"] == "indeterminate" and effect["resolution"] is None
        assert effect["intent"]["idempotency_key"].startswith("nx-")
        assert effect["intent"]["effect_class"] == "non_idempotent"

        body = {"resolution": "confirmed", "result": {"receipt": "local-fake", "api_key": "s3"}}
        resolved = client.post(
            f"{API}/runs/run-1/effects/{effect_id}/resolve", json=body, headers=headers
        )
        assert resolved.status_code == 200, resolved.text
        data = resolved.json()
        assert data["status"] == "resolved" and data["resolution"] == "confirmed"
        # The actor is the authenticated principal, not the forged header.
        assert data["resolved_by"] == "local-owner"
        assert data["result"] == {"receipt": "local-fake", "api_key": "[REDACTED]"}

        again = client.post(
            f"{API}/runs/run-1/effects/{effect_id}/resolve", json=body, headers=headers
        )
        assert again.status_code == 409
        assert again.json()["detail"]["code"] == "EFFECT_ALREADY_RESOLVED"

    async def resume() -> Any:
        store = SQLiteExecutionStore(db)
        try:
            calls: list[str] = []
            engine = DurableExecutionEngine(store, {"fake-pay": lambda _: calls.append("x")})
            state = await engine.run_until_idle("wsA", "run-1", "resumer", SPEC)
            snapshot = await store.get(ExecutionId("wsA", "run-1"))
            return state, calls, snapshot
        finally:
            await store.close()

    state, calls, snapshot = asyncio.run(resume())
    assert state.status == "completed" and calls == []  # confirmed: not dispatched again
    assert state.state["receipt"] == "local-fake"
    audit = [e.payload for e in snapshot.events if e.event_type == "effect_resolved"]
    assert audit == [
        {
            "type": "effect_resolved",
            "effect_id": effect_id,
            "resolution": "confirmed",
            "actor": "local-owner",
        }
    ]


def test_workspace_isolation_unknown_ids_and_bad_bodies(tmp_path: Path) -> None:
    client, db = _local(tmp_path)
    effect_id = asyncio.run(_unknown_effect(db, "wsA", "run-1"))
    with client:
        other = local_headers("wsB")
        assert client.get(f"{API}/runs/run-1/effects", headers=other).status_code == 404
        refused = client.post(
            f"{API}/runs/run-1/effects/{effect_id}/resolve",
            json={"resolution": "failed"},
            headers=other,
        )
        assert refused.status_code == 404
        assert refused.json()["detail"]["code"] == "RUN_NOT_FOUND"

        mine = local_headers("wsA")
        missing = client.get(f"{API}/runs/no-such-run/effects", headers=mine)
        assert missing.status_code == 404
        assert missing.json()["detail"]["code"] == "RUN_NOT_FOUND"
        unknown = client.post(
            f"{API}/runs/run-1/effects/nope/resolve", json={"resolution": "failed"}, headers=mine
        )
        assert unknown.status_code == 404
        assert unknown.json()["detail"]["code"] == "EFFECT_NOT_FOUND"
        for bad in (
            {"resolution": "rolled_back"},
            {"resolution": "failed", "actor": "someone-else"},
            {},
        ):
            response = client.post(
                f"{API}/runs/run-1/effects/{effect_id}/resolve", json=bad, headers=mine
            )
            assert response.status_code == 422, bad
        # Nothing above changed the effect.
        (effect,) = client.get(f"{API}/runs/run-1/effects", headers=mine).json()["effects"]
        assert effect["status"] == "indeterminate"


def test_pending_effect_and_live_worker_are_conflicts(tmp_path: Path) -> None:
    client, db = _local(tmp_path)

    async def running_with_pending_effect() -> str:
        store = SQLiteExecutionStore(db)
        try:
            engine = DurableExecutionEngine(store)
            await engine.init_run("wsA", "run-live", SPEC)
            state = await engine.claim_run("wsA", "run-live", "live-worker", lease_duration=60)
            state.pre_dispatch_intent = NodeIntent(
                node_id="pay", effect_policy="non_idempotent_write", dispatched=True
            )
            records = await engine._records()
            await records.update_run(
                "wsA", "run-live", state.revision, "running", state.model_dump()
            )
            snapshot = await store.get(ExecutionId("wsA", "run-live"))
            (effect,) = snapshot.effects.values()
            assert effect.status == EffectStatus.PENDING
            return effect.effect_id
        finally:
            await store.close()

    effect_id = asyncio.run(running_with_pending_effect())
    with client:
        response = client.post(
            f"{API}/runs/run-live/effects/{effect_id}/resolve",
            json={"resolution": "not_applied"},
            headers=local_headers("wsA"),
        )
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "EFFECT_NOT_INDETERMINATE"

        async def mark_indeterminate_under_live_lease() -> None:
            from dataclasses import replace

            store = SQLiteExecutionStore(db)
            try:
                snapshot = await store.get(ExecutionId("wsA", "run-live"))
                effect = snapshot.effects[effect_id]
                await store.commit(
                    snapshot.run.lease,
                    state=snapshot.run.state,
                    effects=[replace(effect, status=EffectStatus.INDETERMINATE)],
                )
            finally:
                await store.close()

        asyncio.run(mark_indeterminate_under_live_lease())
        response = client.post(
            f"{API}/runs/run-live/effects/{effect_id}/resolve",
            json={"resolution": "not_applied"},
            headers=local_headers("wsA"),
        )
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "RUN_LEASE_HELD"


class _RoleProvider:
    """Test-only shared-service identity: X-Test-Role selects the role."""

    __test__ = False
    name = "a6-effects-test-auth"
    is_authenticating = True
    routes_without_membership: frozenset[tuple[str, str]] = frozenset()

    def authenticate(self, request: Request, target_workspace_id: str | None) -> Identity:
        role = request.headers.get("x-test-role", "viewer")
        return Identity(
            Principal(principal_id=f"person-{role}", profile="authenticated-shared-service"),
            target_workspace_id or "default",
            role,  # type: ignore[arg-type]
        )

    def is_operator(self, request: Request) -> bool:
        return False


def test_shared_service_viewer_can_list_but_not_resolve(tmp_path: Path) -> None:
    db = tmp_path / "shared.sqlite3"
    app = create_api_app(
        db_path=str(db), profile="authenticated-shared-service", auth_provider=_RoleProvider()
    )
    client = TestClient(app, raise_server_exceptions=False)
    effect_id = asyncio.run(_unknown_effect(db, "default", "run-s"))
    # A shared service serves only runs with a recorded owner (fail closed).
    app.state.workspace_repo.record_resource_ownership("run", "run-s", "default", "person-admin")
    url = f"{API}/runs/run-s/effects"
    with client:
        viewer = {"X-Test-Role": "viewer"}
        listed = client.get(url, headers=viewer)
        assert listed.status_code == 200, listed.text
        assert listed.json()["effects"][0]["effect_id"] == effect_id
        denied = client.post(
            f"{url}/{effect_id}/resolve", json={"resolution": "failed"}, headers=viewer
        )
        assert denied.status_code == 403
        allowed = client.post(
            f"{url}/{effect_id}/resolve",
            json={"resolution": "failed"},
            headers={"X-Test-Role": "admin"},
        )
        assert allowed.status_code == 200, allowed.text
        assert allowed.json()["resolved_by"] == "person-admin"


async def test_python_client_effect_methods_encode_ids_and_body() -> None:
    import json

    import httpx

    from nexura_runtime.service.api.client import NexuraClient

    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"effects": []})

    client = NexuraClient(
        base_url="http://mock-service",
        workspace_id="wsA",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    assert await client.runs.effects("run 1") == {"effects": []}
    await client.runs.resolve_effect("run 1", "e/1", "confirmed", result={"receipt": "local"})
    await client.runs.resolve_effect("run 1", "e/1", "not_applied")
    assert calls[0].method == "GET" and calls[0].url.raw_path == b"/api/v1/runs/run%201/effects"
    assert calls[1].method == "POST"
    assert calls[1].url.raw_path == b"/api/v1/runs/run%201/effects/e%2F1/resolve"
    assert json.loads(calls[1].content) == {
        "resolution": "confirmed",
        "result": {"receipt": "local"},
    }
    assert json.loads(calls[2].content) == {"resolution": "not_applied"}


def test_timed_out_node_is_resolved_over_rest_and_continues(tmp_path: Path) -> None:
    """A timed-out non-pure node pauses effect_unknown; REST resolves it and the run continues."""
    from nexura_agents.contracts.errors import DeadlineExceededError

    client, db = _local(tmp_path)
    spec = DurableWorkflowSpec(
        workflow_id="fake-timeout",
        entry_node="pay",
        terminal_nodes=["pay"],
        nodes={"pay": DurableNodeSpec(node_id="pay", handler="fake-pay", timeout_seconds=0.05)},
    )

    async def slow(_: dict[str, Any]) -> dict[str, Any]:
        await asyncio.sleep(1)
        return {}

    async def time_out() -> str:
        store = SQLiteExecutionStore(db)
        try:
            engine = DurableExecutionEngine(store, {"fake-pay": slow})
            await engine.init_run("wsA", "run-t", spec)
            await engine.claim_run("wsA", "run-t", "worker")
            with pytest.raises(DeadlineExceededError):
                await engine.execute_step("wsA", "run-t", "worker", spec)
            (effect_id,) = (await store.get(ExecutionId("wsA", "run-t"))).effects
            return effect_id
        finally:
            await store.close()

    effect_id = asyncio.run(time_out())
    with client:
        headers = local_headers("wsA")
        (effect,) = client.get(f"{API}/runs/run-t/effects", headers=headers).json()["effects"]
        assert effect["status"] == "indeterminate"
        resolved = client.post(
            f"{API}/runs/run-t/effects/{effect_id}/resolve",
            json={"resolution": "confirmed", "result": {"paid": "per ledger"}},
            headers=headers,
        )
        assert resolved.status_code == 200, resolved.text
        assert resolved.json()["resolved_by"] == "local-owner"

    async def resume() -> Any:
        store = SQLiteExecutionStore(db)
        try:
            engine = DurableExecutionEngine(store, {"fake-pay": slow})
            return await engine.run_until_idle("wsA", "run-t", "resumer", spec)
        finally:
            await store.close()

    done = asyncio.run(resume())
    assert done.status == "completed" and done.state["paid"] == "per ledger"
