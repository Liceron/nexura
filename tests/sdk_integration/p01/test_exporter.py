"""Local V2 wire-contract fixtures; never import or call the V2 application."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, Literal
from uuid import UUID

import httpx
import pytest
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from nexura_runtime.integrations.nexura_v2 import (
    NexuraV2TraceExporter,
    TraceExportError,
    TraceMetadata,
    TraceValidationError,
    build_trace_envelope,
)
from nexura_runtime.observability.models import SpanRecord
from nexura_runtime.observability.tracer import NexuraTracer


class WireSpan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    parent_span_id: UUID | None
    name: str = Field(min_length=1, max_length=512)
    span_type: Literal["chain", "llm", "tool", "retriever", "embedding", "parser", "agent"]
    span_kind: Literal["INTERNAL", "CLIENT", "SERVER", "PRODUCER", "CONSUMER"]
    status: Literal["unset", "ok", "error"]
    start_time: AwareDatetime
    end_time: AwareDatetime
    latency_ms: int = Field(ge=0)
    inputs: dict[str, Any] | None
    outputs: dict[str, Any] | None
    error: str | None
    attributes: dict[str, Any]


class WireEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    name: str = Field(min_length=1, max_length=512)
    service_name: str | None
    environment: str | None
    deployment: str | None
    session_id: str | None
    status: Literal["unset", "ok", "error"]
    start_time: AwareDatetime
    end_time: AwareDatetime
    total_latency_ms: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    spans: list[WireSpan] = Field(min_length=1, max_length=1000)


def make_spans() -> list[SpanRecord]:
    tracer = NexuraTracer()
    with tracer.span("Local fake support run"):
        with tracer.span(
            "Local fake model",
            attributes={
                "gen_ai.response.model": "test-only-local-fake",
                "gen_ai.usage.input_tokens": 40,
                "gen_ai.usage.output_tokens": 5,
                "gen_ai.usage.input_token_details.cache_read": 10,
                "nexura.input": {"text": "नमस्ते — order ORD-501", "api_key": "sentinel"},
            },
        ):
            pass
        with tracer.span(
            "Unknown provider usage",
            attributes={
                "gen_ai.response.model": "test-only-unknown",
            },
        ):
            pass
    return tracer.get_spans()


def accepted(request: httpx.Request) -> httpx.Response:
    wire = WireEnvelope.model_validate_json(request.content)
    return httpx.Response(201, json={"id": str(wire.id)})


def test_real_tracer_to_mock_v2_schema_stable_ids_and_missing_usage() -> None:
    spans = make_spans()
    original = [span.model_dump() for span in spans]
    payloads: list[dict[str, Any]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/traces"
        assert request.headers["x-api-key"] == "test-key"
        payloads.append(json.loads(request.content))
        return accepted(request)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        with NexuraV2TraceExporter("https://v2.example", "test-key", client=client) as exporter:
            first = exporter.export(spans, TraceMetadata("Example", session_id="sdk-p01"))
            second = exporter.export(
                list(reversed(spans)), TraceMetadata("Example", session_id="sdk-p01")
            )
        assert not client.is_closed
    assert first.trace_id == second.trace_id
    assert payloads[0] == payloads[1]
    payload = payloads[0]
    assert payload["total_tokens"] == 45
    root = next(row for row in payload["spans"] if row["parent_span_id"] is None)
    children = [row for row in payload["spans"] if row["parent_span_id"]]
    assert all(row["parent_span_id"] == root["id"] for row in children)
    unknown = next(row for row in children if row["name"] == "Unknown provider usage")
    assert "gen_ai.usage.input_tokens" not in unknown["attributes"]
    assert unknown["attributes"]["nexura.trace.unknown_usage_leaves"] == 1
    assert unknown["attributes"]["nexura.trace.usage_complete"] is False
    assert all(row["inputs"] is None and row["outputs"] is None for row in payload["spans"])
    assert "sentinel" not in json.dumps(payload)
    assert [span.model_dump() for span in spans] == original


def test_non_model_tool_leaf_is_not_missing_model_usage() -> None:
    span = SpanRecord(
        trace_id="one",
        span_id="tool",
        name="Tool",
        start_time=1,
        end_time=2,
        status="ok",
        attributes={"nexura.span.type": "tool"},
    )
    row = build_trace_envelope([span], TraceMetadata("Run"))["spans"][0]
    assert row["attributes"]["nexura.trace.unknown_usage_leaves"] == 0


def test_capture_requires_policy_and_redacts_nested_secrets_and_api_key() -> None:
    spans = make_spans()
    spans[0].attributes["nexura.output"] = {
        "nested": {"password": "do-not-export"},
        "message": "opaque-export-key",
    }
    with pytest.raises(TraceValidationError, match="redactor"):
        build_trace_envelope(spans, TraceMetadata("Run"), capture_content=True)
    payloads: list[bytes] = []

    def handle(request: httpx.Request) -> httpx.Response:
        payloads.append(request.content)
        return accepted(request)

    with (
        httpx.Client(transport=httpx.MockTransport(handle)) as client,
        NexuraV2TraceExporter(
            "https://v2.example",
            "opaque-export-key",
            client=client,
            capture_content=True,
            redactor=lambda content: content,
        ) as exporter,
    ):
        exporter.export(spans, TraceMetadata("Run"))
    assert b"opaque-export-key" not in payloads[0]
    assert b"do-not-export" not in payloads[0] and b"sentinel" not in payloads[0]
    assert "नमस्ते" in json.dumps(json.loads(payloads[0]), ensure_ascii=False)


@pytest.mark.parametrize(
    "change",
    [
        lambda spans: setattr(spans[0], "end_time", None),
        lambda spans: setattr(spans[0], "end_time", -1),
        lambda spans: setattr(spans[0], "start_time", float("nan")),
        lambda spans: setattr(spans[0], "parent_span_id", "absent"),
        lambda spans: setattr(spans[0], "parent_span_id", spans[0].span_id),
        lambda spans: setattr(spans[0], "trace_id", "foreign"),
        lambda spans: setattr(spans[0], "span_id", "bad id"),
        lambda spans: spans.append(spans[0]),
    ],
)
def test_invalid_trace_fails_before_transport(change: Callable[[list[SpanRecord]], Any]) -> None:
    spans = make_spans()
    change(spans)
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return accepted(request)

    with (
        httpx.Client(transport=httpx.MockTransport(handle)) as client,
        NexuraV2TraceExporter("https://v2.example", "key", client=client) as exporter,
        pytest.raises(TraceValidationError),
    ):
        exporter.export(spans, TraceMetadata("Run"))
    assert not requests


def test_retry_payload_identity_cap_and_auth_failures() -> None:
    payloads: list[bytes] = []
    sleeps: list[float] = []

    def handle(request: httpx.Request) -> httpx.Response:
        payloads.append(request.content)
        if len(payloads) == 1:
            raise httpx.ReadError("secret-key", request=request)
        if len(payloads) == 2:
            return httpx.Response(429, headers={"retry-after": "999999"})
        return accepted(request)

    with (
        httpx.Client(transport=httpx.MockTransport(handle)) as client,
        NexuraV2TraceExporter(
            "https://v2.example",
            "secret-key",
            client=client,
            sleeper=sleeps.append,
            max_backoff_seconds=0.5,
        ) as exporter,
    ):
        receipt = exporter.export(make_spans(), TraceMetadata("Run"))
    assert receipt.attempts == 3 and len(set(payloads)) == 1
    assert sleeps == [0.25, 0.5]


@pytest.mark.parametrize("status", [301, 401, 403, 422])
def test_nonretryable_status_does_not_leak_body_or_follow_redirect(status: int) -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            status, json={"error": "private-key"}, headers={"location": "https://foreign.example"}
        )

    with (
        httpx.Client(transport=httpx.MockTransport(handle), follow_redirects=True) as client,
        NexuraV2TraceExporter("https://v2.example", "private-key", client=client) as exporter,
        pytest.raises(TraceExportError) as err,
    ):
        exporter.export(make_spans(), TraceMetadata("Run"))
    assert len(requests) == 1
    assert err.value.status_code == status and not err.value.retryable
    assert "private-key" not in str(err.value)


def test_transient_retries_are_bounded() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(503)

    with (
        httpx.Client(transport=httpx.MockTransport(handle)) as client,
        NexuraV2TraceExporter(
            "https://v2.example", "key", client=client, max_attempts=2, sleeper=lambda _: None
        ) as exporter,
        pytest.raises(TraceExportError) as err,
    ):
        exporter.export(make_spans(), TraceMetadata("Run"))
    assert len(requests) == 2 and err.value.retryable and err.value.status_code == 503


def test_invalid_usage_kind_json_and_redactor_are_rejected() -> None:
    spans = make_spans()
    spans[0].attributes["gen_ai.usage.input_tokens"] = True
    with pytest.raises(TraceValidationError):
        build_trace_envelope(spans, TraceMetadata("Run"))
    spans[0].attributes.pop("gen_ai.usage.input_tokens")
    spans[0].attributes["nexura.span.type"] = ["llm"]
    with pytest.raises(TraceValidationError):
        build_trace_envelope(spans, TraceMetadata("Run"))
    spans[0].attributes.pop("nexura.span.type")
    spans[0].attributes["nexura.output"] = {"not_finite": float("nan")}
    with pytest.raises(TraceValidationError, match="JSON"):
        build_trace_envelope(
            spans, TraceMetadata("Run"), capture_content=True, redactor=lambda content: content
        )

    def failing(_: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("secret")

    with pytest.raises(TraceValidationError, match="redactor failed") as err:
        build_trace_envelope(spans, TraceMetadata("Run"), capture_content=True, redactor=failing)
    assert "secret" not in str(err.value)


def test_legacy_depth_and_payload_limits() -> None:
    chain = [
        SpanRecord(
            trace_id="deep",
            span_id=f"s{i}",
            parent_span_id=f"s{i - 1}" if i else None,
            name="Deep",
            start_time=1,
            end_time=2,
            status="ok",
        )
        for i in range(65)
    ]
    with pytest.raises(TraceValidationError, match="depth"):
        build_trace_envelope(chain, TraceMetadata("Run"))
    span = SpanRecord(
        trace_id="large",
        name="Large",
        start_time=1,
        end_time=2,
        attributes={"nexura.output": {"text": "x" * (4 * 1024 * 1024)}},
    )
    with (
        httpx.Client(transport=httpx.MockTransport(accepted)) as client,
        NexuraV2TraceExporter(
            "https://v2.example",
            "key",
            client=client,
            capture_content=True,
            redactor=lambda content: content,
        ) as exporter,
        pytest.raises(TraceValidationError, match="4 MiB"),
    ):
        exporter.export([span], TraceMetadata("Run"))


def test_metadata_url_controls_and_client_lifecycle() -> None:
    for url in ["http://remote.example", "https://user:key@v2.example", "https://v2.example?a=key"]:
        with pytest.raises(TraceValidationError):
            NexuraV2TraceExporter(url, "key")
    for options in [
        {"max_attempts": 0},
        {"max_attempts": True},
        {"timeout": float("inf")},
        {"max_backoff_seconds": -1},
    ]:
        with pytest.raises(TraceValidationError):
            NexuraV2TraceExporter("https://v2.example", "key", **options)  # type: ignore[arg-type]
    with pytest.raises(TraceValidationError):
        build_trace_envelope(make_spans(), TraceMetadata("x" * 513))
    exporter = NexuraV2TraceExporter("http://127.0.0.1:8000", "key")
    exporter.close()
    exporter.close()
    with pytest.raises(TraceExportError):
        exporter.export(make_spans(), TraceMetadata("Run"))
    assert exporter._client.is_closed


def test_acknowledgement_must_match_trace() -> None:
    with (
        httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(201, json={"id": "00000000-0000-0000-0000-000000000000"})
            )
        ) as client,
        NexuraV2TraceExporter("https://v2.example", "key", client=client) as exporter,
        pytest.raises(TraceExportError),
    ):
        exporter.export(make_spans(), TraceMetadata("Run"))
