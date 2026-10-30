# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Module wrapper exposing Speech I/O via B00/B01 contracts."""

from __future__ import annotations

import base64
import os
from typing import Any

import httpx
from nexura_agents.contracts.base import BaseModule
from nexura_agents.contracts.context import ExecutionContext
from nexura_agents.contracts.manifest import ModuleManifest
from nexura_agents.contracts.result import ModuleResult

from nexura_runtime.integrations.speech.service import SpeechService
from nexura_runtime.integrations.speech.types import (
    SynthesisRequest,
    TranscriptionRequest,
    create_synthetic_wav,
)


def _default_speech_mock_handler(request: httpx.Request) -> httpx.Response:
    """Offline keyless mock handler for default Speech module execution."""
    path = request.url.path
    if "/speech-to-text" in path:
        return httpx.Response(
            200,
            json={
                "transcript": "नमस्ते, नेक्सुरा में आपका स्वागत है।",
                "language_code": "hi-IN",
                "duration_seconds": 1.8,
                "timestamps": [
                    {"word": "नमस्ते,", "start": 0.0, "end": 0.5},
                    {"word": "नेक्सुरा", "start": 0.55, "end": 1.0},
                    {"word": "में", "start": 1.05, "end": 1.2},
                    {"word": "आपका", "start": 1.25, "end": 1.45},
                    {"word": "स्वागत", "start": 1.5, "end": 1.7},
                    {"word": "है।", "start": 1.72, "end": 1.8},
                ],
                "usage": {"prompt_tokens": 12, "completion_tokens": 10, "total_tokens": 22},
            },
        )

    # text-to-speech mock
    synth_wav = create_synthetic_wav(sample_rate=16000, duration_seconds=0.1)
    b64_audio = base64.b64encode(synth_wav).decode("ascii")
    return httpx.Response(
        200,
        json={
            "audios": [b64_audio],
            "duration_seconds": 0.1,
            "usage": {"prompt_tokens": 8, "completion_tokens": 16, "total_tokens": 24},
        },
    )


class SpeechModule(BaseModule):
    """Module wrapper for speech-to-text and text-to-speech services."""

    manifest = ModuleManifest(
        schema_version=1,
        module_id="nexura.speech",
        version="1.0.0",
        kind="integration",
        title="Speech Input/Output Integration",
        description=(
            "Multilingual speech-to-text transcription and text-to-speech synthesis "
            "with artifact storage and user consent enforcement."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["transcribe", "synthesize", "stream_synthesize"],
                },
                "audio_base64": {"type": "string"},
                "artifact_id": {"type": "string"},
                "mime_type": {"type": "string"},
                "language_code": {"type": "string"},
                "text": {"type": "string"},
                "speaker": {"type": "string"},
                "sample_rate": {"type": "integer"},
                "user_consent": {"type": "boolean"},
                "store_as_artifact": {"type": "boolean"},
            },
            "required": ["operation"],
        },
        output_schema={
            "type": "object",
            "properties": {
                "transcript": {"type": "string"},
                "language_code": {"type": "string"},
                "timestamps": {"type": "array"},
                "audio_base64": {"type": "string"},
                "artifact_id": {"type": "string"},
                "duration_seconds": {"type": "number"},
                "chunks_count": {"type": "integer"},
            },
        },
        config_schema={"type": "object"},
        named_ports=[],
        capabilities={"streaming": True, "network": True},
        effect_policy="read",
        dependency_extra=None,
        min_sdk_version="0.3.0",
        timeout_seconds=60.0,
        output_limit_bytes=10_000_000,
        required_credential_types=["api_key"],
        supported_modalities=["audio", "text"],
        supported_locales=["en", "hi", "bn", "kn", "ml", "mr", "od", "pa", "ta", "te", "gu"],
        source_reference="src/nexura_runtime/integrations/speech/",
        verification_status="mock_verified",
    )

    def __init__(self, service: SpeechService | None = None) -> None:
        if service is None:
            api_key = os.environ.get("SARVAM_API_KEY")
            if not api_key:
                service = SpeechService(
                    transport=httpx.MockTransport(_default_speech_mock_handler),
                )
            else:
                service = SpeechService(api_key=api_key)
        self.service = service

    async def invoke(
        self,
        context: ExecutionContext,
        inputs: dict[str, Any],
        config: dict[str, Any] | None = None,
    ) -> ModuleResult:
        operation = inputs["operation"]
        user_consent = inputs.get("user_consent", True)

        if operation == "transcribe":
            audio_bytes: bytes | None = None
            raw_b64 = inputs.get("audio_base64")
            if raw_b64:
                audio_bytes = base64.b64decode(raw_b64)
            elif not inputs.get("artifact_id"):
                # Default synthetic audio for keyless CLI invocations
                audio_bytes = create_synthetic_wav(sample_rate=16000, duration_seconds=0.05)

            req_t = TranscriptionRequest(
                audio_data=audio_bytes,
                artifact_id=inputs.get("artifact_id"),
                mime_type=inputs.get("mime_type", "audio/wav"),
                language_code=inputs.get("language_code", "hi-IN"),
                user_consent=user_consent,
            )
            res_t = await self.service.transcribe(req_t, context=context)

            timestamps_list = [
                {"word": ts.word, "start": ts.start_seconds, "end": ts.end_seconds}
                for ts in res_t.timestamps
            ]
            return ModuleResult.success(
                outputs={
                    "transcript": res_t.transcript,
                    "language_code": res_t.language_code,
                    "timestamps": timestamps_list,
                    "duration_seconds": res_t.duration_seconds or 0.0,
                },
                usage=res_t.usage,
            )

        if operation == "synthesize":
            text = inputs.get("text", "नमस्ते दुनिया")
            sample_rate = inputs.get("sample_rate", 16000)
            store_art = inputs.get("store_as_artifact", True)

            req_s = SynthesisRequest(
                text=text,
                target_language_code=inputs.get("language_code", "hi-IN"),
                speaker=inputs.get("speaker", "meera"),
                speech_sample_rate=sample_rate,
                user_consent=user_consent,
                store_as_artifact=store_art,
                artifact_id=inputs.get("artifact_id"),
            )
            res_s = await self.service.synthesize(req_s, context=context)

            b64_audio = base64.b64encode(res_s.audio_bytes).decode("ascii")
            art_id = res_s.artifact_ref.artifact_id if res_s.artifact_ref else None

            artifact_refs = [res_s.artifact_ref] if res_s.artifact_ref else []

            return ModuleResult.success(
                outputs={
                    "audio_base64": b64_audio,
                    "artifact_id": art_id,
                    "duration_seconds": res_s.duration_seconds or 0.0,
                },
                artifact_refs=artifact_refs,
                usage=res_s.usage,
            )

        if operation == "stream_synthesize":
            text = inputs.get("text", "नमस्ते दुनिया")
            req_s = SynthesisRequest(
                text=text,
                target_language_code=inputs.get("language_code", "hi-IN"),
                user_consent=user_consent,
            )
            chunks: list[bytes] = []
            async for chunk in self.service.stream_synthesize(req_s, context=context):
                chunks.append(chunk)

            total_bytes = b"".join(chunks)
            return ModuleResult.success(
                outputs={
                    "audio_base64": base64.b64encode(total_bytes).decode("ascii"),
                    "chunks_count": len(chunks),
                }
            )

        return ModuleResult.failure(
            error_code="INVALID_OPERATION",
            error_message=f"Unsupported operation '{operation}'",
        )
