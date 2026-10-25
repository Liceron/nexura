"""A6 S4: REST effect inspection and resolution over the service's execution journal.

The paused runs here are ``WorkflowEngine`` runs in the service database whose worker died
inside a non-idempotent step (A6 S12 moved these tests off the removed
``DurableExecutionEngine``). All effects are local fake handlers; no network or paid
provider is involved.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from _local_env import local_headers
from fastapi.testclient import TestClient
from nexura_agents.contracts.execution import EffectStatus, ExecutionId
from nexura_agents.execution_store import SQLiteExecutionStore
from starlette.requests import Request

from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.service.auth.models import Principal
from nexura_runtime.service.extensions import Identity
from nexura_runtime.service.workspaces.repository import WorkspaceRepository
from nexura_runtime.workflow import (
    ModuleRegistry,
    SQLiteWorkflowStore,
    WorkflowEngine,
    WorkflowNode,
    WorkflowSpec,
)

API = "/api/v1"
SPEC = WorkflowSpec(
    name="fake-payment",
    entry_node="pay",
    nodes={"pay": WorkflowNode(id="pay", handler="fake-pay")},
    terminal_nodes=["pay"],
)


class Crash(BaseException):
    """The worker process dies inside the step, after the fake payment happened."""


class FakePay:
    """An undeclared (so ``non_idempotent``) local fake payment that crashes once."""

    def __init__(self, crash: bool = True) -> None:
        self.calls = 0
        self.crash = crash
        self.registry = ModuleRegistry()
        self.registry.register("fake-pay", self.pay)

    def pay(self, state: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        if self.crash:
            self.crash = False
            raise Crash
        return {"paid": True}


async def _crashed(db: Path, workspace_id: str, run_id: str, fake: FakePay) -> None:
    """The worker died inside ``pay``: intent dispatched, effect ``pending``, lease gone."""
    store = SQLiteWorkflowStore(db)
    try:
        with pytest.raises(Crash):
            await WorkflowEngine(SPEC, fake.registry, store=store, workspace_id=workspace_id).run(
                {}, workflow_run_id=run_id
            )
    finally:
        await store.close()


async def _recover(db: Path, workspace_id: str, run_id: str, fake: FakePay) -> Any:
    store = SQLiteWorkflowStore(db)
    try:
        engine = WorkflowEngine(SPEC, fake.registry, store=store, workspace_id=workspace_id)
        return await engine.recover(run_id)
    finally:
        await store.close()


async def _unknown_effect(db: Path, workspace_id: str, run_id: str, fake: FakePay) -> str:
    """Leave a run paused with an indeterminate effect, as a crash mid-dispatch does."""
    await _crashed(db, workspace_id, run_id, fake)
    paused = await _recover(db, workspace_id, run_id, fake)
    assert paused.status == "paused" and paused.pause_reason == "effect_unknown"
    assert fake.calls == 1
    store = SQLiteExecutionStore(db)
    try:
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
    fake = FakePay()
    effect_id = asyncio.run(_unknown_effect(db, "wsA", "run-1", fake))
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

    done = asyncio.run(_recover(db, "wsA", "run-1", fake))
    assert done.status == "completed" and fake.calls == 1  # confirmed: not dispatched again
    assert done.state["receipt"] == "local-fake"

    async def journal() -> Any:
        store = SQLiteExecutionStore(db)
        try:
            return await store.get(ExecutionId("wsA", "run-1"))
        finally:
            await store.close()

    snapshot = asyncio.run(journal())
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
    effect_id = asyncio.run(_unknown_effect(db, "wsA", "run-1", FakePay()))
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

    async def crashed_with_pending_effect() -> str:
        await _crashed(db, "wsA", "run-live", FakePay())
        store = SQLiteExecutionStore(db)
        try:
            snapshot = await store.get(ExecutionId("wsA", "run-live"))
            (effect,) = snapshot.effects.values()
            assert effect.status == EffectStatus.PENDING  # nobody has recovered it yet
            return effect.effect_id
        finally:
            await store.close()

    effect_id = asyncio.run(crashed_with_pending_effect())
    with client:
        response = client.post(
            f"{API}/runs/run-live/effects/{effect_id}/resolve",
            json={"resolution": "not_applied"},
            headers=local_headers("wsA"),
        )
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "EFFECT_NOT_INDETERMINATE"

        async def indeterminate_under_a_live_lease() -> None:
            from dataclasses import replace

            store = SQLiteExecutionStore(db)
            try:
                identity = ExecutionId("wsA", "run-live")
                snapshot = await store.get(identity)
                lease = await store.claim(
                    identity, "live-worker", 60, definition_digest=snapshot.run.definition_digest
                )
                assert lease is not None
                effect = snapshot.effects[effect_id]
                await store.commit(
                    lease,
                    state=snapshot.run.state,
                    effects=[replace(effect, status=EffectStatus.INDETERMINATE)],
                )
            finally:
                await store.close()

        asyncio.run(indeterminate_under_a_live_lease())
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
    effect_id = asyncio.run(_unknown_effect(db, "default", "run-s", FakePay()))
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
    """A timeout pauses an unknown payment; REST resolution continues without a duplicate."""
    client, db = _local(tmp_path)
    calls = []

    async def slow(state: dict[str, Any]) -> dict[str, Any]:
        calls.append("local fake debit")
        await asyncio.sleep(1)
        return {"paid": "late"}

    registry = ModuleRegistry()
    registry.register("fake-pay", slow)

    async def time_out() -> str:
        store = SQLiteWorkflowStore(db)
        try:
            engine = WorkflowEngine(SPEC, registry, store=store, workspace_id="wsA", timeout=0.05)
            paused = await engine.run({}, workflow_run_id="run-t")
            assert paused.status == "paused" and paused.pause_reason == "effect_unknown"
            assert paused.pending_effect is not None
            return str(paused.pending_effect["effect_id"])
        finally:
            await store.close()

    effect_id = asyncio.run(time_out())
    with client:
        headers = local_headers("wsA")
        (effect,) = client.get(f"{API}/runs/run-t/effects", headers=headers).json()["effects"]
        assert effect["status"] == "indeterminate"
        resolved = client.post(
            f"{API}/runs/run-t/effects/{effect_id}/resolve",
            json={
                "resolution": "confirmed",
                "result": {"paid": "per ledger"},
                "reason": "Checked local fake ledger",
            },
            headers=headers,
        )
        assert resolved.status_code == 200, resolved.text
        assert resolved.json()["resolved_by"] == "local-owner"

    async def recover() -> Any:
        store = SQLiteWorkflowStore(db)
        try:
            done = await WorkflowEngine(SPEC, registry, store=store, workspace_id="wsA").recover(
                "run-t"
            )
            snapshot = await (await store.open_execution_store()).get(ExecutionId("wsA", "run-t"))
            audit = [e.payload for e in snapshot.events if e.event_type == "effect_resolved"]
            assert len(audit) == 1
            assert audit[0]["actor"] == "local-owner"
            assert audit[0]["reason"] == "Checked local fake ledger"
            return done
        finally:
            await store.close()

    done = asyncio.run(recover())
    assert done.status == "completed" and done.state["paid"] == "per ledger"
    assert calls == ["local fake debit"]
