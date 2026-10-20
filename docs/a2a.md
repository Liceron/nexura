# Agent2Agent (A2A) interoperability

[A2A](https://a2a-protocol.org) is an open protocol that lets agents built with different frameworks talk to each other. Nexura speaks it in both directions: `A2AServer` serves a Nexura agent or team as an A2A agent, and `A2AClient` / `remote_agent_tool` call any remote A2A agent from a Nexura agent.

**Protocol version implemented: A2A 1.0, JSON-RPC binding** (methods `SendMessage`, `SendStreamingMessage`, `GetTask`, `CancelTask`; agent card at `/.well-known/agent-card.json`). The 0.3 method names `message/send`, `message/stream`, `tasks/get` and `tasks/cancel` are also accepted, answered in the 0.3 payload shape. Field names, error codes and states were checked against the official `a2a-sdk` 1.2.2 sources; that package is not a dependency.

## Try it in-process

This example serves an agent and calls it through an `httpx.ASGITransport`, so no port is opened. Against a real deployment you only change the URL.

```python
import asyncio

import httpx

from nexura_agents import A2AClient, A2AServer, Agent, FakeModel

support = Agent(
    name="Support",
    description="Answers questions about orders.",
    instructions="Help customers.",
    model=FakeModel(responses=["Order A1 is shipped.", "Yes, still shipped."]),
)

server = A2AServer(support, url="http://support.test/")
app = server.http_app(token="s3cret")


async def main() -> None:
    client = A2AClient(
        "http://support.test",
        headers={"Authorization": "Bearer s3cret"},
        transport=httpx.ASGITransport(app=app),
    )
    card = await client.get_card()
    assert card["name"] == "Support"

    task = await client.send("Where is order A1?")
    assert task.state == "completed" and task.text == "Order A1 is shipped."

    # Follow-ups in the same context continue the conversation.
    again = await client.send("Still shipped?", context_id=task.context_id)
    assert again.text == "Yes, still shipped."

    # Streaming yields the task, status changes and the output artifact.
    kinds = [event.kind async for event in client.stream("one more", context_id=task.context_id)]
    assert kinds[0] == "task" and kinds[-1] == "status"


asyncio.run(main())
```

## Serve with uvicorn

```python notest
# app.py
from nexura_agents import A2AServer, Agent

agent = Agent(
    name="Support",
    description="Answers order questions.",
    instructions="...",
    model="openai:gpt-4o-mini",
)
server = A2AServer(agent, url="https://agents.example.com/")
app = server.http_app(token="a-long-random-token", allowed_origins=[])
```

Run it with `uvicorn app:app` and point any A2A client at `https://agents.example.com`. `url` is the public address of the JSON-RPC endpoint (the app answers `POST /`; pass `path=` to `http_app` to move it), and it is what the agent card advertises.

`A2AServer(agent_or_team, *, name=None, description=None, url, version="1.0.0", skills=None, deps=None, runner=None)`

- A `Team` (`SequentialAgent`, `ParallelAgent`, `LoopAgent`) is served as one agent.
- `skills` are card skills as dicts (`id`, `name`, `description`, `tags`); by default one skill describes the agent.
- `deps` and `runner` work as in [`AgentMCPServer`](serving-over-mcp.md): pass a `Runner` to apply usage limits, hooks or budgets.

`http_app(*, token=None, allowed_origins=(), max_body_bytes=4 MiB, max_tasks=1000, task_ttl_seconds=3600, path="/")`

- The card is public and never contains secrets. When a `token` is set, the card advertises bearer authentication and every JSON-RPC call needs `Authorization: Bearer <token>` (compared in constant time).
- Requests with an `Origin` header are refused (403) unless listed in `allowed_origins`.
- Bodies over `max_body_bytes` get 413. Errors never include internal details.
- At most `max_tasks` tasks and conversation contexts are kept, least recently used first, and idle ones expire after `task_ttl_seconds`. Running tasks are never evicted; when every slot is running, new requests get an error until one finishes. The store is in memory: tasks are lost on restart.
- Task ids are random (144 bits). One token guards the whole server, so every holder can read, continue and cancel every task. Run one server per tenant if you need isolation.
- Outputs: text output is a text part; an agent with `output_type` (or a team returning a model, dict or list) is a data part. Text and data parts of a message become the prompt; file parts with inline bytes or a URL become attachments (inline bytes are not kept in task history).
- A dropped stream does not cancel the run; use `CancelTask`.

## Call a remote A2A agent as a tool

```python
import asyncio

import httpx

from nexura_agents import A2AClient, A2AServer, Agent, FakeModel, ToolCall, remote_agent_tool

remote = Agent(
    name="Support",
    description="Answers questions about orders.",
    instructions="Help.",
    model=FakeModel(responses=["Order A1 is shipped."]),
)
transport = httpx.ASGITransport(app=A2AServer(remote, url="http://support.test/").http_app())


async def main() -> None:
    client = A2AClient("http://support.test", transport=transport)
    await client.get_card()  # lets the tool description come from the card's skills
    support_tool = remote_agent_tool(client)

    concierge = Agent(
        name="Concierge",
        instructions="Delegate order questions.",
        model=FakeModel(
            responses=[
                [ToolCall(id="1", name=support_tool.name, arguments={"request": "Where is A1?"})],
                "It is shipped.",
            ]
        ),
        tools=[support_tool],
    )
    result = await concierge.run_async("Where is order A1?")
    assert result.output == "It is shipped."


asyncio.run(main())
```

```python notest
agent = Agent(
    name="Concierge",
    instructions="...",
    model="openai:gpt-4o-mini",
    tools=[
        remote_agent_tool(
            "https://partner.example.com/agent",
            headers={"Authorization": "Bearer ..."},
            description="Partner agent that checks stock levels.",
        )
    ],
)
```

`A2AClient(url, *, headers=None, timeout=60, transport=None, policy=None)` offers `get_card()`, `send(text_or_parts, *, task_id=None, context_id=None)`, `stream(...)`, `get_task(id)`, `cancel(id)` and `decide(task_id, approve=..., reject=...)`. It returns `A2ATask` (`state`, `text`, `data`, `output`, `pending_approvals`, `raw`). It uses the 1.0 interface from the card and falls back to the 0.3 shapes for older cards.

`remote_agent_tool(url_or_client, *, name=None, description=None, requires_approval=False)` returns a `Tool` with one `request` string. It returns the remote output (structured output stays a dict). If the remote task is `input-required` or fails, the tool raises a `ToolError` the calling model sees, like `Agent.as_tool`; approvals cannot be given inside a tool call. Set `requires_approval=True` to make your own run pause before calling a partner agent.

Outbound safety matches `MCPServer.http`: requests go through the SSRF-safe client and `DestinationPolicy`; the default policy permits loopback and private addresses for the host you configured, cloud metadata addresses are always blocked, and redirects are not followed. A card that advertises an endpoint on a different host is refused unless you list that host in `DestinationPolicy(allowed_hosts=[...])`. Pass a stricter `policy=` for untrusted URLs.

## Approvals over A2A

A run that reaches a tool with `requires_approval` pauses. The task becomes `input-required`; its status message lists the pending calls with their digests, as text and as a data part `{"pendingApprovals": [{"key", "tool", "arguments", "digest"}]}`. The client continues the same task by sending a message with that `taskId` and a data part `{"approve": [...keys] | true | {key: digest}, "reject": {key: reason}}`. Every pending call needs exactly one decision. The server resumes from the stored `RunInterruption`, so approval is bound to the exact arguments it showed; an `approve` mapping whose digest does not match is refused with `-32602` and the task stays open. Team keys look like `path:call_id`.

```python
import asyncio

import httpx

from nexura_agents import A2AClient, A2AServer, Agent, FakeModel, ToolCall, tool

refunds_sent: list[str] = []


@tool(requires_approval=True)
def refund(order_id: str) -> str:
    """Refund an order."""
    refunds_sent.append(order_id)
    return f"refunded {order_id}"


agent = Agent(
    name="Refunds",
    instructions="Refund orders.",
    model=FakeModel(
        responses=[[ToolCall(id="r1", name="refund", arguments={"order_id": "A1"})], "Refunded."]
    ),
    tools=[refund],
)
app = A2AServer(agent, url="http://refunds.test/").http_app()


async def main() -> None:
    client = A2AClient("http://refunds.test", transport=httpx.ASGITransport(app=app))
    task = await client.send("Refund order A1")
    assert task.is_input_required and refunds_sent == []
    call = task.pending_approvals[0]
    print("needs approval:", call["tool"], call["arguments"])

    done = await client.decide(task.id, approve={call["key"]: call["digest"]})
    assert done.state == "completed" and refunds_sent == ["A1"]


asyncio.run(main())
```

## What is and is not implemented

Implemented: agent card, `SendMessage`, `SendStreamingMessage` (server-sent events), `GetTask`, `CancelTask`, text, data and file parts, task states `submitted`, `working`, `completed`, `failed`, `canceled` and `input-required`, contexts, the A2A and JSON-RPC error codes, the `A2A-Version` header (1.0 and 0.3 accepted), and the 0.3 method names.

Not implemented: push notifications (`-32003`), `ListTasks`, `SubscribeToTask` and the authenticated extended card (answered with their specific error codes), the HTTP+JSON and gRPC bindings, agent card signatures, OAuth and other security schemes beyond a static bearer token, extensions, multi-tenancy, task persistence, and the 0.3 agent card format on the server side (the client does read 0.3 cards). Not verified against a live third-party A2A implementation: tests run in-process against this implementation and hard-coded field names from the reference sources.

## Talk to other agents with A2A

`A2AServer(agent_or_team, url=...).http_app(token=...)` serves an agent card and the
Agent2Agent 1.0 JSON-RPC methods (`SendMessage`, `SendStreamingMessage`, `GetTask`,
`CancelTask`; 0.3 method names accepted). Approvals appear as `input-required` and are
continued with digest-bound decisions. `A2AClient` and `remote_agent_tool(url)` let your
agent call any A2A agent. Not implemented: push notifications, ListTasks and Subscribe,
the HTTP+JSON and gRPC bindings, signed cards and OAuth. The wire format was checked
against the official a2a-sdk source, not against live third-party agents. See `docs/a2a.md`.
