# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""The ``Check`` protocol and the adapters that turn a check into a plain guardrail.

``as_guardrail`` and ``as_tool_guardrail`` return ordinary ``GuardrailFn`` functions, so
every check works with today's ``Agent(input_guardrails=..., output_guardrails=...)`` and
``@tool(input_guardrails=..., output_guardrails=...)``. Mapping from a verdict:

========  ==========================  ==========================  ===========================
Verdict   Agent input / output        Tool input                  Tool output
========  ==========================  ==========================  ===========================
allow     ``passed``                  ``passed``                  ``passed``
block     ``tripped``                 ``rejected`` (or trip)      ``rejected`` (or trip)
rewrite   output: ``rewritten``;      configuration error         ``rewritten``
          input: ``tripped``
review    ``tripped`` (review=True)   ``rejected``                ``rejected``
error     ``on_error="block"``        same                        same
          (default) trips; ``"allow"``
          passes with the error noted
========  ==========================  ==========================  ===========================

``GuardrailResult.info`` always carries the check name, backend, categories, score,
reason, latency and cache flag and masked evidence, never the checked content.

Tool rejections are what the agent model reads, so their text is built only from fixed
words, the check name, taxonomy category ids and the error type. A model judge's free-text
``reason`` can be influenced by the content it judged, so it stays in ``info`` and never
reaches the agent's context.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import re
import time
import uuid
from typing import Any, Literal, Protocol, runtime_checkable

from nexura_agents.guardrails import GuardrailFn, GuardrailResult
from nexura_agents.run_context import RunContext

from nexura_guardrails.verdict import CATEGORIES, CheckInput, Role, Stage, Verdict

__all__ = [
    "Check",
    "as_guardrail",
    "as_tool_guardrail",
    "evaluate",
    "is_hard_error",
    "rejection_message",
    "run_check",
    "value_text",
]

OnError = Literal["block", "allow"]


@runtime_checkable
class Check(Protocol):
    """Anything that can judge a ``CheckInput``.

    ``stages`` lists the stages it supports, ``deterministic`` is True for rules (regex)
    and False for model-based checks.
    """

    name: str
    stages: frozenset[str]
    deterministic: bool

    async def __call__(self, ctx: RunContext[Any], item: CheckInput) -> Verdict:
        """Judge ``item`` and return a verdict."""
        ...


def value_text(value: Any) -> str:
    """The canonical text of a guardrail value: strings as is, objects as sorted JSON."""
    if isinstance(value, str):
        return value
    dump_json = getattr(value, "model_dump_json", None)
    if callable(dump_json):
        return str(dump_json())
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        value = dataclasses.asdict(value)
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(value)


def _error(name: str, reason: str, error_type: str, started: float) -> Verdict:
    return Verdict(
        decision="error",
        reason=reason,
        check=name,
        latency_ms=(time.perf_counter() - started) * 1000,
        evidence={"error_type": error_type},
    )


async def run_check(check: Check, ctx: RunContext[Any], item: CheckInput) -> Verdict:
    """Run ``check`` and always return a verdict (fail closed).

    An exception from the check, or a return value that is not a ``Verdict``, becomes
    ``Verdict(decision="error")`` with the exception type in ``evidence["error_type"]``.
    Cancellation is not swallowed: a cancelled run stays cancelled.
    """
    name = getattr(check, "name", type(check).__name__)
    started = time.perf_counter()
    try:
        verdict = await check(ctx, item)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return _error(name, f"check failed: {type(exc).__name__}", type(exc).__name__, started)
    if not isinstance(verdict, Verdict):
        return _error(
            name, f"check returned {type(verdict).__name__}, not Verdict", "TypeError", started
        )
    changes: dict[str, Any] = {}
    if not verdict.check:
        changes["check"] = name
    if not verdict.latency_ms:
        changes["latency_ms"] = (time.perf_counter() - started) * 1000
    return dataclasses.replace(verdict, **changes) if changes else verdict


def _require_stage(check: Check, stage: str) -> None:
    stages = getattr(check, "stages", None)
    if not isinstance(stages, (set, frozenset)) or stage not in stages:
        raise ValueError(
            f"Check {getattr(check, 'name', check)!r} does not support stage {stage!r} "
            f"(supports {sorted(stages or ())})."
        )


def _latest_user_text(ctx: RunContext[Any]) -> str | None:
    for message in reversed(ctx.messages or []):
        if getattr(message, "role", None) == "user" and isinstance(message.content, str):
            return message.content
    return None


def _named(fn: GuardrailFn, name: str) -> GuardrailFn:
    fn.__name__ = name
    fn.__qualname__ = name
    return fn


async def _verdict(
    check: Check, ctx: RunContext[Any], stage: Stage, value: Any, role: Role
) -> Verdict:
    context = _latest_user_text(ctx) if stage == "output" else None
    item = CheckInput(
        stage=stage,
        text=value_text(value),
        role=role,
        tool_name=ctx.tool_name,
        context=context,
    )
    verdict = await run_check(check, ctx, item)
    if verdict.usage is not None and not verdict.cached:
        ctx.add_usage(verdict.usage)  # the run's usage counts the check's model calls
    return verdict


_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_.:-]{0,63}")


def is_hard_error(verdict: Verdict) -> bool:
    """True for an ``error`` that no ``on_error`` setting may soften (``evidence["hard"]``),
    such as a judge's ``ContentTooLong``."""
    return verdict.decision == "error" and verdict.evidence.get("hard") is True


def _error_info(verdict: Verdict) -> dict[str, Any]:
    info = verdict.info()
    info["error"] = verdict.evidence.get("error_type", "error")
    return info


def _safe_word(value: Any, fallback: str) -> str:
    """``value`` if it is a short identifier, else ``fallback`` (keeps free text out)."""
    return value if isinstance(value, str) and _IDENTIFIER.fullmatch(value) else fallback


def rejection_message(verdict: Verdict, *, review: bool = False) -> str:
    """The fixed notice a tool rejection gives the agent model.

    Built from fixed words, the check name, the taxonomy category ids and the error type
    only, never from the verdict's free-text reason.
    """
    check = _safe_word(verdict.check, "guardrail")
    if verdict.decision == "error":
        error = _safe_word(verdict.evidence.get("error_type"), "error")
        return f"guardrail check '{check}' could not decide ({error}); blocked"
    categories = [c for c in verdict.categories if c in CATEGORIES]
    text = f"blocked by guardrail check '{check}'"
    if categories:
        text += f" (categories: {', '.join(categories)})"
    if review:
        text += "; needs human review"
    return text


def as_guardrail(
    check: Check,
    *,
    stage: Literal["input", "output"] = "input",
    on_error: OnError = "block",
    name: str | None = None,
) -> GuardrailFn:
    """Turn ``check`` into an agent input (default) or output guardrail.

    ``on_error="block"`` (default) trips on an ``error`` verdict; ``"allow"`` lets the run
    continue and records the error type in ``info["error"]``, except for a hard error
    (``is_hard_error``, for example ``ContentTooLong``), which always trips. A ``review``
    verdict trips with ``info["review"] = True``. On the output stage the check also gets
    the latest user message as ``context``. The check's model usage is added to the run's
    usage.
    """
    if stage not in ("input", "output"):
        raise ValueError("stage must be 'input' or 'output'; use as_tool_guardrail for tools.")
    if on_error not in ("block", "allow"):
        raise ValueError("on_error must be 'block' or 'allow'.")
    _require_stage(check, stage)
    role: Role = "user" if stage == "input" else "assistant"

    async def guardrail(ctx: RunContext[Any], value: Any) -> GuardrailResult:
        verdict = await _verdict(check, ctx, stage, value, role)
        if verdict.decision == "allow":
            return GuardrailResult.passed(verdict.info())
        if verdict.decision == "rewrite":
            if stage == "output" and isinstance(value, str) and verdict.replacement is not None:
                return GuardrailResult.rewritten(verdict.replacement, verdict.info())
            info = verdict.info()
            info["reason"] = verdict.reason or "content would need rewriting"
            return GuardrailResult.tripped(info)
        if verdict.decision == "error":
            if on_error == "allow" and not is_hard_error(verdict):
                return GuardrailResult.passed(_error_info(verdict))
            return GuardrailResult.tripped(_error_info(verdict))
        info = verdict.info()
        if verdict.decision == "review":
            info["review"] = True
        return GuardrailResult.tripped(info)

    return _named(guardrail, name or str(getattr(check, "name", "guardrail")))


def as_tool_guardrail(
    check: Check,
    *,
    stage: Literal["tool_input", "tool_output"] = "tool_input",
    on_block: Literal["reject", "trip"] = "reject",
    on_error: OnError = "block",
    name: str | None = None,
) -> GuardrailFn:
    """Turn ``check`` into a tool input (default) or tool output guardrail.

    A block rejects the call or withholds the result and tells the model a fixed notice
    with the check name and category ids (``on_block="reject"``, default; see
    ``rejection_message``), or stops the run (``"trip"``). An ``error``
    verdict is handled like a block unless ``on_error="allow"`` (a hard error, see
    ``is_hard_error``, is always handled like a block). ``review`` rejects with a
    note that a person must review it (approval routing arrives with the policy layer). A
    rewrite on tool input is a configuration error, which the runner turns into a failed
    tool call (fail closed); on tool output it replaces the result text.
    """
    if stage not in ("tool_input", "tool_output"):
        raise ValueError("stage must be 'tool_input' or 'tool_output'.")
    if on_block not in ("reject", "trip"):
        raise ValueError("on_block must be 'reject' or 'trip'.")
    if on_error not in ("block", "allow"):
        raise ValueError("on_error must be 'block' or 'allow'.")
    _require_stage(check, stage)
    role: Role = "assistant" if stage == "tool_input" else "tool"

    def _block(verdict: Verdict, info: dict[str, Any]) -> GuardrailResult:
        if on_block == "trip":
            return GuardrailResult.tripped(info)
        return GuardrailResult.rejected(rejection_message(verdict), info)

    async def guardrail(ctx: RunContext[Any], value: Any) -> GuardrailResult:
        verdict = await _verdict(check, ctx, stage, value, role)
        if verdict.decision == "allow":
            return GuardrailResult.passed(verdict.info())
        if verdict.decision == "rewrite":
            if stage == "tool_input":
                raise TypeError(
                    f"Check {verdict.check!r} rewrote tool arguments; tool input checks can "
                    "allow, block, review or error only."
                )
            if isinstance(value, str) and verdict.replacement is not None:
                return GuardrailResult.rewritten(verdict.replacement, verdict.info())
            return _block(verdict, verdict.info())
        if verdict.decision == "error":
            if on_error == "allow" and not is_hard_error(verdict):
                return GuardrailResult.passed(_error_info(verdict))
            info = _error_info(verdict)
            if on_block == "trip":
                return GuardrailResult.tripped(info)
            return GuardrailResult.rejected(rejection_message(verdict), info)
        info = verdict.info()
        if verdict.decision == "review":
            info["review"] = True
            if on_block == "trip":
                return GuardrailResult.tripped(info)
            return GuardrailResult.rejected(rejection_message(verdict, review=True), info)
        return _block(verdict, info)

    return _named(guardrail, name or str(getattr(check, "name", "guardrail")))


async def evaluate(
    check: Check,
    text: str,
    *,
    stage: Stage = "input",
    role: Role = "user",
    context: str | None = None,
    ctx: RunContext[Any] | None = None,
) -> Verdict:
    """Run one check on ``text`` outside an agent run (tests, notebooks, dry runs).

    Without ``ctx`` a fresh throwaway ``RunContext`` is used, so per-run caches do not carry
    over between calls. Budgets still apply through the active budget scope, if any.
    """
    if ctx is None:
        ctx = RunContext(deps=None, run_id=f"evaluate-{uuid.uuid4().hex}", agent_name="evaluate")
    return await run_check(check, ctx, CheckInput(stage, text, role=role, context=context))
