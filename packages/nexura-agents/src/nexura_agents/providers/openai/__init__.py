# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""OpenAI-compatible and local model provider package."""

from __future__ import annotations

from nexura_agents.providers.openai.module import OpenAIProviderModule
from nexura_agents.providers.openai.provider import OpenAIProvider

__all__ = [
    "OpenAIProvider",
    "OpenAIProviderModule",
]
