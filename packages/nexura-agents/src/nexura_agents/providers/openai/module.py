# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Module wrapper exposing OpenAI-compatible adapter via B00/B01 contracts."""

from __future__ import annotations

import os
from typing import Any

import httpx

from nexura_agents.contracts.base import BaseModule
from nexura_agents.contracts.context import ExecutionContext
from nexura_agents.contracts.manifest import ModuleManifest
from nexura_agents.contracts.result import ModuleResult
from nexura_agents.providers.openai.provider import OpenAIProvider
from nexura_agents.providers.types import GenerationRequest
from nexura_agents.types import Message


def _default_keyless_handler(request: httpx.Request) -> httpx.Response:
    """Offline keyless mock handler for default module execution."""
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-keyless-mock",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "Keyless mock response from OpenAIProviderModule",
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
        },
    )


class OpenAIProviderModule(BaseModule):
    """Module wrapper for OpenAI and local compatible model endpoints."""

    manifest = ModuleManifest(
        schema_version=1,
        module_id="nexura.models.openai",
        version="1.0.0",
        kind="model_adapter",
        title="OpenAI-Compatible Model Adapter",
        description=(
            "OpenAI and local OpenAI-compatible endpoint adapter (Ollama, vLLM, LocalAI)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["generate", "stream", "embed"],
                },
                "model": {"type": "string"},
                "base_url": {"type": "string"},
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
        supported_modalities=["text"],
        supported_locales=["en", "hi", "ta"],
        source_reference="packages/nexura-agents/src/nexura_agents/providers/openai/",
        verification_status="mock_verified",
    )

    def __init__(self, provider: OpenAIProvider | None = None) -> None:
        if provider is None:
            api_key = os.environ.get("OPENAI_API_KEY")
            if not api_key:
                provider = OpenAIProvider(
                    model="gpt-4o-mini",
                    transport=httpx.MockTransport(_default_keyless_handler),
                )
            else:
                provider = OpenAIProvider(model="gpt-4o-mini", api_key=api_key)
        self.provider = provider

    async def invoke(
        self,
        context: ExecutionContext,
        inputs: dict[str, Any],
        config: dict[str, Any] | None = None,
    ) -> ModuleResult:
        operation = inputs["operation"]
        model = inputs.get("model") or self.provider.model
        base_url = inputs.get("base_url") or self.provider.base_url
        api_key = inputs.get("api_key") or self.provider._api_key

        active_provider = self.provider
        if (
            model != self.provider.model
            or base_url != self.provider.base_url
            or api_key != self.provider._api_key
        ):
            active_provider = OpenAIProvider(
                model=model,
                base_url=base_url,
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
            texts = inputs.get("texts", ["default text"])
            emb = await active_provider.embed(texts, context=context)
            return ModuleResult.success(
                outputs={
                    "embeddings": emb.embeddings,
                    "model": emb.model,
                },
                usage=emb.usage,
            )

        return ModuleResult.failure(
            error_code="INVALID_OPERATION",
            error_message=f"Unsupported operation '{operation}'",
        )
