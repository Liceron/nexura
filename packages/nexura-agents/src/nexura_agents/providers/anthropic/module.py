# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Module wrapper exposing Native Anthropic adapter via B00/B01 contracts."""

from __future__ import annotations

import json
import os
from typing import Any

import httpx

from nexura_agents.contracts.base import BaseModule
from nexura_agents.contracts.context import ExecutionContext
from nexura_agents.contracts.manifest import ModuleManifest
from nexura_agents.contracts.result import ModuleResult
from nexura_agents.providers.anthropic.provider import AnthropicNativeProvider
from nexura_agents.providers.types import GenerationRequest
from nexura_agents.types import Message

_MOCK_TEXT = "Keyless mock response from AnthropicProviderModule"


def _mock_sse(*events: tuple[str, dict[str, Any]]) -> str:
    return "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events)


def _default_anthropic_mock_handler(request: httpx.Request) -> httpx.Response:
    """Offline keyless mock handler for default Anthropic module execution."""
    if json.loads(request.content or b"{}").get("stream"):
        usage = {"input_tokens": 12, "output_tokens": 8}
        body = _mock_sse(
            ("message_start", {"type": "message_start", "message": {"usage": usage}}),
            (
                "content_block_start",
                {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}},
            ),
            (
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": _MOCK_TEXT},
                },
            ),
            (
                "message_delta",
                {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": usage},
            ),
            ("message_stop", {"type": "message_stop"}),
        )
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, text=body)
    return httpx.Response(
        200,
        json={
            "id": "msg_mock_anthropic_001",
            "type": "message",
            "role": "assistant",
            "model": "claude-3-5-sonnet-20241022",
            "content": [
                {
                    "type": "text",
                    "text": _MOCK_TEXT,
                }
            ],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {
                "input_tokens": 12,
                "output_tokens": 8,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
        },
    )


class AnthropicProviderModule(BaseModule):
    """Module wrapper for native Anthropic Claude model adapter."""

    manifest = ModuleManifest(
        schema_version=1,
        module_id="nexura.models.anthropic",
        version="1.0.0",
        kind="model_adapter",
        title="Native Anthropic Model Adapter",
        description=(
            "Anthropic Claude native Messages adapter supporting content blocks, "
            "tools, and streaming."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["generate", "stream", "embed"],
                },
                "model": {"type": "string"},
                "api_key": {"type": "string"},
                "messages": {"type": "array"},
                "tools": {"type": "array"},
                "texts": {"type": "array"},
            },
            "required": ["operation"],
        },
        output_schema={
            "type": "object",
            "properties": {
                "content": {"type": "string"},
                "tool_calls": {"type": "array"},
                "usage": {"type": "object"},
                "embeddings": {"type": "array"},
            },
        },
        config_schema={"type": "object"},
        named_ports=[],
        capabilities={"streaming": True, "network": True},
        effect_policy="read",
        dependency_extra=None,
        min_sdk_version="0.3.0",
        timeout_seconds=60.0,
        output_limit_bytes=2_000_000,
        required_credential_types=["api_key"],
        supported_modalities=["text", "image"],
        supported_locales=["en", "hi", "ta"],
        source_reference="packages/nexura-agents/src/nexura_agents/providers/anthropic/",
        verification_status="mock_verified",
    )

    def __init__(self, provider: AnthropicNativeProvider | None = None) -> None:
        if provider is None:
            api_key = os.environ.get("ANTHROPIC_API_KEY")
            if not api_key:
                provider = AnthropicNativeProvider(
                    model="claude-3-5-sonnet-20241022",
                    transport=httpx.MockTransport(_default_anthropic_mock_handler),
                )
            else:
                provider = AnthropicNativeProvider(
                    model="claude-3-5-sonnet-20241022",
                    api_key=api_key,
                )
        self.provider = provider

    async def invoke(
        self,
        context: ExecutionContext,
        inputs: dict[str, Any],
        config: dict[str, Any] | None = None,
    ) -> ModuleResult:
        operation = inputs["operation"]
        model = inputs.get("model") or self.provider.model
        api_key = inputs.get("api_key") or self.provider._api_key

        active_provider = self.provider
        if model != self.provider.model or api_key != self.provider._api_key:
            active_provider = AnthropicNativeProvider(
                model=model,
                api_key=api_key,
                transport=self.provider.transport,
            )

        if operation == "generate":
            raw_msgs = inputs.get("messages", [])
            messages = [
                Message(
                    role=m.get("role", "user"),
                    content=m.get("content"),
                )
                for m in raw_msgs
            ]
            if not messages:
                messages = [Message(role="user", content="Ping")]

            req = GenerationRequest(messages=messages, tools=inputs.get("tools"))
            resp = await active_provider.generate(req, context=context)
            tool_calls_list = (
                [
                    {
                        "id": tc.id,
                        "name": tc.name,
                        "arguments": tc.arguments,
                    }
                    for tc in resp.message.tool_calls
                ]
                if resp.message.tool_calls
                else []
            )
            return ModuleResult.success(
                outputs={
                    "content": resp.message.content or "",
                    "tool_calls": tool_calls_list,
                },
                usage=resp.usage,
            )

        if operation == "stream":
            raw_msgs = inputs.get("messages", [])
            messages = [
                Message(
                    role=m.get("role", "user"),
                    content=m.get("content"),
                )
                for m in raw_msgs
            ]
            if not messages:
                messages = [Message(role="user", content="Ping stream")]

            req = GenerationRequest(messages=messages, tools=inputs.get("tools"))
            chunks: list[str] = []
            final_usage = None
            async for delta in active_provider.stream(req, context=context):
                if delta.text:
                    chunks.append(delta.text)
                if delta.usage:
                    final_usage = delta.usage

            return ModuleResult.success(
                outputs={
                    "content": "".join(chunks),
                    "chunks_count": len(chunks),
                },
                usage=final_usage,
            )

        if operation == "embed":
            try:
                texts = inputs.get("texts", ["default text"])
                emb = await active_provider.embed(texts, context=context)
                return ModuleResult.success(
                    outputs={
                        "embeddings": emb.embeddings,
                        "model": emb.model,
                    },
                    usage=emb.usage,
                )
            except Exception as exc:
                return ModuleResult.failure(
                    error_code="CAPABILITY_UNSUPPORTED",
                    error_message=str(exc),
                )

        return ModuleResult.failure(
            error_code="INVALID_OPERATION",
            error_message=f"Unsupported operation '{operation}'",
        )
