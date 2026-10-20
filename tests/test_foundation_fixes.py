"""Regressions and contract checks for foundation fixes (Task 1).

Covers:
- Total run deadline covering model and tool execution (including multi-tool response).
- Slow async tool timing out promptly and preventing later tools from executing.
- Nonpositive and nonfinite timeout validation.
- Message/tool argument deep-copying and provider mutation isolation.
- Message.to_dict JSON serialization preserving Indic Unicode.
- Aggregated usage completeness tracking.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from nexura_runtime import (
    Agent,
    FakeModel,
    Message,
    ModelResponse,
    ModelTimeoutError,
    Runner,
    SDKValidationError,
    ToolCall,
    Usage,
    tool,
)


@pytest.mark.asyncio
async def test_multitool_timeout_prevents_subsequent_tools() -> None:
    tool_one_started = False
    tool_two_executed = False

    @tool
    async def slow_first_tool() -> str:
        nonlocal tool_one_started
        tool_one_started = True
        await asyncio.sleep(0.5)
        return "slow_done"

    @tool
    async def second_tool() -> str:
        nonlocal tool_two_executed
        tool_two_executed = True
        return "second_done"

    model = FakeModel(
        responses=[
            [
                ToolCall(id="call_slow", name="slow_first_tool", arguments={}),
                ToolCall(id="call_sec", name="second_tool", arguments={}),
            ],
            "Both completed",
        ]
    )

    agent = Agent(
        name="MultiToolTimeoutBot",
        instructions="Run multiple tools",
        model=model,
        tools=[slow_first_tool, second_tool],
        timeout=0.1,  # 100ms total deadline
    )

    runner = Runner()
    with pytest.raises(ModelTimeoutError) as exc_info:
        await runner.run_async(agent, "Execute tools")

    assert exc_info.value.timeout_seconds == 0.1
    assert tool_one_started is True
    assert tool_two_executed is False


@pytest.mark.asyncio
async def test_tool_start_blocked_when_deadline_already_exceeded() -> None:
    tool_two_started = False

    @tool
    def quick_tool() -> str:
        # simulate taking just enough time that deadline passes
        import time

        time.sleep(0.12)
        return "quick_done"

    @tool
    def never_tool() -> str:
        nonlocal tool_two_started
        tool_two_started = True
        return "never"

    model = FakeModel(
        responses=[
            [
                ToolCall(id="c1", name="quick_tool", arguments={}),
                ToolCall(id="c2", name="never_tool", arguments={}),
            ],
            "Final",
        ]
    )

    agent = Agent(
        name="DeadlineBot",
        instructions="inst",
        model=model,
        tools=[quick_tool, never_tool],
        timeout=0.1,
    )

    runner = Runner()
    with pytest.raises(ModelTimeoutError):
        await runner.run_async(agent, "Run")

    assert tool_two_started is False


@pytest.mark.parametrize(
    "invalid_timeout", [0, -1, -0.5, float("inf"), float("-inf"), float("nan")]
)
def test_reject_nonpositive_and_nonfinite_timeouts_in_agent(invalid_timeout: float) -> None:
    model = FakeModel(["Hello"])
    with pytest.raises(SDKValidationError, match="timeout must be a positive number and finite"):
        Agent(name="BadTimeoutBot", instructions="inst", model=model, timeout=invalid_timeout)


@pytest.mark.parametrize(
    "invalid_timeout", [0, -1, -0.5, float("inf"), float("-inf"), float("nan")]
)
@pytest.mark.asyncio
async def test_reject_nonpositive_and_nonfinite_timeouts_in_runner(invalid_timeout: float) -> None:
    model = FakeModel(["Hello"])
    agent = Agent(name="TimeoutBot", instructions="inst", model=model)
    runner = Runner()

    with pytest.raises(SDKValidationError, match="timeout must be a positive number and finite"):
        await runner.run_async(agent, "Hi", timeout=invalid_timeout)

    with pytest.raises(SDKValidationError, match="timeout must be a positive number and finite"):
        async for _ in runner.stream_async(agent, "Hi", timeout=invalid_timeout):
            pass


@pytest.mark.asyncio
async def test_deep_copy_caller_history_and_provider_mutation_isolation() -> None:
    class MutatingModel:
        def __init__(self) -> None:
            self.generate_called = False

        async def generate(
            self,
            messages: list[Message],
            tools: list[dict[str, Any]] | None = None,
            *,
            timeout: float | None = None,
        ) -> ModelResponse:
            self.generate_called = True
            # Attempt malicious mutation on caller messages
            if messages:
                messages[0].content = "TAMPERED_IN_PROVIDER"
                messages.clear()
            return ModelResponse(message=Message(role="assistant", content="Response"))

    model = MutatingModel()
    agent = Agent(name="SafetyBot", instructions="System prompt", model=model)
    runner = Runner()

    original_history = [
        Message(role="user", content="Original user message"),
        Message(role="assistant", content="Original assistant message"),
    ]

    result = await runner.run_async(agent, "New question", history=original_history)

    assert result.status == "completed"
    assert model.generate_called is True
    # Verify original history list and contents were NOT modified
    assert len(original_history) == 2
    assert original_history[0].content == "Original user message"
    assert original_history[1].content == "Original assistant message"


def test_message_to_dict_preserves_unicode_and_json_format() -> None:
    args = {"query": "दिल्ली मौसम", "city": "नई दिल्ली", "details": {"active": True, "pin": 110001}}
    tc = ToolCall(id="call_indic_1", name="search_weather", arguments=args)
    msg = Message(role="assistant", tool_calls=[tc])

    d = msg.to_dict()
    assert d["role"] == "assistant"
    assert len(d["tool_calls"]) == 1
    fn = d["tool_calls"][0]["function"]
    assert fn["name"] == "search_weather"

    # Must be valid JSON string
    parsed_args = json.loads(fn["arguments"])
    assert parsed_args == args
    # Verify Unicode Indic text survived without ASCII escape sequences (\u0926 etc.)
    assert "दिल्ली" in fn["arguments"]


@pytest.mark.asyncio
async def test_usage_completeness_tracking() -> None:
    # 1. Complete usage across all turns
    @tool
    def dummy() -> str:
        return "ok"

    resp1 = ModelResponse(
        message=Message(
            role="assistant",
            tool_calls=[ToolCall(id="1", name="dummy", arguments={})],
        ),
        usage=Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15, is_complete=True),
    )
    resp2 = ModelResponse(
        message=Message(role="assistant", content="Done"),
        usage=Usage(prompt_tokens=20, completion_tokens=10, total_tokens=30, is_complete=True),
    )
    model_complete = FakeModel(responses=[resp1, resp2])
    agent_complete = Agent(
        name="UsageBot",
        instructions="inst",
        model=model_complete,
        tools=[dummy],
    )
    runner = Runner()

    res_complete = await runner.run_async(agent_complete, "Go")
    assert res_complete.usage.is_reported is True
    assert res_complete.usage.is_complete is True
    assert res_complete.usage.total_tokens == 45

    # 2. Incomplete usage: one turn has None for usage
    resp_no_usage = ModelResponse(
        message=Message(role="assistant", content="Done without usage"),
        usage=None,
    )
    model_incomplete = FakeModel(responses=[resp1, resp_no_usage])
    agent_incomplete = Agent(
        name="IncompleteBot", instructions="inst", model=model_incomplete, tools=[dummy]
    )

    res_incomplete = await runner.run_async(agent_incomplete, "Go")
    assert res_incomplete.usage.is_reported is True
    assert res_incomplete.usage.is_complete is False
    assert res_incomplete.usage.total_tokens == 15  # partial sum retained, but flagged incomplete
