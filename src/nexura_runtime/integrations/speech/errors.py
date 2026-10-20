# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Typed errors for speech input/output integration."""

from __future__ import annotations

from typing import Any

from nexura_agents.errors import NexuraError


class SpeechError(NexuraError):
    """Base error for speech-to-text and text-to-speech failures."""

    def __init__(
        self,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        original_error: BaseException | None = None,
    ) -> None:
        super().__init__(message, original_error=original_error)
        self.details = details or {}


class AudioValidationError(SpeechError):
    """Raised when audio payload, MIME type, sample rate, or format is malformed or invalid."""


class AudioConsentError(SpeechError):
    """Raised when user consent is missing or denied for audio processing/retention."""


class SpeechProviderError(SpeechError):
    """Raised when remote or local speech inference service fails."""
