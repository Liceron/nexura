"""TOOL_CALL_DELTA events: tool-call arguments as the model streams them (F43)."""

from __future__ import annotations

import json
from typing import Any

import httpx
from nexura_agents import Agent, Runner, tool
from nexura_agents.events import RunEventType
from nexura_agents.providers.fake import StreamingFakeProvider
from nexura_agents.providers.openai.provider import OpenAIProvider
from nexura_agents.providers.types import StreamDelta

RAN: list[dict[str, Any]] = []


@tool
def lookup(city: str) -> str:
    """Look up a city."""
    RAN.append({"city": city})
    return f"sunny in {city}"


def call_deltas(*parts: StreamDelta) -> list[StreamDelta]:
    return [*parts, StreamDelta(kind="finish", finish_reason="tool_calls")]


def answer(text: str) -> list[StreamDelta]:
    return [
        StreamDelta(kind="text_delta", text=text),
        StreamDelta(kind="finish", finish_reason="stop"),
    ]


def piece(index: int, args: str, call_id: str | None = None, name: str | None = None) -> Any:
    return StreamDelta(
        kind="tool_call_delta",
        tool_call_index=index,
        tool_call_id=call_id,
        tool_name=name,
        tool_arguments_delta=args,
    )


def agent_for(*turns: list[StreamDelta]) -> Agent:
    model = StreamingFakeProvider("fake-stream", queued_deltas=list(turns))
    return Agent(name="A", instructions="i", model=model, tools=[lookup])


async def deltas_of(agent: Agent, **kw: Any) -> list[dict[str, Any]]:
    events = [e async for e in Runner().stream_async(agent, "go", **kw)]
    return [e.data for e in events if e.type == RunEventType.TOOL_CALL_DELTA]


async def test_argument_pieces_stream_with_the_id_and_name_once_known() -> None:
    RAN.clear()
    agent = agent_for(
        call_deltas(piece(0, "", "c1", "lookup"), piece(0, '{"city": '), piece(0, '"Pune"}')),
        answer("done"),
    )
    seen = await deltas_of(agent, stream_text=True)
    assert [(d["index"], d["id"], d["name"]) for d in seen] == [(0, "c1", "lookup")] * 3
    assert "".join(d["arguments_delta"] for d in seen) == '{"city": "Pune"}'
    assert all(d["turn"] == 1 for d in seen)
    assert RAN == [{"city": "Pune"}]  # the assembled call still runs as before


async def test_interleaved_parallel_calls_keep_their_own_index() -> None:
    RAN.clear()
    agent = agent_for(
        call_deltas(
            piece(0, '{"city":', "c1", "lookup"),
            piece(1, '{"city":', "c2", "lookup"),
            piece(1, ' "Goa"}'),
            piece(0, ' "Pune"}'),
        ),
        answer("done"),
    )
    seen = await deltas_of(agent, stream_text=True)
    by_id: dict[str, str] = {}
    for d in seen:
        by_id[d["id"]] = by_id.get(d["id"], "") + d["arguments_delta"]
    assert {k: json.loads(v) for k, v in by_id.items()} == {
        "c1": {"city": "Pune"},
        "c2": {"city": "Goa"},
    }
    assert sorted(r["city"] for r in RAN) == ["Goa", "Pune"]


async def test_no_deltas_without_stream_text() -> None:
    agent = agent_for(call_deltas(piece(0, '{"city": "Pune"}', "c1", "lookup")), answer("ok"))
    assert await deltas_of(agent) == []


async def test_openai_provider_stream_reports_fragmented_arguments() -> None:
    chunks = [
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_9",
                                "function": {"name": "lookup", "arguments": ""},
                            }
                        ]
                    }
                }
            ]
        },
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"ci'}}]}}]},
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [{"index": 0, "function": {"arguments": 'ty": "Delhi"}'}}]
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        },
    ]
    replies = [
        "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n",
        'data: {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}\n\n'
        "data: [DONE]\n\n",
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"Content-Type": "text/event-stream"}, text=replies.pop(0)
        )

    model = OpenAIProvider(
        model="mock-model",  # local mock transport, no network
        api_key="test-key",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    RAN.clear()
    agent = Agent(name="A", instructions="i", model=model, tools=[lookup])
    seen = await deltas_of(agent, stream_text=True)
    assert {(d["id"], d["name"]) for d in seen} == {("call_9", "lookup")}
    assert "".join(d["arguments_delta"] for d in seen) == '{"city": "Delhi"}'
    assert RAN == [{"city": "Delhi"}]
