# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""P39 G4: nexura-guardrails checks on streamed replies (hold and chunked modes).

Offline only: FakeModel agents and FakeModel judges (local fakes), no provider calls.
"""

from __future__ import annotations

from typing import Any

from guardrails_pkg_helpers import ALLOW, verdict_json
from nexura_agents import (
    Agent,
    FakeModel,
    GuardrailTripwireTriggered,
    RunEvent,
    RunEventType,
    Runner,
    StreamGuard,
)
from nexura_guardrails import JudgeCheck, as_guardrail, pii

CARD = "4111 1111 1111 1111"
T = RunEventType


async def run(agent: Agent) -> tuple[list[RunEvent], BaseException | None]:
    events: list[RunEvent] = []
    try:
        async for event in Runner().stream_async(agent, "go", stream_text=True):
            events.append(event)
    except Exception as exc:
        return events, exc
    return events, None


def shown(events: list[RunEvent]) -> str:
    return "".join(e.data["text"] for e in events if e.type == T.TEXT_DELTA)


def judge(model: Any, **kwargs: Any) -> JudgeCheck:
    kwargs.setdefault("categories", ("privacy", "other"))
    return JudgeCheck(model, "Block personal data. Allow the rest.", **kwargs)


async def test_pii_redact_prefilter_scrubs_every_window_before_it_is_shown() -> None:
    reply = "Opening words here. " * 4 + f"Card {CARD} on file. " + "Closing. " * 6
    agent = Agent(
        name="A",
        instructions="i",
        model=FakeModel([reply], stream=True, chunk_size=3),
        output_guardrails=[as_guardrail(pii(["card"], action="redact"), stage="output")],
        stream_guard=StreamGuard(mode="chunked", chunk_chars=16, overlap_chars=40),
    )
    events, error = await run(agent)
    assert error is None
    pieces = [e.data["text"] for e in events if e.type == T.TEXT_DELTA]
    assert len(pieces) > 3
    assert all("4111" not in p and "1111" not in p for p in pieces), pieces
    assert shown(events) == events[-1].data["output"]
    assert "[REDACTED:card]" in shown(events)


async def test_pii_block_prefilter_stops_the_window_with_the_card() -> None:
    reply = "Opening words here. " * 4 + f"Card {CARD} on file. " + "Closing. " * 6
    agent = Agent(
        name="A",
        instructions="i",
        model=FakeModel([reply], stream=True, chunk_size=3),
        output_guardrails=[as_guardrail(pii(["card"]), stage="output")],
        stream_guard=StreamGuard(mode="chunked", chunk_chars=16, overlap_chars=40),
    )
    events, error = await run(agent)
    assert isinstance(error, GuardrailTripwireTriggered)
    text = shown(events)
    assert "4111" not in text and "Card" not in text and reply.startswith(text)
    tripped = next(e for e in events if e.type == T.GUARDRAIL_TRIPPED)
    assert tripped.data["phase"] == "chunk" and "4111" not in str(tripped.data)


async def test_judge_checks_each_window_and_shows_only_allowed_ones() -> None:
    reply = "A harmless sentence. " * 3 + "A sentence the judge blocks. " + "More. " * 5
    seen: list[str] = []

    def scripted(messages: list[Any], tools: Any) -> str:
        content = messages[-1].content or ""
        seen.append(content)
        return (
            verdict_json("block", ["privacy"], 0.9, "personal data")
            if "blocks" in content
            else ALLOW
        )

    agent = Agent(
        name="A",
        instructions="i",
        model=FakeModel([reply], stream=True, chunk_size=4),
        output_guardrails=[as_guardrail(judge(FakeModel(handler=scripted)), stage="output")],
        stream_guard=StreamGuard(mode="chunked", chunk_chars=24, overlap_chars=32),
    )
    events, error = await run(agent)
    assert isinstance(error, GuardrailTripwireTriggered)
    text = shown(events)
    assert text and "blocks" not in text and reply.startswith(text)
    assert len(seen) >= 2  # one judge call per window


async def test_judge_over_its_token_cap_fails_closed_and_shows_nothing() -> None:
    long_reply = "word " * 2000  # far over context_tokens=3000 in one held reply
    judge_model = FakeModel([ALLOW])
    agent = Agent(
        name="A",
        instructions="i",
        model=FakeModel([long_reply], stream=True, chunk_size=50),
        output_guardrails=[
            as_guardrail(judge(judge_model, context_tokens=3000, on_error="allow"), stage="output")
        ],
    )
    events, error = await run(agent)
    assert isinstance(error, GuardrailTripwireTriggered)
    assert error.info["error"] == "ContentTooLong"  # a hard error trips even with "allow"
    assert shown(events) == "" and judge_model.calls == []


async def test_chunked_windows_fit_a_judge_cap_that_the_whole_reply_exceeds() -> None:
    """Windows stay small, but the final whole-answer check still applies the cap: the run
    fails closed after the checked windows, and the reply never completes unchecked."""
    long_reply = "word " * 2000
    judge_model = FakeModel([ALLOW] * 1000)
    agent = Agent(
        name="A",
        instructions="i",
        model=FakeModel([long_reply], stream=True, chunk_size=50),
        output_guardrails=[as_guardrail(judge(judge_model, context_tokens=3000), stage="output")],
        stream_guard=StreamGuard(mode="chunked", chunk_chars=400, overlap_chars=40),
    )
    events, error = await run(agent)
    assert isinstance(error, GuardrailTripwireTriggered)
    assert error.info["error"] == "ContentTooLong"
    assert 0 < len(shown(events)) < len(long_reply)
    assert not any(e.type == T.FINAL_OUTPUT for e in events)
    tripped = next(e for e in events if e.type == T.GUARDRAIL_TRIPPED)
    assert tripped.data["phase"] == "final" and tripped.data["shown"] is True
