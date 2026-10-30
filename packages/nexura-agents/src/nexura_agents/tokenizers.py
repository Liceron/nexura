# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Token estimation protocols and language-safe offline estimators."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class TokenEstimator(Protocol):
    """Protocol for offline or model-specific token counting."""

    def estimate_tokens(self, text: str) -> int:
        """Estimate token count for the given text string."""
        ...


class HeuristicTokenEstimator:
    """Deterministic, language-safe offline token estimator.

    Accurately estimates tokens across mixed Latin, CJK, Indic, Arabic,
    punctuation, and emoji sequences without requiring network calls or
    heavy external tokenizer libraries.
    Guarantees non-zero token counts for any non-empty input.
    """

    def estimate_tokens(self, text: str) -> int:
        if not text:
            return 0

        tokens = 0
        current_latin_len = 0

        for ch in text:
            code = ord(ch)

            # CJK Ideographs and Extensions
            is_cjk = (
                (0x4E00 <= code <= 0x9FFF)  # CJK Unified Ideographs
                or (0x3400 <= code <= 0x4DBF)  # CJK Extension A
                or (0x20000 <= code <= 0x2A6DF)  # CJK Extension B
                or (0x3040 <= code <= 0x309F)  # Hiragana
                or (0x30A0 <= code <= 0x30FF)  # Katakana
                or (0xAC00 <= code <= 0xD7AF)  # Hangul Syllables
            )

            # Indic scripts (Devanagari, Bengali, Gurmukhi, Gujarati,
            # Oriya, Tamil, Telugu, Kannada, Malayalam)
            is_indic = 0x0900 <= code <= 0x0DFF

            # Arabic / Persian / Urdu
            is_arabic = 0x0600 <= code <= 0x06FF

            # Emojis & Pictographs
            is_emoji = (
                (0x1F300 <= code <= 0x1F9FF)
                or (0x1F600 <= code <= 0x1F64F)
                or (0x2600 <= code <= 0x27BF)
            )

            if is_cjk:
                if current_latin_len > 0:
                    tokens += max(1, (current_latin_len + 3) // 4)
                    current_latin_len = 0
                tokens += 2  # CJK chars average ~1.5 - 2 tokens in standard BPE vocabularies
            elif is_indic or is_arabic:
                if current_latin_len > 0:
                    tokens += max(1, (current_latin_len + 3) // 4)
                    current_latin_len = 0
                tokens += 2  # Multibyte non-Latin scripts average ~2 tokens per character
            elif is_emoji:
                if current_latin_len > 0:
                    tokens += max(1, (current_latin_len + 3) // 4)
                    current_latin_len = 0
                tokens += 3  # Emojis usually occupy 2-4 tokens
            elif ch.isspace():
                if current_latin_len > 0:
                    tokens += max(1, (current_latin_len + 3) // 4)
                    current_latin_len = 0
            elif ch in ".,!?;:\"'()[]{}/\\-_@#%&*+=<>|`~^$":
                if current_latin_len > 0:
                    tokens += max(1, (current_latin_len + 3) // 4)
                    current_latin_len = 0
                tokens += 1
            else:
                current_latin_len += 1

        if current_latin_len > 0:
            tokens += max(1, (current_latin_len + 3) // 4)

        return max(1, tokens)


class CharacterTokenEstimator:
    """Fast character-ratio token estimator."""

    def __init__(self, chars_per_token: float = 4.0) -> None:
        self.chars_per_token = max(1.0, chars_per_token)

    def estimate_tokens(self, text: str) -> int:
        if not text:
            return 0
        return max(1, int(len(text) / self.chars_per_token + 0.9))


class WordTokenEstimator:
    """Whitespace word-ratio token estimator."""

    def __init__(self, tokens_per_word: float = 1.33) -> None:
        self.tokens_per_word = max(1.0, tokens_per_word)

    def estimate_tokens(self, text: str) -> int:
        words = text.split()
        if not words:
            return 0
        return max(1, int(len(words) * self.tokens_per_word + 0.5))


default_token_estimator: TokenEstimator = HeuristicTokenEstimator()
