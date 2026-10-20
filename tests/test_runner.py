"""Tests for Runner execution loop, lifecycle events, bounds, and concurrency."""

import asyncio

import pytest

from nexura_runtime import (
    Agent,
    EventLoopError,
    FakeModel,
    MaxToolCallsExceededError,
    MaxTurnsExceededError,
    Message,
    ModelResponse,
    ModelTimeoutError,
    RunEventType,
    Runner,
    ToolCall,
    ToolExecutionError,
    ToolValidationError,
    UnknownToolError,
    Usage,
    tool,
)


@pytest.mark.asyncio
async def test_runner_happy_path_and_event_order() -> None:
    @tool
    def get_weather(location: str) -> str:
        return f"Sunny and 24C in {location}"

    fake_model = FakeModel(
        responses=[
            [ToolCall(id="call_1", name="get_weather", arguments={"location": "Delhi"})],
            "The weather in Delhi is sunny and 24C.",
        ]
    )

    agent = Agent(
        name="WeatherBot",
        instructions="Provide weather updates.",
        model=fake_model,
        tools=[get_weather],
    )

    runner = Runner()
    events = []
    async for event in runner.stream_async(agent, "What is the weather in Delhi?"):
        events.append(event)

    event_types = [e.type for e in events]
    expected_order = [
        RunEventType.RUN_STARTED,
        RunEventType.AGENT_STARTED,  # added in F40 (2026-10-11)
        RunEventType.MODEL_STARTED,
        RunEventType.MODEL_COMPLETED,
        RunEventType.TOOL_STARTED,
        RunEventType.TOOL_COMPLETED,
        RunEventType.MODEL_STARTED,
        RunEventType.MODEL_COMPLETED,
        RunEventType.FINAL_OUTPUT,
        RunEventType.RUN_COMPLETED,
    ]
    assert event_types == expected_order

    # Single terminal outcome
    terminal_events = [e for e in events if e.is_terminal]
    assert len(terminal_events) == 1
    assert terminal_events[0].type == RunEventType.RUN_COMPLETED

    # Verify run_async produces complete RunResult
    fake_model_2 = FakeModel(
        responses=[
            [ToolCall(id="call_2", name="get_weather", arguments={"location": "Delhi"})],
            "The weather in Delhi is sunny and 24C.",
        ]
    )
    agent.model = fake_model_2
    result = await runner.run_async(agent, "What is the weather in Delhi?")
    assert result.status == "completed"
    assert result.turns_count == 2
    assert result.tool_calls_count == 1
    assert "24C" in result.final_output
    assert len(result.messages) == 5  # system, user, assistant(tc), tool, assistant(final)


@pytest.mark.asyncio
async def test_runner_stops_endless_tool_calls_at_max_tool_calls() -> None:
    call_count = 0

    @tool
    def endless_ping() -> str:
        nonlocal call_count
        call_count += 1
        return "pong"

    # Endless model requests a tool call on every turn
    endless_model = FakeModel.endless(tool_name="endless_ping")

    agent = Agent(
        name="BoundedBot",
        instructions="Ping endlessly",
        model=endless_model,
        tools=[endless_ping],
        max_turns=50,
        max_tool_calls=5,  # Upper bound is 5
    )

    runner = Runner()

    with pytest.raises(MaxToolCallsExceededError) as exc_info:
        await runner.run_async(agent, "Start pinging")

    # Assert exact tool call count matches the configured bound
    assert call_count == 5
    assert exc_info.value.calls_count == 6  # Attempted the 6th call
    assert exc_info.value.max_tool_calls == 5


@pytest.mark.asyncio
async def test_runner_stops_endless_turns_at_max_turns() -> None:
    turn_count = 0

    @tool
    def nop_tool() -> str:
        nonlocal turn_count
        turn_count += 1
        return "ok"

    endless_model = FakeModel.endless(tool_name="nop_tool")

    agent = Agent(
        name="TurnBoundedBot",
        instructions="Run",
        model=endless_model,
        tools=[nop_tool],
        max_turns=4,
        max_tool_calls=100,
    )

    runner = Runner()

    with pytest.raises(MaxTurnsExceededError) as exc_info:
        await runner.run_async(agent, "Loop")

    assert exc_info.value.turns_count == 4
    assert exc_info.value.max_turns == 4
    assert turn_count == 4


@pytest.mark.asyncio
async def test_runner_elapsed_timeout() -> None:
    @tool
    async def slow_tool() -> str:
        await asyncio.sleep(0.5)
        return "done"

    model = FakeModel(
        responses=[
            [ToolCall(id="call_slow", name="slow_tool", arguments={})],
            "Slow tool completed",
        ]
    )

    agent = Agent(
        name="TimeoutBot",
        instructions="Test timeout",
        model=model,
        tools=[slow_tool],
        timeout=0.1,  # 100ms timeout
    )

    runner = Runner()

    with pytest.raises(ModelTimeoutError) as exc_info:
        await runner.run_async(agent, "Run slow tool")

    assert exc_info.value.timeout_seconds == 0.1
    assert exc_info.value.elapsed_seconds >= 0.1


@pytest.mark.asyncio
async def test_runner_unknown_tool_fails_explicitly() -> None:
    fake_model = FakeModel(
        responses=[
            [ToolCall(id="call_unk", name="unregistered_tool", arguments={})],
        ]
    )

    agent = Agent(
        name="SafeBot",
        instructions="Safe instructions",
        model=fake_model,
        tools=[],  # No tools registered
    )

    runner = Runner()

    with pytest.raises(UnknownToolError) as exc_info:
        await runner.run_async(agent, "Run unknown")

    assert exc_info.value.tool_name == "unregistered_tool"


@pytest.mark.asyncio
async def test_runner_malformed_arguments_fails_explicitly() -> None:
    tool_executed = False

    @tool
    def typed_tool(count: int) -> str:
        nonlocal tool_executed
        tool_executed = True
        return f"Count: {count}"

    fake_model = FakeModel(
        responses=[
            [ToolCall(id="call_bad", name="typed_tool", arguments={"count": "not_an_int"})],
        ]
    )

    agent = Agent(
        name="TypedBot",
        instructions="Validate arguments",
        model=fake_model,
        tools=[typed_tool],
    )

    runner = Runner()

    with pytest.raises(ToolValidationError):
        await runner.run_async(agent, "Test malformed args")

    assert tool_executed is False


@pytest.mark.asyncio
async def test_runner_tool_exception_fails_explicitly() -> None:
    @tool
    def exploding_tool() -> str:
        raise RuntimeError("Disk write failed: Read-only filesystem")

    fake_model = FakeModel(
        responses=[
            [ToolCall(id="call_err", name="exploding_tool", arguments={})],
        ]
    )

    agent = Agent(
        name="FailBot",
        instructions="Test exception",
        model=fake_model,
        tools=[exploding_tool],
    )

    runner = Runner()

    with pytest.raises(ToolExecutionError) as exc_info:
        await runner.run_async(agent, "Trigger explosion")

    assert "Read-only filesystem" in str(exc_info.value)
    assert isinstance(exc_info.value.original_error, RuntimeError)


@pytest.mark.asyncio
async def test_runner_early_stream_close_cleanup() -> None:
    tool_calls_executed = 0

    @tool
    def counting_tool() -> str:
        nonlocal tool_calls_executed
        tool_calls_executed += 1
        return "counted"

    endless_model = FakeModel.endless(tool_name="counting_tool")

    agent = Agent(
        name="StreamingBot",
        instructions="Continuous streaming",
        model=endless_model,
        tools=[counting_tool],
        max_turns=20,
        max_tool_calls=20,
    )

    runner = Runner()

    # Consumer breaks out of stream early after the first tool completed
    async for event in runner.stream_async(agent, "Start stream"):
        if event.type == RunEventType.TOOL_COMPLETED:
            break

    # Verify stream stopped and scheduled no further tools
    assert tool_calls_executed == 1


@pytest.mark.asyncio
async def test_agent_reuse_across_concurrent_runs_keeps_histories_isolated() -> None:
    @tool
    def echo_message(msg: str) -> str:
        return f"Echo: {msg}"

    def model_handler(
        messages: list[Message], tools: list[dict[str, object]] | None
    ) -> list[ToolCall] | str:
        user_msg = next((m.content for m in messages if m.role == "user"), "")
        last_msg = messages[-1]
        if last_msg.role == "tool":
            return f"Processed for user prompt: {user_msg}"
        return [ToolCall(id=f"echo_{user_msg}", name="echo_message", arguments={"msg": user_msg})]

    concurrent_model = FakeModel(handler=model_handler)

    agent = Agent(
        name="SharedAgent",
        instructions="Shared assistant across multiple users.",
        model=concurrent_model,
        tools=[echo_message],
    )

    runner = Runner()

    async def execute_user_run(user_index: int) -> str:
        prompt = f"User-{user_index}-Secret-Data"
        result = await runner.run_async(agent, prompt)
        # Verify the run only contains this user's data
        for m in result.messages:
            if m.content:
                for other_index in range(10):
                    if other_index != user_index:
                        assert f"User-{other_index}" not in m.content
        return result.final_output

    tasks = [execute_user_run(i) for i in range(10)]
    results = await asyncio.gather(*tasks)

    assert len(results) == 10
    for i, res in enumerate(results):
        assert f"User-{i}-Secret-Data" in res


@pytest.mark.asyncio
async def test_runner_usage_distinguishes_reported_vs_unknown() -> None:
    # 1. Unknown usage: provider reports None
    model_unknown = FakeModel(responses=["Answer without usage metrics"])
    agent_unknown = Agent(
        name="UnknownUsageBot",
        instructions="inst",
        model=model_unknown,
    )
    runner = Runner()
    res_unknown = await runner.run_async(agent_unknown, "Hello")
    assert not res_unknown.usage.is_reported
    assert res_unknown.usage.prompt_tokens is None
    assert res_unknown.usage.completion_tokens is None
    assert res_unknown.usage.total_tokens is None

    # 2. Reported usage across 2 turns
    @tool
    def dummy_tool() -> str:
        return "done"

    resp1 = ModelResponse(
        message=Message(
            role="assistant",
            tool_calls=[ToolCall(id="c1", name="dummy_tool", arguments={})],
        ),
        usage=Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
    )
    resp2 = ModelResponse(
        message=Message(role="assistant", content="Final answer with tokens"),
        usage=Usage(prompt_tokens=20, completion_tokens=8, total_tokens=28),
    )
    fake_model_reported = FakeModel(responses=[resp1, resp2])

    agent_reported = Agent(
        name="ReportedUsageBot",
        instructions="inst",
        model=fake_model_reported,
        tools=[dummy_tool],
    )
    res_reported = await runner.run_async(agent_reported, "Calculate tokens")
    assert res_reported.usage.is_reported
    assert res_reported.usage.prompt_tokens == 30
    assert res_reported.usage.completion_tokens == 13
    assert res_reported.usage.total_tokens == 43


@pytest.mark.asyncio
async def test_runner_refuses_sync_run_inside_running_event_loop() -> None:
    agent = Agent(name="SyncBot", instructions="inst", model=FakeModel(["Answer"]))
    runner = Runner()

    with pytest.raises(EventLoopError, match="Cannot call runner.run\\(\\) synchronously"):
        runner.run(agent, "Should fail because event loop is active")
