"""Automatic tool retries with backoff, and per-run result caching (F34)."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from nexura_agents import (
    Agent,
    CancelToken,
    FakeModel,
    Runner,
    SchemaTool,
    ToolCall,
    ToolRetry,
    tool,
)
from nexura_agents.errors import SDKValidationError, ToolExecutionError
from nexura_agents.events import RunEventType


def calls(*pairs: tuple[str, dict[str, Any]]) -> list[ToolCall]:
    return [ToolCall(id=f"c{i}", name=n, arguments=a) for i, (n, a) in enumerate(pairs)]


def results(result: Any) -> list[str | None]:
    return [m.content for m in result.messages if m.role == "tool"]


async def run(tools: list[Any], responses: list[Any], **kw: Any) -> Any:
    agent = Agent(name="A", instructions="i", model=FakeModel(responses=responses), tools=tools)
    return await Runner().run_async(agent, "go", **kw)


# ---------------------------------------------------------------- retry


async def test_transient_errors_are_retried_with_backoff() -> None:
    attempts: list[float] = []

    @tool(idempotent=True, retry=ToolRetry(max_attempts=3, initial_delay=0.01))
    def flaky(q: str) -> str:
        """Flaky."""
        attempts.append(time.monotonic())
        if len(attempts) < 3:
            raise ConnectionError("reset by peer")
        return f"found {q}"

    result = await run([flaky], [calls(("flaky", {"q": "x"})), "done"])
    assert results(result) == ["found x"]
    assert len(attempts) == 3
    assert attempts[2] - attempts[1] >= attempts[1] - attempts[0]  # backoff grows
    assert not [e for e in result.events if e.type == RunEventType.RETRY]  # invisible to the model


async def test_give_up_after_max_attempts() -> None:
    attempts = {"n": 0}

    @tool(idempotent=True, retry=ToolRetry(max_attempts=2, initial_delay=0))
    def down(q: str) -> str:
        """Down."""
        attempts["n"] += 1
        raise ConnectionError("down")

    with pytest.raises(ToolExecutionError):
        await run([down], [calls(("down", {"q": "x"}))])
    assert attempts["n"] == 2


async def test_other_errors_are_not_retried() -> None:
    attempts = {"n": 0}

    @tool(idempotent=True, retry=ToolRetry(max_attempts=5, initial_delay=0))
    def broken(q: str) -> str:
        """Broken."""
        attempts["n"] += 1
        raise ValueError("bad input")

    with pytest.raises(ToolExecutionError):
        await run([broken], [calls(("broken", {"q": "x"}))])
    assert attempts["n"] == 1


async def test_own_timeout_is_retried_then_reported() -> None:
    attempts = {"n": 0}

    @tool(idempotent=True, timeout=0.05, retry=ToolRetry(max_attempts=2, initial_delay=0))
    async def slow(q: str) -> str:
        """Slow."""
        attempts["n"] += 1
        await asyncio.sleep(1)
        return "late"

    result = await run([slow], [calls(("slow", {"q": "x"})), "gave up"])
    assert attempts["n"] == 2
    assert results(result) == ["Tool call timed out: 'slow' did not finish within 0.05s."]


async def test_cancel_interrupts_the_backoff_wait() -> None:

    token = CancelToken()

    @tool(idempotent=True, retry=ToolRetry(max_attempts=3, initial_delay=5))
    def flaky(q: str) -> str:
        """Flaky."""
        token.cancel()
        raise ConnectionError("down")

    started = time.monotonic()
    result = await run([flaky], [calls(("flaky", {"q": "x"}))], cancel=token)
    assert result.status == "cancelled" and time.monotonic() - started < 1.5


# ---------------------------------------------------------------- cache


async def test_identical_calls_in_a_run_hit_the_cache() -> None:
    executions = {"n": 0}

    @tool(idempotent=True, cache=True)
    def rate(currency: str) -> str:
        """Exchange rate."""
        executions["n"] += 1
        return f"{currency}: 83.1"

    responses = [
        calls(("rate", {"currency": "USD"})),
        calls(("rate", {"currency": "USD"}), ("rate", {"currency": "EUR"})),
        "done",
    ]
    result = await run([rate], responses)
    assert results(result) == ["USD: 83.1", "USD: 83.1", "EUR: 83.1"]
    assert executions["n"] == 2  # USD once, EUR once


async def test_cache_is_per_run() -> None:
    executions = {"n": 0}

    @tool(idempotent=True, cache=True)
    def rate(currency: str) -> str:
        """Rate."""
        executions["n"] += 1
        return "83.1"

    agent = Agent(
        name="A",
        instructions="i",
        model=FakeModel(
            responses=[calls(("rate", {"currency": "USD"})), "done"], repeat_last=False
        ),
        tools=[rate],
    )
    await Runner().run_async(agent, "go")
    agent2 = agent.clone(model=FakeModel(responses=[calls(("rate", {"currency": "USD"})), "done"]))
    await Runner().run_async(agent2, "go")
    assert executions["n"] == 2


async def test_failed_calls_are_not_cached() -> None:
    executions = {"n": 0}

    @tool(idempotent=True, cache=True, max_retries=2)
    def picky(q: str) -> str:
        """Picky."""
        executions["n"] += 1
        if executions["n"] == 1:
            from nexura_agents import ModelRetry

            raise ModelRetry("try again")
        return "ok"

    responses = [calls(("picky", {"q": "x"})), calls(("picky", {"q": "x"})), "done"]
    result = await run([picky], responses)
    assert executions["n"] == 2 and results(result)[-1] == "ok"


# ---------------------------------------------------------------- validation


@pytest.mark.parametrize("kwargs", [{"retry": ToolRetry()}, {"cache": True}])
def test_retry_and_cache_require_idempotent(kwargs: dict[str, Any]) -> None:
    with pytest.raises(SDKValidationError, match="idempotent=True"):

        @tool(**kwargs)
        def t(x: str) -> str:
            """T."""
            return x


def test_schema_tool_accepts_idempotent_retry_and_cache() -> None:
    async def handler(ctx: Any, arguments: dict[str, Any]) -> str:
        return "ok"

    remote = SchemaTool(
        "remote",
        {"type": "object", "properties": {}},
        handler,
        idempotent=True,
        retry=ToolRetry(),
        cache=True,
    )
    assert remote.idempotent and remote.cache and remote.retry is not None


@pytest.mark.parametrize(
    "kwargs, match",
    [
        ({"max_attempts": 0}, "max_attempts"),
        ({"initial_delay": -1}, "initial_delay"),
        ({"backoff": 0.5}, "backoff"),
        ({"retry_on": [ConnectionError]}, "retry_on"),
    ],
)
def test_tool_retry_is_validated(kwargs: dict[str, Any], match: str) -> None:
    with pytest.raises(SDKValidationError, match=match):
        ToolRetry(**kwargs)


# ---------------------------------------------------------------- review fixes


async def test_cache_is_not_shared_between_agents_with_same_named_tools() -> None:
    @tool(idempotent=True, cache=True, name="lookup")
    def lookup_a(q: str) -> str:
        """A's lookup."""
        return "from-A"

    @tool(idempotent=True, cache=True, name="lookup")
    def lookup_b(q: str) -> str:
        """B's lookup."""
        return "from-B"

    b = Agent(
        name="B",
        instructions="i",
        model=FakeModel(responses=[calls(("lookup", {"q": "x"})), "done"]),
        tools=[lookup_b],
    )
    a = Agent(
        name="A",
        instructions="i",
        model=FakeModel(
            responses=[
                calls(("lookup", {"q": "x"})),
                [ToolCall(id="h", name="transfer_to_b", arguments={})],
            ]
        ),
        tools=[lookup_a],
        handoffs=[b],
    )
    result = await Runner().run_async(a, "go")
    assert results(result) == ["from-A", "Transferred to B.", "from-B"]


async def test_timed_out_sync_tool_is_not_retried_while_still_running() -> None:
    import threading

    running = {"now": 0, "max": 0}
    lock = threading.Lock()

    @tool(idempotent=True, timeout=0.05, retry=ToolRetry(max_attempts=3, initial_delay=0))
    def blocking(q: str) -> str:
        """Blocking."""
        with lock:
            running["now"] += 1
            running["max"] = max(running["max"], running["now"])
        time.sleep(0.3)
        with lock:
            running["now"] -= 1
        return "late"

    result = await run([blocking], [calls(("blocking", {"q": "x"})), "gave up"])
    assert running["max"] == 1  # never two executions at once
    assert "timed out" in str(results(result))
    await asyncio.sleep(0.4)  # let the worker thread finish
