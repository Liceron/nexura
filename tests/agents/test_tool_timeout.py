"""Per-tool timeouts: one slow tool cannot hold up the whole run."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from nexura_agents import Agent, FakeModel, Runner, SchemaTool, StopAtTools, ToolCall, tool
from nexura_agents.errors import ModelTimeoutError, SDKValidationError, ToolExecutionError


def call(name: str, **arguments: Any) -> list[ToolCall]:
    return [ToolCall(id="c1", name=name, arguments=arguments)]


@tool(timeout=0.1)
async def slow(x: str) -> str:
    """Slow."""
    await asyncio.sleep(5)
    return "late"


def agent_with(tools: list[Any], responses: list[Any], **kw: Any) -> Agent:
    return Agent(
        name="A", instructions="i", model=FakeModel(responses=responses), tools=tools, **kw
    )


async def test_timed_out_call_tells_the_model_and_the_run_continues() -> None:
    started = time.monotonic()
    result = await Runner().run_async(agent_with([slow], [call("slow", x="a"), "sorry"]), "go")
    assert time.monotonic() - started < 2
    assert result.output == "sorry"
    tool_messages = [m.content for m in result.messages if m.role == "tool"]
    assert tool_messages == ["Tool call timed out: 'slow' did not finish within 0.1s."]


async def test_on_timeout_fail_stops_the_run() -> None:
    @tool(timeout=0.1, on_timeout="fail")
    async def strict(x: str) -> str:
        """Strict."""
        await asyncio.sleep(5)
        return "late"

    with pytest.raises(ToolExecutionError) as info:
        await Runner().run_async(agent_with([strict], [call("strict", x="a")]), "go")
    assert isinstance(info.value.original_error, TimeoutError)


async def test_fast_tool_is_unaffected() -> None:
    @tool(timeout=5)
    async def quick(x: str) -> str:
        """Quick."""
        return f"ok {x}"

    result = await Runner().run_async(agent_with([quick], [call("quick", x="a"), "done"]), "go")
    assert [m.content for m in result.messages if m.role == "tool"] == ["ok a"]


async def test_tighter_run_timeout_still_wins() -> None:
    @tool(timeout=10)
    async def patient(x: str) -> str:
        """Patient."""
        await asyncio.sleep(5)
        return "late"

    agent = agent_with([patient], [call("patient", x="a")], timeout=0.2)
    with pytest.raises(ModelTimeoutError):
        await Runner().run_async(agent, "go")


async def test_sync_tool_timeout() -> None:
    @tool(timeout=0.1)
    def blocking(x: str) -> str:
        """Blocking."""
        time.sleep(0.5)
        return "late"

    result = await Runner().run_async(agent_with([blocking], [call("blocking", x="a"), "ok"]), "go")
    assert "timed out" in str([m.content for m in result.messages if m.role == "tool"])


async def test_timed_out_call_never_ends_a_run_under_tool_use_behavior() -> None:
    agent = agent_with(
        [slow], [call("slow", x="a"), "model answered"], tool_use_behavior=StopAtTools(["slow"])
    )
    result = await Runner().run_async(agent, "go")
    assert result.output == "model answered"


async def test_schema_tool_timeout() -> None:
    async def handler(ctx: Any, arguments: dict[str, Any]) -> str:
        await asyncio.sleep(5)
        return "late"

    remote = SchemaTool("remote", {"type": "object", "properties": {}}, handler, timeout=0.1)
    result = await Runner().run_async(agent_with([remote], [call("remote"), "ok"]), "go")
    assert "timed out" in str([m.content for m in result.messages if m.role == "tool"])


@pytest.mark.parametrize("bad", [0, -1, True, float("inf"), "5"])
def test_timeout_is_validated(bad: Any) -> None:
    with pytest.raises(SDKValidationError, match="timeout must be"):

        @tool(timeout=bad)
        def t(x: str) -> str:
            """T."""
            return x


def test_on_timeout_is_validated() -> None:
    with pytest.raises(SDKValidationError, match="on_timeout"):

        @tool(timeout=1, on_timeout="retry")  # type: ignore[arg-type]
        def t(x: str) -> str:
            """T."""
            return x


async def test_tool_served_over_mcp_keeps_its_timeout() -> None:
    from nexura_agents.mcp.serve import AgentMCPServer

    server = AgentMCPServer("s", tools=[slow])
    started = time.monotonic()
    reply = await server.session.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "slow", "arguments": {"x": "a"}},
        }
    )
    assert time.monotonic() - started < 2
    assert reply is not None and reply["result"]["isError"] is True
    assert "timed out" in reply["result"]["content"][0]["text"]
