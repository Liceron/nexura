"""Regression tests for the third audit (agents/interactions/packaging)."""

from __future__ import annotations

import asyncio
import json
from importlib import metadata

import httpx
import pytest
from nexura_agents import (
    A2AClient,
    A2AServer,
    Agent,
    FakeModel,
    HashingEmbedder,
    InMemoryMemoryStore,
    Message,
    RunInterruption,
    Runner,
    SequentialAgent,
    ToolCall,
    Usage,
    UsageLimits,
    tool,
)


@tool(requires_approval=True)
def refund(order_id: str) -> str:
    """Refund."""
    return "ok " + order_id


@pytest.mark.asyncio
async def test_team_resume_keeps_server_runner_limits() -> None:
    bot = Agent(
        "Refunds",
        "Help.",
        FakeModel(
            responses=[[ToolCall(id="r1", name="refund", arguments={"order_id": "A"})], "done"]
        ),
        tools=[refund],
    )
    team = SequentialAgent("T", [bot])
    server = A2AServer(
        team, url="http://a2a.test/", runner=Runner(usage_limits=UsageLimits(request_limit=1))
    )
    client = A2AClient("http://a2a.test", transport=httpx.ASGITransport(app=server.http_app()))
    task = await client.send("refund A")
    assert task.state == "input-required"
    done = await client.decide(task.id, approve=True)
    assert done.state == "failed", done.state


class _SlowEmbedder(HashingEmbedder):
    async def embed(self, texts):  # type: ignore[no-untyped-def]
        await asyncio.sleep(0.01)
        return await super().embed(texts)


@pytest.mark.asyncio
async def test_concurrent_duplicate_add_collapses() -> None:
    store = InMemoryMemoryStore(_SlowEmbedder())
    a, b = await asyncio.gather(store.add("u", "likes tea"), store.add("u", "likes  tea"))
    assert len(await store.list("u")) == 1
    assert a.id == b.id


def _interruption(usage: Usage) -> RunInterruption:
    call = ToolCall(id="c", name="t", arguments={})
    msgs = [
        Message(role="user", content="x"),
        Message(role="assistant", content=None, tool_calls=[call]),
    ]
    return RunInterruption("r", "A", msgs, [call], usage=usage)


def test_interruption_usage_roundtrip_keeps_cache_and_reasoning() -> None:
    u = Usage(
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
        cached_prompt_tokens=4,
        cache_read_input_tokens=3,
        cache_creation_input_tokens=2,
        reasoning_tokens=1,
    )
    assert RunInterruption.from_json(_interruption(u).to_json()).usage == u


def test_interruption_reads_old_usage_payload() -> None:
    raw = json.loads(_interruption(Usage(prompt_tokens=1)).to_json())
    raw["usage"] = {
        "prompt_tokens": 1,
        "completion_tokens": 2,
        "total_tokens": 3,
        "is_complete": True,
    }
    usage = RunInterruption.from_json(json.dumps(raw)).usage
    assert usage == Usage(prompt_tokens=1, completion_tokens=2, total_tokens=3)


def test_otlp_scope_version_comes_from_installed_package() -> None:
    from nexura_runtime.observability.otlp import export_spans_otlp_payload

    payload = export_spans_otlp_payload([], "svc")
    scope = payload["resourceSpans"][0]["scopeSpans"][0]["scope"]
    assert scope["version"] == metadata.version("nexura-runtime")


def test_vertex_hint_names_installed_distribution(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins

    real = builtins.__import__

    def fake(name, *a, **k):  # type: ignore[no-untyped-def]
        if name.startswith("google.auth"):
            raise ImportError(name)
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake)
    from nexura_agents.errors import ProviderAuthError
    from nexura_vertex.provider import _AdcTokenSource

    with pytest.raises(ProviderAuthError) as ei:
        _AdcTokenSource()
    # google-auth is a dependency of the nexura-vertex package (the old `vertex` extra).
    assert "pip install nexura-vertex" in str(ei.value)
