# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Speech service orchestrating STT, TTS, artifact persistence, and consent."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import os
import re
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
from nexura_agents.contracts.context import ExecutionContext
from nexura_agents.contracts.result import ArtifactReference
from nexura_agents.errors import (
    ProviderAuthError,
    ProviderRateLimitError,
    ProviderRequestError,
    ProviderResponseError,
)
from nexura_agents.providers.errors import StreamCancelledError
from nexura_agents.types import Usage

from nexura_runtime.integrations.speech.errors import (
    AudioConsentError,
    AudioValidationError,
    SpeechProviderError,
)
from nexura_runtime.integrations.speech.types import (
    SUPPORTED_SAMPLE_RATES,
    SpeechTimestamp,
    SynthesisRequest,
    SynthesisResult,
    TranscriptionRequest,
    TranscriptionResult,
    validate_audio_bytes,
)

_KEY_PATTERN = re.compile(r"(sarvam-[a-zA-Z0-9_-]{16,}|bearer\s+[a-zA-Z0-9_-]{16,})", re.I)


def _redact_speech_secrets(text: str) -> str:
    """Mask credential tokens in speech integration error strings."""
    return _KEY_PATTERN.sub("[REDACTED_KEY]", text)


class SpeechService:
    """Speech-to-text and text-to-speech service with artifact storage integration.

    Guarantees:
    - Pre-flight validation of audio headers, MIME types, and size limits.
    - Explicit consent and retention enforcement.
    - Preserves Unicode transcripts (Indic and Latin scripts) and timestamps.
    - Stores synthesized audio as ArtifactReference outside execution checkpoints.
    - Supports bounded streaming synthesis with cancellation cleanup.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str = "https://api.sarvam.ai",
        timeout: float = 30.0,
        max_audio_size_bytes: int = 10_000_000,
        client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        artifact_store: Any | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_audio_size_bytes = max_audio_size_bytes
        self.artifact_store = artifact_store
        self.extra_headers = extra_headers or {}

        resolved_key = api_key or os.environ.get("SARVAM_API_KEY")
        self._api_key = resolved_key

        self._owns_client = client is None
        self._transport = transport
        if client is not None:
            self._client = client
        else:
            self._client = httpx.AsyncClient(
                transport=transport,
                timeout=httpx.Timeout(timeout),
            )

    @property
    def transport(self) -> httpx.AsyncBaseTransport | None:
        """Return underlying HTTP transport."""
        return self._transport

    def __repr__(self) -> str:
        masked_key = "***" if self._api_key else None
        return f"SpeechService(base_url={self.base_url!r}, api_key={masked_key!r})"

    def _build_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self._api_key:
            headers["api-subscription-key"] = self._api_key
        headers.update(self.extra_headers)
        return headers

    def _handle_http_error(self, exc: httpx.HTTPStatusError) -> None:
        """Translate HTTP errors into typed exceptions with secret masking."""
        status = exc.response.status_code
        safe_body = _redact_speech_secrets(exc.response.text)

        if status in (401, 403):
            raise ProviderAuthError(
                f"Speech service authentication failed (HTTP {status}): {safe_body}",
                original_error=exc,
            ) from exc

        if status == 429:
            retry_after_str = exc.response.headers.get("retry-after")
            retry_after: float | None = None
            if retry_after_str:
                with contextlib.suppress(ValueError):
                    retry_after = float(retry_after_str)
            detail = f" (Retry-After: {retry_after}s)" if retry_after else ""
            raise ProviderRateLimitError(
                f"Speech service rate limit exceeded (HTTP 429){detail}: {safe_body}",
                original_error=exc,
            ) from exc

        if status == 400:
            raise ProviderRequestError(
                f"Speech service rejected request (HTTP 400): {safe_body}",
                original_error=exc,
            ) from exc

        if status >= 500:
            raise ProviderResponseError(
                f"Speech service server error (HTTP {status}): {safe_body}",
                original_error=exc,
            ) from exc

        raise SpeechProviderError(
            f"Speech service upstream error (HTTP {status}): {safe_body}",
            original_error=exc,
        ) from exc

    async def transcribe(
        self,
        request: TranscriptionRequest,
        *,
        context: ExecutionContext | None = None,
    ) -> TranscriptionResult:
        """Transcribe audio into text with timestamps and language detection."""
        if context is not None and context.is_cancelled:
            raise StreamCancelledError("Transcription cancelled.", stream_id=context.run_id)

        # 1. Enforce user consent
        if not request.user_consent:
            raise AudioConsentError(
                "Audio transcription rejected: user consent is required for audio "
                "ingestion and processing."
            )

        # 2. Resolve audio bytes from direct payload or artifact store
        audio_data = request.audio_data
        if audio_data is None and request.artifact_id:
            store: Any = (
                context.artifact_store
                if (context and context.artifact_store)
                else self.artifact_store
            )
            if store is not None:
                if hasattr(store, "for_workspace"):
                    ws_id = getattr(context, "workspace_id", "default") if context else "default"
                    store = store.for_workspace(ws_id)
                audio_data = await store.get(request.artifact_id)
                if audio_data is None:
                    raise AudioValidationError(
                        f"Artifact '{request.artifact_id}' not found in artifact store."
                    )
            else:
                raise AudioValidationError(
                    f"Cannot resolve artifact '{request.artifact_id}': No artifact "
                    "store present in execution context or service."
                )

        if audio_data is None:
            raise AudioValidationError(
                "Transcription request must provide either 'audio_data' or a valid 'artifact_id'."
            )

        # 3. Validate audio bytes
        validate_audio_bytes(
            audio_data,
            max_size_bytes=self.max_audio_size_bytes,
            mime_type=request.mime_type,
        )

        url = f"{self.base_url}/speech-to-text"
        headers = self._build_headers()

        files = {
            "file": ("audio_input.wav", audio_data, request.mime_type),
        }
        form_data = {
            "model": request.model,
            "language_code": request.language_code,
            "with_timestamps": "true" if request.with_timestamps else "false",
        }

        try:
            response = await self._client.post(
                url,
                files=files,
                data=form_data,
                headers=headers,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            self._handle_http_error(exc)
        except httpx.RequestError as exc:
            safe_err = _redact_speech_secrets(str(exc))
            raise SpeechProviderError(
                f"Network request to speech-to-text endpoint failed: {safe_err}",
                original_error=exc,
            ) from exc

        try:
            data = response.json()
        except Exception as exc:
            raise SpeechProviderError(
                f"Failed to parse speech-to-text JSON response: {exc}",
                original_error=exc,
            ) from exc

        transcript = data.get("transcript", "")
        detected_lang = data.get("language_code", request.language_code)
        raw_timestamps = data.get("timestamps", [])

        timestamps: list[SpeechTimestamp] = []
        if isinstance(raw_timestamps, dict):
            words = raw_timestamps.get("words", [])
            starts = raw_timestamps.get("start_time_seconds", raw_timestamps.get("start", []))
            ends = raw_timestamps.get("end_time_seconds", raw_timestamps.get("end", []))
            word_list: list[Any] = words if isinstance(words, list) else []
            start_list: list[Any] = starts if isinstance(starts, list) else []
            end_list: list[Any] = ends if isinstance(ends, list) else []
            for i, w in enumerate(word_list):
                s = (
                    float(start_list[i])
                    if i < len(start_list) and start_list[i] is not None
                    else 0.0
                )
                e = float(end_list[i]) if i < len(end_list) and end_list[i] is not None else 0.0
                timestamps.append(SpeechTimestamp(word=str(w), start_seconds=s, end_seconds=e))
        elif isinstance(raw_timestamps, list):
            for ts in raw_timestamps:
                if isinstance(ts, dict):
                    raw_s = ts.get("start", ts.get("start_seconds", 0.0))
                    raw_e = ts.get("end", ts.get("end_seconds", 0.0))
                    s_val = float(raw_s) if raw_s is not None else 0.0
                    e_val = float(raw_e) if raw_e is not None else 0.0
                    timestamps.append(
                        SpeechTimestamp(
                            word=str(ts.get("word", "")),
                            start_seconds=s_val,
                            end_seconds=e_val,
                        )
                    )

        usage = None
        if "usage" in data and isinstance(data["usage"], dict):
            u_dict = data["usage"]
            usage = Usage(
                prompt_tokens=u_dict.get("prompt_tokens"),
                completion_tokens=u_dict.get("completion_tokens"),
                total_tokens=u_dict.get("total_tokens"),
                is_complete=True,
            )

        duration = data.get("duration_seconds")
        if duration is not None:
            with contextlib.suppress(ValueError, TypeError):
                duration = float(duration)
        elif timestamps:
            duration = round(max(ts.end_seconds for ts in timestamps), 3)

        return TranscriptionResult(
            transcript=transcript,
            language_code=detected_lang,
            timestamps=timestamps,
            duration_seconds=duration,
            model=request.model,
            usage=usage,
            provider_metadata=data,
        )

    async def synthesize(
        self,
        request: SynthesisRequest,
        *,
        context: ExecutionContext | None = None,
    ) -> SynthesisResult:
        """Synthesize text into speech audio and optionally store as an Artifact."""
        if context is not None and context.is_cancelled:
            raise StreamCancelledError("Speech synthesis cancelled.", stream_id=context.run_id)

        # 1. Enforce user consent
        if not request.user_consent:
            raise AudioConsentError(
                "Speech synthesis rejected: user consent is required for voice reproduction."
            )

        # 2. Validate parameters
        clean_text = request.text.strip()
        if not clean_text:
            raise AudioValidationError("Text for speech synthesis cannot be empty.")

        if request.speech_sample_rate not in SUPPORTED_SAMPLE_RATES:
            raise AudioValidationError(
                f"Unsupported sample rate {request.speech_sample_rate}. Supported: "
                f"{sorted(SUPPORTED_SAMPLE_RATES)}"
            )

        url = f"{self.base_url}/text-to-speech"
        headers = self._build_headers()
        headers["Content-Type"] = "application/json"

        payload: dict[str, Any] = {
            "inputs": [clean_text],
            "target_language_code": request.target_language_code,
            "speaker": request.speaker,
            "pitch": request.pitch,
            "pace": request.pace,
            "loudness": request.loudness,
            "speech_sample_rate": request.speech_sample_rate,
            "model": request.model,
            "enable_preprocessing": True,
        }

        try:
            response = await self._client.post(url, json=payload, headers=headers)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            self._handle_http_error(exc)
        except httpx.RequestError as exc:
            safe_err = _redact_speech_secrets(str(exc))
            raise SpeechProviderError(
                f"Network request to text-to-speech endpoint failed: {safe_err}",
                original_error=exc,
            ) from exc

        try:
            data = response.json()
        except Exception as exc:
            raise SpeechProviderError(
                f"Failed to parse text-to-speech JSON response: {exc}",
                original_error=exc,
            ) from exc

        audios = data.get("audios", [])
        if not audios:
            raise SpeechProviderError("Provider returned empty audios array.")

        b64_audio = audios[0]
        try:
            audio_bytes = base64.b64decode(b64_audio)
        except Exception as exc:
            raise AudioValidationError(
                f"Failed to base64-decode synthesized audio payload: {exc}",
                original_error=exc,
            ) from exc

        validate_audio_bytes(
            audio_bytes,
            max_size_bytes=self.max_audio_size_bytes,
            mime_type=request.output_format,
        )

        # 3. Store as artifact if requested and store available
        art_ref: ArtifactReference | None = None
        store: Any = (
            context.artifact_store if (context and context.artifact_store) else self.artifact_store
        )
        if request.store_as_artifact and store is not None:
            if hasattr(store, "for_workspace"):
                ws_id = getattr(context, "workspace_id", "default") if context else "default"
                store = store.for_workspace(ws_id)
            art_id = request.artifact_id or f"speech_tts_{uuid.uuid4().hex[:10]}"
            art_metadata = {
                "model": request.model,
                "language": request.target_language_code,
                "speaker": request.speaker,
                "sample_rate": request.speech_sample_rate,
                "text_length": len(clean_text),
            }
            try:
                art_ref = await store.put(
                    art_id,
                    audio_bytes,
                    mime_type=request.output_format,
                    metadata=art_metadata,
                )
            except TypeError:
                ws_id = getattr(context, "workspace_id", "default") if context else "default"
                art_ref = await store.put(
                    ws_id,
                    art_id,
                    audio_bytes,
                    mime_type=request.output_format,
                    metadata=art_metadata,
                )

        usage = None
        if "usage" in data and isinstance(data["usage"], dict):
            u_dict = data["usage"]
            usage = Usage(
                prompt_tokens=u_dict.get("prompt_tokens"),
                completion_tokens=u_dict.get("completion_tokens"),
                total_tokens=u_dict.get("total_tokens"),
                is_complete=True,
            )

        duration = data.get("duration_seconds")
        if duration is not None:
            with contextlib.suppress(ValueError, TypeError):
                duration = float(duration)

        return SynthesisResult(
            audio_bytes=audio_bytes,
            sample_rate=request.speech_sample_rate,
            mime_type=request.output_format,
            duration_seconds=duration,
            artifact_ref=art_ref,
            model=request.model,
            usage=usage,
            provider_metadata=data,
        )

    async def stream_synthesize(
        self,
        request: SynthesisRequest,
        *,
        chunk_size_bytes: int = 4096,
        chunk_size: int | None = None,
        context: ExecutionContext | None = None,
    ) -> AsyncIterator[bytes]:
        """Synthesize text and yield bounded chunks with cancellation enforcement."""
        effective_chunk_size = chunk_size or chunk_size_bytes
        result = await self.synthesize(request, context=context)
        audio = result.audio_bytes

        for idx in range(0, len(audio), effective_chunk_size):
            if context is not None and context.is_cancelled:
                raise StreamCancelledError(
                    "Streaming synthesis cancelled.",
                    stream_id=context.run_id,
                )
            yield audio[idx : idx + effective_chunk_size]

    synthesize_stream = stream_synthesize

    async def aclose(self) -> None:
        """Close owned HTTP client."""
        if self._owns_client and hasattr(self, "_client"):
            await self._client.aclose()

    def close(self) -> None:
        """Close client synchronously if no event loop is active."""
        if self._owns_client and hasattr(self, "_client"):
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self.aclose())
            except RuntimeError:
                asyncio.run(self.aclose())
