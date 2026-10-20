"""A2A interop: serve one agent, call it from another.

Scenario: a Stock agent is served as an A2A agent (``A2AServer``). A Concierge
agent calls it through ``remote_agent_tool``. Everything runs in-process over
``httpx.ASGITransport``, so no port is opened. Against a real deployment you
replace the client with a URL.

Demonstrates: ``A2AServer.http_app`` with bearer token, ``A2AClient``,
``remote_agent_tool``, agent card discovery.

Offline: both agents use scripted ``FakeModel``. To use a real model:
# model="openai:gpt-4.1-mini"
"""

import asyncio

import httpx
from nexura_agents import (
    A2AClient,
    A2AServer,
    Agent,
    FakeModel,
    ToolCall,
    remote_agent_tool,
)
from nexura_agents.a2a import A2AError

TOKEN = "demo-token"


async def main() -> None:
    stock = Agent(
        name="Stock",
        description="Checks stock levels for products.",
        instructions="Answer stock questions.",
        model=FakeModel(responses=["Widget: 12 units in stock."]),
    )
    app = A2AServer(stock, url="http://stock.test/").http_app(token=TOKEN)
    client = A2AClient(
        "http://stock.test",
        headers={"Authorization": f"Bearer {TOKEN}"},
        transport=httpx.ASGITransport(app=app),
    )
    card = await client.get_card()
    print("remote agent card:", card["name"])

    stock_tool = remote_agent_tool(client)
    concierge = Agent(
        name="Concierge",
        instructions="Delegate stock questions to the remote agent.",
        model=FakeModel(
            responses=[
                [ToolCall(id="1", name=stock_tool.name, arguments={"request": "Widget stock?"})],
                "We have 12 widgets available.",
            ]
        ),
        tools=[stock_tool],
    )
    result = await concierge.run_async("Do you have widgets?")
    remote_reply = [m.content for m in result.messages if m.role == "tool"][0]
    print("remote replied:", remote_reply)
    print("concierge says:", result.output)

    # Wrong token is refused.
    bad = A2AClient(
        "http://stock.test",
        headers={"Authorization": "Bearer wrong"},
        transport=httpx.ASGITransport(app=app),
    )
    try:
        await bad.send("hi")
        raise AssertionError("wrong token was accepted")
    except A2AError as exc:
        print("bad token rejected:", type(exc).__name__)
    print("A2A INTEROP OK")


if __name__ == "__main__":
    asyncio.run(main())
