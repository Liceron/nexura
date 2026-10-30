# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Run traces: capture agent-internal events into a run's event log and build a span tree.

Workflow node steps are already recorded as run events. Agent nodes run a Runner inside the
node handler, whose lifecycle events (model calls, tool calls, handoffs, retries) were not
recorded. This module adds:

* ``capture_agent_events`` / ``traced_run``: while a node handler runs, agent ``RunEvent``s are
  appended to the run's event log as ``AGENT_EVENT`` rows. They are redacted with the shared
  server-side redaction, trimmed to small fields and bounded per node.
* ``build_trace`` : a pure function turning a run record into a span tree (workflow node
  steps with model/tool/handoff/retry spans inside agent nodes).
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import time
from collections.abc import AsyncIterator, Iterator, Mapping
from contextvars import ContextVar
from typing import Any

from nexura_runtime.observability.run_diagnostics import describe_run, redact_run_content

AGENT_EVENT = "AGENT_EVENT"
MAX_AGENT_EVENTS_PER_NODE = 200
MAX_TEXT = 2000
MAX_SPANS = 1000

# Lifecycle events worth keeping; text/tool-call deltas and the full message list are dropped.
_KEPT = frozenset(
    {
        "agent_started",
        "model_started",
        "model_completed",
        "tool_started",
        "tool_completed",
        "retry",
        "handoff",
        "run_failed",
        "run_cancelled",
        "run_paused",
        "final_output",
        # P39 G4: which output guardrail stopped a reply (no content in it).
        "guardrail_tripped",
    }
)
_DROP_KEYS = frozenset({"messages", "state", "last_agent", "usage_by_agent", "usage_by_model"})


class AgentEventCapture:
    """Records agent RunEvents for one node attempt, redacted and bounded."""

    def __init__(self, store: Any, run_id: str, node_id: str) -> None:
        self._store = store
        self._run_id = run_id
        self._node_id = node_id
        self._count = 0
        self._truncated = False

    async def record(self, event: Any) -> None:
        kind = str(getattr(getattr(event, "type", None), "value", getattr(event, "type", "")))
        if kind not in _KEPT:
            return
        if self._count >= MAX_AGENT_EVENTS_PER_NODE:
            if not self._truncated:
                self._truncated = True
                await self._store.record_run_event(
                    self._run_id,
                    AGENT_EVENT,
                    node_id=self._node_id,
                    data={"kind": "truncated", "ts": getattr(event, "timestamp", None)},
                )
            return
        self._count += 1
        payload = _slim(getattr(event, "data", {}) or {})
        await self._store.record_run_event(
            self._run_id,
            AGENT_EVENT,
            node_id=self._node_id,
            data={
                "kind": kind,
                "ts": getattr(event, "timestamp", None),
                "agent_run_id": getattr(event, "run_id", None),
                "payload": redact_run_content(payload),
            },
        )


_capture: ContextVar[AgentEventCapture | None] = ContextVar("nexura_agent_capture", default=None)


@contextlib.contextmanager
def capture_agent_events(store: Any, run_id: str, node_id: str) -> Iterator[AgentEventCapture]:
    """Route agent events seen by ``traced_run`` inside this block into the run's event log."""
    capture = AgentEventCapture(store, run_id, node_id)
    token = _capture.set(capture)
    try:
        yield capture
    finally:
        _capture.reset(token)


def _jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _jsonable(dataclasses.asdict(value))
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in list(value)[:100]]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value[:MAX_TEXT] if isinstance(value, str) else value
    return str(value)[:MAX_TEXT]


def _slim(data: Mapping[str, Any]) -> dict[str, Any]:
    out = {k: _jsonable(v) for k, v in data.items() if k not in _DROP_KEYS}
    if "result" in out and not isinstance(out["result"], str):
        out["result"] = json.dumps(out["result"], default=str)[:MAX_TEXT]
    return out


@dataclasses.dataclass
class TracedResult:
    """Subset of a Runner result returned by ``traced_run``."""

    final_output: Any
    turns_count: int
    usage: dict[str, Any] | None


async def traced_run(runner: Any, agent: Any, prompt: Any, **kwargs: Any) -> Any:
    """Run an agent; when called inside a traced node, record its events in the run log.

    Outside a traced node (or without a store) this is exactly ``runner.run_async``.
    """
    capture = _capture.get()
    if capture is None:
        return await runner.run_async(agent, prompt, **kwargs)
    final: Any = None
    turns = 0
    usage: dict[str, Any] | None = None
    stream: AsyncIterator[Any] = runner.stream_async(agent, prompt, **kwargs)
    async for event in stream:
        await capture.record(event)
        kind = str(getattr(getattr(event, "type", None), "value", ""))
        if kind == "run_completed":
            data = event.data
            final = data.get("output_value", data.get("output"))
            turns = int(data.get("turns", 0) or 0)
            raw = data.get("usage")
            usage = _jsonable(raw) if raw is not None else None
    return TracedResult(final_output=final, turns_count=turns, usage=usage)


# --------------------------------------------------------------------------------------
# Span tree
# --------------------------------------------------------------------------------------


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _tok(usage: Any) -> dict[str, int | None]:
    if not isinstance(usage, dict):
        return {"input_tokens": None, "output_tokens": None, "total_tokens": None}

    def pick(*keys: str) -> int | None:
        for key in keys:
            val = usage.get(key)
            if isinstance(val, int) and not isinstance(val, bool) and val >= 0:
                return val
        return None

    inp = pick("prompt_tokens", "input_tokens")
    out = pick("completion_tokens", "output_tokens")
    total = pick("total_tokens")
    if total is None and (inp is not None or out is not None):
        total = (inp or 0) + (out or 0)
    return {"input_tokens": inp, "output_tokens": out, "total_tokens": total}


def _add(a: int | None, b: int | None) -> int | None:
    return b if a is None else (a if b is None else a + b)


def _red(value: Any) -> Any:
    """Redact any value (string or structure) with the shared run-content redaction."""
    return redact_run_content(value) if value is not None else None


def _end(span: dict[str, Any], end_ms: float | None) -> None:
    start_ms = span.get("start_ms")
    if start_ms is not None and end_ms is not None:
        span["duration_ms"] = round(max(end_ms - start_ms, 0.0), 2)


def _new_span(
    span_id: str, kind: str, name: str, parent: str, start_ms: float | None, **extra: Any
) -> dict[str, Any]:
    return {
        "span_id": span_id,
        "parent_id": parent,
        "kind": kind,
        "name": name,
        "status": "running",
        "start_ms": start_ms,
        "duration_ms": None,
        "input_tokens": None,
        "output_tokens": None,
        "total_tokens": None,
        "cost_usd": None,
        "error": None,
        "input": None,
        "output": None,
        **extra,
    }


def build_trace(
    run: Mapping[str, Any],
    *,
    ledger: Any | None = None,
    pricing_table: Any | None = None,
    include_content: bool = False,
    retention_seconds: float | None = None,
) -> dict[str, Any]:
    """Build a span tree for a run record. Content is redacted and only included on request.

    Runs older than ``retention_seconds`` have all content suppressed, like diagnostics.
    """
    run_id = str(run["run_id"])
    created = _num(run.get("created_at"))
    purged = (
        retention_seconds is not None
        and created is not None
        and (time.time() - created > retention_seconds)
    )
    if purged:
        include_content = False
    red = _red
    events = sorted(run.get("events", []), key=lambda e: e.get("seq", 0))
    t0 = _num(run.get("created_at"))
    if t0 is None and events:
        t0 = _num(events[0].get("timestamp")) or 0.0
    t0 = t0 or 0.0

    described = describe_run(
        run,
        ledger=ledger,
        pricing_table=pricing_table,
        include_content=include_content,
        retention_seconds=retention_seconds,
    )
    attempts = described.get("attempts", [])
    spans: list[dict[str, Any]] = []
    truncated = False

    def ms(ts: float | None) -> float | None:
        return None if ts is None else round(max(ts - t0, 0.0) * 1000, 2)

    last_ts = max((_num(e.get("timestamp")) or t0 for e in events), default=t0)
    status = str(run.get("status", ""))
    terminal = status in {"completed", "failed", "cancelled", "rejected", "interrupted"}
    run_end = _num(run.get("updated_at")) if terminal else last_ts
    run_status = (
        "ok" if status == "completed" else "error" if status in {"failed", "rejected"} else status
    )
    root = {
        "span_id": "root",
        "parent_id": None,
        "kind": "run",
        "name": red(f"Workflow run {run_id}"),
        "status": "error" if run_status == "error" else ("ok" if run_status == "ok" else "running"),
        "start_ms": 0.0,
        "duration_ms": ms(run_end),
        "error": red(run.get("error")) if include_content else None,
    }
    spans.append(root)

    # Node spans from the diagnostic attempts.
    node_spans: list[dict[str, Any]] = []
    for att in attempts:
        seq = int(str(att["id"]).rsplit("-", 1)[-1]) if str(att["id"]).startswith("event-") else 0
        start = _num(att.get("started_at"))
        span = {
            "span_id": f"node-{seq}",
            "parent_id": "root",
            "kind": "node",
            "name": red(att.get("node_id", "node")),
            "status": {"completed": "ok", "failed": "error"}.get(att.get("status"), "running"),
            "start_ms": ms(start),
            "duration_ms": att.get("duration_ms"),
            "input_tokens": att.get("input_tokens"),
            "output_tokens": att.get("output_tokens"),
            "total_tokens": att.get("total_tokens"),
            "cost_usd": att.get("calculated_cost_usd"),
            "model_id": red(att.get("model_id")),
            "module_id": red(att.get("module_id")),
            "error": None if purged else red(att.get("error")),
            "input": None if purged else red(att.get("input")),
            "output": None if purged else red(att.get("output")),
            "_seq": seq,
            "_node": att.get("node_id"),
        }
        node_spans.append(span)
    spans.extend(node_spans)

    # Agent-internal spans.
    open_model: dict[str, dict[str, Any]] = {}
    open_tool: dict[str, dict[str, Any]] = {}
    agent_span: dict[str, dict[str, Any]] = {}
    counter = 0
    for ev in events:
        if ev.get("type") != AGENT_EVENT:
            continue
        data = ev.get("data") or {}
        kind = data.get("kind")
        node_id = ev.get("node_id")
        seq = int(ev.get("seq", 0))
        parent_node = next(
            (s for s in reversed(node_spans) if s["_node"] == node_id and s["_seq"] <= seq),
            None,
        )
        if parent_node is None:
            continue
        if len(spans) >= MAX_SPANS:
            truncated = True
            break
        ts = _num(data.get("ts")) or _num(ev.get("timestamp"))
        payload = data.get("payload") or {}
        pid = parent_node["span_id"]
        key = pid
        cur_agent = agent_span.get(key)
        counter += 1
        sid = f"{pid}-a{counter}"

        def end(span: dict[str, Any], at: float | None) -> None:
            _end(span, ms(at))

        def new(
            kind_: str,
            name: str,
            parent: str,
            _sid: str = sid,
            _ts: float | None = ts,
            **extra: Any,
        ) -> dict[str, Any]:
            span = _new_span(_sid, kind_, name, parent, ms(_ts), **extra)
            spans.append(span)
            return span

        parent_for_leaf = cur_agent["span_id"] if cur_agent else pid
        if kind == "agent_started":
            if cur_agent is not None:
                end(cur_agent, ts)
                cur_agent["status"] = (
                    "ok" if cur_agent["status"] == "running" else cur_agent["status"]
                )
            agent_span[key] = new("agent", red(str(payload.get("agent", "agent"))), pid)
        elif kind == "model_started":
            open_model[f"{key}:{payload.get('turn')}"] = new(
                "model",
                red(f"Model call (turn {payload.get('turn')})"),
                parent_for_leaf,
                input={"messages_count": payload.get("messages_count")}
                if include_content
                else None,
            )
        elif kind == "model_completed":
            model_span = open_model.pop(f"{key}:{payload.get('turn')}", None)
            if model_span is not None:
                end(model_span, ts)
                model_span["status"] = "ok"
                model_span.update(_tok(payload.get("usage")))
                if include_content:
                    model_span["output"] = {
                        "finish_reason": red(payload.get("finish_reason")),
                        "has_tool_calls": payload.get("has_tool_calls"),
                    }
        elif kind == "tool_started":
            tool_name = red(str(payload.get("tool_name", "tool")))
            open_tool[f"{key}:{payload.get('tool_call_id')}"] = new(
                "tool",
                tool_name,
                parent_for_leaf,
                input=red(payload.get("arguments")) if include_content else None,
                tool_call_id=red(payload.get("tool_call_id")),
            )
        elif kind == "tool_completed":
            done_tool = open_tool.pop(f"{key}:{payload.get('tool_call_id')}", None)
            if done_tool is not None:
                end(done_tool, ts)
                if done_tool["status"] != "error":
                    done_tool["status"] = "ok"
                if include_content:
                    done_tool["output"] = red(payload.get("result"))
        elif kind == "retry":
            tool_span = open_tool.get(f"{key}:{payload.get('tool_call_id')}")
            if tool_span is not None:
                tool_span["status"] = "error"
            retry = new(
                "retry",
                red(f"Retry: {payload.get('reason', 'retry')}"),
                parent_for_leaf,
                duration_ms=0.0,
            )
            retry["status"] = "error"
            retry["error"] = (
                red(str(payload.get("error") or payload.get("reason"))) if include_content else None
            )
        elif kind == "handoff":
            handoff = new(
                "handoff",
                red(f"Handoff {payload.get('from')} to {payload.get('to')}"),
                parent_for_leaf,
                duration_ms=0.0,
            )
            handoff["status"] = "ok"
            if include_content:
                handoff["output"] = {"reason": red(payload.get("reason"))}
        elif kind in {"run_failed", "run_cancelled"}:
            for open_span in [cur_agent, *open_model.values(), *open_tool.values()]:
                if open_span is not None and open_span["status"] == "running":
                    open_span["status"] = "error"
                    end(open_span, ts)
            fail = new("error", red(str(payload.get("error_type") or kind)), parent_for_leaf)
            fail["status"] = "error"
            fail["duration_ms"] = 0.0
            fail["error"] = red(str(payload.get("error"))) if include_content else None
        elif kind == "final_output" and cur_agent is not None:
            end(cur_agent, ts)
            cur_agent["status"] = "ok"
            if include_content:
                cur_agent["output"] = red(payload.get("output"))
        elif kind == "truncated":
            new("note", "Agent events truncated", parent_for_leaf, status="ok")
        else:
            counter -= 1

    # Roll model usage up to the agent span and to the node (unless it reported its own).
    by_id = {s["span_id"]: s for s in spans}
    own_usage = {s["span_id"] for s in node_spans if s.get("total_tokens") is not None}
    for span in spans:
        if span["kind"] != "model":
            continue
        up = by_id.get(span["parent_id"])
        targets: list[dict[str, Any]] = []
        if up is not None and up["kind"] == "agent":
            targets.append(up)
            up = by_id.get(up["parent_id"])
        if up is not None and up["kind"] == "node" and up["span_id"] not in own_usage:
            targets.append(up)
        for target in targets:
            for field in ("input_tokens", "output_tokens", "total_tokens"):
                target[field] = _add(target.get(field), span.get(field))
    for span in spans:
        for field in ("input_tokens", "output_tokens", "total_tokens", "cost_usd"):
            span.setdefault(field, None)
        span.setdefault("error", None)
        span.setdefault("input", None)
        span.setdefault("output", None)
        span["has_content"] = span["input"] is not None or span["output"] is not None
        span.pop("_seq", None)
        span.pop("_node", None)
    # Unfinished spans of a finished run were interrupted, not still running.
    if terminal:
        for span in spans:
            if span["status"] == "running":
                span["status"] = "error" if run_status == "error" else "unknown"

    totals: dict[str, int | None] = {"input_tokens": None, "output_tokens": None}
    for span in spans:
        if span["kind"] == "model" or (
            span["kind"] == "node" and not any(s["parent_id"] == span["span_id"] for s in spans)
        ):
            totals["input_tokens"] = _add(totals["input_tokens"], span.get("input_tokens"))
            totals["output_tokens"] = _add(totals["output_tokens"], span.get("output_tokens"))
    root["input_tokens"] = totals["input_tokens"]
    root["output_tokens"] = totals["output_tokens"]
    root["total_tokens"] = (
        None
        if totals["input_tokens"] is None and totals["output_tokens"] is None
        else (totals["input_tokens"] or 0) + (totals["output_tokens"] or 0)
    )
    root["cost_usd"] = None
    root["has_content"] = False
    costs = [s["cost_usd"] for s in spans if s["kind"] == "node" and s.get("cost_usd") is not None]
    if costs:
        from decimal import Decimal

        root["cost_usd"] = str(sum((Decimal(str(c)) for c in costs), Decimal(0)))

    return {
        "run_id": run_id,
        "status": status,
        "include_content": include_content,
        "truncated": truncated,
        "span_count": len(spans),
        "duration_ms": root["duration_ms"],
        "spans": spans,
    }
