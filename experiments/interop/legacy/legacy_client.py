"""Official MCP Python SDK 1.30.0 client (handshake revisions only), driven by the tests.

    python legacy_client.py stdio PYTHON SERVER_SCRIPT
    python legacy_client.py http URL TOKEN

Prints one JSON object with what it saw.
"""

from __future__ import annotations

import asyncio
import json
import sys

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client


async def exercise(session: ClientSession) -> dict[str, object]:
    init = await session.initialize()
    tools = await session.list_tools()
    added = await session.call_tool("add", {"a": 20, "b": 22})
    refused = await session.call_tool("delete_order", {"order_id": "A1"})
    resource = await session.read_resource("nexura://docs/readme")  # type: ignore[arg-type]
    prompt = await session.get_prompt("greet", {"name": "Ann"})
    return {
        "protocolVersion": init.protocolVersion,
        "tools": [t.name for t in tools.tools],
        "add": added.content[0].text,  # type: ignore[union-attr]
        "refused": {"isError": refused.isError, "text": refused.content[0].text},  # type: ignore[union-attr]
        "resource": resource.contents[0].text,  # type: ignore[union-attr]
        "prompt": prompt.messages[0].content.text,  # type: ignore[union-attr]
    }


async def main() -> None:
    if sys.argv[1] == "stdio":
        params = StdioServerParameters(command=sys.argv[2], args=[sys.argv[3], "stdio"])
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            out = await exercise(session)
    else:
        headers = {"Authorization": f"Bearer {sys.argv[3]}"}
        async with (
            streamablehttp_client(sys.argv[2], headers=headers) as (read, write, _),
            ClientSession(read, write) as session,
        ):
            out = await exercise(session)
    print(json.dumps(out))


if __name__ == "__main__":
    _ = httpx
    asyncio.run(main())
