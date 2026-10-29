# MCP and OpenAPI tools

You do not have to write a Python function for every capability. Nexura can turn the tools of any MCP server, or every operation in an OpenAPI 3 spec, into agent tools. This page shows both, how approval policies work for them, and what the network safety rules are. All examples run offline against in-process servers.

## An MCP server as tools

The quickest runnable setup serves an `AgentMCPServer` as an ASGI app and connects to it with `MCPServer.http`, passing an `httpx.ASGITransport` so no socket is opened. (A real setup replaces the transport with a URL; see below.)

```python
import asyncio

import httpx

from nexura_agents import Agent, AgentMCPServer, FakeModel, MCPServer, ToolCall, tool


@tool
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


app = AgentMCPServer("calc", tools=[add]).http_app()


async def main() -> None:
    transport = httpx.ASGITransport(app=app)
    async with MCPServer.http("http://calc.test/mcp", transport=transport) as calc:
        tools = await calc.tools()
        assert [t.name for t in tools] == ["add"]

        model = FakeModel(
            responses=[[ToolCall(id="1", name="add", arguments={"a": 2, "b": 40})], "42"]
        )
        agent = Agent(name="Calc", instructions="Use tools.", model=model, tools=tools)
        result = await agent.run_async("2 + 40?")
        assert result.output == "42"
        # MCP results arrive as text.
        assert [m for m in result.messages if m.role == "tool"][0].content == "42"


asyncio.run(main())
```

`await server.tools()` connects if needed, lists the server's tools and returns a list of `SchemaTool`s that you pass to `Agent(tools=...)`. Keep the server open (`async with`, or `await server.connect()` and `await server.close()`) while the agent runs; a tool called after the session is closed raises `ToolExecutionError` and ends the run.

Each tool's name and input schema are pinned when you call `tools()`. If a later `list_tools(refresh=True)` shows that the server changed a pinned tool, calls to it are refused until you build the tools again.

### Connecting to real servers

`MCPServer.stdio` starts a local server as a subprocess. `MCPServer.http` connects to a remote one over Streamable HTTP. These need a subprocess or a network, so the blocks are not run by the docs tests.

```python notest
from nexura_agents import Agent, MCPServer

files = MCPServer.stdio(
    "npx",
    "-y",
    "@modelcontextprotocol/server-filesystem",
    "./docs",
    requires_approval="writes",
    read_only={"read_file", "list_directory"},
)
remote = MCPServer.http(
    "https://mcp.example.com/mcp",
    headers={"Authorization": f"Bearer {token}"},
    prefix="remote",
)

async with files, remote:
    agent = Agent(
        name="Assistant",
        instructions="Help with the docs.",
        model="vertex:gemini-2.5-flash",
        tools=[*await files.tools(), *await remote.tools()],
    )
    print((await agent.run_async("Summarise ./docs")).output)
```

`MCPServer.stdio(command, *args, env=None, cwd=None, allowed_commands=None)` only launches an allow-listed program: python, python3, uv, uvx, node and npx. Pass `allowed_commands={...}` to use another executable. `MCPServer.http(url, headers=None, timeout=30.0, transport=None)` takes extra headers for authentication. For HTTP the endpoint's own host is trusted, and loopback and private addresses are allowed by default, because the URL is your explicit choice.

## Protocol versions

Nexura speaks MCP **2026-07-28** (the current revision, with a stateless core) and the
older handshake revisions **2025-11-25, 2025-06-18, 2025-03-26 and 2024-11-05**. Both the
client (`MCPServer`, `McpClientSession`) and the server (`AgentMCPServer`,
`McpServerSession`) prefer 2026-07-28.

How the client picks a version (once per stdio process or HTTP endpoint):

1. It sends `server/discover` with 2026-07-28 in `params._meta` (over HTTP also in the
   `MCP-Protocol-Version` and `Mcp-Method` headers).
2. A discover result that lists 2026-07-28: the session is stateless. Every request
   carries `io.modelcontextprotocol/protocolVersion`, `clientInfo` and
   `clientCapabilities` in `_meta`; there is no `initialize` and no HTTP session.
3. `UnsupportedProtocolVersionError` (-32022): a mutual stateless version is retried;
   if the server lists only handshake versions, the handshake is used; if it lists
   nothing Nexura speaks, `McpProtocolVersionMismatchError` is raised.
4. Any other answer (a legacy server's -32601/-32602, an HTTP 400/404/405 without a
   2026-07-28 error, or on stdio no answer within `discover_timeout`, 10 s by default):
   the server is treated as legacy and the `initialize` handshake runs.

`session.era` is `"modern"` or `"legacy"` afterwards and `session.server_version` holds the
version in use. To skip detection pass `protocol_version=` to `MCPServer` (or
`McpClientSession`): a 2026-07-28 pin never falls back; a handshake version such as
`"2025-06-18"` runs `initialize` offering it.

What 2026-07-28 support covers:

- **Tools, resources and prompts** over stdio and Streamable HTTP, with `resultType` on
  every result (a missing one from an older server counts as `"complete"`).
- **Request headers.** Over HTTP the client derives `MCP-Protocol-Version`, `Mcp-Method`,
  `Mcp-Name` (tool or prompt name, resource URI) and `Mcp-Param-{Name}` (tool parameters
  annotated with `x-mcp-header`) from each request body, Base64-encoding values that are
  not plain ASCII. A tool whose `x-mcp-header` annotations break the specification is
  left out of `list_tools()` with a warning. The server rejects any header that disagrees
  with the body with HTTP 400 and `HeaderMismatch` (-32020).
- **Cache hints.** The server puts `ttlMs` and `cacheScope` on discover, list and
  `resources/read` results (`list_ttl_ms`, default 60 s on `AgentMCPServer` and 0 on
  `McpServerSession`; `resources/read` is always 0; scope `"private"` by default). The
  client records them in `session.cache_hints`. It does not change its own caching:
  `MCPServer.list_tools()` keeps its list until `refresh=True`, and pinned calls re-list
  as before (`tool_list_max_age`).
- **Multi round-trip requests.** If a server answers `tools/call`, `resources/read` or
  `prompts/get` with `resultType: "input_required"`, the client retries with a new
  request id, echoing `requestState` unchanged. `inputRequests` (elicitation, sampling,
  roots) are answered only through `McpClientSession(input_handler=...)`, together with
  the matching `client_capabilities`; otherwise `McpInputRequiredError` is raised. Retries
  stop after `max_input_rounds` (8). Nexura's own server never asks for input: tools that
  need approval are refused, as before.
- **Authorization.** The client sends the `headers` you pass; it does not run an OAuth
  flow. If you run your own authorization-code flow, `check_metadata_issuer` and
  `check_authorization_response_issuer` (in `nexura_agents.mcp`) apply the RFC 8414 issuer
  check and the RFC 9207 `iss` check that 2026-07-28 requires before the code is redeemed.

Not implemented: `subscriptions/listen` (so the server advertises no `listChanged` on
2026-07-28 and sends no change notifications), progress notifications, completion,
pagination cursors (lists are returned whole), resource templates (the list is always
empty), and the deprecated Roots, Sampling and Logging features. None of the optional
extensions is implemented: **Tasks** (`io.modelcontextprotocol/tasks`), **MCP Apps**
(`io.modelcontextprotocol/ui`) and **Enterprise Managed Authorization**. Neither is the
deprecated HTTP+SSE transport of 2024-11-05: `MCPServer.http` speaks Streamable HTTP only.

**Verified:** the offline tests in `experiments/interop` (a separate uv project) run
Nexura against the official MCP Python SDK in both directions, over stdio and over
Streamable HTTP on 127.0.0.1: the `mcp` 2.3.0 client (`mode="auto"`, `"2026-07-28"` and
`"legacy"`) against `AgentMCPServer`; Nexura's client against an `mcp` 2.3.0 server
(stateless, including an `x-mcp-header` parameter the reference server enforces); the
handshake-only `mcp` 1.30.0 client against `AgentMCPServer`; and Nexura's client falling
back to 2025-11-25 against an `mcp` 1.30.0 server. Desktop MCP apps (Claude Desktop,
Cursor, VS Code) and public MCP servers have not been tested.

## Options common to both constructors

Both constructors pass these keyword arguments on to `MCPServer`:

- `prefix`: prepended to every tool name as `prefix_name`. Use it when two servers expose a tool with the same name. Names are sanitised to letters, digits, `_` and `-`, and cut to 64 characters.
- `include` and `exclude`: keep only the named tools, or drop some. Names refer to the server's own tool names, before the prefix.
- `requires_approval`: the approval policy (next section).
- `read_only`: names of tools you vouch have no side effects. They are not paused by `requires_approval="writes"` and get the `pure` effect class, so a durable run repeats them after a crash instead of pausing. Every other MCP tool stays `non_idempotent`.
- `max_result_chars`: results longer than this (default `20_000`) are truncated, with a note saying how much was cut. `None` turns truncation off.
- `request_timeout`: seconds to wait for each server reply. The default comes from the transport (60 seconds for stdio, the `timeout` value for HTTP).
- `name`: a label used in module ids and logs.

## Timeouts, guardrails and enabled

`openapi_tools`, `MCPServer` and `module_tool` accept the same per-tool options as
`@tool`, and apply them to every tool they build:

```python
from typing import Any

from nexura_agents import GuardrailResult, RunContext, openapi_tools


def no_big_refunds(ctx: RunContext[Any], args: dict[str, Any]) -> GuardrailResult:
    if ctx.tool_name == "createRefund" and args.get("amount", 0) > 100:
        return GuardrailResult.rejected("refunds over 100 need a manager")
    return GuardrailResult.passed()


spec = {
    "openapi": "3.0.3",
    "info": {"title": "Shop", "version": "1"},
    "servers": [{"url": "https://shop.example.com/v1"}],
    "paths": {
        "/refunds": {
            "post": {
                "operationId": "createRefund",
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {"amount": {"type": "number"}},
                            }
                        }
                    }
                },
                "responses": {"201": {"description": "created"}},
            }
        }
    },
}
tools = openapi_tools(spec, timeout=10, input_guardrails=[no_big_refunds])
assert tools[0].timeout == 10.0
```

- `timeout`, `on_timeout`, `input_guardrails`, `output_guardrails` and `enabled` mean the
  same as on [`@tool`](tools.md). A guardrail or `enabled` rule sees `ctx.tool_name`, so
  one function can treat each operation differently.
- On `MCPServer` the timeout option is called `tool_timeout`, because `MCPServer.http`
  already uses `timeout` for the HTTP connection. `request_timeout` still limits each
  protocol request.
- Invalid values raise `SDKValidationError`: at call time for `openapi_tools` and
  `module_tool`, and when `await server.tools()` builds the tools for `MCPServer`.

## Approval policies

To govern an external MCP client with an approval id and exact-action retry, see
the offline [cross-framework governance prototype](crossframework-governance.md).
It adds an explicit application-level pending-result handler; stock
`AgentMCPServer` tools still refuse approval-gated calls without a resume path.

`requires_approval` accepts these forms:

| Value | Meaning |
|---|---|
| `False` (default) | Never pause. |
| `True` | Pause before every tool. |
| `"writes"` | Pause before every tool that is not in `read_only`. |
| a set or list of names | Pause for those tools. The names are the final tool names, including any `prefix`. |
| a function `(tool_name, arguments) -> bool` | Decide per call. The name is the final tool name. |

MCP servers describe tools with hints such as `readOnlyHint`, but a server can say anything, so those hints are ignored. A tool is only treated as read-only if you list it in `read_only`. The hints are kept as metadata only: each of the four boolean hints of the MCP specification (`readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`) becomes a tag such as `mcp.hint:readOnlyHint=true` on the tool (other annotation keys are ignored), which a hook or policy can read but which never changes approval or the effect class.

Each tool also says where it comes from, so a hook or guardrail can treat a whole server or API alike (see [Tool metadata](tools.md#tool-metadata-for-hooks-guardrails-and-policies)): an MCP tool has the origin `mcp:<server name>:<tool>` and the tag `toolset:<server name>`; an OpenAPI tool has the origin `openapi:<METHOD>:<path>` and the tag `toolset:<toolset>`, where `openapi_tools(..., toolset=...)` defaults to `prefix`, else `openapi`.

```python
import asyncio

import httpx

from nexura_agents import (
    Agent,
    AgentMCPServer,
    FakeModel,
    MCPServer,
    Runner,
    ToolCall,
    tool,
)

deleted: list[str] = []


@tool
def read_note(note_id: str) -> str:
    """Read a note."""
    return f"note {note_id}"


@tool
def delete_note(note_id: str) -> str:
    """Delete a note."""
    deleted.append(note_id)
    return "deleted"


app = AgentMCPServer("notes", tools=[read_note, delete_note]).http_app()


async def main() -> None:
    transport = httpx.ASGITransport(app=app)
    server = MCPServer.http(
        "http://notes.test/mcp",
        transport=transport,
        requires_approval="writes",
        read_only={"read_note"},
    )
    async with server:
        tools = await server.tools()
        agent_model = FakeModel(
            responses=[
                [ToolCall(id="1", name="read_note", arguments={"note_id": "1"})],
                [ToolCall(id="2", name="delete_note", arguments={"note_id": "1"})],
                "Deleted.",
            ]
        )
        agent = Agent(name="Notes", instructions="Manage notes.", model=agent_model, tools=tools)
        runner = Runner()

        result = await runner.run_async(agent, "Read note 1, then delete it")
        # read_note ran; delete_note is waiting for a human.
        assert result.status == "paused"
        assert [c.name for c in result.interruption.pending] == ["delete_note"]
        assert deleted == []

        result = await runner.resume_async(agent, result.interruption, approve=True)
        assert result.status == "completed"
        assert deleted == ["1"]


asyncio.run(main())
```

A name set and a predicate work the same way: `requires_approval={"delete_note"}` or `requires_approval=lambda name, args: name == "delete_note" or args.get("force") is True`.

## REST APIs from an OpenAPI spec

`openapi_tools(spec)` returns one tool per operation, named after its `operationId`. `spec` is a dict or a JSON string (YAML is not parsed). If you do not pass `base_url`, the first entry of the spec's `servers` is used.

For durable runs every OpenAPI tool is `non_idempotent`: a crash during the call pauses the run for a person instead of sending the request again. The spec's HTTP method and `x-idempotent` are the API's own claims (real DELETE and PUT endpoints often refund, notify or bill), so they are recorded only as tags (`openapi.method:<METHOD>`, and `openapi.hint:idempotent=true` for GET, HEAD, PUT, DELETE and `x-idempotent` operations). Declare what you vouch for yourself, by `operationId`: `read_only=[...]` makes an operation `pure` (repeated after a crash) and `idempotent=[...]` makes it `idempotent` (repeated with the same `ctx.idempotency_key`). An unknown `operationId`, or one in both lists, raises `SDKValidationError`. These declarations change crash recovery only, never approval: `requires_approval="writes"` still pauses before every non-GET/HEAD call.

```python
import httpx

from nexura_agents import openapi_tools

spec = {
    "openapi": "3.0.3",
    "info": {"title": "Shop", "version": "1"},
    "servers": [{"url": "https://shop.example.com/v1"}],
    "paths": {
        "/orders/{order_id}": {
            "get": {"operationId": "getOrder", "responses": {"200": {"description": "ok"}}},
            "delete": {"operationId": "cancelOrder", "responses": {"200": {"description": "ok"}}},
        }
    },
}
transport = httpx.MockTransport(lambda request: httpx.Response(200, json={}))
tools = {t.name: t for t in openapi_tools(spec, transport=transport, read_only=["getOrder"])}

assert tools["getOrder"].info().effect == "pure"  # declared by you
assert tools["cancelOrder"].info().effect == "non_idempotent"  # DELETE claims idempotence
assert "openapi.method:DELETE" in tools["cancelOrder"].tags  # ...recorded as a tag only
```

Credentials go in `headers`. They are added to every request and never shown to the model. With `requires_approval="writes"`, GET and HEAD calls run freely and every other method pauses first. Even without that option, a workflow that runs an OpenAPI operation module (or an agent that calls one as a tool) pauses for approval before any non-GET/HEAD call; the module refuses to send it unless a human approved.

```python
import asyncio

import httpx

from nexura_agents import Agent, FakeModel, Runner, ToolCall, openapi_tools

SPEC = {
    "openapi": "3.0.0",
    "info": {"title": "Shop", "version": "1"},
    "servers": [{"url": "https://api.shop.example/v1"}],
    "paths": {
        "/orders/{order_id}": {
            "get": {
                "operationId": "getOrder",
                "summary": "Get an order",
                "parameters": [
                    {
                        "name": "order_id",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                    }
                ],
                "responses": {"200": {"description": "ok"}},
            }
        },
        "/orders/{order_id}/cancel": {
            "post": {
                "operationId": "cancelOrder",
                "summary": "Cancel an order",
                "parameters": [
                    {
                        "name": "order_id",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                    }
                ],
                "responses": {"200": {"description": "ok"}},
            }
        },
    },
}

seen: list[httpx.Request] = []


def handler(request: httpx.Request) -> httpx.Response:
    seen.append(request)
    return httpx.Response(200, json={"id": "A1", "status": "open"})


tools = openapi_tools(
    SPEC,
    headers={"Authorization": "Bearer secret-key"},
    requires_approval="writes",
    transport=httpx.MockTransport(handler),
)
assert [t.name for t in tools] == ["getOrder", "cancelOrder"]

model = FakeModel(
    responses=[
        [ToolCall(id="1", name="getOrder", arguments={"order_id": "A1"})],
        [ToolCall(id="2", name="cancelOrder", arguments={"order_id": "A1"})],
        "Cancelled.",
    ]
)
agent = Agent(name="Shop", instructions="Manage orders.", model=model, tools=tools)
runner = Runner()

result = runner.run(agent, "Look at order A1, then cancel it")
assert result.status == "paused"  # the POST is waiting
assert len(seen) == 1 and seen[0].method == "GET"
assert seen[0].url.path == "/v1/orders/A1"
assert seen[0].headers["authorization"] == "Bearer secret-key"

result = runner.resume(agent, result.interruption, approve=True)
assert result.status == "completed"
assert [r.method for r in seen] == ["GET", "POST"]
```

The tool result the model sees is `{"status_code": ..., "body": ...}`. Response headers and timing are not included.

Other `openapi_tools` options: `prefix` (tool name prefix), `include` and `exclude` (by `operationId`), `max_result_chars`, `requires_approval` (same forms as above; `"writes"` means any operation that is not a read), and `transport` (an `httpx` transport, used for tests).

### Network safety

OpenAPI calls go through a client that checks every destination, including each redirect:

- Cloud metadata and link-local addresses (such as `169.254.169.254` and `100.100.100.200`) are always refused.
- Loopback and every other non-public range (`127.0.0.1`, `10.x`, `192.168.x`, carrier-grade NAT, multicast, site-local IPv6, IPv4 hidden inside IPv6, ...) are refused.
- The connection goes to the address that passed the check, so a name that resolves somewhere else a moment later (DNS rebinding) cannot reach an internal address. An injected `transport` (tests) is not pinned.
- A redirect to another origin forwards only `Accept`, `Accept-Encoding`, `Accept-Language`, `User-Agent` and (when the body is kept) `Content-Type`; every other header you supplied, including custom credential headers, is dropped. A 307 or 308 keeps the method and the body; a `HEAD` stays a `HEAD`. More than `max_redirects` hops raises an error. A refused destination raises `ToolExecutionError`, which ends the run; it is not sent back to the model as a result.
- The API's own host from the spec is not trusted: it is checked like any other destination, both before the request and again on the pinned connection. A spec whose `servers` URL is an IP address, or a name that resolves (or later rebinds) to a private or loopback address, is refused unless you opt in with `allow_loopback=True` or list the host in `allowed_hosts`.

If you really need to call a private or local API, you must opt in. In `openapi_tools` that is `allow_loopback=True`, which permits both loopback and private ranges. This is for tests and trusted internal services; do not enable it for a spec you did not write.

```python
import asyncio

import httpx

from nexura_agents import Agent, FakeModel, ToolCall, openapi_tools
from nexura_agents.errors import ToolExecutionError

SPEC = {
    "openapi": "3.0.0",
    "info": {"title": "Local", "version": "1"},
    "servers": [{"url": "http://127.0.0.1:9000"}],
    "paths": {
        "/ping": {
            "get": {
                "operationId": "ping",
                "summary": "Ping",
                "responses": {"200": {"description": "ok"}},
            }
        }
    },
}


def handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"pong": True})


def run(allow: bool) -> str:
    tools = openapi_tools(SPEC, allow_loopback=allow, transport=httpx.MockTransport(handler))
    model = FakeModel(responses=[[ToolCall(id="1", name="ping", arguments={})], "ok"])
    agent = Agent(name="Local", instructions="Ping.", model=model, tools=tools)
    result = agent.run("ping")
    return [m for m in result.messages if m.role == "tool"][0].content


try:
    run(False)
except ToolExecutionError as exc:
    assert "loopback" in str(exc)  # refused before any request was made
else:
    raise AssertionError("expected the request to be refused")

assert '"pong": true' in run(True)
```

To trust one specific private host without opening every private range, pass `allowed_hosts=[...]` to `openapi_tools`.

```python
import httpx

from nexura_agents import Agent, FakeModel, ToolCall, openapi_tools
from nexura_agents.errors import ToolExecutionError

SPEC = {
    "openapi": "3.0.0",
    "info": {"title": "Internal", "version": "1"},
    "servers": [{"url": "http://10.0.0.5:9000"}],
    "paths": {
        "/ping": {
            "get": {
                "operationId": "ping",
                "summary": "Ping",
                "responses": {"200": {"description": "ok"}},
            }
        }
    },
}


def handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"pong": True})


def run(allowed_hosts: list[str]) -> str:
    tools = openapi_tools(SPEC, allowed_hosts=allowed_hosts, transport=httpx.MockTransport(handler))
    model = FakeModel(responses=[[ToolCall(id="1", name="ping", arguments={})], "ok"])
    agent = Agent(name="Internal", instructions="Ping.", model=model, tools=tools)
    result = agent.run("ping")
    return [m for m in result.messages if m.role == "tool"][0].content


try:
    run([])
except ToolExecutionError as exc:
    assert "private network address" in str(exc)
else:
    raise AssertionError("expected the request to be refused")

assert '"pong": true' in run(["10.0.0.5"])
```

## Resources and prompts

An MCP server can also offer resources (documents) and prompt templates. `MCPServer`
exposes both. Resource and prompt text comes from the server, so treat it as untrusted
data: the SDK never adds it to an agent's instructions for you.

- `await mcp.list_resources()` and `await mcp.read_resource(uri)` return definitions and
  the resource text. Binary (`blob`) contents are not decoded; they become a placeholder
  like `[binary content omitted: image/png, about 9 bytes]`.
- `await mcp.list_prompts()` and `await mcp.get_prompt(name, arguments)` return
  `Message` objects with role `user` or `assistant`. Unknown prompts, unknown or missing
  arguments, other roles and non-text content raise `McpIntegrationError`.
- A server without the capability raises `McpIntegrationError` (code
  `MCP_CAPABILITY_MISSING`); a JSON-RPC error reply raises `McpJsonRpcError`.
- `await mcp.resource_tools()` is the opt-in way to let a model use resources. It
  returns `<prefix>_list_resources` and `<prefix>_read_resource` (set `prefix` when you
  use several servers). The model can read only URIs the server listed; other URIs and
  server errors come back as `{"error": ..., "message": ...}`. The server's tool options
  (`requires_approval`, `tool_timeout`, guardrails, `max_result_chars`) apply.

```python
import asyncio
import json

from nexura_agents import Agent, FakeModel, MCPServer, ToolCall
from nexura_agents.mcp import McpInMemoryTransport, McpServerSession
from nexura_agents.mcp.types import McpPromptArgument


async def main() -> None:
    client_side, server_side = McpInMemoryTransport.create_pair()
    server = McpServerSession(server_side)
    server.register_resource("file:///policy.txt", "policy", "Refunds within 30 days.")

    async def summarize(args: dict) -> list[dict]:
        text = {"type": "text", "text": f"Summarize {args['doc']}"}
        return [{"role": "user", "content": text}]

    server.register_prompt(
        "summarize", "Summarize a document.", [McpPromptArgument(name="doc")], summarize
    )
    serving = asyncio.create_task(server.serve_loop())

    async with MCPServer(client_side, name="docs", prefix="docs") as mcp:
        prompt = await mcp.get_prompt("summarize", {"doc": "policy"})
        assert prompt[0].role == "user" and prompt[0].content == "Summarize policy"

        calls = [
            ToolCall(id="r1", name="docs_read_resource", arguments={"uri": "file:///policy.txt"}),
            ToolCall(id="r2", name="docs_read_resource", arguments={"uri": "file:///secret"}),
        ]
        agent = Agent(
            name="Support",
            instructions="Answer from the documents.",
            model=FakeModel(responses=[calls, "Refunds take 30 days."]),
            tools=await mcp.resource_tools(),
        )
        result = await agent.run_async("What is the refund policy?")

    serving.cancel()
    tool_replies = {m.tool_call_id: m.content for m in result.messages if m.role == "tool"}
    assert tool_replies["r1"] == "Refunds within 30 days."
    assert json.loads(tool_replies["r2"])["error"] == "RESOURCE_NOT_LISTED"


asyncio.run(main())
```

## Other modules

Anything that implements `BaseModule` becomes a tool with `module_tool(module)`; see [Tools](tools.md).

## See also

- [Tools](tools.md)
- [Human approval](approvals.md)
- [Serving agents over MCP](serving-over-mcp.md)
- [Testing your agents](testing.md)
- [Troubleshooting](troubleshooting.md)

## Tools from MCP servers and REST APIs

Any [MCP](https://modelcontextprotocol.io) server — local over stdio or remote over
Streamable HTTP — becomes a set of agent tools:

```python notest
from nexura_agents import Agent, MCPServer

files = MCPServer.stdio(
    "npx",
    "-y",
    "@modelcontextprotocol/server-filesystem",
    "./docs",
    requires_approval="writes",
    read_only={"read_file", "list_directory"},
)
github = MCPServer.http(
    "https://api.githubcopilot.com/mcp/", headers={"Authorization": f"Bearer {token}"}, prefix="gh"
)

async with files, github:
    agent = Agent(
        name="Assistant",
        instructions="...",
        model="vertex:gemini-2.5-flash",
        tools=[*await files.tools(), *await github.tools()],
    )
    print((await agent.run_async("Summarise docs/ and open an issue for any TODOs")).output)
```

An OpenAPI 3 spec gives one tool per operation. Credentials go in `headers`; the model
never sees them, and they are redacted from results:

```python notest
from nexura_agents import Agent, openapi_tools

tools = openapi_tools(
    spec,
    base_url="https://api.shop.example/v1",
    headers={"Authorization": f"Bearer {api_key}"},
    requires_approval="writes",
)  # GET runs, POST/PUT/DELETE pause
agent = Agent(name="Shop", instructions="...", model="openai:gpt-4.1-mini", tools=tools)
```

Both work offline in tests. For example, with a scripted model and an in-memory MCP server:

```python
import asyncio

from nexura_agents import Agent, FakeModel, MCPServer, ToolCall
from nexura_agents.mcp import McpInMemoryTransport, McpServerSession


async def main() -> None:
    client_side, server_side = McpInMemoryTransport.create_pair()
    server = McpServerSession(server_side)

    async def add(args: dict) -> dict:
        return {"sum": args["a"] + args["b"]}

    server.register_tool(
        "add",
        "Add two integers.",
        {
            "type": "object",
            "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
            "required": ["a", "b"],
        },
        add,
    )
    serving = asyncio.create_task(server.serve_loop())

    async with MCPServer(client_side) as calc:
        model = FakeModel(
            responses=[[ToolCall(id="1", name="add", arguments={"a": 2, "b": 40})], "42"]
        )
        agent = Agent(name="Calc", instructions="Use tools.", model=model, tools=await calc.tools())
        print((await agent.run_async("2 + 40?")).output)  # 42
    serving.cancel()


asyncio.run(main())
```

What you get for free:
- **Approvals.** `requires_approval` takes `True`, `"writes"`, a set of tool names or a
  predicate. MCP servers' own hints such as `readOnlyHint` are untrusted, so they never
  skip approval; mark tools read-only yourself with `read_only=`.
- **Tool pinning.** Each MCP tool's name and schema are pinned when you call `tools()`.
  If a later `list_tools(refresh=True)` shows the server changed a pinned tool, calls to
  it are refused until you build the tools again.
- **Errors go to the model.** Tool errors, HTTP 4xx/5xx and invalid arguments come back as
  results the model can react to, instead of ending the run.
- **Context protection.** Results longer than `max_result_chars` (20,000) are truncated
  with a note.
- **Safe networking.** OpenAPI calls may only reach the API's own host; cloud metadata,
  loopback and private addresses are refused unless you allow them.
- **Safe launching.** `MCPServer.stdio` only starts allow-listed launchers (python, uv,
  uvx, node, npx); pass `allowed_commands=` to add others.

Any other `BaseModule` (HTTP, SQL, your own) becomes a tool with `module_tool(module)`.
