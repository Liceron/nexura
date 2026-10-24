# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""P39 G4 review: the service chat stream never carries unchecked reply text.

From the independent review's repros (each asserted a leak; here each asserts the
fix) plus the review's m4 discard signal and M3 retries. Local FakeModels, in-process ASGI.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from nexura_agents import (
    Agent,
    FakeModel,
    GuardrailResult,
    StreamGuard,
    ToolCall,
    redact_output_pii,
    set_default_hooks,
    tool,
)
from nexura_agents.types import Message, ModelResponse
from p38_c3_protocol_checks import data_events, validate_stream
from pydantic import BaseModel

from nexura_runtime.service.api import sessions
from nexura_runtime.service.api.app import create_api_app

KEY = "sk-" + "A1b2C3d4E5" * 4
FORMATS = ["native", "ag-ui", "ai-sdk"]


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    async def refuse(self: Any, request: httpx.Request) -> httpx.Response:
        raise AssertionError("local only")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", refuse)


@pytest.fixture(autouse=True)
def _no_default_hooks() -> Iterator[None]:
    from nexura_agents import policy

    policy._unlock_for_tests()
    set_default_hooks(None)
    yield
    policy._unlock_for_tests()
    set_default_hooks(None)


async def chat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent: Agent, fmt: str) -> str:
    monkeypatch.setattr(sessions, "_build_playground_agent", lambda *a, **kw: agent)
    app = create_api_app(db_path=tmp_path / "app.sqlite3", chat_models={"local": agent.model})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.post(
            f"/api/v1/sessions/s/chat/stream?stream_format={fmt}",
            json={"message": "go", "model": "local"},
        )
    assert r.status_code == 200
    validate_stream(r.text, fmt)
    return r.text


@tool
def lookup(order: str) -> str:
    """Look up an order."""
    return "shipped"


def pre_tool_agent() -> Agent:
    turn = ModelResponse(
        message=Message(
            role="assistant",
            content=f"Using the API key {KEY} to look it up.",
            tool_calls=[ToolCall(id="c1", name="lookup", arguments={"order": "A1"})],
        )
    )
    return Agent(
        name="A",
        instructions="i",
        model=FakeModel([turn, "It shipped."], stream=True, chunk_size=4),
        tools=[lookup],
        output_guardrails=[redact_output_pii(["secret"])],
    )


@pytest.mark.parametrize("fmt", FORMATS)
async def test_hold_pre_tool_text_reaches_no_client_unchecked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fmt: str
) -> None:
    body = await chat(tmp_path, monkeypatch, pre_tool_agent(), fmt)
    assert '"stream_guard": "hold"' in body or fmt != "native"
    assert KEY not in body
    if fmt == "native":
        done = data_events(body)[-1]
        assert "Using the API key [REDACTED:secret] to look it up." in json.dumps(done["messages"])


async def test_no_native_frame_carries_the_pre_tool_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = await chat(tmp_path, monkeypatch, pre_tool_agent(), "native")
    frames = [f for f in body.split("\n\n") if KEY in f]
    assert frames == []


@pytest.mark.parametrize("fmt", FORMATS)
async def test_stop_on_first_tool_result_is_not_echoed_before_the_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fmt: str
) -> None:
    @tool
    def fetch(order: str) -> str:
        """Fetch."""
        return f"the key is {KEY}"

    def block_key(ctx: Any, output: Any) -> GuardrailResult:
        return GuardrailResult.tripped() if KEY in str(output) else GuardrailResult.passed()

    agent = Agent(
        name="A",
        instructions="i",
        model=FakeModel(
            [[ToolCall(id="c1", name="fetch", arguments={"order": "A1"})]], stream=True
        ),
        tools=[fetch],
        output_guardrails=[block_key],
        tool_use_behavior="stop_on_first_tool",
    )
    body = await chat(tmp_path, monkeypatch, agent, fmt)
    assert KEY not in body


class Out(BaseModel):
    n: int


@pytest.mark.parametrize("fmt", FORMATS)
async def test_typed_retry_and_failure_frames_carry_no_unchecked_reply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fmt: str
) -> None:
    bad = json.dumps({"n": KEY})

    def ok(ctx: Any, output: Any) -> GuardrailResult:
        return GuardrailResult.passed()

    agent = Agent(
        name="A",
        instructions="i",
        model=FakeModel([bad, bad], stream=True, chunk_size=5),
        output_type=Out,
        output_guardrails=[ok],
        output_retries=1,
    )
    body = await chat(tmp_path, monkeypatch, agent, fmt)
    assert KEY not in body
    if fmt == "native":
        assert "RETRY" in body and "chat_failed" in body


@pytest.mark.parametrize("fmt", ["ag-ui", "ai-sdk"])
async def test_adapters_pass_on_a_discard_signal_after_a_chunked_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fmt: str
) -> None:
    reply = "Safe words here. " * 6 + "FORBIDDEN details. " + "tail " * 30

    def no_forbidden(ctx: Any, output: Any) -> GuardrailResult:
        return GuardrailResult(tripwire="FORB" in str(output))

    agent = Agent(
        name="A",
        instructions="i",
        model=FakeModel([reply], stream=True, chunk_size=3),
        output_guardrails=[no_forbidden],
        stream_guard=StreamGuard(mode="chunked", chunk_chars=16, overlap_chars=32),
    )
    body = await chat(tmp_path, monkeypatch, agent, fmt)
    events = [e for e in data_events(body) if isinstance(e, dict)]
    texts = [e["delta"] for e in events if e.get("type") in {"TEXT_MESSAGE_CONTENT", "text-delta"}]
    assert texts and "FORB" not in "".join(texts)
    if fmt == "ag-ui":
        notice = next(e for e in events if e.get("name") == "nexura.guardrail-tripped")
        assert notice["value"] == {"discard": True, "phase": "chunk", "stream_guard": "chunked"}
        kinds = [e["type"] for e in events]
        assert kinds.index("TEXT_MESSAGE_END") < kinds.index("CUSTOM") < kinds.index("RUN_ERROR")
    else:
        notice = next(e for e in events if e.get("type") == "data-nexura-guardrail-tripped")
        assert notice["data"]["discard"] is True and notice["transient"] is True
