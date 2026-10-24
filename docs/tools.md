# Tools

A tool is a Python function the model can call. You write the function, decorate it with `@tool`, and pass it to an `Agent`. Nexura builds the JSON schema from the type hints, validates the model's arguments before your code runs, and sends the return value back to the model as JSON. This page covers function tools, typed dependencies, retries, parallel calls, approval, and the two lower-level tool types (`SchemaTool` and `module_tool`).

## A first tool

```python
from nexura_agents import Agent, FakeModel, ToolCall, tool


@tool
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b


model = FakeModel(
    responses=[
        [ToolCall(id="1", name="add", arguments={"a": 2, "b": 3})],
        "2 + 3 = 5",
    ]
)
agent = Agent(name="Calc", instructions="Use tools.", model=model, tools=[add])
result = agent.run("What is 2 + 3?")

assert result.output == "2 + 3 = 5"
assert result.tool_calls_count == 1
```

`FakeModel` replays the responses you give it, so the example needs no API key. The first response is a list of `ToolCall`s; the second is the final text.

`@tool` returns a `Tool` object. It can be sync or async. Sync functions run in a worker thread, so a blocking call does not stall the event loop.

## How the schema is built

The tool name is the function name (override with `@tool(name="...")`). The description is the docstring (override with `description=`; if there is neither, it is `"Execute <name>"`). Parameters come from the signature: type hints become the JSON types, and parameters with defaults are optional. Pydantic models work as parameter types.

You can look at exactly what the model is shown with `to_schema()`:

```python
from nexura_agents import tool


@tool(name="find_user", description="Look up a user by email.")
def lookup(email: str, limit: int = 5) -> list[str]:
    return [email][:limit]


schema = lookup.to_schema()
fn = schema["function"]
assert fn["name"] == "find_user"
assert fn["description"] == "Look up a user by email."
assert fn["parameters"]["required"] == ["email"]
assert fn["parameters"]["properties"]["limit"]["default"] == 5
```

If you need a schema that is not derived from the signature, pass `args_schema=` a Pydantic model.

Arguments are validated with Pydantic before your function runs. If the model sends bad arguments, a `ToolValidationError` is raised and the run fails, unless you set `max_retries` (see below).

Return values are serialized to JSON: strings pass through unchanged, Pydantic models are dumped with `model_dump`, and everything else goes through `json.dumps`. A result that cannot be serialized raises `ToolResultSerializationError`.

## Dependencies with RunContext

Tools often need a database handle, an HTTP client or the current user. Pass them once with `deps=` and declare a parameter annotated `RunContext[YourType]`. That parameter is injected by the runner and is not part of the schema the model sees.

```python
from dataclasses import dataclass

from nexura_agents import Agent, FakeModel, RunContext, ToolCall, tool


@dataclass
class Shop:
    prices: dict[str, float]


@tool
def price(ctx: RunContext[Shop], sku: str) -> float:
    """Price of one item."""
    return ctx.deps.prices[sku]


# The context parameter is hidden from the model.
assert list(price.to_schema()["function"]["parameters"]["properties"]) == ["sku"]

model = FakeModel(
    responses=[[ToolCall(id="1", name="price", arguments={"sku": "tea"})], "It costs 4.5."]
)
agent = Agent(name="Shop", instructions="Answer price questions.", model=model, tools=[price])
result = agent.run("How much is tea?", deps=Shop(prices={"tea": 4.5}))
assert result.output == "It costs 4.5."
```

`RunContext` also carries `run_id`, `agent_name`, `turn`, `usage`, `messages`, `tool_call_id`, `tool_name` and `retry` (how many times this tool has already been retried in the run). A tool that has a `RunContext` parameter is meant to run through a `Runner`, which supplies the context.

## Telling the model to try again

Raise `ModelRetry` to send a message back to the model instead of failing the run. By default a tool may ask for one retry in a run; set `max_retries` on the tool to change that. When the limit is exceeded the run fails with `ToolExecutionError`.

```python
from nexura_agents import Agent, FakeModel, ModelRetry, ToolCall, tool

calls: list[str] = []


@tool(max_retries=2)
def get_order(order_id: str) -> str:
    """Fetch an order."""
    calls.append(order_id)
    if not order_id.startswith("A"):
        raise ModelRetry("Order IDs start with A, for example A1.")
    return f"order {order_id}: shipped"


model = FakeModel(
    responses=[
        [ToolCall(id="1", name="get_order", arguments={"order_id": "7"})],
        [ToolCall(id="2", name="get_order", arguments={"order_id": "A7"})],
        "Order A7 has shipped.",
    ]
)
agent = Agent(name="Orders", instructions="Look up orders.", model=model, tools=[get_order])
result = agent.run("Where is order 7?")

assert calls == ["7", "A7"]
assert result.output == "Order A7 has shipped."
# The model saw the retry message as the first tool result.
tool_messages = [m for m in result.messages if m.role == "tool"]
assert "Order IDs start with A" in tool_messages[0].content
```

`max_retries` also controls invalid arguments. If it is `None` (the default), arguments that fail validation raise `ToolValidationError` and end the run. If you set it, the validation error goes back to the model as a message and the model gets that many attempts.

Other exceptions raised inside a tool become `ToolExecutionError`. Nexura does not retry side-effecting tools on its own: a tool only runs again when it raised `ModelRetry` itself.

## Parallel tool calls

When the model returns several tool calls in one turn, the agent runs them one after another by default. Set `parallel_tool_calls=True` to run them concurrently. Results are still appended in the order the model asked. If one call fails, the calls that finished are still recorded (a `TOOL_COMPLETED` event, a tool message and a checkpoint) before the run fails, so recovery never repeats a write that already ran.

```python
import asyncio

from nexura_agents import Agent, FakeModel, ToolCall, tool

running = 0
peak = 0


@tool
async def fetch(name: str) -> str:
    """Fetch something slowly."""
    global running, peak
    running += 1
    peak = max(peak, running)
    await asyncio.sleep(0.05)
    running -= 1
    return name.upper()


model = FakeModel(
    responses=[
        [
            ToolCall(id="1", name="fetch", arguments={"name": "a"}),
            ToolCall(id="2", name="fetch", arguments={"name": "b"}),
        ],
        "done",
    ]
)
agent = Agent(
    name="Fetcher",
    instructions="Fetch both.",
    model=model,
    tools=[fetch],
    parallel_tool_calls=True,
)
agent.run("Fetch a and b")
assert peak == 2
```

Make sure parallel tools do not depend on each other's side effects. `Agent(max_tool_calls=...)` (default 20) caps the total number of calls in a run.

## Approval before a tool runs

`requires_approval=True` pauses the run before the tool executes. A predicate `(ctx, arguments) -> bool` pauses only for some calls. The paused run is returned as `result.interruption`; resume it with `Runner.resume`. [Human approval](approvals.md) covers the full flow, including saving to JSON and approving by digest.

```python
from nexura_agents import Agent, FakeModel, Runner, ToolCall, tool

sent: list[str] = []


@tool(requires_approval=lambda ctx, args: args["amount"] > 100)
def refund(order_id: str, amount: float) -> str:
    """Refund an order."""
    sent.append(order_id)
    return "refunded"


model = FakeModel(
    responses=[
        [ToolCall(id="1", name="refund", arguments={"order_id": "A1", "amount": 250.0})],
        "Refunded.",
    ]
)
agent = Agent(name="Refunds", instructions="Refund.", model=model, tools=[refund])
runner = Runner()

result = runner.run(agent, "Refund A1")
assert result.status == "paused" and sent == []

result = runner.resume(agent, result.interruption, approve=True)
assert result.status == "completed" and sent == ["A1"]
```

A hook can add approval gates without changing the tools, for example for every tool of
one MCP server or every call over an amount: see
[Adding approval gates](guardrails-hooks-limits.md#adding-approval-gates). It can never
remove a tool's own `requires_approval`.

## Tool metadata for hooks, guardrails and policies

Every tool carries metadata that hooks, guardrails and policy rules can match on without
knowing each tool by name. `tool.info()` returns it as a frozen `ToolInfo`, and inside a
tool call the same value is `ctx.tool` (in the tool, its guardrails, `before_tool`,
`after_tool` and `tool_needs_approval`; `None` outside a tool call).

| Field | Value |
| --- | --- |
| `name` | The tool name the model sees. |
| `origin` | Where the tool comes from: `python:<module>.<qualname>` for `@tool`, `mcp:<server>:<tool>` for an MCP tool, `openapi:<METHOD>:<path>` for an OpenAPI operation, `module:<module_id>` for another `module_tool`, `agent:<name>` / `team:<name>` for `as_tool()` (and the runtime's `AgentDefinition.as_tool()`), `a2a:<host>` for a remote agent, `handoff` for a handoff's transfer tool (tagged `handoff:<target>`), `knowledge:<collection>` for the runtime's knowledge-search tools (tagged `knowledge:<collection>`; Studio's `knowledge_search` over several collections lists them comma-separated), `builtin:ask_user`, `builtin:web` and `builtin:memory` for the SDK's own tools. `None` when unknown (a `SchemaTool` you build yourself, unless you pass `origin=`). |
| `effect` | The effect class: `pure`, `idempotent`, `non_idempotent` or `reconcilable` (see `effect=` below). |
| `requires_approval` | `True` when the tool declares an approval flag or rule (a rule may still say no for a given call). |
| `tags` | Labels: your own (`@tool(tags=[...])`, `Tool(..., tags=...)`, `SchemaTool(..., tags=...)`), `toolset:<name>` for MCP and OpenAPI tools, `module:<module_id>` for module tools, `mcp.hint:<hint>=<true/false>` for an MCP server's declared hints, and `openapi.method:<METHOD>` plus `openapi.hint:idempotent=true` (when the spec's method or `x-idempotent` claims it) for OpenAPI operations. `info.toolsets` lists the toolset names. |

Metadata never changes how a tool runs. What a third-party server or spec says about its
own tools is untrusted: an MCP `mcp.hint:readOnlyHint=true` tag or an OpenAPI
`openapi.method:GET` / `openapi.hint:idempotent=true` tag is recorded for policies and
operators to see, but the tool stays `non_idempotent` unless you declare it yourself
(`MCPServer(read_only=...)`, `openapi_tools(read_only=..., idempotent=...)`), and an MCP
tool is still gated by `requires_approval="writes"` unless you list it in `read_only`.

```python
from typing import Any

from nexura_agents import Agent, AgentHooks, FakeModel, RunContext, Runner, ToolCall, tool


@tool(tags=["billing"], effect="idempotent")
def charge(ctx: RunContext[Any], amount: int) -> str:
    """Charge the card on file."""
    assert ctx.tool is not None and ctx.tool.name == "charge"
    return "charged"


seen = []


class Audit(AgentHooks):
    async def before_tool(self, ctx, call):
        info = ctx.tool
        seen.append((info.name, info.origin, info.effect, sorted(info.tags)))


model = FakeModel(responses=[[ToolCall(id="1", name="charge", arguments={"amount": 5})], "Ok."])
agent = Agent(name="Billing", instructions="Bill.", model=model, tools=[charge])
Runner(hooks=Audit()).run(agent, "Charge 5")

assert seen == [("charge", f"python:{__name__}.charge", "idempotent", ["billing"])]
assert charge.info().origin == f"python:{__name__}.charge"
```

## Choosing which tools the model sees

A tool can be offered only when it makes sense: to admins, after a login step, or only
for some users. `enabled` is checked before every model call. A disabled tool is not
sent to the model, and a call to it fails like an unknown tool (`UnknownToolError`).

```python
from typing import Any

from nexura_agents import Agent, FakeModel, RunContext, Runner, Tool, tool


def is_admin(ctx: RunContext[Any]) -> bool:
    return bool(ctx.deps and ctx.deps.get("admin"))


@tool(enabled=is_admin)
def delete_user(user: str) -> str:
    """Delete a user account."""
    return f"deleted {user}"


@tool
def lookup(user: str) -> str:
    """Look a user up."""
    return f"{user}: active"


def no_lookup_after_turn_3(ctx: RunContext[Any], tools: list[Tool]) -> list[Tool]:
    return [t for t in tools if t.name != "lookup"] if ctx.turn > 3 else tools


model = FakeModel(responses=["ok", "ok"])
agent = Agent(
    name="Support",
    instructions="Help with accounts.",
    model=model,
    tools=[delete_user, lookup],
    prepare_tools=no_lookup_after_turn_3,
)
Runner().run(agent, "hi", deps={"admin": False})
Runner().run(agent, "hi", deps={"admin": True})

names = [[t["function"]["name"] for t in call["tools"]] for call in model.calls]
assert names == [["lookup"], ["delete_user", "lookup"]]
```

- `@tool(enabled=...)` takes `True`, `False`, or a function of the `RunContext` (sync or
  async). `SchemaTool(..., enabled=...)` works the same way.
- `Agent(prepare_tools=fn)` runs after the `enabled` checks with `(ctx, tools)` and
  returns the tools to offer for this step, sync or async. It can drop tools, or return a
  copy of a tool with a different description. It cannot add tools the agent does not
  have, or tools that are disabled. Duplicate names and non-`Tool` items raise
  `SDKValidationError`.
- A failing `enabled` check or `prepare_tools` raises `SDKValidationError` naming the
  tool or hook; the original error is in `original_error`.
- `AgentMCPServer(tools=[...])` does not list a tool with `enabled=False`, and checks an
  `enabled` rule on every call (a refused call is an MCP error result).
- If the model calls a hidden tool anywhere in a batch, the whole batch fails with
  `UnknownToolError` before any call runs or the run pauses for approval. So a hidden
  call can never ride along with an approved call into `resume_async` or recovery.
- Hiding a tool is not a permission system on its own. Tools that change data should
  still check permissions inside the function. Approved calls resumed with
  `resume_async`, and calls finished by durable `recover`, run against the agent's tools
  without checking `enabled` again: they were offered when the model made them.
- Handoff tools (`transfer_to_*`) are not affected; the active agent's checks apply.

## Timeouts for one tool

`@tool(timeout=seconds)` (also `SchemaTool(..., timeout=...)`) limits a single call, so
one slow API does not use up the whole run's `timeout`:

```python
import asyncio

from nexura_agents import Agent, FakeModel, Runner, ToolCall, tool


@tool(timeout=0.1)
async def slow_search(q: str) -> str:
    """Search a slow index."""
    await asyncio.sleep(5)
    return "results"


model = FakeModel(
    responses=[[ToolCall(id="c1", name="slow_search", arguments={"q": "x"})], "Try later."]
)
agent = Agent(name="A", instructions="Help.", model=model, tools=[slow_search])
result = Runner().run(agent, "find x")
assert result.output == "Try later."
```

- `on_timeout="tell_model"` (default): the model gets `Tool call timed out: '<name>' did
  not finish within Ns.` as the result and can try something else. A timed-out call never
  ends a run under `tool_use_behavior`.
- `on_timeout="fail"`: the run stops with `ToolExecutionError` (the cause is a
  `TimeoutError`).
- When the run's remaining `timeout` is shorter than the tool's, the run timeout applies
  (`ModelTimeoutError`), as before.
- A tool served with `AgentMCPServer(tools=[...])` keeps its timeout; the MCP client gets
  an error result.
- An `async` tool is cancelled at the timeout. A sync tool runs in a worker thread, and
  Python cannot stop a thread: the run moves on, but the function keeps running in the
  background until it returns. Give blocking clients their own timeouts too.

## Automatic retries and caching

Some tools fail now and then for reasons that have nothing to do with the model: a reset
connection, a slow upstream. Others are called again and again with the same arguments.
For tools that are safe to repeat, `retry` and `cache` handle both without bothering the
model:

```python
from nexura_agents import Agent, FakeModel, Runner, ToolCall, ToolRetry, tool

calls = {"n": 0}


@tool(idempotent=True, retry=ToolRetry(max_attempts=3, initial_delay=0.01), cache=True)
def exchange_rate(currency: str) -> str:
    """Current exchange rate to INR."""
    calls["n"] += 1
    if calls["n"] == 1:
        raise ConnectionError("connection reset")  # retried automatically
    return f"1 {currency} = 83.1 INR"


lookup = [ToolCall(id="c1", name="exchange_rate", arguments={"currency": "USD"})]
model = FakeModel(responses=[lookup, lookup, "About 83 rupees."])
agent = Agent(name="FX", instructions="Answer FX questions.", model=model, tools=[exchange_rate])
assert Runner().run(agent, "USD to INR?").output == "About 83 rupees."
assert calls["n"] == 2  # one failure plus one success; the second lookup came from the cache
```

- `ToolRetry(max_attempts=3, initial_delay=0.5, backoff=2.0, max_delay=8.0,
  retry_on=(ConnectionError, TimeoutError))` tries the call up to `max_attempts` times
  in total. It waits `initial_delay`, then `initial_delay * backoff` and so on (capped at
  `max_delay`) between attempts.
  - Only exceptions of the `retry_on` types are retried. The tool's own `timeout` counts
    as a `TimeoutError`.
  - The model only sees the final outcome: a result, a timeout message, or the error.
  - A wait that would pass the run's `timeout` is skipped, and a `CancelToken` interrupts
    it.
  - A **sync** tool that hit its own `timeout` is not retried: Python cannot stop its
    thread, so a retry would run the same call twice at once. Use an `async` tool for
    retried timeouts.
- `cache=True` remembers successful results within one run and returns them for the same
  tool object and arguments (another agent's tool with the same name has its own
  entries), without running the tool or its `before_tool`/`after_tool` hooks.
  Output guardrails still run on cached results. Failed calls are not cached, and the
  cache starts empty in every run, including after `resume` and `recover`.
- Both require `idempotent=True`, because a retried call may already have had an effect
  and a cached call skips one. `SchemaTool`, and so MCP and OpenAPI tools, accept
  `idempotent`, `retry` and `cache` too.
- Not the same as `max_retries`, which lets the *model* try again after `ModelRetry` or
  invalid arguments.

## Ending the run with a tool result

By default the model is called again after every tool call, so it can phrase an
answer. When the tool's result already is the answer, `tool_use_behavior` skips that
extra model call:

```python
from nexura_agents import Agent, FakeModel, Runner, StopAtTools, ToolCall, tool


@tool
def order_status(order_id: str) -> str:
    """Current status of an order."""
    return f"Order {order_id} shipped on 3 October."


model = FakeModel(
    responses=[[ToolCall(id="c1", name="order_status", arguments={"order_id": "A7"})]]
)
agent = Agent(
    name="Orders",
    instructions="Answer order questions.",
    model=model,
    tools=[order_status],
    tool_use_behavior=StopAtTools(["order_status"]),
)
result = Runner().run(agent, "Where is A7?")

assert result.output == "Order A7 shipped on 3 October."
assert len(model.calls) == 1  # no second model call
```

- `"run_llm_again"` (default): call the model after tools run.
- `"stop_on_first_tool"`: the first tool call of the batch that succeeds ends the run.
- `StopAtTools(["name", ...])`: a successful call to one of these tools ends the run. The
  names must be tools of the agent.
- Every call in the batch still runs; the first matching successful call (in the
  model's order) gives the output. Calls that were rejected, retried with `ModelRetry`,
  had invalid arguments or have an unknown outcome after a crash never end the run, and
  handoffs are not tool results.
- The output is the tool's result text. With `output_type`, that text is validated as
  JSON (a tool returning a dict or model works); a mismatch raises
  `OutputValidationError`, because there is no model to retry.
- The run finishes like any other: output guardrails, session save, `on_run_end` and
  `RUN_COMPLETED` all happen. A guardrail rewrite changes `result.output` but not the
  stored tool message. This also applies when the call ran after an approval
  (`resume_async`) or in durable recovery. Recovery considers the whole batch, including
  calls that finished before the crash, and a run that crashed after completing
  replays the same output the caller got.

## SchemaTool: a tool from a raw JSON schema

Use `SchemaTool` when the arguments are described by a JSON schema rather than a Python signature. MCP and OpenAPI tools are built this way. The handler receives `(ctx, arguments)` after the arguments pass schema validation. `SchemaTool` defaults to `max_retries=1`, so a bad argument goes back to the model once.

```python
import asyncio

from nexura_agents import Agent, FakeModel, SchemaTool, ToolCall

echo = SchemaTool(
    "echo",
    {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
    lambda ctx, args: {"echo": args["text"]},
    description="Repeat the text.",
)

model = FakeModel(responses=[[ToolCall(id="1", name="echo", arguments={"text": "hi"})], "said hi"])
agent = Agent(name="Echo", instructions="Echo.", model=model, tools=[echo])
result = asyncio.run(agent.run_async("say hi"))
assert result.output == "said hi"
assert '"echo": "hi"' in [m for m in result.messages if m.role == "tool"][0].content
```

A `SchemaTool` can only run through a `Runner`; its `run()` method raises.

## module_tool: any Nexura module as a tool

`module_tool(module)` wraps a `BaseModule` (an HTTP call, a SQL query, an MCP tool, an OpenAPI operation, or your own) as a `SchemaTool`. The model sees the module's input schema. A module failure is returned to the model as `{"error": ..., "message": ...}` instead of ending the run, and results longer than `max_result_chars` (default 20,000) are truncated with a note. `requires_approval` accepts `True`, `"writes"`, a collection of tool names, or a predicate `(tool_name, arguments) -> bool`.

The tool keeps the module's identity. Its `origin` is `mcp:<server>:<tool>`,
`openapi:<METHOD>:<path>` or `module:<module_id>`, its tags include `module:<module_id>`
(plus any `tags=` you pass), and its effect class comes from the manifest's `effect_policy`:
`pure` and `read` give `pure`, `idempotent_write` gives `idempotent`, and
`non_idempotent_write` gives `non_idempotent`. A manifest that does not set `effect_policy`,
or declares a read policy together with write operations, stays `non_idempotent`. So a
read-only module tool is repeated after a crash instead of pausing the durable run (see
[Durable execution](durable-execution.md)). Two kinds of module carry a third party's claim
rather than yours, so they stay `non_idempotent` until you declare otherwise: MCP tools (an
MCP module is `read` only when you list it in `MCPServer(read_only=...)`) and OpenAPI
operations (their policy comes from the spec's HTTP method; declare with
`openapi_tools(read_only=..., idempotent=...)`). A module that wraps a tool
(`tool_to_module` in `nexura_runtime`) runs under the stricter of its manifest class and the
wrapped tool's own class, so a wrapper can never make an undeclared tool repeatable. Pass
`effect=` to override the class. Approval combines the same way: a call pauses when any gate
says so, whether `module_tool(requires_approval=...)`, the manifest's write gate, or the
wrapped tool's own `requires_approval`, so wrapping a tool never removes its gate.

You rarely call `module_tool` directly; [MCP and OpenAPI tools](mcp-and-openapi.md) use it for you.

## web_fetch_tool: fetch a web page safely

`web_fetch_tool()` returns a ready-made, read-only tool that fetches one `http` or `https` page with GET and gives the model readable text. HTML is stripped of scripts, styles and tags; plain text, JSON and Markdown come back as text; other content types are refused. It is marked `idempotent`.

It is built on `SafeHttpClient` and `DestinationPolicy`, so a model-chosen URL cannot reach your network. By default it refuses private, loopback, link-local, deprecated site-local IPv6 (`fec0::/10`), multicast, reserved, unspecified and cloud-metadata addresses (IPv4 and IPv6, including IPv4-mapped IPv6 and decimal, octal and hex spellings), hostnames that resolve to such addresses, URLs with a user name or password, and non-http schemes. Each redirect is checked again. The response is cut at `max_bytes` (the result says `"truncated": true`), and there is a timeout and a redirect limit. Failures return to the model as `{"error": ..., "message": ...}`; headers and secrets are never included.

To limit the tool to chosen sites, pass `allowed_domains=["example.com"]`: the domain and its subdomains are allowed, every other site is refused, and the address checks still apply. Do not confuse it with `DestinationPolicy(allowed_hosts=[...])`, which restricts nothing: it lets the listed hosts skip the address checks (for a trusted internal host), so list only hosts you control. The `transport` argument is a hook for tests and local use.

```python
import asyncio
import socket

import httpx

from nexura_agents.network.policy import DestinationPolicy
from nexura_agents.web_tools import web_fetch_tool


def handler(request: httpx.Request) -> httpx.Response:
    html = "<html><script>x()</script><h1>Docs</h1><p>Hello   world</p></html>"
    return httpx.Response(200, text=html, headers={"content-type": "text/html"})


# Offline example: a mock transport stands in for the network, and DNS is faked.
socket.getaddrinfo = lambda host, port, *a, **k: [
    (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))
]
fetch = web_fetch_tool(
    allowed_domains=["example.com"],
    max_bytes=50_000,
    transport=httpx.MockTransport(handler),
)

page = asyncio.run(fetch.arun(url="https://docs.example.com/"))
assert page["text"] == "Docs\nHello world"

blocked = asyncio.run(fetch.arun(url="http://169.254.169.254/latest/meta-data"))
assert blocked["error"] == "refused"
elsewhere = asyncio.run(fetch.arun(url="https://other.example.org/"))
assert elsewhere["error"] == "refused"  # not on allowed_domains

# The default policy object, spelled out; pass your own to change the address rules.
assert web_fetch_tool(policy=DestinationPolicy()).name == "web_fetch"
```

## Code execution sandbox (`nexura.tools.sandbox.execute`)

The sandbox module runs model-written Python in a disposable worker. It is hidden from the Studio palette, and agents cannot use it as a tool, until the operator enables a backend. Every call needs a human approval bound to the exact code, even after it becomes visible.

Backends, weakest isolation first. Monty is part of `nexura-runtime`; `wasi`, `bubblewrap` and `gvisor_oci` are the separate `nexura-sandbox` package (`pip install nexura-sandbox`, or `"nexura-sandbox[wasi]"` for WASI). Without that package those three stay selectable but probe as unavailable with "pip install nexura-sandbox", so selection skips them as it would on a host without their binaries:

| Backend | Isolation | Runs | Needs |
| --- | --- | --- | --- |
| `monty` (default) | `interpreter`: Rust Python-subset interpreter in a separate worker process with an empty environment | A Python subset: no class inheritance, no generators, no third-party packages, partial standard library, no stdin | `pip install "nexura-runtime[sandbox]"`; any OS, no root |
| `wasi` (explicit opt-in) | `interpreter`: CPython 3.13.15 in a bounds-checked WebAssembly VM, in a disposable child | Full pure-Python standard library, classes, generators and stdin; no native extension packages, sockets or processes | `pip install "nexura-sandbox[wasi]"` (wasmtime 49.0.0) and the explicitly installed pinned runtime; no namespaces or root |
| `bubblewrap` (explicit opt-in) | `os_sandbox`: Linux namespaces, read-only runtime, bounded tmpfs | Native CPython and operator-installed packages | `pip install nexura-sandbox`; Linux, `bwrap`, `/usr/bin/prlimit` (util-linux), permitted user namespaces |
| `gvisor_oci` | `gvisor`: user-space kernel, non-root guest, read-only rootfs and inputs, bounded tmpfs | Full CPython from the rootfs | `pip install nexura-sandbox`; Linux, explicit absolute `runsc`/rootfs paths, OCI seccomp support and delegated cgroups |
| `trusted_subprocess` | `none` | Trusted local scripts only | Never selected; only an explicit `worker=` uses it, and with `backends` enabled only if the operator also sets `allow_unisolated=True` |

Enable it as the operator, before the service or Studio starts:

```text
NEXURA_SANDBOX_BACKENDS=monty                 # or monty,gvisor_oci
NEXURA_SANDBOX_MIN_ISOLATION=interpreter      # interpreter | os_sandbox | gvisor | microvm
NEXURA_SANDBOX_TIMEOUT_SECONDS=10             # optional operator limits
NEXURA_SANDBOX_MAX_MEMORY_MB=256
NEXURA_SANDBOX_MAX_OUTPUT_BYTES=1048576
```

In code, pass the same settings in `SandboxConfig(backends=["monty"], min_isolation="interpreter")` to `SandboxExecutionModule(config=...)`. The module picks the first enabled backend that is installed and meets `min_isolation`. If none qualifies, it stays hidden and every call returns `CAPABILITY_UNAVAILABLE` naming each rejected backend. The module only becomes visible in Studio when a backend qualified.

Limits come only from the operator. Tool arguments may lower the timeout, memory and output caps, never raise them. They cannot set environment variables, grant network access, or choose the backend or binary. Code, stdin and input files together are capped at 5 MB and 20 files by default. Oversized inputs return `INPUT_LIMIT_EXCEEDED` before anyone is asked to approve them: a direct call checks them before the approval, and a workflow fails the run (status `failed`, no pending approval) instead of pausing. The workflow does the same for any call the module would refuse anyway, such as when no backend qualifies. Network is disabled by default.

At most 4 sandbox executions run at once per workspace (`SandboxConfig(max_concurrent_executions=...)`, 1 to 64). Further calls wait in a queue for a free slot. A call that is still waiting when its deadline passes returns `EXECUTION_TIMEOUT`.

Monty errors are reported, not crashed on:

- An unsupported feature returns `UNSUPPORTED_FEATURE`.
- The heap limit returns `MEMORY_LIMIT_EXCEEDED`. So does a worker that was killed for exceeding its memory limit.
- The time limit returns `EXECUTION_TIMEOUT`.
- `print()` output past the output cap returns `OUTPUT_LIMIT_EXCEEDED`.
- An ordinary exception gives `exit_code` 1 with the traceback in `stderr`. Error and traceback text shares the output cap with `print()` output. When it does not fit, it is cut and ends with `[truncated: output limit reached]`.

Only errors that Monty raises outside the code's own frames count as limits. If the code raises a `TimeoutError` or `MemoryError` whose message copies a limit message, the result is still an ordinary `exit_code` 1. One consequence: a heap-limit error inside a line of the code, such as `"x" * 10**9`, is also reported as `exit_code` 1 with `MemoryError: memory limit exceeded` in `stderr`. The same applies to sleeping past the time budget.

The value of a final bare expression is never copied back to the host; only `print()` output is returned. Input files appear read-only under `/sandbox`.

On POSIX systems (tested on Linux) the Monty worker starts through a one-time exec wrapper. As a result:

- It inherits no host file descriptors besides its stdin/stdout pipes.
- It runs in an empty, read-only scratch directory.
- Its own stderr (crash messages) goes to a pipe that the host reads, not to the host's stderr.

The result details record this as `worker_spawn: wrapped`. On Windows the worker is spawned directly (`worker_spawn: direct`).

A hosted multi-tenant deployment must use `gvisor` isolation or stronger (`NEXURA_SANDBOX_MIN_ISOLATION=gvisor`), never Monty alone. `create_api_app` enforces this at startup for the `authenticated-shared-service` profile. If any sandbox module in the service registry, or in the versioned registry behind it, would run code below `gvisor`, startup fails with an error that names `gvisor`. That includes Monty and an explicit unisolated worker. A sandbox module with no qualifying backend never runs code, so it is allowed.

### gVisor OCI

For servers, use the hardened profile `SandboxConfig.gvisor_production(...)` and its
host preflight `python -m nexura_sandbox.preflight`: see
[Production gVisor profile](sandbox-production.md) for the operator checklist and limits.

Enable `gvisor_oci` in the operator's backend list and supply **absolute** paths through `SandboxConfig.runsc_path` / `rootfs_path` or `NEXURA_RUNSC_PATH` / `NEXURA_OCI_ROOTFS_PATH`. Neither `PATH` nor a file in the current directory discovers the runtime. The rootfs must contain Python and libraries readable/executable by UID/GID 65534.

For example, construct `SandboxConfig(backends=["gvisor_oci"], min_isolation="gvisor", runsc_path="/opt/gvisor/runsc", rootfs_path="/opt/gvisor/rootfs", gvisor_cgroup_parent="/nexura")` and pass it to `SandboxExecutionModule(config=...)`. runsc requires root on the reviewed host; the operator must supply those privileges and provision a writable/delegated cgroup hierarchy with memory and pids controllers at that parent; runsc creates a separate child per execution. No cgroup is provisioned by the SDK. A missing delegation fails closed, without automatically adding `--ignore-cgroups` or falling back to host execution. The availability probe checks paths/configuration; it does not prove the runtime can start on this host.

The guest runs with UID/GID 65534, no supplementary groups, no capabilities and `noNewPrivileges`. `RLIMIT_NPROC` equals `max_processes` (default 1). Host cgroups also bound memory and tasks. Their pids cap is `max_processes + gvisor_runtime_pids` (default 1 + 128): sentry/gofer threads need a separate allowance from guest processes. `gvisor_runtime_pids` is an operator setting, 64–4096. This does not raise the guest NPROC limit.

The default network mode is `--network=none`. An operator-authorized `allow_network` retains the existing `--network=sandbox` netstack mode; tool arguments cannot grant that permission themselves.

With `max_processes=1`, an OCI seccomp filter denies `fork`, `vfork`, `clone` and `clone3`, including native calls, with `SCMP_ACT_ERRNO` and `errnoRet=EPERM`. New threads are denied too. Guest code may catch the resulting `OSError` and continue; a denied syscall does not itself terminate the program or force a failed execution result. The installed runsc's `features` command documents its supported actions as `KILL`, `TRAP`, `ERRNO`, `TRACE` and `ALLOW` (each prefixed `SCMP_ACT_`); it does not support `KILL_PROCESS`. The generated profile uses only `ALLOW` and `ERRNO`. The backend explicitly passes `--oci-seccomp`; a runsc release without that flag/filter support fails during launch, without disabling seccomp or retrying. For higher process caps this extra fork/clone filter is omitted; guest NPROC and the host pids cgroup remain. Bubblewrap has its own mandatory native Linux seccomp policy.

Scratch is a tmpfs mounted at `/sandbox`, owned by the guest, capped by `gvisor_tmpfs_bytes` (32 MiB by default, 1–512 MiB). `TMPDIR` points there, and FSIZE also caps individual files. The host has no writable scratch bind mount. Code and inputs are staged in a bounded task directory and mounted separately read-only at `/tmp`; links in `/sandbox` expose the input names. The rootfs, `/proc` and the small `/dev` mount are read-only. Code may replace a scratch link but cannot modify the input mount. The backend passes a fixed guest environment and rejects caller environment variables even on direct worker calls.

Timeout, output overflow and cancellation stop the launcher process group, attempt `runsc kill --all ... KILL` and `runsc delete --force`, then drain/reap the subprocess. Deletion alone does not release runsc's host `state/null-netns` mount: cleanup explicitly unmounts it, verifies no task mounts remain, and removes the private task directory. An unmount or directory removal failure is surfaced; removal is never attempted across a remaining mount. Cleanup is awaited on the current event loop and protected from repeated cancellation. Active bookkeeping is removed in a `finally`, including when cleanup fails; an empty active count alone does not prove successful teardown. Teardown errors are reported on normal completion or attached to the original cancellation/error.

The legacy test profile requires **`NEXURA_REAL_GVISOR=1`**. Only that profile permits a non-empty `extra["runsc_command_prefix"]` (such as `sudo -n`) and `extra["allow_without_cgroups"]=True`. The latter adds `--ignore-cgroups` and reports `limits_advisory: true` / `cgroups: ignored_test_profile`: host memory/pids cgroups are then absent; guest NPROC, single-process seccomp and scratch/output/wall limits still apply. The authenticated shared-service startup guard refuses either bypass regardless of the environment variable. CWD discovery remains disabled even in the test profile. This profile is for isolated host tests.

The final audit records real gVisor host verification, including R1–R8 (45 passed with gVisor), using the explicit sudo test profile with host cgroups ignored. That audit did not verify delegated memory/pids cgroups. P38 C8 later ran the `gvisor_production` profile with enforced memory/pids cgroups on the same host (runsc still through the test-only `sudo -n` prefix); see [Production gVisor profile](sandbox-production.md). Local S5/final-fix verification uses fake process transports and harmless fixed scripts; the new crash-cleanup changes still require the lead's host rerun.

Each run has a separate watchdog monitoring the service through a pidfd. Abrupt service death triggers bounded `runsc delete --force`, null-netns unmount and checked task-directory removal. Normal teardown stops and reaps the watchdog. On a worker's first execution, a bounded startup sweep also checks marked `/tmp/nexura_gvisor_*` directories owned by the same service identity (UID, installed backend path, service cgroup and configured runtime/rootfs/prefix). It only deletes stale task state after a successful `runsc list` proves the container absent; it never touches a live container or active worker. Unmarked older directories and uncertain/failed cleanup remain for operator inspection. Watchdog failure, host shutdown or insufficient cleanup privileges can still leave state. Use a supervisor with systemd `KillMode=control-group` or a container to stop all service descendants on exit; this remains necessary if the watchdog itself is killed.

### WASI: full Python without host capabilities

Install `"nexura-sandbox[wasi]"` (it adds `wasmtime==49.0.0`) in the SDK's Python environment. Obtain the CPython 3.13.15
WASI ZIP from the [operator release source](https://github.com/brettcannon/cpython-wasi-build/releases)
and transfer it to the host. Installation never performs a network download:

```bash
nexura-runtime sandbox install-runtime wasi --from-file /path/to/python-wasi.zip \
  --runtime-dir /opt/nexura/cpython-wasi-3.13.15
export NEXURA_SANDBOX_WASI_RUNTIME=/opt/nexura/cpython-wasi-3.13.15
export NEXURA_SANDBOX_BACKENDS=monty,wasi
export NEXURA_SANDBOX_MIN_ISOLATION=interpreter
```

The required archive SHA256 is
`67e1c32a85d5e0600c0939f5fd4023ca0127f1a9e81f5499697467bda67af78d`.
Without `--runtime-dir`, installation uses
`~/.local/share/nexura/runtimes/cpython-wasi-3.13.15`; Python callers can set
`SandboxConfig(wasi_runtime_path="/absolute/operator/runtime", backends=["wasi"])`.
The installer verifies the archive before extraction, preserves its `LICENSE`, hashes
the extracted files, and compiles a native Wasmtime cache in a child process. It refuses
an existing destination. The runtime and serialized cache must be owned by the operator
and inaccessible to tenants for writing. Neither artifact is included in the SDK wheel.
Missing/changed runtime files or a missing dependency produce `CAPABILITY_UNAVAILABLE`.

Every call uses a disposable child with an empty host environment, one Engine and a fresh
Store. CPython starts with `python -I -S -u`; the guest receives only explicitly allowed
request environment variables on direct worker calls. The module continues to refuse
input environment variables. Only `/lib` (stdlib) and `/sandbox` (input copies) are
preopened read-only. No home, project, host root, sockets or process capabilities are
provided. The guest has the full pure-Python stdlib and real classes/generators; modules
depending on unavailable native facilities can fail to import or operate. Native extension
packages such as numpy and Python threads are unsupported. This is `interpreter` isolation
and does not meet the shared-service `gvisor` minimum.

`timeout_seconds` drives a host timer incrementing the Wasmtime epoch; `wasi_fuel`
(default 4 billion instructions of fuel) independently bounds CPU work. Fuel is not a
portable number of CPU seconds. `max_memory_mb` limits guest linear memory, excluding
the native runtime's own memory. A host watchdog kills/reaps blocked children. Native
timeout/memory traps map to `EXECUTION_TIMEOUT` / `MEMORY_LIMIT_EXCEEDED`. CPython may
instead raise an ordinary `MemoryError` and exit with code 1; guest-written error text
cannot forge resource outcomes. Stdout/stderr share `max_output_bytes`, including
tracebacks and `os.write`; overflow produces `OUTPUT_LIMIT_EXCEEDED`. Invalid UTF-8 bytes
are dropped from returned text. The recursion limit defaults to 200
(`wasi_recursion_limit`, maximum 1000); full CPython code can change its own recursion limit,
so the Store/fuel/epoch limits remain the security boundary.

Scratch is absent unless the operator sets `wasi_scratch_enabled=True`. When enabled,
`/scratch` is a fresh temporary directory, with `wasi_scratch_bytes` (32 MiB default) and
`wasi_scratch_files` (200 default). WASI syscall guards cap cumulative requested file-write
bytes and directory entries; overwrites also consume the write budget. Sparse allocation,
resize, writable-file seeking and descriptor renumbering are refused. `pwrite` offsets
cannot exceed the byte cap. Quota overflow produces `SECURITY_VIOLATION_DISK_LIMIT`.
Directory metadata uses host filesystem space bounded by the entry cap. Scratch is
removed after success, error, timeout or cancellation.

With both `monty` and `wasi` enabled, a conservative token scan selects WASI for classes
and generators before execution (it may also select a comprehension inside a call).
Other Monty `UNSUPPORTED_FEATURE` refusals (including stdin) retry the
same approved request on WASI with the remaining deadline and the same caps/isolation
floor. The actual backend/isolation is returned for each call. An unavailable enabled
WASI refuses the full-Python request; it never selects a weaker boundary.

### Bubblewrap on Linux

Bubblewrap is never in the default backend list. Enable it explicitly:

```text
NEXURA_SANDBOX_BACKENDS=bubblewrap
NEXURA_SANDBOX_MIN_ISOLATION=os_sandbox
NEXURA_SANDBOX_BWRAP_PYTHON=/usr/bin/python3
# Optional absolute paths:
# NEXURA_SANDBOX_BWRAP_BIN=/usr/bin/bwrap
# NEXURA_SANDBOX_BWRAP_RUNTIME=/opt/my-sandbox-venv
# NEXURA_SANDBOX_BWRAP_SECCOMP_BPF=/opt/policies/sandbox-native.bpf
```

The equivalent Python setup is `SandboxExecutionModule(config=SandboxConfig(backends=["bubblewrap"], min_isolation="os_sandbox", bubblewrap_python_path="/usr/bin/python3"))`. For an operator-built venv, also set `bubblewrap_runtime_roots=["/opt/my-sandbox-venv"]`, `bubblewrap_runtime_path="/opt/my-sandbox-venv"` and `bubblewrap_python_path="/opt/my-sandbox-venv/bin/python"`. Extra runtime mounts require an explicit approved root and are read-only at `/opt/nexura-runtime`. Home directories (including passwd account homes and their descendants), the project, `/etc`, `/root`, `/var/run`, `/run`, `/proc`, `/sys`, and their ancestors are refused even if approved. Its base Python, libraries and symlink targets must be available under `/usr` or that runtime. Choose a dedicated runtime containing only approved packages and no secrets. Nothing downloads or installs packages during execution.

The probe actually runs a short `bwrap ... /usr/bin/true` with the same namespace/mount policy. Missing binaries, blocked user namespaces (including AppArmor restrictions), unsupported flags and probe timeouts produce an unavailable reason. A failed probe never falls back to a host subprocess. When bubblewrap and a stronger backend are enabled, the stronger backend is tried before bubblewrap even if bubblewrap was listed first. `min_isolation="gvisor"` always excludes bubblewrap.

Workers start with an empty host environment and a fixed guest environment. Only `/usr`, required `/lib`, `/lib64`, `/bin` paths and the approved runtime are exposed read-only; operators must audit these contents for secrets. Home/project trees and host `/proc` are not mounted, network namespaces are private, and the mandatory seccomp policy denies keyctl/add_key/request_key. This blocks key payload access but alone does not hide key descriptions in procfs: `/proc/keys` and `/proc/key-users` are separately masked by read-only binds of `/dev/null`. `--new-session` alone does not isolate keyrings. Input files are read-only under `/sandbox`; code arrives through an anonymous descriptor and is copied into `/tmp/code.py`. The working directory is `/tmp/work`. By default bwrap itself creates `/tmp` with a 32 MiB byte cap (`bubblewrap_tmpfs_bytes`, 1–512 MiB). **The byte cap is enforced; no inode cap is enforced by default**, including on Ubuntu hosts restricting unprivileged user namespaces. The default path needs no Python remount or privileged launcher. Capabilities are dropped, further user namespaces are disabled, code runs as UID/GID 65534 and the root, `/proc` and `/dev` are read-only.

An explicit `bubblewrap_tmpfs_inodes=4096` opts into an inode cap (128–65536). This uses a trusted outer launcher to remount `/tmp` with `size` and `nr_inodes`, followed by inner bwrap applying the same payload restrictions and mandatory seccomp. Ubuntu's default `kernel.apparmor_restrict_unprivileged_userns=1` can deny this remount; a requested cap then fails the probe/launch without silently falling back. Leave `bubblewrap_tmpfs_inodes=None` (the default) on that host. `RLIMIT_NOFILE` does not bound inode creation, and neither a small tmpfs byte cap nor `--perms` supplies an inode limit. Results report `tmpfs_inodes: null` / `tmpfs_inodes_enforced: false` by default, and the requested count / `true` only after a successful opt-in launch.

Address-space and file-size limits are applied before launch. After trusted setup forks, `/usr/bin/prlimit` applies `max_processes`, an exact soft/hard CPU limit (`bubblewrap_cpu_seconds`, default 5 seconds), and `RLIMIT_NOFILE` before Python. `bubblewrap_max_open_files` defaults to 128, with a hard operator cap of 128 (operators may lower it to 16); both soft and hard limits are set. This bounds simultaneous socket descriptors and their buffers, not all kernel memory. `/dev/pts/ptmx` is masked with `/dev/null` to deny PTY allocation. NPROC counts the real user's processes/threads, so other jobs may make it stricter. Signal exits, including bwrap's encoded 137/152, produce `EXECUTION_TIMEOUT`; a SIGKILL cannot distinguish a hard CPU kill from another resource/operator kill or an explicit exit 137. Output shares one byte cap across stdout and stderr. Before releasing execution, both the host and an independent watchdog pin the outer namespace init with pidfds. Timeout, cancellation and overflow SIGKILL that init, killing every nested sandbox process even if code clears PDEATHSIG, then kill the launcher's process group. Readers are cancelled and their transports closed, with a bounded reap; teardown never drains untrusted pipes to EOF. `--die-with-parent` alone cannot guarantee crash cleanup: code acting as PID 1 can clear PDEATHSIG. The watchdog monitors service death, including SIGKILL/OOM, and kills the pinned namespace init. Each run also enters its own cgroup v2 child under the service cgroup when delegated; teardown uses `cgroup.kill`. Each run uses a dedicated stdlib launcher supervisor that alone enables `PR_SET_CHILD_SUBREAPER`, kills its launcher group and reaps adopted descendants. The hosting process's subreaper setting is unchanged; namespace-init/process-group teardown also applies without delegation. These cgroups provide lifecycle cleanup only, not aggregate memory/CPU limits. Linux pidfd support is required. A private start marker distinguishes launch failures from code-written error text. Run the service under systemd with `KillMode=control-group` or inside a container; supervisor cleanup is required if the watchdog also dies or the host interrupts cleanup.

**Seccomp is mandatory.** A dependency-free cBPF generator supports native x86-64 and AArch64 and refuses unknown architectures. It returns `EPERM` for io_uring setup/enter/register, keyctl/add_key/request_key, bpf, perf_event_open, userfaultfd, ptrace, mount/new mount APIs, unshare/setns, process-memory inspection, memfd_create/memfd_secret and SysV shared-memory/message APIs. Socket and socketpair calls permit only AF_UNIX; compat ABIs are denied. The optional `bubblewrap_seccomp_bpf_path` adds a filter through `--add-seccomp-fd` and cannot weaken the built-in policy. Unreadable, malformed or kernel-rejected filters fail closed. There is no no-seccomp opt-out. A successful real launch reports `seccomp: true`.

This backend shares the Linux kernel. It has no aggregate resource accounting, side-channel protection or VM boundary. `max_memory_mb` bounds per-process address space, not total host memory; byte-capped tmpfs data, inode/dentry metadata and other kernel allocations are additional. The default `enforced_limits` names `address_space` and `disk_bytes`, with no aggregate memory or inode guarantee. A successful inode opt-in adds `tmpfs_inodes` to result details' enforced limits. memfd and SysV shared memory are blocked and `/dev/shm` is read-only, but these controls do not establish an aggregate RAM ceiling. Native extensions and subprocesses can run within the configured process cap. Keep the kernel, bubblewrap and approved runtime patched. Bubblewrap does not meet the shared-service gVisor minimum; neither a systemd scope nor Monty/WASI nested inside bubblewrap is implemented here. The final audit verified the default bubblewrap profile on the lead host; the new final-audit fixes still require a host rerun. Offline policy/transport tests do not establish containment.

## See also

- [Agents](agents.md)
- [Human approval](approvals.md)
- [MCP and OpenAPI tools](mcp-and-openapi.md)
- [Testing your agents](testing.md)
- [Troubleshooting](troubleshooting.md)

## Package overview

**Stronger code-execution sandboxes for `nexura-runtime`: WebAssembly (WASI), Linux namespaces
with seccomp (bubblewrap), and a user-space kernel (gVisor).**

```bash
pip install nexura-sandbox           # bubblewrap and gVisor backends (installs nexura-runtime)
pip install "nexura-sandbox[wasi]"   # + wasmtime, for the WASI backend
```

`nexura-runtime` runs untrusted code through one module, `nexura.tools.sandbox.execute`. The
operator picks backends with `NEXURA_SANDBOX_BACKENDS` (for example `monty,gvisor_oci`) and a
floor with `NEXURA_SANDBOX_MIN_ISOLATION`; the runtime selects the first enabled backend that is
available on the host and meets the floor, and every call needs a human approval. The runtime
ships the selection, `SandboxConfig`, the module and the default Monty backend
(`nexura-runtime[sandbox]`). This package adds:

| Backend | Class | Isolation | Needs on the host |
|---|---|---|---|
| `wasi` | `WasiWorker` | `interpreter` | the `wasi` extra (wasmtime 49.0.0) and the pinned CPython WASI runtime: `nexura-runtime sandbox install-runtime wasi --from-file ZIP` |
| `bubblewrap` | `BubblewrapWorker` | `os_sandbox` | Linux with unprivileged user namespaces and the `bwrap` binary |
| `gvisor_oci` | `GVisorOCIWorker` | `gvisor` | an operator-configured `runsc` binary and OCI rootfs (`NEXURA_RUNSC_PATH`, `NEXURA_OCI_ROOTFS_PATH`), delegated cgroups |

Without this package the runtime still works: these backend names stay selectable but probe
as unavailable with "The 'gvisor_oci' sandbox backend is provided by the 'nexura-sandbox'
package, which is not installed. Install it with: pip install nexura-sandbox", so selection
falls through to Monty or refuses with `CAPABILITY_UNAVAILABLE`, exactly as on a host where the
backend's binary is missing. The authenticated shared-service startup guard checks sandbox
modules already in its registries and refuses configurations below `gvisor`; this is a
startup check, not a continuous guard over later registry changes.

> Status: **alpha** (0.4), Python 3.12, Linux only. Escape tests ran against real bubblewrap,
> gVisor and WASI on one Ubuntu x86_64 host. Real gVisor
> verification used only the explicit sudo test profile with host cgroups ignored; the
> production profile with delegated memory/pids cgroups has not been exercised. runsc
> requires root on the reviewed host. No external security review has been done.
> WASI's complete listener-based escape profile still needs a host permitting local sockets;
> the recorded shared escape run had one host-listener setup error (`socket()` denied).

Bubblewrap enforces a tmpfs byte cap but no inode cap unless `bubblewrap_tmpfs_inodes`
is set. It shares the host kernel with no aggregate memory/CPU ceiling; address-space
and CPU limits are per process, and `max_memory_mb` is not total RAM.

Use a service supervisor (systemd `KillMode=control-group` or a container) to stop all
descendants on service exit. Per-run crash watchdogs and delegated bubblewrap lifecycle
cgroups add cleanup, but cannot replace the supervisor if the watchdog is also killed.

Each bubblewrap/gVisor launcher runs under a dedicated stdlib supervisor child,
which alone enables subreaping and kills/reaps its own descendants. Worker calls
leave the hosting process's subreaper flag unchanged. Watchdog and cleanup pipe
polling supports descriptors above 1023. All helpers close unrelated descriptors;
only the explicit lifecycle and bubblewrap input descriptors are inherited.
In the legacy privileged gVisor test profile, the configured command prefix also
invokes the fixed supervisor script with the current Python interpreter, so the
operator's test command permissions must cover that invocation.
gVisor startup sweeps only attributable stale task directories for absent containers;
unmarked older directories need operator inspection. See `docs/tools.md` and
`SECURITY_MODEL.md` for resource and isolation limitations.

## Which backends can run here

`probe` reports whether a backend can run on this host and why not. It never runs user code.

```python
from nexura_runtime.integrations.sandbox import SandboxConfig, build_backend

from nexura_sandbox import BubblewrapWorker, GVisorOCIWorker, WasiWorker

config = SandboxConfig()
for name in ("wasi", "bubblewrap", "gvisor_oci"):
    backend = build_backend(name)  # the runtime returns this package's class
    assert isinstance(backend, (WasiWorker, BubblewrapWorker, GVisorOCIWorker))
    status = backend.probe(config)
    print(f"{name:<11} isolation={backend.isolation:<12} available={status.available}")
```

## Selection with an isolation floor

An operator who requires at least `os_sandbox` never gets Monty (`interpreter`). When no
enabled backend qualifies, the module refuses every call and names each rejected backend.

```python
from nexura_runtime.integrations.sandbox import (
    CapabilityUnavailableError,
    SandboxConfig,
    select_backend,
)

config = SandboxConfig(backends=["monty", "bubblewrap", "gvisor_oci"], min_isolation="os_sandbox")
try:
    backend = select_backend(config)
    print("selected:", backend.backend_name, backend.isolation)
except CapabilityUnavailableError as exc:
    print("refused:", exc.reason)
```
