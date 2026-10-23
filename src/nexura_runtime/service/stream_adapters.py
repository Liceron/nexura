# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Frontend wire adapters for already-public native SSE, never raw runner events.

Pins: AG-UI protocol 1.0 (@ag-ui/core 1.0.3); AI SDK 5.0.0 UIMessageChunk,
SSE wire version v1. See docs/streaming-to-frontends.md for scope and provenance.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import AsyncIterator
from typing import Any, Literal, cast

from fastapi import HTTPException, Query, Request
from fastapi.responses import StreamingResponse

StreamFormat = Literal["native", "ag-ui", "ai-sdk"]
AG_UI_PROTOCOL_VERSION = "1.0"
AI_SDK_VERSION = "5.0.0"
_MEDIA: dict[str, StreamFormat] = {
    "application/vnd.ag-ui+json": "ag-ui",
    "application/vnd.vercel.ai.ui-message-stream+json": "ai-sdk",
    "text/event-stream": "native",
}


def select_stream_format(
    request: Request,
    stream_format: str | None = Query(default=None),
) -> StreamFormat:
    """Query overrides Accept; ordinary SSE, wildcards and absent Accept stay native."""
    if stream_format is not None:
        if stream_format in ("native", "ag-ui", "ai-sdk"):
            return cast(StreamFormat, stream_format)
        raise HTTPException(400, detail={"code": "INVALID_STREAM_FORMAT"})
    choices: list[tuple[float, int, StreamFormat]] = []
    for index, item in enumerate(request.headers.get("accept", "").split(",")):
        media, *params = item.strip().lower().split(";")
        media = media.strip()
        if media not in _MEDIA:
            continue
        quality = 1.0
        for param in params:
            key, _, value = param.strip().partition("=")
            if key == "q":
                try:
                    quality = float(value)
                except ValueError:
                    quality = 0.0
        if 0 < quality <= 1:
            choices.append((quality, -index, _MEDIA[media]))
    if choices:
        return max(choices)[2]
    # Preserve native behavior for refused, malformed and unsupported Accept ranges.
    return "native"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class _Adapter:
    def __init__(self, format_: StreamFormat, thread_id: str, run_id: str, chat: bool) -> None:
        self.format = format_
        self.thread_id = thread_id
        self.run_id = run_id
        self.chat = chat
        self.started = False
        self.ended = False
        self.paused = False
        self.pause_notice: dict[str, Any] = {}
        self.counter = 0
        self.text: dict[str, str] = {}
        self.saw_text: set[str] = set()
        self.tools: dict[tuple[str, str], str] = {}
        self.has_state = False

    def event(
        self, ag: str, ai: str, ag_data: dict[str, Any], ai_data: dict[str, Any]
    ) -> dict[str, Any]:
        return {"type": ag, **ag_data} if self.format == "ag-ui" else {"type": ai, **ai_data}

    def custom(self, name: str, data: dict[str, Any]) -> dict[str, Any]:
        return self.event(
            "CUSTOM",
            f"data-nexura-{name}",
            {"name": f"nexura.{name}", "value": data},
            {"data": data, "transient": True},
        )

    def start(self) -> list[dict[str, Any]]:
        if self.started:
            return []
        self.started = True
        return [
            self.event(
                "RUN_STARTED",
                "start",
                {"threadId": self.thread_id, "runId": self.run_id},
                {"messageId": self.run_id},
            )
        ]

    def close_text(self, scope: str | None = None) -> list[dict[str, Any]]:
        out = []
        for key in list(self.text):
            if scope is None or scope == key:
                mid = self.text.pop(key)
                out.append(
                    self.event("TEXT_MESSAGE_END", "text-end", {"messageId": mid}, {"id": mid})
                )
        return out

    def content(self, scope: str, text: Any) -> list[dict[str, Any]]:
        if not isinstance(text, str) or not text:
            return []
        out = []
        if scope not in self.text:
            self.counter += 1
            mid = f"{self.run_id}:text:{self.counter}"
            self.text[scope] = mid
            out.append(
                self.event(
                    "TEXT_MESSAGE_START",
                    "text-start",
                    {"messageId": mid, "role": "assistant"},
                    {"id": mid},
                )
            )
        mid = self.text[scope]
        self.saw_text.add(scope)
        out.append(
            self.event(
                "TEXT_MESSAGE_CONTENT",
                "text-delta",
                {"messageId": mid, "delta": text},
                {"id": mid, "delta": text},
            )
        )
        return out

    def snapshot(self, value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, dict):
            return []
        self.has_state = True
        return [
            self.event(
                "STATE_SNAPSHOT",
                "data-nexura-state-snapshot",
                {"snapshot": value},
                {"data": value, "transient": True},
            )
        ]

    def finish(self, status: str, data: dict[str, Any]) -> list[dict[str, Any]]:
        if self.ended:
            return []
        out = self.close_text()
        self.ended = True
        if status == "failed":
            message = data.get("error", data.get("message", "Run failed"))
            out.append(
                self.event(
                    "RUN_ERROR",
                    "error",
                    {"message": str(message), "code": str(data.get("code", "RUN_FAILED"))},
                    {"errorText": str(message)},
                )
            )
        elif status == "cancelled":
            out.append(self.custom("cancelled", {}))
            if self.format == "ai-sdk":
                out.append({"type": "abort"})
        if self.format == "ag-ui" and status != "failed":
            terminal: dict[str, Any] = {
                "type": "RUN_FINISHED",
                "threadId": self.thread_id,
                "runId": self.run_id,
            }
            if status == "paused":
                terminal["result"] = {
                    "nexura": {
                        "paused": self.pause_notice.get("reason", "approval"),
                        **{k: v for k, v in self.pause_notice.items() if k != "reason"},
                    }
                }
            out.append(terminal)
        elif self.format == "ai-sdk":
            out.append({"type": "finish"})
        return out

    def feed(self, name: str, data: dict[str, Any]) -> list[dict[str, Any]]:
        if self.ended:
            return []
        out = self.start()
        scope = "root"
        child = False
        if name == "run_event":
            name, data = str(data.get("type", "")), data.get("data", {})
        elif name == "AGENT_EVENT":
            scope = str(data.get("agent_run_id") or data.get("node_id") or "agent")
            name, data = str(data.get("kind", "")), data.get("payload", {})
            child = True
        if not isinstance(data, dict):
            return out
        kind = name.upper()
        pause_kinds = {
            "RUN_PAUSED",
            "RUN_PENDING_APPROVAL",
            "RUN_WAITING_FOR_INPUT",
            "RUN_PAUSED_EFFECT_UNKNOWN",
        }
        if not child:
            self.paused = kind in pause_kinds and not self.chat
        if kind == "MODEL_STARTED":
            out += self.close_text(scope)
            self.saw_text.discard(scope)
        elif kind == "MODEL_COMPLETED":
            out += self.close_text(scope)
        elif kind == "TEXT_DELTA":
            out += self.content(scope, data.get("text"))
        elif kind == "FINAL_OUTPUT" and scope not in self.saw_text:
            out += self.content(scope, data.get("output"))
            out += self.close_text(scope)
        elif kind == "TOOL_STARTED":
            call_id, tool = data.get("tool_call_id"), data.get("tool_name")
            if isinstance(call_id, str) and isinstance(tool, str):
                # Scope identifiers: independent agents may reuse a provider call ID.
                key = (scope, call_id)
                if key not in self.tools:
                    self.counter += 1
                    tid = f"{self.run_id}:tool:{self.counter}"
                    self.tools[key] = tid
                    out += self.close_text(scope)
                    out.append(
                        self.event(
                            "TOOL_CALL_START",
                            "tool-input-start",
                            {"toolCallId": tid, "toolCallName": tool},
                            {"toolCallId": tid, "toolName": tool, "dynamic": True},
                        )
                    )
                    # No TOOL_CALL_DELTA: raw, unvalidated arguments can precede approval.
                    if "arguments" in data:
                        args = data["arguments"]
                        out.append(
                            self.event(
                                "TOOL_CALL_ARGS",
                                "tool-input-delta",
                                {"toolCallId": tid, "delta": _json(args)},
                                {"toolCallId": tid, "inputTextDelta": _json(args)},
                            )
                        )
                    out.append(
                        self.event(
                            "TOOL_CALL_END",
                            "tool-input-available",
                            {"toolCallId": tid},
                            {
                                "toolCallId": tid,
                                "toolName": tool,
                                "input": data.get("arguments", {}),
                                "dynamic": True,
                            },
                        )
                    )
        elif kind == "TOOL_COMPLETED":
            result_tid = self.tools.get((scope, str(data.get("tool_call_id"))))
            if result_tid is not None and "result" in data:
                result = data["result"]
                out.append(
                    self.event(
                        "TOOL_CALL_RESULT",
                        "tool-output-available",
                        {
                            "toolCallId": result_tid,
                            "messageId": f"{result_tid}:result",
                            "content": result if isinstance(result, str) else _json(result),
                            "role": "tool",
                        },
                        {"toolCallId": result_tid, "output": result, "dynamic": True},
                    )
                )
        elif kind in pause_kinds:
            out += self.close_text(scope)
            # Explicit allowlist: never forward pending calls, arguments or interruption.
            notice = {
                key: data[key]
                for key in ("approval_id", "interrupt_id", "effect_id", "node_id")
                if isinstance(data.get(key), str)
            }
            notice["reason"] = {
                "RUN_WAITING_FOR_INPUT": "input",
                "RUN_PAUSED_EFFECT_UNKNOWN": "effect_unknown",
            }.get(kind, "approval")
            out.append(
                self.custom("approval" if notice["reason"] == "approval" else "paused", notice)
            )
            if not child:
                self.pause_notice = notice
            # A replay can continue past an earlier pause. Only EOF at a root pause
            # ends this observation as paused; a later terminal event wins.
        elif kind == "RUN_STARTED":
            out += self.snapshot(data.get("initial_state"))
        elif kind == "NODE_STARTED":
            out += self.snapshot(data.get("input_state"))
        # NODE_COMPLETED carries raw writes, not reduced state. Only authoritative
        # initial/node-input/final snapshots can synchronize frontend state safely.
        elif kind == "RUN_COMPLETED" and not child:
            out += self.snapshot(data.get("final_state"))
            if not self.chat:
                out += self.finish("completed", data)
        elif kind in {"RUN_FAILED", "RUN_REJECTED", "CHAT_FAILED"} and not child:
            out += self.finish("failed", data)
        elif kind == "RUN_CANCELLED" and not child:
            out += self.finish("cancelled", data)
        elif kind == "CHAT_COMPLETED":
            if not self.saw_text:
                out += self.content(scope, data.get("reply"))
            out += self.finish("completed", data)
        elif kind == "RUN_STREAM_CLOSED":
            status = str(data.get("status", "closed"))
            if status in {"completed", "failed", "rejected", "cancelled"}:
                out += self.finish("failed" if status == "rejected" else status, {})
        # Internal/model metadata, custom payloads and unknown events are dropped.
        return out


async def _sse_frames(source: AsyncIterator[str]) -> AsyncIterator[dict[str, str]]:
    """Parse SSE physical lines using CR/LF only, including split CRLF boundaries."""
    buffer = ""
    fields: dict[str, str] = {}
    data: list[str] = []
    skip_lf = False

    def line(value: str) -> dict[str, str] | None:
        nonlocal fields, data
        if not value:
            frame = {**fields, "data": "\n".join(data)} if data else None
            fields, data = {}, []
            return frame
        key, sep, payload = value.partition(":")
        if payload.startswith(" "):
            payload = payload[1:]
        if sep and key == "data":
            data.append(payload)
        elif sep and key in {"event", "id"}:
            fields[key] = payload
        return None

    async for chunk in source:
        buffer += chunk
        while buffer:
            if skip_lf:
                if buffer.startswith("\n"):
                    buffer = buffer[1:]
                skip_lf = False
            match = re.search(r"\r\n|[\r\n]", buffer)
            if match is None:
                break
            skip_lf = match.group() == "\r"
            frame = line(buffer[: match.start()])
            buffer = buffer[match.end() :]
            if frame is not None:
                yield frame
    # An unterminated physical line/frame is not dispatched by SSE. The caller
    # emits an incomplete-stream error if no terminal event was observed.


async def adapt_stream(
    source: AsyncIterator[str],
    format_: StreamFormat,
    *,
    thread_id: str,
    run_id: str,
    chat: bool = False,
) -> AsyncIterator[str]:
    """Adapt only the frames visible to native clients; closing also closes execution."""
    adapter = _Adapter(format_, thread_id, run_id, chat)
    try:
        if format_ == "native":
            async for chunk in source:
                yield chunk
            return
        try:
            async for fields in _sse_frames(source):
                payload = json.loads(fields["data"])
                if not isinstance(payload, dict):
                    raise ValueError("Invalid public SSE payload")
                events = adapter.feed(fields.get("event", "message"), payload)
                for index, event in enumerate(events):
                    # Commit the native replay cursor only after the whole fan-out.
                    cursor = fields.get("id", "")
                    prefix = f"id: {cursor}\n" if cursor and index == len(events) - 1 else ""
                    yield f"{prefix}data: {_json(event)}\n\n"
                if adapter.ended:
                    if format_ == "ai-sdk":
                        yield "data: [DONE]\n\n"
                    return
        except Exception:
            # Never echo exception text: the public source may fail with secrets
            # in a persistence/provider/parser error that native clients never saw.
            for event in adapter.start() + adapter.finish(
                "failed", {"error": "The event stream failed.", "code": "STREAM_SOURCE_ERROR"}
            ):
                yield f"data: {_json(event)}\n\n"
            if format_ == "ai-sdk":
                yield "data: [DONE]\n\n"
            return
        if not adapter.ended:
            if adapter.paused:
                for event in adapter.finish("paused", {}):
                    yield f"data: {_json(event)}\n\n"
                if format_ == "ai-sdk":
                    yield "data: [DONE]\n\n"
                return
            for event in adapter.start():
                yield f"data: {_json(event)}\n\n"
            # A finite replay/poll timeout is not proof of run completion.
            yield f"data: {_json(adapter.custom('stream-closed', {'terminal': False}))}\n\n"
            for event in adapter.finish(
                "failed",
                {
                    "error": "The event stream ended before a terminal event.",
                    "code": "STREAM_INCOMPLETE",
                },
            ):
                yield f"data: {_json(event)}\n\n"
        if format_ == "ai-sdk":
            yield "data: [DONE]\n\n"
    finally:
        close = getattr(source, "aclose", None)
        if close is not None:
            try:
                await close()
            except Exception:
                if format_ == "native":
                    raise


def stream_response(
    source: AsyncIterator[str],
    format_: StreamFormat,
    *,
    thread_id: str,
    run_id: str | None = None,
    chat: bool = False,
    headers: dict[str, str] | None = None,
) -> StreamingResponse:
    """Keep native body bytes unchanged; add only negotiated transport headers."""
    response_headers = {**(headers or {}), "Vary": "Accept"}
    if format_ != "native":
        source = adapt_stream(
            source,
            format_,
            thread_id=thread_id,
            run_id=run_id or f"chat-{uuid.uuid4().hex}",
            chat=chat,
        )
        if format_ == "ai-sdk":
            response_headers["x-vercel-ai-ui-message-stream"] = "v1"
        else:
            response_headers["x-ag-ui-protocol-version"] = AG_UI_PROTOCOL_VERSION
    return StreamingResponse(source, media_type="text/event-stream", headers=response_headers)
