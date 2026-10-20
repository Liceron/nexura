"""Regressions from the F51 review: model label through wrappers, stream usage on any delta."""

from __future__ import annotations

from typing import Any

from nexura_agents.models.ratelimit import RateLimitedModel, RateLimiter
from nexura_agents.providers.types import StreamDelta
from nexura_agents.runner import model_label
from nexura_agents.types import Usage


class Named:
    model = "gpt-x"

    async def generate(self, messages: Any, tools: Any = None, *, timeout: Any = None) -> Any:
        raise NotImplementedError


def test_model_label_sees_through_wrapper() -> None:
    # runner.model_label reads .model, which on the wrapper is the inner object, not a str,
    # so usage_by_model is keyed "RateLimitedModel" instead of the real model name.
    wrapped = RateLimitedModel(Named(), RateLimiter(max_concurrent=1))
    assert model_label(wrapped) == "gpt-x"


async def test_stream_usage_from_non_finish_delta_counts() -> None:
    # runner accepts usage from ANY delta; the wrapper only reads "finish" deltas.
    class S:
        async def stream(self, messages: Any, tools: Any = None, *, timeout: Any = None):
            yield StreamDelta(kind="text_delta", text="a", usage=Usage(total_tokens=500))
            yield StreamDelta(kind="finish", finish_reason="stop")

    lim = RateLimiter(tokens_per_minute=100)
    m = RateLimitedModel(S(), lim)
    async for _ in m.stream([], None):
        pass
    assert sum(t for _, t in lim._usage) == 500
