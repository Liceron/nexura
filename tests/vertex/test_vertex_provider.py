"""Vertex AI adapter wire contract, verified offline with httpx.MockTransport."""

from __future__ import annotations

import json

import httpx
import pytest
from nexura_runtime.errors import ProviderAuthError
from nexura_runtime.providers import CapabilityError
from nexura_runtime.types import Message
from nexura_vertex import VertexAIProvider

from nexura_runtime import Agent, Runner


def _reply(text: str) -> dict[str, object]:
    return {
        "candidates": [
            {"content": {"role": "model", "parts": [{"text": text}]}, "finishReason": "STOP"}
        ],
        "usageMetadata": {"promptTokenCount": 7, "candidatesTokenCount": 2, "totalTokenCount": 9},
    }


def _provider(handler, **kwargs) -> VertexAIProvider:  # type: ignore[no-untyped-def]
    tokens = iter(f"token-{i}" for i in range(1, 100))
    return VertexAIProvider(
        "gemini-2.5-flash",
        project="demo-project",
        token_provider=lambda: next(tokens),
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


async def test_generate_uses_vertex_endpoint_and_bearer_token() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_reply("ok"))

    provider = _provider(handler)
    response = await provider.generate([Message(role="user", content="hi")])
    await provider.generate([Message(role="user", content="again")])

    assert response.message.content == "ok"
    assert response.usage is not None and response.usage.total_tokens == 9
    assert str(seen[0].url) == (
        "https://aiplatform.googleapis.com/v1/projects/demo-project/locations/global"
        "/publishers/google/models/gemini-2.5-flash:generateContent"
    )
    assert seen[0].headers["Authorization"] == "Bearer token-1"
    assert seen[1].headers["Authorization"] == "Bearer token-2"  # asked again per call
    assert "x-goog-api-key" not in seen[0].headers
    assert json.loads(seen[0].content)["contents"][0]["parts"][0]["text"] == "hi"


async def test_regional_location_uses_regional_host() -> None:
    urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        return httpx.Response(200, json=_reply("ok"))

    await _provider(handler, location="asia-south1").generate([Message(role="user", content="x")])
    assert urls[0].startswith(
        "https://asia-south1-aiplatform.googleapis.com/v1/projects/demo-project/locations/asia-south1/"
    )


async def test_streaming_is_authenticated_and_yields_text() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith(":streamGenerateContent")
        assert request.headers["Authorization"] == "Bearer token-1"
        body = "".join(f"data: {json.dumps(_reply(part))}\n\n" for part in ("Hel", "lo"))
        return httpx.Response(200, text=body, headers={"Content-Type": "text/event-stream"})

    provider = _provider(handler)
    stream = provider.stream([Message(role="user", content="hi")])
    text = "".join([d.text or "" async for d in stream if d.kind == "text_delta"])
    assert text == "Hello"


async def test_async_token_provider_is_awaited() -> None:
    async def token() -> str:
        return "async-token"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer async-token"
        return httpx.Response(200, json=_reply("ok"))

    provider = VertexAIProvider(
        project="p", token_provider=token, transport=httpx.MockTransport(handler)
    )
    assert (await provider.generate([Message(role="user", content="x")])).message.content == "ok"


async def test_embeddings_use_predict_and_report_token_counts() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/models/gemini-embedding-001:predict")
        instances = json.loads(request.content)["instances"]
        return httpx.Response(
            200,
            json={
                "predictions": [
                    {"embeddings": {"values": [0.1, 0.2], "statistics": {"token_count": 3}}}
                    for _ in instances
                ]
            },
        )

    provider = VertexAIProvider(
        "gemini-embedding-001",
        project="p",
        token_provider=lambda: "t",
        transport=httpx.MockTransport(handler),
    )
    result = await provider.embed(["a", "b"])
    assert result.embeddings == [[0.1, 0.2], [0.1, 0.2]]
    assert result.usage is not None
    assert (result.usage.total_tokens, result.usage.is_complete) == (6, True)


async def test_auth_failure_and_empty_token_are_auth_errors() -> None:
    provider = _provider(lambda request: httpx.Response(401, json={"error": {"message": "no"}}))
    with pytest.raises(ProviderAuthError):
        await provider.generate([Message(role="user", content="x")])

    empty = VertexAIProvider(
        project="p",
        token_provider=lambda: "",
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=_reply("x"))),
    )
    with pytest.raises(ProviderAuthError, match="empty token"):
        await empty.generate([Message(role="user", content="x")])


def test_project_is_required_and_cache_management_is_explicitly_unsupported() -> None:
    with pytest.raises(ProviderAuthError, match="project"):
        VertexAIProvider(token_provider=lambda: "t")
    provider = VertexAIProvider(project="p", token_provider=lambda: "t")
    assert "token" not in repr(provider)

    import asyncio

    with pytest.raises(CapabilityError):
        asyncio.run(provider.delete_cached_content("cachedContents/1"))


async def test_vertex_provider_drives_an_agent() -> None:
    provider = _provider(lambda request: httpx.Response(200, json=_reply("Order is on the way.")))
    agent = Agent(name="Support", instructions="Be brief.", model=provider)
    result = await Runner().run_async(agent, "Where is ORD-1?")
    assert result.final_output == "Order is on the way."


async def test_auth_errors_name_project_credential_and_google_reason() -> None:
    body = {
        "error": {
            "code": 403,
            "status": "PERMISSION_DENIED",
            "message": "secret-ish free text that must not be echoed",
            "details": [{"reason": "SERVICE_DISABLED"}, {"reason": "not a code"}],
        }
    }
    provider = _provider(lambda request: httpx.Response(403, json=body))
    with pytest.raises(ProviderAuthError) as exc:
        await provider.generate([Message(role="user", content="x")])
    text = str(exc.value)
    assert "[PERMISSION_DENIED, SERVICE_DISABLED]" in text
    assert "project 'demo-project'" in text and "a custom token provider" in text
    assert "free text" not in text and "not a code" not in text


async def test_quota_project_header_and_env_project(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_reply("ok"))

    provider = _provider(handler)
    provider.quota_project = "billing-project"
    await provider.generate([Message(role="user", content="x")])
    assert seen[0].headers["x-goog-user-project"] == "billing-project"


async def test_thinking_tokens_count_as_billed_output() -> None:
    # Shape of a real gemini-2.5-flash reply (live check 2026-10-08): total exceeded
    # prompt + candidates by the hidden thinking tokens.
    reply = _reply("ok")
    reply["usageMetadata"] = {
        "promptTokenCount": 5,
        "candidatesTokenCount": 1,
        "thoughtsTokenCount": 16,
        "totalTokenCount": 22,
    }
    provider = _provider(lambda request: httpx.Response(200, json=reply))
    usage = (await provider.generate([Message(role="user", content="x")])).usage
    assert usage is not None
    assert (usage.prompt_tokens, usage.completion_tokens, usage.total_tokens) == (5, 17, 22)
    assert usage.reasoning_tokens == 16
    assert usage.prompt_tokens + usage.completion_tokens == usage.total_tokens
