# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""CLI login and push contracts against an explicitly labelled mock HTTP transport."""

from __future__ import annotations

import io
import json
import stat
import zipfile
from pathlib import Path
from typing import Any

import httpx
import pytest
from nexura_agents.hosted import credentials_path, resolve_connection

from nexura_runtime.cli import hosted
from nexura_runtime.cli.main import main
from nexura_runtime.codegen import workflow_to_python
from nexura_runtime.service.api.client import NexuraApiError
from nexura_runtime.workflow import WorkflowSpec

KEY = "nxk_12345678_" + "a" * 40
SPEC = {
    "spec_version": 1,
    "name": "mock upload",
    "entry_node": "work",
    "nodes": {"work": {"id": "work", "handler": "mock.module", "transitions": {}}},
    "terminal_nodes": ["work"],
}


@pytest.fixture
def mock_cloud(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[httpx.Request]:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("NEXURA_URL", raising=False)
    monkeypatch.delenv("NEXURA_API_KEY", raising=False)
    requests = []

    def handle(req: httpx.Request) -> httpx.Response:
        requests.append(req)
        assert req.headers.get("x-api-key", req.headers.get("authorization")) in (
            KEY,
            "Bearer " + KEY,
        )
        path = req.url.path
        if path.endswith("/whoami"):
            assert "x-workspace-id" not in req.headers
            return httpx.Response(200, json={"workspace_id": "workspace-alpha", "role": "editor"})
        assert req.headers["x-workspace-id"] == "workspace-alpha"
        if path.endswith("/modules"):
            return httpx.Response(
                200, json={"items": [{"module_id": "mock.module", "version": "1.0"}]}
            )
        if path.endswith("/workflows/import") or path.endswith("/workflows/import/python"):
            return httpx.Response(201, json={"workflow_id": "wf-hosted"})
        if path.endswith("/agents/options"):
            return httpx.Response(200, json={"tools": [{"module_id": "mock.module"}]})
        if path.endswith("/agents"):
            return httpx.Response(
                200 if req.method == "GET" else 201,
                json={"items": []} if req.method == "GET" else {"agent_id": "agent-hosted"},
            )
        if path.endswith("/sdk-traces/otlp"):
            return httpx.Response(200, json={})
        raise AssertionError(str(req.url))

    monkeypatch.setattr(
        hosted, "http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handle))
    )
    hosted.save_credentials("https://mock.invalid", KEY)
    return requests


def test_login_0600_whoami_and_logout(
    mock_cloud: Any, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(KEY + "\n"))
    assert main(["login", "--url", "https://mock.invalid", "--api-key-stdin"]) == 0
    assert stat.S_IMODE(credentials_path().stat().st_mode) == 0o600
    assert json.loads(credentials_path().read_text())["api_key"] == KEY
    assert main(["whoami"]) == 0
    assert main(["project", "whoami"]) == 0
    output = capsys.readouterr()
    assert KEY not in output.out + output.err
    assert "workspace-alpha" in output.out and "editor" in output.out
    assert main(["logout"]) == 0
    assert not credentials_path().exists()
    capsys.readouterr()
    assert main(["whoami"]) == 1
    assert "Not logged in" in capsys.readouterr().err


@pytest.mark.parametrize("kind", ["json", "python", "zip", "directory"])
def test_push_workflows(mock_cloud: Any, tmp_path: Path, capsys: Any, kind: str) -> None:
    path = tmp_path / "workflow.json"
    if kind == "python":
        path = tmp_path / "workflow.py"
        path.write_text(workflow_to_python(WorkflowSpec.from_dict(SPEC)))
    elif kind == "zip":
        path = tmp_path / "bundle.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("workflow.json", json.dumps(SPEC))
            archive.writestr("run_workflow.py", "raise Exception('must never execute')")
    else:
        path.write_text(json.dumps(SPEC))
        if kind == "directory":
            path = tmp_path
    assert main(["push", "workflow", str(path)]) == 0
    output = capsys.readouterr().out
    assert "wf-hosted" in output and "https://mock.invalid/workflows/wf-hosted" in output
    assert mock_cloud[-1].url.path.endswith("/import/python" if kind == "python" else "/import")


def test_push_agent_and_traces(mock_cloud: Any, tmp_path: Path, capsys: Any) -> None:
    agent = tmp_path / "agent.json"
    agent.write_text(
        json.dumps({"name": "mock-agent", "instructions": "Be helpful.", "tools": ["mock.module"]})
    )
    assert main(["push", "agent", str(agent)]) == 0
    traces = tmp_path / "otlp.json"
    traces.write_text('{"resourceSpans": []}')
    assert main(["push", "traces", str(traces)]) == 0
    assert json.loads(mock_cloud[-1].content) == {"resourceSpans": []}
    output = capsys.readouterr()
    assert "agent-hosted" in output.out and "Traces uploaded" in output.out
    assert KEY not in output.out + output.err


def test_update_agent_by_name(
    mock_cloud: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    requests = []

    def handle(req: httpx.Request) -> httpx.Response:
        requests.append(req)
        if req.url.path.endswith("whoami"):
            value = {"workspace_id": "alpha", "role": "editor"}
        elif req.url.path.endswith("options"):
            value = {"tools": []}
        elif req.url.path.endswith("agents"):
            value = {"items": [{"agent_id": "old", "name": "mock-agent"}]}
        else:
            value = {"agent_id": "old", "revision": 4}
        return httpx.Response(200, json=value)

    monkeypatch.setattr(
        hosted, "http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handle))
    )
    path = tmp_path / "agent.json"
    path.write_text('{"name":"mock-agent", "instructions":"hello"}')
    assert main(["push", "agent", str(path)]) == 0
    assert (
        requests[-1].method == "PUT" and json.loads(requests[-1].content)["expected_revision"] == 4
    )


def test_custom_code_refused_and_no_key_echo(mock_cloud: Any, tmp_path: Path, capsys: Any) -> None:
    path = tmp_path / "custom.py"
    path.write_text("def custom_tool():\n    raise Exception('must not execute')\n")
    assert main(["push", "workflow", str(path)]) == 1
    path.write_text(
        workflow_to_python(WorkflowSpec.from_dict(SPEC), registry_import="custom:registry")
    )
    assert main(["push", "workflow", str(path)]) == 1
    path = tmp_path / "agent.json"
    path.write_text('{"name":"custom", "tools":[{"python_source":"def fn(): pass"}]}')
    assert main(["push", "agent", str(path)]) == 1
    path.write_text('{"name":"custom", "instructions":"hello", "tools":["local.python.tool"]}')
    assert main(["push", "agent", str(path)]) == 1
    output = capsys.readouterr()
    assert "Custom Python tools" in output.err and KEY not in output.out + output.err
    assert not any(req.method in ("POST", "PUT") for req in mock_cloud)


def test_credentials_never_cross_origins(mock_cloud: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXURA_URL", "https://other.invalid")
    with pytest.raises(ValueError, match="NEXURA_API_KEY"):
        resolve_connection()
    monkeypatch.setenv("NEXURA_API_KEY", "override")
    assert resolve_connection() == ("https://other.invalid", "override")


def test_invalid_workflow_file_is_a_clean_error(
    mock_cloud: Any, tmp_path: Path, capsys: Any
) -> None:
    # A spec that fails validation must be reported, not crash with a traceback.
    path = tmp_path / "broken.json"
    path.write_text('{"id": "broken", "nodes": [], "edges": []}')
    assert main(["push", "workflow", str(path)]) == 1
    output = capsys.readouterr()
    assert output.err.startswith("Error: ") and "Traceback" not in output.err
    assert not any(req.method in ("POST", "PUT") for req in mock_cloud)


def test_server_refusal_names_the_code_and_fields_only(capsys: Any) -> None:
    error = NexuraApiError(
        422,
        "AGENT_INVALID",
        "secret-echo",
        {"field_errors": [{"field": "model", "message": "secret-echo"}, {"field": "x y"}]},
    )
    assert hosted._api_error_summary(error) == "HTTP 422; AGENT_INVALID; check: model"
    assert "secret-echo" not in hosted._api_error_summary(error)
    assert hosted._api_error_summary(NexuraApiError(500, "<b>", "x")) == "HTTP 500"
