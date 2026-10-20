# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""B00/B01 module adapter converting MCP tools into native BaseModules
with anti-poisoning defenses."""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING, Any

from nexura_agents.contracts.base import BaseModule
from nexura_agents.contracts.canonical import canonical_digest
from nexura_agents.contracts.context import ExecutionContext
from nexura_agents.contracts.manifest import EffectPolicy, ModuleManifest
from nexura_agents.contracts.result import ModuleResult
from nexura_agents.mcp.client import McpClientSession
from nexura_agents.mcp.errors import (
    McpSecurityError,
    McpToolReplacementError,
)
from nexura_agents.mcp.types import McpToolDefinition

if TYPE_CHECKING:
    from nexura_agents.mcp.server import McpServerSession


class McpToolModule(BaseModule):
    """B00/B01 module wrapping a remote MCP tool with digest pinning and anti-poisoning defenses."""

    def __init__(
        self,
        client: McpClientSession | None = None,
        tool_def: McpToolDefinition = ...,  # type: ignore[assignment]
        *,
        server_name: str = "mcp",
        effect_policy: EffectPolicy = "non_idempotent_write",
        is_gated: bool = False,
        version: str = "1.0.0",
        server: McpServerSession | None = None,
        session_factory: Any | None = None,
    ) -> None:
        self.client = client
        self.session_factory = session_factory
        self.tool_def = tool_def
        self.server_name = server_name
        self._is_gated = is_gated
        self.server = server

        clean_srv = re.sub(r"[^a-z0-9_-]", "_", server_name.lower()).strip("_") or "srv"
        clean_name = re.sub(r"[^a-z0-9_-]", "_", tool_def.name.lower()).strip("_") or "tool"
        mod_id = f"mcp.{clean_srv}.{clean_name}"

        # ANTI-POISONING DEFENSE:
        # Untrusted descriptions can never bypass operator approval or downgrade effect policy.
        desc_lower = tool_def.description.lower()
        if (
            "bypass approval" in desc_lower or "read only" in desc_lower or "pure" in desc_lower
        ) and effect_policy == "pure":
            # If untrusted description attempts to claim read-only/pure without operator backing
            raise McpSecurityError(
                f"Untrusted MCP tool '{tool_def.name}' description attempted to claim "
                f"unverified effect policy in description text: '{tool_def.description[:60]}...'. "
                "Policies must be explicitly set by the operator.",
                details={"tool_name": tool_def.name},
            )

        # Pin the canonical digest of the tool definition at discovery time
        self._expected_digest = canonical_digest(
            {
                "name": tool_def.name,
                "inputSchema": tool_def.inputSchema,
            }
        )

        self._manifest = ModuleManifest(
            schema_version=1,
            module_id=mod_id,
            version=version,
            kind="integration",
            title=f"MCP Tool: {tool_def.name}",
            description=tool_def.description or f"MCP tool {tool_def.name} from {server_name}",
            input_schema=tool_def.inputSchema,
            output_schema={
                "type": "object",
                "properties": {
                    "result": {},
                    "content": {"type": "array"},
                },
            },
            config_schema={"type": "object"},
            named_ports=[],
            capabilities={"streaming": False, "network": True, "gated": is_gated},
            effect_policy=effect_policy,
            dependency_extra=None,
            min_sdk_version="0.3.0",
            timeout_seconds=30.0,
            output_limit_bytes=5_000_000,
            required_credential_types=[],
            supported_modalities=["text"],
            supported_locales=["all"],
            source_reference=f"mcp:{server_name}:{tool_def.name}",
            verification_status="mock_verified",
        )

    @property
    def manifest(self) -> ModuleManifest:
        return self._manifest

    @property
    def expected_digest(self) -> str:
        return self._expected_digest

    async def invoke(
        self,
        context: ExecutionContext,
        inputs: dict[str, Any],
        config: dict[str, Any] | None = None,
    ) -> ModuleResult:
        # 1. Enforce digest validation against tool replacement attacks
        actual_digest = canonical_digest(
            {
                "name": self.tool_def.name,
                "inputSchema": self.tool_def.inputSchema,
            }
        )
        if actual_digest != self._expected_digest:
            raise McpToolReplacementError(
                self.tool_def.name,
                self._expected_digest,
                actual_digest,
            )

        client = self.client
        cleanup_fn = None
        server_task = None

        if self.session_factory is not None:
            sess_res = self.session_factory()
            if asyncio.iscoroutine(sess_res):
                sess_res = await sess_res
            client, cleanup_fn = sess_res
        elif self.server is not None:
            server_task = asyncio.create_task(self.server.serve_loop())

        if client is None:
            raise RuntimeError(
                f"MCP tool '{self.tool_def.name}' has no active client or session factory."
            )

        try:
            if not client.is_initialized:
                await client.initialize()

            # 2. Invoke remote tool via MCP client session
            res = await client.call_tool(
                self.tool_def.name,
                inputs,
                expected_digest=self._expected_digest,
            )

            content_dicts = [b.model_dump() for b in res.content]
            primary_text = res.content[0].text if res.content else None

            if res.isError:
                return ModuleResult.failure(
                    error_code="MCP_TOOL_ERROR",
                    error_message=primary_text or "MCP tool returned error.",
                    metadata={"content": content_dicts, "result": primary_text},
                )

            return ModuleResult.success(
                outputs={
                    "result": primary_text,
                    "content": content_dicts,
                }
            )
        finally:
            if server_task is not None:
                server_task.cancel()
            if cleanup_fn is not None:
                if asyncio.iscoroutinefunction(cleanup_fn):
                    await cleanup_fn()
                elif callable(cleanup_fn):
                    c_res = cleanup_fn()
                    if asyncio.iscoroutine(c_res):
                        await c_res


async def import_mcp_tools(
    client: McpClientSession,
    *,
    server_name: str = "mcp",
    default_effect_policy: EffectPolicy = "non_idempotent_write",
    policy_overrides: dict[str, EffectPolicy] | None = None,
) -> list[McpToolModule]:
    """Discover tools from an MCP server and wrap them into B00/B01 McpToolModule instances."""
    overrides = policy_overrides or {}
    tool_defs = await client.list_tools()
    modules: list[McpToolModule] = []

    for t in tool_defs:
        policy = overrides.get(t.name, default_effect_policy)
        mod = McpToolModule(
            client=client,
            tool_def=t,
            server_name=server_name,
            effect_policy=policy,
        )
        modules.append(mod)

    return modules
