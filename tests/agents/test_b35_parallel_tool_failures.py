"""B35: a failing tool in a parallel batch does not hide the siblings that finished.

Offline only: FakeModel scripts the tool calls; the failing tool is a local fake.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from nexura_agents import Agent, FakeModel, MemoryCheckpointStore, Runner, tool
from nexura_agents.errors import ToolExecutionError
from nexura_agents.events import RunEventType
from nexura_agents.types import ToolCall


class Crash(BaseException):
    """Stands in for the process dying while a tool runs."""


def _agent(sent: list[str], fail: BaseException, *, parallel: bool = True) -> Agent:
    @tool
    async def send(to: str) -> str:
        """Write: send a message."""
        sent.append(to)
        return f"sent to {to}"

    @tool
    async def boom(x: str) -> str:
        """Fails after the sibling has finished."""
        await asyncio.sleep(0.05)
        raise fail

    calls = [
        ToolCall(id="c1", name="boom", arguments={"x": "1"}),
        ToolCall(id="c2", name="send", arguments={"to": "bob"}),
    ]
    return Agent(
        name="A",
        instructions="i",
        tools=[send, boom],
        parallel_tool_calls=parallel,
        model=FakeModel(responses=[calls, "done"]),
    )


async def test_completed_sibling_is_reported_before_the_failure() -> None:
    sent: list[str] = []
    events: list[Any] = []
    with pytest.raises(ToolExecutionError):
        async for event in Runner().stream_async(_agent(sent, ValueError("rejected")), "go"):
            events.append(event)
    kinds = [(e.type, e.data.get("tool_name")) for e in events]
    assert (RunEventType.TOOL_COMPLETED, "send") in kinds
    assert kinds.index((RunEventType.TOOL_COMPLETED, "send")) < kinds.index(
        (RunEventType.RUN_FAILED, None)
    )
    assert sent == ["bob"]


async def test_failure_event_masks_credentials_but_the_exception_keeps_them() -> None:
    events: list[Any] = []
    with pytest.raises(ToolExecutionError) as raised:
        async for event in Runner().stream_async(
            _agent([], ValueError("db password=hunter2 rejected")), "go"
        ):
            events.append(event)
    failed = [e for e in events if e.type == RunEventType.RUN_FAILED]
    assert failed and "hunter2" not in failed[0].data["error"]
    assert "boom" in failed[0].data["error"]
    assert "hunter2" in str(raised.value)  # the caller still sees what went wrong


async def test_recovery_after_a_crash_does_not_repeat_the_finished_write() -> None:
    sent: list[str] = []
    store = MemoryCheckpointStore()
    with pytest.raises(Crash):
        await Runner(checkpoint_store=store).run_async(_agent(sent, Crash()), "go")
    assert sent == ["bob"]
    (run_id,) = await store.list_incomplete()
    saved = await store.load(run_id)
    assert saved is not None and "sent to bob" in saved

    calls_after: list[str] = []

    @tool
    async def send(to: str) -> str:
        """Write: send a message."""
        calls_after.append(to)
        return f"sent to {to}"

    @tool
    async def boom(x: str) -> str:
        """Works on the second attempt."""
        return "ok"

    again = Agent(
        name="A",
        instructions="i",
        tools=[send, boom],
        parallel_tool_calls=True,
        model=FakeModel(responses=["done"]),
    )
    result = await Runner(checkpoint_store=store).recover_async(again, run_id)
    assert calls_after == []  # the write that finished before the crash is not sent again
    # Only the call that was running when the process died is uncertain (docs/durable-runs.md).
    assert result.status == "paused"
    assert result.interruption is not None
    assert result.interruption.reason == "outcome_unknown_after_crash"
    assert [c.name for c in result.interruption.pending] == ["boom"]
