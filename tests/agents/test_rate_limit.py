"""RateLimiter / RateLimitedModel: concurrency cap, pacing, token budget, streams."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from nexura_agents import Agent, FakeModel, RetryModel, Runner
from nexura_agents.errors import ProviderRateLimitError, SDKValidationError
from nexura_agents.models.ratelimit import RateLimitedModel, RateLimiter
from nexura_agents.providers.types import StreamDelta
from nexura_agents.types import Message, ModelResponse, Usage


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)


class SlowModel:
    """Records peak concurrency; each call yields to the loop a few times."""

    def __init__(self, usage: Usage | None = None) -> None:
        self.active = 0
        self.peak = 0
        self.calls = 0
        self.usage = usage

    async def generate(self, messages: Any, tools: Any = None, *, timeout: Any = None) -> Any:
        self.calls += 1
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            for _ in range(3):
                await asyncio.sleep(0)
            return ModelResponse(message=Message(role="assistant", content="ok"), usage=self.usage)
        finally:
            self.active -= 1


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_concurrent": 0},
        {"max_concurrent": -1},
        {"max_concurrent": 1.5},
        {"max_concurrent": True},
        {"requests_per_minute": 0},
        {"requests_per_minute": "10"},
        {"tokens_per_minute": 0},
        {"tokens_per_minute": -5},
    ],
)
def test_invalid_arguments(kwargs: dict[str, Any]) -> None:
    with pytest.raises(SDKValidationError):
        RateLimiter(**kwargs)


def test_wrapper_requires_limiter() -> None:
    with pytest.raises(SDKValidationError):
        RateLimitedModel(FakeModel(), object())  # type: ignore[arg-type]


async def test_concurrency_never_exceeds_cap_across_runs() -> None:
    inner = SlowModel()
    limiter = RateLimiter(max_concurrent=2)
    agents = [
        Agent(name=f"a{i}", instructions="x", model=RateLimitedModel(inner, limiter))
        for i in range(12)
    ]
    results = await asyncio.gather(*(a.run_async("hi") for a in agents))
    assert all(r.final_output == "ok" for r in results)
    assert inner.calls == 12
    assert inner.peak == 2
    assert limiter.in_flight == 0


async def test_one_limiter_limits_different_models_together() -> None:
    a, b = SlowModel(), SlowModel()
    limiter = RateLimiter(max_concurrent=1)
    ma, mb = RateLimitedModel(a, limiter), RateLimitedModel(b, limiter)
    await asyncio.gather(*(m.generate([], None) for m in (ma, mb) * 4))
    assert a.peak == 1 and b.peak == 1
    assert a.calls + b.calls == 8
    assert a.active + b.active == 0


async def test_shared_across_event_loops_via_sync_run() -> None:
    limiter = RateLimiter(max_concurrent=1)
    agent = Agent(
        name="s", instructions="x", model=RateLimitedModel(FakeModel(["a", "b"]), limiter)
    )
    assert (await asyncio.to_thread(agent.run, "1")).final_output == "a"
    assert (await asyncio.to_thread(agent.run, "2")).final_output == "b"
    assert limiter.in_flight == 0


async def test_requests_per_minute_pacing_with_injected_clock() -> None:
    clock = FakeClock()
    limiter = RateLimiter(requests_per_minute=2, clock=clock, sleep=clock.sleep)
    model = RateLimitedModel(FakeModel(repeat_last=True, responses=["x"]), limiter)
    start = clock.now
    await model.generate([], None)
    await model.generate([], None)
    assert clock.slept == []  # a burst up to the limit is allowed
    await model.generate([], None)  # third call must wait for the first to age out
    assert clock.slept == [60.0]
    assert clock.now == start + 60.0
    await model.generate([], None)  # second start was at t0, so it is free now
    assert clock.slept == [60.0]
    await model.generate([], None)
    assert clock.slept == [60.0, 60.0]


async def test_tokens_per_minute_waits_after_budget_is_used() -> None:
    clock = FakeClock()
    limiter = RateLimiter(tokens_per_minute=100, clock=clock, sleep=clock.sleep)
    inner = FakeModel(
        repeat_last=True,
        responses=["x"],
        default_usage=Usage(prompt_tokens=40, completion_tokens=30, total_tokens=70),
    )
    model = RateLimitedModel(inner, limiter)
    await model.generate([], None)  # 70 used, under budget
    await model.generate([], None)  # starts freely; now 140 used
    assert clock.slept == []
    clock.now += 10
    await model.generate([], None)  # over budget: waits for the first 70 to expire
    assert clock.slept == [50.0]


async def test_calls_without_usage_do_not_use_token_budget() -> None:
    clock = FakeClock()
    limiter = RateLimiter(tokens_per_minute=10, clock=clock, sleep=clock.sleep)
    model = RateLimitedModel(FakeModel(repeat_last=True, responses=["x"]), limiter)
    for _ in range(5):
        await model.generate([], None)
    assert clock.slept == []


async def test_generate_error_releases_slot() -> None:
    def boom(messages: Any, tools: Any) -> Any:
        raise ProviderRateLimitError("429")

    limiter = RateLimiter(max_concurrent=1)
    model = RateLimitedModel(FakeModel(handler=boom), limiter)
    with pytest.raises(ProviderRateLimitError):
        await model.generate([], None)
    assert limiter.in_flight == 0


def _streaming(responses: list[str]) -> FakeModel:
    return FakeModel(responses, stream=True, default_usage=Usage(total_tokens=5))


async def test_wrapper_forwards_inner_attributes() -> None:
    inner = FakeModel(["x"], stream=True, native_schema=True)
    wrapped = RateLimitedModel(inner, RateLimiter())
    assert wrapped.capabilities is inner.capabilities
    assert wrapped.capabilities.can_stream
    assert wrapped.supports_response_schema is True
    assert wrapped.accepts_model_settings is True
    assert wrapped.streams_response_schema is False
    assert RateLimitedModel(FakeModel(), RateLimiter()).capabilities is None


async def test_stream_holds_slot_until_finished() -> None:
    limiter = RateLimiter(max_concurrent=1)
    model = RateLimitedModel(_streaming(["hello world", "second"]), limiter)
    first = model.stream([], None)
    kinds = []
    async for delta in first:
        kinds.append(delta.kind)
        assert limiter.in_flight == 1
        if len(kinds) == 1:
            other = asyncio.create_task(model.generate([], None))
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert not other.done()  # blocked while the stream is open
    assert kinds[-1] == "finish"
    assert limiter.in_flight <= 1  # the waiting call may have taken the freed slot
    await other
    assert limiter.in_flight == 0


async def test_stream_releases_on_aclose() -> None:
    limiter = RateLimiter(max_concurrent=1)
    model = RateLimitedModel(_streaming(["hello world"]), limiter)
    stream = model.stream([], None)
    await stream.__anext__()
    assert limiter.in_flight == 1
    await stream.aclose()  # type: ignore[attr-defined]
    assert limiter.in_flight == 0


async def test_unstarted_stream_takes_nothing() -> None:
    limiter = RateLimiter(max_concurrent=1)
    model = RateLimitedModel(_streaming(["x"]), limiter)
    stream = model.stream([], None)
    assert limiter.in_flight == 0
    await stream.aclose()  # type: ignore[attr-defined]
    assert limiter.in_flight == 0


async def test_stream_releases_on_error() -> None:
    class Broken:
        async def stream(self, messages: Any, tools: Any = None, **kw: Any) -> AsyncIterator[Any]:
            yield StreamDelta(kind="text_delta", text="a")
            raise ProviderRateLimitError("mid-stream")

    limiter = RateLimiter(max_concurrent=1)
    model = RateLimitedModel(Broken(), limiter)  # type: ignore[arg-type]
    with pytest.raises(ProviderRateLimitError):
        async for _ in model.stream([], None):
            assert limiter.in_flight == 1
    assert limiter.in_flight == 0


async def test_stream_releases_on_cancellation() -> None:
    gate = asyncio.Event()

    class Hanging:
        async def stream(self, messages: Any, tools: Any = None, **kw: Any) -> AsyncIterator[Any]:
            yield StreamDelta(kind="text_delta", text="a")
            await gate.wait()
            yield StreamDelta(kind="finish")

    limiter = RateLimiter(max_concurrent=1)
    model = RateLimitedModel(Hanging(), limiter)  # type: ignore[arg-type]

    async def consume() -> None:
        async for _ in model.stream([], None):
            pass

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.01)
    assert limiter.in_flight == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert limiter.in_flight == 0


async def test_stream_usage_counts_toward_token_budget() -> None:
    clock = FakeClock()
    limiter = RateLimiter(tokens_per_minute=5, clock=clock, sleep=clock.sleep)
    model = RateLimitedModel(_streaming(["x"]), limiter)
    async for _ in model.stream([], None):
        pass
    async for _ in model.stream([], None):  # 5 tokens used = budget reached
        pass
    assert clock.slept == [60.0]


async def test_stream_forwards_model_settings_only_when_accepted() -> None:
    from nexura_agents import ModelSettings

    inner = _streaming(["x"])
    model = RateLimitedModel(inner, RateLimiter())
    async for _ in model.stream([], None, model_settings=ModelSettings(temperature=0.1)):
        pass
    assert inner.calls[-1]["model_settings"] == ModelSettings(temperature=0.1)

    class Plain:
        async def stream(self, messages: Any, tools: Any = None) -> AsyncIterator[Any]:
            yield StreamDelta(kind="finish")

    plain = RateLimitedModel(Plain(), RateLimiter())  # type: ignore[arg-type]
    async for _ in plain.stream([], None, model_settings=ModelSettings(temperature=0.1)):
        pass


async def test_agent_streams_through_limiter() -> None:
    limiter = RateLimiter(max_concurrent=1)
    agent = Agent(
        name="s", instructions="x", model=RateLimitedModel(_streaming(["streamed text"]), limiter)
    )
    events = [e async for e in Runner().stream_async(agent, "hi", stream_text=True)]
    assert any(getattr(e.type, "name", "") == "TEXT_DELTA" for e in events)
    assert limiter.in_flight == 0


async def test_cancel_while_waiting_takes_and_releases_nothing() -> None:
    gate = asyncio.Event()

    class Held:
        async def generate(self, messages: Any, tools: Any = None, *, timeout: Any = None) -> Any:
            await gate.wait()
            return ModelResponse(message=Message(role="assistant", content="ok"))

    limiter = RateLimiter(max_concurrent=1)
    model = RateLimitedModel(Held(), limiter)  # type: ignore[arg-type]
    holder = asyncio.create_task(model.generate([], None))
    await asyncio.sleep(0.01)
    waiter = asyncio.create_task(model.generate([], None))
    await asyncio.sleep(0.01)
    assert limiter.in_flight == 1
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert limiter.in_flight == 1  # the holder still has its slot, untouched
    gate.set()
    await holder
    assert limiter.in_flight == 0
    await model.generate([], None)  # limiter still usable


async def test_cancel_while_timed_wait_takes_nothing() -> None:
    clock = FakeClock()
    block = asyncio.Event()

    async def blocking_sleep(seconds: float) -> None:
        await block.wait()

    limiter = RateLimiter(requests_per_minute=1, clock=clock, sleep=blocking_sleep)
    model = RateLimitedModel(FakeModel(repeat_last=True, responses=["x"]), limiter)
    await model.generate([], None)
    task = asyncio.create_task(model.generate([], None))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert limiter.in_flight == 0
    assert len(limiter._starts) == 1


async def test_wakeup_survives_cancelled_waiter() -> None:
    limiter = RateLimiter(max_concurrent=1)
    await limiter.acquire()
    first = asyncio.create_task(limiter.acquire())
    second = asyncio.create_task(limiter.acquire())
    await asyncio.sleep(0.01)
    limiter.release()
    first.cancel()
    await asyncio.gather(first, return_exceptions=True)
    await asyncio.wait_for(second, timeout=1)
    assert limiter.in_flight == 1
    limiter.release()


async def test_retry_wraps_rate_limited_model() -> None:
    calls = {"n": 0}

    def flaky(messages: Any, tools: Any) -> Any:
        calls["n"] += 1
        if calls["n"] < 3:
            raise ProviderRateLimitError("429")
        return "done"

    clock = FakeClock()
    limiter = RateLimiter(requests_per_minute=2, max_concurrent=1, clock=clock, sleep=clock.sleep)
    retrying = RetryModel(
        RateLimitedModel(FakeModel(handler=flaky), limiter),
        max_attempts=3,
        base_delay=0.001,
        max_delay=0.01,
    )
    agent = Agent(name="r", instructions="x", model=retrying)
    result = await agent.run_async("hi")
    assert result.final_output == "done"
    assert retrying.attempts_made == 3
    assert clock.slept == [60.0]  # the third attempt waited for the request window
    assert limiter.in_flight == 0


def test_repr() -> None:
    limiter = RateLimiter(max_concurrent=3)
    assert "max_concurrent=3" in repr(limiter)
    assert "RateLimitedModel" in repr(RateLimitedModel(FakeModel(), limiter))
