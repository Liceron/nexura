"""tool_choice: force, forbid or pick a tool, mapped per provider (F29)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from nexura_agents import Agent, FakeModel, ModelSettings, Runner, ToolCall, tool
from nexura_agents.errors import SDKValidationError
from nexura_agents.models.litellm import LiteLLMModel
from nexura_agents.models.openai import OpenAIChatModel
from nexura_agents.providers.anthropic.provider import AnthropicNativeProvider
from nexura_agents.providers.gemini.provider import GeminiNativeProvider
from nexura_agents.providers.openai.provider import OpenAIProvider


@tool
def search(q: str) -> str:
    """Search."""
    return f"found {q}"


@tool
def weather(city: str) -> str:
    """Weather."""
    return "sunny"


def choices(model: FakeModel) -> list[str | None]:
    out = []
    for call in model.calls:
        settings = call["model_settings"]
        out.append(None if settings is None else settings.tool_choice)
    return out


async def run(model: FakeModel, choice: str, **kw: Any) -> Any:
    agent = Agent(
        name="A",
        instructions="i",
        model=model,
        tools=[search, weather],
        model_settings=ModelSettings(tool_choice=choice),
        **kw,
    )
    return await Runner().run_async(agent, "go")


async def test_required_is_sent_then_reset_to_auto_after_a_tool_call() -> None:
    model = FakeModel(responses=[[ToolCall(id="c1", name="search", arguments={"q": "x"})], "done"])
    result = await run(model, "required")
    assert result.output == "done"
    assert choices(model) == ["required", None]  # reset, so the run cannot loop


async def test_reset_can_be_turned_off() -> None:
    model = FakeModel(
        responses=[
            [ToolCall(id="c1", name="search", arguments={"q": "x"})],
            [ToolCall(id="c2", name="search", arguments={"q": "y"})],
            "done",
        ]
    )
    agent_kw = {"reset_tool_choice": False}
    await run(model, "search", **agent_kw)
    assert choices(model) == ["search", "search", "search"]


async def test_choice_applies_again_on_the_next_user_turn() -> None:
    from nexura_agents import MemorySessionStore

    model = FakeModel(
        responses=[
            [ToolCall(id="c1", name="search", arguments={"q": "x"})],
            "first",
            [ToolCall(id="c2", name="search", arguments={"q": "y"})],
            "second",
        ]
    )
    agent = Agent(
        name="A",
        instructions="i",
        model=model,
        tools=[search],
        model_settings=ModelSettings(tool_choice="required"),
    )
    runner = Runner(session_store=MemorySessionStore())
    await runner.run_async(agent, "one", session_id="s")
    await runner.run_async(agent, "two", session_id="s")
    assert choices(model) == ["required", None, "required", None]


async def test_unknown_tool_name_and_no_tools_are_errors() -> None:
    with pytest.raises(SDKValidationError, match="does not offer"):
        await run(FakeModel(responses=["x"]), "translate")
    bare = Agent(
        name="A",
        instructions="i",
        model=FakeModel(responses=["x"]),
        model_settings=ModelSettings(tool_choice="required"),
    )
    with pytest.raises(SDKValidationError, match="offers no tools"):
        await Runner().run_async(bare, "go")


async def test_none_without_tools_is_dropped() -> None:
    model = FakeModel(responses=["x"])
    agent = Agent(
        name="A", instructions="i", model=model, model_settings=ModelSettings(tool_choice="none")
    )
    await Runner().run_async(agent, "go")
    assert choices(model) == [None]


@pytest.mark.parametrize("bad", ["", "  ", 3])
def test_tool_choice_is_validated(bad: Any) -> None:
    with pytest.raises(SDKValidationError, match="tool_choice"):
        ModelSettings(tool_choice=bad)


# ---------------------------------------------------------------- provider payloads


def capture(reply: dict[str, Any]) -> tuple[list[dict[str, Any]], httpx.MockTransport]:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=reply)

    return seen, httpx.MockTransport(handler)


OPENAI_REPLY = {
    "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]
}


@pytest.mark.parametrize(
    "choice, expected",
    [
        ("required", "required"),
        ("none", "none"),
        ("search", {"type": "function", "function": {"name": "search"}}),
    ],
)
async def test_openai_formats(choice: str, expected: Any) -> None:
    seen, transport = capture(OPENAI_REPLY)
    for model in (
        OpenAIChatModel("m", transport=transport),
        OpenAIProvider("gpt-x", api_key="k", transport=transport),
    ):
        agent = Agent(
            name="A",
            instructions="i",
            model=model,
            tools=[search],
            model_settings=ModelSettings(tool_choice=choice),
        )
        await Runner().run_async(agent, "go")
    assert [s["tool_choice"] for s in seen] == [expected, expected]


async def test_litellm_format() -> None:
    calls: list[dict[str, Any]] = []

    async def completion(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return OPENAI_REPLY

    agent = Agent(
        name="A",
        instructions="i",
        model=LiteLLMModel("openai/x", completion=completion),
        tools=[search],
        model_settings=ModelSettings(tool_choice="search"),
    )
    await Runner().run_async(agent, "go")
    assert calls[0]["tool_choice"] == {"type": "function", "function": {"name": "search"}}


async def test_gemini_format() -> None:
    reply = {
        "candidates": [
            {"content": {"role": "model", "parts": [{"text": "ok"}]}, "finishReason": "STOP"}
        ]
    }
    seen, transport = capture(reply)
    model = GeminiNativeProvider("gemini-2.5-flash", api_key="k", transport=transport)
    agent = Agent(
        name="A",
        instructions="i",
        model=model,
        tools=[search, weather],
        model_settings=ModelSettings(tool_choice="weather"),
    )
    await Runner().run_async(agent, "go")
    assert seen[0]["toolConfig"] == {
        "functionCallingConfig": {"mode": "ANY", "allowedFunctionNames": ["weather"]}
    }


async def test_anthropic_format() -> None:
    reply = {
        "id": "m",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": "ok"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    seen, transport = capture(reply)
    model = AnthropicNativeProvider("claude-x", api_key="k", transport=transport)
    agent = Agent(
        name="A",
        instructions="i",
        model=model,
        tools=[search],
        model_settings=ModelSettings(tool_choice="required"),
    )
    await Runner().run_async(agent, "go")
    assert seen[0]["tool_choice"] == {"type": "any"}


# ---------------------------------------------------------------- handoffs (review fix)


def _handoff_pair(target_responses: list[Any]) -> tuple[Agent, FakeModel]:
    target_model = FakeModel(responses=target_responses)
    billing = Agent(
        name="Billing",
        instructions="i",
        model=target_model,
        tools=[search],
        model_settings=ModelSettings(tool_choice="required"),
    )
    triage = Agent(
        name="Triage",
        instructions="i",
        model=FakeModel(responses=[[ToolCall(id="h1", name="transfer_to_billing", arguments={})]]),
        handoffs=[billing],
    )
    return triage, target_model


async def test_handoff_does_not_reset_the_target_agents_tool_choice() -> None:
    triage, target_model = _handoff_pair(
        [[ToolCall(id="c1", name="search", arguments={"q": "x"})], "paid"]
    )
    result = await Runner().run_async(triage, "pay my bill")
    assert result.output == "paid"
    assert choices(target_model) == ["required", None]  # its own call, then the reset


async def test_handoff_scope_survives_durable_recovery() -> None:
    from nexura_agents import MemoryCheckpointStore

    class Crash(BaseException):
        pass

    def crash_once(messages: Any, tools: Any) -> Any:
        raise Crash

    store = MemoryCheckpointStore()
    triage, _ = _handoff_pair([])
    triage.handoffs[0].agent.model = FakeModel(handler=crash_once)
    with pytest.raises(Crash):
        await Runner(checkpoint_store=store).run_async(triage, "pay my bill")
    (run_id,) = await store.list_incomplete()
    again, target_model = _handoff_pair(
        [[ToolCall(id="c1", name="search", arguments={"q": "x"})], "paid"]
    )
    result = await Runner(checkpoint_store=store).recover_async(again, run_id)
    assert result.output == "paid"
    assert choices(target_model)[0] == "required"
