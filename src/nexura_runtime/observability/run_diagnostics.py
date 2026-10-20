# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Inspect persisted workflow events using the SDK trace/export contracts."""

from __future__ import annotations

import math
import re
import time
from collections.abc import Mapping
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from nexura_agents.network.redaction import _TEXT_RULES as _TEXT_RULES
from nexura_agents.network.redaction import redact_secret_text as redact_secret_text
from nexura_agents.usage.models import TokenUsage
from nexura_agents.usage.pricing import PricingTable

from nexura_runtime.integrations.nexura_v2 import (
    TraceMetadata,
    TraceValidationError,
    build_trace_envelope,
)
from nexura_runtime.observability.models import SpanRecord

_TERMINAL = {"completed", "failed", "cancelled", "rejected", "interrupted"}
# Normalised (snake_case, lower) names that are never secrets even though they contain a
# secret-looking word.
_NON_SECRET_FIELDS = frozenset(
    {
        "token_limit",
        "max_tokens",
        "token_count",
        "tokens",
        "primary_key",
        "foreign_key",
        "key_attribute",
        "sort_key",
        "partition_key",
        "cache_key",
        "session_key",
        "idempotency_key",
        "public_key",
        "prompt_tokens",
        "completion_tokens",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "reasoning_tokens",
        "cached_tokens",
        "token_usage",
        "token_budget",
        "token_type",
        "max_token",
    }
)
# Words that make a key secret when they appear as a whole word of the normalised name.
_SECRET_WORDS = frozenset(
    {
        "password",
        "passwd",
        "passphrase",
        "passcode",
        "pwd",
        "secret",
        "secrets",
        "token",
        "credential",
        "credentials",
        "authorization",
        "cookie",
        "jwt",
        "bearer",
        "apikey",
        "auth",
    }
)
# Compound spellings that may arrive without separators (ACCESSTOKEN, clientsecret).
_SECRET_COMPOUNDS = (
    "apikey",
    "accesstoken",
    "refreshtoken",
    "authtoken",
    "idtoken",
    "sessiontoken",
    "bearertoken",
    "clientsecret",
    "secretkey",
    "privatekey",
    "signingkey",
    "encryptionkey",
    "accesskey",
    "setcookie",
    "xapikey",
    "xauthtoken",
    "password",
    "passphrase",
    "credential",
    "authorization",
)
_CAMEL_1 = re.compile(r"([a-z0-9])([A-Z])")
_CAMEL_2 = re.compile(r"([A-Z]+)([A-Z][a-z])")
_NON_ALNUM = re.compile(r"[^A-Za-z0-9]+")


def _normalise_key(key: str) -> str:
    """camelCase, kebab-case, dotted and spaced names -> lower snake_case."""
    key = _CAMEL_2.sub(r"\1_\2", _CAMEL_1.sub(r"\1_\2", str(key)))
    return _NON_ALNUM.sub("_", key).strip("_").lower()


_MICRO_CENT = Decimal("0.00000001")


def _is_secret_key(key: str) -> bool:
    norm = _normalise_key(key)
    if not norm or norm in _NON_SECRET_FIELDS:
        return False
    words = norm.split("_")
    if any(w in _SECRET_WORDS for w in words):
        return True
    compact = norm.replace("_", "")
    if any(c in compact for c in _SECRET_COMPOUNDS):
        return True
    # "<anything>_key" (e.g. signing_key) is treated as a credential, as before.
    return len(words) > 1 and words[-1] == "key"


def is_secret_key(key: str) -> bool:
    """Public form of the credential-field-name check shared by run events and exports."""
    return _is_secret_key(key)


def redact_run_content(value: Any, depth: int = 0) -> Any:
    """Bounded credential-field redaction; not a general personal-data detector."""
    if depth > 16:
        return "[DEPTH LIMIT]"
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]"
            if _is_secret_key(str(key))
            else redact_run_content(item, depth + 1)
            for key, item in list(value.items())[:100]
        }
    if isinstance(value, list):
        result = [redact_run_content(item, depth + 1) for item in value[:100]]
        if len(value) > 100:
            result.append("[LIST TRUNCATED]")
        return result
    if isinstance(value, str):
        value = redact_secret_text(value)
        return value[:8000] + (" [TEXT TRUNCATED]" if len(value) > 8000 else "")
    if isinstance(value, float) and not math.isfinite(value):
        return "[NONFINITE VALUE]"
    return value


def _number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if math.isfinite(value) and value >= 0 else None
    return None


def _tokens(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def describe_run(
    run: Mapping[str, Any],
    *,
    ledger: Any | None = None,
    pricing_table: PricingTable | None = None,
    include_content: bool = False,
    retention_seconds: float | None = None,
) -> dict[str, Any]:
    """Authoritative SDK projection for run diagnostics, usage, and cost accounting.

    Links run, node, attempt, provider-call, reservation, approval, and effect IDs.
    Separates observed usage, calculated costs, operator estimates, and uncertainty.
    Module aggregates are never counted as new provider usage or double-settled.
    Missing usage is never rendered as zero.
    """
    run_id = str(run["run_id"])
    created_at = _number(run.get("created_at"))
    now = time.time()

    # Configured retention check
    is_purged_by_retention = False
    if (
        retention_seconds is not None
        and created_at is not None
        and (now - created_at) > retention_seconds
    ):
        is_purged_by_retention = True

    content_retention_status = (
        "purged" if is_purged_by_retention else ("active" if include_content else "omitted")
    )

    attempts: list[dict[str, Any]] = []
    active: dict[tuple[str, str, str, str], dict[str, Any]] = {}

    # Sort events deterministically and deduplicate repeated sequence numbers
    raw_events = list(run.get("events", []))
    events = sorted(
        raw_events,
        key=lambda item: (item.get("seq", 0), _number(item.get("timestamp")) or 0),
    )

    seen_seqs: set[int] = set()
    deduped_events: list[dict[str, Any]] = []
    for event in events:
        seq = event.get("seq")
        if seq is not None and seq in seen_seqs:
            continue
        if seq is not None:
            seen_seqs.add(seq)
        deduped_events.append(event)

    for event in deduped_events:
        event_type = event.get("type", "")
        if event_type not in {
            "NODE_STARTED",
            "NODE_COMPLETED",
            "NODE_FAILED",
            "MODULE_INVOKE_STARTED",
            "MODULE_INVOKE_COMPLETED",
            "MODULE_INVOKE_FAILED",
        }:
            continue

        data = event.get("data") or {}
        module = event_type.startswith("MODULE_")
        node = str(event.get("node_id") or data.get("node_id") or "unknown")
        mod_id = data.get("module_id")
        att_id = str(data.get("attempt_id") or f"{node}-{event.get('seq', len(attempts))}")

        attempt_key = (
            "module" if module else "handler",
            node,
            str(mod_id or ""),
            str(data.get("attempt_id") or ""),
        )
        payload = data.get("payload", {}) if module else data
        stamp = _number(data.get("emitted_at"))
        if stamp is None:
            stamp = _number(event.get("timestamp"))

        # Link IDs across lifecycle events
        provider_call_id = (
            data.get("provider_call_id")
            or payload.get("provider_call_id")
            or (
                payload.get("metadata", {}).get("provider_call_id")
                if isinstance(payload.get("metadata"), dict)
                else None
            )
        )
        reservation_id = (
            data.get("reservation_id")
            or payload.get("reservation_id")
            or (
                payload.get("metadata", {}).get("reservation_id")
                if isinstance(payload.get("metadata"), dict)
                else None
            )
        )
        approval_id = (
            data.get("approval_id") or payload.get("approval_id") or run.get("approval_id")
        )
        effect_id = (
            data.get("effect_id")
            or payload.get("effect_id")
            or data.get("action")
            or payload.get("action")
        )
        is_aggregate = bool(
            data.get("is_aggregate")
            or payload.get("is_aggregate")
            or data.get("entry_category") == "module_aggregate"
            or payload.get("entry_category") == "module_aggregate"
        )
        model_id = (
            data.get("model_id")
            or payload.get("model_id")
            or (
                payload.get("metadata", {}).get("model_id")
                if isinstance(payload.get("metadata"), dict)
                else None
            )
        )
        ctx_dec = (
            data.get("context_decision")
            or payload.get("context_decision")
            or (
                payload.get("metadata", {}).get("context_decision")
                if isinstance(payload.get("metadata"), dict)
                else None
            )
        )
        if ctx_dec is not None and hasattr(ctx_dec, "to_dict"):
            ctx_dec = ctx_dec.to_dict()

        if event_type.endswith("STARTED"):
            in_content: Any = None
            if is_purged_by_retention:
                in_content = "[CONTENT PURGED BY RETENTION POLICY]"
            elif include_content:
                in_content = redact_run_content(payload.get("inputs", payload.get("input_state")))

            row: dict[str, Any] = {
                "id": f"event-{event.get('seq', len(attempts))}",
                "run_id": run_id,
                "node_id": node,
                "identity_source": "sdk_node" if module else "legacy_handler",
                "module_id": mod_id,
                "attempt_id": att_id,
                "provider_call_id": provider_call_id,
                "reservation_id": reservation_id,
                "approval_id": approval_id,
                "effect_id": effect_id,
                "model_id": model_id,
                "is_aggregate": is_aggregate,
                "started_at": stamp,
                "ended_at": None,
                "duration_ms": None,
                "status": "running",
                "input_tokens": None,
                "output_tokens": None,
                "total_tokens": None,
                "cached_input_tokens": None,
                "cache_creation_tokens": None,
                "usage_reported": False,
                "usage_complete": False,
                "calculated_cost_usd": None,
                "cost_state": None,
                "input": in_content,
                "output": None,
                "error": None,
                "context_decision": ctx_dec,
            }
            attempts.append(row)
            active[attempt_key] = row
            continue

        completed_row = active.pop(attempt_key, None)
        if completed_row is None:
            continue
        row = completed_row
        row["ended_at"] = stamp
        if stamp is not None and row["started_at"] is not None and stamp >= row["started_at"]:
            row["duration_ms"] = round((stamp - row["started_at"]) * 1000, 2)
        failed = event_type.endswith("FAILED")
        row["status"] = "failed" if failed else "completed"

        # Update linked IDs from completion payload if missing
        if not row["provider_call_id"]:
            row["provider_call_id"] = provider_call_id
        if not row["reservation_id"]:
            row["reservation_id"] = reservation_id
        if not row["approval_id"]:
            row["approval_id"] = approval_id
        if not row["effect_id"]:
            row["effect_id"] = effect_id
        if not row["model_id"]:
            row["model_id"] = model_id
        if is_aggregate:
            row["is_aggregate"] = True
        if not row.get("context_decision") and ctx_dec:
            row["context_decision"] = ctx_dec

        def _get_val(d: dict[str, Any], *keys: str) -> Any:
            for k in keys:
                if k in d and d[k] is not None:
                    return d[k]
            return None

        usage = payload.get("usage") if module else None
        if isinstance(usage, dict):
            row["input_tokens"] = _tokens(_get_val(usage, "prompt_tokens", "input_tokens"))
            row["output_tokens"] = _tokens(_get_val(usage, "completion_tokens", "output_tokens"))
            row["cached_input_tokens"] = _tokens(
                _get_val(usage, "cached_prompt_tokens", "cached_input_tokens")
            )
            row["cache_creation_tokens"] = _tokens(
                _get_val(usage, "cache_write_tokens", "cache_creation_tokens")
            )
            total_t = _tokens(_get_val(usage, "total_tokens"))
            if total_t is None and row["input_tokens"] is not None:
                total_t = (row["input_tokens"] or 0) + (row["output_tokens"] or 0)
            row["total_tokens"] = total_t

            row["usage_reported"] = (
                row["input_tokens"] is not None
                or row["output_tokens"] is not None
                or row["total_tokens"] is not None
            )
            row["usage_complete"] = (
                row["input_tokens"] is not None and row["output_tokens"] is not None
            )

        if is_purged_by_retention:
            row["error"] = "[CONTENT PURGED BY RETENTION POLICY]" if failed else None
            row["output"] = "[CONTENT PURGED BY RETENTION POLICY]"
        else:
            row["error"] = (
                redact_run_content(payload.get("error_message", payload.get("error")))
                if failed and include_content
                else ("Node execution failed" if failed else None)
            )
            if include_content:
                row["output"] = redact_run_content(
                    payload.get("outputs", payload.get("state_update"))
                )

    # Correlate with ledger entries if ledger is available
    if ledger is not None:
        try:
            ledger_entries = ledger.get_entries(run_id=run_id)
            seen_entry_ids: set[str] = set()
            unique_entries = []
            for e in ledger_entries:
                if e.entry_id not in seen_entry_ids:
                    seen_entry_ids.add(e.entry_id)
                    unique_entries.append(e)

            for row in attempts:
                if row.get("is_aggregate"):
                    continue
                matched_entry = None
                for e in unique_entries:
                    if e.entry_category == "module_aggregate":
                        continue
                    if row["provider_call_id"] and e.provider_call_id == row["provider_call_id"]:
                        matched_entry = e
                        break
                    if row["reservation_id"] and e.reservation_id == row["reservation_id"]:
                        matched_entry = e
                        break
                    if (
                        row["node_id"]
                        and e.node_id == row["node_id"]
                        and e.entry_type in ("reconcile", "commit")
                    ):
                        matched_entry = e
                        break
                if matched_entry:
                    row["provider_call_id"] = (
                        matched_entry.provider_call_id or row["provider_call_id"]
                    )
                    row["reservation_id"] = matched_entry.reservation_id or row["reservation_id"]
                    row["cost_state"] = matched_entry.usage_state
                    row["calculated_cost_usd"] = str(matched_entry.amount_usd)
                    if matched_entry.model_id:
                        row["model_id"] = matched_entry.model_id
        except Exception:
            pass

    # Price uncosted attempts with operator PricingTable
    if pricing_table is not None:
        for row in attempts:
            if row.get("is_aggregate"):
                continue
            if row.get("calculated_cost_usd") is not None:
                continue
            m_id = row.get("model_id")
            if m_id and pricing_table.get_pricing(m_id):
                if row["usage_reported"]:
                    tu = TokenUsage(
                        prompt_tokens=row["input_tokens"],
                        completion_tokens=row["output_tokens"],
                        total_tokens=row["total_tokens"],
                        cached_prompt_tokens=row.get("cached_input_tokens"),
                        state="reported" if row["usage_complete"] else "estimated",
                        is_complete=row["usage_complete"],
                    )
                    cost_est = pricing_table.calculate_cost(m_id, tu)
                    row["calculated_cost_usd"] = str(cost_est.amount_usd)
                    row["cost_state"] = cost_est.state
                else:
                    cost_est = pricing_table.calculate_cost(
                        m_id,
                        None,
                        conservative_fallback_cost=Decimal("0.005"),
                    )
                    row["calculated_cost_usd"] = str(cost_est.amount_usd)
                    row["cost_state"] = "unknown"
            elif m_id:
                row["calculated_cost_usd"] = None
                row["cost_state"] = "unpriced"

    shown = attempts[-200:]
    status = str(run["status"])
    start = created_at
    end = _number(run.get("updated_at")) if status in _TERMINAL else None
    trace = None
    unavailable = "Run must finish before trace export."
    if len(attempts) > 200:
        unavailable = "Trace export unavailable: diagnostic attempt limit exceeded."
    elif start is not None and end is not None and end >= start:
        spans = [
            SpanRecord(
                trace_id=run_id,
                span_id="root",
                name="Workflow run",
                start_time=start,
                end_time=end,
                status="ok" if status == "completed" else "error",
            )
        ]
        for row in attempts:
            if row["started_at"] is None or row["ended_at"] is None:
                continue
            attrs: dict[str, Any] = {"nexura.span.type": "chain"}
            for key, field in [
                ("gen_ai.usage.input_tokens", "input_tokens"),
                ("gen_ai.usage.output_tokens", "output_tokens"),
            ]:
                if row[field] is not None:
                    attrs[key] = row[field]
            if row["module_id"] and str(row["module_id"]).startswith("nexura.models."):
                attrs["nexura.span.type"] = "llm"
            spans.append(
                SpanRecord(
                    trace_id=run_id,
                    span_id=row["id"],
                    parent_span_id="root",
                    name=row["node_id"][:512],
                    start_time=row["started_at"],
                    end_time=row["ended_at"],
                    status="error" if row["status"] == "failed" else "ok",
                    attributes=attrs,
                    error="Node execution failed" if row["status"] == "failed" else None,
                )
            )
        if len(spans) != len(attempts) + 1:
            unavailable = "Trace export unavailable: some attempts have incomplete timing."
        else:
            try:
                trace = build_trace_envelope(spans, TraceMetadata(f"Studio run {run_id}"))
                unavailable = ""
            except TraceValidationError:
                unavailable = "Trace export unavailable: recorded trace failed validation."

    # Module aggregates separation: module aggregates are not counted as new provider usage
    provider_attempts = [a for a in attempts if not a.get("is_aggregate")]

    known_in = sum(a["input_tokens"] or 0 for a in provider_attempts)
    known_out = sum(a["output_tokens"] or 0 for a in provider_attempts)
    known_tot = sum(a["total_tokens"] or 0 for a in provider_attempts)
    cached_in = sum(a["cached_input_tokens"] or 0 for a in provider_attempts)
    cache_create = sum(a["cache_creation_tokens"] or 0 for a in provider_attempts)
    rep_attempts = sum(a["usage_reported"] for a in provider_attempts)
    unrep_attempts = sum(not a["usage_reported"] for a in provider_attempts)
    complete = bool(provider_attempts) and all(a["usage_complete"] for a in provider_attempts)

    # Cost totals across all cohort attempts
    reported_cost = Decimal("0")
    estimated_cost = Decimal("0")
    uncertain_cost = Decimal("0")
    has_any_cost = False

    for a in provider_attempts:
        cost_str = a.get("calculated_cost_usd")
        if cost_str is not None:
            has_any_cost = True
            c_dec = Decimal(cost_str)
            st = a.get("cost_state")
            if st == "reported":
                reported_cost += c_dec
            elif st == "estimated":
                estimated_cost += c_dec
            else:
                uncertain_cost += c_dec

    total_cost_dec = (reported_cost + estimated_cost + uncertain_cost) if has_any_cost else None

    cost_float = float(total_cost_dec) if total_cost_dec is not None else None
    calculated_cost_usd = (
        str(total_cost_dec.quantize(_MICRO_CENT, rounding=ROUND_HALF_UP))
        if total_cost_dec is not None
        else None
    )

    if not has_any_cost:
        cost_status = "not_reported"
    elif unrep_attempts > 0 or uncertain_cost > Decimal("0"):
        cost_status = "partial"
    elif estimated_cost > Decimal("0"):
        cost_status = "estimated"
    else:
        cost_status = "reported"

    # Task success & successful-task cost (Deliverable 6)
    cohort_count = len(provider_attempts)
    success_count = sum(1 for a in provider_attempts if a["status"] == "completed")

    if success_count > 0 and total_cost_dec is not None:
        st_cost_dec = (total_cost_dec / Decimal(success_count)).quantize(
            _MICRO_CENT, rounding=ROUND_HALF_UP
        )
        successful_task_cost_usd: str | None = str(st_cost_dec)
        successful_task_cost_status = "defined"
    elif success_count == 0:
        successful_task_cost_usd = None
        successful_task_cost_status = "undefined_zero_successes"
    else:
        successful_task_cost_usd = None
        successful_task_cost_status = "unpriced"

    cov_ratio = round(rep_attempts / cohort_count, 4) if cohort_count > 0 else 1.0
    cov_expl = (
        f"{unrep_attempts} of {cohort_count} attempts did not report token usage; "
        f"unreported calls are tracked as uncertain, not free."
        if unrep_attempts > 0
        else ""
    )

    return {
        "run_id": run_id,
        "status": status,
        "source": "persisted_run_events",
        "content_included": include_content,
        "content_retention_status": content_retention_status,
        "attempt_count": len(attempts),
        "truncated_attempts": max(0, len(attempts) - len(shown)),
        "attempts": shown,
        "context_decisions": [a["context_decision"] for a in attempts if a.get("context_decision")],
        "usage": {
            "aggregation": "module_result_tally",
            "known_input_tokens": known_in,
            "known_output_tokens": known_out,
            "total_tokens": known_tot,
            "cached_input_tokens": cached_in,
            "cache_creation_tokens": cache_create,
            "reported_attempts": rep_attempts,
            "unreported_attempts": unrep_attempts,
            "complete": complete,
            "cost": cost_float,
            "cost_status": cost_status,
            "calculated_cost_usd": calculated_cost_usd,
            "reported_cost_usd": str(reported_cost.quantize(_MICRO_CENT, rounding=ROUND_HALF_UP)),
            "estimated_cost_usd": str(estimated_cost.quantize(_MICRO_CENT, rounding=ROUND_HALF_UP)),
            "uncertain_cost_usd": str(uncertain_cost.quantize(_MICRO_CENT, rounding=ROUND_HALF_UP)),
            "currency": "USD",
            "pricing_version": pricing_table.version if pricing_table else None,
            "is_provider_invoice": False,
            "disclaimer": (
                "Calculated costs are model-rate estimates based on operator pricing tables "
                "and do not represent reconciled provider invoices or billing statements."
            ),
            "coverage_ratio": cov_ratio,
            "unknown_usage_count": unrep_attempts,
            "incomplete_coverage_explanation": cov_expl,
            "cohort_attempt_count": cohort_count,
            "task_success_count": success_count,
            "successful_task_cost_usd": successful_task_cost_usd,
            "successful_task_cost_status": successful_task_cost_status,
        },
        "trace_export": trace,
        "trace_export_unavailable_reason": unavailable,
    }
