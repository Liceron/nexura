# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Opt-in OpenTelemetry tracing for agents, teams and workflows.

Call :func:`instrument` once at start-up and every ``Runner`` run, model call, tool call,
team run and workflow run emits spans that follow the OpenTelemetry GenAI semantic
conventions (``gen_ai.*`` attributes). Nothing is imported or recorded until then.

Conventions implemented: the OpenTelemetry GenAI semantic conventions (Development status)
at ``open-telemetry/semantic-conventions-genai`` commit
``8538971bd11dba723fdd33c804b5a32121827ff7`` (2026-10-21; schema ``gen-ai-dev/1.42.0-dev``,
built on semantic-conventions v1.44.0). See :data:`SEMCONV_GENAI_COMMIT`.

- ``gen_ai.operation.name`` is ``invoke_agent``, ``chat``, ``execute_tool`` or
  ``invoke_workflow`` (well-known values). Teams use the custom value ``invoke_team``.
  Span names are ``{operation} {name}``; ``chat`` spans are ``CLIENT``, the others
  ``INTERNAL``.
- ``gen_ai.provider.name``, ``gen_ai.agent.name``, ``gen_ai.request.model``,
  ``gen_ai.response.id``, ``gen_ai.response.model``, ``gen_ai.response.finish_reasons``,
  ``gen_ai.usage.*`` (input, output, cache read/write, reasoning), ``gen_ai.tool.name``,
  ``gen_ai.tool.call.id``, ``gen_ai.workflow.name`` and ``error.type``.
- Opt-in content (``capture_content=True``): ``gen_ai.system_instructions``,
  ``gen_ai.input.messages``, ``gen_ai.output.messages``, ``gen_ai.tool.call.arguments`` and
  ``gen_ai.tool.call.result`` as JSON-string span attributes in the conventions' structured
  format.
- Metrics: ``gen_ai.client.inference.duration``, ``gen_ai.invoke_agent.duration``,
  ``gen_ai.execute_tool.duration``, ``gen_ai.invoke_workflow.duration``, the
  ``gen_ai.client.inference.usage.*`` token counters and the
  ``gen_ai.client.inference.operation.*_tokens`` histograms, with low-cardinality attributes
  only (no ids or content).
- Nexura events: ``nexura.approval.*``, ``nexura.budget.*``, ``nexura.effect.*`` and
  ``nexura.guardrail.decision`` (see :func:`record_event`).

Deprecated aliases kept for one release (removed in 0.5.0): ``gen_ai.system`` (same value as
``gen_ai.provider.name``), ``nexura.usage.reasoning_tokens``, ``nexura.operation.name``
(the old ``workflow`` operation value), ``nexura.response.finish_reasons`` (the raw
``tool_calls`` value), the ``gen_ai.content.prompt`` / ``gen_ai.content.completion`` and
``gen_ai.tool.call.arguments`` / ``gen_ai.tool.call.result`` span events, the
``gen_ai.client.token.usage`` histogram, and ``gen_ai.client.operation.duration`` points for
model calls. ``gen_ai.client.operation.duration`` itself is a current convention metric for
non-inference operations; it is recorded with the same attributes as before 0.4.0
(``gen_ai.operation.name=workflow`` for workflows) until 0.5.0.

The ``opentelemetry-api`` package is imported lazily inside :func:`instrument`; this module
never sets the global ``TracerProvider`` or ``MeterProvider``.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import math
import re
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

__all__ = ["connect", "instrument", "uninstrument", "is_instrumented", "span", "trace"]

# Longest content value recorded on a span event, in characters. Structured message
# attributes truncate each text value to this length and keep their JSON structure.
MAX_CONTENT_CHARS = 4000
# Largest JSON value of one message attribute; the oldest input messages are dropped first.
MAX_MESSAGES_CHARS = 64000
_INSTRUMENTATION_NAME = "nexura_agents"

# The GenAI semantic conventions this module implements (Development status, pinned).
SEMCONV_GENAI_REPOSITORY = "https://github.com/open-telemetry/semantic-conventions-genai"
SEMCONV_GENAI_COMMIT = "8538971bd11dba723fdd33c804b5a32121827ff7"
SEMCONV_GENAI_SCHEMA_URL = "https://opentelemetry.io/schemas/gen-ai-dev/1.42.0-dev"
# Release in which the deprecated aliases below are removed.
ALIASES_REMOVED_IN = "0.5.0"

# GenAI semantic-convention attribute and event names.
OPERATION_NAME = "gen_ai.operation.name"
PROVIDER_NAME = "gen_ai.provider.name"
AGENT_NAME = "gen_ai.agent.name"
REQUEST_MODEL = "gen_ai.request.model"
RESPONSE_ID = "gen_ai.response.id"
RESPONSE_MODEL = "gen_ai.response.model"
FINISH_REASONS = "gen_ai.response.finish_reasons"
# The provider-reported value when it differs from the conventions' (``tool_calls``).
RAW_FINISH_REASONS = "nexura.response.finish_reasons"
USAGE_INPUT = "gen_ai.usage.input_tokens"
USAGE_OUTPUT = "gen_ai.usage.output_tokens"
USAGE_CACHE_READ = "gen_ai.usage.cache_read.input_tokens"
USAGE_CACHE_WRITE = "gen_ai.usage.cache_write.input_tokens"
USAGE_REASONING_OUTPUT = "gen_ai.usage.reasoning.output_tokens"
TOOL_NAME = "gen_ai.tool.name"
TOOL_CALL_ID = "gen_ai.tool.call.id"
TOOL_CALL_ARGUMENTS = "gen_ai.tool.call.arguments"
TOOL_CALL_RESULT = "gen_ai.tool.call.result"
CONVERSATION_ID = "gen_ai.conversation.id"
WORKFLOW_NAME = "gen_ai.workflow.name"
INPUT_MESSAGES = "gen_ai.input.messages"
OUTPUT_MESSAGES = "gen_ai.output.messages"
SYSTEM_INSTRUCTIONS = "gen_ai.system_instructions"
ERROR_TYPE = "error.type"
# Deprecated aliases, kept for one release (removed in ALIASES_REMOVED_IN).
SYSTEM = "gen_ai.system"  # replaced by gen_ai.provider.name
EVENT_PROMPT = "gen_ai.content.prompt"  # replaced by gen_ai.input.messages
EVENT_COMPLETION = "gen_ai.content.completion"  # replaced by gen_ai.output.messages
USAGE_REASONING = "nexura.usage.reasoning_tokens"  # replaced by USAGE_REASONING_OUTPUT
LEGACY_OPERATION = "nexura.operation.name"  # the operation value used before 0.4.0
# Nexura-specific attributes, then the GenAI prompt attributes.
AGENT_PATH = "nexura.agent.path"
LAST_AGENT = "nexura.agent.last"
PROMPT_NAME = "gen_ai.prompt.name"
PROMPT_VERSION = "gen_ai.prompt.version"
# GenAI semantic-convention metric names.
METRIC_INFERENCE_DURATION = "gen_ai.client.inference.duration"
METRIC_AGENT_DURATION = "gen_ai.invoke_agent.duration"
METRIC_TOOL_DURATION = "gen_ai.execute_tool.duration"
METRIC_WORKFLOW_DURATION = "gen_ai.invoke_workflow.duration"
METRIC_USAGE_INPUT = "gen_ai.client.inference.usage.input_tokens"
METRIC_USAGE_OUTPUT = "gen_ai.client.inference.usage.output_tokens"
METRIC_USAGE_CACHE_READ = "gen_ai.client.inference.usage.cache_read.input_tokens"
METRIC_USAGE_CACHE_WRITE = "gen_ai.client.inference.usage.cache_write.input_tokens"
METRIC_USAGE_REASONING = "gen_ai.client.inference.usage.reasoning.output_tokens"
METRIC_OPERATION_INPUT = "gen_ai.client.inference.operation.input_tokens"
METRIC_OPERATION_OUTPUT = "gen_ai.client.inference.operation.output_tokens"
TOKEN_MODALITY = "gen_ai.token.modality"
# Deprecated metric names, recorded as before for one release (removed in ALIASES_REMOVED_IN).
METRIC_TOKEN_USAGE = "gen_ai.client.token.usage"
METRIC_DURATION = "gen_ai.client.operation.duration"
# Span attributes copied onto metrics: bounded values only, never ids or content.
_METRIC_ATTRIBUTES = (
    OPERATION_NAME,
    REQUEST_MODEL,
    AGENT_NAME,
    TOOL_NAME,
    "nexura.team.name",
    "nexura.workflow.name",
)
# Explicit bucket boundaries recommended by the GenAI metric conventions.
_DURATION_BUCKETS = [
    0.01,
    0.02,
    0.04,
    0.08,
    0.16,
    0.32,
    0.64,
    1.28,
    2.56,
    5.12,
    10.24,
    20.48,
    40.96,
    81.92,
]
_TOKEN_BUCKETS = [
    1,
    4,
    16,
    64,
    256,
    1024,
    4096,
    16384,
    65536,
    262144,
    1048576,
    4194304,
    16777216,
    67108864,
]
_AGENT_BUCKETS = [0.1, 0.2, 0.4, 0.8, 1.6, 3.2, 6.4, 12.8, 25.6, 51.2, 102.4, 204.8, 409.6]
_WORKFLOW_BUCKETS = [1, 5, 10, 30, 60, 120, 300, 600, 1800, 3600, 7200]
# Spec duration metric per operation, and the span attributes each one carries.
_SPEC_DURATIONS = {
    "chat": (METRIC_INFERENCE_DURATION, (OPERATION_NAME, PROVIDER_NAME, REQUEST_MODEL)),
    "invoke_agent": (METRIC_AGENT_DURATION, (AGENT_NAME,)),
    "execute_tool": (METRIC_TOOL_DURATION, (TOOL_NAME,)),
    "invoke_workflow": (METRIC_WORKFLOW_DURATION, (WORKFLOW_NAME,)),
}
# Attributes copied from the span onto the inference metrics once the response is known.
_LATE_METRIC_ATTRIBUTES = (PROVIDER_NAME, RESPONSE_MODEL)


@dataclass
class _State:
    tracer: Any
    capture_content: bool
    trace: Any  # opentelemetry.trace
    context: Any  # opentelemetry.context
    status: Any
    status_code: Any
    token_usage: Any = None  # histograms, None when metrics are off
    duration: Any = None
    span_kind: Any = None  # opentelemetry.trace.SpanKind
    # Spec instruments by name, empty when metrics are off.
    instruments: dict[str, Any] | None = None


_STATE: _State | None = None
_LOCK = threading.Lock()
_CONNECTED_PROVIDER: Any = None
_CONNECTION: Any = None


def connect(
    url: str | None = None, api_key: str | None = None, *, service_name: str | None = None
) -> Any:
    """Send SDK spans to hosted Nexura via batched OTLP/HTTP JSON.

    Reads NEXURA_URL/NEXURA_API_KEY and the CLI login file. Requires the optional
    opentelemetry-sdk package. Returns a processor with force_flush(), shutdown() and
    dropped_spans/exported_spans/rejected_spans counters. Content capture stays off.
    Subsequent instrument() calls reuse this provider unless one is explicitly supplied.
    """
    from nexura_agents._hosted_tracing import HostedTraceProcessor
    from nexura_agents.hosted import resolve_connection

    target, key = resolve_connection(url, api_key)
    try:
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
    except ImportError as exc:
        raise ImportError(
            "connect() requires opentelemetry-sdk; pip install opentelemetry-sdk"
        ) from exc
    global _CONNECTED_PROVIDER, _CONNECTION
    if _CONNECTION is not None:
        _CONNECTION.shutdown()
    provider = TracerProvider(
        resource=Resource.create({"service.name": service_name or "nexura-sdk"}),
        shutdown_on_exit=False,
    )
    processor = HostedTraceProcessor(target, key)
    provider.add_span_processor(processor)  # type: ignore[arg-type]
    _CONNECTED_PROVIDER, _CONNECTION = provider, processor
    instrument(tracer_provider=provider)
    return processor


def is_instrumented() -> bool:
    """True while :func:`instrument` is active."""
    return _STATE is not None


def instrument(
    *,
    tracer_provider: Any = None,
    capture_content: bool = False,
    meter_provider: Any = None,
    metrics: bool = True,
) -> None:
    """Start emitting OpenTelemetry spans and metrics for agents, teams and workflows.

    Args:
        tracer_provider: The provider to take the tracer from. Defaults to
            ``opentelemetry.trace.get_tracer_provider()``. The global provider is never set.
        capture_content: Also record prompts, completions, tool arguments/results and
            workflow state as span events (redacted and truncated). Off by default.
        meter_provider: The provider for the token and duration histograms. Defaults to
            ``opentelemetry.metrics.get_meter_provider()``. The global provider is never set.
        metrics: Set ``False`` to record spans only.

    Calling it again replaces the earlier settings; it never produces duplicate spans.

    Raises:
        ImportError: If ``opentelemetry-api`` is not installed.
    """
    global _STATE
    try:
        from opentelemetry import context as otel_context
        from opentelemetry import trace
        from opentelemetry.trace import SpanKind, Status, StatusCode
    except ImportError as exc:
        raise ImportError(
            "OpenTelemetry tracing needs opentelemetry-api. "
            'Install it with: pip install "nexura-agents[otel]"'
        ) from exc
    provider = (
        tracer_provider
        if tracer_provider is not None
        else (_CONNECTED_PROVIDER or trace.get_tracer_provider())
    )
    try:
        tracer = provider.get_tracer(_INSTRUMENTATION_NAME, schema_url=SEMCONV_GENAI_SCHEMA_URL)
    except TypeError:  # a provider without the schema_url parameter
        tracer = provider.get_tracer(_INSTRUMENTATION_NAME)
    token_usage = duration = None
    instruments: dict[str, Any] = {}
    if metrics:
        from opentelemetry import metrics as otel_metrics

        meters = meter_provider if meter_provider is not None else otel_metrics.get_meter_provider()
        meter = meters.get_meter(_INSTRUMENTATION_NAME)
        token_usage = _histogram(
            meter,
            METRIC_TOKEN_USAGE,
            "{token}",
            "Tokens used per model call.",
            _TOKEN_BUCKETS,
        )
        duration = _histogram(
            meter, METRIC_DURATION, "s", "Duration of GenAI operations.", _DURATION_BUCKETS
        )
        instruments = _spec_instruments(meter)
    with _LOCK:
        _STATE = _State(
            tracer=tracer,
            capture_content=bool(capture_content),
            trace=trace,
            context=otel_context,
            status=Status,
            status_code=StatusCode,
            token_usage=token_usage,
            duration=duration,
            span_kind=SpanKind,
            instruments=instruments,
        )


def _spec_instruments(meter: Any) -> dict[str, Any]:
    """The metric instruments of the pinned GenAI conventions, by name."""
    made: dict[str, Any] = {}
    for name, description, buckets in (
        (METRIC_INFERENCE_DURATION, "GenAI client inference duration.", _DURATION_BUCKETS),
        (METRIC_AGENT_DURATION, "Duration of an in-process agent invocation.", _AGENT_BUCKETS),
        (METRIC_TOOL_DURATION, "Duration of a tool execution.", _DURATION_BUCKETS),
        (METRIC_WORKFLOW_DURATION, "Duration of a GenAI workflow.", _WORKFLOW_BUCKETS),
    ):
        made[name] = _histogram(meter, name, "s", description, buckets)
    for name, description in (
        (METRIC_OPERATION_INPUT, "Input tokens per inference operation."),
        (METRIC_OPERATION_OUTPUT, "Output tokens per inference operation."),
    ):
        made[name] = _histogram(meter, name, "{token}", description, _TOKEN_BUCKETS)
    for name, description in (
        (METRIC_USAGE_INPUT, "Input tokens used, including cached tokens."),
        (METRIC_USAGE_OUTPUT, "Output tokens used."),
        (METRIC_USAGE_CACHE_READ, "Input tokens served from a provider cache."),
        (METRIC_USAGE_CACHE_WRITE, "Input tokens written to a provider cache."),
        (METRIC_USAGE_REASONING, "Output tokens used for reasoning."),
    ):
        # A meter without counters (a minimal or very old API) records the histograms only.
        with contextlib.suppress(Exception):
            made[name] = meter.create_counter(name, unit="{token}", description=description)
    return made


def _histogram(meter: Any, name: str, unit: str, description: str, buckets: Sequence[float]) -> Any:
    try:
        return meter.create_histogram(
            name, unit=unit, description=description, explicit_bucket_boundaries_advisory=buckets
        )
    except TypeError:  # opentelemetry-api older than 1.23 has no bucket advisory
        return meter.create_histogram(name, unit=unit, description=description)


def uninstrument() -> None:
    """Stop emitting spans. Spans already started still end normally."""
    global _STATE
    with _LOCK:
        _STATE = None


# ---------------------------------------------------------------------- redaction

_SECRET_FIELDS = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "token",
        "api_key",
        "apikey",
        "authorization",
        "cookie",
        "auth",
        "credentials",
        "credential",
        "private_key",
        "privatekey",
        "set_cookie",
        "proxy_authorization",
    }
)
_SECRET_SUFFIXES = ("_key", "_secret", "_token", "_password", "_auth", "_credential")
_BEARER = re.compile(r"(?i)Bearer\s+[^\s,;]+")
_BASIC = re.compile(r"(?i)\bBasic\s+[A-Za-z0-9+/=_\-]{6,}")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_KEYED = re.compile(
    r"""(?i)(api[_-]?key|password|secret|access_token|token|cookie|auth[_-]?token)(["']?\s*[=:]\s*)(?:"[^"]*"|'[^']*'|[^\s,;"'}]+)"""
)
_PROVIDER_KEY = re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_\-]{16,}")
_GITHUB_TOKEN = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})")


def _is_secret_key(key: str) -> bool:
    lowered = _CAMEL.sub("_", key).lower().replace("-", "_").replace(" ", "_")
    return (
        lowered in _SECRET_FIELDS
        or lowered.endswith(_SECRET_SUFFIXES)
        or lowered.startswith(("secret_", "auth_", "credential_"))
    )


def redact(value: Any, depth: int = 0) -> Any:
    """Bounded credential redaction for content recorded on spans.

    Not a personal-data detector: it masks secret-named fields, bearer tokens,
    ``key=value`` credentials and provider API keys.
    """
    if depth > 12:
        return "[DEPTH LIMIT]"
    if isinstance(value, dict):
        return {
            str(k): "[REDACTED]" if _is_secret_key(str(k)) else redact(v, depth + 1)
            for k, v in list(value.items())[:100]
        }
    if isinstance(value, (list, tuple)):
        return [redact(v, depth + 1) for v in list(value)[:100]]
    if isinstance(value, str):
        value = _BEARER.sub("Bearer [REDACTED]", value)
        value = _BASIC.sub("Basic [REDACTED]", value)
        value = _KEYED.sub(r"\1\2[REDACTED]", value)
        return _GITHUB_TOKEN.sub("[REDACTED]", _PROVIDER_KEY.sub("[REDACTED]", value))
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _render(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(redact(value), default=str)
    if isinstance(value, str):
        text = redact(text)
    if len(text) > MAX_CONTENT_CHARS:
        text = text[:MAX_CONTENT_CHARS] + " [TRUNCATED]"
    return text


def _clip(value: Any) -> Any:
    """Truncate every text value to ``MAX_CONTENT_CHARS``, keeping the structure."""
    if isinstance(value, str):
        return (
            value if len(value) <= MAX_CONTENT_CHARS else value[:MAX_CONTENT_CHARS] + " [TRUNCATED]"
        )
    if isinstance(value, dict):
        return {k: _clip(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_clip(v) for v in value]
    return value


def _json_attribute(value: Any) -> str:
    """A redacted, truncated JSON string (structured values are not span attributes yet)."""
    return json.dumps(_clip(redact(value)), ensure_ascii=False, default=str)


def _structured(value: Any) -> Any:
    """Tool arguments or results as an object when they are JSON text (best effort).

    Never raises: text longer than ``MAX_MESSAGES_CHARS`` is not parsed, and anything
    ``json`` rejects (including JSON nested deeper than the recursion limit) stays text.
    """
    if not isinstance(value, str) or len(value) > MAX_MESSAGES_CHARS:
        return value
    if not value.lstrip().startswith(("{", "[")):
        return value
    try:
        parsed = json.loads(value)
    except (ValueError, RecursionError):
        return value
    return parsed if isinstance(parsed, (dict, list)) else value


# Nexura finish reasons that differ from the GenAI conventions' values.
_FINISH_REASONS = {"tool_calls": "tool_call", "function_call": "tool_call"}
# Most recent chat-history messages considered for gen_ai.input.messages.
MAX_HISTORY_MESSAGES = 100


_URL_SCHEMES = frozenset({"http", "https", "gs"})


def _safe_url(url: str) -> str:
    """An attachment URL without userinfo, query or fragment (presigned URLs carry secrets).

    Only ``http``, ``https`` and ``gs`` URLs are kept: any other scheme (a ``data:`` URL
    holds the inline bytes themselves) becomes a placeholder.
    """
    try:
        from urllib.parse import urlsplit

        from nexura_agents.attachments import _redact_url

        if urlsplit(url).scheme.lower() not in _URL_SCHEMES:
            return "[OMITTED non-http URL]"
        return _redact_url(url)
    except Exception:
        return "[REDACTED URL]"


def _parts(message: Any) -> list[dict[str, Any]]:
    """The message parts of the GenAI input/output message JSON schemas."""
    parts: list[dict[str, Any]] = []
    role = getattr(message, "role", None)
    content = getattr(message, "content", None)
    if role == "tool":
        parts.append(
            {
                "type": "tool_call_response",
                "id": getattr(message, "tool_call_id", None),
                "response": _structured(content),
            }
        )
        return parts
    if content:
        parts.append({"type": "text", "content": str(content)})
    for item in getattr(message, "attachments", None) or []:
        part: dict[str, Any] = {
            "modality": getattr(item, "kind", None) or "document",
            "mime_type": getattr(item, "mime_type", None),
        }
        if getattr(item, "url", None):
            parts.append({"type": "uri", **part, "uri": _safe_url(str(item.url))})
        else:  # inline bytes are never copied into telemetry
            data = getattr(item, "data", None) or ""
            parts.append({"type": "blob", **part, "content": f"[OMITTED {len(data)} chars]"})
    for call in getattr(message, "tool_calls", None) or []:
        parts.append(
            {
                "type": "tool_call",
                "id": getattr(call, "id", None),
                "name": getattr(call, "name", ""),
                "arguments": getattr(call, "arguments", None),
            }
        )
    return parts


def _chat_message(message: Any) -> dict[str, Any]:
    item: dict[str, Any] = {"role": str(getattr(message, "role", "user")), "parts": _parts(message)}
    name = getattr(message, "name", None)
    if name and item["role"] != "tool":
        item["name"] = str(name)
    return item


def input_messages(messages: Sequence[Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """``(system_instructions, input_messages)`` in the GenAI conventions' JSON format.

    Leading system messages (the agent's instructions, which Nexura keeps apart from the
    chat history) become ``gen_ai.system_instructions``; the rest is the chat history, of
    which only the newest ``MAX_HISTORY_MESSAGES`` are converted.
    """
    rest = list(messages)
    instructions: list[dict[str, Any]] = []
    while rest and getattr(rest[0], "role", None) == "system":
        text = getattr(rest.pop(0), "content", None)
        if text:
            instructions.append({"type": "text", "content": str(text)})
    return instructions, [_chat_message(m) for m in rest[-MAX_HISTORY_MESSAGES:]]


def output_messages(message: Any) -> list[dict[str, Any]]:
    """The model's reply as ``gen_ai.output.messages`` (one choice).

    The pinned schema deprecates ``finish_reason`` on output messages: finish reasons are
    reported in ``gen_ai.response.finish_reasons`` only.
    """
    item = _chat_message(message)
    item["role"] = "assistant"
    return [item]


def _prepared(value: Any) -> Any:
    """Redacted, then every text value truncated (the JSON structure is kept)."""
    return _clip(redact(value))


def _history_json(history: list[dict[str, Any]]) -> str:
    """``gen_ai.input.messages`` JSON: newest messages kept, oldest dropped to fit.

    Each message is redacted, truncated and serialized once, and the oldest are dropped
    with a running total, so the cost is linear in the history size.
    """
    encoded = [
        json.dumps(_prepared(m), ensure_ascii=False, default=str)
        for m in history[-MAX_HISTORY_MESSAGES:]
    ]
    total = sum(len(e) for e in encoded) + max(len(encoded) - 1, 0) * 2 + 2
    first = 0
    while len(encoded) - first > 1 and total > MAX_MESSAGES_CHARS:
        total -= len(encoded[first]) + 2
        first += 1
    return "[" + ", ".join(encoded[first:]) + "]"


def _bounded_history(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The kept history as objects (see :func:`_history_json`)."""
    kept: list[dict[str, Any]] = json.loads(_history_json(history))
    return kept


# ---------------------------------------------------------------------- span handles


class _NoopHandle:
    """Stand-in used when tracing is off; every method does nothing."""

    __slots__ = ()
    recording = False
    capturing = False

    def set(self, key: str, value: Any) -> None:
        return None

    def set_usage(self, usage: Any) -> None:
        return None

    def content(self, event: str, key: str, value: Any) -> None:
        return None

    def content_attribute(self, key: str, value: Any) -> None:
        return None

    def event(self, name: str, attributes: dict[str, Any] | None = None) -> None:
        return None

    def is_recording(self) -> bool:
        return False

    def skip_spec_duration(self) -> None:
        return None

    def fail(self, exc: BaseException) -> None:
        return None

    def cancelled(self) -> None:
        return None

    def attach(self) -> None:
        return None

    def end(self) -> None:
        return None

    @contextlib.contextmanager
    def activated(self) -> Iterator[None]:
        yield


NOOP: Any = _NoopHandle()


class _Handle:
    """A started span plus the context needed to parent children and propagate it."""

    __slots__ = (
        "_span",
        "_state",
        "_ctx",
        "_token",
        "_ended",
        "_started",
        "_metric",
        "_dims",
        "_error",
        "timed",
        "spec_timed",
    )
    recording = True

    def __init__(
        self, state: _State, span: Any, ctx: Any, attributes: dict[str, Any] | None = None
    ) -> None:
        self._state = state
        self._span = span
        self._ctx = ctx
        self._token: Any = None
        self._ended = False
        self._started = time.monotonic()
        attributes = attributes or {}
        self._metric = {k: attributes[k] for k in _METRIC_ATTRIBUTES if k in attributes}
        legacy = attributes.get(LEGACY_OPERATION)
        if isinstance(legacy, str):
            # The deprecated duration histogram keeps the pre-0.4.0 operation value.
            self._metric[OPERATION_NAME] = legacy
        # Low-cardinality span attributes the spec metrics are recorded with.
        self._dims: dict[str, Any] = {
            k: attributes[k]
            for k in (OPERATION_NAME, PROVIDER_NAME, REQUEST_MODEL, AGENT_NAME, TOOL_NAME)
            + (WORKFLOW_NAME,)
            if k in attributes
        }
        self._error: str | None = None
        self.timed = True  # False: end() records no duration
        # False: no spec duration point (an operation that did not run, such as a rejection)
        self.spec_timed = True

    @property
    def capturing(self) -> bool:
        """True when content capture is on for this span."""
        return self._state.capture_content

    def skip_spec_duration(self) -> None:
        """Record no GenAI-convention duration point for this span (deprecated one unchanged)."""
        self.spec_timed = False

    def is_recording(self) -> bool:
        """True until the span ends (and while the SDK samples it)."""
        if self._ended:
            return False
        try:
            return bool(self._span.is_recording())
        except Exception:
            return False

    def set(self, key: str, value: Any) -> None:
        if value is None:
            return
        if key in _LATE_METRIC_ATTRIBUTES and isinstance(value, str):
            self._dims[key] = value
        with contextlib.suppress(Exception):
            self._span.set_attribute(key, value)

    def set_usage(self, usage: Any) -> None:
        """Record token totals; fields the provider did not report are skipped."""
        if usage is None:
            return
        self.set(USAGE_INPUT, getattr(usage, "prompt_tokens", None))
        self.set(USAGE_OUTPUT, getattr(usage, "completion_tokens", None))
        cache_read = getattr(usage, "cache_read_input_tokens", None)
        if cache_read is None:
            cache_read = getattr(usage, "cached_prompt_tokens", None)
        cache_write = getattr(usage, "cache_creation_input_tokens", None)
        reasoning = getattr(usage, "reasoning_tokens", None)
        # The conventions record these "when available and non-zero".
        self.set(USAGE_CACHE_READ, cache_read or None)
        self.set(USAGE_CACHE_WRITE, cache_write or None)
        self.set(USAGE_REASONING_OUTPUT, reasoning or None)
        self.set(USAGE_REASONING, reasoning)  # deprecated alias
        # Only model calls count tokens; run and team spans hold totals of those calls.
        if self._metric.get(OPERATION_NAME) != "chat":
            return
        histogram = self._state.token_usage
        if histogram is not None:
            for token_type, field in (("input", "prompt_tokens"), ("output", "completion_tokens")):
                count = getattr(usage, field, None)
                if isinstance(count, int) and count >= 0:
                    with contextlib.suppress(Exception):
                        histogram.record(count, {**self._metric, "gen_ai.token.type": token_type})
        self._record_spec_tokens(usage, cache_read, cache_write, reasoning)

    def _record_spec_tokens(
        self, usage: Any, cache_read: Any, cache_write: Any, reasoning: Any
    ) -> None:
        instruments = self._state.instruments
        if not instruments:
            return
        dims = self._inference_dims()
        # Nexura's usage has no per-modality breakdown: the conventions say "unknown".
        counted = {**dims, TOKEN_MODALITY: "unknown"}
        for name, count in (
            (METRIC_USAGE_INPUT, getattr(usage, "prompt_tokens", None)),
            (METRIC_USAGE_OUTPUT, getattr(usage, "completion_tokens", None)),
            (METRIC_USAGE_CACHE_READ, cache_read),
            (METRIC_USAGE_CACHE_WRITE, cache_write),
            (METRIC_USAGE_REASONING, reasoning),
        ):
            if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
                with contextlib.suppress(Exception):
                    if name in instruments:
                        instruments[name].add(count, counted)
        for name, count in (
            (METRIC_OPERATION_INPUT, getattr(usage, "prompt_tokens", None)),
            (METRIC_OPERATION_OUTPUT, getattr(usage, "completion_tokens", None)),
        ):
            if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
                with contextlib.suppress(Exception):
                    instruments[name].record(count, dims)

    def _inference_dims(self) -> dict[str, Any]:
        keys = (OPERATION_NAME, PROVIDER_NAME, REQUEST_MODEL, RESPONSE_MODEL)
        return {k: self._dims[k] for k in keys if k in self._dims}

    def content(self, event: str, key: str, value: Any) -> None:
        """Record content as a span event, only when ``capture_content`` is on."""
        if not self._state.capture_content:
            return
        with contextlib.suppress(Exception):
            self._span.add_event(event, {key: _render(value)})

    def content_attribute(self, key: str, value: Any) -> None:
        """Record content as a JSON span attribute, only when ``capture_content`` is on.

        The value is redacted and each text value truncated, keeping the JSON structure.
        """
        if not self._state.capture_content or value is None:
            return
        with contextlib.suppress(Exception):
            self._span.set_attribute(key, _json_attribute(value))

    def event(self, name: str, attributes: dict[str, Any] | None = None) -> None:
        with contextlib.suppress(Exception):
            self._span.add_event(name, attributes)

    def fail(self, exc: BaseException) -> None:
        """Mark the span as an error and record the exception."""
        kind = str(redact(type(exc).__name__))
        # The exception text can echo credentials or user data: it is recorded (redacted and
        # truncated) only with capture_content on; otherwise just the exception type.
        message = _render(str(exc))[:200] if self._state.capture_content else ""
        with contextlib.suppress(Exception):
            attributes: dict[str, Any] = {"exception.type": kind}
            if message:
                attributes["exception.message"] = message
            self._span.add_event("exception", attributes)
            self._span.set_status(
                self._state.status(self._state.status_code.ERROR, message or kind)
            )
            self._span.set_attribute("error.type", kind)
            if self._dims.get(OPERATION_NAME) == "chat":
                # The generation ended abnormally (registry note on finish_reasons).
                self._span.set_attribute(FINISH_REASONS, ["error"])
        self._error = kind

    def cancelled(self) -> None:
        """The operation was cancelled: its duration metric gets ``error.type=cancelled``.

        The span status is left unset; a cancellation is a requested stop, not a failure.
        """
        self._error = self._error or "cancelled"

    def attach(self) -> None:
        """Make this span current until :meth:`end`; call from the task that will end it."""
        with contextlib.suppress(Exception):
            self._token = self._state.context.attach(self._ctx)

    def end(self) -> None:
        if self._ended:
            return
        self._ended = True
        self._record_duration()
        if self._token is not None:
            with contextlib.suppress(Exception):
                self._state.context.detach(self._token)
            self._token = None
        with contextlib.suppress(Exception):
            self._span.end()

    def _record_duration(self) -> None:
        histogram = self._state.duration
        operation = self._metric.get(OPERATION_NAME)
        # Handoff marks and tool calls waiting for approval are not operations that ran.
        if histogram is None or not self.timed or operation in (None, "handoff"):
            return
        elapsed = time.monotonic() - self._started
        attributes = dict(self._metric)
        if self._error is not None:
            attributes["error.type"] = self._error
        with contextlib.suppress(Exception):
            histogram.record(elapsed, attributes)
        operation = self._dims.get(OPERATION_NAME)
        spec = _SPEC_DURATIONS.get(str(operation))
        instruments = self._state.instruments
        if spec is None or not instruments or not self.spec_timed:
            return
        name, keys = spec
        dims = {k: self._dims[k] for k in keys if k in self._dims}
        if operation == "chat" and RESPONSE_MODEL in self._dims:
            dims[RESPONSE_MODEL] = self._dims[RESPONSE_MODEL]
        if self._error is not None:
            dims[ERROR_TYPE] = self._error
        with contextlib.suppress(Exception):
            instruments[name].record(elapsed, dims)

    @contextlib.contextmanager
    def activated(self) -> Iterator[None]:
        """Make this span current inside the block (no ``yield`` of control across tasks)."""
        token = None
        with contextlib.suppress(Exception):
            token = self._state.context.attach(self._ctx)
        try:
            yield
        finally:
            if token is not None:
                with contextlib.suppress(Exception):
                    self._state.context.detach(token)


def start(
    name: str,
    attributes: dict[str, Any] | None = None,
    *,
    parent: Any = None,
    client: bool = False,
) -> Any:
    """Start a span, or return a no-op handle when tracing is off.

    ``parent`` is another handle; without it the span nests under the current context.
    ``client=True`` makes a ``CLIENT`` span (a call to a model service); the default is
    ``INTERNAL``.
    """
    state = _STATE
    if state is None:
        return NOOP
    try:
        parent_ctx = parent._ctx if isinstance(parent, _Handle) else None
        kinds = state.span_kind
        kind = (kinds.CLIENT if client else kinds.INTERNAL) if kinds is not None else None
        extra: dict[str, Any] = {"kind": kind} if kind is not None else {}
        span = state.tracer.start_span(
            name, context=parent_ctx, attributes=attributes or None, **extra
        )
        ctx = state.trace.set_span_in_context(span, parent_ctx)
        return _Handle(state, span, ctx, attributes)
    except Exception:
        return NOOP


# ---------------------------------------------------------------------- public helpers


def _user_attributes(attributes: dict[str, Any]) -> dict[str, Any]:
    """Plain values only (strings redacted); other values are dropped, never stringified."""
    kept: dict[str, Any] = {}
    for key, value in attributes.items():
        if _is_secret_key(str(key)):
            continue
        if isinstance(value, str):
            kept[str(key)] = redact(value)
        elif isinstance(value, (bool, int)) or (isinstance(value, float) and math.isfinite(value)):
            kept[str(key)] = value
    return kept


@contextlib.contextmanager
def span(name: str, **attributes: Any) -> Iterator[Any]:
    """A span around your own code, nested under the current span (a run, a tool call).

    Use it inside tools or around steps of your application. Attribute values must be
    strings, numbers or booleans; others, and secret-named keys, are dropped, and strings
    are redacted. Yields a handle with ``set(key, value)``. Does nothing while tracing is
    off. An exception marks the span as failed and is re-raised.
    """
    handle = start(name, {"nexura.span.custom": True, **_user_attributes(attributes)})
    if handle is NOOP:
        yield handle
        return
    try:
        with handle.activated():
            yield _UserHandle(handle)
    except BaseException as exc:
        if isinstance(exc, Exception):
            handle.fail(exc)
        raise
    finally:
        handle.end()


@contextlib.contextmanager
def trace(
    name: str, *, group_id: str | None = None, metadata: dict[str, Any] | None = None
) -> Iterator[Any]:
    """Group the runs started inside the block under one parent span.

    ``group_id`` links several traces, for example every turn of one conversation or one
    batch job (``nexura.trace.group_id``). ``metadata`` adds plain attributes under
    ``nexura.trace.metadata.<key>``.
    """
    attributes: dict[str, Any] = {"nexura.trace.name": name}
    if group_id is not None:
        attributes["nexura.trace.group_id"] = str(group_id)
    for key, value in _user_attributes(metadata or {}).items():
        attributes[f"nexura.trace.metadata.{key}"] = value
    with span(f"trace {name}", **attributes) as handle:
        yield handle


class _UserHandle:
    """What ``span()`` yields: set attributes with the same filtering as its arguments."""

    __slots__ = ("_handle",)

    def __init__(self, handle: Any) -> None:
        self._handle = handle

    def set(self, key: str, value: Any) -> None:
        for name, kept in _user_attributes({key: value}).items():
            self._handle.set(name, kept)


# ---------------------------------------------------------------------- helpers used by Runner


def model_name(model: Any) -> str:
    """Best-effort model name for ``gen_ai.request.model``."""
    for attr in ("model", "model_name", "model_id", "name"):
        value = getattr(model, attr, None)
        if isinstance(value, str) and value:
            return value
    return type(model).__name__


def messages_payload(messages: list[Any]) -> list[Any]:
    """Messages as dicts for the deprecated content events (attachment secrets removed)."""
    out: list[Any] = []
    for m in messages:
        item = m.to_dict() if hasattr(m, "to_dict") else str(m)
        if isinstance(item, dict) and item.get("attachments"):
            item["attachments"] = [_attachment_summary(a) for a in item["attachments"]]
        out.append(item)
    return out


def _attachment_summary(item: Any) -> Any:
    if not isinstance(item, dict):
        return "[ATTACHMENT]"
    kept = {k: item[k] for k in ("mime_type", "name") if k in item}
    if item.get("url"):
        kept["url"] = _safe_url(str(item["url"]))
    if item.get("data"):
        kept["data"] = f"[OMITTED {len(str(item['data']))} chars]"
    return kept


# Model classes by provider, for gen_ai.provider.name (the conventions' well-known values).
_PROVIDERS = {
    "AnthropicNativeProvider": "anthropic",
    "OpenAIProvider": "openai",
    "OpenAIResponsesProvider": "openai",
    "OpenAIChatModel": "openai",
    "VertexAIProvider": "gcp.vertex_ai",  # nexura-vertex; checked before its Gemini base
    "GeminiNativeProvider": "gcp.gemini",
    "SarvamProvider": "sarvam",
    "FakeModel": "fake",
    "StreamingFakeProvider": "fake",
}
# OpenAI-compatible adapters: the provider follows the base URL's host.
_OPENAI_COMPATIBLE = frozenset({"OpenAIProvider", "OpenAIResponsesProvider", "OpenAIChatModel"})
_HOSTS = {
    "api.openai.com": "openai",
    "api.groq.com": "groq",
    "api.deepseek.com": "deepseek",
    "api.mistral.ai": "mistral",
    "api.x.ai": "x_ai",
    "api.perplexity.ai": "perplexity",
    "api.moonshot.ai": "moonshot_ai",
    "generativelanguage.googleapis.com": "gcp.gemini",
}
# LiteLLM model-string prefixes.
_LITELLM_PREFIXES = {
    "anthropic": "anthropic",
    "openai": "openai",
    "azure": "azure.ai.openai",
    "azure_ai": "azure.ai.inference",
    "bedrock": "aws.bedrock",
    "cohere": "cohere",
    "deepseek": "deepseek",
    "gemini": "gcp.gemini",
    "vertex_ai": "gcp.vertex_ai",
    "groq": "groq",
    "mistral": "mistral",
    "perplexity": "perplexity",
    "watsonx": "ibm.watsonx.ai",
    "xai": "x_ai",
}


def provider_name(model: Any) -> str | None:
    """Best-effort ``gen_ai.provider.name`` for a model, or ``None`` when unknown.

    A model can declare it with a ``gen_ai_provider_name`` string attribute. OpenAI-compatible
    adapters are named by their base URL host (an unknown host, such as a local Ollama
    server, gives ``None`` rather than a guess). Wrappers
    (retry, rate limit, recording) report their wrapped model's provider; a fallback chain
    reports its first model's.
    """
    seen: set[int] = set()
    while model is not None and id(model) not in seen:
        seen.add(id(model))
        declared = getattr(model, "gen_ai_provider_name", None)
        if isinstance(declared, str) and declared:
            return declared
        for cls in type(model).__mro__:
            if cls.__name__ == "LiteLLMModel":
                prefix = str(getattr(model, "model", "")).split("/", 1)
                return _LITELLM_PREFIXES.get(prefix[0], "openai") if len(prefix) == 2 else "openai"
            if cls.__name__ in _OPENAI_COMPATIBLE:
                return _openai_compatible(getattr(model, "base_url", None))
            if cls.__name__ in _PROVIDERS:
                return _PROVIDERS[cls.__name__]
        inner = getattr(model, "_model", None)
        if inner is None:
            wrapped = getattr(model, "model", None)
            inner = wrapped if wrapped is not None and not isinstance(wrapped, str) else None
        if inner is None:
            chain = getattr(model, "models", None)
            inner = chain[0] if isinstance(chain, (list, tuple)) and chain else None
        model = inner
    return None


def _openai_compatible(base_url: Any) -> str | None:
    """Provider of an OpenAI-compatible endpoint by host; ``None`` for unknown hosts."""
    if not isinstance(base_url, str):
        return "openai"
    from urllib.parse import urlsplit

    try:
        host = (urlsplit(base_url).hostname or "").lower()
    except ValueError:
        return None
    if host.endswith(".openai.azure.com"):
        return "azure.ai.openai"
    return _HOSTS.get(host)


def record_prompt(chat: Any, messages: list[Any]) -> None:
    """Opt-in prompt content on a ``chat`` span (``capture_content=True`` only).

    Sets ``gen_ai.system_instructions`` and ``gen_ai.input.messages`` and, for one release,
    the deprecated ``gen_ai.content.prompt`` event. Never raises: content that cannot be
    converted is left out, the run is not affected.
    """
    if not chat.capturing:
        return
    with contextlib.suppress(Exception):
        instructions, history = input_messages(messages)
        if instructions:
            chat.set(SYSTEM_INSTRUCTIONS, _json_attribute(instructions))
        chat.set(INPUT_MESSAGES, _history_json(history))
    with contextlib.suppress(Exception):
        chat.content(EVENT_PROMPT, "gen_ai.prompt", messages_payload(messages))


def record_response(chat: Any, response: Any) -> None:
    """Usage, finish reason, response id/model and opt-in output content of a model call.

    Never raises. The response model is set before usage, so the token metrics carry it.
    """
    if chat is NOOP:
        return
    with contextlib.suppress(Exception):
        chat.set(RESPONSE_ID, getattr(response, "response_id", None))
        chat.set(RESPONSE_MODEL, getattr(response, "response_model", None))
        chat.set_usage(response.usage)
        if response.finish_reason:
            raw = str(response.finish_reason)
            chat.set(FINISH_REASONS, [_FINISH_REASONS.get(raw, raw)])
            if raw in _FINISH_REASONS:  # the value Nexura reported before 0.4.0
                chat.set(RAW_FINISH_REASONS, [raw])
    if chat.capturing:
        with contextlib.suppress(Exception):
            chat.content_attribute(OUTPUT_MESSAGES, output_messages(response.message))
        with contextlib.suppress(Exception):
            chat.content(
                EVENT_COMPLETION, "gen_ai.completion", messages_payload([response.message])
            )


def record_tool_content(span: Any, key: str, value: Any) -> None:
    """Opt-in tool arguments or result as a JSON attribute; nothing is built with capture off.

    Never raises: a result that cannot be parsed is recorded as text, or left out.
    """
    if not span.capturing:
        return
    with contextlib.suppress(Exception):
        span.content_attribute(key, _structured(value) if key == TOOL_CALL_RESULT else value)


def start_run(agent_name: str, *, resumed: bool) -> Any:
    handle = start(
        f"invoke_agent {agent_name}",
        {OPERATION_NAME: "invoke_agent", AGENT_NAME: agent_name, "nexura.run.resumed": resumed},
    )
    return handle


def start_recover(agent_name: str) -> Any:
    """Span for a run recovered from a durable checkpoint."""
    return start(
        f"recover {agent_name}",
        {
            OPERATION_NAME: "invoke_agent",
            AGENT_NAME: agent_name,
            "nexura.run.resumed": True,
            "nexura.recovered": True,
        },
    )


def start_chat(parent: Any, model: Any) -> Any:
    if parent is NOOP and _STATE is None:
        return NOOP
    name = model_name(model)
    attributes: dict[str, Any] = {OPERATION_NAME: "chat", REQUEST_MODEL: name}
    provider = provider_name(model)
    if provider is not None:
        attributes[PROVIDER_NAME] = provider
        attributes[SYSTEM] = provider  # deprecated alias, removed in ALIASES_REMOVED_IN
    return start(f"chat {name}", attributes, parent=parent, client=True)


def _export_tool_call_id(call_id: str, *, run_id: str, capture_content: bool) -> str:
    """Run-scoped telemetry identity; never replace an application or journal id."""
    if capture_content:
        return call_id
    # Even a Python string containing a lone surrogate must not make tracing fail a run.
    digest = hmac.new(
        run_id.encode("utf-8", errors="surrogatepass"),
        call_id.encode("utf-8", errors="surrogatepass"),
        hashlib.sha256,
    ).hexdigest()
    return f"call-{digest[:16]}"


def start_tool(parent: Any, call: Any, *, run_id: str, pending_approval: bool = False) -> Any:
    state = _STATE
    if state is None:
        return NOOP
    handle = start(
        f"execute_tool {call.name}",
        {
            OPERATION_NAME: "execute_tool",
            TOOL_NAME: call.name,
            TOOL_CALL_ID: _export_tool_call_id(
                call.id, run_id=run_id, capture_content=state.capture_content
            ),
        },
        parent=parent,
    )
    if pending_approval:
        handle.set("nexura.tool.pending_approval", True)
        if isinstance(handle, _Handle):
            handle.timed = False
    return handle


def start_handoff(parent: Any, source: str, target: str, reason: str, path: list[str]) -> Any:
    """A zero-length span marking control moving from ``source`` to ``target``.

    The reason (or structured handoff input) is written by the model, so it is recorded
    only with ``capture_content=True``, like tool arguments.
    """
    span = start(
        f"handoff {source} -> {target}",
        {
            OPERATION_NAME: "handoff",
            "nexura.handoff.from": source,
            "nexura.handoff.to": target,
            AGENT_PATH: list(path),
        },
        parent=parent,
    )
    if reason:
        span.content("nexura.handoff.reason", "nexura.handoff.reason", reason)
    return span


def start_workflow(name: str, attributes: dict[str, Any]) -> Any:
    """An ``invoke_workflow {name}`` span for a workflow run (INTERNAL)."""
    return start(
        f"invoke_workflow {name}",
        {
            OPERATION_NAME: "invoke_workflow",
            WORKFLOW_NAME: name,
            LEGACY_OPERATION: "workflow",  # deprecated alias, removed in ALIASES_REMOVED_IN
            **attributes,
        },
    )


# ---------------------------------------------------------------------- Nexura events

# Event names in the nexura.* namespace (approvals, budget reservations, durable effects).
EVENT_APPROVAL_REQUESTED = "nexura.approval.requested"
EVENT_APPROVAL_DECIDED = "nexura.approval.decided"
EVENT_BUDGET_RESERVE = "nexura.budget.reserve"
EVENT_BUDGET_SETTLE = "nexura.budget.settle"
EVENT_BUDGET_REFUSE = "nexura.budget.refuse"
EVENT_EFFECT_INTENT = "nexura.effect.intent"
EVENT_EFFECT_OUTCOME = "nexura.effect.outcome"
EVENT_EFFECT_UNKNOWN = "nexura.effect.unknown"
EVENT_EFFECT_RESOLVED = "nexura.effect.resolved"
EVENT_GUARDRAIL_DECISION = "nexura.guardrail.decision"
# Attributes of nexura.guardrail.decision (P39 design section 8.6).
GUARDRAIL_STAGE = "nexura.guardrail.stage"
GUARDRAIL_RULE = "nexura.guardrail.rule"
GUARDRAIL_CHECK = "nexura.guardrail.check"
GUARDRAIL_BACKEND = "nexura.guardrail.backend"
GUARDRAIL_ACTION = "nexura.guardrail.action"
GUARDRAIL_CATEGORIES = "nexura.guardrail.categories"
GUARDRAIL_SCORE = "nexura.guardrail.score"
GUARDRAIL_LATENCY_MS = "nexura.guardrail.latency_ms"
GUARDRAIL_CACHED = "nexura.guardrail.cached"
GUARDRAIL_SOURCE = "nexura.guardrail.source"
GUARDRAIL_POLICY_DIGEST = "nexura.guardrail.policy_digest"
GUARDRAIL_MODE = "nexura.guardrail.mode"
GUARDRAIL_APPLIED = "nexura.guardrail.applied"
GUARDRAIL_TOKENS = "nexura.guardrail.tokens"
GUARDRAIL_SKIPPED = "nexura.guardrail.skipped"
GUARDRAIL_DECISION_VALUE = "nexura.guardrail.decision"
_EFFECT_EVENTS = {
    "pending": EVENT_EFFECT_INTENT,
    "confirmed": EVENT_EFFECT_OUTCOME,
    "indeterminate": EVENT_EFFECT_UNKNOWN,
    "resolved": EVENT_EFFECT_RESOLVED,
}


def _plain(attributes: dict[str, Any]) -> dict[str, Any]:
    kept: dict[str, Any] = {}
    for key, value in attributes.items():
        if isinstance(value, (str, bool, int)) or (
            isinstance(value, float) and math.isfinite(value)
        ):
            kept[key] = value
        elif isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value):
            kept[key] = [str(v) for v in value]
    return kept


def record_event(name: str, attributes: dict[str, Any], *, on: Any = None) -> None:
    """Record a ``nexura.*`` event; never raises and does nothing while tracing is off.

    The event goes on ``on`` (a span handle) while that span is open, otherwise on the
    current span. With no span open it is exported as a zero-length span of the same name
    and attributes, so the event is not lost. Values must be plain (strings, numbers,
    booleans, string lists); others are dropped. Never pass content or secrets.
    """
    state = _STATE
    if state is None:
        return
    try:
        attrs = _plain(attributes)
        if isinstance(on, _Handle) and on.is_recording():
            on.event(name, attrs)
            return
        current = state.trace.get_current_span()
        if current is not None and current.is_recording():
            current.add_event(name, attrs)
            return
        kinds = state.span_kind
        extra: dict[str, Any] = {"kind": kinds.INTERNAL} if kinds is not None else {}
        state.tracer.start_span(name, attributes=attrs or None, **extra).end()
    except Exception:
        return


def approval_requested(
    on: Any,
    approval_id: str,
    *,
    kind: str,
    reason: str = "approval",
    tools: Sequence[str] = (),
    node_id: str | None = None,
) -> None:
    """``nexura.approval.requested``: a run or workflow paused for a human decision."""
    if _STATE is None:
        return
    attributes: dict[str, Any] = {
        "nexura.approval.id": approval_id,
        "nexura.approval.kind": kind,
        "nexura.approval.reason": reason,
    }
    if tools:
        attributes["nexura.approval.pending_count"] = len(tools)
        attributes["nexura.approval.tools"] = sorted({str(t) for t in tools})
    if node_id is not None:
        attributes["nexura.workflow.node.id"] = node_id
    record_event(EVENT_APPROVAL_REQUESTED, attributes, on=on)


def approval_decided(on: Any, approval_id: str, *, kind: str, decision: str, **counts: Any) -> None:
    """``nexura.approval.decided``: an accepted decision (after its checks passed).

    ``decision`` is ``approve``, ``reject`` or ``mixed``; ``counts`` become
    ``nexura.approval.<key>`` (for example ``approved_count``). No actor or reason text.
    """
    if _STATE is None:
        return
    attributes: dict[str, Any] = {
        "nexura.approval.id": approval_id,
        "nexura.approval.kind": kind,
        "nexura.approval.decision": decision,
    }
    for key, value in counts.items():
        attributes[f"nexura.approval.{key}"] = value
    record_event(EVENT_APPROVAL_DECIDED, attributes, on=on)


def budget_event(kind: str, model_id: str, *, method: str | None = None, **values: Any) -> None:
    """``nexura.budget.reserve`` / ``settle`` / ``refuse`` for one provider-call reservation.

    ``values`` become ``nexura.budget.<key>``; ``error_type`` becomes ``error.type``.
    """
    if _STATE is None:
        return
    attributes: dict[str, Any] = {"nexura.budget.model": model_id}
    if method is not None:
        attributes["nexura.budget.method"] = method
    for key, value in values.items():
        if value is not None:
            attributes[ERROR_TYPE if key == "error_type" else f"nexura.budget.{key}"] = value
    record_event(f"nexura.budget.{kind}", attributes)


def _export_effect_step_key(
    step_key: str, *, run_id: str, capture_content: bool, raw_call_id: str | None = None
) -> str:
    """Project an agent call key without changing its journal representation."""
    if capture_content:
        return step_key
    from nexura_agents._agent_journal import call_step_key, parse_call_step_key

    try:
        turn, call_id, attempt = parse_call_step_key(step_key)
    except ValueError:
        return step_key  # Other (for example workflow) step keys have no call id.
    # Prefer the model's raw id: the journal codec normalizes an empty id to "-".
    # Decoding remains a fallback for standalone records without a supplied raw id.
    pseudonym = _export_tool_call_id(
        call_id if raw_call_id is None else raw_call_id, run_id=run_id, capture_content=False
    )
    return str(call_step_key(turn, pseudonym, attempt))


def effects_committed(
    effects: Sequence[Any],
    *,
    run_id: str,
    on: Any = None,
    raw_call_ids: Mapping[str, str] | None = None,
) -> None:
    """``nexura.effect.*`` for journal effect records committed by a durable run.

    ``pending`` is the intent written before dispatch, ``confirmed`` the outcome,
    ``indeterminate`` an unknown outcome and ``resolved`` a person's resolution. Results
    are never recorded. ``raw_call_ids`` supplies telemetry-only identities keyed by
    journal step key, preserving ids (including "") the journal codec normalizes.
    """
    state = _STATE
    if state is None or not effects:
        return
    for effect in effects:
        try:
            status = str(getattr(effect.status, "value", effect.status))
            intent = effect.intent
            attributes: dict[str, Any] = {
                "nexura.effect.id": effect.effect_id,
                "nexura.effect.step_key": _export_effect_step_key(
                    str(intent.step_key),
                    run_id=run_id,
                    capture_content=state.capture_content,
                    raw_call_id=(
                        raw_call_ids.get(str(intent.step_key)) if raw_call_ids is not None else None
                    ),
                ),
                "nexura.effect.class": str(getattr(intent.effect_class, "value", "")),
                "nexura.effect.status": status,
                "nexura.run_id": run_id,
            }
            if intent.approval_id:
                attributes["nexura.approval.id"] = intent.approval_id
            if effect.resolution is not None:
                attributes["nexura.effect.resolution"] = str(
                    getattr(effect.resolution, "value", effect.resolution)
                )
        except Exception:
            continue
        name = _EFFECT_EVENTS.get(status)
        if name is not None:
            record_event(name, attributes, on=on)


def guardrail_decision(on: Any, decision: dict[str, Any], *, run_id: str) -> None:
    """``nexura.guardrail.decision`` for one guardrail outcome; never raises.

    ``decision`` is the redacted record the runner also sends as a ``GUARDRAIL_DECISION``
    run event (stage, guardrail, action, tool name and call id, latency and plain
    classification fields). Only those keys are mapped; nothing else is recorded, so no
    content can reach the span.
    """
    state = _STATE
    if state is None:
        return
    mapping = {
        "stage": GUARDRAIL_STAGE,
        "guardrail": GUARDRAIL_CHECK,
        "action": GUARDRAIL_ACTION,
        "rule": GUARDRAIL_RULE,
        "backend": GUARDRAIL_BACKEND,
        "categories": GUARDRAIL_CATEGORIES,
        "score": GUARDRAIL_SCORE,
        "latency_ms": GUARDRAIL_LATENCY_MS,
        "cached": GUARDRAIL_CACHED,
        "decision": GUARDRAIL_DECISION_VALUE,
        "policy_digest": GUARDRAIL_POLICY_DIGEST,
        "mode": GUARDRAIL_MODE,
        "applied": GUARDRAIL_APPLIED,
        "tokens": GUARDRAIL_TOKENS,
        "skipped": GUARDRAIL_SKIPPED,
        "tool_name": TOOL_NAME,
        "tool_call_id": TOOL_CALL_ID,
        "error_type": ERROR_TYPE,
    }
    source = decision.get("source")
    attributes: dict[str, Any] = {
        GUARDRAIL_SOURCE: source if source in ("live", "cache", "journal") else "live"
    }
    for key, name in mapping.items():
        value = decision.get(key)
        if value is not None:
            if name == TOOL_CALL_ID:
                value = _export_tool_call_id(
                    value, run_id=run_id, capture_content=state.capture_content
                )
            attributes[name] = value
    record_event(EVENT_GUARDRAIL_DECISION, attributes, on=on)
