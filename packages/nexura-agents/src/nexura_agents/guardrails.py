# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Input and output guardrails that can stop a run (a "tripwire").

A guardrail is any function ``fn(ctx, value) -> GuardrailResult`` (sync or async).
Input guardrails receive the user's prompt text before the first model call. Output
guardrails receive the final output (the validated ``output_type`` object when one is
set, otherwise the text) before the run completes.

Tool guardrails (``@tool(input_guardrails=..., output_guardrails=...)``) receive a tool
call's arguments before it runs and its result text after; they can also reject a call
(``GuardrailResult.rejected``), which tells the model instead of stopping the run.
"""

from __future__ import annotations

import inspect
import math
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from nexura_agents.run_context import RunContext


@dataclass(frozen=True)
class GuardrailResult:
    """Outcome of one guardrail check. ``tripwire=True`` stops the run."""

    tripwire: bool
    info: Any = None
    # Output guardrails only: replaces the final text output when set (see ``rewritten``).
    replacement: str | None = None
    # Tool guardrails only: block the call (or its result) and tell the model this instead.
    rejection: str | None = None

    @classmethod
    def passed(cls, info: Any = None) -> GuardrailResult:
        """Build a result that lets the run continue."""
        return cls(tripwire=False, info=info)

    @classmethod
    def tripped(cls, info: Any = None) -> GuardrailResult:
        """Build a result whose tripwire stops the run."""
        return cls(tripwire=True, info=info)

    @classmethod
    def rewritten(cls, replacement: str, info: Any = None) -> GuardrailResult:
        """Build a non-tripping result that replaces the final text output.

        Only output guardrails may rewrite, and only text output (no ``output_type``).
        Later guardrails see the rewritten text. The replacement is what the caller
        receives and what is stored in the session.
        """
        return cls(tripwire=False, info=info, replacement=replacement)

    @classmethod
    def rejected(cls, message: str, info: Any = None) -> GuardrailResult:
        """Tool guardrails only: do not run the call (input) or withhold its result
        (output); the model is told ``message`` and the run continues."""
        return cls(tripwire=False, info=info, rejection=message)


GuardrailFn = Callable[[RunContext[Any], Any], "GuardrailResult | Awaitable[GuardrailResult]"]


async def run_guardrail(
    fn: GuardrailFn, ctx: RunContext[Any], value: Any, *, tool_stage: bool = False
) -> GuardrailResult:
    result = fn(ctx, value)
    if inspect.isawaitable(result):
        result = await result
    name = getattr(fn, "__name__", repr(fn))
    if not isinstance(result, GuardrailResult):
        raise TypeError(f"Guardrail '{name}' must return GuardrailResult, got {type(result)}.")
    if result.rejection is not None and not tool_stage:
        raise TypeError(
            f"Guardrail '{name}' returned rejected(); only tool guardrails can reject a call."
        )
    return result


# ---------------------------------------------------------------- decision records

#: ``on_decision(record)``: called with a redacted ``decision_record`` per guardrail outcome.
DecisionSink = Callable[[dict[str, Any]], None]

# Values copied from ``GuardrailResult.info`` into decision records: identifier-like
# tokens only (letters, digits and ``_ . : / + -``, at most 128 characters), so free text
# such as a judge's reason, a rejection message or the checked content can never be
# copied. Tokens that look like a detected value are dropped too: anything with ``@`` (an
# email), a run of 6 or more digits, 9 or more digits joined only by ``-``, ``.`` or spaces
# (formatted card, phone and account numbers), a long unbroken letter-digit run or a known
# key prefix (API keys and tokens).
_TOKEN = re.compile(r"[A-Za-z0-9_.:/+-]{1,128}")
_DIGIT_RUN = re.compile(r"\d{6,}")
# 9+ digits where single separators (- . space) between them are ignored: 4111-1111-...,
# 123-456-7890, 98765.43210. Model ids such as llama-guard3:1b or qwen3:8b stay.
_SEPARATED_DIGITS = re.compile(r"\d(?:[-. ]?\d){8,}")
_LONG_RUN = re.compile(r"[A-Za-z0-9]{33,}")
_KEY_SHAPED = re.compile(
    r"(?i)(?:^|[^a-z0-9])(?:sk|pk|rk|ghp|gho|ghu|ghs|ghr|github_pat|xox[abpr]|akia|asia)[-_]?"
    r"[a-z0-9]{8,}"
)
_TOKEN_KEYS = ("backend", "rule", "check", "error_type", "source", "skipped")
# A policy digest (prefix) is hex: checked on its own, so the digit-run filter for tokens
# (which keeps detected values out) never drops it (P39 G6 round 2, L1).
_DIGEST = re.compile(r"[0-9a-f]{16,64}")
# ``decision`` is copied only from this vocabulary (the nexura-guardrails verdicts).
_DECISIONS = frozenset({"allow", "block", "review", "error", "rewrite"})
# ``mode`` is copied only from this vocabulary (a guardrail policy's enforce or shadow mode).
_MODES = frozenset({"enforce", "shadow"})
_MAX_CATEGORIES = 32


def _token(value: Any) -> str | None:
    """``value`` when it is an identifier-like token that cannot carry content, else None."""
    if not isinstance(value, str) or not _TOKEN.fullmatch(value):
        return None
    if (
        _DIGIT_RUN.search(value)
        or _SEPARATED_DIGITS.search(value)
        or _LONG_RUN.search(value)
        or _KEY_SHAPED.search(value)
    ):
        return None
    return value


def outcome_action(result: GuardrailResult) -> str:
    """The decision action of a guardrail result: ``fail`` (tripwire), ``reject``,
    ``scrub`` (rewritten) or ``allow``."""
    if result.tripwire:
        return "fail"
    if result.rejection is not None:
        return "reject"
    if result.replacement is not None:
        return "scrub"
    return "allow"


def decision_record(
    stage: str,
    guardrail: str,
    action: str,
    *,
    info: Any = None,
    latency_ms: float | None = None,
    agent: str | None = None,
    tool_name: str | None = None,
    tool_call_id: str | None = None,
    error_type: str | None = None,
) -> dict[str, Any]:
    """A redacted record of one guardrail decision (``GUARDRAIL_DECISION`` event data and
    the ``nexura.guardrail.decision`` trace event).

    Holds the stage, guardrail name, action, agent, tool name and call id, latency, and
    from ``info`` (a mapping) only plain classification fields: ``categories`` (tokens),
    ``score`` (a finite number), ``cached``/``review`` (booleans), ``decision`` (one of
    ``allow``, ``block``, ``review``, ``error``, ``rewrite``) and ``backend``, ``rule``,
    ``check``, ``error_type``, ``policy_digest`` (a short digest prefix) and ``source``
    (``live``, ``cache`` or ``journal``) (tokens; ``info["error"]`` counts as ``error_type``),
    ``mode`` (``enforce`` or ``shadow``) and ``applied`` (a boolean; false for a policy's shadow
    decision, which was reported, not applied), ``tokens`` (a whole number: what the check's
    model call used) and ``skipped`` (a token: why a shadow check did not run).
    A token is an identifier: letters, digits and ``_ . : / + -``, at most 128
    characters, with no ``@``, no run of 6 or more digits, no 9 or more digits joined only
    by ``-``, ``.`` or spaces, no unbroken run of 33 or more
    letters and digits and no API-key prefix. Anything else in ``info``, and every value
    that is not such a token, is left out, so put identifiers in these keys, never a
    detected value.
    """
    record: dict[str, Any] = {
        "stage": stage,
        "guardrail": str(guardrail)[:128],
        "action": action,
    }
    if agent is not None:
        record["agent"] = agent
    if tool_name is not None:
        record["tool_name"] = tool_name
    if tool_call_id is not None:
        record["tool_call_id"] = tool_call_id
    if latency_ms is not None and math.isfinite(latency_ms):
        record["latency_ms"] = round(float(latency_ms), 3)
    if isinstance(info, Mapping):
        for key in _TOKEN_KEYS:
            value = _token(info.get(key))
            if value is not None:
                record[key] = value
        if info.get("decision") in _DECISIONS:
            record["decision"] = info["decision"]
        if info.get("mode") in _MODES:
            record["mode"] = info["mode"]
        digest = info.get("policy_digest")
        if isinstance(digest, str) and _DIGEST.fullmatch(digest):
            record["policy_digest"] = digest
        if "error_type" not in record and (error := _token(info.get("error"))) is not None:
            record["error_type"] = error
        categories = info.get("categories")
        if isinstance(categories, (list, tuple, frozenset, set)):
            kept = [c for c in categories if _token(c) is not None]
            if kept:
                record["categories"] = sorted(kept)[:_MAX_CATEGORIES]
        score = info.get("score")
        if isinstance(score, (int, float)) and not isinstance(score, bool) and math.isfinite(score):
            record["score"] = float(score)
        tokens = info.get("tokens")
        if isinstance(tokens, int) and not isinstance(tokens, bool) and tokens >= 0:
            record["tokens"] = tokens
        for flag in ("cached", "review", "applied"):
            if isinstance(info.get(flag), bool):
                record[flag] = info[flag]
    if error_type is not None:
        record["error_type"] = error_type
    return record


def _report(
    on_decision: DecisionSink | None,
    ctx: RunContext[Any],
    stage: str,
    name: str,
    action: str,
    started: float,
    *,
    info: Any = None,
    error_type: str | None = None,
) -> None:
    if on_decision is None:
        return
    on_decision(
        decision_record(
            stage,
            name,
            action,
            info=info,
            latency_ms=(time.monotonic() - started) * 1000,
            agent=ctx.agent_name,
            tool_name=ctx.tool_name,
            tool_call_id=ctx.tool_call_id,
            error_type=error_type,
        )
    )


async def _tool_check(
    fn: GuardrailFn,
    ctx: RunContext[Any],
    value: Any,
    stage: str,
    on_decision: DecisionSink | None = None,
) -> Any:
    from nexura_agents.errors import GuardrailTripwireTriggered, ToolError, ToolExecutionError

    name = getattr(fn, "__name__", "guardrail")
    tool_name = ctx.tool_name or "?"
    stage_id = f"tool_{stage}"
    started = time.monotonic()
    try:
        outcome = await run_guardrail(fn, ctx, value, tool_stage=True)
    except (GuardrailTripwireTriggered, ToolError) as exc:
        _report(on_decision, ctx, stage_id, name, "fail", started, error_type=type(exc).__name__)
        raise
    except Exception as exc:
        _report(on_decision, ctx, stage_id, name, "fail", started, error_type=type(exc).__name__)
        raise ToolExecutionError(tool_name=tool_name, original_error=exc) from exc
    if stage == "input" and outcome.replacement is not None:
        _report(on_decision, ctx, stage_id, name, "fail", started, error_type="TypeError")
        raise ToolExecutionError(
            tool_name=tool_name,
            original_error=TypeError(
                f"Tool input guardrail '{name}' returned rewritten(); input guardrails can "
                "pass, trip or reject only."
            ),
        )
    action = outcome_action(outcome)
    if action != "allow":
        _report(on_decision, ctx, stage_id, name, action, started, info=outcome.info)
    if outcome.tripwire:
        raise GuardrailTripwireTriggered(name, f"tool {stage}", outcome.info, tool=ctx.tool_name)
    return name, outcome


async def check_tool_input(
    tool: Any,
    ctx: RunContext[Any],
    arguments: dict[str, Any],
    *,
    on_decision: DecisionSink | None = None,
) -> str | None:
    """Run a tool's input guardrails on a copy of ``arguments``.

    Returns the text to give the model when a guardrail rejects the call, or None to run
    it. A tripwire raises ``GuardrailTripwireTriggered``; a broken guardrail raises
    ``ToolExecutionError``. Used by the runner and by ``AgentMCPServer``.
    ``on_decision`` receives a redacted ``decision_record`` for every guardrail that does
    more than pass (the runner turns them into ``GUARDRAIL_DECISION`` events).
    """
    import copy

    for fn in tool.input_guardrails:
        name, outcome = await _tool_check(fn, ctx, copy.deepcopy(arguments), "input", on_decision)
        if outcome.rejection is not None:
            return f"Tool call blocked by guardrail '{name}': {outcome.rejection}"
    return None


async def check_tool_output(
    tool: Any,
    ctx: RunContext[Any],
    result: Any,
    *,
    on_decision: DecisionSink | None = None,
) -> tuple[Any, bool]:
    """Run a tool's output guardrails on its result: ``(result to use, rejected?)``.

    A rewrite replaces the result (later guardrails see it); a rejection replaces it with
    the blocked notice and stops checking. ``on_decision`` as in ``check_tool_input``.
    """
    for fn in tool.output_guardrails:
        name, outcome = await _tool_check(fn, ctx, result, "output", on_decision)
        if outcome.rejection is not None:
            return f"Tool result blocked by guardrail '{name}': {outcome.rejection}", True
        if outcome.replacement is not None:
            result = outcome.replacement
    return result, False


_BUILTIN_NAMES = frozenset(
    {
        "PIIFinding",
        "PII_KINDS",
        "allowed_topics",
        "block_pii",
        "detect_pii",
        "json_output_only",
        "max_length",
        "no_prompt_injection",
        "redact_output_pii",
        "redact_pii",
    }
)


def __getattr__(name: str) -> Any:
    # Lazy re-export of the opt-in built-in guardrails (keeps ``import nexura_agents`` light).
    if name in _BUILTIN_NAMES:
        import importlib

        return getattr(importlib.import_module("nexura_agents.guardrails_builtin"), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
