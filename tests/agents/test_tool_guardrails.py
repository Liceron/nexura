"""Tool guardrails: checks around each tool call that can pass, trip, reject or rewrite."""

from __future__ import annotations

from typing import Any

import pytest
from nexura_agents import (
    Agent,
    FakeModel,
    GuardrailResult,
    MemoryCheckpointStore,
    Runner,
    SchemaTool,
    StopAtTools,
    ToolCall,
    tool,
)
from nexura_agents.errors import (
    GuardrailTripwireTriggered,
    SDKValidationError,
    ToolExecutionError,
)
from nexura_agents.run_context import RunContext


def call(name: str, call_id: str = "c1", **arguments: Any) -> list[ToolCall]:
    return [ToolCall(id=call_id, name=name, arguments=arguments)]


def tool_results(result: Any) -> list[str | None]:
    return [m.content for m in result.messages if m.role == "tool"]


def run_agent(model: FakeModel, tools: list[Any], **kw: Any) -> Any:
    return Runner().run_async(
        Agent(name="A", instructions="i", model=model, tools=tools, **kw), "go"
    )


# ---------------------------------------------------------------- input guardrails


async def test_input_guardrail_passes_and_sees_arguments_and_tool_context() -> None:
    seen: list[Any] = []

    def check(ctx: RunContext[Any], args: dict[str, Any]) -> GuardrailResult:
        seen.append((ctx.tool_name, ctx.tool_call_id, args))
        return GuardrailResult.passed()

    @tool(input_guardrails=[check])
    def refund(amount: int) -> str:
        """Refund."""
        return f"refunded {amount}"

    result = await run_agent(FakeModel(responses=[call("refund", amount=5), "done"]), [refund])
    assert seen == [("refund", "c1", {"amount": 5})]
    assert tool_results(result) == ["refunded 5"]


async def test_input_guardrail_rejection_skips_the_tool_and_tells_the_model() -> None:
    ran: list[int] = []

    def limit(ctx: RunContext[Any], args: dict[str, Any]) -> GuardrailResult:
        if args["amount"] > 100:
            return GuardrailResult.rejected("refunds over 100 need a manager")
        return GuardrailResult.passed()

    @tool(input_guardrails=[limit])
    def refund(amount: int) -> str:
        """Refund."""
        ran.append(amount)
        return f"refunded {amount}"

    model = FakeModel(responses=[call("refund", amount=500), "I need a manager for that."])
    result = await run_agent(model, [refund])
    assert ran == []
    assert tool_results(result) == [
        "Tool call blocked by guardrail 'limit': refunds over 100 need a manager"
    ]
    assert result.output == "I need a manager for that."


async def test_input_guardrail_tripwire_stops_the_run_naming_the_tool() -> None:
    def no_wire(ctx: RunContext[Any], args: dict[str, Any]) -> GuardrailResult:
        return GuardrailResult.tripped({"why": "wire transfer"})

    @tool(input_guardrails=[no_wire])
    def transfer(to: str) -> str:
        """Transfer."""
        raise AssertionError("must not run")

    with pytest.raises(GuardrailTripwireTriggered) as info:
        await run_agent(FakeModel(responses=[call("transfer", to="x")]), [transfer])
    err = info.value
    assert (err.guardrail, err.stage, err.tool, err.info) == (
        "no_wire",
        "tool input",
        "transfer",
        {"why": "wire transfer"},
    )
    assert "on tool 'transfer'" in str(err)


async def test_input_guardrail_cannot_mutate_the_arguments_the_tool_gets() -> None:
    def meddle(ctx: RunContext[Any], args: dict[str, Any]) -> GuardrailResult:
        args["amount"] = 999
        return GuardrailResult.passed()

    @tool(input_guardrails=[meddle])
    def refund(amount: int) -> str:
        """Refund."""
        return f"refunded {amount}"

    result = await run_agent(FakeModel(responses=[call("refund", amount=5), "ok"]), [refund])
    assert tool_results(result) == ["refunded 5"]


async def test_input_guardrail_rewrite_is_an_error() -> None:
    def rewrite(ctx: RunContext[Any], args: dict[str, Any]) -> GuardrailResult:
        return GuardrailResult.rewritten("x")

    @tool(input_guardrails=[rewrite])
    def t(x: str) -> str:
        """T."""
        return x

    with pytest.raises(ToolExecutionError, match="pass, trip or reject only"):
        await run_agent(FakeModel(responses=[call("t", x="a")]), [t])


# ---------------------------------------------------------------- output guardrails


async def test_output_guardrail_rewrites_the_result_the_model_sees() -> None:
    order: list[str] = []

    def redact(ctx: RunContext[Any], text: Any) -> GuardrailResult:
        order.append(f"redact:{text}")
        return GuardrailResult.rewritten(str(text).replace("4111", "****"))

    async def audit(ctx: RunContext[Any], text: Any) -> GuardrailResult:
        order.append(f"audit:{text}")
        return GuardrailResult.passed()

    @tool(output_guardrails=[redact, audit])
    def card(user: str) -> str:
        """Card."""
        return "card 4111"

    result = await run_agent(FakeModel(responses=[call("card", user="u"), "ok"]), [card])
    assert tool_results(result) == ["card ****"]
    assert order == ["redact:card 4111", "audit:card ****"]  # later checks see the rewrite


async def test_output_guardrail_rejection_withholds_the_result() -> None:
    def secrets(ctx: RunContext[Any], text: Any) -> GuardrailResult:
        return GuardrailResult.rejected("the result contained a secret")

    @tool(output_guardrails=[secrets])
    def read(path: str) -> str:
        """Read."""
        return "API_KEY=abc"

    result = await run_agent(FakeModel(responses=[call("read", path="x"), "ok"]), [read])
    assert tool_results(result) == [
        "Tool result blocked by guardrail 'secrets': the result contained a secret"
    ]
    assert "abc" not in str([m.content for m in result.messages])


async def test_output_guardrail_tripwire_stops_the_run() -> None:
    def stop(ctx: RunContext[Any], text: Any) -> GuardrailResult:
        return GuardrailResult.tripped()

    @tool(output_guardrails=[stop])
    def t(x: str) -> str:
        """T."""
        return x

    with pytest.raises(GuardrailTripwireTriggered) as info:
        await run_agent(FakeModel(responses=[call("t", x="a")]), [t])
    assert info.value.stage == "tool output"


async def test_failing_guardrail_is_a_tool_execution_error() -> None:
    def broken(ctx: RunContext[Any], args: dict[str, Any]) -> GuardrailResult:
        raise RuntimeError("bug")

    @tool(input_guardrails=[broken])
    def t(x: str) -> str:
        """T."""
        return x

    with pytest.raises(ToolExecutionError) as info:
        await run_agent(FakeModel(responses=[call("t", x="a")]), [t])
    assert isinstance(info.value.original_error, RuntimeError)


async def test_guardrail_returning_the_wrong_type_fails() -> None:
    @tool(input_guardrails=[lambda ctx, args: True])
    def t(x: str) -> str:
        """T."""
        return x

    with pytest.raises(ToolExecutionError):
        await run_agent(FakeModel(responses=[call("t", x="a")]), [t])


# ---------------------------------------------------------------- integration


async def test_rejected_call_never_ends_a_run_under_tool_use_behavior() -> None:
    def block(ctx: RunContext[Any], args: dict[str, Any]) -> GuardrailResult:
        return GuardrailResult.rejected("no")

    @tool(input_guardrails=[block])
    def t(x: str) -> str:
        """T."""
        return x

    model = FakeModel(responses=[call("t", x="a"), "model answered"])
    result = await run_agent(model, [t], tool_use_behavior=StopAtTools(["t"]))
    assert result.output == "model answered"


async def test_schema_tool_guardrails() -> None:
    def block(ctx: RunContext[Any], args: dict[str, Any]) -> GuardrailResult:
        return GuardrailResult.rejected(f"blocked {args['q']}")

    async def handler(ctx: Any, arguments: dict[str, Any]) -> str:
        raise AssertionError("must not run")

    search = SchemaTool(
        "search",
        {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
        handler,
        input_guardrails=[block],
    )
    result = await run_agent(FakeModel(responses=[call("search", q="x"), "ok"]), [search])
    assert tool_results(result) == ["Tool call blocked by guardrail 'block': blocked x"]


async def test_guardrails_run_for_approved_calls_on_resume() -> None:
    seen: list[str] = []

    def check(ctx: RunContext[Any], args: dict[str, Any]) -> GuardrailResult:
        seen.append(args["x"])
        return GuardrailResult.passed()

    @tool(requires_approval=True, input_guardrails=[check])
    def send(x: str) -> str:
        """Send."""
        return "sent"

    agent = Agent(
        name="A",
        instructions="i",
        model=FakeModel(responses=[call("send", x="a"), "ok"]),
        tools=[send],
    )
    runner = Runner()
    paused = await runner.run_async(agent, "go")
    assert paused.interruption is not None and seen == []
    done = await runner.resume_async(
        agent, paused.interruption, approve=paused.interruption.pending_digests()
    )
    assert done.output == "ok" and seen == ["a"]


async def test_guardrails_rerun_when_durable_recovery_reruns_an_idempotent_tool() -> None:
    class Crash(BaseException):
        pass

    seen: list[str] = []
    crash = {"on": True}

    def check(ctx: RunContext[Any], args: dict[str, Any]) -> GuardrailResult:
        seen.append(args["x"])
        return GuardrailResult.passed()

    @tool(idempotent=True, input_guardrails=[check])
    def fetch(x: str) -> str:
        """Fetch."""
        if crash["on"]:
            raise Crash
        return "fetched"

    store = MemoryCheckpointStore()
    first = Agent(
        name="A", instructions="i", model=FakeModel(responses=[call("fetch", x="a")]), tools=[fetch]
    )
    with pytest.raises(Crash):
        await Runner(checkpoint_store=store).run_async(first, "go")
    crash["on"] = False
    again = Agent(name="A", instructions="i", model=FakeModel(responses=["ok"]), tools=[fetch])
    done = await Runner(checkpoint_store=store).recover_async(again, next(iter(store._data)))
    assert done.output == "ok"
    assert seen == ["a", "a"]  # checked again before the re-run


def test_agent_guardrail_cannot_reject() -> None:
    def reject(ctx: RunContext[Any], text: Any) -> GuardrailResult:
        return GuardrailResult.rejected("no")

    agent = Agent(
        name="A", instructions="i", model=FakeModel(responses=["x"]), input_guardrails=[reject]
    )
    with pytest.raises(TypeError, match="only tool guardrails can reject"):
        Runner().run(agent, "go")


@pytest.mark.parametrize("bad", [lambda c, v: None, "nope", [1]])
def test_tool_guardrails_are_validated(bad: Any) -> None:
    with pytest.raises(SDKValidationError, match="guardrail"):

        @tool(input_guardrails=bad)
        def t(x: str) -> str:
            """T."""
            return x


# ---------------------------------------------------------------- review fixes


async def test_tool_served_over_mcp_keeps_its_guardrails() -> None:
    from nexura_agents.mcp.serve import AgentMCPServer

    def block_big(ctx: RunContext[Any], args: dict[str, Any]) -> GuardrailResult:
        if args["amount"] > 100:
            return GuardrailResult.rejected("too large")
        return GuardrailResult.passed()

    def redact(ctx: RunContext[Any], text: Any) -> GuardrailResult:
        return GuardrailResult.rewritten(str(text).replace("SECRET", "***"))

    ran: list[int] = []

    @tool(input_guardrails=[block_big], output_guardrails=[redact])
    def refund(amount: int) -> str:
        """Refund."""
        ran.append(amount)
        return f"refunded {amount} SECRET"

    server = AgentMCPServer("s", tools=[refund])

    async def call_tool(amount: int) -> dict[str, Any]:
        reply = await server.session.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "refund", "arguments": {"amount": amount}},
            }
        )
        assert reply is not None
        return reply["result"]

    blocked = await call_tool(5000)
    assert blocked["isError"] is True
    assert "blocked by guardrail 'block_big': too large" in blocked["content"][0]["text"]
    assert ran == []
    ok = await call_tool(5)
    assert ok["isError"] is False
    assert ok["content"][0]["text"] == "refunded 5 ***"


async def test_slow_tool_guardrail_is_bounded_by_the_run_timeout() -> None:
    import asyncio
    import time

    from nexura_agents.errors import ModelTimeoutError

    async def slow(ctx: RunContext[Any], args: dict[str, Any]) -> GuardrailResult:
        await asyncio.sleep(3)
        return GuardrailResult.passed()

    @tool(input_guardrails=[slow])
    def t(x: str) -> str:
        """T."""
        return x

    agent = Agent(
        name="A",
        instructions="i",
        model=FakeModel(responses=[call("t", x="a")]),
        tools=[t],
        timeout=0.3,
    )
    started = time.monotonic()
    with pytest.raises(ModelTimeoutError):
        await Runner().run_async(agent, "go")
    assert time.monotonic() - started < 1.5


async def test_agent_input_guardrail_cannot_rewrite() -> None:
    def rewrite(ctx: RunContext[Any], text: Any) -> GuardrailResult:
        return GuardrailResult.rewritten("other prompt")

    agent = Agent(
        name="A", instructions="i", model=FakeModel(responses=["x"]), input_guardrails=[rewrite]
    )
    with pytest.raises(TypeError, match="only output guardrails can rewrite"):
        await Runner().run_async(agent, "go")
