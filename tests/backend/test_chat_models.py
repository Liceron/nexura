"""Chat playground model allowlist, lazy resolution, limits and delivery (injected fakes only)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from nexura_agents.limits import UsageLimits
from nexura_agents.models.fake import FakeModel
from nexura_agents.providers.fake import StreamingFakeProvider
from nexura_agents.providers.types import StreamDelta
from nexura_agents.types import Usage

from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.service.api.chat_models import (
    chat_models_from_environment,
    parse_chat_model_specs,
    parse_chat_models_env,
)
from nexura_runtime.studio.store import StudioStore

SECRET = "sk-super-secret-key-material"


def _client(tmp_path: Any, **kwargs: Any) -> tuple[Any, AsyncClient]:
    app = create_api_app(store=StudioStore(tmp_path / "chat.sqlite3"), **kwargs)
    return app, AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _streaming_provider() -> StreamingFakeProvider:
    return StreamingFakeProvider(
        queued_deltas=[
            [
                StreamDelta(kind="text_delta", text="Hello "),
                StreamDelta(kind="text_delta", text="streamed world"),
                StreamDelta(kind="finish", finish_reason="stop"),
            ]
        ]
    )


def _sse_events(text: str) -> list[tuple[str, dict[str, Any]]]:
    out = []
    for block in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        out.append((lines["event"], json.loads(lines["data"])))
    return out


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests must never reach a real provider: any outbound HTTP attempt fails."""
    import httpx

    async def refuse(self: Any, request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"test tried a real network call to {request.url.host}")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", refuse)


@pytest.mark.asyncio
async def test_default_remains_fake(tmp_path: Any) -> None:
    _, client = _client(tmp_path, chat_models={"test-real": FakeModel(["real reply"])})
    async with client:
        resp = await client.post("/api/v1/sessions/s1/chat", json={"message": "hi"})
    body = resp.json()
    assert resp.status_code == 200
    assert body["model"] == "fake-deterministic (Local Offline Fixture)"
    assert body["delivery"] == "final_only"


@pytest.mark.asyncio
async def test_allowlist_enforced(tmp_path: Any) -> None:
    _, client = _client(tmp_path, chat_models={"test-real": FakeModel(["x"])})
    async with client:
        resp = await client.post(
            "/api/v1/sessions/s1/chat", json={"message": "hi", "model": "openai:gpt-4o"}
        )
        stream = await client.post(
            "/api/v1/sessions/s1/chat/stream", json={"message": "hi", "model": "nope"}
        )
    for r in (resp, stream):
        assert r.status_code == 400
        detail = r.json()["detail"]
        assert detail["code"] == "MODEL_NOT_AVAILABLE"
        assert "fake-deterministic" in detail["message"]
        assert "test-real" in detail["message"]


@pytest.mark.asyncio
async def test_injected_model_used_and_reported(tmp_path: Any) -> None:
    model = FakeModel(["from the real model"], default_usage=Usage())
    _, client = _client(tmp_path, chat_models={"test-real": model})
    async with client:
        resp = await client.post(
            "/api/v1/sessions/s1/chat", json={"message": "hi", "model": "test-real"}
        )
    body = resp.json()
    assert resp.status_code == 200
    assert body["reply"] == "from the real model"
    assert body["model"] == "test-real (custom)"
    assert body["provider"] == "custom"
    assert body["delivery"] == "final_only"
    assert body["usage"]["total_tokens"] is None
    assert model.calls and model.calls[0]["tools"]


@pytest.mark.asyncio
async def test_models_listing_has_no_secrets(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", SECRET)
    _, client = _client(
        tmp_path,
        chat_models={"gpt": "openai:gpt-4o-mini", "test-real": _streaming_provider()},
    )
    async with client:
        resp = await client.get("/api/v1/chat/models")
    assert resp.status_code == 200
    items = {m["name"]: m for m in resp.json()}
    assert set(items) == {"fake-deterministic", "gpt", "test-real"}
    assert items["fake-deterministic"]["default"] is True
    assert items["fake-deterministic"]["offline"] is True
    assert items["gpt"]["provider"] == "openai" and items["gpt"]["streaming"] is True
    assert items["test-real"]["streaming"] is True
    assert sum(m["default"] for m in items.values()) == 1
    assert SECRET not in resp.text
    assert "gpt-4o-mini" not in resp.text


@pytest.mark.asyncio
async def test_lazy_resolution_failure_is_503_without_secrets(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
    # Built lazily: app creation and listing succeed even though the key is missing.
    _, client = _client(tmp_path, chat_models={"gpt": "openai:gpt-4o-mini"})
    async with client:
        assert (await client.get("/api/v1/chat/models")).status_code == 200
        resp = await client.post("/api/v1/sessions/s1/chat", json={"message": "hi", "model": "gpt"})
        stream = await client.post(
            "/api/v1/sessions/s2/chat/stream", json={"message": "hi", "model": "gpt"}
        )
        fake = await client.post("/api/v1/sessions/s3/chat", json={"message": "hi"})
    for r in (resp, stream):
        assert r.status_code == 503
        assert r.json()["detail"]["code"] == "MODEL_UNAVAILABLE"
        assert SECRET not in r.text
    assert fake.status_code == 200


@pytest.mark.asyncio
async def test_usage_limit_enforced(tmp_path: Any) -> None:
    heavy = FakeModel(
        ["big"], default_usage=Usage(prompt_tokens=60, completion_tokens=60, total_tokens=120)
    )
    _, client = _client(
        tmp_path,
        chat_models={"heavy": heavy},
        chat_usage_limits=UsageLimits(total_tokens_limit=100),
    )
    async with client:
        resp = await client.post(
            "/api/v1/sessions/s1/chat", json={"message": "hi", "model": "heavy"}
        )
        ok = await client.post("/api/v1/sessions/s2/chat", json={"message": "hi"})
    assert resp.status_code == 429
    assert resp.json()["detail"]["code"] == "USAGE_LIMIT_EXCEEDED"
    assert ok.status_code == 200


@pytest.mark.asyncio
async def test_streamed_delivery_flag(tmp_path: Any) -> None:
    _, client = _client(
        tmp_path,
        chat_models={"test-real": _streaming_provider(), "plain": FakeModel(["whole reply"])},
    )
    async with client:
        resp = await client.post(
            "/api/v1/sessions/s1/chat/stream", json={"message": "hi", "model": "test-real"}
        )
        plain = await client.post(
            "/api/v1/sessions/s2/chat/stream", json={"message": "hi", "model": "plain"}
        )
        fake = await client.post("/api/v1/sessions/s3/chat/stream", json={"message": "hi"})
    events = _sse_events(resp.text)
    assert any(e[1].get("type") == "TEXT_DELTA" for e in events if e[0] == "run_event")
    done = [d for n, d in events if n == "chat_completed"][0]
    assert done["delivery"] == "streamed"
    assert done["reply"] == "Hello streamed world"
    assert done["model"] == "test-real (custom)"
    for r in (plain, fake):
        final = [d for n, d in _sse_events(r.text) if n == "chat_completed"][0]
        assert final["delivery"] == "final_only"


def test_spec_parsers() -> None:
    assert parse_chat_model_specs(["a=openai:gpt-4o", " b = vertex:gemini-2.5-flash "]) == {
        "a": "openai:gpt-4o",
        "b": "vertex:gemini-2.5-flash",
    }
    assert parse_chat_models_env("a=openai:x, b=ollama:llama3,") == {
        "a": "openai:x",
        "b": "ollama:llama3",
    }
    assert parse_chat_models_env(None) == {}
    for bad in ("noequals", "=openai:x", "a=nocolon", "a=:x", "a=openai:"):
        with pytest.raises(ValueError):
            parse_chat_model_specs([bad])


def test_cli_wins_over_env() -> None:
    merged = chat_models_from_environment(
        ["a=openai:cli"],
        {"NEXURA_CHAT_MODELS": "a=openai:env,b=ollama:llama3"},
    )
    assert merged == {"a": "openai:cli", "b": "ollama:llama3"}


def test_reserved_default_name_rejected(tmp_path: Any) -> None:
    with pytest.raises(ValueError):
        create_api_app(
            store=StudioStore(tmp_path / "x.sqlite3"),
            chat_models={"fake-deterministic": FakeModel(["x"])},
        )


class _LeakyModel(FakeModel):
    async def generate(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError(f"401 Incorrect API key provided: {SECRET}")


@pytest.mark.asyncio
async def test_provider_error_text_is_not_streamed_or_returned(tmp_path: Any) -> None:
    _, client = _client(tmp_path, chat_models={"leaky": _LeakyModel(["x"])})
    async with client:
        stream = await client.post(
            "/api/v1/sessions/s1/chat/stream", json={"message": "hi", "model": "leaky"}
        )
        plain = await client.post(
            "/api/v1/sessions/s2/chat", json={"message": "hi", "model": "leaky"}
        )
    assert SECRET not in stream.text
    failed = [d for e, d in _sse_events(stream.text) if e == "chat_failed"]
    assert failed and failed[0]["code"] == "MODEL_PROVIDER_ERROR"
    assert plain.status_code == 502
    assert SECRET not in plain.text
    assert plain.json()["detail"]["code"] == "MODEL_PROVIDER_ERROR"


@pytest.mark.asyncio
async def test_local_owner_uses_configured_and_fake_models_despite_viewer_header(
    tmp_path: Any,
) -> None:
    model = FakeModel(["local configured reply"], repeat_last=True)
    _, client = _client(tmp_path, chat_models={"test-configured": model})
    viewer = {"x-principal-id": "mallory", "x-principal-role": "viewer"}
    async with client:
        for suffix in ("chat", "chat/stream"):
            r = await client.post(
                f"/api/v1/sessions/s1/{suffix}",
                json={"message": "hi", "model": "test-configured"},
                headers=viewer,
            )
            assert r.status_code == 200, r.text
            assert "local configured reply" in r.text
        fake = await client.post("/api/v1/sessions/s1/chat", json={"message": "hi"}, headers=viewer)
        assert fake.status_code == 200, fake.text
        assert fake.json()["model"] == "fake-deterministic (Local Offline Fixture)"
    assert len(model.calls) == 2
