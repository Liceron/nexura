import asyncio
import json

import httpx
import pytest
from nexura_agents.mcp.errors import McpCommandNotAllowedError
from nexura_agents.mcp.transports import McpStdioTransport
from nexura_studio.server import create_app as create_studio_app

from nexura_runtime.service.api.app import create_api_app


@pytest.mark.parametrize(
    "args",
    [["-cprint(1)"], ["-Sc", "print(1)"], ["-"], ["--command=print(1)"]],
)
def test_python_inline_code_variants_refused(args):
    with pytest.raises(McpCommandNotAllowedError):
        McpStdioTransport(["python", *args])


@pytest.mark.parametrize("args", [["--eval=1"], ["-pe", "1"], ["-e1"], ["--print=1"], ["-r", "x"]])
def test_node_inline_code_variants_refused(args):
    with pytest.raises(McpCommandNotAllowedError):
        McpStdioTransport(["node", *args])


def test_studio_body_cap_applies_to_import_path(tmp_path):
    app = create_studio_app(db_path=tmp_path / "s.db", max_request_body_bytes=1000)

    async def go():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as c:
            return await c.post(
                "/api/v1/workflows/import",
                content=json.dumps({"x": "a" * 5000}),
                headers={"content-type": "application/json"},
            )

    r = asyncio.run(go())
    assert r.status_code == 413


def test_studio_invalid_content_length_not_500(tmp_path):
    app = create_studio_app(db_path=tmp_path / "s.db")

    async def go():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://testserver",
        ) as c:
            return await c.post(
                "/api/v1/workflows",
                content=b"{}",
                headers={"content-type": "application/json", "content-length": "abc"},
            )

    r = asyncio.run(go())
    assert r.status_code < 500


def test_empty_studio_token_does_not_count_as_auth(tmp_path):
    from nexura_runtime.service.auth import UnauthenticatedNetworkError

    with pytest.raises(UnauthenticatedNetworkError):
        create_studio_app(db_path=tmp_path / "s.db", listen_host="0.0.0.0", api_token="")


def test_service_bound_host_is_used_when_listen_host_missing(tmp_path):
    from nexura_runtime.service.auth import UnauthenticatedNetworkError

    with pytest.raises(UnauthenticatedNetworkError):
        create_api_app(db_path=tmp_path / "s.db", bound_host="0.0.0.0")


def test_whitespace_studio_token_does_not_count_as_auth(tmp_path):
    from nexura_runtime.service.auth import UnauthenticatedNetworkError

    with pytest.raises(UnauthenticatedNetworkError):
        create_studio_app(db_path=tmp_path / "s.db", listen_host="0.0.0.0", api_token="  ")


def test_studio_middleware_rejects_empty_token():
    from nexura_runtime.studio.security import StudioAuthMiddleware

    with pytest.raises(ValueError):
        StudioAuthMiddleware(lambda *a: None, token="")


def test_studio_import_default_cap_is_larger_than_general_cap(tmp_path):
    app = create_studio_app(db_path=tmp_path / "s.db")

    async def go():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as c:
            return await c.post(
                "/api/v1/workflows/import",
                content=json.dumps({"x": "a" * (11 * 1024 * 1024)}),
                headers={"content-type": "application/json"},
            )

    assert asyncio.run(go()).status_code != 413


@pytest.mark.parametrize(
    "cmd",
    [
        ["python3", "-Sc", "1"],
        ["python", "-W", "ignore", "-c", "1"],
        ["python", "--command", "1"],
        ["python", "-X", "dev", "-"],
        ["node", "--require=x"],
        ["node", "--import", "x"],
        ["node", "-"],
        ["uv", "run", "python", "-c", "1"],
        ["npx", "--call", "ls"],
    ],
)
def test_more_inline_code_variants_refused(cmd):
    with pytest.raises(McpCommandNotAllowedError):
        McpStdioTransport(cmd)
    McpStdioTransport(cmd, allow_inline_code=True)


@pytest.mark.parametrize(
    "cmd",
    [
        ["python", "server.py", "-c", "conf"],
        ["python", "-m", "pkg.server", "-c", "conf"],
        ["python", "-u", "-W", "ignore", "server.py"],
        ["node", "server.js", "-e", "x"],
        ["node", "--max-old-space-size=64", "server.js"],
        ["npx", "-y", "@scope/server", "-c", "conf"],
    ],
)
def test_server_arguments_after_script_are_not_refused(cmd):
    McpStdioTransport(cmd)


def test_mcpserver_stdio_default_refuses_inline_code_and_can_opt_in():
    from nexura_agents.mcp.toolset import MCPServer

    with pytest.raises(McpCommandNotAllowedError):
        MCPServer.stdio("python", "-c", "1")
    MCPServer.stdio("python", "-c", "1", allow_inline_code=True)
    MCPServer.stdio("uvx", "some-server")


def _serve_args(monkeypatch, tmp_path, argv):
    import uvicorn

    from nexura_runtime.cli.main import main

    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: calls.append(kw))
    monkeypatch.delenv("NEXURA_INSECURE_ALLOW_UNAUTHENTICATED_NETWORK", raising=False)
    code = main(["serve", "--db", str(tmp_path / "s.db"), *argv])
    return code, calls


def test_cli_serve_refuses_non_loopback_without_auth(monkeypatch, tmp_path, capsys):
    code, calls = _serve_args(monkeypatch, tmp_path, ["--host", "0.0.0.0"])
    assert code == 2
    assert calls == []
    assert "refuses to listen" in capsys.readouterr().err


def test_cli_serve_loopback_starts(monkeypatch, tmp_path):
    code, calls = _serve_args(monkeypatch, tmp_path, [])
    assert code == 0
    assert calls[0]["host"] == "127.0.0.1"


def test_cli_serve_explicit_insecure_flag_allows_network_bind(monkeypatch, tmp_path):
    code, calls = _serve_args(
        monkeypatch,
        tmp_path,
        ["--host", "0.0.0.0", "--insecure-allow-unauthenticated-network"],
    )
    assert code == 0
    assert calls[0]["host"] == "0.0.0.0"
