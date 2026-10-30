# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Strict pinned-wire key, pairing and lifecycle assertions for C3 goldens."""

from __future__ import annotations

import json
from typing import Any


def data_events(text: str) -> list[Any]:
    return [
        json.loads(line[6:]) if line[6:] != "[DONE]" else "[DONE]"
        for line in text.split("\n")
        if line.startswith("data: ")
    ]


def validate_stream(text: str, format_: str) -> None:
    if format_ == "native":
        return
    events = data_events(text)
    assert events
    if format_ == "ai-sdk":
        assert events[-1] == "[DONE]" and events.count("[DONE]") == 1
        events = events[:-1]
        assert events[0]["type"] == "start"
        assert events[-1]["type"] == "finish"
        assert sum(e["type"] == "start" for e in events) == 1
        assert sum(e["type"] == "finish" for e in events) == 1
    else:
        assert events[0]["type"] == "RUN_STARTED"
        assert events[-1]["type"] in {"RUN_FINISHED", "RUN_ERROR"}
        assert sum(e["type"] == "RUN_STARTED" for e in events) == 1
        assert sum(e["type"] in {"RUN_FINISHED", "RUN_ERROR"} for e in events) == 1
    schema = {
        "RUN_STARTED": ({"type", "threadId", "runId"}, set()),
        "RUN_FINISHED": ({"type", "threadId", "runId"}, {"result"}),
        "RUN_ERROR": ({"type", "message"}, {"code"}),
        "TEXT_MESSAGE_START": ({"type", "messageId", "role"}, set()),
        "TEXT_MESSAGE_CONTENT": ({"type", "messageId", "delta"}, set()),
        "TEXT_MESSAGE_END": ({"type", "messageId"}, set()),
        "TOOL_CALL_START": ({"type", "toolCallId", "toolCallName"}, set()),
        "TOOL_CALL_ARGS": ({"type", "toolCallId", "delta"}, set()),
        "TOOL_CALL_END": ({"type", "toolCallId"}, set()),
        "TOOL_CALL_RESULT": ({"type", "toolCallId", "messageId", "content"}, {"role"}),
        "STATE_SNAPSHOT": ({"type", "snapshot"}, set()),
        "CUSTOM": ({"type", "name", "value"}, set()),
        "start": ({"type"}, {"messageId", "messageMetadata"}),
        "finish": ({"type"}, {"messageMetadata"}),
        "error": ({"type", "errorText"}, set()),
        "abort": ({"type"}, set()),
        "text-start": ({"type", "id"}, {"providerMetadata"}),
        "text-delta": ({"type", "id", "delta"}, {"providerMetadata"}),
        "text-end": ({"type", "id"}, {"providerMetadata"}),
        "tool-input-start": ({"type", "toolCallId", "toolName"}, {"dynamic", "providerExecuted"}),
        "tool-input-delta": ({"type", "toolCallId", "inputTextDelta"}, set()),
        "tool-input-available": (
            {"type", "toolCallId", "toolName", "input"},
            {"dynamic", "providerExecuted", "providerMetadata"},
        ),
        "tool-output-available": (
            {"type", "toolCallId", "output"},
            {"dynamic", "providerExecuted"},
        ),
    }
    text_open: set[str] = set()
    text_seen: set[str] = set()
    calls: dict[str, str] = {}
    for event in events:
        kind = event["type"]
        required, optional = (
            schema[kind]
            if not kind.startswith("data-")
            else ({"type", "data"}, {"id", "transient"})
        )
        assert required <= event.keys() <= required | optional, event
        if kind in {"TEXT_MESSAGE_START", "text-start"}:
            mid = event.get("messageId", event.get("id"))
            assert mid not in text_seen
            text_seen.add(mid)
            text_open.add(mid)
        elif kind in {"TEXT_MESSAGE_CONTENT", "text-delta"}:
            assert event.get("messageId", event.get("id")) in text_open
            assert isinstance(event["delta"], str) and event["delta"]
        elif kind in {"TEXT_MESSAGE_END", "text-end"}:
            text_open.remove(event.get("messageId", event.get("id")))
        elif kind in {"TOOL_CALL_START", "tool-input-start"}:
            assert event["toolCallId"] not in calls
            calls[event["toolCallId"]] = "args"
        elif kind in {"TOOL_CALL_ARGS", "tool-input-delta"}:
            assert calls[event["toolCallId"]] == "args"
        elif kind in {"TOOL_CALL_END", "tool-input-available"}:
            assert calls[event["toolCallId"]] == "args"
            calls[event["toolCallId"]] = "ended"
        elif kind in {"TOOL_CALL_RESULT", "tool-output-available"}:
            assert calls[event["toolCallId"]] == "ended"
            calls[event["toolCallId"]] = "result"
    assert not text_open
    assert "args" not in calls.values()
