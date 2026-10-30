# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Offline MCP calculator registered for Studio and workflow examples."""

from __future__ import annotations

from typing import Any

from nexura_agents.mcp import (
    McpClientSession,
    McpInMemoryTransport,
    McpServerSession,
    McpToolDefinition,
    McpToolModule,
)


def register_default_mcp_tools(registry: Any) -> None:
    # Pre-register a default offline local MCP calculator tool into default_registry
    _c_trans, _s_trans = McpInMemoryTransport.create_pair()
    _default_server = McpServerSession(_s_trans)

    async def _default_calc(args: dict[str, Any]) -> dict[str, Any]:
        a = float(args.get("a", 0))
        b = float(args.get("b", 0))
        op = str(args.get("op", "add"))
        if op == "multiply":
            val = a * b
        elif op == "subtract":
            val = a - b
        else:
            val = a + b
        return {"a": a, "b": b, "op": op, "result": val}

    _default_calc_def = McpToolDefinition(
        name="calculator",
        description="Offline mathematical calculator tool.",
        inputSchema={
            "type": "object",
            "properties": {
                "a": {"type": "number"},
                "b": {"type": "number"},
                "op": {"type": "string", "enum": ["add", "subtract", "multiply"]},
            },
            "required": ["a", "b"],
        },
    )
    _default_server.register_tool(
        name=_default_calc_def.name,
        description=_default_calc_def.description,
        input_schema=_default_calc_def.inputSchema,
        handler=_default_calc,
    )
    _default_client = McpClientSession(_c_trans)

    registry.register(
        McpToolModule(
            client=_default_client,
            tool_def=_default_calc_def,
            server_name="local",
            server=_default_server,
            effect_policy="pure",
        )
    )
