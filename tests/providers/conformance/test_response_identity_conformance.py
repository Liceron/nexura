"""P38 C2 review (m10): each adapter reports the response id and model from its recorded
fixture, and tracing names its provider. Recorded fixtures through a mock transport; no
network.
"""

from __future__ import annotations

from typing import Any

import pytest
from conformance_harness import Replay, load
from nexura_agents import tracing
from nexura_agents.models.infer import infer_model
from nexura_agents.providers.anthropic import AnthropicNativeProvider
from nexura_agents.providers.gemini import GeminiNativeProvider
from nexura_agents.providers.openai import OpenAIProvider
from nexura_agents.providers.openai.responses import OpenAIResponsesProvider
from nexura_agents.providers.sarvam import SarvamProvider
from nexura_agents.providers.types import GenerationRequest
from nexura_agents.types import Message


def ask() -> GenerationRequest:
    return GenerationRequest(
        messages=[Message(role="system", content="Be brief."), Message(role="user", content="Hi")]
    )


def ollama(replay: Replay) -> Any:
    model = infer_model("ollama:qwen3:0.6b")
    assert isinstance(model, OpenAIProvider)
    model.transport = replay.transport
    return model


BUILDERS: dict[str, tuple[Any, str, str, str | None]] = {
    # fixture dir: (builder, id key, model key, expected gen_ai.provider.name)
    "anthropic": (
        lambda r: AnthropicNativeProvider(
            "claude-haiku-5-5", api_key="sk-ant-api03-conformance0000", transport=r.transport
        ),
        "id",
        "model",
        "anthropic",
    ),
    "gemini": (
        lambda r: GeminiNativeProvider(
            "gemini-3.8-flash", api_key="AIzaSyConformance000000", transport=r.transport
        ),
        "responseId",
        "modelVersion",
        "gcp.gemini",
    ),
    "openai_chat": (
        lambda r: OpenAIProvider(
            "gpt-5.4-nano", api_key="sk-test-conformance-0000000000", transport=r.transport
        ),
        "id",
        "model",
        "openai",
    ),
    "openai_responses": (
        lambda r: OpenAIResponsesProvider(
            "gpt-5.4-nano", api_key="sk-test-responses-000000000000", transport=r.transport
        ),
        "id",
        "model",
        "openai",
    ),
    "sarvam": (
        lambda r: SarvamProvider(api_key="sk_sarvam_conformance_000000", transport=r.transport),
        "id",
        "model",
        "sarvam",
    ),
    # A local OpenAI-compatible server: no well-known provider, so none is claimed.
    "ollama": (ollama, "id", "model", None),
}


@pytest.mark.parametrize("fixture", sorted(BUILDERS))
async def test_response_identity_and_provider(fixture: str) -> None:
    build, id_key, model_key, provider = BUILDERS[fixture]
    body = load(fixture, "text")["response"]["json"]
    expected_id, expected_model = body.get(id_key), body.get(model_key)
    assert isinstance(expected_id, str) and isinstance(expected_model, str), body.keys()
    adapter = build(Replay(fixture, "text"))
    resp = await adapter.generate(ask())
    assert resp.response_id == expected_id
    assert resp.response_model == expected_model
    assert tracing.provider_name(adapter) == provider


async def test_vertex_is_named_vertex_ai() -> None:
    pytest.importorskip("nexura_vertex")
    from nexura_vertex import VertexAIProvider

    vertex = VertexAIProvider(
        "gemini-3.8-flash",
        project="demo",
        token_provider=lambda: "tok",
        transport=Replay("gemini", "text").transport,
    )
    assert tracing.provider_name(vertex) == "gcp.vertex_ai"
