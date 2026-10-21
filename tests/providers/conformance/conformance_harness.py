"""Shared helpers for the provider conformance suite (P37 B3).

Fixtures live in ``tests/providers/fixtures/<provider>/<name>.json``. Each one records the
documentation page it was taken from (``source``), when it was read (``retrieved``) and the
HTTP response to replay: ``{"status", "headers", "json"}`` for a whole body, or
``{"status", "headers", "sse": [{"event"?, "data"}]}`` for a server-sent event stream.
Everything runs through ``httpx.MockTransport``; nothing leaves the machine.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
from nexura_agents.contracts.context import ExecutionContext
from nexura_agents.providers.types import GenerationRequest, StreamDelta, ToolCallAssembler
from nexura_agents.types import ToolCall, Usage
from nexura_agents.usage import BudgetConfig, BudgetManager
from nexura_agents.usage.models import ModelPricing
from nexura_agents.usage.pricing import PricingTable

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

WEATHER_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Current weather for a city.",
        "parameters": {
            "type": "object",
            "properties": {"location": {"type": "string"}},
            "required": ["location"],
        },
    },
}

ORDER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"order_id": {"type": "string"}, "status": {"type": "string"}},
    "required": ["order_id", "status"],
}


def load(provider: str, name: str) -> dict[str, Any]:
    """A fixture document; asserts it names its documentation source and date."""
    doc = json.loads((FIXTURES / provider / f"{name}.json").read_text(encoding="utf-8"))
    assert doc["source"] and doc["retrieved"], f"{provider}/{name} lacks provenance"
    return doc


def to_response(doc: dict[str, Any]) -> httpx.Response:
    spec = doc["response"]
    headers = dict(spec.get("headers") or {})
    if "sse" in spec:
        lines: list[str] = []
        for event in spec["sse"]:
            if "event" in event:
                lines.append(f"event: {event['event']}")
            data = event["data"]
            lines.append(f"data: {data if isinstance(data, str) else json.dumps(data)}")
            lines.append("")
        headers.setdefault("content-type", "text/event-stream")
        return httpx.Response(
            spec["status"], headers=headers, content=("\n".join(lines) + "\n").encode()
        )
    return httpx.Response(spec["status"], headers=headers, json=spec["json"])


class Replay:
    """A mock transport that answers with fixtures in order and records every request."""

    def __init__(self, provider: str, *names: str) -> None:
        self.docs = [load(provider, n) for n in names]
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        index = min(len(self.requests), len(self.docs)) - 1
        return to_response(self.docs[index])

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def body(self, index: int = 0) -> dict[str, Any]:
        return json.loads(self.requests[index].content)


class Streamed:
    """Text, tool calls, finish reasons and the last usage collected from a stream."""

    def __init__(self, deltas: list[StreamDelta]) -> None:
        self.deltas = deltas
        self.text = "".join(d.text or "" for d in deltas if d.kind == "text_delta")
        assembler = ToolCallAssembler()
        for d in deltas:
            assembler.feed(d)
        self.tool_calls: list[ToolCall] = assembler.validate_and_assemble()
        self.finish_reasons = [d.finish_reason for d in deltas if d.finish_reason]
        usages = [d.usage for d in deltas if d.usage is not None]
        self.usage: Usage | None = usages[-1] if usages else None


async def drain(provider: Any, request: GenerationRequest, **kwargs: Any) -> Streamed:
    return Streamed([d async for d in provider.stream(request, **kwargs)])


def priced_context(
    model_id: str,
    *,
    input_per_m: str = "1.00",
    output_per_m: str = "4.00",
    cached_per_m: str | None = "0.10",
    write_per_m: str | None = "1.25",
) -> tuple[ExecutionContext, Any]:
    """An ExecutionContext with a budget whose pricing table knows ``model_id``."""
    table = PricingTable(
        version="b3-conformance",
        date="2026-10-21",
        models={
            model_id: ModelPricing(
                model_id=model_id,
                input_cost_per_million=Decimal(input_per_m),
                output_cost_per_million=Decimal(output_per_m),
                cached_prompt_cost_per_million=Decimal(cached_per_m) if cached_per_m else None,
                cache_write_cost_per_million=Decimal(write_per_m) if write_per_m else None,
            )
        },
    )
    budget = BudgetManager(pricing_table=table).create_run_budget(
        "ws", "run", config=BudgetConfig(max_cost_usd=Decimal("10")), default_model=model_id
    )
    return ExecutionContext(run_id="run", node_id="n", budget=budget), budget


def expected_cost(
    *,
    uncached: int,
    cached: int = 0,
    written: int = 0,
    output: int,
    input_per_m: str = "1.00",
    output_per_m: str = "4.00",
    cached_per_m: str = "0.10",
    write_per_m: str = "1.25",
) -> Decimal:
    per_m = Decimal(1_000_000)
    return (
        Decimal(uncached) * Decimal(input_per_m)
        + Decimal(cached) * Decimal(cached_per_m)
        + Decimal(written) * Decimal(write_per_m)
        + Decimal(output) * Decimal(output_per_m)
    ) / per_m


def spent(budget: Any) -> Decimal:
    return Decimal(budget.get_status().total_spent_usd)


def assert_close(actual: Decimal, expected: Decimal) -> None:
    # The ledger rounds each settlement up to 1e-8 USD.
    assert expected <= actual <= expected + Decimal("0.00000002"), (actual, expected)


ErrorCheck = Callable[[BaseException], bool]
