# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Base protocol definition for LLM provider adapters."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from nexura_agents.types import Message, ModelResponse


@runtime_checkable
class ModelProtocol(Protocol):
    """Protocol defining the asynchronous interface for language model adapters.

    All model adapters must implement generate(). Implementations are expected
    to mask sensitive credentials in repr and errors, handle timeouts, and
    distinguish reported versus unknown token usage.

    Optional structured-output opt-in (not part of the protocol, so existing models are
    unaffected): set ``supports_response_schema = True`` on the class and accept a
    ``response_schema`` keyword in ``generate()``. The runner then passes the output JSON
    schema natively instead of putting it in the prompt. Also set
    ``response_schema_with_tools = True`` if the model accepts a schema on turns where tools
    are offered; otherwise the schema stays in the prompt for agents that have tools.
    """

    async def generate(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        *,
        timeout: float | None = None,
    ) -> ModelResponse:
        """Generate a model completion.

        Args:
            messages: Conversation history.
            tools: Optional OpenAI-compatible tool definitions.
            timeout: Maximum seconds to wait for generation.

        Returns:
            ModelResponse containing the assistant message, finish reason, and optional usage.
        """
        ...
