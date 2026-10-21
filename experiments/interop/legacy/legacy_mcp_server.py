"""A server built with the official MCP Python SDK 1.30.0, which only knows the
``initialize`` handshake revisions (newest: 2025-11-25).

    python legacy_mcp_server.py stdio
    python legacy_mcp_server.py http PORT
"""

from __future__ import annotations

import sys

from mcp.server.fastmcp import FastMCP

server = FastMCP("legacy-interop")


@server.tool()
def subtract(a: int, b: int) -> int:
    """Subtract b from a."""
    return a - b


@server.resource("memo://legacy", mime_type="text/plain")
def legacy_memo() -> str:
    """A static memo."""
    return "hello from mcp 1.30"


@server.prompt()
def summarize(text: str) -> str:
    """Ask for a summary."""
    return f"Summarize: {text}"


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
