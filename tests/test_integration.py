"""End-to-end integration tests using only top-level public exports."""

import json

import httpx
import pytest

from nexura_runtime import (
    Agent,
    FakeModel,
    OpenAIChatModel,
    RunEventType,
    Runner,
    ToolCall,
    tool,
)


@pytest.mark.asyncio
async def test_full_pipeline_multi_tool_workflow() -> None:
    users_db = {"u1": {"name": "विक्रम (Vikram)", "tier": "gold"}}
    rewards_db = {"gold": "20% discount on all books"}

    @tool
    def get_user_profile(user_id: str) -> dict[str, str]:
        """Fetch user profile and tier."""
        return users_db.get(user_id, {"error": "User not found"})

    @tool
    def get_tier_reward(tier: str) -> dict[str, str]:
        """Fetch tier rewards description."""
        reward = rewards_db.get(tier, "No benefits")
        return {"tier": tier, "reward": reward}

    # Model performs 2 sequential tool calls then returns final synthesis
    model = FakeModel(
        responses=[
            [ToolCall(id="tc_user", name="get_user_profile", arguments={"user_id": "u1"})],
            [ToolCall(id="tc_reward", name="get_tier_reward", arguments={"tier": "gold"})],
            "उपयोगकर्ता विक्रम (gold tier): 20% discount on all books उपलब्ध है।",
        ]
    )

    agent = Agent(
        name="RewardsConcierge",
        instructions="Look up user profiles and rewards.",
        model=model,
        tools=[get_user_profile, get_tier_reward],
    )

    runner = Runner()
    result = await runner.run_async(agent, "Check rewards for user u1")

    assert result.status == "completed"
    assert result.turns_count == 3
    assert result.tool_calls_count == 2
    assert "विक्रम" in result.final_output
    assert "20% discount" in result.final_output

    # Check that events list contains all steps
    event_types = [e.type for e in result.events]
    assert event_types[0] == RunEventType.RUN_STARTED
    assert event_types[-1] == RunEventType.RUN_COMPLETED
    assert event_types.count(RunEventType.TOOL_COMPLETED) == 2


@pytest.mark.asyncio
async def test_full_pipeline_with_openai_model_and_mock_transport() -> None:
    request_count = 0

    def mock_server(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        data = json.loads(request.content)
        messages = data["messages"]

        if request_count == 1:
            # First turn: model requests tool
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call_calc_1",
                                        "type": "function",
                                        "function": {
                                            "name": "multiply",
                                            "arguments": '{"a": 7, "b": 8}',
                                        },
                                    }
                                ],
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                    "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
                },
            )
        else:
            # Second turn: verify tool message arrived, then return final answer
            assert any(m.get("role") == "tool" and m.get("content") == "56" for m in messages)
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "7 multiplied by 8 equals 56.",
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 35, "completion_tokens": 12, "total_tokens": 47},
                },
            )

    transport = httpx.MockTransport(mock_server)
    model = OpenAIChatModel(
        model="gpt-4o-mini",
        api_key="sk-test-mocked",
        transport=transport,
    )

    @tool
    def multiply(a: int, b: int) -> int:
        """Multiply two integers."""
        return a * b

    agent = Agent(
        name="CalculatorBot",
        instructions="Multiply numbers when asked.",
        model=model,
        tools=[multiply],
    )

    runner = Runner()
    result = await runner.run_async(agent, "Multiply 7 by 8")

    assert result.status == "completed"
    assert result.tool_calls_count == 1
    assert "56" in result.final_output
    assert result.usage.is_reported
    assert result.usage.total_tokens == 77
    assert result.usage.prompt_tokens == 55
    assert result.usage.completion_tokens == 22

    await model.close()
