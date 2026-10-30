# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""P38 C3 wire goldens. Every model is an explicitly local FakeModel; no paid calls."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from nexura_agents import Agent, CancelToken, FakeModel, Runner, ToolCall, tool
from nexura_agents.session import SQLiteSessionStore
from p38_c3_protocol_checks import validate_stream
from starlette.requests import Request

from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.service.api.sessions import (
    ChatRequest,
    _chat_event_stream,
    _public_event_data,
    _sse,
)
from nexura_runtime.service.stream_adapters import (
    StreamFormat,
    adapt_stream,
    select_stream_format,
    stream_response,
)
from nexura_runtime.studio.store import StudioStore
from nexura_runtime.workflow import ModuleRegistry, WorkflowNode, WorkflowSpec

FORMATS: list[StreamFormat] = ["native", "ag-ui", "ai-sdk"]
SECRET = "hidden-approval-arguments-731946"


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    async def refuse(self: Any, request: httpx.Request) -> httpx.Response:
        raise AssertionError("Wire tests must never call a real provider")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", refuse)


def frames(text: str) -> list[tuple[str, Any]]:
    out = []
    for frame in text.strip().split("\n\n"):
        fields = dict(line.split(": ", 1) for line in frame.split("\n") if ": " in line)
        if "data" in fields:
            data = fields["data"]
            out.append(
                (fields.get("event", "message"), data if data == "[DONE]" else json.loads(data))
            )
    return out


async def collect(source: AsyncIterator[str], format_: StreamFormat, *, chat: bool = True) -> str:
    if format_ != "native":
        source = adapt_stream(source, format_, thread_id="session", run_id="run", chat=chat)
    text = "".join([chunk async for chunk in source])
    validate_stream(text, format_)
    return text


def types(text: str, format_: StreamFormat) -> list[str]:
    validate_stream(text, format_)
    return [
        "[DONE]"
        if data == "[DONE]"
        else (data.get("type", name) if format_ != "native" or name == "run_event" else name)
        for name, data in frames(text)
    ]


@tool
def lookup(q: str) -> str:
    """Offline lookup fixture."""
    return "found " + q


@tool(requires_approval=True)
def write(q: str) -> str:
    """A forbidden write: the approval/refusal tests must never execute it."""
    raise AssertionError("Unapproved write executed")


@pytest.mark.parametrize("format_", FORMATS)
async def test_fake_chat_tool_golden(tmp_path: Path, format_: StreamFormat) -> None:
    model = FakeModel(
        [[ToolCall(id="c1", name="lookup", arguments={"q": "x"})], "Done"],
        stream=True,
        chunk_size=2,
    )
    agent = Agent(name="Local FakeModel", model=model, instructions="fixture", tools=[lookup])
    store = SQLiteSessionStore(tmp_path / "chat.sqlite3")
    try:
        text = await collect(
            _chat_event_stream(store, "s", ChatRequest(message="go"), model=model, agent=agent),
            format_,
        )
        events = [data for _, data in frames(text)]
        if format_ == "native":
            assert types(text, format_) == [
                "RUN_STARTED",
                "AGENT_STARTED",
                "MODEL_STARTED",
                *(["TOOL_CALL_DELTA"] * 5),
                "MODEL_COMPLETED",
                "TOOL_STARTED",
                "TOOL_COMPLETED",
                "MODEL_STARTED",
                "TEXT_DELTA",
                "TEXT_DELTA",
                "MODEL_COMPLETED",
                "FINAL_OUTPUT",
                "RUN_COMPLETED",
                "chat_completed",
            ]
            assert events[-1]["reply"] == "Done"
            assert events[-1]["tool_executions"][0]["output"] == "found x"
        elif format_ == "ag-ui":
            assert events == [
                {"type": "RUN_STARTED", "threadId": "session", "runId": "run"},
                {"type": "TOOL_CALL_START", "toolCallId": "run:tool:1", "toolCallName": "lookup"},
                {"type": "TOOL_CALL_ARGS", "toolCallId": "run:tool:1", "delta": '{"q":"x"}'},
                {"type": "TOOL_CALL_END", "toolCallId": "run:tool:1"},
                {
                    "type": "TOOL_CALL_RESULT",
                    "toolCallId": "run:tool:1",
                    "messageId": "run:tool:1:result",
                    "content": "found x",
                    "role": "tool",
                },
                {"type": "TEXT_MESSAGE_START", "messageId": "run:text:2", "role": "assistant"},
                {"type": "TEXT_MESSAGE_CONTENT", "messageId": "run:text:2", "delta": "Do"},
                {"type": "TEXT_MESSAGE_CONTENT", "messageId": "run:text:2", "delta": "ne"},
                {"type": "TEXT_MESSAGE_END", "messageId": "run:text:2"},
                {"type": "RUN_FINISHED", "threadId": "session", "runId": "run"},
            ]
        else:
            assert events == [
                {"type": "start", "messageId": "run"},
                {
                    "type": "tool-input-start",
                    "toolCallId": "run:tool:1",
                    "toolName": "lookup",
                    "dynamic": True,
                },
                {
                    "type": "tool-input-delta",
                    "toolCallId": "run:tool:1",
                    "inputTextDelta": '{"q":"x"}',
                },
                {
                    "type": "tool-input-available",
                    "toolCallId": "run:tool:1",
                    "toolName": "lookup",
                    "input": {"q": "x"},
                    "dynamic": True,
                },
                {
                    "type": "tool-output-available",
                    "toolCallId": "run:tool:1",
                    "output": "found x",
                    "dynamic": True,
                },
                {"type": "text-start", "id": "run:text:2"},
                {"type": "text-delta", "id": "run:text:2", "delta": "Do"},
                {"type": "text-delta", "id": "run:text:2", "delta": "ne"},
                {"type": "text-end", "id": "run:text:2"},
                {"type": "finish"},
                "[DONE]",
            ]
        assert len(model.calls) == 2
        assert (await store.get("s")) is not None
    finally:
        await store.close()


async def public_runner(agent: Agent, **kwargs: Any) -> AsyncIterator[str]:
    """Same public envelope as chat, without chat's automatic approval refusal loop."""
    async for event in Runner().stream_async(agent, "go", stream_text=True, **kwargs):
        yield _sse(
            1,
            "run_event",
            {
                "type": event.type.value.upper(),
                "data": _public_event_data(event, scrub_errors=True),
            },
        )


@pytest.mark.parametrize("format_", FORMATS)
async def test_fake_approval_pause_golden(format_: StreamFormat) -> None:
    model = FakeModel([[ToolCall(id="c1", name="write", arguments={"q": SECRET})]], stream=True)
    agent = Agent(name="Local approval fixture", instructions="fixture", model=model, tools=[write])
    text = await collect(public_runner(agent), format_, chat=False)
    if format_ == "native":
        assert types(text, format_)[-1] == "RUN_PAUSED"
        assert "interruption" not in frames(text)[-1][1]["data"]
    else:
        assert SECRET not in text
        assert "arguments" not in text
        if format_ == "ag-ui":
            assert [data for _, data in frames(text)] == [
                {"type": "RUN_STARTED", "threadId": "session", "runId": "run"},
                {"type": "CUSTOM", "name": "nexura.approval", "value": {"reason": "approval"}},
                {
                    "type": "RUN_FINISHED",
                    "threadId": "session",
                    "runId": "run",
                    "result": {"nexura": {"paused": "approval"}},
                },
            ]
        else:
            assert [data for _, data in frames(text)] == [
                {"type": "start", "messageId": "run"},
                {"type": "data-nexura-approval", "data": {"reason": "approval"}, "transient": True},
                {"type": "finish"},
                "[DONE]",
            ]


@pytest.mark.parametrize("format_", FORMATS)
async def test_chat_approval_refusal_continues(tmp_path: Path, format_: StreamFormat) -> None:
    model = FakeModel(
        [[ToolCall(id="c1", name="write", arguments={"q": "x"})], "Refused"], stream=True
    )
    agent = Agent(name="Local refusal fixture", instructions="fixture", model=model, tools=[write])
    store = SQLiteSessionStore(tmp_path / "chat.sqlite3")
    try:
        text = await collect(
            _chat_event_stream(store, "s", ChatRequest(message="go"), model=model, agent=agent),
            format_,
        )
        assert len(model.calls) == 2
        if format_ == "native":
            assert "RUN_PAUSED" in types(text, format_)
            assert types(text, format_)[-1] == "chat_completed"
        else:
            assert types(text, format_).count("RUN_STARTED" if format_ == "ag-ui" else "start") == 1
            assert (
                types(text, format_).count("RUN_FINISHED" if format_ == "ag-ui" else "finish") == 1
            )
            assert "nexura.approval" in text or "data-nexura-approval" in text
            assert "Refused" in text
    finally:
        await store.close()


@pytest.mark.parametrize("format_", FORMATS)
async def test_fake_chat_error_golden(tmp_path: Path, format_: StreamFormat) -> None:
    def fail(messages: Any, tools: Any) -> str:
        raise RuntimeError(SECRET)

    model = FakeModel(handler=fail, stream=True)
    store = SQLiteSessionStore(tmp_path / "chat.sqlite3")
    try:
        text = await collect(
            _chat_event_stream(store, "s", ChatRequest(message="go"), model=model), format_
        )
        assert SECRET not in text
        if format_ == "native":
            assert types(text, format_)[-2:] == ["RUN_FAILED", "chat_failed"]
        elif format_ == "ag-ui":
            assert types(text, format_) == ["RUN_STARTED", "RUN_ERROR"]
            assert frames(text)[-1][1] == {
                "type": "RUN_ERROR",
                "message": "The model provider returned an error.",
                "code": "MODEL_PROVIDER_ERROR",
            }
        else:
            assert types(text, format_) == ["start", "error", "finish", "[DONE]"]
            assert frames(text)[1][1] == {
                "type": "error",
                "errorText": "The model provider returned an error.",
            }
        assert await store.get("s") is None
    finally:
        await store.close()


@pytest.mark.parametrize("format_", FORMATS)
async def test_fake_cancel_golden(format_: StreamFormat) -> None:
    token = CancelToken()
    token.cancel(SECRET)
    model = FakeModel(["must not run"])
    agent = Agent(name="Local cancellation fixture", instructions="fixture", model=model)
    text = await collect(public_runner(agent, cancel=token), format_)
    assert model.calls == []
    if format_ == "native":
        assert types(text, format_)[-1] == "RUN_CANCELLED"
    elif format_ == "ag-ui":
        assert types(text, format_) == ["RUN_STARTED", "CUSTOM", "RUN_FINISHED"]
        assert SECRET not in text
    else:
        assert types(text, format_) == [
            "start",
            "data-nexura-cancelled",
            "abort",
            "finish",
            "[DONE]",
        ]
        assert SECRET not in text


@pytest.mark.parametrize("format_", FORMATS)
async def test_chat_http_negotiation(tmp_path: Path, format_: StreamFormat) -> None:
    app = create_api_app(
        db_path=tmp_path / "app.sqlite3",
        chat_models={"local": FakeModel(["hello"], repeat_last=True, stream=True)},
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        media = {
            "native": "text/event-stream",
            "ag-ui": "application/vnd.ag-ui+json",
            "ai-sdk": "application/vnd.vercel.ai.ui-message-stream+json",
        }[format_]
        for suffix, headers in [(f"?stream_format={format_}", {}), ("", {"Accept": media})]:
            response = await client.post(
                "/api/v1/sessions/s/chat/stream" + suffix,
                json={"message": "hi", "model": "local"},
                headers=headers,
            )
            assert response.status_code == 200, response.text
            assert response.headers["content-type"].startswith("text/event-stream")
            assert response.headers["vary"] == "Accept"
            assert response.headers.get("x-vercel-ai-ui-message-stream") == (
                "v1" if format_ == "ai-sdk" else None
            )
            assert response.headers.get("x-ag-ui-protocol-version") == (
                "1.0" if format_ == "ag-ui" else None
            )
            if format_ == "native":
                assert frames(response.text)[0][0] == "run_event"
                assert frames(response.text)[-1][0] == "chat_completed"
            else:
                assert types(response.text, format_)[0] == (
                    "RUN_STARTED" if format_ == "ag-ui" else "start"
                )
        invalid = await client.post(
            "/api/v1/sessions/s/chat/stream?stream_format=invalid", json={"message": "hi"}
        )
        assert invalid.status_code == 400


@pytest.mark.parametrize(
    ("accept", "query", "expected"),
    [
        ("", None, "native"),
        ("*/*", None, "native"),
        ("text/event-stream", None, "native"),
        ("application/vnd.ag-ui+json", None, "ag-ui"),
        (
            "application/vnd.ag-ui+json;q=0.4, "
            "application/vnd.vercel.ai.ui-message-stream+json;q=0.8",
            None,
            "ai-sdk",
        ),
        ("application/vnd.ag-ui+json;q=0, text/event-stream;q=0.5", None, "native"),
        ("application/vnd.ag-ui+json", "native", "native"),
        ("text/event-stream", "ai-sdk", "ai-sdk"),
    ],
)
def test_negotiation(accept: str, query: str | None, expected: str) -> None:
    request = Request({"type": "http", "headers": [(b"accept", accept.encode())]})
    assert select_stream_format(request, query) == expected


@pytest.mark.parametrize("accept", ["application/vnd.ag-ui+json;q=0", "text/event-stream;q=bad"])
def test_unacceptable(accept: str) -> None:
    assert (
        select_stream_format(
            Request({"type": "http", "headers": [(b"accept", accept.encode())]}), None
        )
        == "native"
    )


async def sequence(*events: tuple[str, dict[str, Any]]) -> AsyncIterator[str]:
    for seq, (name, data) in enumerate(events, 1):
        yield _sse(seq, name, data)


@pytest.mark.parametrize("format_", ["ag-ui", "ai-sdk"])
async def test_drop_internal_and_hidden_approval_data(format_: StreamFormat) -> None:
    text = await collect(
        sequence(
            ("run_event", {"type": "TOOL_CALL_DELTA", "data": {"arguments_delta": SECRET}}),
            ("INTERNAL_AUTH", {"token": SECRET}),
            ("AGENT_EVENT", {"kind": "custom", "payload": {"data": SECRET}}),
            (
                "RUN_PENDING_APPROVAL",
                {
                    "approval_id": "a1",
                    "arguments": SECRET,
                    "interruption": {"messages": SECRET},
                    "pending": [{"arguments": SECRET}],
                },
            ),
        ),
        format_,
        chat=False,
    )
    assert SECRET not in text
    assert "a1" in text
    assert "TOOL_CALL" not in text and "tool-input" not in text


@pytest.mark.parametrize("format_", FORMATS)
async def test_native_bytes_and_disconnect_cleanup(format_: StreamFormat) -> None:
    closed = []

    if format_ == "native":
        original = _sse(1, "opaque_event", {"visible": "unchanged"})
        assert (
            "".join(
                [
                    chunk
                    async for chunk in adapt_stream(
                        sequence(("opaque_event", {"visible": "unchanged"})),
                        format_,
                        thread_id="s",
                        run_id="r",
                    )
                ]
            )
            == original
        )

    async def source() -> AsyncIterator[str]:
        try:
            yield _sse(1, "run_event", {"type": "TEXT_DELTA", "data": {"text": "hello"}})
            raise AssertionError("Disconnect must not advance execution")
        finally:
            closed.append(True)

    response = stream_response(source(), format_, thread_id="s", run_id="r")
    iterator = aiter(response.body_iterator)
    first = await anext(iterator)
    if format_ == "native":
        assert first == _sse(1, "run_event", {"type": "TEXT_DELTA", "data": {"text": "hello"}})
    await iterator.aclose()  # type: ignore[attr-defined]
    assert closed == [True]


@pytest.mark.parametrize("format_", ["ag-ui", "ai-sdk"])
async def test_partial_replay_is_not_success(format_: StreamFormat) -> None:
    text = await collect(
        sequence(("NODE_COMPLETED", {"state_update": {"a/b~c": 1}})), format_, chat=False
    )
    assert "STATE_DELTA" not in text and "data-nexura-state-delta" not in text
    assert "RUN_FINISHED" not in text
    assert types(text, format_)[-1] == ("RUN_ERROR" if format_ == "ag-ui" else "[DONE]")
    assert "STREAM_INCOMPLETE" in text if format_ == "ag-ui" else '"type":"error"' in text
    assert "stream-closed" in text


@pytest.mark.parametrize("format_", FORMATS)
async def test_workflow_route_redaction_and_replay(tmp_path: Path, format_: StreamFormat) -> None:
    registry = ModuleRegistry()

    async def node(state: dict[str, Any]) -> dict[str, Any]:
        return {"a/b~c": 2, "password": SECRET}

    registry.register("local_fixture", node)
    spec = WorkflowSpec(
        name="Local fixture",
        entry_node="n",
        nodes={"n": WorkflowNode(id="n", handler="local_fixture")},
        terminal_nodes=["n"],
    )
    store = StudioStore(tmp_path / "runs.sqlite3")
    app = create_api_app(store=store, registry=registry)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            started = await client.post(
                "/api/v1/runs",
                json={"spec": spec.to_dict(), "initial_state": {"x": 1, "api_key": SECRET}},
            )
            assert started.status_code == 201, started.text
            run_id = started.json()["run_id"]
            response = await client.get(
                f"/api/v1/runs/{run_id}/events", params={"stream_format": format_, "live": True}
            )
            assert response.status_code == 200, response.text
            assert SECRET not in response.text
            expected = {
                "native": [
                    "RUN_STARTED",
                    "NODE_STARTED",
                    "NODE_COMPLETED",
                    "RUN_COMPLETED",
                    "RUN_STREAM_CLOSED",
                ],
                "ag-ui": [
                    "RUN_STARTED",
                    "STATE_SNAPSHOT",
                    "STATE_SNAPSHOT",
                    "STATE_SNAPSHOT",
                    "RUN_FINISHED",
                ],
                "ai-sdk": [
                    "start",
                    "data-nexura-state-snapshot",
                    "data-nexura-state-snapshot",
                    "data-nexura-state-snapshot",
                    "finish",
                    "[DONE]",
                ],
            }
            actual = (
                types(response.text, format_)
                if format_ != "native"
                else [name for name, _ in frames(response.text)]
            )
            assert actual == expected[format_]
            if format_ == "native":
                default = await client.get(f"/api/v1/runs/{run_id}/events?live=true")
                assert default.text == response.text
            else:
                media = (
                    "application/vnd.ag-ui+json"
                    if format_ == "ag-ui"
                    else "application/vnd.vercel.ai.ui-message-stream+json"
                )
                negotiated = await client.get(
                    f"/api/v1/runs/{run_id}/events", headers={"Accept": media}
                )
                assert types(negotiated.text, format_) == expected[format_]
                override = await client.get(
                    f"/api/v1/runs/{run_id}/events?stream_format=native", headers={"Accept": media}
                )
                assert frames(override.text)[0][0] == "RUN_STARTED"
            replay = await client.get(
                f"/api/v1/runs/{run_id}/events",
                params={"stream_format": format_, "cursor": 1},
                headers={"Last-Event-ID": "3"},
            )
            assert replay.status_code == 200
            assert "NODE_STARTED" not in replay.text
            if format_ == "ag-ui":
                assert types(replay.text, format_) == [
                    "RUN_STARTED",
                    "STATE_SNAPSHOT",
                    "RUN_FINISHED",
                ]
            elif format_ == "ai-sdk":
                assert types(replay.text, format_) == [
                    "start",
                    "data-nexura-state-snapshot",
                    "finish",
                    "[DONE]",
                ]
    finally:
        await store.close()


@pytest.mark.parametrize("format_", ["ag-ui", "ai-sdk"])
@pytest.mark.parametrize("status", ["completed", "failed", "rejected", "cancelled"])
async def test_terminal_status_without_replayed_terminal_event(
    format_: StreamFormat, status: str
) -> None:
    text = await collect(sequence(("RUN_STREAM_CLOSED", {"status": status})), format_, chat=False)
    if status in {"failed", "rejected"}:
        assert types(text, format_) == (
            ["RUN_STARTED", "RUN_ERROR"]
            if format_ == "ag-ui"
            else ["start", "error", "finish", "[DONE]"]
        )
    elif status == "completed":
        assert types(text, format_) == (
            ["RUN_STARTED", "RUN_FINISHED"] if format_ == "ag-ui" else ["start", "finish", "[DONE]"]
        )
    else:
        assert types(text, format_) == (
            ["RUN_STARTED", "CUSTOM", "RUN_FINISHED"]
            if format_ == "ag-ui"
            else ["start", "data-nexura-cancelled", "abort", "finish", "[DONE]"]
        )


@pytest.mark.parametrize("format_", ["ag-ui", "ai-sdk"])
async def test_child_agent_events_have_scoped_ids_and_do_not_end_parent(
    format_: StreamFormat,
) -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    for run_id in ("child-one", "child-two"):
        for kind, payload in [
            ("model_started", {}),
            (
                "tool_started",
                {"tool_call_id": "c1", "tool_name": "lookup", "arguments": {"q": "x"}},
            ),
            ("tool_completed", {"tool_call_id": "c1", "result": "found x"}),
            ("final_output", {"output": "answer"}),
            ("run_failed", {"error": SECRET}),
            ("run_completed", {}),
        ]:
            events.append(
                ("AGENT_EVENT", {"agent_run_id": run_id, "kind": kind, "payload": payload})
            )
    events.append(("RUN_COMPLETED", {}))
    text = await collect(sequence(*events), format_, chat=False)
    assert SECRET not in text
    decoded = [data for _, data in frames(text) if isinstance(data, dict)]
    tool_starts = [
        data for data in decoded if data["type"] in {"TOOL_CALL_START", "tool-input-start"}
    ]
    assert len(tool_starts) == 2
    assert tool_starts[0]["toolCallId"] != tool_starts[1]["toolCallId"]
    assert sum(data["type"] in {"RUN_FINISHED", "finish"} for data in decoded) == 1
    assert decoded[-1]["type"] in {"RUN_FINISHED", "finish"}
    assert not any(data["type"] in {"RUN_ERROR", "error"} for data in decoded)


@pytest.mark.parametrize("format_", ["ag-ui", "ai-sdk"])
async def test_full_history_continues_past_resolved_approval(format_: StreamFormat) -> None:
    text = await collect(
        sequence(
            ("RUN_PENDING_APPROVAL", {"approval_id": "a1"}),
            ("APPROVAL_DECISION", {"approval_id": "a1", "arguments": SECRET}),
            ("NODE_COMPLETED", {"state_update": {"after_approval": True}}),
            ("RUN_COMPLETED", {"final_state": {"after_approval": True}}),
        ),
        format_,
        chat=False,
    )
    assert SECRET not in text
    assert "after_approval" in text
    assert types(text, format_).count("RUN_FINISHED" if format_ == "ag-ui" else "finish") == 1
    assert types(text, format_)[-1] == ("RUN_FINISHED" if format_ == "ag-ui" else "[DONE]")
