# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Model Context Protocol (MCP) client and server interoperability package."""

from __future__ import annotations

from nexura_agents.mcp.client import McpClientSession
from nexura_agents.mcp.errors import (
    McpCommandNotAllowedError,
    McpIntegrationError,
    McpJsonRpcError,
    McpProtocolVersionMismatchError,
    McpSecurityError,
    McpToolReplacementError,
    McpTransportError,
)
from nexura_agents.mcp.module import (
    McpToolModule,
    import_mcp_tools,
)
from nexura_agents.mcp.serve import AgentMCPServer
from nexura_agents.mcp.server import McpServerSession
from nexura_agents.mcp.toolset import MCPServer, mcp_tools
from nexura_agents.mcp.transports import (
    McpHttpTransport,
    McpInMemoryTransport,
    McpStdioTransport,
    McpTransport,
)
from nexura_agents.mcp.types import (
    LATEST_PROTOCOL_VERSION,
    SUPPORTED_PROTOCOL_VERSIONS,
    McpContentBlock,
    McpImplementation,
    McpPromptArgument,
    McpPromptDefinition,
    McpResourceDefinition,
    McpServerCapabilities,
    McpToolCallResult,
    McpToolDefinition,
)

__all__ = [
    "AgentMCPServer",
    "MCPServer",
    "mcp_tools",
    "McpClientSession",
    "McpServerSession",
    "McpToolModule",
    "import_mcp_tools",
    "McpTransport",
    "McpInMemoryTransport",
    "McpStdioTransport",
    "McpHttpTransport",
    "McpToolDefinition",
    "McpResourceDefinition",
    "McpPromptDefinition",
    "McpPromptArgument",
    "McpContentBlock",
    "McpToolCallResult",
    "McpImplementation",
    "McpServerCapabilities",
    "LATEST_PROTOCOL_VERSION",
    "SUPPORTED_PROTOCOL_VERSIONS",
    "McpIntegrationError",
    "McpProtocolVersionMismatchError",
    "McpToolReplacementError",
    "McpCommandNotAllowedError",
    "McpSecurityError",
    "McpTransportError",
    "McpJsonRpcError",
]
