"""B28: SSRF and MCP stdio regression tests mirroring October competitor bug classes.

Langflow's authenticated MCP stdio RCE and n8n's SSRF bypasses: the shared destination policy
must refuse every non-public address, IPv4 hidden in IPv6, and metadata addresses even for
allow-listed hosts; stdio servers must refuse env variables that load caller-chosen code.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Any

import pytest
from nexura_agents.mcp.errors import McpCommandNotAllowedError
from nexura_agents.mcp.toolset import MCPServer
from nexura_agents.network.errors import DestinationPolicyDeniedError
from nexura_agents.network.policy import DestinationPolicy, classify_address


@pytest.mark.parametrize(
    "address,kind",
    [
        ("169.254.169.254", "metadata"),
        ("100.100.100.200", "metadata"),
        ("fd00:ec2::254", "metadata"),
        ("::ffff:169.254.169.254", "metadata"),
        ("64:ff9b::a9fe:a9fe", "metadata"),
        ("2002:a9fe:a9fe::", "metadata"),
        ("127.0.0.1", "loopback"),
        ("::1", "loopback"),
        ("64:ff9b::7f00:1", "loopback"),
        ("10.0.0.5", "private"),
        ("100.64.0.1", "private"),
        ("0.1.2.3", "private"),
        ("224.0.0.1", "private"),
        ("240.0.0.1", "private"),
        ("fc00::1", "private"),
        ("93.184.216.34", None),
        ("2606:2800:220:1::1", None),
    ],
)
def test_classify_address(address: str, kind: str | None) -> None:
    assert classify_address(ipaddress.ip_address(address)) == kind


@pytest.mark.parametrize(
    "url",
    [
        "http://100.100.100.200/latest/meta-data/",
        "http://[64:ff9b::a9fe:a9fe]/",
        "http://[::ffff:127.0.0.1]/",
        "http://224.0.0.1/",
        "http://100.64.0.1/",
    ],
)
def test_policy_refuses_non_public_literals(url: str) -> None:
    with pytest.raises(DestinationPolicyDeniedError):
        DestinationPolicy().check_url(url)


def _resolve_to(monkeypatch: pytest.MonkeyPatch, ip: str) -> None:
    def gai(host: str, port: Any, *a: Any, **k: Any) -> list[Any]:
        family = socket.AF_INET6 if ":" in ip else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 6, "", (ip, port or 443))]

    monkeypatch.setattr(socket, "getaddrinfo", gai)


def test_allow_listed_host_never_reaches_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    _resolve_to(monkeypatch, "169.254.169.254")
    policy = DestinationPolicy(allowed_hosts=["intranet.example"])
    with pytest.raises(DestinationPolicyDeniedError, match="metadata"):
        policy.check_url("https://intranet.example/")


def test_allow_listed_host_may_reach_private_ranges(monkeypatch: pytest.MonkeyPatch) -> None:
    _resolve_to(monkeypatch, "10.1.2.3")
    DestinationPolicy(allowed_hosts=["intranet.example"]).check_url("https://intranet.example/")
    with pytest.raises(DestinationPolicyDeniedError):
        DestinationPolicy().check_url("https://intranet.example/")


def test_public_address_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    _resolve_to(monkeypatch, "93.184.216.34")
    DestinationPolicy().check_url("https://example.com/")


@pytest.mark.parametrize(
    "env",
    [
        {"LD_PRELOAD": "/tmp/x.so"},
        {"NODE_OPTIONS": "--require /tmp/x.js"},
        {"PYTHONSTARTUP": "/tmp/x.py"},
        {"PYTHONPATH": "/tmp"},
        {"DYLD_INSERT_LIBRARIES": "/tmp/x.dylib"},
        {"UV_INDEX_URL": "https://evil.example/simple"},
        {"npm_config_registry": "https://evil.example"},
        {"BASH_ENV": "/tmp/x.sh"},
    ],
)
def test_stdio_refuses_code_loading_env(env: dict[str, str]) -> None:
    with pytest.raises(McpCommandNotAllowedError):
        MCPServer.stdio("npx", "-y", "server-x", env=env)


def test_stdio_allows_plain_env_and_explicit_opt_in() -> None:
    MCPServer.stdio("npx", "-y", "server-x", env={"GITHUB_TOKEN": "t", "LOG_LEVEL": "debug"})
    MCPServer.stdio(
        "node",
        "server.js",
        env={"NODE_OPTIONS": "--max-old-space-size=512"},
        allow_inline_code=True,
    )
    MCPServer.stdio("python3", "-m", "srv", env={"PYTHONUNBUFFERED": "1", "NODE_ENV": "production"})


def test_custom_allowlist_still_checks_env() -> None:
    """Choosing the launchers does not let the caller change what they load (review 2026-10-17)."""
    with pytest.raises(McpCommandNotAllowedError):
        MCPServer.stdio(
            "node",
            "server.js",
            env={"NODE_OPTIONS": "--require /tmp/x.js"},
            allowed_commands={"node"},
        )
    with pytest.raises(McpCommandNotAllowedError):
        MCPServer.stdio(
            "python3", "-m", "srv", env={"PATH": "/tmp/bin"}, allowed_commands={"python3"}
        )


async def test_safe_http_client_pins_connections_against_dns_rebinding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The policy check sees a public address; DNS then answers metadata: no connection."""
    import httpx
    from nexura_agents.network.client import SafeHttpClient

    answers = iter(["93.184.216.34", "169.254.169.254", "169.254.169.254"])

    def gai(host: str, port: Any, *a: Any, **k: Any) -> list[Any]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (next(answers), port or 80))]

    def no_connect(*a: Any, **k: Any) -> Any:
        raise AssertionError("a socket was opened")

    monkeypatch.setattr(socket, "getaddrinfo", gai)
    monkeypatch.setattr(socket.socket, "connect", no_connect)
    async with SafeHttpClient(DestinationPolicy()) as client:
        with pytest.raises(httpx.ConnectError, match="metadata"):
            await client.get("http://rebind.example/")


async def test_redirect_to_another_origin_drops_credentials_and_307_keeps_body() -> None:
    import httpx
    from nexura_agents.network.client import SafeHttpClient

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "a.example" and request.url.path == "/start":
            return httpx.Response(307, headers={"location": "https://b.example/next"})
        return httpx.Response(200)

    policy = DestinationPolicy(allowed_hosts=["a.example", "b.example"])
    async with SafeHttpClient(policy, transport=httpx.MockTransport(handler)) as client:
        await client.post(
            "https://a.example/start",
            json={"x": 1},
            headers={"Authorization": "Bearer t", "Cookie": "s=1", "X-Trace": "keep"},
        )
    final = seen[-1]
    assert final.url.host == "b.example" and final.method == "POST"
    assert final.content == b'{"x":1}' or final.content == b'{"x": 1}'
    assert "authorization" not in final.headers and "cookie" not in final.headers
    # Audit 13: a cross-origin redirect forwards only an allowlist of safe headers.
    assert "x-trace" not in final.headers


@pytest.mark.parametrize(
    "url",
    ["http://[fec0::1]/", "http://example.com:99999/", "http://[::1/"],
)
def test_policy_refuses_site_local_and_malformed_urls(url: str) -> None:
    with pytest.raises(DestinationPolicyDeniedError):
        DestinationPolicy().check_url(url)


def test_allow_list_ignores_trailing_dot_and_case(monkeypatch: pytest.MonkeyPatch) -> None:
    _resolve_to(monkeypatch, "10.0.0.5")
    DestinationPolicy(allowed_hosts=["Intra.Example."]).check_url("https://intra.example/")


# ---------------------------------------------------------------- audit 13: NET group
import asyncio  # noqa: E402
import contextlib  # noqa: E402
import threading  # noqa: E402

import httpx  # noqa: E402
from nexura_agents.mcp.errors import McpTransportError  # noqa: E402
from nexura_agents.mcp.transports import McpHttpTransport  # noqa: E402
from nexura_agents.network.client import SafeHttpClient  # noqa: E402

_PUB = "93.184.216.34"


def _fake_dns(monkeypatch: pytest.MonkeyPatch, table: dict[str, str]) -> None:
    def fake(host: str, port: Any, *a: Any, **k: Any) -> list[Any]:
        if host not in table:
            raise socket.gaierror("no such host")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (table[host], port))]

    monkeypatch.setattr(socket, "getaddrinfo", fake)


@pytest.mark.parametrize(
    "location", ["http://127.0.0.1:8080/admin", "http://10.0.0.5/x", "https://other.example/x"]
)
def test_mcp_http_transport_treats_a_redirect_as_an_error(
    monkeypatch: pytest.MonkeyPatch, location: str
) -> None:
    _fake_dns(monkeypatch, {"mcp.example.com": _PUB, "other.example": "93.184.216.35"})
    hosts: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        hosts.append(req.url.host)
        return httpx.Response(307, headers={"location": location})

    t = McpHttpTransport("https://mcp.example.com/mcp", transport=httpx.MockTransport(handler))

    async def go() -> None:
        with pytest.raises(McpTransportError, match="redirect"):
            await t.send_message({"jsonrpc": "2.0", "id": 1, "method": "ping"})
        await t.close()

    asyncio.run(go())
    assert hosts == ["mcp.example.com"]


def test_mcp_http_transport_copies_the_callers_policy() -> None:
    shared = DestinationPolicy()
    t = McpHttpTransport("http://10.9.9.9:8000/mcp", policy=shared)
    assert shared.allowed_hosts == []
    assert t.policy.allowed_hosts == ["10.9.9.9"]


def test_cross_origin_redirect_forwards_only_safe_headers() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if req.url.host == "a.example":
            return httpx.Response(307, headers={"location": "https://b.example/n"})
        return httpx.Response(200)

    async def go() -> None:
        policy = DestinationPolicy(allowed_hosts=["a.example", "b.example"])
        async with SafeHttpClient(policy, transport=httpx.MockTransport(handler)) as c:
            await c.post(
                "https://a.example/s",
                json={"x": 1},
                headers={
                    "X-Subscription-Token": "S",
                    "Private-Token": "S",
                    "Accept": "application/json",
                    "User-Agent": "ua",
                },
            )

    asyncio.run(go())
    final = seen[-1]
    assert "x-subscription-token" not in final.headers and "private-token" not in final.headers
    assert final.headers["accept"] == "application/json" and final.headers["user-agent"] == "ua"
    assert final.headers["content-type"] == "application/json"


def test_local_use_nat64_range_is_private_whatever_the_low_bits() -> None:
    for text in ("64:ff9b:1:a00:0:100:808:808", "64:ff9b:1::808:808", "64:ff9b:1:ffff::1"):
        assert classify_address(ipaddress.IPv6Address(text)) == "private"
    assert classify_address(ipaddress.IPv6Address("64:ff9b::808:808")) is None
    assert classify_address(ipaddress.IPv6Address("64:ff9b::a00:1")) == "private"


def test_metadata_opt_in_flag_is_gone() -> None:
    # The removed option is ignored; it cannot lift the refusal.
    assert DestinationPolicy(allow_metadata_service=True).allow_metadata_service is False
    policy = DestinationPolicy(
        allow_metadata_service=True, allow_loopback=True, allow_private_ranges=True
    )
    with pytest.raises(DestinationPolicyDeniedError):
        policy.check_url("http://169.254.169.254/latest/")


def test_safe_client_redirect_limit_head_stream_and_dns_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    methods: list[str] = []

    def loop(req: httpx.Request) -> httpx.Response:
        methods.append(req.method)
        return httpx.Response(302, headers={"location": f"http://{_PUB}/loop"})

    async def go() -> None:
        async with SafeHttpClient(DestinationPolicy(), transport=httpx.MockTransport(loop)) as c:
            with pytest.raises(httpx.TooManyRedirects):
                await c.request("HEAD", f"http://{_PUB}/s", max_redirects=2)
            assert methods == ["HEAD"] * 3
            with pytest.raises(httpx.HTTPStatusError, match="redirect"):
                async with c.stream("GET", f"http://{_PUB}/sse"):
                    pass

    asyncio.run(go())

    main = threading.get_ident()
    seen: list[int] = []

    def spy(host: Any, port: Any, *a: Any, **k: Any) -> Any:
        seen.append(threading.get_ident())
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (_PUB, port))]

    monkeypatch.setattr(socket, "getaddrinfo", spy)

    async def dns() -> None:
        async with SafeHttpClient(
            DestinationPolicy(), transport=httpx.MockTransport(lambda r: httpx.Response(200))
        ) as c:
            await c.get("http://some-name.test/")

    asyncio.run(dns())
    assert seen and main not in seen


def test_openapi_spec_host_is_not_trusted_after_one_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nexura_agents import Agent, FakeModel, ToolCall, openapi_tools

    _fake_dns(monkeypatch, {"api.example": "10.1.2.3"})
    spec = {
        "openapi": "3.0.0",
        "info": {"title": "T", "version": "1"},
        "servers": [{"url": "https://api.example"}],
        "paths": {"/x": {"get": {"operationId": "x", "responses": {"200": {"description": "ok"}}}}},
    }
    hit: list[str] = []
    tools = openapi_tools(
        spec, transport=httpx.MockTransport(lambda r: hit.append(str(r.url)) or httpx.Response(200))
    )
    agent = Agent(
        name="A",
        instructions="x",
        model=FakeModel(responses=[[ToolCall(id="1", name="x", arguments={})], "ok"]),
        tools=tools,
    )
    with contextlib.suppress(Exception):  # a refused destination ends the run
        agent.run("go")
    assert hit == []
