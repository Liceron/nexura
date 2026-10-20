"""Dynamic tool availability: ``@tool(enabled=...)`` and ``Agent(prepare_tools=...)``."""

from __future__ import annotations

from typing import Any

import pytest
from nexura_agents import Agent, FakeModel, Runner, Tool, ToolCall, tool
from nexura_agents.errors import SDKValidationError, UnknownToolError
from nexura_agents.run_context import RunContext


def offered(model: FakeModel, call: int = 0) -> list[str]:
    tools = model.calls[call]["tools"] or []
    return [t["function"]["name"] for t in tools]


def make_agent(model: FakeModel, tools: list[Tool], **kwargs: Any) -> Agent:
    return Agent(name="A", instructions="i", model=model, tools=tools, **kwargs)


@tool
def search(q: str) -> str:
    """Search."""
    return f"found {q}"


@tool(enabled=False)
def hidden(q: str) -> str:
    """Never offered."""
    return "should not run"


def admin_only(ctx: RunContext[Any]) -> bool:
    return bool(ctx.deps and ctx.deps.get("admin"))


@tool(enabled=admin_only)
def delete_user(user: str) -> str:
    """Delete a user."""
    return f"deleted {user}"


# ---------------------------------------------------------------- enabled


async def test_disabled_tool_is_not_offered() -> None:
    model = FakeModel(responses=["ok"])
    await Runner().run_async(make_agent(model, [search, hidden]), "hi")
    assert offered(model) == ["search"]


async def test_enabled_predicate_sees_deps() -> None:
    model = FakeModel(responses=["ok", "ok"])
    agent = make_agent(model, [search, delete_user])
    await Runner().run_async(agent, "hi", deps={"admin": False})
    await Runner().run_async(agent, "hi", deps={"admin": True})
    assert offered(model, 0) == ["search"]
    assert offered(model, 1) == ["search", "delete_user"]


async def test_async_predicate_is_awaited() -> None:
    async def later(ctx: RunContext[Any]) -> bool:
        return ctx.turn >= 2

    @tool(enabled=later)
    def finish(x: str) -> str:
        """Finish."""
        return "finished"

    model = FakeModel(responses=[[ToolCall(id="c1", name="search", arguments={"q": "a"})], "done"])
    await Runner().run_async(make_agent(model, [search, finish]), "hi")
    assert offered(model, 0) == ["search"]
    assert offered(model, 1) == ["search", "finish"]  # re-checked before every model call


async def test_calling_a_disabled_tool_is_an_unknown_tool() -> None:
    model = FakeModel(responses=[[ToolCall(id="c1", name="delete_user", arguments={"user": "u"})]])
    agent = make_agent(model, [search, delete_user])
    with pytest.raises(UnknownToolError) as info:
        await Runner().run_async(agent, "hi", deps={"admin": False})
    assert "delete_user" not in info.value.permitted_tools


async def test_no_tools_enabled_sends_no_tool_list() -> None:
    model = FakeModel(responses=["ok"])
    await Runner().run_async(make_agent(model, [hidden]), "hi")
    assert model.calls[0]["tools"] is None


async def test_failing_predicate_names_the_tool() -> None:
    def broken(ctx: RunContext[Any]) -> bool:
        raise RuntimeError("boom")

    @tool(enabled=broken)
    def fragile(x: str) -> str:
        """Fragile."""
        return x

    agent = make_agent(FakeModel(responses=["ok"]), [fragile])
    with pytest.raises(SDKValidationError, match="enabled check of tool 'fragile' failed"):
        await Runner().run_async(agent, "hi")


@pytest.mark.parametrize("bad", ["yes", 1, None])
def test_enabled_must_be_bool_or_callable(bad: Any) -> None:
    with pytest.raises(SDKValidationError, match="enabled must be"):

        @tool(enabled=bad)
        def t(x: str) -> str:
            """T."""
            return x


# ---------------------------------------------------------------- prepare_tools


async def test_prepare_tools_filters_per_step() -> None:
    def only_search_first(ctx: RunContext[Any], tools: list[Tool]) -> list[Tool]:
        return [t for t in tools if t.name == "search"] if ctx.turn <= 1 else tools

    @tool
    def summarize(x: str) -> str:
        """Summarize."""
        return "summary"

    model = FakeModel(responses=[[ToolCall(id="c1", name="search", arguments={"q": "a"})], "done"])
    agent = make_agent(model, [search, summarize], prepare_tools=only_search_first)
    result = await Runner().run_async(agent, "hi")
    assert result.output == "done"
    assert offered(model, 0) == ["search"]
    assert offered(model, 1) == ["search", "summarize"]


async def test_prepare_tools_only_sees_enabled_tools_and_can_be_async() -> None:
    seen: list[list[str]] = []

    async def spy(ctx: RunContext[Any], tools: list[Tool]) -> list[Tool]:
        seen.append([t.name for t in tools])
        return tools

    model = FakeModel(responses=["ok"])
    await Runner().run_async(make_agent(model, [search, hidden], prepare_tools=spy), "hi")
    assert seen == [["search"]]


async def test_prepare_tools_may_adjust_a_tool_description() -> None:
    def reword(ctx: RunContext[Any], tools: list[Tool]) -> list[Tool]:
        out = []
        for t in tools:
            copy = Tool(name=t.name, fn=t.fn, description=f"{t.description} (user {ctx.deps})")
            out.append(copy)
        return out

    model = FakeModel(responses=[[ToolCall(id="c1", name="search", arguments={"q": "a"})], "done"])
    agent = make_agent(model, [search], prepare_tools=reword)
    result = await Runner().run_async(agent, "hi", deps="u1")
    assert result.output == "done"
    assert model.calls[0]["tools"][0]["function"]["description"] == "Search. (user u1)"


async def test_tool_removed_by_prepare_tools_cannot_be_called() -> None:
    model = FakeModel(responses=[[ToolCall(id="c1", name="search", arguments={"q": "a"})]])
    agent = make_agent(model, [search], prepare_tools=lambda ctx, tools: [])
    with pytest.raises(UnknownToolError):
        await Runner().run_async(agent, "hi")


@pytest.mark.parametrize(
    "prepare, match",
    [
        (lambda ctx, tools: None, "must return a list of Tool"),
        (lambda ctx, tools: ["search"], "must return a list of Tool"),
        (lambda ctx, tools: [hidden], "not enabled on agent 'A': hidden"),
        (lambda ctx, tools: [search, search], "same tool name twice"),
    ],
)
async def test_invalid_prepare_tools_output_is_rejected(prepare: Any, match: str) -> None:
    agent = make_agent(FakeModel(responses=["ok"]), [search, hidden], prepare_tools=prepare)
    with pytest.raises(SDKValidationError, match=match):
        await Runner().run_async(agent, "hi")


async def test_failing_prepare_tools_is_reported_as_such() -> None:
    def broken(ctx: RunContext[Any], tools: list[Tool]) -> list[Tool]:
        raise RuntimeError("bug")

    agent = make_agent(FakeModel(responses=["ok"]), [search], prepare_tools=broken)
    with pytest.raises(SDKValidationError, match="prepare_tools failed: bug"):
        await Runner().run_async(agent, "hi")


def test_prepare_tools_must_be_callable() -> None:
    with pytest.raises(SDKValidationError, match="prepare_tools must be a function"):
        make_agent(FakeModel(responses=["ok"]), [search], prepare_tools="nope")  # type: ignore[arg-type]


# ---------------------------------------------------------------- approvals


async def test_approved_call_to_a_conditionally_enabled_tool_runs_on_resume() -> None:
    @tool(requires_approval=True, enabled=admin_only)
    def wipe(user: str) -> str:
        """Wipe."""
        return f"wiped {user}"

    model = FakeModel(responses=[[ToolCall(id="c1", name="wipe", arguments={"user": "u"})], "done"])
    agent = make_agent(model, [wipe])
    runner = Runner()
    paused = await runner.run_async(agent, "hi", deps={"admin": True})
    assert paused.status == "paused"
    assert paused.interruption is not None
    done = await runner.resume_async(
        agent,
        paused.interruption,
        approve=paused.interruption.pending_digests(),
        deps={"admin": True},
    )
    assert done.output == "done"
    assert any(m.content == "wiped u" for m in done.messages if m.role == "tool")


async def test_unknown_tool_error_does_not_list_hidden_tools_when_none_offered() -> None:
    model = FakeModel(responses=[[ToolCall(id="c1", name="guess", arguments={})]])
    with pytest.raises(UnknownToolError) as info:
        await Runner().run_async(make_agent(model, [hidden]), "hi")
    assert info.value.permitted_tools == []
    assert "hidden" not in str(info.value)


# ---------------------------------------------------------------- audit 4 (security)


async def test_hidden_tool_beside_an_approval_tool_fails_before_pausing() -> None:
    ran: list[str] = []

    @tool(enabled=False)
    def hidden_admin(x: str) -> str:
        """Never offered."""
        ran.append(x)
        return "ran"

    @tool(requires_approval=True)
    def send(x: str) -> str:
        """Needs approval."""
        ran.append(f"send {x}")
        return "sent"

    model = FakeModel(
        responses=[
            [
                ToolCall(id="c1", name="send", arguments={"x": "a"}),
                ToolCall(id="c2", name="hidden_admin", arguments={"x": "pwn"}),
            ]
        ]
    )
    with pytest.raises(UnknownToolError, match="hidden_admin"):
        await Runner().run_async(make_agent(model, [send, hidden_admin]), "hi")
    assert ran == []  # nothing in the batch ran, and no pause could carry the hidden call


async def test_hidden_tool_in_a_batch_fails_before_any_call_runs() -> None:
    ran: list[str] = []

    @tool
    def visible(x: str) -> str:
        """Visible."""
        ran.append(x)
        return "ok"

    model = FakeModel(
        responses=[
            [
                ToolCall(id="c1", name="visible", arguments={"x": "first"}),
                ToolCall(id="c2", name="hidden", arguments={"q": "x"}),
            ]
        ]
    )
    with pytest.raises(UnknownToolError):
        await Runner().run_async(make_agent(model, [visible, hidden]), "hi")
    assert ran == []


async def test_hidden_tool_never_reaches_a_durable_checkpoint() -> None:
    from nexura_agents import MemoryCheckpointStore

    @tool(requires_approval=True)
    def send(x: str) -> str:
        """Needs approval."""
        return "sent"

    store = MemoryCheckpointStore()
    model = FakeModel(
        responses=[
            [
                ToolCall(id="c1", name="send", arguments={"x": "a"}),
                ToolCall(id="c2", name="hidden", arguments={"q": "x"}),
            ]
        ]
    )
    with pytest.raises(UnknownToolError):
        await Runner(checkpoint_store=store).run_async(make_agent(model, [send, hidden]), "hi")
    assert await store.list_incomplete() == []  # failed runs leave no checkpoint to recover


# ---------------------------------------------------------------- audit 5 (MCP serving)


async def _mcp(server: Any, method: str, params: dict[str, Any] | None = None) -> Any:
    request: dict[str, Any] = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        request["params"] = params
    return await server.session.handle_request(request)


async def test_mcp_server_does_not_list_or_run_a_disabled_tool() -> None:
    from nexura_agents.mcp.serve import AgentMCPServer

    ran: list[str] = []

    @tool(enabled=False)
    def secret(x: str) -> str:
        """Disabled."""
        ran.append(x)
        return "done"

    server = AgentMCPServer("s", tools=[secret, lookup_tool()])
    listing = await _mcp(server, "tools/list")
    assert [t["name"] for t in listing["result"]["tools"]] == ["lookup"]
    reply = await _mcp(server, "tools/call", {"name": "secret", "arguments": {"x": "a"}})
    assert "error" in reply and "not found" in reply["error"]["message"]
    assert ran == []


async def test_mcp_server_checks_an_enabled_rule_per_call() -> None:
    from nexura_agents.mcp.serve import AgentMCPServer

    allowed = {"on": False}

    @tool(enabled=lambda ctx: allowed["on"])
    def gated(x: str) -> str:
        """Gated."""
        return f"ran {x}"

    server = AgentMCPServer("s", tools=[gated])
    refused = await _mcp(server, "tools/call", {"name": "gated", "arguments": {"x": "a"}})
    assert refused["result"]["isError"] is True
    assert "not available" in refused["result"]["content"][0]["text"]
    allowed["on"] = True
    ok = await _mcp(server, "tools/call", {"name": "gated", "arguments": {"x": "a"}})
    assert ok["result"]["content"][0]["text"] == "ran a"


def lookup_tool() -> Any:
    @tool
    def lookup(q: str) -> str:
        """Lookup."""
        return q

    return lookup
