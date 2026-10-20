# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Typed data contracts and schemas for Model Context Protocol (MCP)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

# Newest first. 2025-03-26 introduced Streamable HTTP; 2025-06-18 requires the
# MCP-Protocol-Version header on HTTP requests after initialization.
SUPPORTED_PROTOCOL_VERSIONS = ["2025-06-18", "2025-03-26", "2024-11-05"]
LATEST_PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[0]


class JsonRpcRequest(BaseModel):
    """JSON-RPC 2.0 Request payload."""

    jsonrpc: str = "2.0"
    id: str | int
    method: str
    params: dict[str, Any] | None = None


class JsonRpcNotification(BaseModel):
    """JSON-RPC 2.0 Notification payload without id."""

    jsonrpc: str = "2.0"
    method: str
    params: dict[str, Any] | None = None


class JsonRpcErrorObj(BaseModel):
    """JSON-RPC 2.0 error descriptor."""

    code: int
    message: str
    data: Any = None


class JsonRpcResponse(BaseModel):
    """JSON-RPC 2.0 Response payload."""

    jsonrpc: str = "2.0"
    id: str | int | None = None
    result: Any = None
    error: JsonRpcErrorObj | None = None


class McpImplementation(BaseModel):
    """Client or server implementation identity metadata."""

    name: str
    version: str = "1.0.0"


class McpServerCapabilities(BaseModel):
    """Capabilities exposed by an MCP server."""

    tools: dict[str, Any] | None = Field(default_factory=dict)
    resources: dict[str, Any] | None = None
    prompts: dict[str, Any] | None = None
    logging: dict[str, Any] | None = None


class McpClientCapabilities(BaseModel):
    """Capabilities declared by an MCP client."""

    roots: dict[str, Any] | None = Field(default_factory=dict)
    sampling: dict[str, Any] | None = None


class McpToolDefinition(BaseModel):
    """Definition of an exposed MCP tool."""

    name: str
    description: str = ""
    inputSchema: dict[str, Any] = Field(default_factory=lambda: {"type": "object"})
    # Server-declared hints (readOnlyHint, destructiveHint, ...). Untrusted: they are
    # shown to operators but never used to skip an approval.
    annotations: dict[str, Any] | None = None


class McpResourceDefinition(BaseModel):
    """Definition of an exposed MCP resource."""

    uri: str
    name: str
    description: str = ""
    mimeType: str = "text/plain"


class McpPromptArgument(BaseModel):
    """Argument descriptor for an MCP prompt template."""

    name: str
    description: str = ""
    required: bool = False  # the MCP spec default when a server omits it


class McpPromptDefinition(BaseModel):
    """Definition of an exposed MCP prompt."""

    name: str
    description: str = ""
    arguments: list[McpPromptArgument] = Field(default_factory=list)


class McpContentBlock(BaseModel):
    """Content item returned from tool invocation or resource retrieval."""

    type: str = "text"
    text: str | None = None
    data: str | None = None
    mimeType: str | None = None
    resource: dict[str, Any] | None = None
    # Set on resources/read contents: the resource URI and, for binary resources, the
    # base64 ``blob`` (kept so callers can tell binary contents from empty text).
    uri: str | None = None
    blob: str | None = None


class McpToolCallResult(BaseModel):
    """Structured result returned from tools/call."""

    content: list[McpContentBlock] = Field(default_factory=list)
    isError: bool = False
