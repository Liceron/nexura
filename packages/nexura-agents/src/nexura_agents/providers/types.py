# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Core types and models for provider-neutral model protocol and streaming."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from nexura_agents.attachments import Attachment
from nexura_agents.providers.errors import MalformedToolCallArgumentsError
from nexura_agents.types import Message, Role, ToolCall, Usage


@dataclass(frozen=True)
class ProviderCapabilities:
    """Explicit capability declarations for a model provider.

    Final-only providers must set can_stream=False.
    """

    can_generate: bool = True
    can_stream: bool = False
    can_embed: bool = False
    can_tools: bool = False
    can_structured_output: bool = False
    can_multimodal: bool = False
    reports_usage: bool = False


# Provider-neutral content parts
@dataclass(frozen=True)
class TextPart:
    """Standard textual content part."""

    text: str


@dataclass(frozen=True)
class ArtifactPart:
    """Reference to an artifact stored in the artifact subsystem."""

    artifact_id: str
    mime_type: str = "application/octet-stream"


@dataclass(frozen=True)
class ToolCallPart:
    """Represents a structured tool call requested by a model."""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    raw_arguments: str | None = None


@dataclass(frozen=True)
class ToolResultPart:
    """Represents the execution outcome of a tool call."""

    tool_call_id: str
    name: str
    content: str
    is_error: bool = False


@dataclass(frozen=True)
class ContinuationPart:
    """Provider-required opaque continuation metadata.

    Must be preserved across turns without being normalized away.
    """

    token: str
    provider_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AttachmentPart:
    """An image, document or audio clip sent to the model."""

    attachment: Attachment


ContentPart = (
    TextPart | ArtifactPart | ToolCallPart | ToolResultPart | ContinuationPart | AttachmentPart
)


@dataclass
class ProviderMessage:
    """Provider-neutral conversation message supporting multimodal parts and continuation."""

    role: Role
    parts: list[ContentPart] = field(default_factory=list)
    provider_metadata: dict[str, Any] = field(default_factory=dict)
    name: str | None = None
    tool_call_id: str | None = None

    @property
    def text_content(self) -> str:
        """Concatenate all TextPart items into a single string."""
        return "".join(part.text for part in self.parts if isinstance(part, TextPart))

    @property
    def tool_calls(self) -> list[ToolCall]:
        """Extract tool calls as standard ToolCall objects."""
        return [
            ToolCall(
                id=part.id,
                name=part.name,
                arguments=part.arguments,
                raw_arguments=part.raw_arguments,
            )
            for part in self.parts
            if isinstance(part, ToolCallPart)
        ]

    @classmethod
    def from_legacy(cls, msg: Message) -> ProviderMessage:
        """Convert a legacy Message into a provider-neutral ProviderMessage."""
        parts: list[ContentPart] = []
        if msg.content is not None:
            parts.append(TextPart(text=msg.content))
        if msg.tool_calls:
            for tc in msg.tool_calls:
                parts.append(
                    ToolCallPart(
                        id=tc.id,
                        name=tc.name,
                        arguments=tc.arguments,
                        raw_arguments=tc.raw_arguments,
                    )
                )
        for attachment in msg.attachments or []:
            parts.append(AttachmentPart(attachment))
        return cls(
            role=msg.role,
            parts=parts,
            name=msg.name,
            tool_call_id=msg.tool_call_id,
        )

    def to_legacy(self) -> Message:
        """Convert to a legacy Message for backwards compatibility."""
        content = self.text_content if self.text_content else None
        calls = self.tool_calls if self.tool_calls else None
        attachments = [p.attachment for p in self.parts if isinstance(p, AttachmentPart)]
        return Message(
            role=self.role,
            content=content,
            tool_calls=calls,
            tool_call_id=self.tool_call_id,
            name=self.name,
            attachments=attachments or None,
        )


StreamDeltaKind = Literal[
    "text_delta",
    "tool_call_delta",
    "continuation_delta",
    "usage_delta",
    "finish",
]


@dataclass(frozen=True)
class StreamDelta:
    """Real-time incremental delta streamed by a model provider."""

    kind: StreamDeltaKind
    text: str | None = None
    tool_call_index: int | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    tool_arguments_delta: str | None = None
    continuation: ContinuationPart | None = None
    usage: Usage | None = None
    finish_reason: str | None = None
    provider_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class _ToolCallAccumulator:
    """Internal accumulator for a single streaming tool call."""

    id: str | None = None
    name: str | None = None
    raw_buffer: list[str] = field(default_factory=list)
    index: int = 0


class ToolCallAssembler:
    """Assembles fragmented and interleaved streaming tool call chunks into validated calls.

    Guarantees:
    - Retains tool IDs across interleaved/fragmented chunks.
    - Validates arguments JSON before dispatch.
    - Enforces bounded memory buffers.
    - Malformed arguments raise MalformedToolCallArgumentsError and never execute.
    """

    def __init__(self, *, max_argument_bytes: int = 1_000_000) -> None:
        self.max_argument_bytes = max_argument_bytes
        self._calls_by_index: dict[int, _ToolCallAccumulator] = {}
        self._calls_by_id: dict[str, _ToolCallAccumulator] = {}
        self._total_bytes: int = 0

    def feed(self, delta: StreamDelta) -> None:
        """Feed a stream delta into the assembler."""
        if delta.kind != "tool_call_delta":
            return

        # Identify target accumulator by index or id
        idx = delta.tool_call_index if delta.tool_call_index is not None else 0
        if idx not in self._calls_by_index:
            acc = _ToolCallAccumulator(index=idx)
            self._calls_by_index[idx] = acc
        else:
            acc = self._calls_by_index[idx]

        if delta.tool_call_id:  # some servers repeat "" on later chunks; keep the real one
            acc.id = delta.tool_call_id
            self._calls_by_id[delta.tool_call_id] = acc

        if delta.tool_name:
            acc.name = delta.tool_name

        if delta.tool_arguments_delta:
            delta_len = len(delta.tool_arguments_delta.encode("utf-8"))
            self._total_bytes += delta_len
            if self._total_bytes > self.max_argument_bytes:
                raise MalformedToolCallArgumentsError(
                    tool_name=acc.name or "unknown",
                    tool_call_id=acc.id or f"idx_{idx}",
                    raw_arguments="",
                    reason=f"Exceeded max argument buffer limit of {self.max_argument_bytes} bytes",
                )
            acc.raw_buffer.append(delta.tool_arguments_delta)

    def validate_and_assemble(self) -> list[ToolCall]:
        """Validate and return assembled tool calls.

        Raises MalformedToolCallArgumentsError if any tool call has malformed JSON arguments,
        missing name, or missing ID, ensuring malformed arguments never execute.
        """
        results: list[ToolCall] = []

        # Sort by index for deterministic ordering
        for idx in sorted(self._calls_by_index.keys()):
            acc = self._calls_by_index[idx]
            call_id = acc.id or f"call_{idx}"
            tool_name = acc.name or "unnamed_tool"
            raw_json = "".join(acc.raw_buffer).strip()

            if not raw_json:
                parsed_args: dict[str, Any] = {}
            else:
                try:
                    parsed_obj = json.loads(raw_json)
                    if not isinstance(parsed_obj, dict):
                        raise MalformedToolCallArgumentsError(
                            tool_name=tool_name,
                            tool_call_id=call_id,
                            raw_arguments=raw_json,
                            reason=f"Expected JSON object dict, got {type(parsed_obj).__name__}",
                        )
                    parsed_args = parsed_obj
                except json.JSONDecodeError as exc:
                    raise MalformedToolCallArgumentsError(
                        tool_name=tool_name,
                        tool_call_id=call_id,
                        raw_arguments=raw_json,
                        reason=f"JSON decode failure: {exc}",
                    ) from exc

            results.append(
                ToolCall(
                    id=call_id,
                    name=tool_name,
                    arguments=parsed_args,
                    raw_arguments=raw_json,
                )
            )

        return results


@dataclass(frozen=True)
class EmbeddingResult:
    """Result of vector embedding generation."""

    embeddings: list[list[float]]
    model: str
    usage: Usage | None = None


@dataclass
class GenerationRequest:
    """Request payload for model generation or streaming."""

    messages: Sequence[ProviderMessage | Message]
    tools: list[dict[str, Any]] | None = None
    schema: dict[str, Any] | None = None
    timeout: float | None = None
    max_tokens: int | None = None
    temperature: float | None = None
    stop_sequences: list[str] | None = None
    # "auto" | "required" | "none" | a tool name; providers map it to their own field.
    tool_choice: str | None = None
    # ModelSettings fields added in F30 (see limits.apply_*_settings for the mappings).
    top_p: float | None = None
    seed: int | None = None
    presence_penalty: float | None = None
    frequency_penalty: float | None = None
    reasoning_effort: str | None = None
    thinking_budget_tokens: int | None = None
    extra: dict[str, Any] | None = None
    provider_metadata: dict[str, Any] = field(default_factory=dict)
    cache_plan: Any | None = None
    cache_policy: Any | None = None
    context_policy: Any | None = None
    context_decision: Any | None = None
    context_bundle: Any | None = None

    def to_legacy_messages(self) -> list[Message]:
        """Convert all messages to legacy Message format."""
        legacy: list[Message] = []
        for m in self.messages:
            if isinstance(m, Message):
                legacy.append(m)
            elif isinstance(m, ProviderMessage):
                legacy.append(m.to_legacy())
        return legacy
