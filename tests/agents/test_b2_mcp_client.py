"""Regression tests for MCP client transport robustness (all offline)."""

import asyncio
import json
import sys
import textwrap

import httpx
import pytest
from nexura_agents.mcp.client import McpClientSession
from nexura_agents.mcp.errors import McpTransportError
from nexura_agents.mcp.transports import McpHttpTransport, McpStdioTransport

ALLOWED = {"python", "python3", sys.executable.rsplit("/", 1)[-1]}

LARGE_REPLY_SERVER = textwrap.dedent(
    """
    import sys, json
    def send(m): sys.stdout.write(json.dumps(m) + "\\n"); sys.stdout.flush()
    for line in sys.stdin:
        m = json.loads(line)
        if m.get("method") == "tools/call":
            send({"jsonrpc": "2.0", "id": m["id"],
                  "result": {"content": [{"type": "text", "text": "x" * 200000}]}})
    """
)

CHATTY_STDERR_SERVER = textwrap.dedent(
    """
    import sys, json
    for line in sys.stdin:
        m = json.loads(line)
        if m.get("method") == "initialize":
            sys.stderr.write("log line\\n" * 200000 + "final-words\\n"); sys.stderr.flush()
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": {
                "protocolVersion": m["params"]["protocolVersion"], "capabilities": {}}}) + "\\n")
            sys.stdout.flush()
            sys.exit(0)
    """
)


def stdio(script: str, max_line_bytes: int | None = None) -> McpStdioTransport:
    limit = max_line_bytes or McpStdioTransport.DEFAULT_MAX_LINE_BYTES
    return McpStdioTransport(
        [sys.executable, "-c", script], allowed_commands=ALLOWED, max_line_bytes=limit
    )


async def test_stdio_accepts_json_line_over_asyncio_default_limit() -> None:
    session = McpClientSession(stdio(LARGE_REPLY_SERVER))
    try:
        result = await asyncio.wait_for(session.call_tool("t", {}), 20)
        assert len(result.content[0].text or "") == 200000
    finally:
        await session.close()


async def test_stdio_line_over_configured_limit_is_a_transport_error() -> None:
    transport = stdio(LARGE_REPLY_SERVER, max_line_bytes=1024)
    session = McpClientSession(transport)
    try:
        with pytest.raises(McpTransportError, match="exceeded"):
            await asyncio.wait_for(session.call_tool("t", {}), 20)
        proc = transport._process
    finally:
        await session.close()
    # The unread stdout pipe never reaches EOF, so close() must close the subprocess
    # transport itself; left to its finalizer it fails with "Event loop is closed".
    assert proc is not None and proc._transport.is_closing()  # type: ignore[attr-defined]


async def test_chatty_stderr_does_not_deadlock_and_is_kept_bounded() -> None:
    transport = stdio(CHATTY_STDERR_SERVER)
    session = McpClientSession(transport)
    try:
        await asyncio.wait_for(session.initialize(), 15)
        await asyncio.sleep(0.5)  # let the drain task consume the rest
        assert len(transport._stderr_tail) <= McpStdioTransport.STDERR_TAIL_BYTES
        assert b"final-words" in transport._stderr_tail
    finally:
        await session.close()
    assert transport._stderr_task is None


async def test_request_without_reply_times_out_with_transport_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=b": keepalive\n\n"
        )

    transport = McpHttpTransport("http://127.0.0.1:9/mcp", transport=httpx.MockTransport(handler))
    session = McpClientSession(transport, request_timeout=0.2)
    try:
        with pytest.raises(McpTransportError, match="Timed out"):
            await asyncio.wait_for(session.initialize(), 5)
        # The lock was released, so the session is still usable for another attempt.
        with pytest.raises(McpTransportError, match="Timed out"):
            await asyncio.wait_for(session.list_tools(), 5)
    finally:
        await transport.close()


async def test_sse_unicode_line_separators_inside_json_are_preserved() -> None:
    text = "a b c\u0085d"
    body = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": text}]}},
        ensure_ascii=False,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream; charset=utf-8"},
            content=f"data: {body}\r\n\r\n".encode(),
        )

    transport = McpHttpTransport("http://127.0.0.1:9/mcp", transport=httpx.MockTransport(handler))
    try:
        await transport.send_message({"jsonrpc": "2.0", "id": 1, "method": "x"})
        message = await transport.receive_message(timeout=1)
        assert message["result"]["content"][0]["text"] == text
    finally:
        await transport.close()
