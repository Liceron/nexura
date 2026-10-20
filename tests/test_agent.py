"""Tests for Agent configuration and eager validation."""

import pytest

from nexura_runtime import Agent, FakeModel, SDKValidationError, tool


def test_agent_valid_configuration() -> None:
    @tool
    def sample_tool(query: str) -> str:
        return f"result: {query}"

    model = FakeModel(["Hello"])
    agent = Agent(
        name="SupportAgent",
        instructions="You are helpful.",
        model=model,
        tools=[sample_tool],
        max_turns=5,
        max_tool_calls=10,
        timeout=15.0,
    )

    assert agent.name == "SupportAgent"
    assert agent.instructions == "You are helpful."
    assert len(agent.tools) == 1
    assert "sample_tool" in agent.tools_by_name
    assert agent.max_turns == 5
    assert agent.max_tool_calls == 10
    assert agent.timeout == 15.0
    assert "SupportAgent" in repr(agent)


def test_agent_rejects_empty_name() -> None:
    model = FakeModel(["Hello"])
    with pytest.raises(SDKValidationError, match="Agent name must be a non-empty string"):
        Agent(name="  ", instructions="inst", model=model)


def test_agent_rejects_invalid_instructions() -> None:
    model = FakeModel(["Hello"])
    with pytest.raises(SDKValidationError, match="Agent instructions must be a string"):
        Agent(name="Bot", instructions=123, model=model)  # type: ignore[arg-type]


def test_agent_rejects_invalid_model() -> None:
    with pytest.raises(
        SDKValidationError, match="Agent model must provide a generate\\(\\) method"
    ):
        Agent(name="Bot", instructions="inst", model=None)  # type: ignore[arg-type]


def test_agent_rejects_duplicate_tool_names() -> None:
    @tool(name="duplicate_tool")
    def tool_a(x: int) -> int:
        return x

    @tool(name="duplicate_tool")
    def tool_b(y: str) -> str:
        return y

    model = FakeModel(["Hello"])
    with pytest.raises(SDKValidationError, match="Duplicate tool name registered"):
        Agent(name="Bot", instructions="inst", model=model, tools=[tool_a, tool_b])


def test_agent_rejects_non_tool_in_tools() -> None:
    model = FakeModel(["Hello"])
    with pytest.raises(SDKValidationError, match="Expected Tool instance in tools list"):
        Agent(name="Bot", instructions="inst", model=model, tools=["not_a_tool"])  # type: ignore[list-item]


def test_agent_rejects_invalid_limits() -> None:
    model = FakeModel(["Hello"])

    with pytest.raises(SDKValidationError, match="max_turns must be an integer >= 1"):
        Agent(name="Bot", instructions="inst", model=model, max_turns=0)

    with pytest.raises(SDKValidationError, match="max_turns must be an integer >= 1"):
        Agent(name="Bot", instructions="inst", model=model, max_turns=-5)

    with pytest.raises(SDKValidationError, match="max_tool_calls must be an integer >= 0"):
        Agent(name="Bot", instructions="inst", model=model, max_tool_calls=-1)

    with pytest.raises(SDKValidationError, match="timeout must be a positive number"):
        Agent(name="Bot", instructions="inst", model=model, timeout=0)

    with pytest.raises(SDKValidationError, match="timeout must be a positive number"):
        Agent(name="Bot", instructions="inst", model=model, timeout=-10.0)
