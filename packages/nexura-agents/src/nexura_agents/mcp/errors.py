# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Typed error hierarchy for Model Context Protocol (MCP) interoperability."""

from __future__ import annotations

from typing import Any


class McpIntegrationError(Exception):
    """Base exception for all MCP client, server, and transport operations."""

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class McpProtocolVersionMismatchError(McpIntegrationError):
    """Raised when an MCP server or client negotiates an unsupported protocol version."""

    def __init__(self, negotiated_version: str, supported_versions: list[str]) -> None:
        super().__init__(
            f"MCP protocol version mismatch: received '{negotiated_version}', "
            f"supported versions are {supported_versions}.",
            details={
                "negotiated_version": negotiated_version,
                "supported_versions": supported_versions,
                "error_code": "PROTOCOL_VERSION_MISMATCH",
            },
        )


class McpToolReplacementError(McpIntegrationError):
    """Raised when a tool schema or identity changes between discovery and invocation."""

    def __init__(self, tool_name: str, expected_digest: str, actual_digest: str) -> None:
        super().__init__(
            f"MCP tool replacement detected for '{tool_name}': manifest digest changed "
            f"from '{expected_digest[:12]}' to '{actual_digest[:12]}'. "
            "Tool mutation or substitution is prohibited.",
            details={
                "tool_name": tool_name,
                "expected_digest": expected_digest,
                "actual_digest": actual_digest,
                "error_code": "TOOL_REPLACEMENT_DETECTED",
            },
        )


class McpCommandNotAllowedError(McpIntegrationError):
    """Raised when an unauthorized command executable is requested for stdio transport."""

    def __init__(self, command: str, allowed_commands: list[str]) -> None:
        super().__init__(
            f"Command '{command}' is not in the allowlisted stdio executables: {allowed_commands}. "
            "Arbitrary shell commands are prohibited.",
            details={
                "command": command,
                "allowed_commands": allowed_commands,
                "error_code": "COMMAND_NOT_ALLOWED",
            },
        )


class McpSecurityError(McpIntegrationError):
    """Raised when untrusted metadata attempts to bypass policy or mutate security constraints."""

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(
            message,
            details={"error_code": "MCP_SECURITY_VIOLATION", **(details or {})},
        )


class McpTransportError(McpIntegrationError):
    """Raised when a transport failure occurs during communication."""

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(
            message,
            details={"error_code": "MCP_TRANSPORT_ERROR", **(details or {})},
        )


class McpJsonRpcError(McpIntegrationError):
    """Raised when an MCP JSON-RPC protocol-level error response is received."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(
            f"MCP JSON-RPC error ({code}): {message}",
            details={"rpc_code": code, "rpc_message": message, "rpc_data": data},
        )
        self.code = code
