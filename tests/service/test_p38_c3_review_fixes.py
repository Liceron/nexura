# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""P38 C3 adversarial review repros. Local FakeModel only; no network, no paid calls.

Each original review scenario now asserts the fixed contract. The original
defect-asserting repros are preserved in c3_backup/round2/.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from nexura_agents import Agent, FakeModel, GuardrailResult, ToolCall, tool
from nexura_agents.session import SQLiteSessionStore
from p38_c3_protocol_checks import validate_stream
from starlette.requests import Request

from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.service.api.sessions import ChatRequest, _chat_event_stream, _sse
from nexura_runtime.service.stream_adapters import adapt_stream, select_stream_format

PII = "card-4111-1111-1111-1111"


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    async def refuse(self: Any, request: httpx.Request) -> httpx.Response:
        raise AssertionError("no real provider calls")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", refuse)


def data_frames(text: str) -> list[Any]:
    out = []
    for frame in text.strip().split("\n\n"):
        for line in frame.split("\n"):
            if line.startswith("data: "):
                raw = line[6:]
                out.append(raw if raw == "[DONE]" else json.loads(raw))
    return out


async def run_chat(tmp_path: Path, agent: Agent, model: FakeModel, fmt: str) -> str:
    store = SQLiteSessionStore(tmp_path / f"{fmt}.sqlite3")
    try:
        source: AsyncIterator[str] = _chat_event_stream(
            store, "s", ChatRequest(message="go"), model=model, agent=agent
        )
        if fmt != "native":
            source = adapt_stream(source, fmt, thread_id="s", run_id="r", chat=True)  # type: ignore[arg-type]
        text = "".join([c async for c in source])
        validate_stream(text, fmt)
        return text
    finally:
        await store.close()


# ---------------------------------------------------------------- R1 Unicode framing
@pytest.mark.parametrize("sep", [" ", " ", "\x85"])
@pytest.mark.parametrize("fmt", ["ag-ui", "ai-sdk"])
async def test_r1_unicode_line_separators_survive(tmp_path: Path, fmt: str, sep: str) -> None:
    """SSE physical lines preserve raw U+2028/U+2029/U+0085 in native and adapted text."""
    model = FakeModel([f"line one{sep}line two"], stream=True)
    agent = Agent(name="Local FakeModel", model=model, instructions="fixture")
    native = await run_chat(tmp_path, agent, model, "native")
    assert sep in native  # native stream is fine
    model2 = FakeModel([f"line one{sep}line two"], stream=True)
    agent2 = Agent(name="Local FakeModel", model=model2, instructions="fixture")
    adapted = await run_chat(tmp_path, agent2, model2, fmt)
    text = "".join(
        e["delta"]
        for e in data_frames(adapted)
        if isinstance(e, dict) and e["type"] in {"TEXT_MESSAGE_CONTENT", "text-delta"}
    )
    assert text == f"line one{sep}line two"
    assert (
        data_frames(adapted)[-1] == "[DONE]"
        if fmt == "ai-sdk"
        else data_frames(adapted)[-1]["type"] == "RUN_FINISHED"
    )


# ---------------------------------------------------------------- R2 guardrail rewrite
def scrub(ctx: Any, output: Any) -> GuardrailResult:
    return GuardrailResult.rewritten(str(output).replace(PII, "[card removed]"))


def block(ctx: Any, output: Any) -> GuardrailResult:
    return GuardrailResult.tripped({"rule": "pii"})


@pytest.mark.parametrize("fmt", ["ag-ui", "ai-sdk"])
async def test_r2_output_guardrail_rewrite_is_authoritative(tmp_path: Path, fmt: str) -> None:
    """Hold the reply until guardrails rewrite it; no unchecked text reaches a client."""
    model = FakeModel([f"Your number is {PII}."], stream=True, chunk_size=4)
    agent = Agent(
        name="Local FakeModel", model=model, instructions="fixture", output_guardrails=[scrub]
    )
    (tmp_path / "n").mkdir()
    native = await run_chat(tmp_path / "n", agent, model, "native")
    final = [f for f in data_frames(native) if isinstance(f, dict) and "reply" in f][-1]
    assert final["reply"] == "Your number is [card removed]."
    assert all(m.get("content") != f"Your number is {PII}." for m in final["messages"])

    model2 = FakeModel([f"Your number is {PII}."], stream=True, chunk_size=4)
    agent2 = Agent(
        name="Local FakeModel", model=model2, instructions="fixture", output_guardrails=[scrub]
    )
    adapted = await run_chat(tmp_path, agent2, model2, fmt)
    events = [e for e in data_frames(adapted) if isinstance(e, dict)]
    text = "".join(
        e["delta"] for e in events if e["type"] in ("TEXT_MESSAGE_CONTENT", "text-delta")
    )
    assert text == "Your number is [card removed]."
    assert PII not in adapted
    assert events[-1]["type"] in ("RUN_FINISHED", "finish")  # reported as success


@pytest.mark.parametrize("fmt", ["ag-ui", "ai-sdk"])
async def test_r2b_output_guardrail_tripwire_emits_no_text(tmp_path: Path, fmt: str) -> None:
    """A tripwire produces an error without publishing a blocked text message."""
    model = FakeModel([f"Your number is {PII}."], stream=True, chunk_size=4)
    agent = Agent(
        name="Local FakeModel", model=model, instructions="fixture", output_guardrails=[block]
    )
    adapted = await run_chat(tmp_path, agent, model, fmt)
    events = [e for e in data_frames(adapted) if isinstance(e, dict)]
    types = [e["type"] for e in events]
    text = "".join(
        e["delta"] for e in events if e["type"] in ("TEXT_MESSAGE_CONTENT", "text-delta")
    )
    assert not text
    assert PII not in adapted
    assert not any(
        kind in types
        for kind in ("TEXT_MESSAGE_START", "TEXT_MESSAGE_END", "text-start", "text-end")
    )
    err = "RUN_ERROR" if fmt == "ag-ui" else "error"
    assert err in types


# ---------------------------------------------------------------- R3 back-compat 406
@pytest.mark.parametrize("accept", ["text/event-stream;q=0", "text/event-stream;q=bad"])
def test_r3_native_accept_falls_back(accept: str) -> None:
    """Preserve native fallback for malformed/refused Accept ranges."""
    request = Request({"type": "http", "headers": [(b"accept", accept.encode())]})
    assert select_stream_format(request, None) == "native"


# ---------------------------------------------------------------- R4 AG-UI run boundary
async def test_r4_ag_ui_nonterminal_replay_ends_with_error() -> None:
    """An incomplete observation must terminate the protocol with RUN_ERROR."""

    async def source() -> AsyncIterator[str]:
        yield _sse(1, "RUN_STARTED", {"initial_state": {"x": 1}})
        yield _sse(2, "NODE_STARTED", {"input_state": {"x": 1}})

    out = "".join([c async for c in adapt_stream(source(), "ag-ui", thread_id="t", run_id="r")])
    types = [e["type"] for e in data_frames(out)]
    assert types[0] == "RUN_STARTED"
    assert types[-1] == "RUN_ERROR"
    assert data_frames(out)[-1]["code"] == "STREAM_INCOMPLETE"


# ---------------------------------------------------------------- R5 reducer delta
async def test_r5_raw_writes_do_not_replace_reduced_state() -> None:
    """Raw writes cannot replace reducer state; wait for an authoritative snapshot."""

    async def source() -> AsyncIterator[str]:
        yield _sse(1, "NODE_STARTED", {"input_state": {"items": [1]}})
        yield _sse(2, "NODE_COMPLETED", {"state_update": {"items": [2]}})

    out = "".join([c async for c in adapt_stream(source(), "ag-ui", thread_id="t", run_id="r")])
    assert not any(e["type"] == "STATE_DELTA" for e in data_frames(out))
    snapshots = [e["snapshot"] for e in data_frames(out) if e["type"] == "STATE_SNAPSHOT"]
    assert snapshots == [{"items": [1]}]  # No false [2] state before the next snapshot.


# ---------------------------------------------------------------- R6 refused tool
@tool(requires_approval=True)
def write(q: str) -> str:
    """Approval-gated fixture."""
    raise AssertionError("must not run")


@pytest.mark.parametrize("fmt", ["ag-ui", "ai-sdk"])
async def test_r6_chat_refused_call_arguments_visible_as_tool_input(
    tmp_path: Path, fmt: str
) -> None:
    """Chat refuses approval-gated calls. The refused call's arguments are emitted as
    TOOL_CALL_ARGS / tool-input-available (same bytes as native TOOL_STARTED and native
    RUN_PAUSED.pending, so not a new leak), while the approval notice itself omits them.
    Documented as 'pending arguments omitted', which is only true of the notice."""
    secret = "refused-arg-7731"
    model = FakeModel(
        [[ToolCall(id="c1", name="write", arguments={"q": secret})], "ok"], stream=True
    )
    agent = Agent(name="Local FakeModel", model=model, instructions="fixture", tools=[write])
    (tmp_path / "n").mkdir()
    native = await run_chat(tmp_path / "n", agent, model, "native")
    assert secret in native
    model2 = FakeModel(
        [[ToolCall(id="c1", name="write", arguments={"q": secret})], "ok"], stream=True
    )
    agent2 = Agent(name="Local FakeModel", model=model2, instructions="fixture", tools=[write])
    adapted = await run_chat(tmp_path, agent2, model2, fmt)
    assert secret in adapted
    notices = [
        e
        for e in data_frames(adapted)
        if isinstance(e, dict)
        and (e.get("name") == "nexura.approval" or e.get("type") == "data-nexura-approval")
    ]
    assert len(notices) == 1
    assert secret not in json.dumps(notices)
    inputs = [
        e
        for e in data_frames(adapted)
        if isinstance(e, dict) and e["type"] in {"TOOL_CALL_ARGS", "tool-input-available"}
    ]
    assert secret in json.dumps(inputs)


# ---------------------------------------------------------------- R7 route-level unicode
async def test_r7_route_level_unicode_completes(tmp_path: Path) -> None:
    """Same as R1 through the HTTP route with a configured local chat model."""
    app = create_api_app(
        db_path=tmp_path / "app.sqlite3",
        chat_models={"local": FakeModel(["a b"], repeat_last=True, stream=True)},
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        native = await client.post(
            "/api/v1/sessions/s/chat/stream", json={"message": "hi", "model": "local"}
        )
        assert native.status_code == 200 and "chat_completed" in native.text
        adapted = await client.post(
            "/api/v1/sessions/s2/chat/stream?stream_format=ai-sdk",
            json={"message": "hi", "model": "local"},
        )
        assert adapted.status_code == 200
        assert (
            "".join(
                e["delta"]
                for e in data_frames(adapted.text)
                if isinstance(e, dict) and e["type"] == "text-delta"
            )
            == "a\u2028b"
        )
        assert data_frames(adapted.text)[-2:] == [{"type": "finish"}, "[DONE]"]
