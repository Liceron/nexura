# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""P46: deleting a workspace in the open-source edition (the local owner is its operator).

A workspace seeded with one of everything (``nexura_runtime.testing.workspace_purge``) is
deleted through ``DELETE /api/v1/workspaces/{id}``: its live run is cancelled in the request,
then the purge leaves no row and no blob of it (blobs filed under ``default/`` included), while
another workspace keeps everything. The id is retired for good (also after a restart), the
workspace is hidden and closed, ``default`` is protected, the confirmation is required, and an
interrupted purge is finished by the sweep the trigger tick runs. Runs on SQLite, and on
Postgres under ``--service-backend=postgres``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest

from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.service.triggers.tick import run_trigger_tick
from nexura_runtime.service.workspaces import deletion
from nexura_runtime.service.workspaces.repository import WorkspaceDeletedError
from nexura_runtime.testing.workspace_purge import Seeded, seed_workspace, workspace_footprint
from nexura_runtime.workflow import WorkflowSpec

API = "/api/v1"


@pytest.fixture(autouse=True)
def _blobs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXURA_BLOB_URL", (tmp_path / "blobs").as_uri())


def _app(tmp_path: Path) -> Any:
    app = create_api_app(db_path=tmp_path / "svc.sqlite3")
    app.state.workspace_purge_final_delay = 0.0
    return app


def _client(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1")


def _ws(workspace_id: str) -> dict[str, str]:
    return {"X-Workspace-ID": workspace_id}


async def _create(client: httpx.AsyncClient, workspace_id: str) -> None:
    res = await client.post(f"{API}/workspaces", json={"workspace_id": workspace_id, "name": "x"})
    assert res.status_code == 201, res.text


async def _seed(app: Any, client: httpx.AsyncClient, workspace_id: str) -> Seeded:
    return await seed_workspace(
        app.state, client, _ws(workspace_id), workspace_id, owner="local-owner"
    )


async def _settle(app: Any) -> None:
    tasks = list(getattr(app.state, "workspace_purge_tasks", ()))
    if tasks:
        await asyncio.gather(*tasks)


def _left(counts: dict[str, int]) -> dict[str, int]:
    return {table: n for table, n in counts.items() if n}


async def test_delete_purges_everything_and_retires_the_id(tmp_path: Path) -> None:
    app = _app(tmp_path)
    async with app.router.lifespan_context(app), _client(app) as client:
        await _create(client, "ws-a")
        await _create(client, "ws-b")
        a = await _seed(app, client, "ws-a")
        b = await _seed(app, client, "ws-b")
        before_a = _left(await workspace_footprint(app.state, a))
        before_b = await workspace_footprint(app.state, b)
        # Every kind of row exists before (the seeding reached each store).
        for table in (
            "nexura_studio_runs",
            "nexura_studio_run_events",
            "nexura_workflow_checkpoints",
            "nexura_exec_runs",
            "nexura_studio_workflows",
            "nexura_sessions",
            "nexura_service_agents",
            "nexura_service_skills",
            "nexura_service_skill_counters",
            "eval_traces",
            "studio_eval_runs",
            "hk_documents",
            "channel_outbox",
            "nexura_service_resource_ownership",
            "blobs",
        ):
            assert before_a.get(table), (table, before_a)
        assert app.state.credential_store.list_credentials("ws-a")
        # A legacy run's blob lives under default/, found through the run's events.
        assert any(key.startswith("default/") for key in a.blob_keys)

        wrong = await client.request("DELETE", f"{API}/workspaces/ws-a", json={"confirm": "ws-b"})
        assert wrong.status_code == 400
        assert wrong.json()["detail"]["code"] == "CONFIRMATION_REQUIRED"
        missing = await client.request("DELETE", f"{API}/workspaces/ws-a")
        assert missing.status_code == 400, missing.text
        protected = await client.request(
            "DELETE", f"{API}/workspaces/default", json={"confirm": "default"}
        )
        assert protected.status_code == 409
        assert protected.json()["detail"]["code"] == "WORKSPACE_PROTECTED"
        unknown = await client.request("DELETE", f"{API}/workspaces/nope", json={"confirm": "nope"})
        assert unknown.status_code == 404

        res = await client.request("DELETE", f"{API}/workspaces/ws-a", json={"confirm": "ws-a"})
        assert res.status_code == 202, res.text
        assert res.json() == {"workspace_id": "ws-a", "status": "purging"}
        await _settle(app)

        assert _left(await workspace_footprint(app.state, a)) == {}
        assert await workspace_footprint(app.state, b) == before_b
        assert app.state.credential_store.list_credentials("ws-a") == []
        assert app.state.credential_store.list_credentials("ws-b")
        repo = app.state.workspace_repo
        record = repo.get_workspace_deletion("ws-a")
        assert record is not None and record.status == "done" and record.attempts == 1
        assert record.requested_by == "local-owner" and record.completed_at is not None

        # Hidden, closed and retired.
        listed = (await client.get(f"{API}/workspaces?limit=100")).json()["items"]
        assert "ws-a" not in {w["workspace_id"] for w in listed}
        assert "ws-b" in {w["workspace_id"] for w in listed}
        for path, headers in (
            (f"{API}/workspaces/ws-a", {}),
            (f"{API}/workflows", _ws("ws-a")),
            (f"{API}/workspaces/ws-a/runs", {}),
        ):
            closed = await client.get(path, headers=headers)
            assert closed.status_code == 403, (path, closed.text)
            assert closed.json()["detail"]["code"] == "WORKSPACE_DELETED"
        again = await client.post(f"{API}/workspaces", json={"workspace_id": "ws-a", "name": "x"})
        assert again.status_code == 409 and again.json()["detail"]["code"] == "WORKSPACE_ID_RETIRED"
        with pytest.raises(WorkspaceDeletedError):
            repo.create_workspace("ws-a", "x", "local-owner")
        # The operator (the local owner) retrying gets the status; ws-b still works.
        retry = await client.request("DELETE", f"{API}/workspaces/ws-a", json={"confirm": "ws-a"})
        assert retry.status_code == 202 and retry.json()["status"] == "deleted"
        assert (await client.get(f"{API}/workflows", headers=_ws("ws-b"))).status_code == 200

    # After a restart on the same database the id is still retired.
    if app.state.service_stores.database_url:
        return
    reopened = create_api_app(db_path=tmp_path / "svc.sqlite3")
    assert reopened.state.workspace_repo.is_workspace_deleted("ws-a")
    assert reopened.state.workspace_repo.get_workspace("ws-a") is None
    with pytest.raises(WorkspaceDeletedError):
        reopened.state.workspace_repo.create_workspace("ws-a", "x", "local-owner")


async def test_live_runs_are_cancelled_in_the_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _app(tmp_path)
    monkeypatch.setattr(deletion, "start_background_purge", lambda state, ws: None)
    async with app.router.lifespan_context(app), _client(app) as client:
        await _create(client, "ws-a")
        a = await _seed(app, client, "ws-a")
        res = await client.request("DELETE", f"{API}/workspaces/ws-a", json={"confirm": "ws-a"})
        assert res.status_code == 202, res.text
        run = await app.state.store.get_run(a.run_ids[0], payloads=False)
        assert run["status"] == "cancelled" and run["cancel_reason"] == "workspace_deleted"
        # Webhooks, schedules and trigger jobs went in the request too.
        assert app.state.trigger_store.list_webhook_configs("ws-a") == []
        assert app.state.trigger_store.list_schedules("ws-a") == []
        # Nothing purged yet: the tick finishes it.
        assert (await workspace_footprint(app.state, a))["nexura_studio_runs"] == 2
        summary = await run_trigger_tick(app.state, budget_seconds=5)
        assert summary["workspace_deletions_done"] == 1, summary
        assert _left(await workspace_footprint(app.state, a)) == {}


async def test_an_interrupted_purge_is_finished_by_the_sweep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _app(tmp_path)
    async with app.router.lifespan_context(app), _client(app) as client:
        await _create(client, "ws-a")
        a = await _seed(app, client, "ws-a")
        agents = app.state.agent_store
        real = agents.purge_workspace
        calls: list[str] = []

        def broken(workspace_id: str) -> int:
            calls.append(workspace_id)
            raise RuntimeError("database went away")

        monkeypatch.setattr(agents, "purge_workspace", broken)
        res = await client.request("DELETE", f"{API}/workspaces/ws-a", json={"confirm": "ws-a"})
        assert res.status_code == 202, res.text
        await _settle(app)
        record = app.state.workspace_repo.get_workspace_deletion("ws-a")
        assert record.status == "purging" and record.attempts == 1
        assert "database went away" in (record.last_error or "")
        left = _left(await workspace_footprint(app.state, a))
        # Other stores were purged; the ownership rows stay so the next pass finds the runs.
        assert set(left) == {"nexura_service_agents", "nexura_service_resource_ownership"}, left

        monkeypatch.setattr(agents, "purge_workspace", real)
        summary = await deletion.sweep_workspace_deletions(app.state)
        assert summary == {"workspace_deletions_done": 1, "workspace_deletions_pending": 0}
        record = app.state.workspace_repo.get_workspace_deletion("ws-a")
        assert record.status == "done" and record.attempts == 2 and record.last_error is None
        assert _left(await workspace_footprint(app.state, a)) == {}
        assert calls == ["ws-a"]
        # Nothing left to sweep.
        assert await deletion.sweep_workspace_deletions(app.state) == {}


async def test_the_final_pass_waits_for_the_lease_ttl(tmp_path: Path) -> None:
    app = _app(tmp_path)
    app.state.workspace_purge_final_delay = 3600.0
    async with app.router.lifespan_context(app), _client(app) as client:
        await _create(client, "ws-a")
        a = await _seed(app, client, "ws-a")
        res = await client.request("DELETE", f"{API}/workspaces/ws-a", json={"confirm": "ws-a"})
        assert res.status_code == 202, res.text
        await _settle(app)
        record = app.state.workspace_repo.get_workspace_deletion("ws-a")
        assert record.status == "purging" and record.attempts == 1 and record.last_error is None
        assert _left(await workspace_footprint(app.state, a)) == {}
        # A run of another instance writes after the first pass; the re-sweep catches it.
        await app.state.store.record_run_start(
            run_id="late-run",
            workflow_id=None,
            spec=WorkflowSpec.from_dict(
                {
                    "name": "late",
                    "entry_node": "n",
                    "nodes": {"n": {"id": "n", "handler": "synthetic_order_lookup"}},
                    "terminal_nodes": ["n"],
                }
            ),
            initial_state={},
            journal_workspace="ws-a",
        )
        a.run_ids.append("late-run")
        retry = await client.request("DELETE", f"{API}/workspaces/ws-a", json={"confirm": "ws-a"})
        assert retry.status_code == 202 and retry.json()["status"] == "purging"
        await _settle(app)
        assert app.state.workspace_repo.get_workspace_deletion("ws-a").status == "purging"
        app.state.workspace_purge_final_delay = 0.0
        await deletion.sweep_workspace_deletions(app.state)
        assert app.state.workspace_repo.get_workspace_deletion("ws-a").status == "done"
        assert _left(await workspace_footprint(app.state, a)) == {}
