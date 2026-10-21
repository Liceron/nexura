# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Types and format converters for Native Gemini Provider."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
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


# Message.provider_data key: {tool call id: thoughtSignature} for the calls of one model turn.
GEMINI_SIGNATURES_KEY = "gemini_thought_signatures"

# Ids the adapter makes up when Gemini sends a functionCall without an ``id``. They are never
# sent back to Gemini (only ids Gemini generated are echoed in functionCall/functionResponse).
_SYNTHETIC_CALL_ID = re.compile(r"^call_.+_\d+$")


def synthetic_call_id(name: str, index: int) -> str:
    """The id used for a functionCall that came without one."""
    return f"call_{name}_{index}"


def _is_gemini_id(call_id: str | None) -> bool:
    return bool(call_id) and not _SYNTHETIC_CALL_ID.match(call_id or "")


def to_gemini_request(
    messages: Sequence[Message | ProviderMessage],
    signatures: Mapping[str, str] | None = None,
    *,
    unsigned_call_signature: str | None = None,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """``(systemInstruction, contents)`` for a ``generateContent`` request.

    - System messages become ``systemInstruction`` (text only), not user turns.
    - A model turn's ``functionCall`` parts carry the ``id`` Gemini gave them and the
      ``thoughtSignature`` it returned (Gemini 3 and later reject a turn whose first function
      call lost its signature). Signatures come from ``Message.provider_data`` or from
      ``signatures`` (the provider's record of recent calls). When the first call of a turn has
      no known signature, ``unsigned_call_signature`` is used if set (Google documents
      ``"skip_thought_signature_validator"`` for calls it did not generate).
    - Consecutive tool results are sent as one user turn holding every ``functionResponse``,
      as Gemini expects after parallel calls.
    """
    system_texts: list[str] = []
    contents: list[dict[str, Any]] = []
    known = dict(signatures or {})

    for msg in messages:
        if isinstance(msg, ProviderMessage):
            msg = msg.to_legacy()

        if msg.role == "system":
            if msg.content and msg.content.strip():
                system_texts.append(msg.content)
            continue

        role = "user" if msg.role == "user" else "model"
        parts: list[dict[str, Any]] = []

        # Tool result message
        if msg.role == "tool":
            resp_content: Any
            try:
                resp_content = json.loads(msg.content or "{}")
            except (json.JSONDecodeError, TypeError):
                resp_content = {"output": msg.content}

            resp_dict = resp_content if isinstance(resp_content, dict) else {"result": resp_content}
            function_response: dict[str, Any] = {
                "name": msg.name or "unknown_function",
                "response": resp_dict,
            }
            if _is_gemini_id(msg.tool_call_id):
                function_response["id"] = msg.tool_call_id
            part = {"functionResponse": function_response}
            previous = contents[-1] if contents else None
            if (
                previous is not None
                and previous["role"] == "user"
                and previous["parts"]
                and all("functionResponse" in p for p in previous["parts"])
            ):
                previous["parts"].append(part)
            else:
                contents.append({"role": "user", "parts": [part]})
            continue

        # Standard text part
        if msg.content:
            parts.append({"text": msg.content})
        if msg.attachments:
            parts.extend(gemini_parts(msg.attachments))

        # Tool calls requested by model
        if msg.tool_calls:
            own = (msg.provider_data or {}).get(GEMINI_SIGNATURES_KEY) or {}
            for position, tc in enumerate(msg.tool_calls):
                function_call: dict[str, Any] = {"name": tc.name, "args": tc.arguments}
                if _is_gemini_id(tc.id):
                    function_call["id"] = tc.id
                call_part: dict[str, Any] = {"functionCall": function_call}
                signature = own.get(tc.id) if isinstance(own, dict) else None
                signature = signature or known.get(tc.id)
                if signature is None and position == 0 and role == "model":
                    signature = unsigned_call_signature
                if signature:
                    call_part["thoughtSignature"] = signature
                parts.append(call_part)

        if parts:
            contents.append({"role": role, "parts": parts})

    if system_texts and not contents:
        # A request with only system text still needs one user turn.
        return None, [{"role": "user", "parts": [{"text": "\n\n".join(system_texts)}]}]
    system_instruction = {"parts": [{"text": "\n\n".join(system_texts)}]} if system_texts else None
    return system_instruction, contents


def to_gemini_contents(messages: Sequence[Message | ProviderMessage]) -> list[dict[str, Any]]:
    """Convert SDK messages into Gemini ``contents`` (system text kept as a first user turn).

    The provider itself uses :func:`to_gemini_request`, which sends system text as
    ``systemInstruction``; this helper is kept for callers that want a single list.
    """
    system_instruction, contents = to_gemini_request(messages)
    if system_instruction is not None:
        contents.insert(0, {"role": "user", "parts": system_instruction["parts"]})
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
    """Extract ToolCall instances from Gemini parts (Gemini's own ``id`` when it sent one)."""
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
            given_id = fn_call.get("id")
            calls.append(
                ToolCall(
                    id=given_id
                    if isinstance(given_id, str) and given_id
                    else synthetic_call_id(name, idx),
                    name=name,
                    arguments=args,
                    raw_arguments=json.dumps(args, ensure_ascii=False),
                )
            )
    return calls


def extract_gemini_signatures(parts: list[dict[str, Any]], calls: list[ToolCall]) -> dict[str, str]:
    """``{call id: thoughtSignature}`` for the functionCall parts that carry a signature."""
    signatures: dict[str, str] = {}
    call_iter = iter(calls)
    for part in parts:
        if not isinstance(part.get("functionCall"), dict):
            continue
        call = next(call_iter, None)
        signature = part.get("thoughtSignature")
        if call is not None and isinstance(signature, str) and signature:
            signatures[call.id] = signature
    return signatures


# Gemini finishReason values that mean a safety or policy filter stopped the answer.
CONTENT_FILTER_FINISH = frozenset(
    {
        "SAFETY",
        "RECITATION",
        "BLOCKLIST",
        "PROHIBITED_CONTENT",
        "SPII",
        "IMAGE_SAFETY",
        "IMAGE_PROHIBITED_CONTENT",
        "IMAGE_RECITATION",
        "LANGUAGE",
        "ESCALATION",
        "PUP_LIMITED_DISABLED",
    }
)


def map_gemini_finish_reason(reason: str | None) -> str:
    """Gemini ``finishReason`` as a Nexura finish reason: ``MAX_TOKENS`` becomes ``length``
    and the safety/policy family ``content_filter`` (as OpenAI reports them); other values are
    lower-cased (``stop``, ``malformed_function_call``, ...)."""
    if not reason:
        return "stop"
    upper = reason.upper()
    if upper == "MAX_TOKENS":
        return "length"
    if upper in CONTENT_FILTER_FINISH:
        return "content_filter"
    return reason.lower()
