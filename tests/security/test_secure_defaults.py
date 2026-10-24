"""Secure-default tests backing SECURITY_MODEL.md (offline; fakes only)."""

from __future__ import annotations

import ast
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
from nexura_agents.mcp.errors import McpCommandNotAllowedError
from nexura_agents.mcp.transports import McpStdioTransport
from nexura_studio.server import create_app as create_studio_app

from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.service.api.bind_guard import (
    INSECURE_ENV_VALUE,
    INSECURE_ENV_VAR,
    assert_safe_bind,
    is_loopback_bind,
)
from nexura_runtime.service.auth import UnauthenticatedNetworkError

ROOT = Path(__file__).resolve().parents[2]
SPEC: dict[str, Any] = {
    "name": "w",
    "entry_node": "n",
    "terminal_nodes": ["n"],
    "nodes": {"n": {"id": "n", "handler": "synthetic_order_lookup", "transitions": {}}},
}


def _client(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


@pytest.fixture(autouse=True)
def _no_optout_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(INSECURE_ENV_VAR, raising=False)
    monkeypatch.delenv("NEXURA_STUDIO_TOKEN", raising=False)


# ---------------------------------------------------------------- (a) bind guard


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "[::1]", "127.0.0.2", None])
def test_loopback_hosts_are_safe(host: str | None) -> None:
    assert is_loopback_bind(host)
    assert_safe_bind(host, authenticated=False)


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "", "192.168.1.5", "example.com", "10.0.0.1"])
def test_non_loopback_hosts_are_unsafe(host: str) -> None:
    assert not is_loopback_bind(host)
    with pytest.raises(UnauthenticatedNetworkError, match="refuses to listen"):
        assert_safe_bind(host, authenticated=False)


def test_bind_guard_opt_outs_are_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    assert_safe_bind("0.0.0.0", authenticated=True)
    assert_safe_bind("0.0.0.0", authenticated=False, allow_unauthenticated_network=True)
    monkeypatch.setenv(INSECURE_ENV_VAR, "1")  # a casual value is not the opt-out
    with pytest.raises(UnauthenticatedNetworkError):
        assert_safe_bind("0.0.0.0", authenticated=False)
    monkeypatch.setenv(INSECURE_ENV_VAR, INSECURE_ENV_VALUE)
    assert_safe_bind("0.0.0.0", authenticated=False)


def test_service_refuses_unauthenticated_network_bind(tmp_path: Path) -> None:
    with pytest.raises(UnauthenticatedNetworkError, match="127.0.0.1"):
        create_api_app(db_path=tmp_path / "s.db", listen_host="0.0.0.0")
    # Loopback and the loud opt-out still start.
    create_api_app(db_path=tmp_path / "a.db", listen_host="127.0.0.1")
    create_api_app(
        db_path=tmp_path / "b.db", listen_host="0.0.0.0", allow_unauthenticated_network=True
    )


def test_studio_refuses_unauthenticated_network_bind(tmp_path: Path) -> None:
    with pytest.raises(UnauthenticatedNetworkError, match="NEXURA_STUDIO_TOKEN"):
        create_studio_app(db_path=tmp_path / "s.db", listen_host="0.0.0.0")
    create_studio_app(db_path=tmp_path / "a.db", listen_host="127.0.0.1")
    create_studio_app(db_path=tmp_path / "b.db", listen_host="0.0.0.0", api_token="t" * 24)


async def test_studio_token_is_enforced_when_configured(tmp_path: Path) -> None:
    token = "s3cret-token-value"
    app = create_studio_app(db_path=tmp_path / "s.db", listen_host="0.0.0.0", api_token=token)
    async with _client(app) as c:
        for headers in ({}, {"Authorization": "Bearer wrong"}, {"x-nexura-token": "nope"}):
            r = await c.get("/api/v1/modules", headers=headers)
            assert r.status_code == 401
            assert r.json()["error"]["code"] == "UNAUTHENTICATED"
            assert token not in r.text and "wrong" not in r.text
        assert (await c.get("/openapi.json")).status_code == 401
        ok = await c.get("/api/v1/modules", headers={"Authorization": f"Bearer {token}"})
        assert ok.status_code == 200
        assert (
            await c.get("/api/v1/modules", headers={"x-nexura-token": token})
        ).status_code == 200
        assert (await c.get("/healthz")).status_code == 200


async def test_studio_token_from_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NEXURA_STUDIO_TOKEN", "env-token-123456")
    app = create_studio_app(db_path=tmp_path / "s.db", listen_host="0.0.0.0")
    async with _client(app) as c:
        assert (await c.get("/api/v1/modules")).status_code == 401


async def test_studio_loopback_default_needs_no_token(tmp_path: Path) -> None:
    app = create_studio_app(db_path=tmp_path / "s.db")
    async with _client(app) as c:
        assert (await c.get("/api/v1/modules")).status_code == 200


def test_studio_cli_refuses_unauthenticated_network_bind(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from nexura_studio import cli

    monkeypatch.setattr(
        sys, "argv", ["nexura-studio", "--host", "0.0.0.0", "--db", str(tmp_path / "s.db")]
    )
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    assert "refuses to listen" in capsys.readouterr().err


# ---------------------------------------------------------------- (b) no code execution

_FORBIDDEN_CALLS = {"eval", "exec", "compile", "__import__"}
# Allowlist: (relative path, call) pairs that are reviewed and not reachable from a spec.
_ALLOWED_CALLS: set[tuple[str, str]] = set()
_PROCESS_CALLS = {"system", "popen", "Popen", "create_subprocess_shell", "create_subprocess_exec"}
# Reviewed process launches: sandbox worker (isolated), MCP stdio (allowlisted), diagnostics.
_ALLOWED_PROCESS_FILES = {
    # Fixed Python/-I workers, constant -c entries, empty env, cwd=/, JSON pipes,
    # close_fds and timeouts; SQLite identity proof and read-only export inspection.
    "src/nexura_runtime/retention.py",
    "src/nexura_runtime/integrations/sandbox/worker.py",
    # The gVisor launcher (moved out of worker.py into the nexura-sandbox package).
    "packages/nexura-sandbox/src/nexura_sandbox/gvisor_worker.py",
    "packages/nexura-sandbox/src/nexura_sandbox/bubblewrap_worker.py",
    # Fixed Python/-I runner, no shell, empty env and private temporary cwd.
    "packages/nexura-sandbox/src/nexura_sandbox/wasi_worker.py",
    # Fixed Python/-I compiler, no shell, empty env and verified-runtime staging cwd.
    "packages/nexura-sandbox/src/nexura_sandbox/wasi_runtime.py",
    "packages/nexura-agents/src/nexura_agents/mcp/transports.py",
    "src/nexura_runtime/release/diagnostics.py",
}


def _python_files() -> list[Path]:
    files = []
    for base in (ROOT / "src", ROOT / "packages"):
        files += [p for p in base.rglob("*.py") if ".venv" not in p.parts]
    return files


def test_no_eval_exec_compile_in_packages() -> None:
    offenders = []
    for path in _python_files():
        rel = str(path.relative_to(ROOT))
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in _FORBIDDEN_CALLS
                and (rel, node.func.id) not in _ALLOWED_CALLS
            ):
                offenders.append(f"{rel}:{node.lineno} {node.func.id}()")
    assert not offenders, offenders


def test_process_launches_are_confined_to_reviewed_files() -> None:
    offenders = []
    for path in _python_files():
        rel = str(path.relative_to(ROOT))
        if rel in _ALLOWED_PROCESS_FILES:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in {"os", "subprocess", "asyncio"}
                and node.func.attr in _PROCESS_CALLS | {"run", "check_output", "check_call"}
                and not (node.func.value.id == "asyncio" and node.func.attr == "run")
            ):
                offenders.append(f"{rel}:{node.lineno} {node.func.value.id}.{node.func.attr}")
    assert not offenders, offenders


async def test_spec_cannot_name_builtins_or_dotted_callables(tmp_path: Path) -> None:
    app = create_api_app(db_path=tmp_path / "s.db")
    async with _client(app) as c:
        for handler in ("eval", "builtins.eval", "os.system", "subprocess:run", "__import__"):
            spec = {**SPEC, "nodes": {"n": {"id": "n", "handler": handler, "transitions": {}}}}
            res = await c.post("/api/v1/workflows/validate", json={"spec": spec})
            assert res.status_code == 200
            assert res.json()["valid"] is False, handler
            created = await c.post("/api/v1/workflows", json={"workflow_id": "evil", "spec": spec})
            assert created.status_code >= 400, handler


async def test_code_in_node_config_is_never_executed(tmp_path: Path) -> None:
    marker = tmp_path / "pwned"
    payload = f"__import__('pathlib').Path({str(marker)!r}).write_text('x')"
    node = {
        "id": "n",
        "handler": "synthetic_order_lookup",
        "transitions": {},
        "config": {"code": payload, "eval": payload, "script": payload},
        "input_mapping": {"order_id": payload},
        "metadata": {"run": payload},
    }
    spec = {**SPEC, "nodes": {"n": node}}
    app = create_api_app(db_path=tmp_path / "s.db")
    async with _client(app) as c:
        await c.post("/api/v1/workflows/validate", json={"spec": spec})
        created = await c.post("/api/v1/workflows", json={"workflow_id": "w1", "spec": spec})
        if created.status_code in (200, 201):
            await c.post("/api/v1/runs", json={"workflow_id": "w1", "initial_state": {}})
            await asyncio.sleep(0.2)
    assert not marker.exists()


def test_template_condition_evaluator_does_not_use_eval() -> None:
    source = (ROOT / "src/nexura_runtime/modules/text/template.py").read_text()
    assert "eval(" not in source.replace("without eval()", "")


# ---------------------------------------------------------------- (c) MCP stdio allowlist


def test_stdio_default_allowlist_rejects_unlisted_commands() -> None:
    for cmd in (["bash", "-c", "id"], ["sh"], ["curl", "http://x"], ["rm", "-rf", "x"]):
        with pytest.raises(McpCommandNotAllowedError):
            McpStdioTransport(cmd)


def test_stdio_rejects_binary_merely_named_like_an_allowed_launcher(tmp_path: Path) -> None:
    fake = tmp_path / "python"
    fake.write_text("#!/bin/sh\necho pwned\n")
    fake.chmod(0o755)
    with pytest.raises(McpCommandNotAllowedError):
        McpStdioTransport([str(fake)])
    with pytest.raises(McpCommandNotAllowedError):
        McpStdioTransport([str(tmp_path / ".." / tmp_path.name / "node")])


def test_stdio_accepts_current_interpreter_path() -> None:
    McpStdioTransport([sys.executable, "-m", "some_server"], allowed_commands={"python3", "python"})


@pytest.mark.parametrize(
    "cmd",
    [
        ["python3", "-c", "import os; os.system('id')"],
        ["node", "-e", "require('child_process').exec('id')"],
        ["node", "--eval", "1"],
        ["python", "-c", "1"],
    ],
)
def test_stdio_rejects_inline_code_flags_by_default(cmd: list[str]) -> None:
    with pytest.raises(McpCommandNotAllowedError):
        McpStdioTransport(cmd)
    McpStdioTransport(cmd, allow_inline_code=True)  # explicit opt-in
    McpStdioTransport(cmd, allowed_commands={cmd[0]})  # operator-chosen allowlist


def test_stdio_custom_allowlist_is_honoured() -> None:
    with pytest.raises(McpCommandNotAllowedError):
        McpStdioTransport(["python3", "server.py"], allowed_commands={"mytool"})
    McpStdioTransport(["mytool", "--serve"], allowed_commands={"mytool"})


async def test_service_rejects_caller_supplied_stdio(tmp_path: Path) -> None:
    app = create_api_app(db_path=tmp_path / "s.db")
    async with _client(app) as c:
        for path in ("/api/v1/integrations/mcp/discover", "/api/v1/integrations/mcp/test-run"):
            res = await c.post(
                path,
                json={
                    "transport_type": "stdio",
                    "command": "python3",
                    "tool_name": "t",
                    "args": [],
                },
            )
            assert res.status_code == 403
            assert "STDIO_EXECUTION_DISABLED" in res.text


# ---------------------------------------------------------------- (d) request limits


async def test_service_rejects_oversized_bodies_and_imports(tmp_path: Path) -> None:
    app = create_api_app(
        db_path=tmp_path / "s.db", max_request_body_bytes=1024, max_import_body_bytes=4096
    )
    json_header = {"content-type": "application/json"}
    async with _client(app) as c:
        big = json.dumps({"x": "a" * 2000}).encode()
        assert (await c.post("/api/v1/runs", content=big, headers=json_header)).status_code == 413
        huge = json.dumps({"x": "a" * 6000}).encode()
        res = await c.post("/api/v1/workflows/import", content=huge, headers=json_header)
        assert res.status_code == 413
        assert "REQUEST_TOO_LARGE" in res.text

        async def chunks() -> Any:
            for _ in range(60):
                yield b"a" * 512

        streamed = await c.post("/api/v1/runs", content=chunks(), headers=json_header)
        assert streamed.status_code == 413


async def test_service_rejects_deeply_nested_json(tmp_path: Path) -> None:
    app = create_api_app(db_path=tmp_path / "s.db")
    deep = b"[" * 5000 + b"]" * 5000
    async with _client(app) as c:
        res = await c.post(
            "/api/v1/runs", content=deep, headers={"content-type": "application/json"}
        )
        assert res.status_code == 422
        assert "JSON_TOO_DEEP" in res.text


async def test_studio_rejects_oversized_bodies(tmp_path: Path) -> None:
    app = create_studio_app(db_path=tmp_path / "s.db", max_request_body_bytes=1024)
    async with _client(app) as c:
        body = json.dumps({"x": "a" * 4000}).encode()
        res = await c.post(
            "/api/v1/workflows", content=body, headers={"content-type": "application/json"}
        )
        assert res.status_code == 413


def test_default_registry_has_no_code_execution_module() -> None:
    from nexura_runtime.studio.builtin_modules import get_default_registry

    ids = {str(name).lower() for name in get_default_registry()._handlers}
    assert ids, "expected built-in handlers"
    assert not any("sandbox" in i or "exec" in i or "python" in i for i in ids), ids


def test_shared_profile_needs_test_provider_to_allow_network_bind(tmp_path: Path) -> None:
    from _local_env import TestAuthProvider

    from nexura_runtime.service.extensions import AuthProviderRequiredError

    with pytest.raises(AuthProviderRequiredError):
        create_api_app(
            db_path=tmp_path / "no.db",
            profile="authenticated-shared-service",
            listen_host="0.0.0.0",
        )
    create_api_app(
        db_path=tmp_path / "yes.db",
        profile="authenticated-shared-service",
        listen_host="0.0.0.0",
        auth_provider=TestAuthProvider(),
    )


async def test_test_provider_refuses_unauthenticated_requests(tmp_path: Path) -> None:
    from _local_env import TestAuthProvider

    app = create_api_app(
        db_path=tmp_path / "test.db",
        profile="authenticated-shared-service",
        auth_provider=TestAuthProvider(),
    )
    async with _client(app) as c:
        for path in ("/api/v1/workflows", "/api/v1/runs", "/api/v1/credentials"):
            r = await c.get(
                path, headers={"x-principal-id": "local-owner", "x-principal-role": "admin"}
            )
            assert r.status_code == 401
            assert r.json()["detail"]["code"] == "TEST_UNAUTHENTICATED"
            assert (await c.get(path, headers={"x-test-user": "local-owner"})).status_code == 200
