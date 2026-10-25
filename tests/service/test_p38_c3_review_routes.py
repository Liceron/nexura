# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Route-level review goldens with real workflow execution and local FakeModels."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from nexura_agents import Agent, FakeModel, GuardrailResult, Runner, ToolCall, tool
from nexura_agents.providers.types import StreamDelta
from nexura_agents.session import SQLiteSessionStore
from p38_c3_protocol_checks import data_events, validate_stream

from nexura_runtime.service.api import sessions
from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.service.stream_adapters import StreamFormat, adapt_stream
from nexura_runtime.studio.traces import traced_run
from nexura_runtime.workflow import ModuleRegistry, WorkflowNode, WorkflowSpec

FORMATS: list[StreamFormat] = ["native", "ag-ui", "ai-sdk"]
SECRET = "card-4111-1111-1111-1111"


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    async def refuse(self: Any, request: httpx.Request) -> httpx.Response:
        raise AssertionError("C3 uses local FakeModels, never paid providers")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", refuse)


def adapted_types(text: str) -> list[str]:
    return [e["type"] if isinstance(e, dict) else e for e in data_events(text)]


@pytest.mark.parametrize("format_", FORMATS)
@pytest.mark.parametrize("outcome", ["approval", "failure", "cancel"])
async def test_workflow_route_terminal_goldens(
    tmp_path: Path, format_: StreamFormat, outcome: str
) -> None:
    registry = ModuleRegistry()
    executed = []

    async def node(state: dict[str, Any]) -> dict[str, Any]:
        executed.append(True)
        if outcome == "failure":
            raise RuntimeError("local fixture failure")
        return {"checked": True}

    registry.register("fixture", node, effect="pure")
    spec = WorkflowSpec(
        name="Review fixture",
        entry_node="n",
        nodes={
            "n": WorkflowNode(id="n", handler="fixture", requires_approval=outcome != "failure")
        },
        terminal_nodes=["n"],
    )
    app = create_api_app(db_path=tmp_path / "app.sqlite3", registry=registry)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        started = await client.post(
            "/api/v1/runs", json={"spec": spec.to_dict(), "initial_state": {"password": SECRET}}
        )
        assert started.status_code == 201, started.text
        run_id = started.json()["run_id"]
        cursor = 0
        if outcome == "cancel":
            cursor = max(e["seq"] for e in started.json()["events"])
            cancelled = await client.post(
                f"/api/v1/runs/{run_id}/cancel", json={"reason": "user requested"}
            )
            assert cancelled.status_code == 200, cancelled.text
            assert cancelled.json()["status"] == "cancelled"
        response = await client.get(
            f"/api/v1/runs/{run_id}/events", params={"stream_format": format_, "cursor": cursor}
        )
        assert response.status_code == 200
        assert SECRET not in response.text
        validate_stream(response.text, format_)
        if format_ == "native":
            expected_last = {
                "approval": "RUN_PENDING_APPROVAL",
                "failure": "RUN_FAILED",
                "cancel": "RUN_CANCELLED",
            }[outcome]
            assert response.text.split("event: ")[-1].split("\n")[0] == expected_last
        else:
            snapshots = ["STATE_SNAPSHOT"] if format_ == "ag-ui" else ["data-nexura-state-snapshot"]
            start = "RUN_STARTED" if format_ == "ag-ui" else "start"
            if outcome == "approval":
                expected = (
                    [start, *snapshots, "CUSTOM", "RUN_FINISHED"]
                    if format_ == "ag-ui"
                    else [start, *snapshots, "data-nexura-approval", "finish", "[DONE]"]
                )
                if format_ == "ag-ui":
                    final = data_events(response.text)[-1]
                    assert final["result"]["nexura"]["paused"] == "approval"
                    assert (
                        final["result"]["nexura"]["approval_id"]
                        == started.json()["pending_approval"]["approval_id"]
                    )
            elif outcome == "failure":
                expected = (
                    [start, *snapshots, *snapshots, "RUN_ERROR"]
                    if format_ == "ag-ui"
                    else [start, *snapshots, *snapshots, "error", "finish", "[DONE]"]
                )
            else:
                expected = (
                    [start, "CUSTOM", "RUN_FINISHED"]
                    if format_ == "ag-ui"
                    else [start, "data-nexura-cancelled", "abort", "finish", "[DONE]"]
                )
            assert adapted_types(response.text) == expected
        assert bool(executed) == (outcome == "failure")


@pytest.mark.parametrize("format_", FORMATS)
@pytest.mark.parametrize("guard", ["rewrite", "block", "none"])
async def test_chat_route_output_guardrails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, format_: StreamFormat, guard: str
) -> None:
    def check(ctx: Any, output: Any) -> GuardrailResult:
        return (
            GuardrailResult.tripped({"rule": "fixture"})
            if guard == "block"
            else GuardrailResult.rewritten("checked reply")
        )

    model = FakeModel([SECRET], stream=True, chunk_size=4)
    agent = Agent(
        name="Local guarded fixture",
        instructions="fixture",
        model=model,
        output_guardrails=[check] if guard != "none" else [],
    )
    monkeypatch.setattr(sessions, "_build_playground_agent", lambda *a, **kw: agent)
    app = create_api_app(db_path=tmp_path / "app.sqlite3", chat_models={"local": model})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/api/v1/sessions/s/chat/stream?stream_format={format_}",
            json={"message": "go", "model": "local"},
        )
        assert response.status_code == 200
        validate_stream(response.text, format_)
        events = data_events(response.text)
        if format_ == "native":
            deltas = [e for e in events if e.get("type") == "TEXT_DELTA"]
            assert bool(deltas) == (guard == "none")
            assert events[-1].get("reply") == (
                "checked reply" if guard == "rewrite" else SECRET if guard == "none" else None
            )
        else:
            content = [
                e
                for e in events
                if isinstance(e, dict) and e["type"] in {"TEXT_MESSAGE_CONTENT", "text-delta"}
            ]
            if guard == "none":
                assert len(content) > 1
                assert "".join(e["delta"] for e in content) == SECRET
            elif guard == "rewrite":
                assert [e["delta"] for e in content] == ["checked reply"]
            else:
                assert not content
                assert adapted_types(response.text) == (
                    ["RUN_STARTED", "RUN_ERROR"]
                    if format_ == "ag-ui"
                    else ["start", "error", "finish", "[DONE]"]
                )
        if guard != "none":
            assert SECRET not in response.text


@tool
def unicode_lookup() -> str:
    """Local Unicode tool fixture."""
    return "result\u2028with\u2029all\u0085separators"


@pytest.mark.parametrize("format_", FORMATS)
async def test_chat_route_unicode_text_and_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, format_: StreamFormat
) -> None:
    answer = "a\u2028b\u2029c\u0085d"
    model = FakeModel(
        [[ToolCall(id="c1", name="unicode_lookup", arguments={})], answer], stream=True
    )
    agent = Agent(
        name="Local Unicode fixture", instructions="fixture", model=model, tools=[unicode_lookup]
    )
    monkeypatch.setattr(sessions, "_build_playground_agent", lambda *a, **kw: agent)
    app = create_api_app(db_path=tmp_path / "app.sqlite3", chat_models={"local": model})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/api/v1/sessions/s/chat/stream?stream_format={format_}",
            json={"message": "go", "model": "local"},
        )
    assert response.status_code == 200
    validate_stream(response.text, format_)
    events = data_events(response.text)
    if format_ == "native":
        assert events[-1]["reply"] == answer
        assert (
            events[-1]["tool_executions"][0]["output"]
            == "result\u2028with\u2029all\u0085separators"
        )
    else:
        assert (
            "".join(
                e["delta"]
                for e in events
                if isinstance(e, dict) and e["type"] in {"TEXT_MESSAGE_CONTENT", "text-delta"}
            )
            == answer
        )
        result = next(
            e
            for e in events
            if isinstance(e, dict) and e["type"] in {"TOOL_CALL_RESULT", "tool-output-available"}
        )
        assert (
            result.get("content", result.get("output"))
            == "result\u2028with\u2029all\u0085separators"
        )


@pytest.mark.parametrize("format_", ["ag-ui", "ai-sdk"])
@pytest.mark.parametrize("failure", ["model", "response"])
async def test_chat_route_error_after_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, format_: StreamFormat, failure: str
) -> None:
    class FailingFake(FakeModel):
        async def stream(self, *args: Any, **kwargs: Any) -> AsyncIterator[StreamDelta]:
            yield StreamDelta(kind="text_delta", text="partial reply")
            raise RuntimeError(SECRET)

    model = (
        FailingFake(stream=True)
        if failure == "model"
        else FakeModel(["partial reply"], stream=True)
    )
    if failure == "response":

        async def fail_response(*args: Any, **kwargs: Any) -> dict[str, Any]:
            raise RuntimeError(SECRET)

        monkeypatch.setattr(sessions, "_chat_response", fail_response)
    app = create_api_app(db_path=tmp_path / "app.sqlite3", chat_models={"local": model})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/api/v1/sessions/s/chat/stream?stream_format={format_}",
            json={"message": "go", "model": "local"},
        )
    validate_stream(response.text, format_)
    assert SECRET not in response.text
    assert "partial reply" in response.text if failure == "model" else "partial " in response.text
    assert adapted_types(response.text)[-1] == ("RUN_ERROR" if format_ == "ag-ui" else "[DONE]")
    if failure == "response":
        assert (
            "STREAM_SOURCE_ERROR" in response.text
            if format_ == "ag-ui"
            else "The event stream failed." in response.text
        )


@pytest.mark.parametrize("format_", ["ag-ui", "ai-sdk"])
@pytest.mark.parametrize("newline", ["\r", "\n", "\r\n"])
async def test_physical_sse_lines_and_failure_completion(
    format_: StreamFormat, newline: str
) -> None:
    async def source() -> AsyncIterator[str]:
        frame = (
            "event: run_event"
            + newline
            + 'data: {"type":"TEXT_DELTA","data":{"text":"a\u2028b\u2029c\u0085d"}}'
            + newline * 2
        )
        for char in frame:
            yield char
        raise RuntimeError(SECRET)

    text = "".join(
        [part async for part in adapt_stream(source(), format_, thread_id="t", run_id="r")]
    )
    validate_stream(text, format_)
    assert SECRET not in text
    assert "a\u2028b\u2029c\u0085d" in text
    assert adapted_types(text)[-1] == ("RUN_ERROR" if format_ == "ag-ui" else "[DONE]")


@pytest.mark.parametrize("format_", FORMATS)
@pytest.mark.parametrize("guard", ["rewrite", "block"])
async def test_workflow_agent_route_guardrails(
    tmp_path: Path, format_: StreamFormat, guard: str
) -> None:
    def check(ctx: Any, output: Any) -> GuardrailResult:
        return (
            GuardrailResult.tripped({"rule": "fixture"})
            if guard == "block"
            else GuardrailResult.rewritten("checked workflow reply")
        )

    agent = Agent(
        name="Local workflow FakeModel",
        instructions="fixture",
        model=FakeModel([SECRET], stream=True),
        output_guardrails=[check],
    )

    async def node(state: dict[str, Any]) -> dict[str, Any]:
        result = await traced_run(Runner(), agent, "go", stream_text=True)
        return {"reply": result.final_output}

    registry = ModuleRegistry()
    registry.register("fixture", node, effect="pure")
    spec = WorkflowSpec(
        name="Guarded workflow",
        entry_node="n",
        nodes={"n": WorkflowNode(id="n", handler="fixture")},
        terminal_nodes=["n"],
    )
    app = create_api_app(db_path=tmp_path / "workflow.sqlite3", registry=registry)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        started = await client.post(
            "/api/v1/runs", json={"spec": spec.to_dict(), "initial_state": {}}
        )
        assert started.status_code == 201
        response = await client.get(
            f"/api/v1/runs/{started.json()['run_id']}/events?stream_format={format_}"
        )
    validate_stream(response.text, format_)
    assert SECRET not in response.text
    if guard == "rewrite":
        assert "checked workflow reply" in response.text
    elif format_ != "native":
        assert not any(
            e["type"] in {"TEXT_MESSAGE_START", "text-start"}
            for e in data_events(response.text)
            if isinstance(e, dict)
        )
        assert (
            "RUN_ERROR" in response.text
            if format_ == "ag-ui"
            else '"type":"error"' in response.text
        )


@pytest.mark.parametrize("format_", FORMATS)
async def test_chat_handoff_to_guarded_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, format_: StreamFormat
) -> None:
    def rewrite(ctx: Any, output: Any) -> GuardrailResult:
        return GuardrailResult.rewritten("checked handoff reply")

    target = Agent(
        name="Guarded target",
        instructions="fixture",
        model=FakeModel([SECRET], stream=True),
        output_guardrails=[rewrite],
    )
    root = Agent(
        name="Unguarded root",
        instructions="fixture",
        model=FakeModel(
            [[ToolCall(id="c1", name="transfer_to_guarded_target", arguments={})]], stream=True
        ),
        handoffs=[target],
    )
    monkeypatch.setattr(sessions, "_build_playground_agent", lambda *a, **kw: root)
    app = create_api_app(db_path=tmp_path / "handoff.sqlite3", chat_models={"local": root.model})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/api/v1/sessions/s/chat/stream?stream_format={format_}",
            json={"message": "go", "model": "local"},
        )
    validate_stream(response.text, format_)
    assert SECRET not in response.text
    assert "checked handoff reply" in response.text


@pytest.mark.parametrize("format_", ["ag-ui", "ai-sdk"])
@pytest.mark.parametrize("tail", ["malformed", "missing-terminal", "empty"])
async def test_bad_source_always_has_terminal_error(format_: StreamFormat, tail: str) -> None:
    async def source() -> AsyncIterator[str]:
        if tail != "empty":
            yield 'event: run_event\ndata: {"type":"TEXT_DELTA","data":{"text":"open text"}}\n\n'
        if tail == "malformed":
            yield 'event: run_event\ndata: {broken: "' + SECRET + '"}\n\n'

    text = "".join(
        [part async for part in adapt_stream(source(), format_, thread_id="t", run_id="r")]
    )
    validate_stream(text, format_)
    assert SECRET not in text
    assert adapted_types(text)[-1] == ("RUN_ERROR" if format_ == "ag-ui" else "[DONE]")
    assert "RUN_FINISHED" not in text


@pytest.mark.parametrize("format_", FORMATS)
async def test_workflow_reducer_snapshots_are_authoritative(
    tmp_path: Path, format_: StreamFormat
) -> None:
    async def a(state: dict[str, Any]) -> dict[str, Any]:
        return {"items": 2}

    async def b(state: dict[str, Any]) -> dict[str, Any]:
        return {"items": 3}

    registry = ModuleRegistry()
    registry.register("a", a)
    registry.register("b", b)
    spec = WorkflowSpec(
        name="Append fixture",
        entry_node="p",
        nodes={
            "p": WorkflowNode(id="p", handler="", parallel=["a", "b"], reducers={"items": "append"})
        },
        terminal_nodes=["p"],
    )
    app = create_api_app(db_path=tmp_path / "append.sqlite3", registry=registry)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        started = await client.post(
            "/api/v1/runs", json={"spec": spec.to_dict(), "initial_state": {"items": [1]}}
        )
        assert started.status_code == 201, started.text
        assert started.json()["current_state"]["items"] == [1, 2, 3]
        response = await client.get(
            f"/api/v1/runs/{started.json()['run_id']}/events?stream_format={format_}"
        )
    validate_stream(response.text, format_)
    if format_ == "native":
        assert data_events(response.text)[-1]["final_state"]["items"] == [1, 2, 3]
    else:
        snapshots = [
            e.get("snapshot", e.get("data"))
            for e in data_events(response.text)
            if isinstance(e, dict) and e["type"] in {"STATE_SNAPSHOT", "data-nexura-state-snapshot"}
        ]
        assert isinstance(snapshots[-1], dict)
        assert snapshots[-1]["items"] == [1, 2, 3]
        assert not any(
            e["type"] in {"STATE_DELTA", "data-nexura-state-delta"}
            for e in data_events(response.text)
            if isinstance(e, dict)
        )


@pytest.mark.parametrize("format_", ["ag-ui", "ai-sdk"])
async def test_reply_stays_hidden_while_guardrail_is_running(
    tmp_path: Path, format_: StreamFormat
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    async def waiting_guard(ctx: Any, output: Any) -> GuardrailResult:
        entered.set()
        await release.wait()
        return GuardrailResult.rewritten("checked after review")

    model = FakeModel([SECRET], stream=True, chunk_size=2)
    agent = Agent(
        name="Waiting local FakeModel",
        instructions="fixture",
        model=model,
        output_guardrails=[waiting_guard],
    )
    store = SQLiteSessionStore(tmp_path / "waiting.sqlite3")
    seen: list[str] = []

    async def consume() -> None:
        source = sessions._chat_event_stream(
            store, "s", sessions.ChatRequest(message="go"), model=model, agent=agent
        )
        async for chunk in adapt_stream(source, format_, thread_id="s", run_id="r", chat=True):
            seen.append(chunk)

    task = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert not task.done()
        assert not any(
            e["type"] in {"TEXT_MESSAGE_START", "TEXT_MESSAGE_CONTENT", "text-start", "text-delta"}
            for e in data_events("".join(seen))
        )
        release.set()
        await asyncio.wait_for(task, 5)
        text = "".join(seen)
        validate_stream(text, format_)
        assert "checked after review" in text and SECRET not in text
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await store.close()
