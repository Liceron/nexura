# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Module wrapper exposing provider-neutral generation and streaming via B00/B01 contracts."""

from __future__ import annotations

from typing import Any

from nexura_agents.contracts.base import BaseModule
from nexura_agents.contracts.context import ExecutionContext
from nexura_agents.contracts.manifest import ModuleManifest
from nexura_agents.contracts.result import ModuleResult
from nexura_agents.providers.fake import StreamingFakeProvider
from nexura_agents.providers.types import (
    GenerationRequest,
    StreamDelta,
    ToolCallAssembler,
)
from nexura_agents.types import Message


class ModelProviderModule(BaseModule):
    """Module wrapper for provider-neutral model generation, streaming and tool assembly."""

    manifest = ModuleManifest(
        schema_version=1,
        module_id="nexura.models.provider",
        version="1.0.0",
        kind="model_adapter",
        title="Model Protocol and Streaming Adapter",
        description=("Provider-neutral LLM generation, tool call assembly, and token streaming."),
        input_schema={
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["generate", "stream", "embed", "assemble_tool_calls"],
                },
                "messages": {"type": "array"},
                "tools": {"type": "array"},
                "texts": {"type": "array"},
                "deltas": {"type": "array"},
                "model_id": {"type": "string"},
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
                "chunks_count": {"type": "integer"},
            },
        },
        config_schema={"type": "object"},
        named_ports=[],
        capabilities={"streaming": True, "network": False},
        effect_policy="pure",
        dependency_extra=None,
        min_sdk_version="0.3.0",
        timeout_seconds=30.0,
        output_limit_bytes=2_000_000,
        required_credential_types=[],
        supported_modalities=["text"],
        supported_locales=["en", "hi", "ta"],
        source_reference="packages/nexura-agents/src/nexura_agents/providers/",
        verification_status="mock_verified",
    )

    def __init__(self, provider: StreamingFakeProvider | None = None) -> None:
        self.provider = provider or StreamingFakeProvider()

    async def invoke(
        self,
        context: ExecutionContext,
        inputs: dict[str, Any],
        config: dict[str, Any] | None = None,
    ) -> ModuleResult:
        operation = inputs["operation"]

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
                messages = [Message(role="user", content="Hello")]

            req = GenerationRequest(
                messages=messages,
                tools=inputs.get("tools"),
            )
            response = await self.provider.generate(req, context=context)
            tool_calls_dict = (
                [
                    {
                        "id": tc.id,
                        "name": tc.name,
                        "arguments": tc.arguments,
                    }
                    for tc in response.message.tool_calls
                ]
                if response.message.tool_calls
                else []
            )
            usage_dict = {
                "prompt_tokens": response.usage.prompt_tokens if response.usage else None,
                "completion_tokens": response.usage.completion_tokens if response.usage else None,
                "total_tokens": response.usage.total_tokens if response.usage else None,
                "is_complete": response.usage.is_complete if response.usage else False,
            }
            return ModuleResult.success(
                outputs={
                    "content": response.message.content or "",
                    "tool_calls": tool_calls_dict,
                    "usage": usage_dict,
                },
                usage=response.usage,
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
                messages = [Message(role="user", content="Stream query")]

            req = GenerationRequest(messages=messages, tools=inputs.get("tools"))
            chunks: list[str] = []
            final_usage = None

            async for delta in self.provider.stream(req, context=context):
                if delta.text:
                    chunks.append(delta.text)
                if delta.usage:
                    final_usage = delta.usage

            combined_text = "".join(chunks)
            return ModuleResult.success(
                outputs={
                    "content": combined_text,
                    "chunks_count": len(chunks),
                },
                usage=final_usage,
            )

        if operation == "embed":
            texts = inputs.get("texts", ["default text"])
            emb_res = await self.provider.embed(texts, context=context)
            return ModuleResult.success(
                outputs={
                    "embeddings": emb_res.embeddings,
                    "model": emb_res.model,
                },
                usage=emb_res.usage,
            )

        if operation == "assemble_tool_calls":
            raw_deltas = inputs.get("deltas", [])
            assembler = ToolCallAssembler()
            for d in raw_deltas:
                assembler.feed(
                    StreamDelta(
                        kind="tool_call_delta",
                        tool_call_index=d.get("index"),
                        tool_call_id=d.get("id"),
                        tool_name=d.get("name"),
                        tool_arguments_delta=d.get("arguments_delta"),
                    )
                )
            assembled = assembler.validate_and_assemble()
            return ModuleResult.success(
                outputs={
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "name": tc.name,
                            "arguments": tc.arguments,
                        }
                        for tc in assembled
                    ]
                }
            )

        return ModuleResult.failure(
            error_code="INVALID_OPERATION",
            error_message=f"Unsupported operation: {operation}",
        )
