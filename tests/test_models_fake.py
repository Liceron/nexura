"""Tests for FakeModel adapter and endless fixtures."""

import pytest

from nexura_runtime import FakeModel, Message, ModelResponse, ToolCall, Usage


@pytest.mark.asyncio
async def test_fake_model_canned_responses() -> None:
    model = FakeModel(
        responses=[
            "Hello there",
            [ToolCall(id="call_1", name="search", arguments={"q": "news"})],
            Message(role="assistant", content="How can I assist?"),
            ModelResponse(
                message=Message(role="assistant", content="With custom usage"),
                usage=Usage(prompt_tokens=5, completion_tokens=4, total_tokens=9),
            ),
        ]
    )

    r1 = await model.generate([Message(role="user", content="Hi")])
    assert r1.message.content == "Hello there"
    assert r1.message.tool_calls is None

    r2 = await model.generate([Message(role="user", content="Search")])
    assert r2.message.content is None
    assert len(r2.message.tool_calls or []) == 1
    assert r2.message.tool_calls[0].name == "search"  # type: ignore[index]

    r3 = await model.generate([Message(role="user", content="Ask")])
    assert r3.message.content == "How can I assist?"

    r4 = await model.generate([Message(role="user", content="Usage")])
    assert r4.message.content == "With custom usage"
    assert r4.usage is not None
    assert r4.usage.total_tokens == 9


@pytest.mark.asyncio
async def test_fake_model_endless_fixture() -> None:
    endless = FakeModel.endless(tool_name="worker_task", arguments={"step": 1})

    for _ in range(5):
        resp = await endless.generate([Message(role="user", content="Do next")])
        assert resp.message.tool_calls is not None
        assert len(resp.message.tool_calls) == 1
        tc = resp.message.tool_calls[0]
        assert tc.name == "worker_task"
        assert tc.arguments == {"step": 1}
        assert tc.id.startswith("call_")


@pytest.mark.asyncio
async def test_fake_model_custom_handler() -> None:
    def handler(messages: list[Message], tools: list[dict[str, object]] | None) -> str:
        last = messages[-1].content or ""
        return f"Echoed: {last}"

    model = FakeModel(handler=handler)
    resp = await model.generate([Message(role="user", content="Ping")])
    assert resp.message.content == "Echoed: Ping"
    assert len(model.calls) == 1
