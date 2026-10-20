"""F35: Anthropic extended thinking together with tools (thinking blocks sent back)."""

from __future__ import annotations

import json
from typing import Any

import httpx
from nexura_agents import Agent, MemorySessionStore, ModelSettings, Runner, tool
from nexura_agents.providers.anthropic.provider import AnthropicNativeProvider

THINKING = {"type": "thinking", "thinking": "I should look the order up.", "signature": "sig-1"}


def reply(content: list[dict[str, Any]], stop: str) -> dict[str, Any]:
    return {
        "id": "m",
        "type": "message",
        "role": "assistant",
        "content": content,
        "stop_reason": stop,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }


def scripted(replies: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], httpx.MockTransport]:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=replies[len(seen) - 1])

    return seen, httpx.MockTransport(handler)


@tool
def lookup(order: str) -> str:
    """Look an order up."""
    return f"{order}: shipped"


def thinking_agent(transport: httpx.MockTransport) -> Agent:
    return Agent(
        name="A",
        instructions="Help.",
        model=AnthropicNativeProvider("claude-x", api_key="k", transport=transport),
        tools=[lookup],
        model_settings=ModelSettings(thinking_budget_tokens=2000, max_tokens=8000),
    )


TOOL_TURN = reply(
    [THINKING, {"type": "tool_use", "id": "t1", "name": "lookup", "input": {"order": "A7"}}],
    "tool_use",
)
FINAL = reply([{"type": "text", "text": "A7 has shipped."}], "end_turn")


async def test_thinking_blocks_are_sent_back_with_the_tool_call() -> None:
    seen, transport = scripted([TOOL_TURN, FINAL])
    result = await Runner().run_async(thinking_agent(transport), "Where is A7?")
    assert result.output == "A7 has shipped."
    assert seen[0]["thinking"] == {"type": "enabled", "budget_tokens": 2000}
    assistant = [m for m in seen[1]["messages"] if m["role"] == "assistant"][-1]
    assert assistant["content"][0] == THINKING  # first, with its signature
    assert assistant["content"][1]["type"] == "tool_use"
    assert "should look" not in str([m.content for m in result.messages])  # never the answer


async def test_streamed_thinking_blocks_are_assembled_and_sent_back() -> None:
    events = [
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
                "delta": {"type": "thinking_delta", "thinking": "I should look "},
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "thinking_delta", "thinking": "the order up."},
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "signature_delta", "signature": "sig-1"},
            },
        ),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        (
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {"type": "tool_use", "id": "t1", "name": "lookup"},
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "input_json_delta", "partial_json": '{"order": "A7"}'},
            },
        ),
        ("content_block_stop", {"type": "content_block_stop", "index": 1}),
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use"},
                "usage": {"output_tokens": 2},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    ]
    sse = "".join(f"event: {e}\ndata: {json.dumps(d)}\n\n" for e, d in events)
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        if len(seen) == 1:
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, content=sse.encode()
            )
        final = [
            ("message_start", {"type": "message_start", "message": {"usage": {"input_tokens": 1}}}),
            (
                "content_block_start",
                {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}},
            ),
            (
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "A7 has shipped."},
                },
            ),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
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
        body = "".join(f"event: {e}\ndata: {json.dumps(d)}\n\n" for e, d in final)
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=body.encode()
        )

    agent = thinking_agent(httpx.MockTransport(handler))
    events_out = [e async for e in Runner().stream_async(agent, "Where is A7?", stream_text=True)]
    text = "".join(e.data["text"] for e in events_out if e.type == "text_delta")
    assert text == "A7 has shipped."
    assistant = [m for m in seen[1]["messages"] if m["role"] == "assistant"][-1]
    assert assistant["content"][0] == {
        "type": "thinking",
        "thinking": "I should look the order up.",
        "signature": "sig-1",
    }


async def test_thinking_blocks_survive_a_session_round_trip() -> None:
    seen, transport = scripted([TOOL_TURN, FINAL, FINAL])
    store = MemorySessionStore()
    runner = Runner(session_store=store)
    await runner.run_async(thinking_agent(transport), "Where is A7?", session_id="s")
    saved = await store.get("s")
    assert saved is not None
    with_thinking = [m for m in saved.messages if m.provider_data]
    assert with_thinking and with_thinking[0].provider_data == {"anthropic_thinking": [THINKING]}
    await runner.run_async(thinking_agent(transport), "Thanks", session_id="s")
    replayed = [m for m in seen[2]["messages"] if m["role"] == "assistant"]
    assert replayed[0]["content"][0] == THINKING


async def test_redacted_thinking_is_kept_too() -> None:
    redacted = {"type": "redacted_thinking", "data": "opaque"}
    turn = reply(
        [redacted, {"type": "tool_use", "id": "t1", "name": "lookup", "input": {"order": "B"}}],
        "tool_use",
    )
    seen, transport = scripted([turn, FINAL])
    await Runner().run_async(thinking_agent(transport), "B?")
    assistant = [m for m in seen[1]["messages"] if m["role"] == "assistant"][-1]
    assert assistant["content"][0] == redacted


def test_thinking_blocks_stay_first_when_assistant_turns_are_merged() -> None:
    from nexura_agents import Message, ToolCall
    from nexura_agents.providers.anthropic.types import to_anthropic_messages

    _, turns = to_anthropic_messages(
        [
            Message(role="user", content="q"),
            Message(role="assistant", content="first"),
            Message(
                role="assistant",
                tool_calls=[ToolCall(id="t1", name="f", arguments={})],
                provider_data={"anthropic_thinking": [THINKING]},
            ),
            Message(role="tool", content="r", tool_call_id="t1"),
        ]
    )
    assistant = [t for t in turns if t["role"] == "assistant"][0]
    assert [b["type"] for b in assistant["content"]] == ["thinking", "text", "tool_use"]
