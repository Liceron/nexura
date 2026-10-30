# Serving agents over MCP

`AgentMCPServer` exposes your agents and tools as an MCP server, so Claude Desktop, Cursor, VS Code, another agent or any other MCP client can use them. Each agent becomes one tool that takes a `request` string and returns the agent's answer. Plain tools are exposed with their own schemas. You can serve over stdio (the client launches your script) or over HTTP (an ASGI app).
Tested against the official MCP Python SDK clients (2026-07-28 and handshake revisions);
not yet checked with those apps.

## Try it in-process

This example serves an agent as an ASGI app and calls it through `MCPServer.http` with an `httpx.ASGITransport`. No port is opened. It is the same client code you would use against a real URL.

```python
import asyncio

import httpx

from nexura_agents import Agent, AgentMCPServer, FakeModel, MCPServer, ToolCall, tool


@tool
def order_status(order_id: str) -> str:
    """Status of an order."""
    return f"order {order_id} is shipped"


support = Agent(
    name="Support",
    description="Answers customer questions about orders.",
    instructions="Help customers.",
    model=FakeModel(
        responses=[
            [ToolCall(id="1", name="order_status", arguments={"order_id": "A1"})],
            "Order A1 is shipped.",
        ]
    ),
    tools=[order_status],
)

server = AgentMCPServer("support", agents=[support], tools=[order_status])
app = server.http_app(token="s3cret")


async def main() -> None:
    transport = httpx.ASGITransport(app=app)
    headers = {"Authorization": "Bearer s3cret"}
    async with MCPServer.http(
        "http://support.test/mcp", headers=headers, transport=transport
    ) as client:
        tools = await client.tools()
        assert sorted(t.name for t in tools) == ["order_status", "support"]

        # The client side is just more tools; call them from another agent.
        caller = Agent(
            name="Caller",
            instructions="Delegate to support.",
            model=FakeModel(
                responses=[
                    [ToolCall(id="1", name="support", arguments={"request": "Where is A1?"})],
                    "Support says: shipped.",
                ]
            ),
            tools=tools,
        )
        result = await caller.run_async("Where is order A1?")
        assert result.output == "Support says: shipped."
        tool_result = [m for m in result.messages if m.role == "tool"][0].content
        assert tool_result == "Order A1 is shipped."


asyncio.run(main())
```

Agent tool names are the agent name lowercased with spaces replaced by `_` (cleaned to letters, digits, `_` and `-`). The tool description is `Agent(description=...)`, so say what the agent is for. If two agents or tools would get the same name, the constructor raises `ValueError`.

## AgentMCPServer arguments

`AgentMCPServer(name, *, agents=(), tools=(), deps=None, runner=None, version="0.4.2", list_ttl_ms=60_000, instructions=None)`

- `agents`: each becomes a tool named after the agent.
- `tools`: `Tool` objects exposed with their own schemas.
- `deps`: passed to every agent run and tool call as `RunContext.deps`.
- `runner`: the `Runner` used for agent calls. Pass one to apply usage limits, hooks or budgets.
- `list_ttl_ms`: the `ttlMs` cache hint on 2026-07-28 discover and list results. The tool list is fixed when the server is built, so clients may reuse it this long.
- `instructions`: optional guidance for the client's model, returned by `server/discover`.

## Approval-gated work is refused

An MCP client has no way to answer an approval prompt here, so nothing that needs approval runs unattended:

- A plain tool whose `requires_approval` applies to the arguments is refused, and the client gets an error result. The tool function is not called.
- An agent run that pauses for approval is reported back as an error that names the blocked tools. Nothing pending is executed.

```python
import asyncio

import httpx

from nexura_agents import Agent, AgentMCPServer, FakeModel, MCPServer, ToolCall, tool

deleted: list[str] = []


@tool(requires_approval=True)
def delete_account(user: str) -> str:
    """Delete an account."""
    deleted.append(user)
    return "deleted"


admin = Agent(
    name="Admin",
    instructions="Manage accounts.",
    model=FakeModel(
        responses=[[ToolCall(id="1", name="delete_account", arguments={"user": "bob"})]]
    ),
    tools=[delete_account],
)
app = AgentMCPServer("admin", agents=[admin], tools=[delete_account]).http_app()


async def main() -> None:
    transport = httpx.ASGITransport(app=app)
    async with MCPServer.http("http://admin.test/mcp", transport=transport) as client:
        by_name = {t.name: t for t in await client.tools()}
        for name, args in [
            ("admin", {"request": "delete bob"}),
            ("delete_account", {"user": "bob"}),
        ]:
            result = await by_name[name].execute(args, _ctx())
            assert "approval" in result, result
    assert deleted == []


def _ctx():
    from nexura_agents import RunContext

    return RunContext(deps=None, run_id="r", agent_name="client")


asyncio.run(main())
```

If you want approvals, run the agent in your own application with `Runner.resume` instead of serving it over MCP; see [Human approval](approvals.md).

## Serving over stdio

For a local client, write a script and let the client launch it. `run_stdio()` blocks and speaks newline-delimited JSON-RPC on stdin and stdout until the client closes the pipe. Never `print` to stdout in this script; it would corrupt the protocol. Log to stderr.

```python notest
# support_server.py
from nexura_agents import AgentMCPServer

AgentMCPServer("support", agents=[support_agent], tools=[order_status]).run_stdio()
```

In Claude Desktop, add it to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "support": {
      "command": "python",
      "args": ["/absolute/path/to/support_server.py"]
    }
  }
}
```

Use the Python from the environment where Nexura is installed (for example the full path to a virtualenv's `python`). If your agent calls a real model, the client can pass credentials to the script through an `env` object in the same entry.

`run_stdio()` uses `asyncio.run`. Inside an existing event loop, `await server.serve_stdio()` instead.

## Serving over HTTP

`server.http_app(...)` returns an ASGI app for the MCP Streamable HTTP transport. Run it with any ASGI server:

```python notest
# support_http.py
from nexura_agents import AgentMCPServer

server = AgentMCPServer("support", agents=[support_agent])
app = server.http_app(token="a-long-random-secret")
# uvicorn support_http:app --host 127.0.0.1 --port 8000
```

Options of `http_app`:

| Argument | Default | Meaning |
|---|---|---|
| `token` | `None` | Require `Authorization: Bearer <token>`. Compared in constant time. Always set one unless the server is only reachable from localhost. Without it, anyone who can reach the port can run your agents. |
| `path` | `"/mcp"` | The URL path. Other paths return 404. |
| `allowed_origins` | `()` | Requests with an `Origin` header are refused with 403 unless the origin is listed. This protects against DNS-rebinding from browsers. Clients that send no `Origin` (most MCP clients) are unaffected. |
| `max_body_bytes` | 4 MiB | Larger request bodies get 413. |
| `max_sessions` | 1000 | Handshake-era sessions only: when exceeded, the least recently used session is dropped. |
| `session_ttl_seconds` | 3600.0 | Handshake-era sessions idle for longer expire. |

The app serves both MCP eras on the same path:

- **2026-07-28 (stateless).** Each request carries its protocol version and client
  capabilities in `params._meta`; no session is created and an `Mcp-Session-Id` header is
  ignored. The `MCP-Protocol-Version`, `Mcp-Method`, `Mcp-Name` and `Mcp-Param-*` headers
  must match the body, otherwise the reply is HTTP 400 with `HeaderMismatch` (-32020). A
  missing `_meta` field gives 400 with -32602, an unsupported version 400 with
  `UnsupportedProtocolVersionError` (-32022, listing the supported versions), and an unknown
  method 404 with -32601. `server/discover` returns the supported versions, capabilities
  and server identity, with `ttlMs` (`list_ttl_ms`, default 60 s) and `cacheScope`
  (`"private"`) cache hints, as do the list results.
- **2025-11-25 back to 2024-11-05 (handshake).** Sessions start with the `initialize` request: the server returns an `Mcp-Session-Id` header, and later requests must send it (missing gives 400, unknown or expired gives 404). `DELETE` ends a session.

`GET` returns 405, because the server does not stream notifications. The token, `Origin`
and body-size checks apply to both eras, and tools that need approval are refused in both.

The checks are visible with plain HTTP calls through the ASGI transport:

```python
import asyncio

import httpx

from nexura_agents import AgentMCPServer

app = AgentMCPServer("demo").http_app(
    token="s3cret", allowed_origins=["https://app.example.com"], max_body_bytes=200
)
INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2025-03-26", "capabilities": {}},
}


async def main() -> None:
    transport = httpx.ASGITransport(app=app)
    auth = {"Authorization": "Bearer s3cret"}
    async with httpx.AsyncClient(transport=transport, base_url="http://demo.test") as http:
        assert (await http.post("/mcp", json=INIT)).status_code == 401  # no token
        bad_origin = {**auth, "Origin": "https://evil.example"}
        assert (await http.post("/mcp", json=INIT, headers=bad_origin)).status_code == 403
        assert (await http.post("/other", json=INIT, headers=auth)).status_code == 404
        big = {**INIT, "params": {"pad": "x" * 500}}
        assert (await http.post("/mcp", json=big, headers=auth)).status_code == 413

        ok = await http.post("/mcp", json=INIT, headers=auth)
        assert ok.status_code == 200 and "mcp-session-id" in ok.headers
        # A follow-up request without the session id is rejected.
        ping = {"jsonrpc": "2.0", "id": 2, "method": "ping"}
        assert (await http.post("/mcp", json=ping, headers=auth)).status_code == 400
        with_session = {**auth, "Mcp-Session-Id": ok.headers["mcp-session-id"]}
        assert (await http.post("/mcp", json=ping, headers=with_session)).status_code == 200


asyncio.run(main())
```

## Limits to know about

- Tested against the official MCP Python SDK clients (`mcp` 2.3.0 in its 2026-07-28 and handshake modes, and the handshake-only `mcp` 1.30.0) over stdio and HTTP on localhost; see `experiments/interop`. Not yet checked with Claude Desktop, Cursor or VS Code.
- Agent tools return only the final answer as text. Streaming, progress notifications, `subscriptions/listen`, multi round-trip input requests and the optional extensions (Tasks, MCP Apps, Enterprise Managed Authorization) are not implemented.
- HTTP session state lives in the memory of one process; there is no shared store, so run a single worker or use sticky routing.
- Every agent call runs with the same `deps`, so do not put per-user credentials in `deps` if clients should not share them.

## See also

- [MCP and OpenAPI tools](mcp-and-openapi.md)
- [Tools](tools.md)
- [Agents](agents.md)
- [Human approval](approvals.md)
- [Troubleshooting](troubleshooting.md)

## Serve your agents over MCP

`AgentMCPServer` turns your agents and tools into an MCP server, so they can be used
from Claude Desktop, Cursor, VS Code or any other MCP client — or from another agent.
Tested against the official MCP Python SDK clients (2026-07-28 and handshake revisions);
not yet checked with those apps.

Point your client at it, e.g. in Claude Desktop's `claude_desktop_config.json`:

```json
{"mcpServers": {"support": {"command": "python", "args": ["support_server.py"]}}}
```

Or serve it over HTTP with any ASGI server (`uvicorn support_server:app`):

```python notest
app = AgentMCPServer("support", agents=[support_agent]).http_app(token=os.environ["MCP_TOKEN"])
```

- Each agent becomes a tool taking a `request`; set `Agent(description=...)` so clients
  know what it's for. Typed outputs (`output_type`) are returned as JSON.
- Anything that needs human approval is refused over MCP and reported back, never run
  unattended.
- The HTTP app speaks Streamable HTTP with sessions, requires the bearer token when one
  is set, and rejects browser requests from origins you haven't allowed.
