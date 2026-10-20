# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Module wrapper exposing Sarvam chat and translation via B00/B01 contracts."""

from __future__ import annotations

import os
from typing import Any

import httpx

from nexura_agents.contracts.base import BaseModule
from nexura_agents.contracts.context import ExecutionContext
from nexura_agents.contracts.manifest import ModuleManifest
from nexura_agents.contracts.result import ModuleResult
from nexura_agents.providers.sarvam.provider import SarvamProvider
from nexura_agents.providers.sarvam.types import TranslationRequest
from nexura_agents.providers.types import GenerationRequest
from nexura_agents.types import Message


def _default_sarvam_mock_handler(request: httpx.Request) -> httpx.Response:
    """Offline keyless mock handler for default Sarvam module execution."""
    path = request.url.path
    if "/translate" in path:
        return httpx.Response(
            200,
            json={
                "translated_text": "नमस्ते, मैं आपकी क्या सहायता कर सकता हूँ?",
                "source_language_code": "en-IN",
                "target_language_code": "hi-IN",
                "usage": {
                    "prompt_tokens": 8,
                    "completion_tokens": 12,
                    "total_tokens": 20,
                },
            },
        )
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-sarvam-mock-001",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "नमस्कार! Sarvam AI में आपका स्वागत है।",
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 15,
                "total_tokens": 25,
            },
        },
    )


class SarvamProviderModule(BaseModule):
    """Module wrapper for Sarvam Indic language chat and translation services."""

    manifest = ModuleManifest(
        schema_version=1,
        module_id="nexura.models.sarvam",
        version="1.0.0",
        kind="model_adapter",
        title="Sarvam Indic Text and Translation Adapter",
        description=(
            "Sarvam AI adapter for Indic language chat generation and high-fidelity "
            "translation with identifier preservation."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["chat", "generate", "translate"],
                },
                "model": {"type": "string"},
                "api_key": {"type": "string"},
                "messages": {"type": "array"},
                "prompt": {"type": "string"},
                "input_text": {"type": "string"},
                "source_language_code": {"type": "string"},
                "target_language_code": {"type": "string"},
                "preserve_identifiers": {"type": "boolean"},
            },
            "required": ["operation"],
        },
        output_schema={
            "type": "object",
            "properties": {
                "content": {"type": "string"},
                "translated_text": {"type": "string"},
                "source_language": {"type": "string"},
                "target_language": {"type": "string"},
                "characters_translated": {"type": "integer"},
                "usage": {"type": "object"},
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
        supported_locales=["en", "hi", "bn", "kn", "ml", "mr", "od", "pa", "ta", "te", "gu"],
        source_reference="packages/nexura-agents/src/nexura_agents/providers/sarvam/",
        verification_status="mock_verified",
    )

    def __init__(self, provider: SarvamProvider | None = None) -> None:
        if provider is None:
            api_key = os.environ.get("SARVAM_API_KEY")
            if not api_key:
                provider = SarvamProvider(
                    model="sarvam-2b",
                    transport=httpx.MockTransport(_default_sarvam_mock_handler),
                )
            else:
                provider = SarvamProvider(
                    model="sarvam-2b",
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
            active_provider = SarvamProvider(
                model=model,
                api_key=api_key,
                transport=self.provider.transport,
            )

        if operation in ("chat", "generate"):
            raw_msgs = inputs.get("messages", [])
            messages = [
                Message(
                    role=m.get("role", "user"),
                    content=m.get("content"),
                )
                for m in raw_msgs
            ]
            if not messages:
                prompt_val = inputs.get("prompt", "नमस्ते")
                messages = [Message(role="user", content=prompt_val)]

            req = GenerationRequest(messages=messages)
            resp = await active_provider.generate(req, context=context)
            return ModuleResult.success(
                outputs={"content": resp.message.content or ""},
                usage=resp.usage,
            )

        if operation == "translate":
            in_text = inputs.get("input_text") or inputs.get("input") or ""
            src_lang = inputs.get("source_language_code") or inputs.get("source") or "en-IN"
            tgt_lang = inputs.get("target_language_code") or inputs.get("target") or "hi-IN"
            preserve_ids = inputs.get("preserve_identifiers", True)

            req_t = TranslationRequest(
                input_text=in_text,
                source_language_code=src_lang,
                target_language_code=tgt_lang,
                preserve_identifiers=preserve_ids,
            )
            t_res = await active_provider.translate(req_t, context=context)
            return ModuleResult.success(
                outputs={
                    "translated_text": t_res.translated_text,
                    "source_language": t_res.source_language_code,
                    "target_language": t_res.target_language_code,
                    "characters_translated": t_res.characters_translated,
                },
                usage=t_res.usage,
            )

        return ModuleResult.failure(
            error_code="INVALID_OPERATION",
            error_message=f"Unsupported operation '{operation}'",
        )
