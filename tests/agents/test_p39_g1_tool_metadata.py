"""P39 G1: tool metadata for policies (``ctx.tool``, ``Tool.origin``, ``Tool.tags``) and the
``module_tool`` effect-class fix.

MCP tools run against an in-memory MCP server, OpenAPI tools against an ``httpx``
mock transport; nothing leaves the process.
"""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Any

import httpx
import pytest
from nexura_agents import (
    Agent,
    AgentHooks,
    FakeModel,
    GuardrailResult,
    MCPServer,
    MemoryCheckpointStore,
    RunContext,
    RunInterruption,
    Runner,
    RunResult,
    SchemaTool,
    Tool,
    ToolCall,
    ToolInfo,
    module_tool,
    openapi_tools,
    tool,
    web_fetch_tool,
)
from nexura_agents.contracts.base import BaseModule
from nexura_agents.contracts.context import ExecutionContext
from nexura_agents.contracts.execution import EffectClass
from nexura_agents.contracts.manifest import ModuleManifest
from nexura_agents.contracts.result import ModuleResult
from nexura_agents.errors import SDKValidationError
from nexura_agents.mcp import McpInMemoryTransport, McpServerSession
from nexura_agents.mcp.types import McpToolDefinition


def call(name: str, call_id: str = "c1", **arguments: Any) -> list[ToolCall]:
    return [ToolCall(id=call_id, name=name, arguments=arguments)]


def held(result: RunResult[Any]) -> RunInterruption:
    """The interruption of a paused result."""
    assert result.interruption is not None
    return result.interruption


@tool(tags=["payments", "team:billing"])
def refund(amount: int) -> str:
    """Refund."""
    return "ok"


# ---------------------------------------------------------------- local tools


def test_local_tool_origin_tags_and_info() -> None:
    assert refund.origin == f"python:{__name__}.refund"
    assert refund.tags == frozenset({"payments", "team:billing"})
    info = refund.info()
    assert info == ToolInfo(
        name="refund",
        origin=f"python:{__name__}.refund",
        effect="non_idempotent",
        requires_approval=False,
        tags=frozenset({"payments", "team:billing"}),
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        info.name = "other"  # type: ignore[misc]


def test_info_reflects_effect_and_approval_rules() -> None:
    @tool(effect="pure", requires_approval=lambda ctx, args: False)
    def read() -> str:
        """Read."""
        return "r"

    info = read.info()
    assert info.effect == "pure"
    assert info.requires_approval is True  # declares a rule, even if it says no for a call
    assert Tool("t", lambda: "x", origin="custom:thing").origin == "custom:thing"
    assert SchemaTool("s", {}, lambda ctx, a: "x").origin is None


@pytest.mark.parametrize("tags", ["payments", ["ok", ""], [1]])
def test_bad_tags_are_refused(tags: Any) -> None:
    with pytest.raises(SDKValidationError):
        Tool("t", lambda: "x", tags=tags)


def test_bad_origin_is_refused() -> None:
    with pytest.raises(SDKValidationError):
        Tool("t", lambda: "x", origin="")


async def test_ctx_tool_reaches_the_tool_its_guardrails_and_hooks() -> None:
    seen: dict[str, Any] = {}

    def guard(ctx: RunContext[Any], args: Any) -> GuardrailResult:
        seen["guardrail"] = ctx.tool
        return GuardrailResult.passed()

    @tool(input_guardrails=[guard], output_guardrails=[guard], tags=["x"])
    def probe(ctx: RunContext[Any]) -> str:
        """Probe."""
        seen["tool"] = ctx.tool
        return "ok"

    class Hooks(AgentHooks):
        async def on_run_start(self, ctx: RunContext[Any]) -> None:
            seen["run"] = ctx.tool

        async def before_tool(self, ctx: RunContext[Any], call: Any) -> None:
            seen["before_tool"] = ctx.tool

        async def after_tool(self, ctx: RunContext[Any], call: Any, result: str) -> None:
            seen["after_tool"] = ctx.tool

    agent = Agent(
        name="A", instructions="i", model=FakeModel(responses=[call("probe"), "ok"]), tools=[probe]
    )
    await Runner(hooks=Hooks()).run_async(agent, "go")
    assert seen.pop("run") is None  # outside a tool call
    expected = probe.info()
    assert all(value == expected for value in seen.values())
    assert set(seen) == {"guardrail", "tool", "before_tool", "after_tool"}


def test_builtin_and_wrapper_tools_have_origins() -> None:
    inner = Agent(name="Research", instructions="r", model=FakeModel(responses=["x"]))
    asker = Agent(name="Q", instructions="q", model=FakeModel(responses=["x"]), ask_user=True)
    assert inner.as_tool().origin == "agent:Research"
    ask = next(t for t in asker.tools if t.name == "ask_user")
    assert ask.origin == "builtin:ask_user"
    assert web_fetch_tool().origin == "builtin:web"
    from nexura_agents import SequentialAgent

    team = SequentialAgent("Crew", [inner])
    assert team.as_tool().origin == "team:Crew"


# ---------------------------------------------------------------- MCP tools


async def mcp_server(**options: Any) -> tuple[MCPServer, asyncio.Task[None]]:
    client_side, server_side = McpInMemoryTransport.create_pair()
    server = McpServerSession(server_side)

    async def echo(args: dict[str, Any]) -> str:
        return "echo"

    schema = {"type": "object", "properties": {"q": {"type": "string"}}}
    for name in ("search", "delete_row", "hinted"):
        server.register_tool(name, f"{name} tool", schema, echo)
    # The server declares hints. They are untrusted: metadata only.
    server._tools["hinted"] = McpToolDefinition(
        name="hinted",
        description="claims to be read-only",
        inputSchema=schema,
        annotations={"readOnlyHint": True, "destructiveHint": False, "title": "Hinted"},
    )
    task = asyncio.create_task(server.serve_loop())
    return MCPServer(client_side, name="files", **options), task


async def test_mcp_tool_metadata_origin_toolset_and_untrusted_hints() -> None:
    mcp, task = await mcp_server(read_only=["search"])
    try:
        async with mcp:
            tools = {t.name: t for t in await mcp.tools()}
    finally:
        task.cancel()
    search, delete, hinted = tools["search"], tools["delete_row"], tools["hinted"]
    assert search.origin == "mcp:files:search"
    assert delete.origin == "mcp:files:delete_row"
    assert "toolset:files" in search.tags and "module:mcp.files.search" in search.tags
    assert search.info().toolsets == frozenset({"files"})
    # Operator-marked read-only: pure. Everything else stays non_idempotent.
    assert search.effect_class() is EffectClass.PURE
    assert delete.effect_class() is EffectClass.NON_IDEMPOTENT
    # The server's readOnlyHint is recorded as a tag only; it changes nothing else.
    assert {"mcp.hint:readOnlyHint=true", "mcp.hint:destructiveHint=false"} <= hinted.tags
    assert hinted.effect_class() is EffectClass.NON_IDEMPOTENT
    assert not any(t.startswith("mcp.hint:title") for t in hinted.tags)


async def test_mcp_hints_never_skip_writes_approval() -> None:
    mcp, task = await mcp_server(requires_approval="writes")
    try:
        async with mcp:
            tools = {t.name: t for t in await mcp.tools()}
            ctx = RunContext(deps=None, run_id="r", agent_name="a")
            assert tools["hinted"].needs_approval(ctx, {"q": "x"}) is True
    finally:
        task.cancel()


async def test_mcp_resource_tools_have_origins() -> None:
    client_side, server_side = McpInMemoryTransport.create_pair()
    server = McpServerSession(server_side)
    server.register_resource("file:///a.txt", "a", "A")
    task = asyncio.create_task(server.serve_loop())
    try:
        async with MCPServer(client_side, name="docs") as mcp:
            list_tool, read_tool = await mcp.resource_tools()
    finally:
        task.cancel()
    assert list_tool.origin == "mcp:docs:resources/list"
    assert read_tool.origin == "mcp:docs:resources/read"
    assert "toolset:docs" in read_tool.tags


# ---------------------------------------------------------------- OpenAPI tools

SPEC: dict[str, Any] = {
    "openapi": "3.0.3",
    "info": {"title": "Shop", "version": "1"},
    "servers": [{"url": "https://shop.example.com/v1"}],
    "paths": {
        "/orders/{order_id}": {
            "get": {
                "operationId": "getOrder",
                "parameters": [
                    {
                        "name": "order_id",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                    }
                ],
                "responses": {"200": {"description": "ok"}},
            },
            "put": {
                "operationId": "replaceOrder",
                "responses": {"200": {"description": "ok"}},
            },
        },
        "/refunds": {
            "post": {"operationId": "createRefund", "responses": {"201": {"description": "c"}}}
        },
    },
}


def mock() -> httpx.MockTransport:
    return httpx.MockTransport(lambda request: httpx.Response(200, json={}))


def test_openapi_tool_metadata_records_the_method_as_a_tag_only() -> None:
    """The spec's HTTP method and x-idempotent are the API's own claims (founder answer 6,
    A6 decision 1): tags only, never an effect class."""
    tools = {t.name: t for t in openapi_tools(SPEC, transport=mock())}
    get, put, post = tools["getOrder"], tools["replaceOrder"], tools["createRefund"]
    assert get.origin == "openapi:GET:/orders/{order_id}"
    assert put.origin == "openapi:PUT:/orders/{order_id}"
    assert post.origin == "openapi:POST:/refunds"
    assert {"toolset:openapi", "openapi.method:GET", "openapi.hint:idempotent=true"} <= get.tags
    assert {"openapi.method:PUT", "openapi.hint:idempotent=true"} <= put.tags
    assert "openapi.method:POST" in post.tags
    assert not any(t.startswith("openapi.hint:") for t in post.tags)
    # Undeclared: non_idempotent, whatever the method says.
    assert {t.effect_class() for t in tools.values()} == {EffectClass.NON_IDEMPOTENT}
    named = openapi_tools(SPEC, transport=mock(), toolset="shop", prefix="s")
    assert all("toolset:shop" in t.tags for t in named)
    prefixed = openapi_tools(SPEC, transport=mock(), prefix="crm")
    assert all("toolset:crm" in t.tags for t in prefixed)


def test_x_idempotent_is_a_hint_only() -> None:
    spec = {
        **SPEC,
        "paths": {
            "/charges": {
                "post": {
                    "operationId": "charge",
                    "x-idempotent": True,
                    "responses": {"201": {"description": "c"}},
                }
            }
        },
    }
    (charge,) = openapi_tools(spec, transport=mock())
    assert "openapi.hint:idempotent=true" in charge.tags
    assert charge.effect_class() is EffectClass.NON_IDEMPOTENT


def test_the_operator_declares_openapi_effect_classes() -> None:
    tools = {
        t.name: t
        for t in openapi_tools(
            SPEC, transport=mock(), read_only=["getOrder"], idempotent=["replaceOrder"]
        )
    }
    assert tools["getOrder"].effect_class() is EffectClass.PURE
    assert tools["replaceOrder"].effect_class() is EffectClass.IDEMPOTENT
    assert tools["createRefund"].effect_class() is EffectClass.NON_IDEMPOTENT
    # Declarations change recovery only, never approval.
    gated = {
        t.name: t
        for t in openapi_tools(
            SPEC, transport=mock(), idempotent=["replaceOrder"], requires_approval="writes"
        )
    }
    ctx = RunContext(deps=None, run_id="r", agent_name="a")
    assert gated["replaceOrder"].needs_approval(ctx, {"order_id": "A1"}) is True


@pytest.mark.parametrize(
    "kwargs",
    [
        {"read_only": ["nope"]},
        {"idempotent": ["getOrder", "missing"]},
        {"read_only": ["getOrder"], "idempotent": ["getOrder"]},
        {"read_only": "getOrder"},
    ],
)
def test_bad_openapi_declarations_are_refused(kwargs: dict[str, Any]) -> None:
    with pytest.raises(SDKValidationError):
        openapi_tools(SPEC, transport=mock(), **kwargs)


def test_openapi_agent_digest_is_unchanged_from_before_g1() -> None:
    """Pre-G1 checkpoints recover without the definition-changed warning. The expected
    digest was computed with the pre-G1 source for the same agent, using a separate
    cross-version recovery script."""
    spec = {
        "openapi": "3.0.3",
        "info": {"title": "Shop", "version": "1"},
        "servers": [{"url": "https://shop.example.com/v1"}],
        "paths": {
            "/orders/{order_id}": {
                "get": {
                    "operationId": "getOrder",
                    "parameters": [
                        {
                            "name": "order_id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                        }
                    ],
                    "responses": {"200": {"description": "ok"}},
                },
                "delete": {
                    "operationId": "cancelOrder",
                    "responses": {"200": {"description": "ok"}},
                },
            },
            "/refunds": {
                "post": {"operationId": "createRefund", "responses": {"201": {"description": "c"}}}
            },
        },
    }
    agent = Agent(
        name="Shop",
        instructions="i",
        model=FakeModel(responses=["x"]),
        tools=openapi_tools(spec, transport=mock()),
    )
    assert agent.definition_digest() == (
        "sha256:a5a866a95bd606487baf3348b85eec3efc72b1620a0c7525916e71413a63317e"
    )


@pytest.mark.parametrize(
    ("declared", "expected", "requests"), [((), "paused", 1), (("getOrder",), "completed", 2)]
)
async def test_an_undeclared_openapi_read_still_pauses_after_a_crash(
    declared: tuple[str, ...], expected: str, requests: int
) -> None:
    """A GET that crashed mid-call is in doubt (non_idempotent): recovery pauses for a
    person instead of repeating it; declared read_only, it is repeated."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise Crash
        return httpx.Response(200, json={"id": "A1"})

    tools = openapi_tools(SPEC, transport=httpx.MockTransport(handler), read_only=declared)
    store = MemoryCheckpointStore()
    agent = Agent(
        name="A",
        instructions="i",
        model=FakeModel(responses=[call("getOrder", order_id="A1"), "x"]),
        tools=tools,
    )
    with pytest.raises(Crash):
        await Runner(checkpoint_store=store).run_async(agent, "go")
    again = Agent(name="A", instructions="i", model=FakeModel(responses=["done"]), tools=tools)
    result = await Runner(checkpoint_store=store).recover_async(again, next(iter(store._data)))
    assert result.status == expected and calls["n"] == requests


# ---------------------------------------------------------------- module_tool fix


class Module(BaseModule):
    def __init__(self, module_id: str = "acme.lookup", **manifest: Any) -> None:
        self.calls = 0
        self.crash_first = False
        self._manifest = ModuleManifest(
            module_id=module_id,
            version="1.0.0",
            kind="integration",
            title="Lookup",
            description="Lookup.",
            input_schema={"type": "object", "properties": {}},
            **manifest,
        )

    @property
    def manifest(self) -> ModuleManifest:
        return self._manifest

    async def invoke(
        self, context: ExecutionContext, inputs: dict[str, Any], config: dict[str, Any]
    ) -> ModuleResult:
        self.calls += 1
        if self.crash_first and self.calls == 1:
            raise Crash
        return ModuleResult.success({"answer": "42"})


class Crash(BaseException):
    """Simulated process death."""


@pytest.mark.parametrize(
    ("manifest", "effect"),
    [
        ({}, EffectClass.NON_IDEMPOTENT),  # unset policy (the manifest default): as before
        ({"effect_policy": "pure"}, EffectClass.PURE),
        ({"effect_policy": "read"}, EffectClass.PURE),
        ({"effect_policy": "idempotent_write"}, EffectClass.IDEMPOTENT),
        ({"effect_policy": "non_idempotent_write"}, EffectClass.NON_IDEMPOTENT),
        # A read policy that contradicts declared writes is not trusted.
        ({"effect_policy": "read", "always_write": True}, EffectClass.NON_IDEMPOTENT),
        ({"effect_policy": "pure", "write_operations": ["delete"]}, EffectClass.NON_IDEMPOTENT),
    ],
)
def test_module_tool_keeps_the_declared_effect_policy(
    manifest: dict[str, Any], effect: EffectClass
) -> None:
    built = module_tool(Module(**manifest))
    assert built.effect_class() is effect
    assert built.origin == "module:acme.lookup"
    assert built.tags == frozenset({"module:acme.lookup"})


def test_module_tool_effect_override_and_extra_tags() -> None:
    built = module_tool(Module(effect_policy="read"), effect="non_idempotent", tags=["crm"])
    assert built.effect_class() is EffectClass.NON_IDEMPOTENT
    assert built.tags == frozenset({"module:acme.lookup", "crm"})


async def test_a_read_module_tool_is_repeated_after_a_crash_instead_of_pausing() -> None:
    """What the effect fix means for durable runs: a pure tool is re-run on recovery."""
    module = Module(effect_policy="read")
    module.crash_first = True
    store = MemoryCheckpointStore()
    lookup = module_tool(module, name="lookup")
    agent = Agent(
        name="A", instructions="i", model=FakeModel(responses=[call("lookup"), "x"]), tools=[lookup]
    )
    with pytest.raises(Crash):
        await Runner(checkpoint_store=store).run_async(agent, "go")
    run_id = next(iter(store._data))
    again = Agent(name="A", instructions="i", model=FakeModel(responses=["done"]), tools=[lookup])
    result = await Runner(checkpoint_store=store).recover_async(again, run_id)
    assert result.status == "completed" and module.calls == 2


async def test_an_unmarked_module_tool_still_pauses_after_a_crash() -> None:
    module = Module()  # no effect policy declared: non_idempotent, as before G1
    module.crash_first = True
    store = MemoryCheckpointStore()
    lookup = module_tool(module, name="lookup")
    agent = Agent(
        name="A", instructions="i", model=FakeModel(responses=[call("lookup"), "x"]), tools=[lookup]
    )
    with pytest.raises(Crash):
        await Runner(checkpoint_store=store).run_async(agent, "go")
    run_id = next(iter(store._data))
    again = Agent(name="A", instructions="i", model=FakeModel(responses=["done"]), tools=[lookup])
    result = await Runner(checkpoint_store=store).recover_async(again, run_id)
    assert result.status == "paused" and module.calls == 1
    assert held(result).pause_reason == "effect_unknown"


def test_module_tool_on_an_openapi_operation_ignores_the_spec_method() -> None:
    from nexura_agents.openapi.module import import_openapi_operations

    modules = {m.op_spec.operation_id: m for m in import_openapi_operations(SPEC)}
    assert modules["getOrder"].manifest.effect_policy == "read"  # the spec's claim
    built = module_tool(modules["getOrder"])
    assert built.effect_class() is EffectClass.NON_IDEMPOTENT
    assert built.origin == "openapi:GET:/orders/{order_id}"
    assert module_tool(modules["getOrder"], effect="pure").effect_class() is EffectClass.PURE
