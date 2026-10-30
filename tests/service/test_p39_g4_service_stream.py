# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""P39 G4: the service chat stream and the C3 adapters use the Runner's native holding.

Local FakeModels and an in-process ASGI transport only; never a paid provider.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest
from nexura_agents import (
    Agent,
    AgentHooks,
    FakeModel,
    GuardrailResult,
    StreamGuard,
    redact_output_pii,
    set_default_hooks,
)
from p38_c3_protocol_checks import data_events, validate_stream

from nexura_runtime.service.api import sessions
from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.service.stream_adapters import StreamFormat

FORMATS: list[StreamFormat] = ["native", "ag-ui", "ai-sdk"]
CARD = "4111 1111 1111 1111"
REPLY = "Thanks for asking. " * 3 + f"Card {CARD} is on file. " + "Goodbye now. " * 3
CHECKED = REPLY.replace(CARD, "[REDACTED:card]")


def leaked_card_digits(text: str) -> bool:
    """True if the card's leading digits appear outside event timestamps.

    Timestamps such as ``1792855522.154111`` can contain ``4111`` by chance.
    """
    return "4111" in re.sub(r'"timestamp": [0-9.]+', '"timestamp": 0', text)


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    async def refuse(self: Any, request: httpx.Request) -> httpx.Response:
        raise AssertionError("G4 uses local FakeModels, never paid providers")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", refuse)


@pytest.fixture(autouse=True)
def _no_default_hooks() -> Iterator[None]:
    from nexura_agents import policy

    policy._unlock_for_tests()
    set_default_hooks(None)
    yield
    policy._unlock_for_tests()
    set_default_hooks(None)


async def chat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent: Agent, format_: StreamFormat
) -> str:
    monkeypatch.setattr(sessions, "_build_playground_agent", lambda *a, **kw: agent)
    app = create_api_app(db_path=tmp_path / "app.sqlite3", chat_models={"local": agent.model})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/api/v1/sessions/s/chat/stream?stream_format={format_}",
            json={"message": "go", "model": "local"},
        )
    assert response.status_code == 200
    validate_stream(response.text, format_)
    return response.text


def adapted_text(text: str) -> str:
    return "".join(
        e["delta"]
        for e in data_events(text)
        if isinstance(e, dict) and e.get("type") in {"TEXT_MESSAGE_CONTENT", "text-delta"}
    )


def native_runs(text: str) -> list[dict[str, Any]]:
    return [e for e in data_events(text) if isinstance(e, dict) and "type" in e and "data" in e]


@pytest.mark.parametrize("format_", FORMATS)
async def test_chunked_chat_streams_only_checked_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, format_: StreamFormat
) -> None:
    agent = Agent(
        name="Chunked local fake",
        instructions="fixture",
        model=FakeModel([REPLY], stream=True, chunk_size=3),
        output_guardrails=[redact_output_pii(["card"])],
        stream_guard=StreamGuard(mode="chunked", chunk_chars=16, overlap_chars=32),
    )
    text = await chat(tmp_path, monkeypatch, agent, format_)
    assert not leaked_card_digits(text)
    if format_ == "native":
        runs = native_runs(text)
        started = next(e for e in runs if e["type"] == "MODEL_STARTED")
        assert started["data"]["stream_guard"] == "chunked"
        pieces = [e["data"]["text"] for e in runs if e["type"] == "TEXT_DELTA"]
        assert len(pieces) > 2 and "".join(pieces) == CHECKED
        assert all(e["data"]["checked"] is True for e in runs if e["type"] == "TEXT_DELTA")
        done = data_events(text)[-1]
        assert done["reply"] == CHECKED and done["delivery"] == "streamed"
    else:
        assert adapted_text(text) == CHECKED  # deltas, and FINAL_OUTPUT is not repeated


class ScrubPolicy(AgentHooks):
    def output_guardrails(self, ctx: Any, agent: Any) -> Sequence[Any]:
        return [redact_output_pii(["card"])]


@pytest.mark.parametrize("format_", FORMATS)
async def test_default_policy_holds_the_chat_reply_of_an_agent_without_guardrails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, format_: StreamFormat
) -> None:
    """P38 C3's source only knew agent-level guardrails; the native rule also covers a
    process-default policy."""
    set_default_hooks(ScrubPolicy())
    agent = Agent(
        name="Plain local fake",
        instructions="fixture",
        model=FakeModel([REPLY], stream=True, chunk_size=3),
    )
    text = await chat(tmp_path, monkeypatch, agent, format_)
    assert not leaked_card_digits(text)
    if format_ == "native":
        runs = native_runs(text)
        assert not any(e["type"] == "TEXT_DELTA" for e in runs)
        assert next(e for e in runs if e["type"] == "MODEL_STARTED")["data"]["stream_guard"] == (
            "hold"
        )
        assert data_events(text)[-1]["reply"] == CHECKED
    else:
        assert adapted_text(text) == CHECKED


@pytest.mark.parametrize("format_", FORMATS)
async def test_chunked_trip_reports_guardrail_tripped_natively_and_errors_in_adapters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, format_: StreamFormat
) -> None:
    def no_cards(ctx: Any, output: Any) -> GuardrailResult:
        return GuardrailResult(tripwire="4111" in str(output), info={"rule": "cards"})

    agent = Agent(
        name="Tripping local fake",
        instructions="fixture",
        model=FakeModel([REPLY], stream=True, chunk_size=3),
        output_guardrails=[no_cards],
        stream_guard=StreamGuard(mode="chunked", chunk_chars=16, overlap_chars=32),
    )
    text = await chat(tmp_path, monkeypatch, agent, format_)
    assert not leaked_card_digits(text) and "Card" not in adapted_text(text)
    if format_ == "native":
        runs = native_runs(text)
        tripped = next(e for e in runs if e["type"] == "GUARDRAIL_TRIPPED")["data"]
        assert tripped == {
            "stage": "output",
            "guardrail": "no_cards",
            "agent": "Tripping local fake",
            "turn": 1,
            "phase": "chunk",
            "stream_guard": "chunked",
            "shown": True,
        }
        assert "chat_failed" in text
    else:
        types = [e["type"] if isinstance(e, dict) else e for e in data_events(text)]
        assert "GUARDRAIL_TRIPPED" not in text  # adapters drop the native-only event
        assert ("RUN_ERROR" if format_ == "ag-ui" else "error") in types
