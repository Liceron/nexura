# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Types and format converters for Native Gemini Provider."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from nexura_agents.providers.multimodal import gemini_parts
from nexura_agents.providers.types import ProviderMessage
from nexura_agents.types import Message, ToolCall


@dataclass(frozen=True)
class GeminiThoughtMetadata:
    """Thought and reasoning metadata emitted by Gemini thinking models."""

    thought: str
    signature: str | None = None
    grounding_metadata: dict[str, Any] = field(default_factory=dict)


def to_gemini_contents(messages: Sequence[Message | ProviderMessage]) -> list[dict[str, Any]]:
    """Convert SDK messages into Gemini native contents payload structure."""
    contents: list[dict[str, Any]] = []

    for msg in messages:
        if isinstance(msg, ProviderMessage):
            msg = msg.to_legacy()

        role = "user" if msg.role in ("user", "system") else "model"
        parts: list[dict[str, Any]] = []

        # Tool result message
        if msg.role == "tool":
            role = "user"
            resp_content: Any
            try:
                resp_content = json.loads(msg.content or "{}")
            except (json.JSONDecodeError, TypeError):
                resp_content = {"output": msg.content}

            resp_dict = resp_content if isinstance(resp_content, dict) else {"result": resp_content}
            parts.append(
                {
                    "functionResponse": {
                        "name": msg.name or "unknown_function",
                        "response": resp_dict,
                    }
                }
            )
            contents.append({"role": role, "parts": parts})
            continue

        # Standard text part
        if msg.content:
            parts.append({"text": msg.content})
        if msg.attachments:
            parts.extend(gemini_parts(msg.attachments))

        # Tool calls requested by model
        if msg.tool_calls:
            for tc in msg.tool_calls:
                parts.append(
                    {
                        "functionCall": {
                            "name": tc.name,
                            "args": tc.arguments,
                        }
                    }
                )

        if parts:
            contents.append({"role": role, "parts": parts})

    return contents


def to_gemini_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    """Convert OpenAI-compatible tool definitions to Gemini functionDeclarations."""
    if not tools:
        return None

    # Check if already in Gemini native format
    if any("functionDeclarations" in t for t in tools):
        return tools

    function_declarations: list[dict[str, Any]] = []
    for t in tools:
        fn = t.get("function", t)
        decl: dict[str, Any] = {
            "name": fn.get("name", "unnamed_tool"),
            "description": fn.get("description", ""),
        }
        if "parameters" in fn:
            decl["parameters"] = fn["parameters"]
        function_declarations.append(decl)

    return [{"functionDeclarations": function_declarations}]


def extract_gemini_tool_calls(parts: list[dict[str, Any]]) -> list[ToolCall]:
    """Extract ToolCall instances from Gemini parts."""
    calls: list[ToolCall] = []
    for idx, part in enumerate(parts):
        fn_call = part.get("functionCall")
        if fn_call and isinstance(fn_call, dict):
            name = fn_call.get("name", f"tool_{idx}")
            args = fn_call.get("args", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    args = {"raw": args}
            calls.append(
                ToolCall(
                    id=f"call_{name}_{idx}",
                    name=name,
                    arguments=args,
                    raw_arguments=json.dumps(args, ensure_ascii=False),
                )
            )
    return calls
