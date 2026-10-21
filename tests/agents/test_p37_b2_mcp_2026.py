"""MCP 2026-07-28: stateless core, version fallback, request headers, cache hints, MRTR.

Request and response fixtures are copied from the specification pages (fetched
2026-10-21):
https://modelcontextprotocol.io/specification/2026-07-28/server/discover
https://modelcontextprotocol.io/specification/2026-07-28/basic/versioning
https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http
https://modelcontextprotocol.io/specification/2026-07-28/basic/patterns/mrtr
Interop with the official SDKs lives in experiments/interop (its own environment).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from nexura_agents import AgentMCPServer, MCPServer, tool
from nexura_agents.mcp import (
    LATEST_HANDSHAKE_VERSION,
    McpClientSession,
    McpInMemoryTransport,
    McpInputRequiredError,
    McpIntegrationError,
    McpProtocolVersionMismatchError,
    McpSecurityError,
    McpServerSession,
    McpTransport,
    check_authorization_response_issuer,
    check_metadata_issuer,
)
from nexura_agents.mcp.headers import (
    InvalidHeaderAnnotation,
    check_param_headers,
    decode_value,
    encode_value,
    header_paths,
)
from nexura_agents.mcp.transports import McpHttpTransport
from nexura_agents.network.errors import DestinationPolicyDeniedError

MODERN = "2026-07-28"
META = {
    "io.modelcontextprotocol/protocolVersion": MODERN,
    "io.modelcontextprotocol/clientInfo": {"name": "ExampleClient", "version": "1.0.0"},
    "io.modelcontextprotocol/clientCapabilities": {},
}
# server/discover request, verbatim from the specification.
DISCOVER = {
    "jsonrpc": "2.0",
    "id": "discover-1",
    "method": "server/discover",
    "params": {"_meta": META},
}


def _session(transport: McpTransport | None = None) -> McpServerSession:
    server = McpServerSession(transport or McpInMemoryTransport.create_pair()[1], list_ttl_ms=5000)

    async def add(args: dict[str, Any]) -> int:
        return int(args["a"]) + int(args["b"])

    server.register_tool(
        "add",
        "Add.",
        {"type": "object", "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}}},
        add,
    )
    server.register_resource("memo://a", "a", "alpha")
    return server


# ------------------------------------------------------------------ server, any transport


async def test_discover_answers_with_versions_capabilities_identity_and_cache_hints() -> None:
    reply = await _session().handle_request(DISCOVER)
    assert reply is not None and reply["id"] == "discover-1"
    result = reply["result"]
    assert result["resultType"] == "complete"
    assert result["supportedVersions"][0] == MODERN
    assert LATEST_HANDSHAKE_VERSION in result["supportedVersions"]
    assert set(result["capabilities"]) == {"tools", "resources", "prompts"}
    assert result["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "nexura-mcp-server"
    assert result["ttlMs"] == 5000 and result["cacheScope"] == "private"


async def test_unsupported_version_lists_supported_versions() -> None:
    meta = {**META, "io.modelcontextprotocol/protocolVersion": "1900-01-01"}
    req = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": meta}}
    reply = await _session().handle_request(req)
    assert reply is not None
    assert reply["error"]["code"] == -32022
    assert reply["error"]["data"]["requested"] == "1900-01-01"
    assert MODERN in reply["error"]["data"]["supported"]


@pytest.mark.parametrize("missing", ["io.modelcontextprotocol/clientCapabilities"])
async def test_request_missing_required_meta_is_invalid_params(missing: str) -> None:
    meta = {k: v for k, v in META.items() if k != missing}
    req = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": meta}}
    reply = await _session().handle_request(req)
    assert reply is not None and reply["error"]["code"] == -32602


async def test_modern_results_carry_result_type_server_info_and_cache_hints() -> None:
    server = _session()
    listing = await server.handle_request(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {"_meta": META}}
    )
    assert listing is not None
    assert listing["result"]["resultType"] == "complete"
    assert listing["result"]["ttlMs"] == 5000
    call = await server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "add", "arguments": {"a": 2, "b": 3}, "_meta": META},
        }
    )
    assert call is not None
    assert call["result"]["content"][0]["text"] == "5"
    assert call["result"]["resultType"] == "complete"
    assert "ttlMs" not in call["result"]  # tools/call is not cacheable
    read = await server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "resources/read",
            "params": {"uri": "memo://a", "_meta": META},
        }
    )
    assert read is not None and read["result"]["ttlMs"] == 0  # contents are never cached


async def test_unknown_tool_is_invalid_params_and_removed_methods_are_not_found() -> None:
    server = _session()
    unknown = await server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {"name": "nope", "_meta": META},
        }
    )
    assert unknown is not None and unknown["error"]["code"] == -32602
    for method in ("ping", "logging/setLevel", "subscriptions/listen"):
        reply = await server.handle_request(
            {"jsonrpc": "2.0", "id": 6, "method": method, "params": {"_meta": META}}
        )
        assert reply is not None and reply["error"]["code"] == -32601, method


async def test_server_pinned_to_modern_refuses_initialize_naming_its_versions() -> None:
    server = McpServerSession(McpInMemoryTransport.create_pair()[1], protocol_version=MODERN)
    reply = await server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"},
        }
    )
    assert reply is not None and reply["error"]["code"] == -32022
    assert reply["error"]["data"]["supported"] == [MODERN]


# ------------------------------------------------------------------ server over HTTP


@tool
def lookup(region: str, key: str) -> str:
    """Look up a key in a region."""
    return f"{key}@{region}"


@tool(requires_approval=True)
def wipe(target: str) -> str:
    """Wipe something."""
    return "wiped " + target


def _app(**options: Any) -> Any:
    server = AgentMCPServer("desk", tools=[lookup, wipe])
    # Mirror ``region`` into an Mcp-Param-Region header, as a server may declare.
    definition = server.session.tool_definition("lookup")
    assert definition is not None
    definition.inputSchema["properties"]["region"]["x-mcp-header"] = "Region"
    return server.http_app(**options)


def _headers(method: str, name: str | None = None, **extra: str) -> dict[str, str]:
    out = {"MCP-Protocol-Version": MODERN, "Mcp-Method": method, **extra}
    if name is not None:
        out["Mcp-Name"] = name
    return out


def _call(name: str, arguments: dict[str, Any], rid: int = 1) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": rid,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments, "_meta": META},
    }


async def _post(app: Any, body: Any, headers: dict[str, str]) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://desk.test"
    ) as http:
        return await http.post("/mcp", content=json.dumps(body), headers=headers)


async def test_http_stateless_request_mints_no_session() -> None:
    response = await _post(_app(), DISCOVER, _headers("server/discover"))
    assert response.status_code == 200
    assert "mcp-session-id" not in response.headers
    assert response.json()["result"]["supportedVersions"][0] == MODERN
    # A stray session id from an old client is ignored, not validated.
    again = await _post(_app(), DISCOVER, _headers("server/discover", **{"Mcp-Session-Id": "x"}))
    assert again.status_code == 200


@pytest.mark.parametrize(
    ("headers", "reason"),
    [
        ({"Mcp-Method": "tools/call", "Mcp-Name": "lookup"}, "missing protocol version header"),
        (_headers("tools/list", "lookup", **{"Mcp-Param-Region": "eu"}), "wrong method"),
        (_headers("tools/call", **{"Mcp-Param-Region": "eu"}), "missing name"),
        (_headers("tools/call", "wipe", **{"Mcp-Param-Region": "eu"}), "wrong name"),
        (_headers("tools/call", "lookup"), "missing param header"),
        (_headers("tools/call", "lookup", **{"Mcp-Param-Region": "us"}), "param mismatch"),
        (
            {
                **_headers("tools/call", "lookup", **{"Mcp-Param-Region": "eu"}),
                "MCP-Protocol-Version": "2025-06-18",
            },
            "version header differs from body",
        ),
    ],
)
async def test_http_header_mismatch_is_rejected(headers: dict[str, str], reason: str) -> None:
    response = await _post(_app(), _call("lookup", {"region": "eu", "key": "k"}), headers)
    assert response.status_code == 400, reason
    assert response.json()["error"]["code"] == -32020, reason


async def test_http_matching_headers_including_base64_values_are_accepted() -> None:
    region = "Zürich "
    headers = _headers(
        "tools/call", encode_value("lookup"), **{"Mcp-Param-Region": encode_value(region)}
    )
    assert headers["Mcp-Param-Region"].startswith("=?base64?")
    response = await _post(_app(), _call("lookup", {"region": region, "key": "k"}), headers)
    assert response.status_code == 200
    assert response.json()["result"]["content"][0]["text"] == f"k@{region}"


async def test_http_unsupported_version_and_unknown_method_statuses() -> None:
    meta = {**META, "io.modelcontextprotocol/protocolVersion": "2099-01-01"}
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": meta}}
    bad = await _post(
        _app(), body, {"MCP-Protocol-Version": "2099-01-01", "Mcp-Method": "tools/list"}
    )
    assert bad.status_code == 400 and bad.json()["error"]["code"] == -32022
    unknown = {"jsonrpc": "2.0", "id": 2, "method": "tasks/get", "params": {"_meta": META}}
    missing = await _post(_app(), unknown, _headers("tasks/get"))
    assert missing.status_code == 404 and missing.json()["error"]["code"] == -32601


async def test_http_modern_requests_keep_token_origin_and_approval_checks() -> None:
    app = _app(token="s3cret", allowed_origins=["https://ok.example"])
    headers = _headers("server/discover")
    assert (await _post(app, DISCOVER, headers)).status_code == 401
    auth = {"Authorization": "Bearer s3cret"}
    evil = {**headers, **auth, "Origin": "https://evil.example"}
    assert (await _post(app, DISCOVER, evil)).status_code == 403
    assert (await _post(app, DISCOVER, {**headers, **auth})).status_code == 200
    refused = await _post(
        app, _call("wipe", {"target": "db"}), {**_headers("tools/call", "wipe"), **auth}
    )
    assert refused.status_code == 200
    result = refused.json()["result"]
    assert result["isError"] is True and "Nothing was executed" in result["content"][0]["text"]


# ------------------------------------------------------------------ headers


def test_header_value_encoding_matches_the_specification_table() -> None:
    # Examples from the "Value Encoding" table of the Streamable HTTP page.
    assert encode_value("us-west1") == "us-west1"
    assert encode_value("Hello, 世界") == "=?base64?SGVsbG8sIOS4lueVjA==?="
    assert encode_value(" padded ") == "=?base64?IHBhZGRlZCA=?="
    assert encode_value("line1\nline2") == "=?base64?bGluZTEKbGluZTI=?="
    assert encode_value("=?base64?literal?=") == "=?base64?PT9iYXNlNjQ/bGl0ZXJhbD89?="
    for value in ("us-west1", "Hello, 世界", " padded ", "line1\nline2", "=?base64?literal?="):
        assert decode_value(encode_value(value)) == value
    assert decode_value("=?base64?not base64!?=") is None


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "object", "properties": {"n": {"type": "number", "x-mcp-header": "N"}}},
        {"type": "object", "properties": {"o": {"type": "object", "x-mcp-header": "O"}}},
        {"type": "object", "properties": {"a": {"type": "string", "x-mcp-header": ""}}},
        {"type": "object", "properties": {"a": {"type": "string", "x-mcp-header": "Bad Name"}}},
        {"type": "object", "properties": {"a": {"type": "string", "x-mcp-header": "Line\r\nX"}}},
        {
            "type": "object",
            "properties": {
                "a": {"type": "string", "x-mcp-header": "Dup"},
                "b": {"type": "string", "x-mcp-header": "dup"},
            },
        },
        {
            "type": "object",
            "properties": {
                "l": {"type": "array", "items": {"type": "string", "x-mcp-header": "Item"}}
            },
        },
        {
            "anyOf": [
                {"type": "object", "properties": {"a": {"type": "string", "x-mcp-header": "A"}}}
            ]
        },
    ],
)
def test_invalid_x_mcp_header_annotations_are_rejected(schema: dict[str, Any]) -> None:
    with pytest.raises(InvalidHeaderAnnotation):
        header_paths(schema)


def test_nested_annotations_and_numeric_comparison() -> None:
    schema = {
        "type": "object",
        "properties": {
            "cfg": {
                "type": "object",
                "properties": {"limit": {"type": "integer", "x-mcp-header": "Limit"}},
            }
        },
    }
    paths = header_paths(schema)
    assert paths == {("cfg", "limit"): "Limit"}
    assert check_param_headers(paths, {"cfg": {"limit": 42}}, {"mcp-param-limit": "42.0"}) is None
    assert check_param_headers(paths, {"cfg": {"limit": 42}}, {"mcp-param-limit": "43"})
    assert check_param_headers(paths, {}, {}) is None  # absent value, absent header
    assert check_param_headers(paths, {}, {"mcp-param-limit": "1"})  # header without value


# ------------------------------------------------------------------ client


class _Recorder(McpTransport):
    """Answers like a scripted server and records every message the client sends."""

    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.sent: list[dict[str, Any]] = []
        self._out: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def send_message(self, message: dict[str, Any]) -> None:
        self.sent.append(message)
        if "id" in message:
            reply = self.answer(message)
            if reply is not None:
                await self._out.put({"jsonrpc": "2.0", "id": message["id"], **reply})

    async def receive_message(self, timeout: float | None = None) -> dict[str, Any]:
        return await self._out.get()

    async def close(self) -> None:
        return None


def _legacy_answer(message: dict[str, Any]) -> dict[str, Any]:
    if message["method"] == "initialize":
        return {
            "result": {
                "protocolVersion": message["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "old", "version": "1"},
            }
        }
    if message["method"] == "tools/list":
        return {"result": {"tools": []}}
    return {"error": {"code": -32601, "message": "Method not found"}}


async def test_client_speaks_modern_to_a_nexura_server_with_the_envelope() -> None:
    c_side, s_side = McpInMemoryTransport.create_pair()
    server = _session(s_side)
    loop = asyncio.create_task(server.serve_loop())
    seen: list[dict[str, Any]] = []
    original = s_side.receive_message

    async def spy(timeout: float | None = None) -> dict[str, Any]:
        message = await original(timeout)
        seen.append(message)
        return message

    s_side.receive_message = spy  # type: ignore[method-assign]
    client = McpClientSession(c_side)
    try:
        info = await client.initialize()
        assert info["protocolVersion"] == MODERN and client.era == "modern"
        assert client.cache_hints["server/discover"].ttl_ms == 5000
        result = await client.call_tool("add", {"a": 1, "b": 2})
        assert result.content[0].text == "3"
    finally:
        loop.cancel()
    methods = [m["method"] for m in seen]
    assert methods == ["server/discover", "tools/call"]  # no initialize, no notifications
    for message in seen:
        meta = message["params"]["_meta"]
        assert meta["io.modelcontextprotocol/protocolVersion"] == MODERN
        assert meta["io.modelcontextprotocol/clientCapabilities"] == {}
        assert meta["io.modelcontextprotocol/clientInfo"]["name"] == "nexura-agents"


async def test_client_falls_back_to_handshake_for_a_legacy_server() -> None:
    transport = _Recorder(_legacy_answer)
    client = McpClientSession(transport)
    info = await client.initialize()
    assert client.era == "legacy" and info["protocolVersion"] == LATEST_HANDSHAKE_VERSION
    assert [m["method"] for m in transport.sent] == [
        "server/discover",
        "initialize",
        "notifications/initialized",
    ]
    await client.list_tools()
    assert "_meta" not in transport.sent[-1]["params"]  # legacy requests carry no envelope


async def test_client_treats_a_silent_stdio_server_as_legacy_after_the_probe_timeout() -> None:
    def answer(message: dict[str, Any]) -> dict[str, Any] | None:
        return None if message["method"] == "server/discover" else _legacy_answer(message)

    client = McpClientSession(_Recorder(answer), discover_timeout=0.05)
    await client.initialize()
    assert client.era == "legacy"


async def test_client_uses_handshake_when_modern_error_lists_only_handshake_versions() -> None:
    def answer(message: dict[str, Any]) -> dict[str, Any]:
        if message["method"] == "server/discover":
            data = {"supported": ["2025-06-18"], "requested": MODERN}
            return {
                "error": {"code": -32022, "message": "Unsupported protocol version", "data": data}
            }
        return _legacy_answer(message)

    client = McpClientSession(_Recorder(answer))
    info = await client.initialize()
    assert client.era == "legacy" and info["protocolVersion"] == LATEST_HANDSHAKE_VERSION


async def test_client_raises_when_no_version_is_shared() -> None:
    def answer(message: dict[str, Any]) -> dict[str, Any]:
        data = {"supported": ["2099-01-01"], "requested": MODERN}
        return {"error": {"code": -32022, "message": "Unsupported protocol version", "data": data}}

    with pytest.raises(McpProtocolVersionMismatchError):
        await McpClientSession(_Recorder(answer)).initialize()


async def test_pinned_versions() -> None:
    legacy = _Recorder(_legacy_answer)
    client = McpClientSession(legacy, protocol_version="2025-03-26")
    await client.initialize()
    assert [m["method"] for m in legacy.sent][0] == "initialize"  # no probe
    assert client.server_version == "2025-03-26"
    with pytest.raises(McpProtocolVersionMismatchError):
        await McpClientSession(_Recorder(_legacy_answer), protocol_version=MODERN).initialize()
    with pytest.raises(ValueError):
        McpClientSession(_Recorder(_legacy_answer), protocol_version="1999-01-01")


def _modern_answer(call: Any) -> Any:
    def answer(message: dict[str, Any]) -> dict[str, Any]:
        if message["method"] == "server/discover":
            return {
                "result": {
                    "resultType": "complete",
                    "supportedVersions": [MODERN],
                    "capabilities": {"tools": {}},
                    "ttlMs": 0,
                    "cacheScope": "public",
                }
            }
        return call(message)

    return answer


async def test_mrtr_request_state_is_echoed_on_a_retry_with_a_new_id() -> None:
    def call(message: dict[str, Any]) -> dict[str, Any]:
        params = message["params"]
        if params.get("requestState") != "opaque-1":
            return {"result": {"resultType": "input_required", "requestState": "opaque-1"}}
        return {"result": {"resultType": "complete", "content": [{"type": "text", "text": "ok"}]}}

    transport = _Recorder(_modern_answer(call))
    client = McpClientSession(transport)
    await client.initialize()
    result = await client.call_tool("t", {"x": 1})
    assert result.content[0].text == "ok"
    first, retry = transport.sent[-2], transport.sent[-1]
    assert first["id"] != retry["id"]
    assert retry["params"]["requestState"] == "opaque-1"
    assert retry["params"]["arguments"] == {"x": 1}
    assert "inputResponses" not in retry["params"]


ELICIT = {
    "github_login": {
        "method": "elicitation/create",
        "params": {
            "mode": "form",
            "message": "Please provide your GitHub username",
            "requestedSchema": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
    }
}


async def test_mrtr_input_requests_need_a_handler() -> None:
    def call(message: dict[str, Any]) -> dict[str, Any]:
        if "inputResponses" in message["params"]:
            name = message["params"]["inputResponses"]["github_login"]["content"]["name"]
            return {
                "result": {"resultType": "complete", "content": [{"type": "text", "text": name}]}
            }
        return {"result": {"resultType": "input_required", "inputRequests": ELICIT}}

    client = McpClientSession(_Recorder(_modern_answer(call)))
    await client.initialize()
    with pytest.raises(McpInputRequiredError) as exc:
        await client.call_tool("t", {})
    assert exc.value.details["requested"] == ["elicitation/create"]

    async def handler(key: str, request: dict[str, Any]) -> dict[str, Any]:
        assert key == "github_login" and request["method"] == "elicitation/create"
        return {"action": "accept", "content": {"name": "octocat"}}

    answered = McpClientSession(
        _Recorder(_modern_answer(call)),
        input_handler=handler,
        client_capabilities={"elicitation": {"form": {}}},
    )
    await answered.initialize()
    assert (await answered.call_tool("t", {})).content[0].text == "octocat"


async def test_mrtr_rounds_are_bounded_and_unknown_result_types_are_refused() -> None:
    def loop(message: dict[str, Any]) -> dict[str, Any]:
        return {"result": {"resultType": "input_required", "requestState": "again"}}

    client = McpClientSession(_Recorder(_modern_answer(loop)), max_input_rounds=3)
    await client.initialize()
    with pytest.raises(McpIntegrationError, match="3 rounds"):
        await client.call_tool("t", {})

    def odd(message: dict[str, Any]) -> dict[str, Any]:
        return {"result": {"resultType": "task", "tools": []}}

    weird = McpClientSession(_Recorder(_modern_answer(odd)))
    await weird.initialize()
    with pytest.raises(McpIntegrationError, match="resultType"):
        await weird.list_tools()


async def test_http_client_sends_metadata_headers_and_drops_invalid_annotated_tools() -> None:
    seen: list[httpx.Request] = []
    tools = [
        {
            "name": "good",
            "inputSchema": {
                "type": "object",
                "properties": {"region": {"type": "string", "x-mcp-header": "Region"}},
            },
        },
        {
            "name": "bad",
            "inputSchema": {
                "type": "object",
                "properties": {"n": {"type": "number", "x-mcp-header": "N"}},
            },
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = json.loads(request.content)
        method = body["method"]
        if method == "server/discover":
            result: dict[str, Any] = {"supportedVersions": [MODERN], "capabilities": {"tools": {}}}
        elif method == "tools/list":
            result = {"tools": tools, "ttlMs": 1000, "cacheScope": "public"}
        else:
            result = {"content": [{"type": "text", "text": "ok"}]}
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": {"resultType": "complete", **result},
            },
        )

    remote = MCPServer.http("http://mcp.test/mcp", transport=httpx.MockTransport(handler))
    async with remote:
        listed = await remote.list_tools()
        assert [t.name for t in listed] == ["good"]  # the invalid annotation is left out
        assert remote.session.cache_hints["tools/list"].ttl_ms == 1000
        assert remote.session.cache_hints["tools/list"].scope == "public"
        await remote.session.call_tool("good", {"region": "Zürich"})
    call = seen[-1]
    assert call.headers["mcp-protocol-version"] == MODERN
    assert call.headers["mcp-method"] == "tools/call"
    assert call.headers["mcp-name"] == "good"
    assert call.headers["mcp-param-region"] == encode_value("Zürich")
    assert all("mcp-session-id" not in r.headers for r in seen)


async def test_http_client_still_enforces_the_destination_policy() -> None:
    remote = MCPServer.http("http://169.254.169.254/mcp")
    with pytest.raises(DestinationPolicyDeniedError):
        await remote.connect()
    await remote.close()


async def test_http_transport_hands_modern_protocol_errors_to_the_client() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        error = {"code": -32022, "message": "Unsupported", "data": {"supported": ["2099-01-01"]}}
        return httpx.Response(400, json={"jsonrpc": "2.0", "id": body["id"], "error": error})

    client = McpClientSession(
        McpHttpTransport("http://x.test/mcp", transport=httpx.MockTransport(handler))
    )
    with pytest.raises(McpProtocolVersionMismatchError):
        await client.initialize()


# ------------------------------------------------------------------ RFC 9207


def test_issuer_checks_follow_rfc_9207_table() -> None:
    issuer = "https://auth.example.com"
    check_metadata_issuer(issuer, {"issuer": issuer})
    with pytest.raises(McpSecurityError):
        check_metadata_issuer(issuer, {"issuer": "https://honest.example"})
    # supported + present: compare; supported + absent: reject.
    check_authorization_response_issuer(
        issuer, {"code": "c", "iss": issuer}, iss_parameter_supported=True
    )
    with pytest.raises(McpSecurityError):
        check_authorization_response_issuer(issuer, {"code": "c"}, iss_parameter_supported=True)
    # not advertised + present: still compared; not advertised + absent: proceed.
    with pytest.raises(McpSecurityError):
        check_authorization_response_issuer(
            issuer, {"code": "c", "iss": "https://evil.example"}, iss_parameter_supported=False
        )
    check_authorization_response_issuer(issuer, {"code": "c"}, iss_parameter_supported=False)
    # No normalization: a trailing slash is a different issuer.
    with pytest.raises(McpSecurityError):
        check_authorization_response_issuer(
            issuer, {"iss": issuer + "/"}, iss_parameter_supported=True
        )
