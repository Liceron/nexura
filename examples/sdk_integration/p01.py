"""Keyless SDK execution -> local mock V2 trace ingestion (no network)."""

from __future__ import annotations

import json
from uuid import UUID

import httpx

from nexura_runtime import Agent, FakeModel, Runner
from nexura_runtime.integrations.nexura_v2 import NexuraV2TraceExporter, TraceMetadata
from nexura_runtime.observability.tracer import NexuraTracer


def main() -> None:
    captured: list[dict[str, object]] = []

    def local_mock_v2(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/traces"
        envelope = json.loads(request.content)
        UUID(envelope["id"])
        captured.append(envelope)
        return httpx.Response(201, json={"id": envelope["id"]})

    tracer = NexuraTracer()
    agent = Agent(
        name="Local fake support agent",
        instructions="Preserve the order identifier and Hindi/English text.",
        model=FakeModel(responses=["नमस्ते! Order ORD-501 is ready for review."]),
    )
    with (
        tracer.span("SDK support fixture"),
        tracer.span("Local fake model", attributes={"gen_ai.response.model": "local-fake"}),
    ):
        result = Runner().run(agent, "नमस्ते — check ORD-501")
    with (
        httpx.Client(transport=httpx.MockTransport(local_mock_v2)) as client,
        NexuraV2TraceExporter("https://v2.invalid", "test-only-key", client=client) as exporter,
    ):
        receipt = exporter.export(tracer.get_spans(), TraceMetadata("P01 SDK fixture"))
    print(result.final_output)
    print(f"Local mock ingestion accepted {len(captured)} trace; ID={receipt.trace_id}")
    print("Usage is unknown for this fake model; no provider cost or cache hit was invented.")


if __name__ == "__main__":
    main()
