# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Typed data contracts and schemas for Model Context Protocol (MCP)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

# Protocol revisions, newest first.
#
# "Modern" revisions (2026-07-28 and later) are stateless: there is no ``initialize``
# handshake and every request carries its version and client capabilities in
# ``params._meta``. "Handshake" (legacy) revisions open a session with ``initialize``.
# 2025-03-26 introduced Streamable HTTP; 2025-06-18 requires the MCP-Protocol-Version
# header on HTTP requests after initialization.
MODERN_PROTOCOL_VERSIONS = ["2026-07-28"]
HANDSHAKE_PROTOCOL_VERSIONS = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]
SUPPORTED_PROTOCOL_VERSIONS = [*MODERN_PROTOCOL_VERSIONS, *HANDSHAKE_PROTOCOL_VERSIONS]
LATEST_PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[0]
LATEST_HANDSHAKE_VERSION = HANDSHAKE_PROTOCOL_VERSIONS[0]

# Reserved ``_meta`` keys of the 2026-07-28 per-request envelope.
META_PROTOCOL_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CLIENT_INFO = "io.modelcontextprotocol/clientInfo"
META_CLIENT_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"
META_LOG_LEVEL = "io.modelcontextprotocol/logLevel"

# Error codes the 2026-07-28 specification reserves (-32020..-32099). A peer that answers
# with one of these speaks a modern revision.
HEADER_MISMATCH = -32020
MISSING_REQUIRED_CLIENT_CAPABILITY = -32021
UNSUPPORTED_PROTOCOL_VERSION = -32022
MODERN_ERROR_CODES = frozenset(
    {HEADER_MISMATCH, MISSING_REQUIRED_CLIENT_CAPABILITY, UNSUPPORTED_PROTOCOL_VERSION}
)

# Methods whose results carry ``ttlMs`` / ``cacheScope`` hints under 2026-07-28.
CACHEABLE_METHODS = frozenset(
    {
        "server/discover",
        "tools/list",
        "prompts/list",
        "resources/list",
        "resources/templates/list",
        "resources/read",
    }
)
# Methods that may answer with an ``input_required`` (multi round-trip) result.
MRTR_METHODS = frozenset({"tools/call", "resources/read", "prompts/get"})
# Method -> params key mirrored into the ``Mcp-Name`` HTTP header.
NAME_BEARING_METHODS = {"tools/call": "name", "prompts/get": "name", "resources/read": "uri"}


def is_modern_version(version: str | None) -> bool:
    """True for revisions that use the stateless per-request envelope (no handshake)."""
    return version in MODERN_PROTOCOL_VERSIONS


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
    # Optional extensions (2026-07-28): extension identifier -> settings object.
    extensions: dict[str, Any] | None = None


class McpCacheHint(BaseModel):
    """Freshness hint a 2026-07-28 server attaches to cacheable results.

    ``ttl_ms`` is how long the result may be considered fresh (0 = immediately stale;
    absent or negative values from older servers read as 0). ``scope`` is ``"public"``
    (shareable across callers) or ``"private"`` (one authorization context only).
    """

    ttl_ms: int = 0
    scope: str = "private"
    received_at: float = 0.0


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
