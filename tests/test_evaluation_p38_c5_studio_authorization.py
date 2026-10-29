"""Workspace registration and durable Studio capture provenance, using local fakes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from _service_state import store_sql
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from nexura_agents import Agent, FakeModel
from starlette.requests import Request

from nexura_runtime.secrets.keys import generate_master_key
from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.service.api.client import NexuraClient
from nexura_runtime.service.auth.models import Principal
from nexura_runtime.service.extensions import Identity
from nexura_runtime.workflow import WorkflowNode, WorkflowSpec

BASE = "/api/v1/evaluations"
STUDIO = BASE + "/studio"


class OfflineProvider:
    name = "c5-studio-offline"
    is_authenticating = True

    def authenticate(self, request: Request, target_workspace_id: str | None) -> Identity:
        return Identity(
            Principal(principal_id="caller", profile="authenticated-shared-service"),
            target_workspace_id or "default",
            "editor",
        )

    def is_operator(self, request: Request) -> bool:
        return False


async def close_app(app: FastAPI) -> None:
    for name in ("studio_eval_store", "eval_store"):
        store = getattr(app.state, name, None)
        if store is not None:
            if hasattr(store, "close"):
                store.close()
            else:
                store._conn.close()
    await app.state.store.close()


@pytest.mark.parametrize("shared", [False, True])
async def test_registration_policy_discovery_dispatch_and_default_compatibility(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    shared: bool,
) -> None:
    monkeypatch.setenv("NEXURA_MASTER_KEY", generate_master_key())
    app = create_api_app(
        db_path=str(tmp_path / "registry.sqlite"),
        profile="authenticated-shared-service" if shared else "local-owner",
        auth_provider=OfflineProvider() if shared else None,
    )
    called: list[str] = []

    def factory(label: str) -> Any:
        def build(version: str | None) -> Agent:
            called.append(label)
            return Agent(name=label, instructions="Offline fake", model=FakeModel([label]))

        return build

    app.state.eval_agents = {"legacy": factory("legacy"), "collision": factory("legacy")}
    app.state.eval_agents_by_workspace = {
        "default": {"collision": factory("default-scoped")},
        "a": {"collision": factory("a-scoped")},
    }
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1") as c:
            for workspace in ("default", "a", "b"):
                headers = {"X-Workspace-ID": workspace}
                listing = await c.get(STUDIO + "/targets", headers=headers)
                assert listing.status_code == 200, listing.text
                ids = [t["id"] for t in listing.json()["targets"]]
                assert ("legacy" in ids) is (not shared and workspace == "default")
                assert ("collision" in ids) is (workspace in ("default", "a"))
                assert ids.count("local-fake") == 1
                assert (
                    await c.put(
                        STUDIO + "/datasets/own",
                        headers=headers,
                        json={
                            "cases": [{"name": "x", "input": "hi"}],
                        },
                    )
                ).status_code == 200
                assert (
                    await c.post(
                        BASE + "/datasets",
                        headers=headers,
                        json={
                            "dataset_id": "own",
                            "examples": [
                                {"input_data": {"prompt": "hi"}, "simulation": {"turns": ["hi"]}}
                            ],
                        },
                    )
                ).status_code == 200
                before = len(called)
                legacy = await c.post(
                    STUDIO + "/runs",
                    headers=headers,
                    json={
                        "dataset_id": "own",
                        "target": {"kind": "agent", "id": "legacy"},
                    },
                )
                assert legacy.status_code == (200 if not shared and workspace == "default" else 404)
                assert len(called) == before + int(not shared and workspace == "default")
                for path, body in [
                    (
                        STUDIO + "/runs",
                        {"dataset_id": "own", "target": {"kind": "agent", "id": "collision"}},
                    ),
                    (BASE + "/simulate", {"dataset_id": "own", "agent_id": "collision"}),
                ]:
                    before = len(called)
                    response = await c.post(path, headers=headers, json=body)
                    if workspace == "b":
                        assert response.status_code == 404, response.text
                        assert len(called) == before
                    else:
                        assert response.status_code == 200, response.text
                        assert called[-1] == (
                            "default-scoped" if workspace == "default" else "a-scoped"
                        )
    finally:
        await close_app(app)


@pytest.mark.parametrize("historical", [False, True])
async def test_capture_provenance_survives_dataset_changes_and_deleted_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    historical: bool,
) -> None:
    monkeypatch.setenv("NEXURA_MASTER_KEY", generate_master_key())
    app = create_api_app(
        db_path=str(tmp_path / "sources.sqlite"),
        profile="authenticated-shared-service",
        auth_provider=OfflineProvider(),
    )
    spec = WorkflowSpec(
        name="capture",
        entry_node="x",
        terminal_nodes=["x"],
        nodes={"x": WorkflowNode(id="x", handler="synthetic_order_lookup")},
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://127.0.0.1",
            headers={"X-Workspace-ID": "a"},
        ) as http:
            client = NexuraClient("http://127.0.0.1", workspace_id="a", http_client=http)
            await client.workflows.create(spec.to_dict(), workflow_id="wf")
            await app.state.store.record_run_start("source", "wf", spec, {"query": "private input"})
            await app.state.store.update_run("source", "completed", None, {"answer": "private"}, 1)
            repo = app.state.workspace_repo
            repo.record_resource_ownership("run", "source", "a", "caller")
            await client.evaluations.studio.add_case_from_run(
                "captured", "source", dataset_name="captured"
            )
            report = await client.evaluations.studio.create_run(
                "captured", {"kind": "agent", "id": "local-fake"}
            )
            run_id = report["run_id"]
            assert report["report"]["source_run_ids"] == ["source"]
            assert report["report"]["results"][0]["metadata"]["source_run_id"] == "source"
            store = app.state.studio_eval_store
            if historical:
                # Model an already persisted pre-fix report, whose provenance was dropped.
                report["report"].pop("source_run_ids")
                for result in report["report"]["results"]:
                    result["metadata"].pop("source_run_id")
                import json

                # The service's eval tables on either backend (MVP WP8: Postgres with a URL).
                store_sql(
                    store,
                    "UPDATE studio_eval_runs SET report_json=? WHERE workspace_id=? AND run_id=?",
                    json.dumps(report["report"]),
                    "a",
                    run_id,
                )
            assert (await http.get(STUDIO + f"/runs/{run_id}")).status_code == 200
            repo.record_resource_ownership("run", "source", "b", "other")
            for method, path, body in [
                ("GET", "/datasets", None),
                ("PUT", "/datasets/captured", {"cases": [{"name": "new", "input": "hi"}]}),
                ("GET", f"/runs/{run_id}", None),
            ]:
                response = await http.request(method, STUDIO + path, json=body)
                assert response.status_code in (403, 404), response.text
                assert "private input" not in response.text
            # Deleting/replacing the dataset cannot remove copied-report provenance.
            assert (await http.delete(STUDIO + "/datasets/captured")).status_code == 200
            repo.record_resource_ownership("run", "source", "a", "caller")
            response = await http.get(STUDIO + f"/runs/{run_id}")
            assert response.status_code == (403 if historical else 200), response.text
            if historical:
                assert response.json()["detail"]["code"] == "SOURCE_PROVENANCE_UNAVAILABLE"
            repo.delete_resource_ownership("run", "source")
            response = await http.get(STUDIO + f"/runs/{run_id}")
            assert response.status_code in (403, 404), response.text
    finally:
        await close_app(app)
