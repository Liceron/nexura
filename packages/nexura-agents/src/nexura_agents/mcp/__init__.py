# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Model Context Protocol (MCP) client and server interoperability package."""

from __future__ import annotations

from nexura_agents.mcp.auth import (
    check_authorization_response_issuer,
    check_metadata_issuer,
)
from nexura_agents.mcp.client import McpClientSession
from nexura_agents.mcp.errors import (
    McpCommandNotAllowedError,
    McpInputRequiredError,
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
    HANDSHAKE_PROTOCOL_VERSIONS,
    LATEST_HANDSHAKE_VERSION,
    LATEST_PROTOCOL_VERSION,
    MODERN_PROTOCOL_VERSIONS,
    SUPPORTED_PROTOCOL_VERSIONS,
    McpCacheHint,
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
    "LATEST_HANDSHAKE_VERSION",
    "MODERN_PROTOCOL_VERSIONS",
    "HANDSHAKE_PROTOCOL_VERSIONS",
    "SUPPORTED_PROTOCOL_VERSIONS",
    "McpCacheHint",
    "McpInputRequiredError",
    "check_authorization_response_issuer",
    "check_metadata_issuer",
    "McpIntegrationError",
    "McpProtocolVersionMismatchError",
    "McpToolReplacementError",
    "McpCommandNotAllowedError",
    "McpSecurityError",
    "McpTransportError",
    "McpJsonRpcError",
]
