"""F30: top_p, seed, penalties, reasoning effort, thinking budget and extra fields."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from nexura_agents import Agent, ModelSettings, Runner
from nexura_agents.errors import SDKValidationError
from nexura_agents.models.litellm import LiteLLMModel
from nexura_agents.models.openai import OpenAIChatModel
from nexura_agents.providers.anthropic.provider import AnthropicNativeProvider
from nexura_agents.providers.gemini.provider import GeminiNativeProvider
from nexura_agents.providers.openai.provider import OpenAIProvider

FULL = ModelSettings(
    top_p=0.9,
    seed=7,
    presence_penalty=0.5,
    frequency_penalty=-0.5,
    reasoning_effort="low",
    extra={"user": "u-42"},
)
OPENAI_REPLY = {
    "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]
}


def capture(reply: dict[str, Any]) -> tuple[list[dict[str, Any]], httpx.MockTransport]:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=reply)

    return seen, httpx.MockTransport(handler)


async def run(model: Any, settings: ModelSettings) -> None:
    agent = Agent(name="A", instructions="i", model=model, model_settings=settings)
    await Runner().run_async(agent, "go")


# ---------------------------------------------------------------- validation and merge


@pytest.mark.parametrize(
    "kwargs, match",
    [
        ({"top_p": 1.5}, "top_p"),
        ({"top_p": True}, "top_p"),
        ({"presence_penalty": 3}, "presence_penalty"),
        ({"frequency_penalty": "0.1"}, "frequency_penalty"),
        ({"seed": 1.5}, "seed"),
        ({"seed": False}, "seed"),
        ({"reasoning_effort": "max"}, "reasoning_effort"),
        ({"thinking_budget_tokens": -1}, "thinking_budget_tokens"),
        ({"extra": ["x"]}, "extra"),
        ({"extra": {1: "x"}}, "extra"),
    ],
)
def test_invalid_values_are_rejected(kwargs: dict[str, Any], match: str) -> None:
    with pytest.raises(SDKValidationError, match=match):
        ModelSettings(**kwargs)


def test_extra_is_copied_and_merged_key_by_key() -> None:
    extra = {"a": {"x": 1}}
    settings = ModelSettings(extra=extra)
    extra["a"]["x"] = 99
    assert settings.extra == {"a": {"x": 1}}
    merged = ModelSettings(extra={"a": 1, "b": 2}).merged(ModelSettings(extra={"b": 3}))
    assert merged.extra == {"a": 1, "b": 3}


# ---------------------------------------------------------------- provider payloads


async def test_openai_provider_and_chat_model_send_the_openai_names() -> None:
    seen, transport = capture(OPENAI_REPLY)
    await run(OpenAIProvider("gpt-x", api_key="k", transport=transport), FULL)
    await run(OpenAIChatModel("m", transport=transport), FULL)
    for body in seen:
        assert body["top_p"] == 0.9 and body["seed"] == 7
        assert body["presence_penalty"] == 0.5 and body["frequency_penalty"] == -0.5
        assert body["reasoning_effort"] == "low"
        assert body["user"] == "u-42"


async def test_openai_warns_about_thinking_budget() -> None:
    seen, transport = capture(OPENAI_REPLY)
    with pytest.warns(RuntimeWarning, match="thinking_budget_tokens"):
        await run(
            OpenAIProvider("gpt-x", api_key="k", transport=transport),
            ModelSettings(thinking_budget_tokens=1000),
        )
    assert "thinking" not in seen[0]


async def test_litellm_passes_names_and_thinking() -> None:
    calls: list[dict[str, Any]] = []

    async def completion(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return OPENAI_REPLY

    settings = ModelSettings(top_p=0.8, thinking_budget_tokens=2048, extra={"metadata": {"t": 1}})
    await run(LiteLLMModel("anthropic/claude", completion=completion), settings)
    assert calls[0]["top_p"] == 0.8
    assert calls[0]["thinking"] == {"type": "enabled", "budget_tokens": 2048}
    assert calls[0]["metadata"] == {"t": 1}


async def test_gemini_generation_config_and_thinking_budget() -> None:
    reply = {
        "candidates": [
            {"content": {"role": "model", "parts": [{"text": "ok"}]}, "finishReason": "STOP"}
        ]
    }
    seen, transport = capture(reply)
    settings = ModelSettings(
        top_p=0.9, seed=7, presence_penalty=0.5, frequency_penalty=0.1, thinking_budget_tokens=0
    )
    await run(GeminiNativeProvider("gemini-2.5-flash", api_key="k", transport=transport), settings)
    config = seen[0]["generationConfig"]
    assert config["topP"] == 0.9 and config["seed"] == 7
    assert config["presencePenalty"] == 0.5 and config["frequencyPenalty"] == 0.1
    assert config["thinkingConfig"] == {"thinkingBudget": 0}  # 0 turns thinking off


async def test_gemini_warns_about_reasoning_effort() -> None:
    reply = {
        "candidates": [
            {"content": {"role": "model", "parts": [{"text": "ok"}]}, "finishReason": "STOP"}
        ]
    }
    seen, transport = capture(reply)
    with pytest.warns(RuntimeWarning, match="reasoning_effort"):
        await run(
            GeminiNativeProvider("gemini-2.5-flash", api_key="k", transport=transport),
            ModelSettings(reasoning_effort="high"),
        )
    assert "reasoningEffort" not in json.dumps(seen[0])


ANTHROPIC_REPLY = {
    "id": "m",
    "type": "message",
    "role": "assistant",
    "content": [{"type": "text", "text": "ok"}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 1, "output_tokens": 1},
}


async def test_anthropic_thinking_and_top_p_and_warning() -> None:
    seen, transport = capture(ANTHROPIC_REPLY)
    settings = ModelSettings(top_p=0.97, thinking_budget_tokens=2000, max_tokens=8000, seed=3)
    with pytest.warns(RuntimeWarning, match="seed"):
        await run(AnthropicNativeProvider("claude-x", api_key="k", transport=transport), settings)
    assert seen[0]["top_p"] == 0.97
    assert seen[0]["thinking"] == {"type": "enabled", "budget_tokens": 2000}
    assert "seed" not in seen[0]


async def test_anthropic_thinking_budget_must_be_below_max_tokens() -> None:
    _, transport = capture(ANTHROPIC_REPLY)
    settings = ModelSettings(thinking_budget_tokens=5000, max_tokens=4000)
    with pytest.raises(SDKValidationError, match="lower than max_tokens"):
        await run(AnthropicNativeProvider("claude-x", api_key="k", transport=transport), settings)


@pytest.mark.parametrize(
    "kwargs, match",
    [
        ({"thinking_budget_tokens": 500, "max_tokens": 8000}, "at least 1024"),
        (
            {"thinking_budget_tokens": 2000, "max_tokens": 8000, "temperature": 0.2},
            "temperature must be unset or 1",
        ),
        (
            {"thinking_budget_tokens": 2000, "max_tokens": 8000, "top_p": 0.5},
            "top_p must be between 0.95 and 1",
        ),
    ],
)
async def test_anthropic_rejects_invalid_thinking_combinations(
    kwargs: dict[str, Any], match: str
) -> None:
    seen, transport = capture(ANTHROPIC_REPLY)
    with pytest.raises(SDKValidationError, match=match):
        await run(
            AnthropicNativeProvider("claude-x", api_key="k", transport=transport),
            ModelSettings(**kwargs),
        )
    assert seen == []  # nothing invalid was sent


async def test_anthropic_thinking_with_a_forced_tool_choice_is_refused() -> None:
    # F35 replaced the F30 blanket refusal of thinking + tools: tools now work (see
    # test_anthropic_thinking.py); only forcing a tool call stays invalid for Anthropic.
    from nexura_agents import tool

    ran: list[str] = []

    @tool
    def charge(amount: int) -> str:
        """Charge."""
        ran.append("charged")
        return "ok"

    seen, transport = capture(ANTHROPIC_REPLY)
    agent = Agent(
        name="A",
        instructions="i",
        model=AnthropicNativeProvider("claude-x", api_key="k", transport=transport),
        tools=[charge],
        model_settings=ModelSettings(
            thinking_budget_tokens=2000, max_tokens=8000, tool_choice="required"
        ),
    )
    with pytest.raises(SDKValidationError, match="cannot force a tool call"):
        await Runner().run_async(agent, "go")
    assert seen == [] and ran == []


async def test_anthropic_zero_budget_leaves_thinking_off() -> None:
    seen, transport = capture(ANTHROPIC_REPLY)
    await run(
        AnthropicNativeProvider("claude-x", api_key="k", transport=transport),
        ModelSettings(thinking_budget_tokens=0),
    )
    assert "thinking" not in seen[0]


async def test_anthropic_thinking_blocks_never_become_the_answer() -> None:
    reply = dict(ANTHROPIC_REPLY)
    reply["content"] = [
        {"type": "thinking", "thinking": "secret chain of thought", "signature": "sig"},
        {"type": "text", "text": "final answer"},
    ]
    _, transport = capture(reply)
    agent = Agent(
        name="A",
        instructions="i",
        model=AnthropicNativeProvider("claude-x", api_key="k", transport=transport),
        model_settings=ModelSettings(thinking_budget_tokens=2000, max_tokens=8000),
    )
    result = await Runner().run_async(agent, "go")
    assert result.output == "final answer"
    assert "secret" not in str([m.content for m in result.messages])


async def test_extra_cannot_replace_fields_the_sdk_sets() -> None:
    seen, transport = capture(OPENAI_REPLY)
    for key in ("model", "messages", "temperature"):
        with pytest.raises(SDKValidationError, match=f"cannot replace {key}"):
            await run(
                OpenAIProvider("gpt-x", api_key="k", transport=transport),
                ModelSettings(temperature=0.3, extra={key: "evil"}),
            )
    assert seen == []


async def test_anthropic_streamed_thinking_is_not_emitted_as_text() -> None:
    events_sse = [
        ("message_start", {"type": "message_start", "message": {"usage": {"input_tokens": 1}}}),
        (
            "content_block_start",
            {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking"}},
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "thinking_delta", "thinking": "secret reasoning"},
            },
        ),
        (
            "content_block_start",
            {"type": "content_block_start", "index": 1, "content_block": {"type": "text"}},
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "text_delta", "text": "answer"},
            },
        ),
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 2},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    ]
    body = "".join(f"event: {e}\ndata: {json.dumps(d)}\n\n" for e, d in events_sse)
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=body.encode()
        )

    agent = Agent(
        name="A",
        instructions="i",
        model=AnthropicNativeProvider(
            "claude-x", api_key="k", transport=httpx.MockTransport(handler)
        ),
        model_settings=ModelSettings(thinking_budget_tokens=2000, max_tokens=8000, top_p=0.97),
    )
    events = [e async for e in Runner().stream_async(agent, "go", stream_text=True)]
    text = "".join(e.data["text"] for e in events if e.type == "text_delta")
    assert text == "answer"
    assert seen[0]["thinking"] == {"type": "enabled", "budget_tokens": 2000}
    assert seen[0]["stream"] is True
