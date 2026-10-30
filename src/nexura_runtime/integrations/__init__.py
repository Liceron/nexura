# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Integrations package for external services and multimodal I/O."""

from __future__ import annotations

from nexura_agents.mcp import (
    McpClientSession,
    McpIntegrationError,
    McpServerSession,
    McpToolModule,
    import_mcp_tools,
)
from nexura_agents.openapi import (
    OpenAPIOperationModule,
    import_openapi_operations,
)

from nexura_runtime.integrations.channels import (
    ChannelEnvelope,
    ChannelError,
    EmailAdapter,
    EmailChannelModule,
    WhatsAppAdapter,
    WhatsAppChannelModule,
)
from nexura_runtime.integrations.http import (
    DuplicateOperationIdError,
    HttpIntegrationError,
    HttpMaxResponseSizeExceededError,
    HttpRequestModule,
    NonIdempotentRetryForbiddenError,
    UnsupportedOpenAPIFeatureError,
)
from nexura_runtime.integrations.speech import (
    SpeechModule,
    SpeechService,
    SynthesisRequest,
    SynthesisResult,
    TranscriptionRequest,
    TranscriptionResult,
)
from nexura_runtime.integrations.sql import (
    SqlExecuteModule,
    SqlIntegrationError,
    SqlQueryModule,
    SqlReadOnlyViolationError,
    SqlSchemaInspectModule,
)
from nexura_runtime.integrations.tools import ToolModule, tool_to_module

__all__ = [
    "SpeechModule",
    "SpeechService",
    "SynthesisRequest",
    "SynthesisResult",
    "TranscriptionRequest",
    "TranscriptionResult",
    "HttpRequestModule",
    "OpenAPIOperationModule",
    "import_openapi_operations",
    "ToolModule",
    "tool_to_module",
    "McpClientSession",
    "McpServerSession",
    "McpToolModule",
    "import_mcp_tools",
    "McpIntegrationError",
    "SqlQueryModule",
    "SqlSchemaInspectModule",
    "SqlExecuteModule",
    "SqlIntegrationError",
    "SqlReadOnlyViolationError",
    "HttpIntegrationError",
    "DuplicateOperationIdError",
    "UnsupportedOpenAPIFeatureError",
    "HttpMaxResponseSizeExceededError",
    "NonIdempotentRetryForbiddenError",
    "ChannelError",
    "ChannelEnvelope",
    "EmailAdapter",
    "WhatsAppAdapter",
    "EmailChannelModule",
    "WhatsAppChannelModule",
    "SandboxExecutionModule",
    "SandboxConfig",
    "IsolatedProcessWorker",
]

from nexura_runtime.integrations.mcp_defaults import register_default_mcp_tools  # noqa: E402
from nexura_runtime.integrations.sandbox import (  # noqa: E402
    IsolatedProcessWorker,
    SandboxConfig,
    SandboxExecutionModule,
)
from nexura_runtime.modules.registry import default_registry  # noqa: E402

if not default_registry.has("mcp.local.calculator"):
    register_default_mcp_tools(default_registry)
