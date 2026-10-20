"""Local cancellation races, adapted from .autopilot/b40/repros.

The "cancelled is final" case runs the revocation as an independent task (a real revocation is
its own request); the original repro nested it inside the run's own execution task through the
in-process client, so the revocation cancelled itself (harness artifact, recorded in LOG).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest
from httpx import ASGITransport

from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.service.workspaces import WorkspaceRepository


def _headers() -> dict[str, str]:
    return {"X-Workspace-ID": "wsA", "X-Principal-ID": "forged", "X-Principal-Role": "viewer"}


def _node(node_id: str, handler: str, nxt: str | None = None, **extra: Any) -> dict[str, Any]:
    return {
        "id": node_id,
        "handler": handler,
        "transitions": {},
        "default_transition": nxt,
        **extra,
    }


def _chain(*nodes: dict[str, Any]) -> dict[str, Any]:
    return {
        "spec_version": 1,
        "name": "B40 review",
        "entry_node": nodes[0]["id"],
        "nodes": {n["id"]: n for n in nodes},
        "terminal_nodes": [nodes[-1]["id"]],
    }


class Fakes:
    """``r_block`` parks until released; ``r_effect`` records a side effect."""

    def __init__(self) -> None:
        self.entered = 0
        self.release = asyncio.Event()
        self.effects: list[str] = []

    def install(self, app: Any) -> None:
        registry = app.state.registry

        async def r_block(state: dict[str, Any]) -> dict[str, Any]:
            self.entered += 1
            await self.release.wait()
            return {"blocked": True}

        def r_effect(state: dict[str, Any]) -> dict[str, Any]:
            self.effects.append("sent")
            return {"sent": True}

        def r_noop(state: dict[str, Any]) -> dict[str, Any]:
            return {"noop": True}

        registry.register("r_block", r_block)
        registry.register("r_effect", r_effect)
        registry.register("r_noop", r_noop)

    async def wait_entered(self, count: int = 1) -> None:
        for _ in range(1000):
            if self.entered >= count:
                return
            await asyncio.sleep(0.01)
        raise AssertionError("blocking handler never entered")


def _client(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


@pytest.fixture
def local(tmp_path: Path) -> tuple[Any, Fakes]:
    repo = WorkspaceRepository()
    repo.create_workspace("wsA", "A", "local-owner")
    app = create_api_app(db_path=tmp_path / "local.db", workspace_repo=repo)
    fakes = Fakes()
    fakes.install(app)
    return app, fakes


OWNER = _headers()


def _hook_event(app: Any, event_type: str, action: Any) -> dict[str, bool]:
    """Run ``action()`` once, right after the run event ``event_type`` is stored."""
    store = app.state.store
    original = store.record_run_event
    state = {"armed": True, "fired": False}

    async def wrapped(run_id: str, etype: str, node_id: Any = None, data: Any = None) -> None:
        await original(run_id, etype, node_id=node_id, data=data)
        if state["armed"] and etype == event_type:
            state["armed"] = False
            state["fired"] = True
            await action()

    store.record_run_event = wrapped
    return state


async def _cancel_runs(app: Any) -> dict[str, Any]:
    cancelled = []
    async with _client(app) as client:
        rows = await app.state.store.list_runs(limit=None)
        for run in rows:
            if run["status"] in {"running", "pending_approval", "waiting_for_input"}:
                r = await client.post(f"/api/v1/runs/{run['run_id']}/cancel", headers=_headers())
                assert r.status_code == 200, r.text
                assert r.json()["status"] == "cancelled"
                cancelled.append(run["run_id"])
    return {"cancelled_run_ids": sorted(cancelled)}


# --- 1. a synchronous run keeps executing its later nodes after its starter is removed -------


async def test_sync_run_mid_step_does_not_run_later_nodes_after_cancel(
    local: tuple[Any, Fakes],
) -> None:
    app, fakes = local
    spec = _chain(_node("n1", "r_block", "n2"), _node("n2", "r_effect"))
    async with _client(app) as c:
        start = asyncio.create_task(
            c.post(
                "/api/v1/runs",
                json={"spec": spec, "initial_state": {}, "async_execution": False},
                headers=OWNER,
            )
        )
        await fakes.wait_entered()
        removed = await _cancel_runs(app)
        assert len(removed["cancelled_run_ids"]) == 1  # the run was found and "cancelled"
        fakes.release.set()
        await start
        # Expected: revocation stops the run, so node n2 (a side effect) never runs.
        assert fakes.effects == [], "n2 executed after its starter's access was revoked"


# --- 2. an approval being resumed keeps executing after its starter is removed ---------------


async def test_in_flight_approve_does_not_run_later_nodes_after_cancel(
    local: tuple[Any, Fakes],
) -> None:
    app, fakes = local
    spec = _chain(
        _node("n1", "r_noop", "n2", requires_approval=True),
        _node("n2", "r_block", "n3"),
        _node("n3", "r_effect"),
    )
    async with _client(app) as c:
        r = await c.post("/api/v1/runs", json={"spec": spec, "initial_state": {}}, headers=OWNER)
        run = r.json()
        assert run["status"] == "pending_approval", run
        decide = asyncio.create_task(
            c.post(
                f"/api/v1/runs/{run['run_id']}/decide",
                json={"approval_id": run["pending_approval"]["approval_id"]},
                headers=OWNER,
            )
        )
        await fakes.wait_entered()
        removed = await _cancel_runs(app)
        assert removed["cancelled_run_ids"] == [run["run_id"]]
        fakes.release.set()
        await decide
        assert fakes.effects == [], "n3 executed after the run was cancelled by revocation"


# --- 3. revoke landing between the RUN_ACCESS_REVOKED check and engine.resume ---------------


async def test_cancel_racing_decide_does_not_execute_gated_node(
    local: tuple[Any, Fakes],
) -> None:
    app, fakes = local
    spec = _chain(_node("work", "r_effect", requires_approval=True))
    async with _client(app) as c:
        r = await c.post("/api/v1/runs", json={"spec": spec, "initial_state": {}}, headers=OWNER)
        run = r.json()
        assert run["status"] == "pending_approval", run

        async def revoke() -> None:
            removed = await _cancel_runs(app)
            assert removed["cancelled_run_ids"] == [run["run_id"]]

        hook = _hook_event(app, "APPROVAL_GRANTED", revoke)
        resp = await c.post(
            f"/api/v1/runs/{run['run_id']}/decide",
            json={"approval_id": run["pending_approval"]["approval_id"]},
            headers=OWNER,
        )
        assert hook["fired"]
        # Expected: the gated write never runs and the approver is told the run was revoked.
        assert fakes.effects == [], "approval-gated handler ran after the run was cancelled"
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"]["code"] == "NOT_PENDING_APPROVAL"


# --- 4. an in-flight resume that pauses again overwrites 'cancelled' (run revived) ----------


async def test_cancelled_is_final_when_resume_pauses_again(local: tuple[Any, Fakes]) -> None:
    app, fakes = local
    spec = _chain(
        _node("g1", "r_noop", "g2", requires_approval=True),
        _node("g2", "r_effect", requires_approval=True),
    )
    async with _client(app) as c:
        r = await c.post("/api/v1/runs", json={"spec": spec, "initial_state": {}}, headers=OWNER)
        run = r.json()
        assert run["status"] == "pending_approval", run
        rid = run["run_id"]

        async def revoke() -> None:
            # A real revocation is its own request: run it as an independent task and let the
            # resuming request wait for it at this point (after the "already cancelled?" check).
            task = asyncio.create_task(_cancel_runs(app))
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
                raise
            assert task.result()["cancelled_run_ids"] == [rid]

        # Fires inside _persist_result, after its "already cancelled?" check.
        hook = _hook_event(app, "RUN_PENDING_APPROVAL", revoke)
        first = await c.post(
            f"/api/v1/runs/{rid}/decide",
            json={"approval_id": run["pending_approval"]["approval_id"]},
            headers=OWNER,
        )
        assert first.status_code == 200, first.text
        assert hook["fired"]
        latest = (await c.get(f"/api/v1/runs/{rid}", headers=OWNER)).json()
        # Expected: cancelled stays final.
        assert latest["status"] == "cancelled", (latest["status"], latest["cancel_reason"])
        second = await c.post(
            f"/api/v1/runs/{rid}/decide",
            json={"approval_id": (latest.get("pending_approval") or {}).get("approval_id", "x")},
            headers=OWNER,
        )
        assert second.status_code == 409
        assert fakes.effects == []


# --- 5. a reject racing the revocation overwrites 'cancelled' with 'rejected' ---------------


async def test_reject_racing_cancel_does_not_overwrite_cancelled(
    local: tuple[Any, Fakes],
) -> None:
    app, _ = local
    spec = _chain(_node("work", "r_effect", requires_approval=True))
    async with _client(app) as c:
        r = await c.post("/api/v1/runs", json={"spec": spec, "initial_state": {}}, headers=OWNER)
        run = r.json()
        rid = run["run_id"]
        cancelled: list[str] = []

        async def revoke() -> None:
            cancelled.extend((await _cancel_runs(app))["cancelled_run_ids"])

        hook = _hook_event(app, "APPROVAL_REJECTED", revoke)
        await c.post(
            f"/api/v1/runs/{rid}/decide",
            json={"approval_id": run["pending_approval"]["approval_id"], "decision": "reject"},
            headers=OWNER,
        )
        assert hook["fired"] and cancelled == [rid]  # reported (and audited) as cancelled
        latest = (await c.get(f"/api/v1/runs/{rid}", headers=OWNER)).json()
        # Expected: the run the revocation reported and audited as cancelled stays cancelled.
        assert latest["status"] == "cancelled", (latest["status"], latest["cancel_reason"])


# --- 6. a run started while its starter is being removed survives the removal ---------------


async def test_background_run_started_concurrently_with_cancel_is_not_left_running(
    local: tuple[Any, Fakes],
) -> None:
    app, fakes = local
    spec = _chain(_node("wait", "r_block", "effect"), _node("effect", "r_effect"))
    async with _client(app) as c:
        r = await c.post(
            "/api/v1/runs",
            json={"spec": spec, "initial_state": {}, "async_execution": True},
            headers=_headers(),
        )
        assert r.status_code == 201, r.text
        rid = r.json()["run_id"]
        await fakes.wait_entered()
        try:
            stopped = await c.post(f"/api/v1/runs/{rid}/cancel", headers=_headers())
            assert stopped.status_code == 200, stopped.text
            assert stopped.json()["status"] == "cancelled"
        finally:
            fakes.release.set()
        latest = await c.get(f"/api/v1/runs/{rid}", headers=_headers())
        assert latest.json()["status"] == "cancelled"
        assert rid not in app.state.active_run_tasks
        assert fakes.effects == []
