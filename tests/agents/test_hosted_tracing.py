# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Hosted tracing uses mock HTTP only; no paid models or hosted calls."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
from nexura_agents import Agent, FakeModel, Runner, tracing
from nexura_agents._hosted_tracing import HostedTraceProcessor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider


def provider(processor: HostedTraceProcessor) -> Any:
    value = TracerProvider(
        resource=Resource.create({"service.name": "mock-sdk"}), shutdown_on_exit=False
    )
    value.add_span_processor(processor)  # type: ignore[arg-type]
    return value


def test_batches_retry_partial_success_and_nonblocking_queue() -> None:
    calls = []

    def send(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"partialSuccess": {"rejectedSpans": "1"}})

    processor = HostedTraceProcessor(
        "https://mock.invalid",
        "mock-key",
        batch_size=128,
        interval=60,
        transport=httpx.MockTransport(send),
    )
    tracer = provider(processor).get_tracer("test")
    try:
        for _ in range(3):
            with tracer.start_as_current_span("example"):
                pass
        assert not calls
        assert processor.force_flush()
        assert len(calls) == 2 and processor.retries == 1
        assert processor.exported_spans == 2 and processor.rejected_spans == 1
        spans = [
            s for r in calls[-1]["resourceSpans"] for sc in r["scopeSpans"] for s in sc["spans"]
        ]
        assert len(spans) == 3 and spans[0]["kind"] == 1
        assert len(spans[0]["traceId"]) == 32 and len(spans[0]["spanId"]) == 16
    finally:
        processor.shutdown()
    full = HostedTraceProcessor(
        "https://mock.invalid",
        "key",
        queue_size=1,
        interval=60,
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})),
    )
    tracer = provider(full).get_tracer("test")
    try:
        for _ in range(3):
            with tracer.start_as_current_span("bounded"):
                pass
        assert full.dropped_spans == 2
        assert full.force_flush()
        assert full.exported_spans == 1
    finally:
        full.shutdown()


def test_connect_and_instrument_fake_model(monkeypatch: pytest.MonkeyPatch) -> None:
    import nexura_agents._hosted_tracing as hosted

    sent = []

    def transport(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/traces"
        assert request.headers["x-api-key"] == "mock-key"
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={})

    def make(*args: Any, **kwargs: Any) -> HostedTraceProcessor:
        return HostedTraceProcessor(*args, **kwargs, transport=httpx.MockTransport(transport))

    monkeypatch.setattr(hosted, "HostedTraceProcessor", make)
    monkeypatch.setattr(tracing, "_CONNECTED_PROVIDER", None)
    monkeypatch.setattr(tracing, "_CONNECTION", None)
    processor = tracing.connect("https://mock.invalid", "mock-key", service_name="fake-model-test")
    try:
        tracing.instrument()  # reuses connect's provider, still content off
        import asyncio

        asyncio.run(
            Runner().run_async(
                Agent(
                    name="test",
                    instructions="private instructions",
                    model=FakeModel(responses=["private answer"]),
                ),
                "private prompt",
            )
        )
        assert processor.force_flush()
        body = json.dumps(sent)
        assert "invoke_agent" in body and "chat" in body
        assert "private instructions" not in body and "private prompt" not in body
        assert "private answer" not in body and "fake-model-test" in body
    finally:
        tracing.uninstrument()
        processor.shutdown()


def test_export_failure_never_fails_user_run() -> None:
    processor = HostedTraceProcessor(
        "https://mock.invalid",
        "key",
        interval=60,
        transport=httpx.MockTransport(lambda r: httpx.Response(400)),
    )
    try:
        with provider(processor).get_tracer("test").start_as_current_span("works"):
            pass
        assert processor.force_flush()
        assert processor.dropped_spans == 1
    finally:
        processor.shutdown()


def test_atexit_flushes_pending_spans(tmp_path: Path) -> None:
    target = tmp_path / "exit.json"
    script = """
import httpx, pathlib, sys
from nexura_agents._hosted_tracing import HostedTraceProcessor
from opentelemetry.sdk.trace import TracerProvider
def send(request):
    pathlib.Path(sys.argv[1]).write_bytes(request.content)
    return httpx.Response(200, json={})
p = HostedTraceProcessor("https://mock.invalid", "key", interval=60,
    transport=httpx.MockTransport(send))
provider = TracerProvider(shutdown_on_exit=False)
provider.add_span_processor(p)
with provider.get_tracer("test").start_as_current_span("exit-flush"):
    pass
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(target)],
        timeout=20,
        capture_output=True,
        text=True,
        env=os.environ.copy(),
    )
    assert result.returncode == 0, result.stderr
    assert "exit-flush" in target.read_text()
