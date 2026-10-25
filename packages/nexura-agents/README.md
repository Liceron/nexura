# Nexura Agents

Build typed Python agents with tools, estimated-cost admission control, exact-action approvals and resumable runs.

## Install

```bash
pip install nexura-agents
```

Alpha and unpublished: use `pip install -e packages/nexura-agents` from the [source checkout](https://github.com/Liceron/nexura), or a locally built wheel. Core dependencies are only `httpx` and `pydantic`; `nexura-vertex`, `[litellm]` and `[otel]` add Vertex AI, LiteLLM and tracing.

## Minimal examples

A real model needs credentials and is not run by the docs tests:

```python notest
from nexura_agents import Agent

agent = Agent(name="Support", instructions="Be brief.", model="vertex:gemini-2.5-flash")
print(agent.run("Hello").output)  # requires nexura-vertex and Google ADC
```

Run the same interface with a deterministic local fake, without keys:

```python
from nexura_agents import Agent, FakeModel

agent = Agent(name="Support", instructions="Be brief.", model=FakeModel(responses=["Hello!"]))
print(agent.run("Hi").output)
```

For an async application:

```python
import asyncio
from nexura_agents import Agent, FakeModel

async def main() -> None:
    agent = Agent(name="Support", instructions="Be brief.", model=FakeModel(responses=["Hello!"]))
    assert (await agent.run_async("Hi")).output == "Hello!"

asyncio.run(main())
```

A local fake refund example shows typed dependencies, typed output and approval persisted as JSON:

```python
from dataclasses import dataclass, field

from pydantic import BaseModel

from nexura_agents import (
    Agent,
    FakeModel,
    ModelRetry,
    RunContext,
    Runner,
    RunInterruption,
    ToolCall,
    UsageLimits,
    tool,
)


@dataclass
class Shop:  # your database, clients, current user...
    orders: dict[str, float]
    refunds: list[str] = field(default_factory=list)


class Decision(BaseModel):  # the agent must return exactly this
    order_id: str
    refunded: float


@tool
async def get_order(ctx: RunContext[Shop], order_id: str) -> float:
    """Return the order total."""
    if order_id not in ctx.deps.orders:
        raise ModelRetry(f"No order {order_id}. Order IDs look like A1.")  # model retries
    return ctx.deps.orders[order_id]


@tool(requires_approval=lambda ctx, args: args["amount"] > 100)  # big refunds wait
async def refund(ctx: RunContext[Shop], order_id: str, amount: float) -> str:
    """Refund an order."""
    ctx.deps.refunds.append(f"{order_id}:{amount}")
    return "refunded"


model = FakeModel(
    responses=[  # stands in for "vertex:gemini-2.5-flash"
        [ToolCall(id="1", name="get_order", arguments={"order_id": "A1"})],
        [ToolCall(id="2", name="refund", arguments={"order_id": "A1", "amount": 250.0})],
        '{"order_id": "A1", "refunded": 250.0}',
    ]
)
agent = Agent(
    name="Refunds",
    instructions="Resolve refund requests.",
    model=model,
    tools=[get_order, refund],
    output_type=Decision,
)
runner = Runner(usage_limits=UsageLimits(request_limit=8))
shop = Shop(orders={"A1": 250.0})

result = runner.run(agent, "Refund order A1 in full", deps=shop)
assert result.status == "paused" and shop.refunds == []  # nothing happened yet
saved = result.interruption.to_json()  # store it anywhere

# ...later, maybe in another process, after a human reviews paused.pending:
paused = RunInterruption.from_json(saved)
result = runner.resume(agent, paused, approve=True, deps=shop)  # or reject={id: "reason"}
print(result.output)  # order_id='A1' refunded=250.0
print(shop.refunds)  # ['A1:250.0']
```

## Status and limits

**Alpha (0.4), unpublished.** Python 3.11–3.13 declared; GitHub Actions runs the wheel's test suite on 3.11, 3.12 and 3.13 (Linux) and on 3.12 for macOS and Windows, all passing since 2026-10-21. On 2026-10-09, 295 SDK tests passed from wheels on 3.11 and 3.13, excluding 4 platform-importing files. `nexura-runtime` requires Python 3.12. Providers are mock-verified except owner-run Vertex AI (2026-10-08: reply/tool/stream) and Gemini API (2026-10-20: reply/tool/stream/structured output); OpenAI, Anthropic and Sarvam live checks have not run. LiteLLM is checked only with the real library's offline mock mode.

MIT edition: **single user, this machine only; for a team or a public endpoint use the hosted or enterprise edition**. Agent teams compose code, not human accounts. No external security review; no real-world users yet. Budgets reserve estimates, not an invoice ceiling; unknown usage fails closed under token limits. Only marked tools pause for approval; canonical digests refuse non-string mapping keys. MCP serving is checked with this SDK's in-process client only, not external apps. A2A has no push notifications, ListTasks/Subscribe, HTTP+JSON/gRPC, signed cards or OAuth; no live third-party-agent checks. There are no hosted web-search/code/file tools or tracing UI. See [all SDK details](https://github.com/Liceron/nexura/blob/main/docs/agents.md), [approvals](https://github.com/Liceron/nexura/blob/main/docs/approvals.md), [budget limits](https://github.com/Liceron/nexura/blob/main/docs/budgets-and-usage.md), [verification history](https://github.com/Liceron/nexura/blob/main/docs/testing.md#verification-record) and the [historical comparison caveats](https://github.com/Liceron/nexura/blob/main/docs/comparisons.md).

## Documentation and licence

[Main documentation](https://github.com/Liceron/nexura/blob/main/docs/README.md): 46 guides plus the generated [API reference](https://github.com/Liceron/nexura/blob/main/docs/api-reference.md). More examples: [getting started](https://github.com/Liceron/nexura/blob/main/docs/getting-started.md), [harness](https://github.com/Liceron/nexura/blob/main/docs/harness.md), [teams](https://github.com/Liceron/nexura/blob/main/docs/teams.md), [providers](https://github.com/Liceron/nexura/blob/main/docs/providers.md), [MCP/OpenAPI](https://github.com/Liceron/nexura/blob/main/docs/mcp-and-openapi.md). MIT, Copyright (c) 2026 Liceron; see [LICENSE](https://github.com/Liceron/nexura/blob/main/packages/nexura-agents/LICENSE). Security reports: info@liceron.in.
