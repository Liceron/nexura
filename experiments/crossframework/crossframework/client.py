"""External agent stand-in: stdlib + official mcp SDK only, with a scripted fake model."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from mcp import Client, StdioServerParameters
from mcp.types import TextContent


@asynccontextmanager
async def connect(
    state_dir: Path,
    *,
    approval_ttl: float = 900,
    mode: Literal["auto", "legacy"] = "auto",
    server_module: str = "crossframework.server",
    extra_args: tuple[str, ...] = (),
) -> AsyncIterator[Client]:
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            server_module,
            "--state-dir",
            str(state_dir.resolve()),
            "--approval-ttl",
            str(approval_ttl),
            *extra_args,
        ],
        cwd=str(Path(__file__).resolve().parents[1]),
        env={
            "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
            "PYTHONDONTWRITEBYTECODE": "1",
            "NEXURA_CROSSFRAMEWORK_PIPE_READER": os.environ.get(
                "NEXURA_CROSSFRAMEWORK_PIPE_READER", ""
            ),
        },
    )
    async with Client(parameters, mode=mode, read_timeout_seconds=15.0) as session:
        yield session


async def call_refund(session: Client, arguments: dict[str, Any]) -> dict[str, Any]:
    result = await session.call_tool("refund", arguments)
    # Stock AgentMCPServer serializes objects into JSON text, not structuredContent.
    if len(result.content) != 1 or not isinstance(result.content[0], TextContent):
        raise ValueError("Expected one JSON text result from the governed tool")
    text = result.content[0].text
    if result.is_error:
        first_line, text = text.split("\n", 1)
        if not first_line.startswith("NOT EXECUTED:"):
            raise ValueError("Expected explicit non-execution message")
    envelope = json.loads(text)
    if not isinstance(envelope, dict) or not isinstance(envelope.get("status"), str):
        raise ValueError("Expected a governance result with a status")
    if envelope.get("executed") is not (not result.is_error):
        raise ValueError("MCP error flag disagrees with execution outcome")
    return envelope


class ScriptedFakeModel:
    """Deterministic tool proposals; no LLM or framework runtime is called."""

    def proposals(self) -> Iterator[dict[str, Any]]:
        for amount in (50, 200, 900):
            yield {"request_id": f"demo-{amount}", "order_id": "ORDER-1", "amount": amount}


async def run_agent(
    session: Client,
    review: Callable[[dict[str, Any]], Awaitable[None]],
) -> list[dict[str, Any]]:
    tools = await session.list_tools()
    assert [tool.name for tool in tools.tools] == ["refund"]
    transcript = []
    for arguments in ScriptedFakeModel().proposals():
        outcome = await call_refund(session, arguments)
        transcript.append({"amount": arguments["amount"], **outcome})
        if outcome["status"] == "pending_approval":
            # The application retains the exact arguments. Never ask the model to rebuild them.
            await review(outcome)
            resumed = await call_refund(
                session, {**arguments, "approval_id": outcome["approval_id"]}
            )
            transcript.append({"amount": arguments["amount"], **resumed})
    return transcript
