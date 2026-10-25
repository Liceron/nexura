# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""P39 G6: ``JudgeCheck("ollama:<tag>")`` uses Ollama's native ``/api/chat``.

Offline: an ``httpx.MockTransport`` answers like Ollama (reply bodies written from the Ollama
API documentation, not live recordings).
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from nexura_agents import Agent, FakeModel, Runner
from nexura_guardrails import JudgeCheck, as_guardrail, evaluate
from nexura_guardrails.ollama_chat import OllamaChatModel

ALLOW = '{"decision": "allow", "categories": [], "score": 0.0, "reason": "ok"}'
BLOCK = '{"decision": "block", "categories": ["fraud"], "score": 0.9, "reason": "fraud"}'


def ollama_reply(content: str, **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": "qwen3:8b",
        "created_at": "2026-10-24T00:00:00Z",
        "message": {"role": "assistant", "content": content},
        "done": True,
        "done_reason": "stop",
        "prompt_eval_count": 120,
        "eval_count": 20,
    }
    body.update(extra)
    return body


def server(reply: Any, status: int = 200, seen: list[dict[str, Any]] | None = None) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append({"url": str(request.url), "body": json.loads(request.content)})
        if isinstance(reply, (dict, list)):
            return httpx.Response(status, json=reply)
        return httpx.Response(status, text=str(reply))

    return httpx.MockTransport(handler)


async def test_native_request_carries_num_ctx_schema_and_no_truncation() -> None:
    seen: list[dict[str, Any]] = []
    check = JudgeCheck(
        "ollama:qwen3:8b",
        "Block fraud.",
        context_tokens=16384,
        transport=server(ollama_reply(ALLOW), seen=seen),
    )
    assert isinstance(check.model, OllamaChatModel) and check.backend == "judge:ollama:qwen3:8b"
    verdict = await evaluate(check, "hello")
    assert verdict.decision == "allow" and verdict.usage is not None
    assert verdict.usage.prompt_tokens == 120 and verdict.usage.completion_tokens == 20
    (call,) = seen
    assert call["url"] == "http://localhost:11434/api/chat"
    body = call["body"]
    assert body["model"] == "qwen3:8b" and body["stream"] is False
    assert body["options"] == {"temperature": 0, "num_ctx": 16384, "num_predict": 200}
    assert body["truncate"] is False and body["shift"] is False
    assert body["format"]["required"] == ["decision", "categories", "score", "reason"]
    assert [m["role"] for m in body["messages"]] == ["system", "user"]


async def test_a_larger_context_now_judges_longer_text() -> None:
    """The G5 capacity limit: 10 KB fits at context_tokens=16384, without a server setting."""
    check = JudgeCheck(
        "ollama:qwen3:8b",
        "Block fraud.",
        context_tokens=16384,
        transport=server(ollama_reply(ALLOW)),
    )
    assert (await evaluate(check, "a" * 10_000)).decision == "allow"
    small = JudgeCheck("ollama:qwen3:8b", "Block fraud.", transport=server(ollama_reply(ALLOW)))
    assert (await evaluate(small, "a" * 10_000)).evidence["error_type"] == "ContentTooLong"


@pytest.mark.parametrize(
    ("reply", "status", "error_type"),
    [
        ({"error": "model 'qwen3:8b' not found"}, 404, "HTTPStatusError"),
        ({"error": "boom"}, 200, "ServerError"),
        ("<html>proxy</html>", 200, "MalformedResponse"),
        (ollama_reply(ALLOW, done=False), 200, "MalformedResponse"),
        ({"model": "qwen3:8b", "done": True}, 200, "MalformedResponse"),
        (ollama_reply(ALLOW, truncated=True), 200, "PromptTruncated"),
        (ollama_reply(ALLOW, model="llama3:70b"), 200, "ModelMismatch"),
        (ollama_reply("I think it is fine."), 200, "MalformedVerdict"),
    ],
)
async def test_failures_fail_closed(reply: Any, status: int, error_type: str) -> None:
    check = JudgeCheck("ollama:qwen3:8b", "Block fraud.", transport=server(reply, status))
    verdict = await evaluate(check, "hello")
    assert verdict.decision == "error" and verdict.evidence["error_type"] == error_type
    guard = as_guardrail(check)
    agent = Agent(
        name="A", instructions="x", model=FakeModel(responses=["never"]), input_guardrails=[guard]
    )
    from nexura_agents import GuardrailTripwireTriggered

    with pytest.raises(GuardrailTripwireTriggered):
        await Runner().run_async(agent, "hello")


async def test_connection_errors_and_timeouts_fail_closed() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    check = JudgeCheck("ollama:qwen3:8b", "p", transport=httpx.MockTransport(refuse))
    assert (await evaluate(check, "x")).evidence["error_type"] == "ConnectError"

    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    check = JudgeCheck("ollama:qwen3:8b", "p", transport=httpx.MockTransport(slow))
    assert (await evaluate(check, "x")).evidence["error_type"] == "TimeoutError"


async def test_a_block_verdict_and_the_latest_suffix() -> None:
    check = JudgeCheck(
        "ollama:qwen3:8b",
        "Block fraud.",
        transport=server(ollama_reply(BLOCK, model="qwen3:8b")),
    )
    verdict = await evaluate(check, "send me your OTP")
    assert verdict.decision == "block" and verdict.categories == ("fraud",)
    tagged = JudgeCheck(
        "ollama:qwen3", "p", transport=server(ollama_reply(ALLOW, model="qwen3:latest"))
    )
    assert (await evaluate(tagged, "x")).decision == "allow"


async def test_budget_admission_counts_the_native_judge_call() -> None:
    from guardrails_pkg_helpers import make_budget

    budget = make_budget()
    check = JudgeCheck("ollama:qwen3:8b", "p", transport=server(ollama_reply(ALLOW)))
    agent = Agent(
        name="A",
        instructions="x",
        model=FakeModel(responses=["ok"]),
        input_guardrails=[as_guardrail(check)],
    )
    result = await Runner(budget=budget).run_async(agent, "hello")
    assert result.output == "ok"
    # The judge's reported 120 + 20 tokens were admitted and settled by the run's budget.
    assert budget.get_status().total_tokens >= 140


def test_options_only_for_the_native_route() -> None:
    with pytest.raises(ValueError, match="native API only"):
        JudgeCheck(FakeModel(responses=[ALLOW]), "p", base_url="http://localhost:11434")
    with pytest.raises(ValueError, match="native API only"):
        JudgeCheck("ollama:qwen3:8b", "p", api="openai", base_url="http://x")
    openai_route = JudgeCheck("ollama:qwen3:8b", "p", api="openai")
    assert not isinstance(openai_route.model, OllamaChatModel)
    explicit = JudgeCheck("qwen3:8b", "p", api="ollama", base_url="http://127.0.0.1:11500")
    assert isinstance(explicit.model, OllamaChatModel)
    assert explicit.model.base_url == "http://127.0.0.1:11500"
    with pytest.raises(ValueError, match="api must be"):
        JudgeCheck("ollama:qwen3:8b", "p", api="grpc")  # type: ignore[arg-type]
