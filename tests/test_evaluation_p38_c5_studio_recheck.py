"""Promoted independent C5 Studio authorization re-checks; original assertions preserved."""

import json
from collections.abc import Awaitable
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from nexura_agents import Agent, FakeModel
from starlette.requests import Request

from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.service.api.client import NexuraApiError, NexuraClient
from nexura_runtime.service.auth.models import Principal
from nexura_runtime.service.extensions import Identity
from nexura_runtime.workflow import WorkflowNode, WorkflowSpec


@pytest.fixture(autouse=True)
def disposable_master_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from nexura_runtime.secrets.keys import generate_master_key

    monkeypatch.setenv("NEXURA_MASTER_KEY", generate_master_key())


class OfflineIdentity:
    """Local authenticated identities; no enterprise implementation or network."""

    name = "independent-c5-recheck-offline"
    is_authenticating = True
    resources: list[str] | None = None

    def authenticate(self, request: Request, target_workspace_id: str | None) -> Identity:
        workspace = request.headers.get("authorization", "Bearer a").removeprefix("Bearer ")
        assert workspace in ("a", "b")
        return Identity(
            Principal(
                principal_id=f"caller-{workspace}",
                profile="authenticated-shared-service",
                scopes=["run", "edit"],
                resources=self.resources,
            ),
            workspace,
            "editor",
        )

    def is_operator(self, request: Request) -> bool:
        return False


async def close_app(app: FastAPI) -> None:
    for field in ("eval_store", "studio_eval_store"):
        if store := getattr(app.state, field, None):
            if hasattr(store, "close"):
                store.close()
            else:
                store._conn.close()
    await app.state.store.close()


async def denied(awaitable: Awaitable[Any], statuses: tuple[int, ...] = (403, 404)) -> None:
    with pytest.raises(NexuraApiError) as error:
        await awaitable
    assert error.value.status_code in statuses, str(error.value)


async def test_studio_must_not_bypass_simulation_legacy_registration_boundary(
    tmp_path: Path,
) -> None:
    """Same host registration and shared caller: /simulate refuses, Studio must too."""
    app = create_api_app(
        db_path=str(tmp_path / "legacy.sqlite"),
        profile="authenticated-shared-service",
        auth_provider=OfflineIdentity(),
    )
    called: list[str] = []

    def legacy_factory(version: str | None) -> Agent:
        called.append("legacy-dispatched")
        return Agent(
            name="legacy-private",
            instructions="offline private fixture",
            model=FakeModel(handler=lambda m, t: "default workspace private capture"),
        )

    app.state.eval_agents = {"legacy-private": legacy_factory}
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as http:
            client = NexuraClient(
                "http://127.0.0.1", api_key="b", workspace_id="b", http_client=http
            )
            await client.evaluations.create_dataset(
                "own", "1", [{"input_data": {"query": "hi"}, "simulation": {"turns": ["hi"]}}]
            )
            await client.evaluations.studio.put_dataset("own", cases=[{"name": "x", "input": "hi"}])
            await denied(client.evaluations.simulate("own", agent_id="legacy-private"), (404,))
            assert called == []
            try:
                report = await client.evaluations.studio.create_run(
                    "own", {"kind": "agent", "id": "legacy-private"}
                )
            except NexuraApiError as error:
                assert error.status_code in (403, 404)
                assert called == []
            else:
                reread = await client.evaluations.studio.get_run(report["run_id"])
                evidence = {"called": called, "report": report, "reread": reread}
                (tmp_path / "legacy-evidence.json").write_text(json.dumps(evidence, indent=2))
                pytest.fail(
                    "Studio dispatched refused legacy agent and returned/persisted its output: "
                    f"{evidence}"
                )
    finally:
        await close_app(app)


async def test_studio_from_run_capture_rechecks_changed_source_ownership(tmp_path: Path) -> None:
    app = create_api_app(
        db_path=str(tmp_path / "from-run.sqlite"),
        profile="authenticated-shared-service",
        auth_provider=OfflineIdentity(),
    )
    spec = WorkflowSpec(
        name="capture",
        entry_node="x",
        terminal_nodes=["x"],
        nodes={"x": WorkflowNode(id="x", handler="synthetic_order_lookup")},
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as http:
            client = NexuraClient(
                "http://127.0.0.1", api_key="a", workspace_id="a", http_client=http
            )
            await client.workflows.create(spec.to_dict(), workflow_id="source-wf")
            await app.state.store.record_run_start("source-run", "source-wf", spec, {"query": "hi"})
            await app.state.store.update_run(
                "source-run", "completed", None, {"answer": "source run confidential capture"}, 1
            )
            repo = app.state.workspace_repo
            repo.record_resource_ownership("run", "source-run", "a", "caller-a")
            await client.evaluations.studio.add_case_from_run(
                "capture", "source-run", dataset_name="capture"
            )
            report = await client.evaluations.studio.create_run(
                "capture", {"kind": "agent", "id": "local-fake"}
            )
            repo.record_resource_ownership("run", "source-run", "b", "caller-b")
            await denied(client.runs.get("source-run"))
            outcomes: dict[str, Any] = {}
            for name, call in [
                ("dataset", client.evaluations.studio.get_dataset("capture")),
                ("export", client.evaluations.studio.export_dataset("capture")),
                ("report", client.evaluations.studio.get_run(report["run_id"])),
                ("list", client.evaluations.studio.list_runs()),
                ("compare", client.evaluations.studio.compare(report["run_id"], report["run_id"])),
                (
                    "grade",
                    client.evaluations.studio.create_run(
                        "capture", {"kind": "agent", "id": "local-fake"}
                    ),
                ),
            ]:
                try:
                    outcomes[name] = {"status": 200, "body": await call}
                except NexuraApiError as error:
                    outcomes[name] = {"status": error.status_code, "code": error.code}
            (tmp_path / "reauthorization-evidence.json").write_text(json.dumps(outcomes, indent=2))
            assert all(row["status"] in (403, 404) for row in outcomes.values()), outcomes
    finally:
        await close_app(app)
