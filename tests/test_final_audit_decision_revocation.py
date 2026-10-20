"""Decision/revocation ordering with OSS stores and local ASGI requests only."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest

from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.service.api.run_revocation import ACCESS_REVOKED, cancel_revoked_runs
from nexura_runtime.workflow import ModuleRegistry, WorkflowNode, WorkflowSpec


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["grant", "reject", "claim", "rejected_result"])
async def test_revocation_wins_at_decision_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    effects: list[str] = []

    async def effect(state: dict[str, Any]) -> dict[str, Any]:
        effects.append("sent")
        return {"sent": True}

    registry = ModuleRegistry()
    registry.register("effect", effect)
    app = create_api_app(db_path=tmp_path / "decisions.sqlite3", registry=registry)
    spec = WorkflowSpec(
        name="revocation",
        entry_node="n",
        nodes={"n": WorkflowNode("n", "effect", requires_approval=True)},
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 5000)),
            base_url="http://127.0.0.1",
            headers={"X-Nexura-Session": app.state.session_token},
        ) as client,
    ):
        started = await client.post("/api/v1/runs", json={"spec": spec.to_dict()})
        assert started.status_code == 201, started.text
        run = started.json()
        run_id = run["run_id"]
        approval_id = run["pending_approval"]["approval_id"]
        cancelled: list[str] = []

        async def revoke() -> None:
            cancelled.extend(
                await cancel_revoked_runs(
                    app.state,
                    workspace_id="default",
                    principal_id="local-owner",
                    reason=ACCESS_REVOKED,
                )
            )

        original_event = app.state.store.record_run_event
        event_type = {
            "grant": "APPROVAL_GRANTED",
            "reject": "APPROVAL_REJECTED",
            "rejected_result": "RUN_REJECTED",
        }.get(boundary)
        fired = False

        async def record_event(rid: str, etype: str, **kwargs: Any) -> None:
            nonlocal fired
            await original_event(rid, etype, **kwargs)
            if etype == event_type and not fired:
                fired = True
                await revoke()

        monkeypatch.setattr(app.state.store, "record_run_event", record_event)
        checkpoints = app.state.store.checkpoint_store
        original_claim = checkpoints.claim_approval
        claims: list[str] = []
        claiming = asyncio.Event()

        async def claim(rid: str, aid: str) -> bool:
            claims.append(aid)
            if boundary == "claim":
                claiming.set()
                await asyncio.Event().wait()  # cancelled by the independent revocation
            return bool(await original_claim(rid, aid))

        monkeypatch.setattr(checkpoints, "claim_approval", claim)
        deciding = asyncio.create_task(
            client.post(
                f"/api/v1/runs/{run_id}/decide",
                json={
                    "approval_id": approval_id,
                    "decision": "reject"
                    if boundary in ("reject", "rejected_result")
                    else "approve",
                },
            )
        )
        try:
            if boundary == "claim":
                await asyncio.wait_for(claiming.wait(), timeout=5)
                await revoke()
            response = await asyncio.wait_for(deciding, timeout=5)
        finally:
            if not deciding.done():
                deciding.cancel()
                await asyncio.gather(deciding, return_exceptions=True)

        assert cancelled == [run_id]
        assert effects == []
        latest = (await client.get(f"/api/v1/runs/{run_id}")).json()
        assert latest["status"] == "cancelled"
        assert latest["cancel_reason"] == "access_revoked"
        if boundary == "rejected_result":
            assert response.status_code == 200, response.text
            assert response.json()["status"] == "cancelled"
            assert (await checkpoints.get(run_id)).status == "rejected"
            assert not await original_claim(run_id, approval_id)
        else:
            assert response.status_code == 409, response.text
            assert response.json()["detail"]["code"] == "RUN_ACCESS_REVOKED"
        if boundary in ("grant", "reject"):
            assert fired
            assert claims == []
        else:
            assert claims == [approval_id]
        later = await client.post(
            f"/api/v1/runs/{run_id}/decide",
            json={"approval_id": approval_id, "decision": "approve"},
        )
        assert later.status_code == 409
        assert later.json()["detail"]["code"] == "RUN_ACCESS_REVOKED"
        assert effects == []
