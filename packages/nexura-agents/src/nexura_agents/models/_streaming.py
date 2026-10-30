# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Shared decoding of OpenAI-style streaming chat chunks into ``StreamDelta`` values.

Used by ``OpenAIChatModel`` (SSE JSON bodies) and ``LiteLLMModel`` (``ModelResponseStream``
chunks converted to dicts). The deltas are the ones the Runner already consumes from
provider models (``text_delta``, ``tool_call_delta``, ``usage_delta``, ``finish``).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from nexura_agents.errors import ProviderAuthError, ProviderResponseError
from nexura_agents.providers.errors import _mask_secrets, http_status_error
from nexura_agents.providers.types import StreamDelta
from nexura_agents.types import Usage

UsageParser = Callable[[dict[str, Any]], Usage]


_STRING_CODE_STATUS: dict[str, int] = {
    "rate_limit_exceeded": 429,
    "rate_limit_error": 429,
    "insufficient_quota": 429,
    "invalid_api_key": 401,
    "authentication_error": 401,
    "permission_error": 403,
    "server_error": 500,
    "overloaded": 503,
    "overloaded_error": 529,
    "api_error": 500,
    "server_is_overloaded": 503,
    "service_unavailable_error": 503,
    "credit_balance_exhausted": 429,
    "invalid_request_error": 400,
    "request_too_large": 413,
}


def _error_from_chunk(err: Any, redact: Callable[[str], str]) -> BaseException:
    """Typed error for an in-stream ``{"error": ...}`` object (same mapping as HTTP errors)."""
    message = err.get("message") if isinstance(err, dict) else None
    text = message if isinstance(message, str) and message else "provider reported an error"
    text = redact(text)
    status: Any = None
    if isinstance(err, dict):
        status = err.get("status") or err.get("status_code") or err.get("code")
        if isinstance(status, str) and status.isdigit():
            status = int(status)
    if isinstance(status, str) and not isinstance(status, int):
        status = _STRING_CODE_STATUS.get(status.strip().lower())
    if status is None and isinstance(err, dict):
        etype = err.get("type")
        if isinstance(etype, str):
            status = _STRING_CODE_STATUS.get(etype.strip().lower())
    if isinstance(status, int) and not isinstance(status, bool):
        if status in (401, 403):
            return ProviderAuthError(f"Provider authentication failed (HTTP {status}): {text}")
        if status >= 400:
            codes = [
                str(err.get(k)) for k in ("code", "type") if isinstance(err, dict) and err.get(k)
            ]
            return http_status_error(
                status, f"Provider returned error HTTP {status}: {text}", codes=codes
            )
    return ProviderResponseError(f"Provider stream error: {text}")


class ChatChunkDecoder:
    """Stateful decoder for one streamed chat completion (first choice only)."""

    def __init__(
        self,
        parse_usage: UsageParser,
        *,
        api_key: str | None = None,
        secrets: Iterable[str] = (),
    ) -> None:
        self._parse_usage = parse_usage
        self._api_key = api_key
        # Configured header values: masked wherever an error message echoes them.
        self._secrets = sorted({v for v in secrets if len(v) >= 4}, key=len, reverse=True)
        self.saw_content = False
        self.finish_seen = False
        self.done = False
        self._tool_ids: dict[str, int] = {}
        self._tool_indexes: set[int] = set()
        self._named: set[int] = set()
        self._last_index = 0
        self._remap: dict[int, int] = {}
        self._index_ids: dict[int, str] = {}

    def _redact(self, text: str) -> str:
        if self._api_key:
            text = text.replace(self._api_key, "[REDACTED_SECRET]")
        for secret in self._secrets:
            text = text.replace(secret, "[REDACTED_SECRET]")
        return _mask_secrets(text)

    def _tool_index(self, tc: dict[str, Any]) -> int:
        index = tc.get("index")
        tc_id = tc.get("id")
        has_id = isinstance(tc_id, str) and bool(tc_id)
        if has_id and tc_id in self._tool_ids:
            resolved = self._tool_ids[tc_id]
        elif isinstance(index, int) and not isinstance(index, bool):
            resolved = self._remap.get(index, index)
            bound = self._index_ids.get(resolved)
            if has_id and bound is not None and bound != tc_id:
                # Same provider index reused for a distinct call id: allocate a fresh slot.
                used = self._tool_indexes | set(self._index_ids) | {resolved}
                resolved = max(used) + 1
                self._remap[index] = resolved
        elif has_id:
            resolved = len(self._tool_indexes)
        else:
            resolved = self._last_index
        if has_id:
            self._tool_ids.setdefault(tc_id, resolved)  # type: ignore[arg-type]
            self._index_ids.setdefault(resolved, tc_id)  # type: ignore[arg-type]
        self._last_index = resolved
        return resolved

    def feed(self, chunk: Any) -> list[StreamDelta]:
        """Decode one parsed chunk; raise the typed error for malformed or error chunks."""
        if not isinstance(chunk, dict):
            raise ProviderResponseError("Malformed stream chunk (expected a JSON object)")
        if chunk.get("error") is not None:
            raise _error_from_chunk(chunk["error"], self._redact)
        out: list[StreamDelta] = []
        choices = chunk.get("choices")
        if choices is not None and not isinstance(choices, list):
            raise ProviderResponseError("Provider stream chunk choices must be a list")
        choice = choices[0] if choices else None
        if choice is not None:
            if not isinstance(choice, dict):
                raise ProviderResponseError("Provider stream choice must be an object")
            delta = choice.get("delta")
            if delta is None:
                delta = {}
            if not isinstance(delta, dict):
                raise ProviderResponseError("Provider stream delta must be an object")
            content = delta.get("content")
            if content is not None:
                if not isinstance(content, str):
                    raise ProviderResponseError("Provider message content must be text or null")
                self.saw_content = True
                if content:
                    out.append(StreamDelta(kind="text_delta", text=content))
            raw_calls = delta.get("tool_calls")
            if raw_calls is not None and not isinstance(raw_calls, list):
                raise ProviderResponseError("Provider tool_calls must be a list or null")
            for tc in raw_calls or []:
                if not isinstance(tc, dict):
                    raise ProviderResponseError("Malformed tool_call item in response")
                if tc.get("type", "function") not in ("function", None):
                    raise ProviderResponseError("Unsupported tool call type")
                fn = tc.get("function")
                if fn is None:
                    fn = {}
                if not isinstance(fn, dict):
                    raise ProviderResponseError("Malformed tool_call function in response")
                name = fn.get("name")
                args = fn.get("arguments")
                tc_id = tc.get("id")
                if name is not None and not isinstance(name, str):
                    raise ProviderResponseError("Tool call name must be nonempty text")
                if args is not None and not isinstance(args, str):
                    raise ProviderResponseError("Malformed tool call JSON arguments")
                index = self._tool_index(tc)
                self._tool_indexes.add(index)
                if isinstance(name, str) and name.strip():
                    self._named.add(index)
                out.append(
                    StreamDelta(
                        kind="tool_call_delta",
                        tool_call_index=index,
                        tool_call_id=tc_id if isinstance(tc_id, str) and tc_id else None,
                        tool_name=name or None,
                        tool_arguments_delta=args or None,
                    )
                )
            finish_reason = choice.get("finish_reason")
            if isinstance(finish_reason, str) and finish_reason:
                self.finish_seen = True
                out.append(StreamDelta(kind="finish", finish_reason=finish_reason))
        usage = chunk.get("usage")
        if isinstance(usage, dict):
            out.append(StreamDelta(kind="usage_delta", usage=self._parse_usage(usage)))
        return out

    def finish(self) -> None:
        """Validate that the stream ended cleanly and produced a usable message."""
        if not (self.done or self.finish_seen):
            raise ProviderResponseError("Provider stream ended before completion")
        if not self.saw_content and not self._tool_indexes:
            raise ProviderResponseError("Provider message has neither content nor tool calls")
        if self._tool_indexes - self._named:
            raise ProviderResponseError("Tool call name must be nonempty text")
