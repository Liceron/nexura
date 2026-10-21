"""A server built with the official MCP Python SDK (``mcp`` 2.3.0), used by the interop tests.

    python official_mcp_server.py stdio
    python official_mcp_server.py http PORT

Offline: tools, one resource and one prompt; nothing calls a model or the network.
"""

from __future__ import annotations

import sys
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from pydantic import Field

server = MCPServer("official-interop", version="2.3.0")


@server.tool()
def multiply(a: int, b: int) -> int:
    """Multiply two integers."""
    return a * b


@server.tool()
def shout(text: str) -> str:
    """Upper-case the text."""
    return text.upper()


@server.tool()
def regional_lookup(
    region: Annotated[str, Field(json_schema_extra={"x-mcp-header": "Region"})],
    key: str,
) -> str:
    """Look something up in a region (the region is mirrored into an HTTP header)."""
    return f"{key}@{region}"


@server.resource("memo://greeting", mime_type="text/plain")
def greeting() -> str:
    """A static greeting."""
    return "hello from the official SDK"


@server.prompt()
def review(code: str) -> str:
    """Ask for a code review."""
    return f"Please review: {code}"


if __name__ == "__main__":
    if sys.argv[1] == "stdio":
        server.run()
    else:
        import uvicorn

        uvicorn.run(
            server.streamable_http_app(),
            host="127.0.0.1",
            port=int(sys.argv[2]),
            log_level="warning",
        )
