# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Speech input and output integration package."""

from __future__ import annotations

from nexura_runtime.integrations.speech.errors import (
    AudioConsentError,
    AudioValidationError,
    SpeechError,
    SpeechProviderError,
)
from nexura_runtime.integrations.speech.module import SpeechModule
from nexura_runtime.integrations.speech.service import SpeechService
from nexura_runtime.integrations.speech.types import (
    SUPPORTED_AUDIO_MIMES,
    SUPPORTED_SAMPLE_RATES,
    SpeechTimestamp,
    SynthesisRequest,
    SynthesisResult,
    TranscriptionRequest,
    TranscriptionResult,
    create_synthetic_wav,
    validate_audio_bytes,
)
from nexura_runtime.modules.registry import default_registry

# Auto-register speech module
default_registry.register(SpeechModule())

__all__ = [
    "SUPPORTED_AUDIO_MIMES",
    "SUPPORTED_SAMPLE_RATES",
    "AudioConsentError",
    "AudioValidationError",
    "SpeechError",
    "SpeechModule",
    "SpeechProviderError",
    "SpeechService",
    "SpeechTimestamp",
    "SynthesisRequest",
    "SynthesisResult",
    "TranscriptionRequest",
    "TranscriptionResult",
    "create_synthetic_wav",
    "validate_audio_bytes",
]
