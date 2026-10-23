"""Wire-contract checks for the C5 evaluation and C6 migration clients.

The contract checks use a mock transport; the /migrate checks at the end also run both
first-party clients against a real local service (ASGI for Python, a loopback uvicorn
server for the TypeScript client). Every handler is a local fake.
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.service.api.client import NexuraApiError, NexuraClient
from nexura_runtime.workflow import ModuleRegistry, WorkflowNode, WorkflowSpec

EXAMPLE = {
    "input_data": {},
    "expected_trajectory": [{"name": "lookup", "args": {"amount": 0}}],
}
GRADERS = [{"type": "tool_argument_accuracy", "argument_rules": {"amount": {}}}]
CONFIG = {"sampling_rate": 0, "seed": 0, "start_time": 0, "end_time": 10}
TRACE = {
    "run_id": "recorded",
    "timestamp": 0,
    "output": None,
    "events": [{"type": "tool_started", "args": {"amount": 0}}],
    "messages": [],
}

CASES = [
    ("evaluations", "graders", {}, "GET", "/evaluations/graders", None),
    ("evaluations", "import_trace", {"trace": TRACE}, "POST", "/evaluations/traces", TRACE),
    (
        "evaluations",
        "evaluate_online",
        {"example": EXAMPLE, "graders": GRADERS, "config": CONFIG},
        "POST",
        "/evaluations/online",
        {"example": EXAMPLE, "graders": GRADERS, "config": CONFIG},
    ),
    (
        "evaluations",
        "simulate",
        {"dataset_id": "ds", "dataset_version": "v1", "agent_id": "fake", "graders": GRADERS},
        "POST",
        "/evaluations/simulate",
        {"dataset_id": "ds", "dataset_version": "v1", "agent_id": "fake", "graders": GRADERS},
    ),
    (
        "runs",
        "migrate",
        {
            "run_id": "a/b?c#d",
            "expected_definition_digest": "sha256-saved",
            "resume": False,
            "on_uncertain": "fail",
        },
        "POST",
        "/runs/a%2Fb%3Fc%23d/migrate",
        {"expected_definition_digest": "sha256-saved", "resume": False, "on_uncertain": "fail"},
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("subclient,method,kwargs,verb,path,payload", CASES)
async def test_p38_wire_contract(
    subclient: str, method: str, kwargs: dict[str, Any], verb: str, path: str, payload: Any
) -> None:
    response_data = {"run_id": "reply", "metadata": {"nested": [0, False, None]}}

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.method == verb
        assert request.url.raw_path.decode() == f"/api/v1{path}"
        assert request.headers["X-Workspace-ID"] == "test-workspace"
        assert request.headers["Authorization"] == "Bearer fake-key"
        assert request.headers["X-Nexura-Session"] == "fake-session"
        assert (json.loads(request.content) if request.content else None) == payload
        return httpx.Response(200, json=response_data)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as transport:
        client = NexuraClient(
            "http://mock-service",
            api_key="fake-key",
            workspace_id="test-workspace",
            session_token="fake-session",
            http_client=transport,
        )
        assert await getattr(getattr(client, subclient), method)(**kwargs) == response_data


@pytest.mark.asyncio
async def test_p38_defaults_and_explicit_empty_graders() -> None:
    bodies = []

    def handle(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as transport:
        client = NexuraClient("http://mock-service", http_client=transport)
        await client.evaluations.evaluate_online(EXAMPLE)
        await client.evaluations.simulate("ds")
        await client.evaluations.evaluate_online(EXAMPLE, graders=[], config={})
        await client.evaluations.simulate("ds", graders=[])
        await client.runs.migrate("run")

    assert bodies == [
        {"example": EXAMPLE},
        {"dataset_id": "ds"},
        {"example": EXAMPLE, "graders": [], "config": {}},
        {"dataset_id": "ds", "graders": []},
        {"resume": True, "on_uncertain": "pause"},
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("subclient,method,kwargs,verb,path,payload", CASES)
async def test_p38_structured_errors(
    subclient: str, method: str, kwargs: dict[str, Any], verb: str, path: str, payload: Any
) -> None:
    code = "WORKFLOW_MIGRATION_REFUSED" if method == "migrate" else "INVALID_GRADER"
    status = 409 if method == "migrate" else 422

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"detail": {"code": code, "message": "refused"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as transport:
        client = NexuraClient("http://mock-service", http_client=transport)
        with pytest.raises(NexuraApiError) as caught:
            await getattr(getattr(client, subclient), method)(**kwargs)
        assert (caught.value.status_code, caught.value.code, caught.value.message) == (
            status,
            code,
            "refused",
        )


# ------------------------------------------------------------------ real service (C6 /migrate)
def _migration_app(tmp_path: Path) -> Any:
    """A local service whose saved ``client-migration`` workflow has a v1 -> v2 hook."""
    registry = ModuleRegistry()
    registry.register("local_fake_pay", lambda state: {}, effect="pure")

    def spec(version: int) -> WorkflowSpec:
        return WorkflowSpec(
            name="client-migration",
            version=version,
            entry_node="pay",
            nodes={"pay": WorkflowNode(id="pay", handler="local_fake_pay", requires_approval=True)},
        )

    app = create_api_app(registry=registry, db_path=tmp_path / "api.db", profile="local-owner")
    app.state.workflow_migrations = {
        "client-migration": {(1, 2): lambda a, b, state: {**state, "amount": 500}}
    }
    app.state.test_specs = {version: spec(version) for version in (1, 2)}
    return app


def _paused_run_on_v1_with_v2_saved(app: Any, http: Any) -> tuple[str, str]:
    """Start a v1 run (paused at its gate), then save v2; returns the run id and v2 digest."""
    v1, v2 = app.state.test_specs[1], app.state.test_specs[2]
    saved = http.post(
        "/api/v1/workflows", json={"name": "client-migration", "spec": v1.to_dict()}
    ).json()
    run_id = http.post(
        "/api/v1/runs",
        json={"workflow_id": saved["workflow_id"], "initial_state": {"amount": 5}},
    ).json()["run_id"]
    updated = http.put(
        f"/api/v1/workflows/{saved['workflow_id']}",
        json={"spec": v2.to_dict(), "expected_revision": saved["revision"]},
    )
    assert updated.status_code == 200, updated.text
    return str(run_id), v2.canonical_hash()


@pytest.mark.asyncio
async def test_p38_python_client_migrates_a_real_service_run(tmp_path: Path) -> None:
    app = _migration_app(tmp_path)
    with TestClient(app) as tc:
        run_id, digest = _paused_run_on_v1_with_v2_saved(app, tc)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://local-asgi"
        ) as http:
            client = NexuraClient("http://local-asgi", http_client=http)
            with pytest.raises(NexuraApiError) as stale:
                await client.runs.migrate(run_id, expected_definition_digest="sha256-stale")
            assert (stale.value.status_code, stale.value.code) == (409, "SPEC_MISMATCH")
            run = await client.runs.migrate(run_id, expected_definition_digest=digest)
            assert (run["workflow_version"], run["started_workflow_version"]) == (2, 1)
            assert run["definition_digest"] == digest
            assert run["status"] == "pending_approval"  # approvals stay pending
            assert run["current_state"]["amount"] == 500
            again = await client.runs.migrate(run_id, resume=False, on_uncertain="fail")
            assert again["workflow_version"] == 2


def test_p38_typescript_client_migrates_a_real_service_run(tmp_path: Path) -> None:
    project = Path(__file__).resolve().parents[3] / "clients" / "typescript"
    if shutil.which("node") is None or not (project / "node_modules").is_dir():
        pytest.skip("Node.js and clients/typescript/node_modules are needed")
    build = subprocess.run(
        ["npm", "run", "build"], cwd=project, capture_output=True, text=True, timeout=120
    )
    assert build.returncode == 0, build.stdout + build.stderr
    app = _migration_app(tmp_path)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    script = tmp_path / "migrate.mjs"
    module = json.dumps((project / "dist" / "index.js").as_uri())
    script.write_text(
        f"""
import {{ NexuraClient, NexuraApiError }} from {module};
const [baseUrl, runId, digest, sessionToken] = process.argv.slice(2);
const client = new NexuraClient({{ baseUrl, sessionToken }});
try {{
  await client.runs.migrate(runId, {{ expectedDefinitionDigest: 'sha256-stale' }});
  throw new Error('a stale digest was accepted');
}} catch (error) {{
  if (!(error instanceof NexuraApiError) || error.statusCode !== 409) throw error;
}}
const run = await client.runs.migrate(runId, {{ expectedDefinitionDigest: digest }});
const again = await client.runs.migrate(runId);
console.log(JSON.stringify({{
  version: run.workflow_version, started: run.started_workflow_version,
  digest: run.definition_digest, status: run.status, amount: run.current_state.amount,
  again: again.workflow_version,
}}));
""",
        encoding="utf-8",
    )
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        assert server.started, "the local service did not start"
        # Loopback requests that change state carry the local session token.
        token = str(app.state.session_token)
        with httpx.Client(
            base_url=f"http://127.0.0.1:{port}", headers={"X-Nexura-Session": token}
        ) as http:
            run_id, digest = _paused_run_on_v1_with_v2_saved(app, http)
        result = subprocess.run(
            ["node", str(script), f"http://127.0.0.1:{port}", run_id, digest, token],
            capture_output=True,
            text=True,
            timeout=60,
        )
    finally:
        server.should_exit = True
        thread.join(timeout=5.0)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {
        "version": 2,
        "started": 1,
        "digest": digest,
        "status": "pending_approval",
        "amount": 500,
        "again": 2,
    }
