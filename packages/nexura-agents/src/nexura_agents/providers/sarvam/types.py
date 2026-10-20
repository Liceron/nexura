# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Types, schemas, and identifier preservation for Sarvam text and translation API."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from nexura_agents.providers.errors import CapabilityError
from nexura_agents.types import Usage

# Supported Indic language codes in Sarvam API
SARVAM_SUPPORTED_LANGUAGES: frozenset[str] = frozenset(
    {
        "hi-IN",  # Hindi
        "bn-IN",  # Bengali
        "kn-IN",  # Kannada
        "ml-IN",  # Malayalam
        "mr-IN",  # Marathi
        "od-IN",  # Odia
        "pa-IN",  # Punjabi
        "ta-IN",  # Tamil
        "te-IN",  # Telugu
        "gu-IN",  # Gujarati
        "en-IN",  # Indian English
    }
)

_SHORT_CODE_MAP: dict[str, str] = {
    "hi": "hi-IN",
    "bn": "bn-IN",
    "kn": "kn-IN",
    "ml": "ml-IN",
    "mr": "mr-IN",
    "od": "od-IN",
    "or": "od-IN",
    "pa": "pa-IN",
    "ta": "ta-IN",
    "te": "te-IN",
    "gu": "gu-IN",
    "en": "en-IN",
}

_IDENTIFIER_PATTERN = re.compile(
    r"(\{\{[^}]+\}\}|\{[^}]+\}|<[^>]+>|#[A-Za-z0-9_-]+|₹\s*[\d,]+(?:\.\d+)?|https?://[^\s<>]+)",
    re.UNICODE,
)


def normalize_language_code(code: str) -> str:
    """Normalize language code to Sarvam canonical format (e.g. 'hi' -> 'hi-IN')."""
    clean = code.strip()
    if clean in SARVAM_SUPPORTED_LANGUAGES:
        return clean
    normalized = _SHORT_CODE_MAP.get(clean.lower())
    if normalized and normalized in SARVAM_SUPPORTED_LANGUAGES:
        return normalized
    raise CapabilityError(
        f"Unsupported language code '{code}'. Supported Indic language codes: "
        f"{sorted(SARVAM_SUPPORTED_LANGUAGES)}",
        provider_name="sarvam",
        capability="language",
    )


@dataclass(frozen=True)
class TranslationRequest:
    """Input payload for Sarvam translation."""

    input_text: str
    source_language_code: str
    target_language_code: str
    speaker_gender: str | None = None
    mode: str = "formal"
    model: str = "mayura:v1"
    enable_preprocessing: bool = True
    preserve_identifiers: bool = True


@dataclass(frozen=True)
class TranslationResult:
    """Result of Sarvam text translation."""

    translated_text: str
    source_language_code: str
    target_language_code: str
    model: str
    characters_translated: int
    usage: Usage | None = None
    provider_metadata: dict[str, Any] | None = None


class IdentifierPreserver:
    """Masks and restores identifiers (templates, IDs, amounts, URLs) across translation."""

    @staticmethod
    def mask(text: str) -> tuple[str, dict[str, str]]:
        """Replace identifiers with opaque placeholders."""
        matches = _IDENTIFIER_PATTERN.findall(text)
        if not matches:
            return text, {}

        replacements: dict[str, str] = {}
        masked_text = text

        for idx, match_str in enumerate(matches):
            token = f"__SARVAM_ID_{idx}__"
            replacements[token] = match_str
            masked_text = masked_text.replace(match_str, token, 1)

        return masked_text, replacements

    @staticmethod
    def unmask(translated_text: str, replacements: dict[str, str]) -> str:
        """Restore original identifiers from placeholders."""
        result = translated_text
        for token, original in replacements.items():
            # Match token even if spaces were inserted by translation engine
            loose_token_pattern = re.compile(
                r"__\s*SARVAM\s*_?\s*ID\s*_?\s*" + re.escape(token.split("_")[-2]) + r"\s*__",
                re.IGNORECASE,
            )
            if loose_token_pattern.search(result):
                result = loose_token_pattern.sub(original, result, count=1)
            else:
                result = result.replace(token, original)
        return result
