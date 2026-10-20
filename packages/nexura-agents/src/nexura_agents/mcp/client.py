# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Client session implementation for Model Context Protocol (MCP)."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from nexura_agents.contracts.canonical import canonical_digest
from nexura_agents.mcp.errors import (
    McpJsonRpcError,
    McpProtocolVersionMismatchError,
    McpToolReplacementError,
    McpTransportError,
)
from nexura_agents.mcp.transports import McpTransport
from nexura_agents.mcp.types import (
    LATEST_PROTOCOL_VERSION,
    SUPPORTED_PROTOCOL_VERSIONS,
    McpContentBlock,
    McpImplementation,
    McpPromptDefinition,
    McpResourceDefinition,
    McpServerCapabilities,
    McpToolCallResult,
    McpToolDefinition,
)


class McpClientSession:
    """Client managing an MCP connection, protocol negotiation, and remote invocations."""

    def __init__(self, transport: McpTransport, *, request_timeout: float | None = None) -> None:
        self.transport = transport
        # Upper bound on waiting for one reply; ``None`` uses the transport's own default.
        self.request_timeout = (
            request_timeout if request_timeout is not None else transport.request_timeout
        )
        self._request_counter = 0
        self.server_version: str | None = None
        self.server_info: dict[str, Any] = {}
        self.server_capabilities: McpServerCapabilities = McpServerCapabilities()
        self._known_tool_digests: dict[str, str] = {}
        # Pinned tool calls re-check the server's tool list when it announces a change
        # (notifications/tools/list_changed) or when the last listing is older than this
        # many seconds; 0 re-lists before every pinned call.
        self.tool_list_max_age = 30.0
        self._tools_listed_at: float | None = None
        self._tools_changed = False
        self._is_initialized = False
        # One request in flight per session: replies are matched by id, and a shared
        # transport must not interleave two callers' messages.
        self._lock = asyncio.Lock()

    @property
    def is_initialized(self) -> bool:
        return self._is_initialized

    def _next_id(self) -> int:
        self._request_counter += 1
        return self._request_counter

    async def _send_request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        req_id = self._next_id()
        payload = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params or {},
        }
        async with self._lock:
            await self.transport.send_message(payload)
            try:
                resp = await asyncio.wait_for(
                    self._receive_response(req_id), timeout=self.request_timeout
                )
            except TimeoutError as exc:
                raise McpTransportError(
                    f"Timed out after {self.request_timeout}s waiting for reply to '{method}'."
                ) from exc

        if "error" in resp and resp["error"] is not None:
            err = resp["error"]
            raise McpJsonRpcError(
                code=err.get("code", -32000),
                message=err.get("message", "Unknown error"),
                data=err.get("data"),
            )

        return resp.get("result")

    async def _receive_response(self, req_id: int) -> dict[str, Any]:
        """Read until the reply to ``req_id``, handling whatever the server sends first.

        Servers may interleave log/progress notifications and their own requests (such
        as ``ping``) before replying; those must not be mistaken for the response.
        """
        while True:
            message = await self.transport.receive_message()
            if message.get("id") == req_id and "method" not in message:
                return message
            if "method" in message and "id" in message:
                await self._answer_server_request(message)
            elif message.get("method") == "notifications/tools/list_changed":
                self._tools_changed = True
            # Other notifications and stale replies are ignored.

    async def _answer_server_request(self, message: dict[str, Any]) -> None:
        if message["method"] == "ping":
            reply: dict[str, Any] = {"jsonrpc": "2.0", "id": message["id"], "result": {}}
        else:
            reply = {
                "jsonrpc": "2.0",
                "id": message["id"],
                "error": {"code": -32601, "message": f"Method not supported: {message['method']}"},
            }
        await self.transport.send_message(reply)

    async def _send_notification(self, method: str, params: dict[str, Any] | None = None) -> None:
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
        }
        await self.transport.send_message(payload)

    async def initialize(
        self,
        client_info: McpImplementation | None = None,
    ) -> dict[str, Any]:
        """Perform MCP initialization handshake and version negotiation."""
        c_info = client_info or McpImplementation(name="nexura-agents", version="0.4.0")
        init_params = {
            "protocolVersion": LATEST_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": c_info.model_dump(),
        }

        result = await self._send_request("initialize", init_params)
        negotiated_version = result.get("protocolVersion", "")

        if negotiated_version not in SUPPORTED_PROTOCOL_VERSIONS:
            raise McpProtocolVersionMismatchError(
                negotiated_version,
                SUPPORTED_PROTOCOL_VERSIONS,
            )

        self.server_version = negotiated_version
        self.server_info = result.get("serverInfo", {})
        self.server_capabilities = McpServerCapabilities(**result.get("capabilities", {}))

        # Send initialized confirmation notification
        await self._send_notification("notifications/initialized")
        self._is_initialized = True
        return dict(result) if isinstance(result, dict) else {}

    async def list_tools(self) -> list[McpToolDefinition]:
        """Discover tools exposed by the MCP server and record their integrity digests."""
        result = await self._send_request("tools/list")
        raw_tools = result.get("tools", [])
        tools: list[McpToolDefinition] = []
        digests: dict[str, str] = {}

        for item in raw_tools:
            tool_def = McpToolDefinition(**item)
            tools.append(tool_def)
            # Record canonical digest to detect subsequent tool replacement/poisoning
            tool_digest = canonical_digest(
                {
                    "name": tool_def.name,
                    "inputSchema": tool_def.inputSchema,
                }
            )
            digests[tool_def.name] = tool_digest

        # Replace, not merge, so tools the server removed are no longer known.
        self._known_tool_digests = digests
        self._tools_listed_at = time.monotonic()
        self._tools_changed = False
        return tools

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        expected_digest: str | None = None,
    ) -> McpToolCallResult:
        """Invoke a remote tool, verifying schema integrity against tool replacement attacks."""
        known_digest = expected_digest or self._known_tool_digests.get(name)
        if expected_digest is not None and (
            self._tools_changed
            or self._tools_listed_at is None
            or time.monotonic() - self._tools_listed_at >= self.tool_list_max_age
        ):
            # A pinned caller must see the server's current definition, not a stale cache.
            await self.list_tools()
        if known_digest is not None:
            current_digest = self._known_tool_digests.get(name)
            if current_digest is None and expected_digest is not None:
                current_digest = "<removed>"
            if current_digest is not None and current_digest != known_digest:
                raise McpToolReplacementError(name, known_digest, current_digest)

        call_params = {
            "name": name,
            "arguments": arguments or {},
        }
        res = await self._send_request("tools/call", call_params)

        content_blocks = [McpContentBlock(**b) for b in res.get("content", [])]
        is_error = bool(res.get("isError", False))
        return McpToolCallResult(content=content_blocks, isError=is_error)

    async def list_resources(self) -> list[McpResourceDefinition]:
        """List resources available on the MCP server."""
        result = await self._send_request("resources/list")
        return [McpResourceDefinition(**r) for r in result.get("resources", [])]

    async def read_resource(self, uri: str) -> list[McpContentBlock]:
        """Read content of a specified MCP resource URI."""
        result = await self._send_request("resources/read", {"uri": uri})
        return [McpContentBlock(**c) for c in result.get("contents", [])]

    async def list_prompts(self) -> list[McpPromptDefinition]:
        """List prompt templates offered by the MCP server."""
        result = await self._send_request("prompts/list")
        return [McpPromptDefinition(**p) for p in result.get("prompts", [])]

    async def get_prompt(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Retrieve a rendered prompt template from the MCP server."""
        result = await self._send_request(
            "prompts/get",
            {"name": name, "arguments": arguments or {}},
        )
        return dict(result) if isinstance(result, dict) else {}

    async def close(self) -> None:
        """Close the client session and associated transport."""
        await self.transport.close()
