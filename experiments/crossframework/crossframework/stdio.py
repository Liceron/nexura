"""POSIX pipe reader for a sandbox that forbids asyncio thread-wakeup socket sends."""

from __future__ import annotations

import asyncio
import os
import sys

from nexura_agents import AgentMCPServer


async def serve(server: AgentMCPServer) -> None:
    """Keep Nexura's stdio framing/dispatch; replace only its thread-based stdin reader."""
    if os.environ.get("NEXURA_CROSSFRAMEWORK_PIPE_READER") != "1":
        await server.serve_stdio()
        return
    reader = asyncio.StreamReader()
    transport, _ = await asyncio.get_running_loop().connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), sys.stdin
    )

    async def read() -> str:
        return (await reader.readline()).decode("utf-8")

    try:
        await server.serve_stdio(reader=read)
    finally:
        transport.close()
