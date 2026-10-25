# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Serve Nexura agents and tools over MCP, so Claude Desktop, Cursor, VS Code or any
other MCP client can use them.

    server = AgentMCPServer("support", agents=[support_agent], tools=[lookup_order])
    server.run_stdio()                       # local clients launch this as a command
    app = server.http_app(token="secret")    # or serve over HTTP: uvicorn module:app

Each agent becomes one tool that takes a ``request`` string and returns the agent's
answer. Tools that need human approval are refused over MCP rather than run unattended,
and a run that pauses for approval is reported back as an error, not executed.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import secrets
import sys
import time
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable, MutableMapping, Sequence
from typing import Any

from pydantic import BaseModel

from nexura_agents import policy
from nexura_agents.agent import Agent
from nexura_agents.mcp.headers import (
    METHOD_HEADER,
    NAME_HEADER,
    PROTOCOL_VERSION_HEADER,
    InvalidHeaderAnnotation,
    check_param_headers,
    decode_value,
    header_paths,
    header_safe,
)
from nexura_agents.mcp.server import McpServerSession
from nexura_agents.mcp.transports import McpTransport
from nexura_agents.mcp.types import (
    HANDSHAKE_PROTOCOL_VERSIONS,
    HEADER_MISMATCH,
    META_PROTOCOL_VERSION,
    NAME_BEARING_METHODS,
    SUPPORTED_PROTOCOL_VERSIONS,
    McpImplementation,
)
from nexura_agents.run_context import RunContext
from nexura_agents.teams import Team
from nexura_agents.tools import Tool
from nexura_agents.types import ToolCall

Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]


class ApprovalRequiredOverMCP(Exception):
    """A call needed a human decision, which MCP clients cannot give here."""


class ToolGuardrailRejected(Exception):
    """A tool guardrail rejected the call or its result; the message says which."""


class ToolNotAvailable(Exception):
    """The tool's ``enabled`` rule refused this call."""


class ToolTimedOut(Exception):
    """A served tool did not finish within its ``timeout``."""


class _NoTransport(McpTransport):
    """Requests are fed to ``handle_request`` directly; the transport is never used."""

    async def send_message(self, message: dict[str, Any]) -> None:
        raise RuntimeError("unused")

    async def receive_message(self, timeout: float | None = None) -> dict[str, Any]:
        raise RuntimeError("unused")

    async def close(self) -> None:
        return None


class AgentMCPServer:
    """Expose agents and tools as an MCP server.

    Args:
        name: Server name shown to MCP clients.
        agents: Each becomes a tool named after the agent (``Agent(description=...)``
            is shown to the client, so describe what the agent is for). Teams
            (``SequentialAgent`` and friends) are served the same way.
        tools: Plain tools, exposed with their own schemas.
        deps: Passed to every run and tool call as ``RunContext.deps``.
        runner: Runner used for agent calls (for usage limits, hooks or budgets), and for
            teams that were not given their own runner. Plain tools use the hooks a run of
            this runner would use (``Runner.effective_hooks()``: the process default from
            ``set_default_hooks`` plus the runner's own): ``before_tool``, ``after_tool``
            and ``tool_needs_approval`` apply to them as in a run.
        list_ttl_ms: ``ttlMs`` cache hint on 2026-07-28 discover and list results. The
            tool list is fixed when the server is built, so clients may reuse it for this
            long (default 60 s).
        instructions: Optional guidance for the client's model, sent by ``server/discover``.

    Both MCP eras are served: 2026-07-28 clients send stateless requests (no session), and
    clients of the handshake revisions (2025-11-25 back to 2024-11-05) send ``initialize``.
    """

    def __init__(
        self,
        name: str,
        *,
        agents: Sequence[Agent | Team] = (),
        tools: Sequence[Tool] = (),
        deps: Any = None,
        runner: Any = None,
        version: str = "0.4.0",
        list_ttl_ms: int = 60_000,
        instructions: str | None = None,
    ) -> None:
        from nexura_agents.runner import Runner

        self.name = name
        self.deps = deps
        self.runner = runner or Runner()
        self.info = McpImplementation(name=name, version=version)
        self.session = McpServerSession(
            _NoTransport(),
            server_info=self.info,
            list_ttl_ms=list_ttl_ms,
            instructions=instructions,
        )
        seen: set[str] = set()
        for agent in agents:
            self._add(seen, _agent_tool_name(agent))
            self._register_agent(agent)
        for tool in tools:
            self._add(seen, tool.name)
            self._register_tool(tool)

    @staticmethod
    def _add(seen: set[str], name: str) -> None:
        if name in seen:
            raise ValueError(f"Two agents or tools would both be exposed as '{name}'.")
        seen.add(name)

    # ---------------------------------------------------------------- registration

    def _register_agent(self, agent: Agent | Team) -> None:
        async def handler(args: dict[str, Any]) -> str:
            if isinstance(agent, Team):
                team_result = await agent._run(
                    args["request"], deps=self.deps, timeout=None, runner=self.runner
                )
                status: str = team_result.status
                output = team_result.output
                held = team_result.interruption
                pending = [c for _, c in held.pending_calls()] if held else []
            else:
                result = await self.runner.run_async(agent, args["request"], deps=self.deps)
                status = result.status
                output = result.output
                pending = result.interruption.pending if result.interruption else []
            if status == "paused" and pending:
                names = ", ".join(c.name for c in pending)
                if isinstance(agent, Team):
                    raise ApprovalRequiredOverMCP(
                        f"Stopped before {names}: these actions need human approval, which "
                        "cannot be given over MCP. Team members before the pause already ran "
                        "and their effects stand."
                    )
                raise ApprovalRequiredOverMCP(
                    f"Stopped before {names}: these actions need human approval, which "
                    "cannot be given over MCP. Nothing was executed."
                )
            if isinstance(output, BaseModel):
                return output.model_dump_json()
            return output if isinstance(output, str) else json.dumps(output, default=str)

        kind = "team" if isinstance(agent, Team) else "agent"
        self.session.register_tool(
            _agent_tool_name(agent),
            agent.description or f"Ask the {agent.name} {kind}.",
            {
                "type": "object",
                "properties": {"request": {"type": "string", "description": "What to do."}},
                "required": ["request"],
            },
            handler,
        )

    def _hooks(self) -> Any:
        """The hooks for plain tool calls: those a run of ``self.runner`` would use."""
        effective = getattr(self.runner, "effective_hooks", None)
        # Plain tools make no model calls of their own: only the tool stages apply here.
        return (
            effective()
            if callable(effective)
            else policy.effective_hooks(applies_model_stages=True)
        )

    def _register_tool(self, tool: Tool) -> None:
        if tool.enabled is False:
            return  # never offered, as in the runner
        schema = tool.to_schema()["function"]

        async def handler(args: dict[str, Any]) -> str:
            call_id = f"mcp-{uuid.uuid4().hex[:12]}"
            ctx: RunContext[Any] = RunContext(
                deps=self.deps,
                run_id=call_id,
                agent_name=self.name,
                tool_call_id=call_id,
                tool_name=tool.name,
                tool=tool.info(),
            )
            if not await tool.is_enabled(ctx):
                # An ``enabled`` rule that says no for this call: refuse, as the runner does.
                raise ToolNotAvailable(f"Tool '{tool.name}' is not available.")
            hooks = self._hooks()
            # The tool's own gate, or a hook's (P39 G2): approval cannot be given over MCP.
            if policy.tool_call_needs_approval(tool, ctx, args, hooks):
                raise ApprovalRequiredOverMCP(
                    f"'{tool.name}' needs human approval for these arguments, which cannot "
                    "be given over MCP. Nothing was executed."
                )

            async def execute() -> str:
                try:
                    return await asyncio.wait_for(tool.execute(args, ctx), timeout=tool.timeout)
                except TimeoutError as expired:
                    # Either way the client gets an error result; nothing is half-returned.
                    raise ToolTimedOut(
                        f"Tool call timed out: '{tool.name}' did not finish within "
                        f"{tool.timeout:g}s."
                    ) from expired

            # The tool's guardrails and the hooks' before_tool / after_tool apply as in a
            # run; a guardrail rejection is an error result.
            call = ToolCall(id=call_id, name=tool.name, arguments=dict(args))
            result, rejected = await policy.run_tool_call(tool, ctx, call, hooks, execute)
            if rejected:
                raise ToolGuardrailRejected(result)
            return str(result)

        self.session.register_tool(tool.name, schema["description"], schema["parameters"], handler)

    # ---------------------------------------------------------------- stdio

    def run_stdio(self) -> None:
        """Serve on stdin/stdout until the client closes the pipe (blocks)."""
        asyncio.run(self.serve_stdio())

    async def serve_stdio(
        self,
        reader: Callable[[], Awaitable[str]] | None = None,
        writer: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        """Newline-delimited JSON-RPC over stdio. Logs must go to stderr, never stdout."""

        async def read_stdin() -> str:
            return await asyncio.to_thread(sys.stdin.readline)

        async def write_stdout(line: str) -> None:
            sys.stdout.write(line)
            sys.stdout.flush()

        read = reader or read_stdin
        write = writer or write_stdout
        pending: set[asyncio.Task[None]] = set()
        lock = asyncio.Lock()

        async def answer(message: dict[str, Any]) -> None:
            try:
                reply = await self.session.handle_request(message)
            except Exception:
                reply = {
                    "jsonrpc": "2.0",
                    "id": message.get("id"),
                    "error": {"code": -32603, "message": "Internal error"},
                }
            if reply is not None and reply.get("id") is not None:
                async with lock:
                    await write(json.dumps(reply, ensure_ascii=False) + "\n")

        while True:
            line = await read()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                error = {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": "Parse error"},
                }
                async with lock:
                    await write(json.dumps(error) + "\n")
                continue
            if isinstance(message, dict) and "method" in message:
                # Calls run concurrently so a slow agent doesn't block pings.
                task = asyncio.create_task(answer(message))
                pending.add(task)
                task.add_done_callback(pending.discard)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    # ---------------------------------------------------------------- HTTP

    def http_app(
        self,
        *,
        token: str | None = None,
        path: str = "/mcp",
        allowed_origins: Sequence[str] = (),
        max_body_bytes: int = 4 * 1024 * 1024,
        max_sessions: int = 1000,
        session_ttl_seconds: float = 3600.0,
    ) -> Callable[[Scope, Receive, Send], Awaitable[None]]:
        """An ASGI app speaking MCP Streamable HTTP. Run it with any ASGI server.

        ``token`` requires ``Authorization: Bearer <token>``; always set one unless the
        server is bound to localhost. Browser requests are refused unless their
        ``Origin`` is listed in ``allowed_origins`` (DNS-rebinding protection).
        Bodies over ``max_body_bytes`` get 413.

        2026-07-28 requests are stateless: the ``MCP-Protocol-Version``, ``Mcp-Method``,
        ``Mcp-Name`` and ``Mcp-Param-*`` headers must match the body (else 400 with
        ``HeaderMismatch`` -32020), an unsupported version gets 400 with -32022, an
        unknown method 404, and no session is created. Handshake-revision clients get a
        session from ``initialize``: sessions idle past ``session_ttl_seconds`` expire, and
        beyond ``max_sessions`` the least recently used one is evicted.
        """
        # id -> last-used time, least recently used first.
        sessions: OrderedDict[str, float] = OrderedDict()

        def live_session(session_id: str | None) -> bool:
            now = time.monotonic()
            while sessions:
                oldest = next(iter(sessions))
                if now - sessions[oldest] <= session_ttl_seconds:
                    break
                del sessions[oldest]
            if session_id is None or session_id not in sessions:
                return False
            sessions[session_id] = now
            sessions.move_to_end(session_id)
            return True

        async def respond(
            send: Send, status: int, body: Any = None, headers: dict[str, str] | None = None
        ) -> None:
            raw = b"" if body is None else json.dumps(body, ensure_ascii=False).encode()
            hdrs = [(b"content-type", b"application/json")] if body is not None else []
            hdrs += [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
            await send({"type": "http.response.start", "status": status, "headers": hdrs})
            await send({"type": "http.response.body", "body": raw})

        async def app(scope: Scope, receive: Receive, send: Send) -> None:
            if scope["type"] == "lifespan":
                while True:
                    event = await receive()
                    if event["type"] == "lifespan.startup":
                        await send({"type": "lifespan.startup.complete"})
                    elif event["type"] == "lifespan.shutdown":
                        await send({"type": "lifespan.shutdown.complete"})
                        return
            if scope["type"] != "http":
                return
            # HTTP header bytes are latin-1; decoding as UTF-8 can fail on hostile input.
            headers = {
                k.decode("latin-1").lower(): v.decode("latin-1")
                for k, v in scope.get("headers", [])
            }
            if scope["path"] != path:
                await respond(send, 404, {"error": "not found"})
                return
            origin = headers.get("origin")
            if origin and origin not in allowed_origins:
                await respond(send, 403, {"error": "origin not allowed"})
                return
            if token is not None:
                supplied = headers.get("authorization", "").encode("latin-1")
                if not hmac.compare_digest(supplied, f"Bearer {token}".encode()):
                    await respond(
                        send, 401, {"error": "unauthorized"}, {"WWW-Authenticate": "Bearer"}
                    )
                    return

            method = scope["method"]
            session_id = headers.get("mcp-session-id")
            if method == "DELETE":
                sessions.pop(session_id or "", None)
                await respond(send, 200 if session_id else 400)
                return
            if method == "GET":
                await respond(send, 405, None, {"Allow": "POST, DELETE"})
                return
            if method != "POST":
                await respond(send, 405, None, {"Allow": "POST, DELETE"})
                return

            declared = headers.get("content-length", "")
            too_large = declared.isdigit() and int(declared) > max_body_bytes
            body = b""
            while not too_large:
                event = await receive()
                body += event.get("body", b"")
                if len(body) > max_body_bytes:
                    too_large = True
                elif not event.get("more_body"):
                    break
            if too_large:
                await respond(send, 413, {"error": "request body too large"})
                return
            try:
                message = json.loads(body)
            except json.JSONDecodeError:
                await respond(
                    send,
                    400,
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32700, "message": "Parse error"},
                    },
                )
                return
            if not isinstance(message, dict) or "method" not in message:
                # Responses to server requests (we send none) and batches are not accepted.
                await respond(
                    send,
                    400,
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32600, "message": "Invalid request"},
                    },
                )
                return

            header_version = headers.get("mcp-protocol-version")
            if _meta_version(message) is not None or (
                header_version is not None and header_version not in HANDSHAKE_PROTOCOL_VERSIONS
            ):
                await self._serve_modern(message, headers, send, respond)
                return

            if message["method"] == "initialize":
                reply = await self.session.handle_request(message)
                new_id = secrets.token_urlsafe(24)
                live_session(None)  # drop expired sessions first
                sessions[new_id] = time.monotonic()
                while len(sessions) > max_sessions:
                    sessions.popitem(last=False)
                await respond(send, 200, reply, {"Mcp-Session-Id": new_id})
                return
            if not live_session(session_id):
                await respond(
                    send,
                    404 if session_id else 400,
                    {
                        "jsonrpc": "2.0",
                        "id": message.get("id"),
                        "error": {"code": -32000, "message": "Missing or unknown Mcp-Session-Id."},
                    },
                )
                return
            version = headers.get("mcp-protocol-version")
            if version and version not in SUPPORTED_PROTOCOL_VERSIONS:
                await respond(send, 400, {"error": f"Unsupported MCP-Protocol-Version {version}"})
                return
            reply = await self.session.handle_request(message)
            if reply is None:
                await respond(send, 202)
            else:
                await respond(send, 200, reply)

        return app

    async def _serve_modern(
        self,
        message: dict[str, Any],
        headers: dict[str, str],
        send: Send,
        respond: Callable[..., Awaitable[None]],
    ) -> None:
        """One stateless 2026-07-28 request: validate the headers, then answer it."""
        if "id" not in message:
            await respond(send, 202)  # no client notifications are defined; accept and drop
            return

        def error(code: int, text: str, data: Any = None) -> dict[str, Any]:
            return {
                "jsonrpc": "2.0",
                "id": message.get("id"),
                "error": {"code": code, "message": text, "data": data},
            }

        params = message.get("params")
        problem = self.session.check_envelope(params)
        mismatch = _header_mismatch(message, headers)
        if problem is not None and problem[0] == -32602:
            await respond(send, 400, error(*problem))
            return
        if mismatch is not None:
            await respond(send, 400, error(HEADER_MISMATCH, mismatch))
            return
        if problem is not None:
            await respond(send, 400, error(*problem))
            return
        if message.get("method") == "tools/call" and isinstance(params, dict):
            definition = self.session.tool_definition(str(params.get("name")))
            arguments = params.get("arguments")
            if definition is not None and isinstance(arguments, dict):
                try:
                    paths = header_paths(definition.inputSchema)
                except InvalidHeaderAnnotation:
                    paths = {}
                lowered = {k.lower(): v for k, v in headers.items()}
                reason = check_param_headers(paths, arguments, lowered)
                if reason is not None:
                    await respond(send, 400, error(HEADER_MISMATCH, reason))
                    return

        reply = await self.session.handle_request(message)
        if reply is None:
            await respond(send, 202)
            return
        code = reply.get("error", {}).get("code") if isinstance(reply.get("error"), dict) else None
        status = 404 if code == -32601 else 400 if code in _BAD_REQUEST_CODES else 200
        await respond(send, status, reply)


# JSON-RPC errors a 2026-07-28 Streamable HTTP server answers with HTTP 400.
_BAD_REQUEST_CODES = frozenset({-32700, -32600, -32602, -32020, -32021, -32022})


def _meta_version(message: dict[str, Any]) -> Any:
    params = message.get("params")
    meta = params.get("_meta") if isinstance(params, dict) else None
    return meta.get(META_PROTOCOL_VERSION) if isinstance(meta, dict) else None


def _header_mismatch(message: dict[str, Any], headers: dict[str, str]) -> str | None:
    """Why the request metadata headers disagree with the body, or None."""
    for name in ("mcp-protocol-version", "mcp-method", "mcp-name"):
        value = headers.get(name)
        if value is not None and not header_safe(value):
            return f"The {name} header contains invalid characters."
    version = headers.get("mcp-protocol-version")
    if version is None or version != _meta_version(message):
        return (
            f"The {PROTOCOL_VERSION_HEADER} header does not match the protocol version in "
            "params._meta."
        )
    method = message.get("method")
    if headers.get("mcp-method") != method:
        return f"The {METHOD_HEADER} header does not match the request method."
    key = NAME_BEARING_METHODS.get(str(method))
    params = message.get("params")
    if key is not None and isinstance(params, dict):
        body_value = params.get(key)
        raw = headers.get("mcp-name")
        if raw is None:
            return f"The {NAME_HEADER} header is required for {method}."
        if decode_value(raw) != body_value:
            return f"The {NAME_HEADER} header does not match the request's '{key}'."
    return None


def _agent_tool_name(agent: Agent | Team) -> str:
    from nexura_agents.module_tools import tool_name

    return tool_name(agent.name.lower().replace(" ", "_"))
