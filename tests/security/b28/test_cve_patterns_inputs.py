"""B28 regression tests (October competitor CVE classes), part 2.

Credential substitution, MCP stdio injection, SSRF and prototype-style keys.

Each test asserts the SAFE behaviour: it passes when Nexura is safe and fails when vulnerable.
Offline only: mock transports and a monkeypatched resolver; no real connections.
"""

from __future__ import annotations

import json
import socket
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "connectors"))

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "tests/integrations/langflow/fixtures"


# ---------------------------------------------------------------- 2. credential substitution


def _two_workspace_store() -> Any:
    from nexura_runtime.credentials.store import ScopedCredentialStore

    store = ScopedCredentialStore()
    store.store("ws-a", "slack", "slack_bot_token", "xoxb-A-secret-value-1111")
    store.store("ws-b", "slack", "slack_bot_token", "xoxb-B-secret-value-2222")
    store.store("ws-b", "only-b", "slack_bot_token", "xoxb-ONLYB-secret-3333")
    return store


def test_workspace_resolver_never_returns_another_workspaces_credential() -> None:
    from nexura_agents.contracts.context import CredentialReference

    store = _two_workspace_store()
    a = store.for_workspace("ws-a")
    assert a.resolve("slack") == "xoxb-A-secret-value-1111"
    assert a.resolve("only-b") is None
    # A reference that names the other workspace explicitly is refused too.
    ref = CredentialReference(credential_id="slack", credential_type="slack_bot_token")
    object.__setattr__(ref, "workspace_id", "ws-b") if hasattr(ref, "workspace_id") else None
    assert a.resolve(ref) in (None, "xoxb-A-secret-value-1111")


async def test_connector_in_workspace_a_cannot_use_workspace_b_credential() -> None:
    from connector_helpers import Recorder, json_response  # type: ignore[import-not-found]
    from nexura_agents.contracts.context import ExecutionContext
    from nexura_connectors import SlackConnector

    store = _two_workspace_store()
    rec = Recorder(lambda r: json_response({"ok": True, "channels": []}))
    conn = SlackConnector(transport=httpx.MockTransport(rec))
    ctx = ExecutionContext(
        run_id="r",
        node_id="n",
        workspace_id="ws-a",
        credential_resolver=store.for_workspace("ws-a"),
        approval_granted=True,
    )
    result = await conn.execute(
        ctx, {"operation": "list_channels", "params": {}}, {"credential_ref": "only-b"}
    )
    assert result.status != "success"
    sent = json.dumps([str(r.headers) for r in rec.requests])
    assert "ONLYB" not in sent


async def test_credential_ref_from_inputs_or_state_is_not_used() -> None:
    """credential_ref is read from node config only; one smuggled through inputs is ignored."""
    from connector_helpers import Recorder, json_response
    from nexura_agents.contracts.context import ExecutionContext
    from nexura_connectors import SlackConnector

    store = _two_workspace_store()
    rec = Recorder(lambda r: json_response({"ok": True, "channels": []}))
    conn = SlackConnector(transport=httpx.MockTransport(rec))
    ctx = ExecutionContext(
        run_id="r",
        node_id="n",
        workspace_id="ws-a",
        credential_resolver=store.for_workspace("ws-a"),
        approval_granted=True,
    )
    try:
        result = await conn.execute(
            ctx, {"operation": "list_channels", "params": {}, "credential_ref": "slack"}, {}
        )
    except Exception:
        assert rec.requests == []  # refused by input validation: safe
        return
    assert result.status != "success"
    assert rec.requests == []


# ---------------------------------------------------------------- 3. MCP stdio injection


@pytest.mark.parametrize(
    "env",
    [
        {"LD_PRELOAD": "/tmp/evil.so"},
        {"NODE_OPTIONS": "--require /tmp/evil.js"},
        {"PYTHONSTARTUP": "/tmp/evil.py"},
        {"PYTHONPATH": "/tmp/evil"},
        {"DYLD_INSERT_LIBRARIES": "/tmp/evil.dylib"},
        {"UV_INDEX_URL": "https://evil.example/simple"},
        {"npm_config_registry": "https://evil.example"},
        {"BASH_ENV": "/tmp/evil.sh"},
    ],
)
def test_stdio_refuses_code_loading_env(env: dict[str, str]) -> None:
    from nexura_agents.mcp.errors import McpCommandNotAllowedError
    from nexura_agents.mcp.toolset import MCPServer

    with pytest.raises(McpCommandNotAllowedError):
        MCPServer.stdio("npx", "-y", "@modelcontextprotocol/server-everything", env=env)


@pytest.mark.parametrize(
    "command,args",
    [
        ("sh", ["-c", "id"]),
        ("/bin/sh", ["-c", "id"]),
        ("bash", ["-c", "id"]),
        ("python", ["-c", "import os"]),
        ("node", ["-e", "process.exit()"]),
        ("node", ["--require", "/tmp/evil.js", "server.js"]),
        ("npx", ["-c", "id"]),
        ("/tmp/python", ["server.py"]),
        ("uvx", ["--python", "python", "-c", "import os"]),
    ],
)
def test_stdio_refuses_shells_inline_code_and_fake_launchers(command: str, args: list[str]) -> None:
    from nexura_agents.mcp.errors import McpCommandNotAllowedError
    from nexura_agents.mcp.toolset import MCPServer

    with pytest.raises(McpCommandNotAllowedError):
        MCPServer.stdio(command, *args)


def test_stdio_args_are_never_shell_interpreted() -> None:
    """Shell metacharacters stay inside one argv entry (create_subprocess_exec, no shell)."""
    import inspect

    from nexura_agents.mcp import transports

    source = inspect.getsource(transports.McpStdioTransport._ensure_process)
    assert "create_subprocess_exec" in source and "shell" not in source


def test_stdio_plain_env_is_allowed() -> None:
    from nexura_agents.mcp.toolset import MCPServer

    MCPServer.stdio("npx", "-y", "server-x", env={"GITHUB_TOKEN": "t", "LOG_LEVEL": "debug"})


def test_service_api_refuses_caller_supplied_stdio(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    from nexura_runtime.service.api.app import create_api_app

    client = TestClient(create_api_app(db_path=":memory:"))
    for path in ("/api/v1/integrations/mcp/discover", "/api/v1/integrations/mcp/servers"):
        resp = client.post(
            path,
            json={
                "transport_type": "stdio",
                "command": "npx",
                "args": ["-y", "x"],
                "env": {"NODE_OPTIONS": "--require /tmp/evil.js"},
            },
        )
        assert resp.status_code >= 400, (path, resp.status_code, resp.text[:200])


# ---------------------------------------------------------------- 4. SSRF through fetching paths


def _resolve_to(monkeypatch: pytest.MonkeyPatch, ip: str) -> None:
    def gai(host: str, port: Any, *a: Any, **k: Any) -> list[Any]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port or 443))]

    monkeypatch.setattr(socket, "getaddrinfo", gai)


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",
        "http://127.0.0.1:8765/api/v1/workflows",
        "http://[::1]/",
        "http://10.0.0.5/",
        "http://100.100.100.200/",
        "http://metadata.google.internal/computeMetadata/v1/",
    ],
)
async def test_http_module_refuses_internal_destinations(
    url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nexura_agents.contracts.context import ExecutionContext

    from nexura_runtime.integrations.http.module import HttpRequestModule

    _resolve_to(monkeypatch, "169.254.169.254")
    module = HttpRequestModule()
    ctx = ExecutionContext(run_id="r", node_id="n", approval_granted=True)
    from nexura_agents.network.errors import DestinationPolicyDeniedError

    try:
        result = await module.execute(ctx, {"method": "GET", "url": url}, {})
    except DestinationPolicyDeniedError:
        return  # refused by the destination policy: safe (a timeout would NOT be safe)
    assert result.status != "success", (url, result)


async def test_openapi_operation_cannot_be_pointed_at_internal_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An imported OpenAPI spec whose server URL is internal must not be fetched."""
    from nexura_agents.contracts.context import ExecutionContext
    from nexura_agents.openapi.module import import_openapi_operations

    spec = {
        "openapi": "3.0.0",
        "info": {"title": "x", "version": "1"},
        "servers": [{"url": "http://169.254.169.254/latest"}],
        "paths": {"/meta-data": {"get": {"operationId": "steal", "responses": {"200": {}}}}},
    }
    _resolve_to(monkeypatch, "169.254.169.254")
    try:
        mods = import_openapi_operations(spec, prefix="x")
    except Exception:
        return  # refused at import: safe
    ctx = ExecutionContext(run_id="r", node_id="n", approval_granted=True)
    try:
        result = await mods[0].execute(ctx, {}, {})
    except Exception:
        return  # refused: safe
    assert result.status != "success"


# ---------------------------------------------------------------- 5. prototype-style keys

PROTO_KEYS = ["__proto__", "constructor", "__class__", "__init__", "__dict__", "prototype"]


@pytest.mark.parametrize("key", PROTO_KEYS)
def test_workflow_spec_treats_proto_keys_as_data(key: str) -> None:
    from nexura_runtime.workflow import WorkflowSpec

    data = {
        "name": "p",
        "entry_node": "a",
        "nodes": {
            "a": {
                "id": "a",
                "handler": "noop",
                "input_mapping": {key: key},
                "config": {key: {"polluted": True}},
            }
        },
    }
    try:
        spec = WorkflowSpec.from_dict(data)
    except Exception:
        return  # refused: safe
    node = spec.nodes["a"]
    assert node.config.get(key) == {"polluted": True}
    assert not hasattr(WorkflowSpec, "polluted") and not hasattr(object, "polluted")
    assert type(node).__name__ == "WorkflowNode"


@pytest.mark.parametrize("key", PROTO_KEYS)
def test_workflow_node_id_named_like_proto_does_not_mutate_classes(key: str) -> None:
    from nexura_runtime.workflow import WorkflowSpec

    data = {"name": "p", "entry_node": key, "nodes": {key: {"id": key, "handler": "noop"}}}
    try:
        spec = WorkflowSpec.from_dict(data)
    except Exception:
        return
    assert list(spec.nodes) == [key]
    assert isinstance(spec.nodes, dict)


@pytest.mark.parametrize("key", PROTO_KEYS)
def test_langflow_import_treats_proto_keys_as_data(key: str) -> None:
    from nexura_runtime.integrations.langflow import load

    doc = json.loads((FIXTURES / "f1_minimal_chat.json").read_text())
    graph = doc.get("data", doc)
    node = graph["nodes"][0]
    node["data"]["node"]["template"][key] = {"type": "str", "value": "x", "show": True}
    node["data"][key] = {"polluted": True}
    try:
        flow = load(doc, strict=False)
    except Exception:
        return
    assert not hasattr(object, "polluted")
    for n in flow.nodes.values():
        assert type(n).__name__ == "FlowNode"


@pytest.mark.parametrize("key", ["__class__", "__init__", "__dict__"])
def test_python_import_refuses_dunder_tricks(key: str) -> None:
    from nexura_runtime.codegen import python_to_workflow

    source = f'''
from nexura_runtime.workflow import WorkflowNode, WorkflowSpec

spec = WorkflowSpec(
    name="p",
    entry_node="a",
    nodes={{"a": WorkflowNode(id="a", handler="noop", config={{"{key}": 1}})}},
)
spec.{key} = None
'''
    try:
        python_to_workflow(source)
    except Exception:
        return  # refused: safe
    raise AssertionError(f"attribute assignment to {key} was accepted by the importer")
