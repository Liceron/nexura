# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Deterministic fake model for testing, fixtures, and offline examples."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from typing import Any

from nexura_agents.limits import ModelSettings
from nexura_agents.models.base import ModelProtocol
from nexura_agents.providers.types import ContinuationPart, ProviderCapabilities, StreamDelta
from nexura_agents.types import Message, ModelResponse, ToolCall, Usage

FakeResponseItem = ModelResponse | Message | str | list[ToolCall]


class FakeModel(ModelProtocol):
    """Deterministic, keyless model adapter for tests and local examples.

    Does not perform network calls or require credentials. Supports canned
    response queues, custom programmatic handlers, and an endless tool-call fixture
    to verify runner bounds.
    """

    def __init__(
        self,
        responses: Sequence[FakeResponseItem] | None = None,
        *,
        handler: Callable[[list[Message], list[dict[str, Any]] | None], FakeResponseItem]
        | None = None,
        default_usage: Usage | None = None,
        repeat_last: bool = False,
        native_schema: bool = False,
        schema_with_tools: bool = False,
        stream: bool = False,
        chunk_size: int = 8,
    ) -> None:
        self._responses: list[FakeResponseItem] = list(responses) if responses is not None else []
        self._handler = handler
        self._default_usage = default_usage
        self._repeat_last = repeat_last
        if chunk_size < 1:
            raise ValueError("chunk_size must be at least 1")
        self._chunk_size = chunk_size
        if stream:
            # Opt-in only: without this attribute the runner never streams a FakeModel.
            self.capabilities = ProviderCapabilities(
                can_generate=True, can_stream=True, can_tools=True, reports_usage=True
            )
        self._last_response: FakeResponseItem | None = None
        # Opt-in native structured output: records the schema in ``schemas`` and in each call.
        self.supports_response_schema = native_schema
        self.response_schema_with_tools = native_schema and schema_with_tools
        self.schemas: list[dict[str, Any] | None] = []
        self.calls: list[dict[str, Any]] = []

    @classmethod
    def endless(
        cls,
        tool_name: str = "ping",
        arguments: dict[str, Any] | None = None,
        *,
        default_usage: Usage | None = None,
    ) -> FakeModel:
        """Create a fixture that endlessly returns a tool call to verify runner limits."""
        args = arguments if arguments is not None else {}

        def _handler(messages: list[Message], tools: list[dict[str, Any]] | None) -> list[ToolCall]:
            return [ToolCall(id=f"call_{uuid.uuid4().hex[:8]}", name=tool_name, arguments=args)]

        return cls(handler=_handler, default_usage=default_usage)

    async def generate(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        *,
        timeout: float | None = None,
        response_schema: dict[str, Any] | None = None,
        model_settings: ModelSettings | None = None,
    ) -> ModelResponse:
        """Generate the next canned or handler response."""
        return await self._next_response(messages, tools, timeout, response_schema, model_settings)

    async def _next_response(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None,
        timeout: float | None,
        response_schema: dict[str, Any] | None,
        model_settings: ModelSettings | None,
    ) -> ModelResponse:
        self.schemas.append(response_schema)
        self.calls.append(
            {
                "messages": list(messages),
                "tools": tools,
                "timeout": timeout,
                "response_schema": response_schema,
                "model_settings": model_settings,
            }
        )

        if self._handler is not None:
            raw_item = self._handler(messages, tools)
        elif self._responses:
            raw_item = self._responses.pop(0)
            if self._repeat_last:
                self._last_response = raw_item
        elif self._repeat_last and self._last_response is not None:
            raw_item = self._last_response
        else:
            raw_item = "FakeModel: No more canned responses."

        return self._coerce_response(raw_item)

    async def stream(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        *,
        timeout: float | None = None,
        response_schema: dict[str, Any] | None = None,
        model_settings: ModelSettings | None = None,
    ) -> AsyncIterator[StreamDelta]:
        """Stream the next scripted response as deltas (used when ``stream=True``).

        Text is split into ``chunk_size``-character ``text_delta`` chunks. Each tool call
        becomes ``tool_call_delta`` chunks: id and name in the first, the JSON arguments
        split across the rest. ``provider_data`` becomes ``continuation_delta`` chunks
        (Anthropic thinking blocks in the form the runner keeps). A ``finish`` delta carries
        usage and the same finish reason as :meth:`generate`. Calls are recorded in
        ``calls`` exactly like :meth:`generate`.
        """
        response = await self._next_response(
            messages, tools, timeout, response_schema, model_settings
        )
        message = response.message
        size = self._chunk_size
        for token, value in (message.provider_data or {}).items():
            if token == "anthropic_thinking" and isinstance(value, list):
                for block in value:
                    part = ContinuationPart(token=token, provider_metadata={"block": block})
                    yield StreamDelta(kind="continuation_delta", continuation=part)
            else:
                part = ContinuationPart(token=token, provider_metadata={"value": value})
                yield StreamDelta(kind="continuation_delta", continuation=part)
        text = message.content if isinstance(message.content, str) else ""
        for i in range(0, len(text), size):
            yield StreamDelta(kind="text_delta", text=text[i : i + size])
        for index, call in enumerate(message.tool_calls or []):
            raw = (
                call.raw_arguments if call.raw_arguments is not None else json.dumps(call.arguments)
            )
            pieces = [raw[i : i + size] for i in range(0, len(raw), size)] or [""]
            for n, piece in enumerate(pieces):
                yield StreamDelta(
                    kind="tool_call_delta",
                    tool_call_index=index,
                    tool_call_id=call.id if n == 0 else None,
                    tool_name=call.name if n == 0 else None,
                    tool_arguments_delta=piece,
                )
        yield StreamDelta(kind="finish", usage=response.usage, finish_reason=response.finish_reason)

    def _coerce_response(self, item: FakeResponseItem) -> ModelResponse:
        """Convert response item into standardized ModelResponse."""
        if isinstance(item, ModelResponse):
            return item

        if isinstance(item, Message):
            return ModelResponse(message=item, usage=self._default_usage)

        if isinstance(item, str):
            return ModelResponse(
                message=Message(role="assistant", content=item),
                usage=self._default_usage,
            )

        if isinstance(item, list):
            # List of ToolCall
            return ModelResponse(
                message=Message(role="assistant", content=None, tool_calls=item),
                usage=self._default_usage,
            )

        raise TypeError(f"Unsupported FakeModel response item type: {type(item)}")

    def __repr__(self) -> str:
        return (
            f"FakeModel(remaining_responses={len(self._responses)}, calls_made={len(self.calls)})"
        )
