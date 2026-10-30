# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Server session and provider implementation for Model Context Protocol (MCP)."""

from __future__ import annotations

import json
from collections.abc import Callable, Coroutine
from typing import Any

from nexura_agents.contracts.base import BaseModule
from nexura_agents.contracts.context import ExecutionContext
from nexura_agents.mcp.transports import McpTransport
from nexura_agents.mcp.types import (
    CACHEABLE_METHODS,
    LATEST_HANDSHAKE_VERSION,
    LATEST_PROTOCOL_VERSION,
    META_CLIENT_CAPABILITIES,
    META_PROTOCOL_VERSION,
    META_SERVER_INFO,
    SUPPORTED_PROTOCOL_VERSIONS,
    UNSUPPORTED_PROTOCOL_VERSION,
    McpContentBlock,
    McpImplementation,
    McpPromptArgument,
    McpPromptDefinition,
    McpResourceDefinition,
    McpToolCallResult,
    McpToolDefinition,
    is_modern_version,
)

ToolHandler = Callable[[dict[str, Any]], Coroutine[Any, Any, Any]]
PromptHandler = Callable[[dict[str, Any]], Coroutine[Any, Any, list[dict[str, Any]]]]


class McpServerSession:
    """Server hosting tools, resources, and Nexura modules as an MCP endpoint.

    Dual-era: a request whose ``params._meta`` carries
    ``io.modelcontextprotocol/protocolVersion`` is served statelessly under that
    (2026-07-28) revision, with ``server/discover``, ``resultType`` and cache hints; an
    ``initialize`` request selects one of the handshake revisions for that connection.

    Args:
        transport: Where requests come from (unused when ``handle_request`` is fed directly).
        server_info: Name and version reported to clients.
        protocol_version: Pin one version; the server then speaks only that one.
        list_ttl_ms: ``ttlMs`` freshness hint on discover and list results (2026-07-28).
            0 (the default) tells clients to re-fetch; raise it when the tool, resource and
            prompt lists do not change while the server runs.
        cache_scope: ``"private"`` (default; caches are per authorization context) or
            ``"public"`` (lists are identical for every caller).
        instructions: Optional guidance returned by ``server/discover``.
    """

    def __init__(
        self,
        transport: McpTransport,
        *,
        server_info: McpImplementation | None = None,
        protocol_version: str | None = None,
        list_ttl_ms: int = 0,
        cache_scope: str = "private",
        instructions: str | None = None,
    ) -> None:
        if list_ttl_ms < 0:
            raise ValueError("list_ttl_ms must be >= 0.")
        if cache_scope not in ("public", "private"):
            raise ValueError("cache_scope must be 'public' or 'private'.")
        self.transport = transport
        self.server_info = server_info or McpImplementation(
            name="nexura-mcp-server",
            version="1.0.0",
        )
        # Pinning a version makes this server speak only that version.
        self.protocol_version = protocol_version or LATEST_PROTOCOL_VERSION
        self.supported_versions = (
            [protocol_version] if protocol_version else list(SUPPORTED_PROTOCOL_VERSIONS)
        )
        self.list_ttl_ms = list_ttl_ms
        self.cache_scope = cache_scope
        self.instructions = instructions
        self._tools: dict[str, McpToolDefinition] = {}
        self._tool_handlers: dict[str, ToolHandler] = {}
        self._resources: dict[str, tuple[McpResourceDefinition, str]] = {}
        self._prompts: dict[str, McpPromptDefinition] = {}
        self._prompt_handlers: dict[str, PromptHandler] = {}
        self._is_running = False

    def register_tool(
        self,
        name: str,
        description: str,
        input_schema: dict[str, Any],
        handler: ToolHandler,
    ) -> None:
        """Register a native tool handler."""
        self._tools[name] = McpToolDefinition(
            name=name,
            description=description,
            inputSchema=input_schema,
        )
        self._tool_handlers[name] = handler

    def tool_definition(self, name: str) -> McpToolDefinition | None:
        """The registered definition of tool ``name``, if any."""
        return self._tools.get(name)

    def register_module(
        self,
        module: BaseModule,
        *,
        tool_name: str | None = None,
    ) -> None:
        """Expose an existing Nexura B00/B01 BaseModule as an MCP tool."""
        name = tool_name or module.manifest.module_id.replace(".", "_")
        desc = module.manifest.description

        async def _module_handler(args: dict[str, Any]) -> Any:
            ctx = ExecutionContext(
                run_id="mcp_server_run",
                node_id=f"node_{name}",
            )
            res = await module.execute(ctx, args, {})
            if res.status != "success":
                raise RuntimeError(res.error_message or "Module execution failed.")
            return res.outputs

        self.register_tool(
            name=name,
            description=desc,
            input_schema=module.manifest.input_schema,
            handler=_module_handler,
        )

    def register_resource(
        self,
        uri: str,
        name: str,
        content: str,
        *,
        description: str = "",
        mime_type: str = "text/plain",
    ) -> None:
        """Register an in-memory document resource."""
        res_def = McpResourceDefinition(
            uri=uri,
            name=name,
            description=description,
            mimeType=mime_type,
        )
        self._resources[uri] = (res_def, content)

    def register_prompt(
        self,
        name: str,
        description: str,
        arguments: list[McpPromptArgument],
        handler: PromptHandler,
    ) -> None:
        """Register a prompt template handler."""
        self._prompts[name] = McpPromptDefinition(
            name=name,
            description=description,
            arguments=arguments,
        )
        self._prompt_handlers[name] = handler

    async def handle_request(self, req: dict[str, Any]) -> dict[str, Any] | None:
        """Handle an incoming JSON-RPC request and produce a response dictionary.

        Unexpected failures become a generic -32603 so no internal detail leaks and
        the client always gets a reply.
        """
        try:
            return await self._dispatch(req)
        except Exception:
            req_id = req.get("id")
            if req_id is None:
                return None
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32603, "message": "Internal error", "data": None},
            }

    def modern_versions(self) -> list[str]:
        """Supported revisions served statelessly (no ``initialize``)."""
        return [v for v in self.supported_versions if is_modern_version(v)]

    def check_envelope(self, params: Any) -> tuple[int, str, Any] | None:
        """Validate a modern request's ``_meta`` envelope: ``(code, message, data)`` or None."""
        meta = params.get("_meta") if isinstance(params, dict) else None
        if not isinstance(meta, dict):
            return (-32602, "Invalid params: params._meta must be an object.", None)
        missing = [k for k in (META_PROTOCOL_VERSION, META_CLIENT_CAPABILITIES) if k not in meta]
        if missing:
            return (-32602, f"Invalid params: _meta is missing {', '.join(missing)}.", None)
        version = meta[META_PROTOCOL_VERSION]
        if not isinstance(version, str):
            return (-32602, "Invalid params: the protocol version must be a string.", None)
        if not isinstance(meta[META_CLIENT_CAPABILITIES], dict):
            return (-32602, "Invalid params: clientCapabilities must be an object.", None)
        if version not in self.modern_versions():
            return (
                UNSUPPORTED_PROTOCOL_VERSION,
                "Unsupported protocol version",
                {"supported": list(self.supported_versions), "requested": version},
            )
        return None

    async def _dispatch(self, req: dict[str, Any]) -> dict[str, Any] | None:
        req_id = req.get("id")
        method = req.get("method")
        params = req.get("params") or {}

        # Handle notification (no id)
        if req_id is None:
            return None

        if _has_modern_meta(params) or method == "server/discover":
            return await self._dispatch_modern(req_id, method, params)

        def _make_res(result_val: Any) -> dict[str, Any]:
            return {"jsonrpc": "2.0", "id": req_id, "result": result_val}

        def _make_err(code: int, msg: str, data: Any = None) -> dict[str, Any]:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": code, "message": msg, "data": data},
            }

        if method == "ping":
            return _make_res({})

        if not isinstance(params, dict):
            return _make_err(-32602, "Invalid params: expected an object.")

        if method == "initialize":
            requested = params.get("protocolVersion")
            # Handshake versions only: the stateless revisions have no initialize.
            offers = [v for v in self.supported_versions if not is_modern_version(v)]
            if not offers:
                return _make_err(
                    UNSUPPORTED_PROTOCOL_VERSION,
                    "This server speaks only stateless MCP revisions, which have no "
                    f"initialize; supported versions: {', '.join(self.supported_versions)}.",
                    {"supported": list(self.supported_versions), "requested": requested},
                )
            # Echo the client's version when supported; otherwise offer ours.
            if requested in offers:
                version = requested
            elif LATEST_HANDSHAKE_VERSION in offers:
                version = LATEST_HANDSHAKE_VERSION
            else:
                version = offers[0]
            return _make_res(
                {
                    "protocolVersion": version,
                    "capabilities": {
                        "tools": {"listChanged": True},
                        "resources": {"listChanged": True},
                        "prompts": {"listChanged": True},
                    },
                    "serverInfo": self.server_info.model_dump(),
                }
            )

        if method == "tools/list":
            tools_list = [t.model_dump(exclude_none=True) for t in self._tools.values()]
            return _make_res({"tools": tools_list})

        if method == "tools/call":
            tool_name = params.get("name")
            tool_args = params.get("arguments") or {}
            if not isinstance(tool_name, str) or not isinstance(tool_args, dict):
                return _make_err(-32602, "Invalid params: name must be a string.")

            if tool_name not in self._tool_handlers:
                return _make_err(-32601, f"Tool '{tool_name}' not found.")

            handler = self._tool_handlers[tool_name]
            try:
                res_data = await handler(tool_args)
                result_obj = McpToolCallResult(
                    content=[McpContentBlock(type="text", text=_as_text(res_data))],
                    isError=False,
                )
                return _make_res(result_obj.model_dump())
            except Exception as exc:
                result_obj = McpToolCallResult(
                    content=[McpContentBlock(type="text", text=str(exc))],
                    isError=True,
                )
                return _make_res(result_obj.model_dump())

        if method == "resources/list":
            resources_list = [r[0].model_dump() for r in self._resources.values()]
            return _make_res({"resources": resources_list})

        if method == "resources/read":
            uri = params.get("uri")
            if not isinstance(uri, str):
                return _make_err(-32602, "Invalid params: uri must be a string.")
            if uri not in self._resources:
                return _make_err(-32602, f"Resource URI '{uri}' not found.")
            res_def, content_str = self._resources[uri]
            return _make_res(
                {
                    "contents": [
                        {
                            "uri": uri,
                            "mimeType": res_def.mimeType,
                            "text": content_str,
                        }
                    ]
                }
            )

        if method == "prompts/list":
            prompts_list = [p.model_dump() for p in self._prompts.values()]
            return _make_res({"prompts": prompts_list})

        if method == "prompts/get":
            prompt_name = params.get("name")
            prompt_args = params.get("arguments") or {}
            if not isinstance(prompt_name, str) or not isinstance(prompt_args, dict):
                return _make_err(-32602, "Invalid params: name must be a string.")
            if prompt_name not in self._prompt_handlers:
                return _make_err(-32601, f"Prompt '{prompt_name}' not found.")

            p_handler = self._prompt_handlers[prompt_name]
            try:
                messages = await p_handler(prompt_args)
                return _make_res(
                    {
                        "description": self._prompts[prompt_name].description,
                        "messages": messages,
                    }
                )
            except Exception as exc:
                return _make_err(-32000, f"Prompt error: {exc}")

        if method == "resources/templates/list":
            return _make_res({"resourceTemplates": []})

        return _make_err(-32601, f"Method '{method}' not implemented.")

    # ------------------------------------------------------------------ 2026-07-28

    def discover_result(self) -> dict[str, Any]:
        """The ``server/discover`` result body (without the cache hint fields)."""
        result: dict[str, Any] = {
            "supportedVersions": list(self.supported_versions),
            # No subscriptions/listen stream is offered, so no listChanged here.
            "capabilities": {"tools": {}, "resources": {}, "prompts": {}},
        }
        if self.instructions:
            result["instructions"] = self.instructions
        return result

    async def _dispatch_modern(self, req_id: Any, method: Any, params: Any) -> dict[str, Any]:
        def _make_err(code: int, msg: str, data: Any = None) -> dict[str, Any]:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": code, "message": msg, "data": data},
            }

        problem = self.check_envelope(params)
        if problem is not None:
            return _make_err(*problem)
        assert isinstance(params, dict)

        if method == "server/discover":
            result: dict[str, Any] = self.discover_result()
        elif method in (
            "tools/list",
            "tools/call",
            "resources/list",
            "resources/read",
            "resources/templates/list",
            "prompts/list",
            "prompts/get",
        ):
            # The method bodies are the same as for the handshake revisions; only the
            # error codes for unknown names differ (-32602 under 2026-07-28).
            inner = await self._dispatch(
                {"jsonrpc": "2.0", "id": req_id, "method": method, "params": _strip_meta(params)}
            )
            assert inner is not None
            if "error" in inner:
                error = dict(inner["error"])
                if error.get("code") == -32601 and method in ("tools/call", "prompts/get"):
                    error["code"] = -32602  # unknown tool / prompt name
                if error.get("code") == -32000:
                    error["code"] = -32603  # -32000..-32019 are legacy-only codes
                return {"jsonrpc": "2.0", "id": req_id, "error": error}
            result = dict(inner["result"])
        else:
            return _make_err(-32601, f"Method '{method}' not found.")

        result["resultType"] = "complete"
        meta = dict(result.get("_meta") or {})
        meta[META_SERVER_INFO] = self.server_info.model_dump()
        result["_meta"] = meta
        if method in CACHEABLE_METHODS:
            result["ttlMs"] = 0 if method == "resources/read" else self.list_ttl_ms
            result["cacheScope"] = self.cache_scope
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    async def serve_once(self) -> None:
        """Read and process a single message from the transport."""
        msg = await self.transport.receive_message()
        resp = await self.handle_request(msg)
        if resp is not None:
            await self.transport.send_message(resp)

    async def serve_loop(self) -> None:
        """Continuously process incoming requests until closed."""
        self._is_running = True
        try:
            while self._is_running:
                await self.serve_once()
        except Exception:
            pass
        finally:
            self._is_running = False

    async def close(self) -> None:
        """Stop serving and close transport."""
        self._is_running = False
        await self.transport.close()


def _as_text(value: Any) -> str:
    """Tool results travel as text; non-strings are sent as JSON, not Python repr."""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


def _has_modern_meta(params: Any) -> bool:
    meta = params.get("_meta") if isinstance(params, dict) else None
    return isinstance(meta, dict) and META_PROTOCOL_VERSION in meta


def _strip_meta(params: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in params.items() if k != "_meta"}
