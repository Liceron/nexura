# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""OpenAI Responses API adapter (``POST /v1/responses``).

The Responses API is OpenAI's recommended API for new work (the Chat Completions reference
says so). This adapter is stateless: it sends ``store: false`` and the whole conversation as
input items every turn. Reasoning items a model returns next to its function calls are sent
back with the tool outputs, as the function-calling guide requires: from
``Message.provider_data`` for whole responses and from a per-provider record of recent calls
for streamed ones.

Wire format checked against developers.openai.com (Responses create reference, Responses
streaming events, function-calling guide), retrieved 2026-10-21.
"""

from __future__ import annotations

import json
from collections import OrderedDict
from collections.abc import AsyncIterator
from typing import Any

import httpx

from nexura_agents.contracts.context import ExecutionContext
from nexura_agents.errors import ProviderRequestError, ProviderResponseError
from nexura_agents.json_schema import to_openai_schema
from nexura_agents.limits import merge_extra, warn_unsupported
from nexura_agents.models._streaming import _error_from_chunk
from nexura_agents.providers.errors import (
    CapabilityError,
    ProviderContentFilterError,
    _mask_secrets,
    read_error_body,
)
from nexura_agents.providers.multimodal import openai_content_parts
from nexura_agents.providers.openai.provider import OpenAIProvider
from nexura_agents.providers.types import (
    GenerationRequest,
    ProviderMessage,
    StreamDelta,
)
from nexura_agents.types import Message, ModelResponse, ToolCall, Usage

# Message.provider_data key: the raw output items of the response that produced the message
# (reasoning items with encrypted_content, function calls with their ids).
OPENAI_OUTPUT_ITEMS_KEY = "openai_output_items"

_ITEM_CACHE_SIZE = 512


def responses_usage(u: dict[str, Any]) -> Usage:
    """Responses ``usage`` as a Nexura ``Usage``.

    ``input_tokens`` includes the cached and cache-written tokens; ``output_tokens`` includes
    the reasoning tokens. Both breakdowns are reported, never added on top.
    """

    def num(container: Any, key: str) -> int | None:
        value = container.get(key) if isinstance(container, dict) else None
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    in_details = u.get("input_tokens_details")
    out_details = u.get("output_tokens_details")
    cached = num(in_details, "cached_tokens")
    return Usage(
        prompt_tokens=num(u, "input_tokens"),
        completion_tokens=num(u, "output_tokens"),
        total_tokens=num(u, "total_tokens"),
        reasoning_tokens=num(out_details, "reasoning_tokens"),
        cached_prompt_tokens=cached,
        cache_read_input_tokens=cached,
        cache_creation_input_tokens=num(in_details, "cache_write_tokens"),
    )


def _input_content(msg: Message) -> Any:
    """A message's content as Responses input parts (plain text stays a string)."""
    if not msg.attachments:
        return msg.content or ""
    parts: list[dict[str, Any]] = []
    for part in openai_content_parts(msg.content, msg.attachments):
        kind = part.get("type")
        if kind == "text":
            parts.append({"type": "input_text", "text": part["text"]})
        elif kind == "image_url":
            parts.append({"type": "input_image", "image_url": part["image_url"]["url"]})
        elif kind == "file":
            parts.append({"type": "input_file", **part["file"]})
        else:
            raise CapabilityError(
                f"The OpenAI Responses adapter does not send '{kind}' attachments.",
                provider_name="openai-responses",
                capability="multimodal",
            )
    return parts


def _tool_definition(tool: dict[str, Any]) -> dict[str, Any]:
    """A chat-completions style tool as a Responses function tool.

    ``strict`` is sent as ``False`` unless the tool sets it: when it is omitted the API tries
    to turn the schema strict, which would make optional parameters required.
    """
    if tool.get("type") not in (None, "function") or "function" not in tool:
        return dict(tool)  # already in Responses form, or a built-in tool
    fn = tool["function"]
    out: dict[str, Any] = {
        "type": "function",
        "name": fn.get("name"),
        "parameters": fn.get("parameters", {"type": "object", "properties": {}}),
        "strict": bool(fn.get("strict", False)),
    }
    if fn.get("description"):
        out["description"] = fn["description"]
    return out


def _tool_choice(choice: str) -> Any:
    if choice in ("auto", "required", "none"):
        return choice
    return {"type": "function", "name": choice}


class OpenAIResponsesProvider(OpenAIProvider):
    """OpenAI Responses API (``/v1/responses``): text, function calls (also parallel),
    structured output (``text.format``), streaming, reasoning items and usage.

    Same constructor as :class:`OpenAIProvider` (key, ``base_url``, transport, ...). Set
    ``store=True`` to let OpenAI keep responses; the adapter still sends the full history.
    Embeddings use ``/embeddings`` exactly like :class:`OpenAIProvider`.
    """

    supports_response_schema = True
    streams_response_schema = True
    response_schema_with_tools = True

    def __init__(self, model: str, *, store: bool = False, **kwargs: Any) -> None:
        super().__init__(model, **kwargs)
        self.store = store
        # Raw output items of recent responses by function call_id, for streamed turns
        # (their Message cannot carry provider_data).
        self._items_by_call: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()

    def __repr__(self) -> str:
        return super().__repr__().replace("OpenAIProvider(", "OpenAIResponsesProvider(", 1)

    # ------------------------------------------------------------------ request

    def _remember_items(self, items: list[dict[str, Any]]) -> None:
        for item in items:
            if item.get("type") == "function_call" and isinstance(item.get("call_id"), str):
                self._items_by_call[item["call_id"]] = items
                self._items_by_call.move_to_end(item["call_id"])
        while len(self._items_by_call) > _ITEM_CACHE_SIZE:
            self._items_by_call.popitem(last=False)

    def _assistant_items(self, msg: Message) -> list[dict[str, Any]]:
        stored = (msg.provider_data or {}).get(OPENAI_OUTPUT_ITEMS_KEY)
        if not stored and msg.tool_calls:
            stored = self._items_by_call.get(msg.tool_calls[0].id)
        call_ids = {tc.id for tc in msg.tool_calls or []}
        if isinstance(stored, list) and stored:
            stored_ids = {i.get("call_id") for i in stored if i.get("type") == "function_call"}
            if stored_ids == call_ids:
                # Send the response's own reasoning and call items back unchanged.
                reasoning = [self._input_item(i) for i in stored if i.get("type") == "reasoning"]
                stored_calls = [i for i in stored if i.get("type") == "function_call"]
                text = [self._assistant_message(msg.content)] if msg.content else []
                return reasoning + text + [self._input_item(i) for i in stored_calls]
        items: list[dict[str, Any]] = []
        if msg.content:
            items.append(self._assistant_message(msg.content))
        for tc in msg.tool_calls or []:
            items.append(
                {
                    "type": "function_call",
                    "call_id": tc.id,
                    "name": tc.name,
                    "arguments": tc.raw_arguments
                    if tc.raw_arguments is not None
                    else json.dumps(tc.arguments, ensure_ascii=False),
                }
            )
        return items

    @staticmethod
    def _assistant_message(text: str) -> dict[str, Any]:
        return {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text}],
        }

    @staticmethod
    def _input_item(item: dict[str, Any]) -> dict[str, Any]:
        """An output item as an input item (status is output-only for reasoning)."""
        out = dict(item)
        if out.get("type") == "reasoning":
            out.pop("status", None)
        return out

    def _build_payload(self, request: GenerationRequest, *, stream: bool) -> dict[str, Any]:
        instructions: list[str] = []
        items: list[dict[str, Any]] = []
        for raw in request.messages:
            msg = raw.to_legacy() if isinstance(raw, ProviderMessage) else raw
            if msg.role == "system":
                if msg.content:
                    instructions.append(msg.content)
            elif msg.role == "user":
                items.append({"type": "message", "role": "user", "content": _input_content(msg)})
            elif msg.role == "assistant":
                items.extend(self._assistant_items(msg))
            elif msg.role == "tool":
                items.append(
                    {
                        "type": "function_call_output",
                        "call_id": msg.tool_call_id,
                        "output": msg.content or "",
                    }
                )
        payload: dict[str, Any] = {"model": self.model, "input": items, "store": self.store}
        if instructions:
            payload["instructions"] = "\n\n".join(instructions)
        if stream:
            payload["stream"] = True
        if request.tools:
            payload["tools"] = [_tool_definition(t) for t in request.tools]
            if request.tool_choice is not None:
                payload["tool_choice"] = _tool_choice(request.tool_choice)
        if request.schema and self.capabilities.can_structured_output:
            schema, strict = to_openai_schema(request.schema)
            payload["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": "output_schema",
                    "schema": schema,
                    "strict": strict,
                }
            }
        if request.max_tokens:
            payload["max_output_tokens"] = request.max_tokens
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.top_p is not None:
            payload["top_p"] = request.top_p
        if request.reasoning_effort is not None:
            payload["reasoning"] = {"effort": request.reasoning_effort}
        warn_unsupported(
            "The OpenAI Responses API",
            request,
            (
                "stop_sequences",
                "seed",
                "presence_penalty",
                "frequency_penalty",
                "thinking_budget_tokens",
            ),
        )
        if request.extra:
            merge_extra(payload, request.extra)
        return payload

    # ----------------------------------------------------------------- response

    def _parse_response(self, data: dict[str, Any]) -> ModelResponse:
        status = data.get("status")
        if status == "failed":
            raise self._failed_error(data.get("error"))
        output = data.get("output")
        if not isinstance(output, list):
            raise ProviderResponseError("Responses API reply has no output list")
        texts: list[str] = []
        refusals: list[str] = []
        calls: list[ToolCall] = []
        seen: set[str] = set()
        for item in output:
            if not isinstance(item, dict):
                raise ProviderResponseError("Malformed output item in Responses API reply")
            kind = item.get("type")
            if kind == "message":
                for part in item.get("content") or []:
                    if part.get("type") == "output_text":
                        texts.append(part.get("text", ""))
                    elif part.get("type") == "refusal":
                        refusals.append(part.get("refusal", ""))
            elif kind == "function_call":
                call_id = item.get("call_id")
                if not isinstance(call_id, str) or not call_id or call_id in seen:
                    raise ProviderResponseError("Function call ids must be nonempty and unique")
                seen.add(call_id)
                raw = item.get("arguments") or "{}"
                try:
                    args = json.loads(raw) if isinstance(raw, str) else raw
                    if not isinstance(args, dict):
                        raise ValueError("arguments must decode to an object")
                except Exception as exc:
                    raise ProviderResponseError(
                        "Malformed function call arguments", original_error=exc
                    ) from exc
                calls.append(
                    ToolCall(
                        id=call_id,
                        name=str(item.get("name") or ""),
                        arguments=args,
                        raw_arguments=raw if isinstance(raw, str) else json.dumps(raw),
                    )
                )
        reason = (data.get("incomplete_details") or {}).get("reason")
        finish = self._finish_reason(status, reason, bool(calls))
        content: str | None = "".join(texts) if texts else None
        if refusals:
            content = "Model Refusal: " + "".join(refusals)
            finish = "refusal"
        if content is None and not calls:
            if reason == "content_filter":
                raise ProviderContentFilterError(
                    "The OpenAI content filter withheld the answer "
                    "(incomplete_details.reason: content_filter)",
                    reason="content_filter",
                )
            if status != "incomplete":
                raise ProviderResponseError("Responses API reply has neither text nor calls")
        keep = [i for i in output if i.get("type") in ("reasoning", "function_call")]
        if calls:
            self._remember_items(keep)
        usage = responses_usage(data["usage"]) if isinstance(data.get("usage"), dict) else None
        return ModelResponse(
            message=Message(
                role="assistant",
                content=content,
                tool_calls=calls or None,
                provider_data={OPENAI_OUTPUT_ITEMS_KEY: keep} if calls and keep else None,
            ),
            usage=usage,
            finish_reason=finish,
        )

    @staticmethod
    def _finish_reason(status: Any, reason: Any, has_calls: bool) -> str:
        if status == "incomplete":
            if reason == "max_output_tokens":
                return "length"
            if reason == "content_filter":
                return "content_filter"
            return str(reason or "incomplete")
        return "tool_calls" if has_calls else "stop"

    def _failed_error(self, error: Any) -> BaseException:
        err = error if isinstance(error, dict) else {}
        return _error_from_chunk(
            {
                "code": err.get("code"),
                "message": f"OpenAI response failed: {err.get('message') or 'no message'}",
            },
            self._redact,
        )

    def _redact(self, text: str) -> str:
        if self._api_key:
            text = text.replace(self._api_key, "[REDACTED_SECRET]")
        return _mask_secrets(text)

    # --------------------------------------------------------------- transport

    async def _generate_internal(
        self,
        request: GenerationRequest,
        context: ExecutionContext | None,
    ) -> ModelResponse:
        payload = self._build_payload(request, stream=False)
        request_timeout = request.timeout or self.timeout
        try:
            response = await self._get_client().post(
                f"{self.base_url}/responses",
                json=payload,
                headers=self._build_headers(),
                timeout=request_timeout,
            )
        except httpx.TimeoutException as exc:
            raise ProviderRequestError(
                f"Request to {self.base_url} timed out ({request_timeout}s)", original_error=exc
            ) from exc
        except httpx.RequestError as exc:
            raise ProviderRequestError(
                f"HTTP network error contacting provider: {exc}", original_error=exc
            ) from exc
        self._handle_error_response(response)
        try:
            data = response.json()
        except Exception as exc:
            raise ProviderResponseError(
                "Failed to parse provider response as JSON", original_error=exc
            ) from exc
        if not isinstance(data, dict):
            raise ProviderResponseError("Responses API reply must be a JSON object")
        return self._parse_response(data)

    def _stream_internal(
        self,
        request: GenerationRequest,
        context: ExecutionContext | None,
    ) -> AsyncIterator[StreamDelta]:
        payload = self._build_payload(request, stream=True)
        headers = self._build_headers()
        headers["Accept"] = "text/event-stream"
        request_timeout = request.timeout or self.timeout
        client = self._get_client()
        url = f"{self.base_url}/responses"

        async def _generator() -> AsyncIterator[StreamDelta]:
            index_of_item: dict[str, int] = {}
            args_seen: set[int] = set()
            refusal: list[str] = []
            terminal = False
            try:
                async with client.stream(
                    "POST", url, json=payload, headers=headers, timeout=request_timeout
                ) as response:
                    await read_error_body(response)
                    self._handle_error_response(response)
                    async for line in response.aiter_lines():
                        line = line.strip()
                        if not line.startswith("data:"):
                            continue  # "event:" names repeat the data "type"; comments, blanks
                        data_str = line[5:].strip()
                        if not data_str or data_str == "[DONE]":
                            continue
                        try:
                            event = json.loads(data_str)
                        except json.JSONDecodeError as exc:
                            raise ProviderResponseError(
                                "Malformed SSE JSON event", original_error=exc
                            ) from exc
                        if not isinstance(event, dict):
                            raise ProviderResponseError("Malformed stream event")
                        kind = event.get("type")
                        if kind == "error":
                            raise _error_from_chunk(
                                {"code": event.get("code"), "message": event.get("message")},
                                self._redact,
                            )
                        if kind == "response.output_text.delta":
                            yield StreamDelta(kind="text_delta", text=event.get("delta", ""))
                        elif kind == "response.refusal.delta":
                            refusal.append(event.get("delta", ""))
                        elif kind == "response.output_item.added":
                            item = event.get("item") or {}
                            if item.get("type") == "function_call":
                                index = int(event.get("output_index", 0))
                                if isinstance(item.get("id"), str):
                                    index_of_item[item["id"]] = index
                                yield StreamDelta(
                                    kind="tool_call_delta",
                                    tool_call_index=index,
                                    tool_call_id=item.get("call_id"),
                                    tool_name=item.get("name"),
                                    tool_arguments_delta=item.get("arguments") or None,
                                )
                                if item.get("arguments"):
                                    args_seen.add(index)
                        elif kind == "response.function_call_arguments.delta":
                            raw_index = event.get("output_index")
                            index = (
                                raw_index
                                if isinstance(raw_index, int)
                                else index_of_item.get(event.get("item_id", ""), 0)
                            )
                            args_seen.add(index)
                            yield StreamDelta(
                                kind="tool_call_delta",
                                tool_call_index=index,
                                tool_arguments_delta=event.get("delta", ""),
                            )
                        elif kind == "response.output_item.done":
                            item = event.get("item") or {}
                            index = int(event.get("output_index", 0))
                            if item.get("type") == "function_call" and index not in args_seen:
                                # A server that sends the arguments only in the final item.
                                args_seen.add(index)
                                yield StreamDelta(
                                    kind="tool_call_delta",
                                    tool_call_index=index,
                                    tool_call_id=item.get("call_id"),
                                    tool_name=item.get("name"),
                                    tool_arguments_delta=item.get("arguments") or "{}",
                                )
                        elif kind in ("response.completed", "response.incomplete"):
                            terminal = True
                            body = event.get("response") or {}
                            output = [i for i in body.get("output") or [] if isinstance(i, dict)]
                            has_calls = any(i.get("type") == "function_call" for i in output)
                            if has_calls:
                                self._remember_items(
                                    [
                                        i
                                        for i in output
                                        if i.get("type") in ("reasoning", "function_call")
                                    ]
                                )
                            reason = (body.get("incomplete_details") or {}).get("reason")
                            usage = body.get("usage")
                            if refusal:
                                yield StreamDelta(
                                    kind="finish", finish_reason="refusal", text="".join(refusal)
                                )
                            yield StreamDelta(
                                kind="finish",
                                finish_reason=self._finish_reason(
                                    body.get("status"), reason, has_calls
                                ),
                                usage=responses_usage(usage) if isinstance(usage, dict) else None,
                            )
                        elif kind == "response.failed":
                            body = event.get("response") or {}
                            raise self._failed_error(body.get("error"))
                    if not terminal:
                        raise ProviderResponseError("Provider stream ended before completion")
            except httpx.TimeoutException as exc:
                raise ProviderRequestError(
                    f"Streaming from {self.base_url} timed out", original_error=exc
                ) from exc
            except httpx.RequestError as exc:
                raise ProviderRequestError(
                    f"HTTP network error in stream: {exc}", original_error=exc
                ) from exc

        return _generator()
