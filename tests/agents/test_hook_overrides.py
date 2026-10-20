"""Hooks that take over: skip or replace model calls and tool results (F48)."""

from __future__ import annotations

from typing import Any

import pytest
from nexura_agents import Agent, AgentHooks, FakeModel, Message, ModelResponse, Runner, ToolCall
from nexura_agents import tool as make_tool
from nexura_agents.errors import SDKValidationError
from nexura_agents.events import RunEventType
from nexura_agents.run_context import RunContext
from nexura_agents.types import Usage

RAN: list[str] = []


@make_tool
def lookup(city: str) -> str:
    """Look up a city."""
    RAN.append(city)
    return f"sunny in {city}"


@make_tool(cache=True, idempotent=True)
def cached_lookup(city: str) -> str:
    """Look up a city (cached)."""
    RAN.append(city)
    return f"sunny in {city}"


def usage(n: int) -> Usage:
    return Usage(prompt_tokens=n, completion_tokens=n, total_tokens=2 * n)


def call(name: str = "lookup", city: str = "Pune", call_id: str = "c1") -> list[ToolCall]:
    return [ToolCall(id=call_id, name=name, arguments={"city": city})]


def reply(text: str) -> ModelResponse:
    return ModelResponse(message=Message(role="assistant", content=text))


async def test_before_model_can_answer_without_calling_or_billing_the_model() -> None:
    class Cache(AgentHooks):
        async def before_model(self, ctx: RunContext[Any], messages: list[Message]) -> Any:
            return reply("from cache")

    model = FakeModel(responses=["from model"], default_usage=usage(5))
    agent = Agent(name="A", instructions="i", model=model)
    events = [e async for e in Runner(hooks=Cache()).stream_async(agent, "hi")]
    done = next(e for e in events if e.type == RunEventType.RUN_COMPLETED)
    assert done.data["output"] == "from cache"
    assert model.calls == []  # the provider was never called
    assert done.data["usage"].total_tokens is None  # nothing billed
    completed = next(e for e in events if e.type == RunEventType.MODEL_COMPLETED)
    assert completed.data["from_hook"] is True and completed.data["usage"] is None


async def test_a_skipped_model_call_does_not_count_against_request_limits() -> None:
    from nexura_agents.limits import UsageLimits

    class Cache(AgentHooks):
        async def before_model(self, ctx: RunContext[Any], messages: list[Message]) -> Any:
            return reply("cached")

    agent = Agent(name="A", instructions="i", model=FakeModel(responses=["x"]))
    result = await Runner(hooks=Cache(), usage_limits=UsageLimits(request_limit=0)).run_async(
        agent, "hi"
    )
    assert result.output == "cached"


async def test_after_model_replaces_the_reply_but_usage_counts_what_was_billed() -> None:
    class Redact(AgentHooks):
        async def after_model(self, ctx: RunContext[Any], response: ModelResponse) -> Any:
            text = (response.message.content or "").replace("4111", "[card]")
            return ModelResponse(message=Message(role="assistant", content=text))

    model = FakeModel(responses=["card 4111 ok"], default_usage=usage(3))
    agent = Agent(name="A", instructions="i", model=model)
    result = await Runner(hooks=Redact()).run_async(agent, "hi")
    assert result.output == "card [card] ok"
    assert result.usage.total_tokens == 6
    assert result.messages[-1].content == "card [card] ok"  # the stored transcript too


async def test_before_tool_can_answer_instead_of_running_the_tool() -> None:
    class Stub(AgentHooks):
        async def before_tool(self, ctx: RunContext[Any], call: ToolCall) -> Any:
            return "stubbed weather"

    RAN.clear()
    model = FakeModel(responses=[call(), "done"])
    agent = Agent(name="A", instructions="i", model=model, tools=[lookup])
    result = await Runner(hooks=Stub()).run_async(agent, "go")
    assert RAN == []
    assert [m.content for m in result.messages if m.role == "tool"] == ["stubbed weather"]


async def test_a_hook_answer_is_not_cached_and_after_tool_still_sees_it() -> None:
    seen: list[str] = []

    class Once(AgentHooks):
        def __init__(self) -> None:
            self.first = True

        async def before_tool(self, ctx: RunContext[Any], call: ToolCall) -> Any:
            if self.first:
                self.first = False
                return "from hook"
            return None

        async def after_tool(self, ctx: RunContext[Any], call: ToolCall, result: str) -> Any:
            seen.append(result)
            return None

    RAN.clear()
    calls = [call("cached_lookup", call_id="c1"), call("cached_lookup", call_id="c2"), "done"]
    agent = Agent(
        name="A", instructions="i", model=FakeModel(responses=calls), tools=[cached_lookup]
    )
    await Runner(hooks=Once()).run_async(agent, "go")
    assert seen == ["from hook", "sunny in Pune"]  # the second call really ran
    assert RAN == ["Pune"]


async def test_after_tool_rewrites_the_result_before_output_guardrails() -> None:
    from nexura_agents import GuardrailResult

    checked: list[str] = []

    def guard(ctx: Any, result: str) -> GuardrailResult:
        checked.append(result)
        return GuardrailResult.passed()

    @make_tool(output_guardrails=[guard])
    def secret() -> str:
        """Secret."""
        return "token=abc123"

    class Mask(AgentHooks):
        async def after_tool(self, ctx: RunContext[Any], call: ToolCall, result: str) -> Any:
            return result.replace("abc123", "***")

    model = FakeModel(responses=[[ToolCall(id="c1", name="secret", arguments={})], "done"])
    agent = Agent(name="A", instructions="i", model=model, tools=[secret])
    result = await Runner(hooks=Mask()).run_async(agent, "go")
    assert checked == ["token=***"]
    assert [m.content for m in result.messages if m.role == "tool"] == ["token=***"]


async def test_after_tool_rewrite_withholds_the_tools_files() -> None:
    import base64

    from nexura_agents import ToolOutput
    from nexura_agents.attachments import Attachment

    png = Attachment.from_bytes(base64.b64decode("iVBORw0KGgo="), "image/png", name="x.png")

    @make_tool
    def snap() -> ToolOutput:
        """Snap."""
        return ToolOutput("photo", [png])

    class Rewrite(AgentHooks):
        async def after_tool(self, ctx: RunContext[Any], call: ToolCall, result: str) -> Any:
            return "[photo removed]"

    model = FakeModel(responses=[[ToolCall(id="c1", name="snap", arguments={})], "done"])
    agent = Agent(name="A", instructions="i", model=model, tools=[snap])
    await Runner(hooks=Rewrite()).run_async(agent, "go")
    sent = [a for m in model.calls[1]["messages"] for a in (m.attachments or [])]
    assert sent == []


@pytest.mark.parametrize(
    "hook, bad",
    [
        ("before_model", "text"),
        ("after_model", {"content": "x"}),
        ("before_tool", 42),
        ("after_tool", b"x"),
    ],
)
async def test_wrong_return_types_fail_clearly(hook: str, bad: Any) -> None:
    class Bad(AgentHooks):
        pass

    async def returns_bad(self: Any, *args: Any) -> Any:
        return bad

    setattr(Bad, hook, returns_bad)
    model = FakeModel(responses=[call(), "done"])
    agent = Agent(name="A", instructions="i", model=model, tools=[lookup])
    with pytest.raises(SDKValidationError, match=f"AgentHooks.{hook} must return None or a"):
        await Runner(hooks=Bad()).run_async(agent, "go")


async def test_hooks_that_return_none_change_nothing() -> None:
    class Observe(AgentHooks):
        async def before_model(self, ctx: RunContext[Any], messages: list[Message]) -> None:
            return None

        async def after_tool(self, ctx: RunContext[Any], call: ToolCall, result: str) -> None:
            return None

    RAN.clear()
    model = FakeModel(responses=[call(), "done"], default_usage=usage(1))
    agent = Agent(name="A", instructions="i", model=model, tools=[lookup])
    result = await Runner(hooks=Observe()).run_async(agent, "go")
    assert result.output == "done" and RAN == ["Pune"]
    assert result.usage.total_tokens == 4 and len(model.calls) == 2


async def test_after_model_also_sees_a_reply_from_before_model() -> None:
    seen: list[str | None] = []

    class Both(AgentHooks):
        async def before_model(self, ctx: RunContext[Any], messages: list[Message]) -> Any:
            return reply("cached")

        async def after_model(self, ctx: RunContext[Any], response: ModelResponse) -> Any:
            seen.append(response.message.content)
            return None

    agent = Agent(name="A", instructions="i", model=FakeModel(responses=["x"]))
    await Runner(hooks=Both()).run_async(agent, "hi")
    assert seen == ["cached"]
