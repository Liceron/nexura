# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Opt-in OpenTelemetry tracing for agents, teams and workflows.

Call :func:`instrument` once at start-up and every ``Runner`` run, model call, tool call,
team run and workflow run emits spans that follow the OpenTelemetry GenAI semantic
conventions (``gen_ai.*`` attributes). Nothing is imported or recorded until then.

Conventions used (https://opentelemetry.io/docs/specs/semconv/gen-ai/):

- ``gen_ai.operation.name`` is ``invoke_agent``, ``chat`` or ``execute_tool`` (well-known
  values). Teams and workflows use the custom values ``invoke_team`` and ``workflow``.
- ``gen_ai.agent.name``, ``gen_ai.request.model``, ``gen_ai.response.finish_reasons``,
  ``gen_ai.usage.input_tokens`` / ``gen_ai.usage.output_tokens``, ``gen_ai.tool.name``,
  ``gen_ai.tool.call.id``.
- Opt-in content uses the ``gen_ai.content.prompt`` / ``gen_ai.content.completion`` span
  event names from the GenAI conventions.
- Metrics: ``gen_ai.client.token.usage`` (tokens per model call, by ``gen_ai.token.type``)
  and ``gen_ai.client.operation.duration`` (seconds per run, model call, tool call, team
  run and workflow run), with low-cardinality attributes only (no ids or content).

The ``opentelemetry-api`` package is imported lazily inside :func:`instrument`; this module
never sets the global ``TracerProvider`` or ``MeterProvider``.
"""

from __future__ import annotations

import contextlib
import json
import math
import re
import threading
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any

__all__ = ["instrument", "uninstrument", "is_instrumented", "span", "trace"]

# Longest content value recorded on a span event, in characters.
MAX_CONTENT_CHARS = 4000
_INSTRUMENTATION_NAME = "nexura_agents"

# GenAI semantic-convention attribute and event names.
OPERATION_NAME = "gen_ai.operation.name"
AGENT_NAME = "gen_ai.agent.name"
REQUEST_MODEL = "gen_ai.request.model"
FINISH_REASONS = "gen_ai.response.finish_reasons"
USAGE_INPUT = "gen_ai.usage.input_tokens"
USAGE_OUTPUT = "gen_ai.usage.output_tokens"
TOOL_NAME = "gen_ai.tool.name"
TOOL_CALL_ID = "gen_ai.tool.call.id"
CONVERSATION_ID = "gen_ai.conversation.id"
EVENT_PROMPT = "gen_ai.content.prompt"
EVENT_COMPLETION = "gen_ai.content.completion"
# Nexura-specific attributes.
USAGE_REASONING = "nexura.usage.reasoning_tokens"
# No GenAI convention exists for prompt registries; these follow the gen_ai.* naming.
AGENT_PATH = "nexura.agent.path"
LAST_AGENT = "nexura.agent.last"
PROMPT_NAME = "gen_ai.prompt.name"
PROMPT_VERSION = "gen_ai.prompt.version"
# GenAI semantic-convention metric names.
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


_STATE: _State | None = None
_LOCK = threading.Lock()


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
        from opentelemetry.trace import Status, StatusCode
    except ImportError as exc:
        raise ImportError(
            "OpenTelemetry tracing needs opentelemetry-api. "
            'Install it with: pip install "nexura-agents[otel]"'
        ) from exc
    provider = tracer_provider if tracer_provider is not None else trace.get_tracer_provider()
    tracer = provider.get_tracer(_INSTRUMENTATION_NAME)
    token_usage = duration = None
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
        )


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

    def event(self, name: str, attributes: dict[str, Any] | None = None) -> None:
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
        "_error",
        "timed",
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
        self._error: str | None = None
        self.timed = True  # False: end() records no duration

    @property
    def capturing(self) -> bool:
        """True when content capture is on for this span."""
        return self._state.capture_content

    def set(self, key: str, value: Any) -> None:
        if value is None:
            return
        with contextlib.suppress(Exception):
            self._span.set_attribute(key, value)

    def set_usage(self, usage: Any) -> None:
        """Record token totals; fields the provider did not report are skipped."""
        if usage is None:
            return
        self.set(USAGE_INPUT, getattr(usage, "prompt_tokens", None))
        self.set(USAGE_OUTPUT, getattr(usage, "completion_tokens", None))
        self.set(USAGE_REASONING, getattr(usage, "reasoning_tokens", None))
        histogram = self._state.token_usage
        # Only model calls count tokens; run and team spans hold totals of those calls.
        if histogram is None or self._metric.get(OPERATION_NAME) != "chat":
            return
        for token_type, field in (("input", "prompt_tokens"), ("output", "completion_tokens")):
            count = getattr(usage, field, None)
            if isinstance(count, int) and count >= 0:
                with contextlib.suppress(Exception):
                    histogram.record(count, {**self._metric, "gen_ai.token.type": token_type})

    def content(self, event: str, key: str, value: Any) -> None:
        """Record content as a span event, only when ``capture_content`` is on."""
        if not self._state.capture_content:
            return
        with contextlib.suppress(Exception):
            self._span.add_event(event, {key: _render(value)})

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
        attributes = dict(self._metric)
        if self._error is not None:
            attributes["error.type"] = self._error
        with contextlib.suppress(Exception):
            histogram.record(time.monotonic() - self._started, attributes)

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


def start(name: str, attributes: dict[str, Any] | None = None, *, parent: Any = None) -> Any:
    """Start a span, or return a no-op handle when tracing is off.

    ``parent`` is another handle; without it the span nests under the current context.
    """
    state = _STATE
    if state is None:
        return NOOP
    try:
        parent_ctx = parent._ctx if isinstance(parent, _Handle) else None
        span = state.tracer.start_span(name, context=parent_ctx, attributes=attributes or None)
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
    return [m.to_dict() if hasattr(m, "to_dict") else str(m) for m in messages]


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
    return start(f"chat {name}", {OPERATION_NAME: "chat", REQUEST_MODEL: name}, parent=parent)


def start_tool(parent: Any, call: Any, *, pending_approval: bool = False) -> Any:
    handle = start(
        f"execute_tool {call.name}",
        {OPERATION_NAME: "execute_tool", TOOL_NAME: call.name, TOOL_CALL_ID: call.id},
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
