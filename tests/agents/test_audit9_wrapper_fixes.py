"""Audit 9 regressions: streaming through Retry/Fallback wrappers, docs claims, emit copy."""

import asyncio
import pathlib
import threading
from typing import Any

import httpx
import pytest
from nexura_agents import Agent, AgentHooks, FakeModel, Message, Runner
from nexura_agents.errors import ProviderAuthError, ProviderRequestError
from nexura_agents.events import RunEventType
from nexura_agents.models.openai import OpenAIChatModel
from nexura_agents.models.ratelimit import RateLimitedModel, RateLimiter
from nexura_agents.models.resilience import FallbackModel, RetryModel
from nexura_agents.providers.errors import ProviderServerError
from nexura_agents.providers.types import ProviderCapabilities, StreamDelta
from nexura_agents.run_context import RunContext

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _limiter() -> RateLimiter:
    return RateLimiter(max_concurrent=2)


def _mk(kind: str, inner):
    return {
        "bare": lambda: inner,
        "rl": lambda: RateLimitedModel(inner, _limiter()),
        "retry": lambda: RetryModel(inner),
        "fb": lambda: FallbackModel(inner),
        "rl_retry": lambda: RateLimitedModel(RetryModel(inner), _limiter()),
        "retry_rl": lambda: RetryModel(RateLimitedModel(inner, _limiter())),
        "fb_rl": lambda: FallbackModel(RateLimitedModel(inner, _limiter()), inner),
    }[kind]()


@pytest.mark.parametrize("kind", ["bare", "rl", "retry", "fb", "rl_retry", "retry_rl", "fb_rl"])
def test_streaming_survives_wrapper(kind):
    model = _mk(kind, FakeModel(["hello world, long text"], stream=True))

    async def go():
        agent = Agent(name="a", model=model, instructions="x")
        return [e async for e in Runner().stream_async(agent, "hi", stream_text=True)]

    assert any(e.type == RunEventType.TEXT_DELTA for e in asyncio.run(go())), kind


class _Flaky:
    """Streams ``plan`` per call: a list of deltas, optionally ending in an exception."""

    capabilities = ProviderCapabilities(can_stream=True)

    def __init__(self, *plans):
        self.plans = list(plans)
        self.calls = 0

    async def generate(self, *a, **k):  # pragma: no cover
        raise AssertionError

    async def stream(self, messages, tools=None, *, timeout=None):
        plan = self.plans[self.calls]
        self.calls += 1
        for item in plan:
            if isinstance(item, BaseException):
                raise item
            yield StreamDelta(kind="text_delta", text=item)


async def _collect(model):
    return [d.text async for d in model.stream([Message(role="user", content="x")], None)]


def test_retry_stream_retries_before_first_delta():
    inner = _Flaky([ProviderRequestError("down")], ["ok"])
    model = RetryModel(inner, base_delay=0.001, max_delay=0.01)
    assert asyncio.run(_collect(model)) == ["ok"]
    assert inner.calls == 2


def test_retry_stream_does_not_retry_after_first_delta():
    inner = _Flaky(["a", ProviderRequestError("mid")], ["a", "b"])
    model = RetryModel(inner, base_delay=0.001, max_delay=0.01)
    got = []

    async def go():
        async for d in model.stream([Message(role="user", content="x")], None):
            got.append(d.text)

    with pytest.raises(ProviderRequestError):
        asyncio.run(go())
    assert got == ["a"]
    assert inner.calls == 1


def test_retry_stream_never_retries_auth_and_gives_up():
    inner = _Flaky([ProviderAuthError("bad key")], ["ok"])
    with pytest.raises(ProviderAuthError):
        asyncio.run(_collect(RetryModel(inner, base_delay=0.001)))
    assert inner.calls == 1
    inner = _Flaky([ProviderRequestError("1")], [ProviderRequestError("2")])
    with pytest.raises(ProviderRequestError):
        asyncio.run(_collect(RetryModel(inner, max_attempts=2, base_delay=0.001)))
    assert inner.calls == 2


def test_fallback_stream_switches_only_before_first_delta():
    first = _Flaky([ProviderRequestError("down")])
    second = _Flaky(["x", "y"])
    model = FallbackModel(first, second)
    assert asyncio.run(_collect(model)) == ["x", "y"]
    assert model.last_used is second

    mid = _Flaky(["a", ProviderRequestError("mid")])
    spare = _Flaky(["z"])
    with pytest.raises(ProviderRequestError):
        asyncio.run(_collect(FallbackModel(mid, spare)))
    assert spare.calls == 0


def test_fallback_capabilities_need_every_model_to_stream():
    class NoStream:
        capabilities = ProviderCapabilities(can_stream=False)

    assert FallbackModel(_Flaky([]), _Flaky([])).capabilities.can_stream is True
    assert FallbackModel(_Flaky([]), NoStream()).capabilities.can_stream is False  # type: ignore[arg-type]


def _openai_with_status(status: int, headers: dict | None = None):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(status, headers=headers or {}, json={"error": "x"})

    model = OpenAIChatModel("m", api_key="k", transport=httpx.MockTransport(handler))
    return model, calls


def test_builtin_provider_429_sets_retry_after():
    model, _ = _openai_with_status(429, {"Retry-After": "2"})
    with pytest.raises(Exception) as info:
        asyncio.run(model.generate([Message(role="user", content="hi")]))
    assert getattr(info.value, "retry_after", None) == 2.0


def test_http_5xx_is_retried_by_default():
    model, calls = _openai_with_status(503)
    retry = RetryModel(model, max_attempts=3, base_delay=0.001, max_delay=0.01)
    with pytest.raises(ProviderServerError):
        asyncio.run(retry.generate([Message(role="user", content="hi")]))
    assert calls["n"] == 3


def test_docs_match_behaviour():
    providers = " ".join((ROOT / "docs/providers.md").read_text().split())
    assert "do not set that attribute" not in providers
    assert (
        "A plain `ProviderError` (what the built-in providers raise for HTTP 5xx" not in providers
    )
    # The comparison table moved from the package README to docs/comparisons.md (P34).
    comparisons = (ROOT / "docs/comparisons.md").read_text()
    row = next(ln for ln in comparisons.splitlines() if "100+ providers through LiteLLM" in ln)
    assert "non-streaming" not in row


def test_emit_copies_each_value_separately():
    shared = [1]

    class Hooks(AgentHooks):
        async def before_model(self, ctx: RunContext, messages):
            if ctx.turn <= 1:
                ctx.emit("p", items=shared, lock=threading.Lock())
                shared.append(2)
            return None

    agent = Agent(name="a", instructions="", model=FakeModel(responses=["ok"]))
    result = Runner(hooks=Hooks()).run(agent, "x")
    customs = [e for e in result.events if e.type == RunEventType.CUSTOM]
    assert customs and customs[0].data["data"]["items"] == [1]


async def test_litellm_stream_errors_never_echo_extra_header_values() -> None:
    import pytest
    from nexura_agents.errors import ProviderError
    from nexura_agents.models.litellm import LiteLLMModel
    from nexura_agents.types import Message

    secret = "gw-secret-9f8e7d6c"

    async def completion(**kwargs: Any) -> Any:
        async def chunks() -> Any:
            yield {"error": {"message": f"bad gateway key {secret}", "code": 400}}

        return chunks()

    model = LiteLLMModel(
        "openai/gpt-x", completion=completion, extra_headers={"X-Gateway-Key": secret}
    )
    with pytest.raises(ProviderError) as caught:  # a 400 in-stream error
        async for _ in model.stream([Message(role="user", content="hi")]):
            pass
    assert secret not in str(caught.value)
