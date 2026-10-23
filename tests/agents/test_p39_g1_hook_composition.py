"""P39 G1: ``Runner(hooks=[a, b])`` composes hooks objects (``ComposedHooks``).

Observer hooks run in list order (``on_agent_end`` / ``on_run_end`` in reverse), takeover
hooks use the first non-None answer (``before_*``) or chain in reverse (``after_*``), and
exceptions propagate exactly as from a single hooks object. All offline (``FakeModel``).
"""

from __future__ import annotations

from typing import Any

import pytest
from nexura_agents import (
    Agent,
    AgentHooks,
    ComposedHooks,
    FakeModel,
    GuardrailResult,
    Message,
    ModelResponse,
    ModelRetry,
    Runner,
    ToolCall,
    handoff,
    tool,
)
from nexura_agents.errors import SDKValidationError, ToolExecutionError
from nexura_agents.events import RunEventType

RAN: list[str] = []


@tool(max_retries=1)
def lookup(city: str) -> str:
    """Look up a city."""
    RAN.append(city)
    return f"sunny in {city}"


def reply(text: str) -> ModelResponse:
    return ModelResponse(message=Message(role="assistant", content=text))


def calls(city: str = "Pune", call_id: str = "c1") -> list[ToolCall]:
    return [ToolCall(id=call_id, name="lookup", arguments={"city": city})]


class Recorder(AgentHooks):
    """Logs every hook call as ``<label>:<hook>`` into a shared list."""

    def __init__(self, label: str, log: list[str]) -> None:
        self.label = label
        self.log = log

    def note(self, hook: str) -> None:
        self.log.append(f"{self.label}:{hook}")

    async def on_run_start(self, ctx: Any) -> None:
        self.note("on_run_start")

    async def on_agent_start(self, ctx: Any, agent: Any) -> None:
        self.note(f"on_agent_start({agent.name})")

    async def on_agent_end(self, ctx: Any, agent: Any, output: Any) -> None:
        self.note(f"on_agent_end({agent.name})")

    async def on_handoff(self, ctx: Any, source: Any, target: Any) -> None:
        self.note("on_handoff")

    async def on_run_end(self, ctx: Any, output: Any) -> None:
        self.note("on_run_end")

    async def before_model(self, ctx: Any, messages: Any) -> Any:
        self.note("before_model")
        return None

    async def after_model(self, ctx: Any, response: Any) -> Any:
        self.note("after_model")
        return None

    async def before_tool(self, ctx: Any, call: Any) -> Any:
        self.note("before_tool")
        return None

    async def after_tool(self, ctx: Any, call: Any, result: Any) -> Any:
        self.note("after_tool")
        return None


# ---------------------------------------------------------------- observer ordering


async def test_observer_hooks_run_in_list_order_and_end_hooks_in_reverse() -> None:
    log: list[str] = []
    specialist = Agent(name="B", instructions="b", model=FakeModel(responses=["from B"]))
    front = Agent(
        name="A",
        instructions="a",
        model=FakeModel(responses=[[ToolCall(id="h", name="transfer_to_b", arguments={})]]),
        handoffs=[handoff(specialist)],
    )
    runner = Runner(hooks=[Recorder("p", log), Recorder("q", log)])
    result = await runner.run_async(front, "hi")
    assert result.output == "from B"
    assert log == [
        "p:on_run_start",
        "q:on_run_start",
        "p:on_agent_start(A)",
        "q:on_agent_start(A)",
        "p:before_model",
        "q:before_model",
        "q:after_model",
        "p:after_model",
        "q:on_agent_end(A)",  # the handoff ends A: reverse order
        "p:on_agent_end(A)",
        "p:on_handoff",
        "q:on_handoff",
        "p:on_agent_start(B)",
        "q:on_agent_start(B)",
        "p:before_model",
        "q:before_model",
        "q:after_model",
        "p:after_model",
        "q:on_agent_end(B)",
        "p:on_agent_end(B)",
        "q:on_run_end",
        "p:on_run_end",
    ]


async def test_tool_hooks_run_before_in_order_and_after_in_reverse() -> None:
    log: list[str] = []
    RAN.clear()
    agent = Agent(
        name="A", instructions="i", model=FakeModel(responses=[calls(), "ok"]), tools=[lookup]
    )
    runner = Runner(hooks=[Recorder("p", log), Recorder("q", log), Recorder("r", log)])
    await runner.run_async(agent, "go")
    tool_hooks = [e for e in log if "tool" in e]
    assert tool_hooks == [
        "p:before_tool",
        "q:before_tool",
        "r:before_tool",
        "r:after_tool",
        "q:after_tool",
        "p:after_tool",
    ]
    assert RAN == ["Pune"]


# ---------------------------------------------------------------- takeover precedence


async def test_before_model_first_non_none_wins_and_later_members_are_skipped() -> None:
    log: list[str] = []

    class Answer(Recorder):
        def __init__(self, label: str, text: str | None) -> None:
            super().__init__(label, log)
            self.text = text

        async def before_model(self, ctx: Any, messages: Any) -> Any:
            self.note("before_model")
            return reply(self.text) if self.text is not None else None

    model = FakeModel(responses=["from model"])
    agent = Agent(name="A", instructions="i", model=model)
    runner = Runner(hooks=[Answer("p", None), Answer("q", "from q"), Answer("r", "from r")])
    events = [e async for e in runner.stream_async(agent, "hi")]
    done = next(e for e in events if e.type == RunEventType.RUN_COMPLETED)
    assert done.data["output"] == "from q"
    assert model.calls == []  # the model is not called
    assert done.data["usage"].total_tokens is None  # nothing billed
    assert [e for e in log if "before_model" in e] == ["p:before_model", "q:before_model"]
    # after_model still runs for the hook-supplied reply, in reverse order.
    assert [e for e in log if "after_model" in e] == [
        "r:after_model",
        "q:after_model",
        "p:after_model",
    ]


async def test_after_model_chains_in_reverse_and_the_last_replacement_wins() -> None:
    seen: list[tuple[str, str]] = []

    class Rewrite(AgentHooks):
        def __init__(self, label: str, replace: bool) -> None:
            self.label = label
            self.replace = replace

        async def after_model(self, ctx: Any, response: ModelResponse) -> Any:
            text = response.message.content or ""
            seen.append((self.label, text))
            return reply(f"{text}+{self.label}") if self.replace else None

    agent = Agent(name="A", instructions="i", model=FakeModel(responses=["base"]))
    runner = Runner(
        hooks=[Rewrite("outer", True), Rewrite("middle", False), Rewrite("inner", True)]
    )
    result = await runner.run_async(agent, "hi")
    # inner runs first on the original; middle sees inner's replacement; outer sees it too.
    assert seen == [("inner", "base"), ("middle", "base+inner"), ("outer", "base+inner")]
    assert result.output == "base+inner+outer"
    assert result.messages[-1].content == "base+inner+outer"


async def test_after_model_with_no_replacement_keeps_the_original() -> None:
    agent = Agent(name="A", instructions="i", model=FakeModel(responses=["base"]))
    runner = Runner(hooks=[AgentHooks(), AgentHooks()])
    assert (await runner.run_async(agent, "hi")).output == "base"
    composed = ComposedHooks(AgentHooks(), AgentHooks())
    assert await composed.after_model(None, reply("x")) is None  # type: ignore[arg-type]
    assert await composed.after_tool(None, None, "x") is None  # type: ignore[arg-type]


async def test_before_tool_first_string_wins_tool_skipped_after_hooks_and_guardrails_run() -> None:
    log: list[str] = []
    checked: list[str] = []

    def see(ctx: Any, text: Any) -> GuardrailResult:
        checked.append(str(text))
        return GuardrailResult.passed()

    @tool(output_guardrails=[see])
    def risky(city: str) -> str:
        """Risky."""
        log.append("tool ran")
        return "ran"

    class Block(Recorder):
        def __init__(self, label: str, answer: str | None) -> None:
            super().__init__(label, log)
            self.answer = answer

        async def before_tool(self, ctx: Any, call: Any) -> Any:
            self.note("before_tool")
            return self.answer

        async def after_tool(self, ctx: Any, call: Any, result: Any) -> Any:
            self.note(f"after_tool({result})")
            return None

    agent = Agent(
        name="A",
        instructions="i",
        model=FakeModel(
            responses=[[ToolCall(id="c1", name="risky", arguments={"city": "x"})], "k"]
        ),
        tools=[risky],
    )
    runner = Runner(hooks=[Block("p", None), Block("q", "blocked by q"), Block("r", "by r")])
    result = await runner.run_async(agent, "go")
    assert "tool ran" not in log
    assert [e for e in log if "before_tool" in e] == ["p:before_tool", "q:before_tool"]
    assert [e for e in log if "after_tool" in e] == [
        "r:after_tool(blocked by q)",
        "q:after_tool(blocked by q)",
        "p:after_tool(blocked by q)",
    ]
    assert checked == ["blocked by q"]  # output guardrails still saw the hook's answer
    tool_msg = next(m for m in result.messages if m.role == "tool")
    assert tool_msg.content == "blocked by q"


async def test_after_tool_chains_in_reverse() -> None:
    class Tag(AgentHooks):
        def __init__(self, label: str) -> None:
            self.label = label

        async def after_tool(self, ctx: Any, call: Any, result: str) -> Any:
            return f"{result}|{self.label}"

    RAN.clear()
    agent = Agent(
        name="A", instructions="i", model=FakeModel(responses=[calls(), "ok"]), tools=[lookup]
    )
    result = await Runner(hooks=[Tag("outer"), Tag("inner")]).run_async(agent, "go")
    tool_msg = next(m for m in result.messages if m.role == "tool")
    assert tool_msg.content == "sunny in Pune|inner|outer"


# ---------------------------------------------------------------- exceptions


class Boom(RuntimeError):
    pass


async def test_an_exception_stops_the_remaining_members_and_propagates() -> None:
    log: list[str] = []

    class Raiser(Recorder):
        async def on_run_start(self, ctx: Any) -> None:
            self.note("on_run_start")
            raise Boom("no")

    agent = Agent(name="A", instructions="i", model=FakeModel(responses=["x"]))
    with pytest.raises(Boom):
        await Runner(hooks=[Recorder("p", log), Raiser("q", log), Recorder("r", log)]).run_async(
            agent, "hi"
        )
    assert log == ["p:on_run_start", "q:on_run_start"]


@pytest.mark.parametrize("hook", ["before_model", "after_model", "before_tool", "after_tool"])
async def test_exceptions_propagate_exactly_as_from_a_single_hooks_object(hook: str) -> None:
    class Raiser(AgentHooks):
        pass

    async def boom(self: Any, *args: Any) -> Any:
        raise Boom(hook)

    setattr(Raiser, hook, boom)

    async def outcome(hooks: Any) -> tuple[type[BaseException], list[RunEventType]]:
        RAN.clear()
        agent = Agent(
            name="A", instructions="i", model=FakeModel(responses=[calls(), "ok"]), tools=[lookup]
        )
        events: list[RunEventType] = []
        try:
            async for event in Runner(hooks=hooks).stream_async(agent, "go"):
                events.append(event.type)
        except BaseException as exc:  # noqa: BLE001 - comparing what escapes
            return type(exc), events
        raise AssertionError("expected an exception")

    single = await outcome(Raiser())
    composed = await outcome([AgentHooks(), Raiser(), AgentHooks()])
    assert single == composed
    assert single[0] in (Boom, ToolExecutionError)


async def test_model_retry_from_a_member_keeps_its_meaning() -> None:
    class Refuse(AgentHooks):
        async def before_tool(self, ctx: Any, call: Any) -> Any:
            if call.arguments["city"] == "secret":
                raise ModelRetry("not that one")
            return None

    RAN.clear()
    agent = Agent(
        name="A",
        instructions="i",
        model=FakeModel(responses=[calls("secret", "c1"), calls("Pune", "c2"), "done"]),
        tools=[lookup],
    )
    result = await Runner(hooks=[AgentHooks(), Refuse()]).run_async(agent, "go")
    assert result.output == "done"
    assert RAN == ["Pune"]
    first = next(m for m in result.messages if m.role == "tool")
    assert "not that one" in (first.content or "")


async def test_invalid_return_value_names_the_member() -> None:
    class Bad(AgentHooks):
        async def before_model(self, ctx: Any, messages: Any) -> Any:
            return "not a response"

    agent = Agent(name="A", instructions="i", model=FakeModel(responses=["x"]))
    with pytest.raises(SDKValidationError, match="before_model of Bad must return None"):
        await Runner(hooks=[AgentHooks(), Bad()]).run_async(agent, "hi")


# ---------------------------------------------------------------- back-compat and API


def test_a_single_hooks_object_is_used_as_is() -> None:
    hooks = AgentHooks()
    runner = Runner(hooks=hooks)
    assert runner.hooks is hooks  # isinstance checks and identity are unaffected
    assert Runner().hooks is None
    assert Runner(hooks=[]).hooks is None


def test_a_list_becomes_composed_hooks_also_when_assigned_later() -> None:
    a, b = AgentHooks(), AgentHooks()
    runner = Runner(hooks=[a, b])
    assert isinstance(runner.hooks, ComposedHooks) and runner.hooks.members == (a, b)
    runner.hooks = (b, a)
    assert isinstance(runner.hooks, ComposedHooks) and runner.hooks.members == (b, a)
    runner.hooks = a
    assert runner.hooks is a


def test_nested_compositions_are_flattened_and_bad_members_refused() -> None:
    a, b, c = AgentHooks(), AgentHooks(), AgentHooks()
    assert ComposedHooks(a, ComposedHooks(b, c)).members == (a, b, c)
    assert "AgentHooks" in repr(ComposedHooks(a))
    with pytest.raises(SDKValidationError):
        ComposedHooks(a, None)
    with pytest.raises(SDKValidationError):
        Runner(hooks="hooks")  # type: ignore[arg-type]


async def test_members_may_omit_methods() -> None:
    log: list[str] = []

    class OnlyStart:  # not an AgentHooks subclass
        async def on_run_start(self, ctx: Any) -> None:
            log.append("start")

    RAN.clear()
    agent = Agent(
        name="A", instructions="i", model=FakeModel(responses=[calls(), "ok"]), tools=[lookup]
    )
    members: list[Any] = [OnlyStart(), AgentHooks()]  # duck-typed members are accepted
    result = await Runner(hooks=members).run_async(agent, "go")
    assert result.output == "ok" and log == ["start"]


async def test_single_hooks_behaviour_is_unchanged() -> None:
    """The F48 takeover example, with one hooks object and with it as a one-item list."""

    class Cache(AgentHooks):
        async def before_model(self, ctx: Any, messages: Any) -> Any:
            return reply("cached") if (messages[-1].content or "") == "cached?" else None

        async def after_model(self, ctx: Any, response: ModelResponse) -> Any:
            text = (response.message.content or "").replace("4111", "****")
            return reply(text)

    for hooks in (Cache(), [Cache()]):
        model = FakeModel(responses=["card 4111"])
        agent = Agent(name="A", instructions="i", model=model)
        runner = Runner(hooks=hooks)
        assert (await runner.run_async(agent, "cached?")).output == "cached"
        assert model.calls == []
        assert (await runner.run_async(agent, "card?")).output == "card ****"
