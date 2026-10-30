# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Source-grounded compatibility with Nexura V2 POST /api/v1/traces.

This exports observability, not workflow execution. Content capture requires an
explicit redactor. Unknown usage remains unknown in attributes even though the
legacy aggregate integer can only contain the known-token subtotal.
"""

from __future__ import annotations

import ipaddress
import json
import math
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, UUID, uuid5

import httpx
from nexura_agents.tracing import redact as _shared_redact

from nexura_runtime.observability.models import SpanRecord

_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_SECRET_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "password",
    "secret",
    "client_secret",
    "access_token",
    "refresh_token",
    "cookie",
    "set_cookie",
    "credentials",
}
_TYPES = {"chain", "llm", "tool", "retriever", "embedding", "parser", "agent"}
_KINDS = {"INTERNAL", "CLIENT", "SERVER", "PRODUCER", "CONSUMER"}
_USAGE = ("gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens")
# Breakdowns of the totals above (cached tokens are part of the input tokens, reasoning tokens
# part of the output tokens): forwarded and validated, but never counted in usage
# completeness or total tokens.
_USAGE_DETAIL = (
    "gen_ai.usage.cache_read.input_tokens",
    "gen_ai.usage.cache_write.input_tokens",
    "gen_ai.usage.reasoning.output_tokens",
)
# gen_ai.provider.name is the current GenAI convention; gen_ai.system is its deprecated alias
# (still read for spans from older instrumentation).
_MODEL_KEYS = {
    "gen_ai.response.model",
    "gen_ai.request.model",
    "gen_ai.provider.name",
    "gen_ai.system",
}
Redactor = Callable[[dict[str, Any]], dict[str, Any]]


class TraceValidationError(ValueError):
    """Invalid/incomplete trace; nothing was dispatched."""


class TraceExportError(RuntimeError):
    """Sanitized transport/status failure; never includes server body or key."""

    def __init__(self, status_code: int | None = None, *, retryable: bool = False) -> None:
        self.status_code = status_code
        self.retryable = retryable
        super().__init__(f"Trace export failed (status={status_code}, retryable={retryable})")


@dataclass(frozen=True)
class TraceMetadata:
    name: str
    service_name: str | None = None
    environment: str | None = None
    deployment: str | None = None
    session_id: str | None = None


@dataclass(frozen=True)
class TraceExportReceipt:
    trace_id: UUID
    attempts: int


def _identifier(value: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise TraceValidationError("Invalid source trace/span identifier")
    return value


def _timestamp(value: float | None) -> str:
    if value is None or not math.isfinite(value) or value < 0:
        raise TraceValidationError("Finished finite nonnegative timestamps required")
    try:
        return datetime.fromtimestamp(value, UTC).isoformat()
    except (OverflowError, OSError, ValueError):
        raise TraceValidationError("Timestamp outside supported range") from None


def _is_secret_key(key: str) -> bool:
    """Secret-named key, by the SDK's shared rule (handles camelCase, kebab-case, suffixes)."""
    if key.lower().replace("-", "_") in _SECRET_KEYS:
        return True
    return bool(_shared_redact({key: None})[key] == "[REDACTED]")


def _redact(value: Any, sensitive_strings: tuple[str, ...]) -> Any:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if _is_secret_key(str(key)) else _redact(item, sensitive_strings)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(item, sensitive_strings) for item in value]
    if isinstance(value, str):
        for secret in sensitive_strings:
            if secret:
                value = value.replace(secret, "[REDACTED]")
    return value


def _attributes(span: SpanRecord) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in span.attributes.items():
        if key in _MODEL_KEYS:
            if not isinstance(value, str):
                raise TraceValidationError("Model telemetry must be text")
            result[key] = value
        elif (
            key in _USAGE
            or key in _USAGE_DETAIL
            or key.startswith("gen_ai.usage.input_token_details.")
            or key.startswith("gen_ai.usage.output_token_details.")
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise TraceValidationError("Reported token telemetry must be nonnegative integers")
            result[key] = value
    result["nexura.source.trace_id"] = span.trace_id
    result["nexura.source.span_id"] = span.span_id
    return result


def build_trace_envelope(
    spans: Sequence[SpanRecord],
    metadata: TraceMetadata,
    *,
    capture_content: bool = False,
    redactor: Redactor | None = None,
    sensitive_strings: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Build stable V2 UUID hierarchy from complete SDK spans without mutation.

    Opt-in content uses attributes ``nexura.input`` and ``nexura.output``.
    Default capture excludes them, arbitrary attributes and exception text.
    Redaction is not a general PII detector; operators define their capture policy.
    """
    if not 1 <= len(spans) <= 1000:
        raise TraceValidationError("V2 traces require 1–1000 spans")
    if capture_content and redactor is None:
        raise TraceValidationError("Content capture requires an explicit redactor")
    for name, value in vars(metadata).items():
        limit = 512 if name == "name" else 255
        if value is not None and (not isinstance(value, str) or not value or len(value) > limit):
            raise TraceValidationError("Invalid trace metadata")
    if not metadata.name:
        raise TraceValidationError("Trace name is required")
    source_trace = _identifier(spans[0].trace_id)
    try:
        trace_id = UUID(source_trace)
    except ValueError:
        trace_id = uuid5(NAMESPACE_URL, "urn:nexura:trace:" + source_trace)
    by_id: dict[str, SpanRecord] = {}
    for span in spans:
        if _identifier(span.trace_id) != source_trace:
            raise TraceValidationError("Mixed source traces are not allowed")
        key = _identifier(span.span_id)
        if key in by_id:
            raise TraceValidationError("Duplicate source span identifier")
        by_id[key] = span
        _timestamp(span.start_time)
        _timestamp(span.end_time)
        if span.end_time is None or span.end_time < span.start_time:
            raise TraceValidationError("Span end precedes start")
        if not span.name or len(span.name) > 512 or span.status not in {"ok", "error", "unset"}:
            raise TraceValidationError("Invalid span name/status")
    for span in spans:
        visited: set[str] = set()
        current: SpanRecord | None = span
        while current is not None:
            if current.span_id in visited:
                raise TraceValidationError("Cyclic span hierarchy")
            visited.add(current.span_id)
            if len(visited) > 64:
                raise TraceValidationError("V2 span hierarchy exceeds depth 64")
            if current.parent_span_id is None:
                break
            parent = by_id.get(_identifier(current.parent_span_id))
            if parent is None:
                raise TraceValidationError("Missing parent span")
            current = parent
    ids = {key: str(uuid5(trace_id, "span:" + key)) for key in by_id}
    parents = {s.parent_span_id for s in spans if s.parent_span_id is not None}
    unknown = 0
    total_tokens = 0
    wire_spans: list[dict[str, Any]] = []
    for span in sorted(spans, key=lambda item: (item.start_time, item.span_id)):
        attributes = _attributes(span)
        inferred_model = any(key in attributes for key in _USAGE) or any(
            key in attributes for key in {"gen_ai.response.model", "gen_ai.request.model"}
        )
        span_type = span.attributes.get("nexura.span.type", "llm" if inferred_model else "chain")
        if not isinstance(span_type, str) or span_type not in _TYPES:
            raise TraceValidationError("Invalid V2 span type")
        span_kind = span.attributes.get(
            "nexura.span.kind", "CLIENT" if span_type in {"llm", "embedding"} else "INTERNAL"
        )
        if not isinstance(span_kind, str) or span_kind not in _KINDS:
            raise TraceValidationError("Invalid V2 span kind")
        if span.span_id not in parents and (inferred_model or span_type in {"llm", "embedding"}):
            complete = all(key in attributes for key in _USAGE)
            unknown += int(not complete)
            total_tokens += sum(attributes.get(key, 0) for key in _USAGE)
            attributes["nexura.usage.complete"] = complete
        inputs = outputs = None
        error = "Span execution failed" if span.error is not None else None
        if capture_content:
            assert redactor is not None
            try:
                content = redactor(
                    {
                        "inputs": span.attributes.get("nexura.input"),
                        "outputs": span.attributes.get("nexura.output"),
                        "error": span.error,
                    }
                )
            except Exception:
                raise TraceValidationError("Content redactor failed") from None
            if not isinstance(content, dict):
                raise TraceValidationError("Redactor must return a content dictionary")
            inputs = content.get("inputs")
            outputs = content.get("outputs")
            error = content.get("error")
            if inputs is not None and not isinstance(inputs, dict):
                inputs = {"value": inputs}
            if outputs is not None and not isinstance(outputs, dict):
                outputs = {"value": outputs}
            if error is not None and not isinstance(error, str):
                raise TraceValidationError("Redacted error must be text or None")
        end = span.end_time
        assert end is not None
        wire_spans.append(
            {
                "id": ids[span.span_id],
                "parent_span_id": ids[span.parent_span_id] if span.parent_span_id else None,
                "name": span.name,
                "span_type": span_type,
                "span_kind": span_kind,
                "status": span.status,
                "start_time": _timestamp(span.start_time),
                "end_time": _timestamp(end),
                "latency_ms": int((end - span.start_time) * 1000),
                "inputs": inputs,
                "outputs": outputs,
                "error": error,
                "attributes": attributes,
            }
        )
    start = min(s.start_time for s in spans)
    finish = max(s.end_time for s in spans if s.end_time is not None)
    payload = {
        "id": str(trace_id),
        **vars(metadata),
        "status": "error" if any(s.status == "error" for s in spans) else "ok",
        "start_time": _timestamp(start),
        "end_time": _timestamp(finish),
        "total_latency_ms": int((finish - start) * 1000),
        "total_tokens": total_tokens,
        "spans": wire_spans,
    }
    for row in wire_spans:
        row["attributes"]["nexura.trace.usage_complete"] = unknown == 0
        row["attributes"]["nexura.trace.unknown_usage_leaves"] = unknown
    try:
        result: dict[str, Any] = _redact(payload, sensitive_strings)
        json.dumps(result, allow_nan=False)
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise TraceValidationError("Trace content is not finite JSON data") from None
    return result


class NexuraV2TraceExporter:
    """Synchronous, bounded exporter. Export finished traces outside async hot paths."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        client: httpx.Client | None = None,
        timeout: float = 10,
        max_attempts: int = 3,
        max_backoff_seconds: float = 2,
        capture_content: bool = False,
        redactor: Redactor | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        url = urlsplit(base_url)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or url.path not in {"", "/"}
        ):
            raise TraceValidationError(
                "Platform base URL must be an HTTP(S) origin without secrets"
            )
        if url.scheme == "http":
            try:
                local = ipaddress.ip_address(url.hostname).is_loopback
            except ValueError:
                local = url.hostname == "localhost"
            if not local:
                raise TraceValidationError("Remote platform transport requires HTTPS")
        if not api_key or any(char in api_key for char in "\r\n"):
            raise TraceValidationError("A nonempty API key without newlines is required")
        if not math.isfinite(timeout) or not 0 < timeout <= 300:
            raise TraceValidationError("Timeout must be finite and within 0–300 seconds")
        if (
            isinstance(max_attempts, bool)
            or not isinstance(max_attempts, int)
            or not 1 <= max_attempts <= 5
        ):
            raise TraceValidationError("Attempts must be within 1–5")
        if not math.isfinite(max_backoff_seconds) or not 0 <= max_backoff_seconds <= 5:
            raise TraceValidationError("Backoff must be finite and within 0–5 seconds")
        if capture_content and redactor is None:
            raise TraceValidationError("Content capture requires an explicit redactor")
        self._url = base_url.rstrip("/") + "/api/v1/traces"
        self._api_key = api_key
        self._client = client if client is not None else httpx.Client(follow_redirects=False)
        self._owned = client is None
        self._closed = False
        self._timeout = timeout
        self._attempts = max_attempts
        self._backoff = max_backoff_seconds
        self._capture = capture_content
        self._redactor = redactor
        self._sleep = sleeper

    def export(self, spans: Sequence[SpanRecord], metadata: TraceMetadata) -> TraceExportReceipt:
        """Send finished spans to Nexura V2's trace ingestion."""
        if self._closed:
            raise TraceExportError()
        payload = build_trace_envelope(
            spans,
            metadata,
            capture_content=self._capture,
            redactor=self._redactor,
            sensitive_strings=(self._api_key,),
        )
        encoded = json.dumps(payload, allow_nan=False, separators=(",", ":")).encode()
        if len(encoded) > 4 * 1024 * 1024:
            raise TraceValidationError("V2 trace body exceeds 4 MiB")
        for attempt in range(1, self._attempts + 1):
            response: httpx.Response | None = None
            try:
                response = self._client.post(
                    self._url,
                    content=encoded,
                    headers={"x-api-key": self._api_key, "content-type": "application/json"},
                    timeout=self._timeout,
                    follow_redirects=False,
                )
            except httpx.TransportError:
                pass
            else:
                if response.status_code in {200, 201}:
                    try:
                        received = UUID(response.json()["id"])
                        if str(received) != payload["id"]:
                            raise ValueError
                    except (ValueError, KeyError, TypeError, AttributeError):
                        raise TraceExportError(response.status_code) from None
                    return TraceExportReceipt(received, attempt)
                if response.status_code not in {408, 429, 500, 502, 503, 504}:
                    raise TraceExportError(response.status_code)
            if attempt == self._attempts:
                raise TraceExportError(response.status_code if response else None, retryable=True)
            delay = min(self._backoff, 0.25 * 2 ** (attempt - 1))
            if response is not None:
                try:
                    retry_after = float(response.headers.get("retry-after", ""))
                    if math.isfinite(retry_after) and retry_after >= 0:
                        delay = min(self._backoff, retry_after)
                except ValueError:
                    pass
            self._sleep(delay)
        raise AssertionError("Validated retry count must produce a result")

    def close(self) -> None:
        """Flush and close the exporter."""
        if not self._closed:
            self._closed = True
            if self._owned:
                self._client.close()

    def __enter__(self) -> NexuraV2TraceExporter:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
