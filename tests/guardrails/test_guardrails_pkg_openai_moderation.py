# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""OpenAIModeration against an in-process fake of POST /v1/moderations (no network)."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from guardrails_pkg_helpers import run_ctx
from nexura_agents import Agent, FakeModel, GuardrailTripwireTriggered, Runner
from nexura_guardrails import OpenAIModeration, VerdictCache, as_guardrail
from nexura_guardrails.moderation import OPENAI_MODERATION_LABELS
from nexura_guardrails.verdict import CheckInput

KEY = "sk-test-moderation-secret-123"


def reply(
    flags: dict[str, bool] | None = None, scores: dict[str, float] | None = None, **extra: Any
) -> dict[str, Any]:
    flags = flags or {}
    scores = scores or {}
    result = {
        "flagged": any(flags.values()),
        "categories": {label: flags.get(label, False) for label in OPENAI_MODERATION_LABELS},
        "category_scores": {label: scores.get(label, 0.001) for label in OPENAI_MODERATION_LABELS},
        "category_applied_input_types": {label: ["text"] for label in OPENAI_MODERATION_LABELS},
    }
    body = {"id": "modr-1", "model": "omni-moderation-2024-09-26", "results": [result]}
    body.update(extra)
    return body


class Server:
    """Records requests and answers with a queued body or status."""

    def __init__(self, body: Any = None, status: int = 200) -> None:
        self.body = body if body is not None else reply()
        self.status = status
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if isinstance(self.body, bytes):
            return httpx.Response(self.status, content=self.body)
        return httpx.Response(self.status, json=self.body)

    @property
    def payload(self) -> dict[str, Any]:
        return json.loads(self.requests[-1].content)


def check(server: Server, **kwargs: Any) -> OpenAIModeration:
    return OpenAIModeration(api_key=KEY, transport=httpx.MockTransport(server), **kwargs)


def item(text: str = "hello", stage: str = "input", **kwargs: Any) -> CheckInput:
    return CheckInput(stage=stage, text=text, **kwargs)  # type: ignore[arg-type]


async def judge(guard: OpenAIModeration, value: CheckInput) -> Any:
    return await guard(run_ctx(), value)


async def test_request_follows_the_documented_api() -> None:
    server = Server()
    await judge(check(server), item("is this fine?"))
    request = server.requests[-1]
    assert request.method == "POST" and str(request.url) == "https://api.openai.com/v1/moderations"
    assert request.headers["authorization"] == f"Bearer {KEY}"
    assert server.payload == {"model": "omni-moderation-latest", "input": "is this fine?"}


async def test_clean_text_is_allowed_with_the_top_score() -> None:
    verdict = await judge(check(Server(reply(scores={"harassment": 0.02}))), item())
    assert verdict.decision == "allow" and verdict.categories == ()
    assert verdict.score == pytest.approx(0.02)
    assert verdict.backend == "openai-moderation:omni-moderation-latest"


@pytest.mark.parametrize(
    ("label", "category"),
    [
        ("violence/graphic", "violence"),
        ("illicit", "crime"),
        ("illicit/violent", "weapons"),
        ("sexual/minors", "child_safety"),
        ("hate/threatening", "hate"),
        ("self-harm/instructions", "self_harm"),
        ("harassment", "harassment"),
    ],
)
async def test_each_flagged_label_maps_to_the_nexura_category(label: str, category: str) -> None:
    verdict = await judge(check(Server(reply({label: True}, {label: 0.91}))), item("x"))
    assert verdict.decision == "block" and verdict.categories == (category,)
    assert verdict.evidence["labels"] == [label] and verdict.score == pytest.approx(0.91)


async def test_threshold_overrides_openai_flags_in_both_directions() -> None:
    below = reply({"violence": True}, {"violence": 0.4})
    assert (await judge(check(Server(below), threshold=0.5), item())).decision == "allow"
    above = reply({}, {"harassment": 0.6})
    verdict = await judge(check(Server(above), threshold=0.5), item())
    assert verdict.decision == "block" and verdict.categories == ("harassment",)


async def test_labels_limit_what_counts() -> None:
    body = reply({"sexual": True}, {"sexual": 0.9})
    assert (
        await judge(check(Server(body), labels=["violence", "hate"]), item())
    ).decision == "allow"
    with pytest.raises(ValueError):
        check(Server(), labels=["not-a-label"])


async def test_flagged_without_a_known_label_blocks_as_other() -> None:
    body = reply()
    body["results"][0]["flagged"] = True
    verdict = await judge(check(Server(body)), item())
    assert verdict.decision == "block" and verdict.categories == ("other",)


@pytest.mark.parametrize(
    ("server", "error_type"),
    [
        (Server(status=401), "HTTPStatusError"),
        (Server(status=429), "HTTPStatusError"),
        (Server(status=500), "HTTPStatusError"),
        (Server(b"not json"), "MalformedResponse"),
        (Server({"results": []}), "MalformedResponse"),
        (Server({"results": [{"flagged": "yes", "categories": {}}]}), "MalformedResponse"),
        (Server(reply(scores={"hate": 7.0})), "MalformedResponse"),
        (Server(json.dumps(reply()).replace("0.001", "NaN", 1).encode()), "MalformedResponse"),
        (Server(reply(model="text-moderation-007")), "ModelMismatch"),
    ],
)
async def test_failures_fail_closed_without_leaking_the_key(
    server: Server, error_type: str
) -> None:
    verdict = await judge(check(server), item("secret text"))
    assert verdict.decision == "error" and verdict.evidence["error_type"] == error_type
    rendered = repr(verdict) + json.dumps(dict(verdict.evidence), default=str) + verdict.reason
    assert KEY not in rendered and "secret text" not in rendered


async def test_on_error_softens_only_when_asked() -> None:
    assert (await judge(check(Server(status=503), on_error="allow"), item())).decision == "allow"
    assert (await judge(check(Server(status=503), on_error="review"), item())).decision == "review"


async def test_timeout_fails_closed() -> None:
    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, json=reply())

    guard = OpenAIModeration(api_key=KEY, transport=httpx.MockTransport(slow), timeout=0.05)
    verdict = await judge(guard, item())
    assert verdict.decision == "error" and verdict.evidence["error_type"] == "TimeoutError"


async def test_oversized_input_is_never_sent_and_cannot_be_softened() -> None:
    server = Server()
    guard = check(server, max_input_tokens=10, on_error="allow")
    verdict = await judge(guard, item("x" * 200))
    assert verdict.decision == "error" and verdict.evidence == {
        "error_type": "ContentTooLong",
        "hard": True,
        "detail": verdict.evidence["detail"],
    }
    assert server.requests == []


async def test_huge_reply_is_refused() -> None:
    server = Server(b"{" + b" " * (300 * 1024) + b"}")
    verdict = await judge(check(server), item())
    assert verdict.decision == "error" and verdict.evidence["error_type"] == "ResponseTooLarge"


def test_key_is_required_https_enforced_and_never_shown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        OpenAIModeration()
    with pytest.raises(ValueError, match="https"):
        OpenAIModeration(api_key=KEY, base_url="http://api.example.com/v1")
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    guard = OpenAIModeration()
    assert KEY not in repr(guard) and KEY not in str(vars(guard).get("name"))


async def test_context_is_sent_only_when_asked_and_the_cache_reuses_verdicts() -> None:
    server = Server()
    value = item("answer", stage="output", role="agent", context="question")
    await judge(check(server), value)
    assert server.payload["input"] == "answer"
    await judge(check(server, include_context=True), value)
    assert server.payload["input"] == "question\n\nanswer"
    cache = VerdictCache()
    guard = check(server, cache=cache)
    first = await judge(guard, item("same"))
    second = await judge(guard, item("same"))
    assert len(server.requests) == 3 and second.cached and first.decision == second.decision


async def test_as_an_agent_guardrail_it_trips_on_flagged_input() -> None:
    server = Server(reply({"violence": True}, {"violence": 0.97}))
    agent = Agent(
        name="Support",
        instructions="Help.",
        model=FakeModel(responses=["never reached"]),
        input_guardrails=[as_guardrail(check(server))],
    )
    with pytest.raises(GuardrailTripwireTriggered):
        await Runner().run_async(agent, "a violent request")
    clean = Agent(
        name="Support",
        instructions="Help.",
        model=FakeModel(responses=["Hello!"]),
        input_guardrails=[as_guardrail(check(Server()))],
    )
    assert (await Runner().run_async(clean, "hi")).final_output == "Hello!"
