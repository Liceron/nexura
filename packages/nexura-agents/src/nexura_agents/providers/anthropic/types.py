# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Types, schemas, and transformers for native Anthropic Messages API."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from nexura_agents.errors import (
    ProviderRequestError,
    ProviderResponseError,
)
from nexura_agents.providers.multimodal import anthropic_blocks
from nexura_agents.providers.types import (
    AttachmentPart,
    ContentPart,
    ProviderMessage,
    TextPart,
    ToolCallPart,
    ToolResultPart,
)
from nexura_agents.types import Message, ToolCall, Usage


@dataclass(frozen=True)
class AnthropicMetadata:
    """Provider-specific metadata returned by Anthropic Messages API."""

    message_id: str | None = None
    model: str | None = None
    stop_reason: str | None = None
    stop_sequence: str | None = None
    cache_creation_input_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


def map_anthropic_stop_reason(stop_reason: str | None) -> str:
    """Map Anthropic stop_reason to standardized finish_reason.

    Ensures max_tokens is accurately mapped to 'length' rather than silently
    reporting normal completion success.
    """
    if not stop_reason:
        return "stop"
    mapping = {
        "end_turn": "stop",
        "tool_use": "tool_calls",
        "max_tokens": "length",
        "stop_sequence": "stop",
        "refusal": "refusal",
    }
    return mapping.get(stop_reason, "stop")


def to_anthropic_tools(tools: Sequence[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Convert tool specifications into Anthropic native tool schema.

    Accepts OpenAPI/OpenAI function definitions or native Anthropic tool declarations.
    Rejects duplicate tool names eagerly.
    """
    if not tools:
        return []

    anthropic_tools: list[dict[str, Any]] = []
    seen_names: set[str] = set()

    for tool in tools:
        if "function" in tool and isinstance(tool["function"], dict):
            fn = tool["function"]
            name = fn.get("name")
            desc = fn.get("description", "")
            schema = fn.get("parameters", {"type": "object", "properties": {}})
        else:
            name = tool.get("name")
            desc = tool.get("description", "")
            schema = tool.get("input_schema") or tool.get(
                "parameters", {"type": "object", "properties": {}}
            )

        if not name or not isinstance(name, str) or not name.strip():
            raise ProviderRequestError("Anthropic tool definition requires a non-empty name.")

        clean_name = name.strip()
        if clean_name in seen_names:
            raise ProviderRequestError(f"Duplicate tool name '{clean_name}' in tools declaration.")
        seen_names.add(clean_name)

        tool_dict: dict[str, Any] = {
            "name": clean_name,
            "description": desc,
            "input_schema": schema,
        }
        if "cache_control" in tool and isinstance(tool["cache_control"], dict):
            tool_dict["cache_control"] = tool["cache_control"]

        anthropic_tools.append(tool_dict)

    return anthropic_tools


def to_anthropic_messages(
    messages: Sequence[Message | ProviderMessage],
) -> tuple[str | None, list[dict[str, Any]]]:
    """Transform unified messages into Anthropic system prompt and message array.

    Enforces:
    1. Extraction and concatenation of top-level system prompts.
    2. Conversion of tool results into Anthropic user turn content blocks.
    3. Strict validation that tool IDs are present, non-empty, and unique.
    4. Preservation of mixed text and tool-use blocks across turn replay.
    5. Merging of consecutive turns of the same role for Anthropic role alternation.
    """
    system_parts: list[str] = []
    anthropic_messages: list[dict[str, Any]] = []
    seen_tool_use_ids: set[str] = set()

    for msg in messages:
        if isinstance(msg, ProviderMessage):
            role = msg.role
            parts: list[ContentPart] = msg.parts
        else:
            role = msg.role
            parts = []
            if role != "tool" and msg.content is not None:
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
            if role == "tool" and msg.tool_call_id is not None:
                parts.append(
                    ToolResultPart(
                        tool_call_id=msg.tool_call_id,
                        name=msg.name or "",
                        content=msg.content or "",
                    )
                )

        if role == "system":
            sys_text = "".join(p.text for p in parts if isinstance(p, TextPart))
            if sys_text.strip():
                system_parts.append(sys_text.strip())
            continue

        if role == "tool":
            # Tool result turns are mapped to user role content blocks in Anthropic
            blocks: list[dict[str, Any]] = []
            for p in parts:
                if isinstance(p, ToolResultPart):
                    if not p.tool_call_id or not p.tool_call_id.strip():
                        raise ProviderRequestError(
                            "Tool result content block missing valid tool_use_id."
                        )
                    blocks.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": p.tool_call_id.strip(),
                            "content": p.content,
                            "is_error": p.is_error,
                        }
                    )
                elif isinstance(p, TextPart) and p.text:
                    blocks.append({"type": "text", "text": p.text})

            # If legacy message was role="tool" without parts:
            if not blocks and isinstance(msg, Message):
                if not msg.tool_call_id or not msg.tool_call_id.strip():
                    raise ProviderRequestError(
                        "Tool result message missing valid non-empty tool_call_id."
                    )
                blocks.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": msg.tool_call_id.strip(),
                        "content": msg.content or "",
                    }
                )

            _append_or_merge_turn(anthropic_messages, "user", blocks)
            continue

        if role == "user":
            user_blocks: list[dict[str, Any]] = []
            for p in parts:
                if isinstance(p, TextPart):
                    user_blocks.append({"type": "text", "text": p.text})
                elif isinstance(p, ToolResultPart):
                    if not p.tool_call_id or not p.tool_call_id.strip():
                        raise ProviderRequestError(
                            "Tool result content block missing valid tool_use_id."
                        )
                    user_blocks.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": p.tool_call_id.strip(),
                            "content": p.content,
                            "is_error": p.is_error,
                        }
                    )
            if not user_blocks and isinstance(msg, Message) and msg.content:
                user_blocks.append({"type": "text", "text": msg.content})
            # Attachments go first, as Anthropic recommends for images and documents.
            attached = [p.attachment for p in parts if isinstance(p, AttachmentPart)]
            if isinstance(msg, Message) and msg.attachments:
                attached.extend(msg.attachments)
            if attached:
                user_blocks = anthropic_blocks(attached) + user_blocks

            _append_or_merge_turn(anthropic_messages, "user", user_blocks)
            continue

        if role == "assistant":
            asst_blocks: list[dict[str, Any]] = []
            for p in parts:
                if isinstance(p, TextPart):
                    asst_blocks.append({"type": "text", "text": p.text})
                elif isinstance(p, ToolCallPart):
                    if not p.id or not p.id.strip():
                        raise ProviderRequestError(
                            f"Tool use block for '{p.name}' missing valid non-empty ID."
                        )
                    tc_id = p.id.strip()
                    if tc_id in seen_tool_use_ids:
                        raise ProviderRequestError(
                            f"Duplicate tool use ID '{tc_id}' detected in request."
                        )
                    seen_tool_use_ids.add(tc_id)
                    asst_blocks.append(
                        {
                            "type": "tool_use",
                            "id": tc_id,
                            "name": p.name,
                            "input": p.arguments,
                        }
                    )
            if not asst_blocks and isinstance(msg, Message):
                if msg.content:
                    asst_blocks.append({"type": "text", "text": msg.content})
                if msg.tool_calls:
                    for tc in msg.tool_calls:
                        if not tc.id or not tc.id.strip():
                            raise ProviderRequestError(
                                f"Tool call '{tc.name}' missing valid non-empty ID."
                            )
                        if tc.id in seen_tool_use_ids:
                            raise ProviderRequestError(
                                f"Duplicate tool use ID '{tc.id}' detected in request."
                            )
                        seen_tool_use_ids.add(tc.id)
                        asst_blocks.append(
                            {
                                "type": "tool_use",
                                "id": tc.id,
                                "name": tc.name,
                                "input": tc.arguments,
                            }
                        )

            if isinstance(msg, Message) and msg.provider_data:
                # Extended thinking: the thinking blocks (with signatures) must come back
                # first in the assistant turn that called the tools.
                thinking = msg.provider_data.get(ANTHROPIC_THINKING_KEY) or []
                asst_blocks = [dict(b) for b in thinking if isinstance(b, dict)] + asst_blocks
            _append_or_merge_turn(anthropic_messages, "assistant", asst_blocks)

    system_prompt = "\n\n".join(system_parts) if system_parts else None
    return system_prompt, anthropic_messages


def _append_or_merge_turn(
    messages: list[dict[str, Any]],
    role: str,
    blocks: list[dict[str, Any]],
) -> None:
    """Append a turn or merge blocks if consecutive turns share the same role."""
    if not blocks:
        return

    if messages and messages[-1]["role"] == role:
        existing_content = messages[-1]["content"]
        if isinstance(existing_content, list):
            existing_content.extend(blocks)
        elif isinstance(existing_content, str):
            messages[-1]["content"] = [{"type": "text", "text": existing_content}, *blocks]
        if role == "assistant":
            # Anthropic requires thinking blocks first in an assistant turn, also when two
            # assistant messages were merged into one.
            content = messages[-1]["content"]
            thinking = [b for b in content if b.get("type") in _THINKING_TYPES]
            if thinking:
                rest = [b for b in content if b.get("type") not in _THINKING_TYPES]
                messages[-1]["content"] = thinking + rest
    else:
        messages.append({"role": role, "content": blocks})


_THINKING_TYPES = ("thinking", "redacted_thinking")


ANTHROPIC_THINKING_KEY = "anthropic_thinking"


def extract_anthropic_response(
    resp_dict: dict[str, Any],
) -> tuple[Message, Usage | None, str, AnthropicMetadata]:
    """Parse Anthropic response JSON into unified Message, Usage, finish_reason, and metadata.

    Validates that:
    1. Tool call IDs in the response are non-empty and unique.
    2. Mixed text and tool_use blocks are parsed and preserved.
    3. Max-token stop reason is cleanly reported as 'length'.
    4. Prompt cache metrics are preserved.
    """
    if resp_dict.get("type") == "error":
        err = resp_dict.get("error", {})
        err_msg = err.get("message", "Unknown Anthropic error")
        raise ProviderResponseError(f"Anthropic error ({err.get('type')}): {err_msg}")

    content_blocks = resp_dict.get("content", [])
    if not isinstance(content_blocks, list):
        raise ProviderResponseError("Anthropic response 'content' must be an array of blocks.")

    text_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    seen_response_tool_ids: set[str] = set()
    thinking_blocks: list[dict[str, Any]] = []

    for block in content_blocks:
        b_type = block.get("type")
        if b_type in ("thinking", "redacted_thinking"):
            thinking_blocks.append(dict(block))  # never the answer; kept for the next call
        elif b_type == "text":
            text_parts.append(block.get("text", ""))
        elif b_type == "tool_use":
            tc_id = block.get("id")
            if not tc_id or not isinstance(tc_id, str) or not tc_id.strip():
                raise ProviderResponseError(
                    "Anthropic response contains tool_use block with missing or empty ID."
                )
            clean_id = tc_id.strip()
            if clean_id in seen_response_tool_ids:
                raise ProviderResponseError(
                    f"Duplicate tool use ID '{clean_id}' returned in Anthropic response."
                )
            seen_response_tool_ids.add(clean_id)

            raw_args = block.get("input", {})
            if isinstance(raw_args, str):
                try:
                    parsed_args = json.loads(raw_args)
                except Exception:
                    parsed_args = {"raw": raw_args}
            else:
                parsed_args = raw_args

            tool_calls.append(
                ToolCall(
                    id=clean_id,
                    name=block.get("name", "unnamed_tool"),
                    arguments=parsed_args,
                    raw_arguments=json.dumps(parsed_args),
                )
            )

    full_text = "".join(text_parts) if text_parts else None
    final_tool_calls = tool_calls if tool_calls else None
    stop_reason = resp_dict.get("stop_reason")
    finish_reason = map_anthropic_stop_reason(stop_reason)

    usage_data = resp_dict.get("usage")
    usage: Usage | None = None
    cache_creation: int | None = None
    cache_read: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None

    if usage_data and isinstance(usage_data, dict):
        input_tokens = usage_data.get("input_tokens")
        output_tokens = usage_data.get("output_tokens")
        cache_creation = usage_data.get("cache_creation_input_tokens")
        cache_read = usage_data.get("cache_read_input_tokens")

        total = 0
        has_metric = False
        for val in [input_tokens, output_tokens, cache_creation, cache_read]:
            if val is not None:
                total += val
                has_metric = True

        if has_metric:
            usage = Usage(
                prompt_tokens=input_tokens,
                completion_tokens=output_tokens,
                total_tokens=total,
                is_complete=True,
                cached_prompt_tokens=cache_read,
                cache_creation_input_tokens=cache_creation,
                cache_read_input_tokens=cache_read,
            )

    metadata = AnthropicMetadata(
        message_id=resp_dict.get("id"),
        model=resp_dict.get("model"),
        stop_reason=stop_reason,
        stop_sequence=resp_dict.get("stop_sequence"),
        cache_creation_input_tokens=cache_creation,
        cache_read_input_tokens=cache_read,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )

    message = Message(
        role="assistant",
        content=full_text,
        tool_calls=final_tool_calls,
        provider_data={ANTHROPIC_THINKING_KEY: thinking_blocks} if thinking_blocks else None,
    )

    return message, usage, finish_reason, metadata
