# SPDX-License-Identifier: MIT
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
    LATEST_PROTOCOL_VERSION,
    SUPPORTED_PROTOCOL_VERSIONS,
    McpContentBlock,
    McpImplementation,
    McpPromptArgument,
    McpPromptDefinition,
    McpResourceDefinition,
    McpToolCallResult,
    McpToolDefinition,
)

ToolHandler = Callable[[dict[str, Any]], Coroutine[Any, Any, Any]]
PromptHandler = Callable[[dict[str, Any]], Coroutine[Any, Any, list[dict[str, Any]]]]


class McpServerSession:
    """Server hosting tools, resources, and Nexura modules as an MCP endpoint."""

    def __init__(
        self,
        transport: McpTransport,
        *,
        server_info: McpImplementation | None = None,
        protocol_version: str | None = None,
    ) -> None:
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

    async def _dispatch(self, req: dict[str, Any]) -> dict[str, Any] | None:
        req_id = req.get("id")
        method = req.get("method")
        params = req.get("params") or {}

        # Handle notification (no id)
        if req_id is None:
            return None

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
            # Echo the client's version when supported; otherwise offer ours.
            version = requested if requested in self.supported_versions else self.protocol_version
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

        return _make_err(-32601, f"Method '{method}' not implemented.")

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
