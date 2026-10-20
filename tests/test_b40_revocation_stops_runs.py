"""The OSS run-cancellation seam remains idempotent and workspace-scoped.

Membership/key mutation triggers live in enterprise. These tests exercise the
kept cancellation helper against local-owner runs and local fake handlers only.
"""

from pathlib import Path
from typing import Any

import httpx
import pytest
from _local_env import local_headers

from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.service.api.run_revocation import ACCESS_REVOKED, cancel_revoked_runs
from nexura_runtime.workflow import ModuleRegistry, interrupt


@pytest.mark.parametrize("kind", ["approval", "input"])
async def test_local_cancellation_is_scoped_idempotent_and_blocks_resume(
    tmp_path: Path, kind: str
) -> None:
    reg = ModuleRegistry()
    effects = []

    def gated(state: dict[str, Any]) -> dict[str, Any]:
        effects.append("ran")
        return {"done": True}

    def asking(state: dict[str, Any]) -> dict[str, Any]:
        return {"answer": interrupt("answer?")}

    reg.register("gated", gated)
    reg.register("asking", asking)
    app = create_api_app(db_path=tmp_path / "local.db", registry=reg)
    spec = {
        "name": "local",
        "entry_node": "n",
        "nodes": {
            "n": {
                "id": "n",
                "handler": "gated" if kind == "approval" else "asking",
                "requires_approval": kind == "approval",
            }
        },
        "terminal_nodes": ["n"],
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as c:
        started = []
        for wid in ("A", "B"):
            r = await c.post(
                "/api/v1/runs", headers=local_headers(wid), json={"spec": spec, "initial_state": {}}
            )
            assert r.status_code == 201, r.text
            started.append(r.json())
        a, b = started
        assert (
            await cancel_revoked_runs(
                app.state, workspace_id="A", principal_id="forged-other-user", reason=ACCESS_REVOKED
            )
            == []
        )
        stopped = await cancel_revoked_runs(
            app.state, workspace_id="A", principal_id="local-owner", reason=ACCESS_REVOKED
        )
        assert stopped == [a["run_id"]]
        repeat = await cancel_revoked_runs(
            app.state, workspace_id="A", principal_id="local-owner", reason=ACCESS_REVOKED
        )
        assert repeat == []
        row = (await c.get(f"/api/v1/runs/{a['run_id']}", headers=local_headers("A"))).json()
        assert row["status"] == "cancelled" and row["cancel_reason"] == ACCESS_REVOKED
        assert [e["type"] for e in row["events"]].count("RUN_CANCELLED") == 1
        pending = a["pending_approval"] if kind == "approval" else a["pending_input"]
        body = (
            {"approval_id": pending["approval_id"]}
            if kind == "approval"
            else {"interrupt_id": pending["interrupt_id"], "value": "ok"}
        )
        suffix = "decide" if kind == "approval" else "input"
        denied = await c.post(
            f"/api/v1/runs/{a['run_id']}/{suffix}", headers=local_headers("A"), json=body
        )
        assert denied.status_code == 409
        assert denied.json()["detail"]["code"] == "RUN_ACCESS_REVOKED"
        other = (await c.get(f"/api/v1/runs/{b['run_id']}", headers=local_headers("B"))).json()
        assert other["status"] == (
            "pending_approval" if kind == "approval" else "waiting_for_input"
        )
        assert effects == []


async def test_local_cancel_selector_validation(tmp_path: Path) -> None:
    app = create_api_app(db_path=tmp_path / "selectors.db")
    with pytest.raises(ValueError, match="exactly one"):
        await cancel_revoked_runs(
            app.state,
            workspace_id="A",
            principal_id="local-owner",
            api_key_id="forged",
            reason=ACCESS_REVOKED,
        )
