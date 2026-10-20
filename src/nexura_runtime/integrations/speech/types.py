# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Types, schemas, and validators for speech input/output integration."""

from __future__ import annotations

import io
import wave
from dataclasses import dataclass, field
from typing import Any

from nexura_agents.contracts.result import ArtifactReference
from nexura_agents.types import Usage

from nexura_runtime.integrations.speech.errors import AudioValidationError

SUPPORTED_AUDIO_MIMES: frozenset[str] = frozenset(
    {
        "audio/wav",
        "audio/x-wav",
        "audio/mpeg",
        "audio/mp3",
        "audio/ogg",
        "audio/webm",
        "audio/flac",
    }
)

SUPPORTED_SAMPLE_RATES: frozenset[int] = frozenset(
    {
        8000,
        16000,
        22050,
        24000,
        44100,
        48000,
    }
)


def validate_audio_bytes(
    data: bytes,
    *,
    max_size_bytes: int = 10_000_000,
    mime_type: str = "audio/wav",
) -> dict[str, Any]:
    """Validate audio bytes before network dispatch.

    Rejects empty payloads, oversized buffers, unsupported MIME types,
    and corrupted headers eagerly.
    """
    if not data:
        raise AudioValidationError("Audio data payload is empty.")

    if len(data) > max_size_bytes:
        raise AudioValidationError(
            f"Audio payload size ({len(data)} bytes) exceeds maximum limit "
            f"of {max_size_bytes} bytes."
        )

    clean_mime = mime_type.lower().split(";")[0].strip()
    if clean_mime not in SUPPORTED_AUDIO_MIMES:
        raise AudioValidationError(
            f"Unsupported audio MIME type '{mime_type}'. Supported: {sorted(SUPPORTED_AUDIO_MIMES)}"
        )

    metadata: dict[str, Any] = {
        "mime_type": clean_mime,
        "size_bytes": len(data),
    }

    # Magic byte header validation and sample rate extraction for WAV
    if clean_mime in ("audio/wav", "audio/x-wav"):
        if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
            raise AudioValidationError("Malformed audio payload: Expected valid RIFF/WAVE header.")
        channels = int.from_bytes(data[22:24], "little")
        sample_rate = int.from_bytes(data[24:28], "little")
        if sample_rate not in SUPPORTED_SAMPLE_RATES:
            raise AudioValidationError(
                f"Unsupported sample rate: {sample_rate} Hz. Supported: "
                f"{sorted(SUPPORTED_SAMPLE_RATES)}"
            )
        metadata["sample_rate"] = sample_rate
        metadata["channels"] = channels

    return metadata


def create_synthetic_wav(
    sample_rate: int = 16000,
    duration_seconds: float = 0.05,
) -> bytes:
    """Generate a minimal valid 16-bit mono RIFF/WAVE in pure Python."""
    num_frames = int(sample_rate * duration_seconds)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        # Write silence frames (16-bit zero PCM)
        wf.writeframes(b"\x00\x00" * num_frames)
    return buf.getvalue()


@dataclass(frozen=True)
class SpeechTimestamp:
    """Timestamp segment for transcribed words or tokens."""

    word: str
    start_seconds: float
    end_seconds: float


@dataclass(frozen=True)
class TranscriptionRequest:
    """Input parameters for speech-to-text transcription."""

    audio_data: bytes | None = None
    artifact_id: str | None = None
    mime_type: str = "audio/wav"
    language_code: str = "hi-IN"
    model: str = "saarika:v1"
    with_timestamps: bool = True
    user_consent: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TranscriptionResult:
    """Result of speech transcription."""

    transcript: str
    language_code: str
    timestamps: list[SpeechTimestamp] = field(default_factory=list)
    duration_seconds: float | None = None
    model: str = "saarika:v1"
    usage: Usage | None = None
    provider_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SynthesisRequest:
    """Input parameters for text-to-speech synthesis."""

    text: str
    target_language_code: str = "hi-IN"
    speaker: str = "meera"
    pitch: float = 0.0
    pace: float = 1.0
    loudness: float = 1.0
    speech_sample_rate: int = 16000
    model: str = "bulbul:v1"
    output_format: str = "audio/wav"
    user_consent: bool = True
    store_as_artifact: bool = True
    artifact_id: str | None = None


@dataclass(frozen=True)
class SynthesisResult:
    """Result of speech synthesis."""

    audio_bytes: bytes
    sample_rate: int
    mime_type: str
    duration_seconds: float | None = None
    artifact_ref: ArtifactReference | None = None
    model: str = "bulbul:v1"
    usage: Usage | None = None
    provider_metadata: dict[str, Any] = field(default_factory=dict)
