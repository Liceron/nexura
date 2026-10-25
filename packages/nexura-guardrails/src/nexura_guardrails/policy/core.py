# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""``GuardrailPolicy``: one declarative policy applied to every stage of every run.

A ``GuardrailPolicy`` is an ``AgentHooks``. Install it once (``policy.install()`` or
``Runner(hooks=[policy])``) and it applies its rules to:

=================  ===========================================  ===============================
Stage              Where                                        What an action does
=================  ===========================================  ===============================
``input``          an input guardrail it adds to every agent    block/fail: the run trips
``model_input``    ``before_model``                             block: a refusal reply, no
                                                                model call; fail: run fails
``model_output``   ``after_model``                              block: the reply (and its tool
                                                                calls) becomes a refusal;
                                                                scrub: the text is redacted
``tool_input``     ``before_tool`` and ``tool_needs_approval``  block: the tool does not run,
                                                                the model is told the rule;
                                                                require_approval: a person
                                                                approves the call first
``tool_output``    ``after_tool``                               block: the result is withheld;
                                                                scrub: it is redacted
``output``         an output guardrail it adds to every agent   block/fail: the run trips;
                                                                scrub: the answer is redacted
=================  ===========================================  ===============================

For each stage the most severe matching action wins (``fail`` > ``block`` >
``require_approval`` > ``scrub`` > ``allow``). ``default_tool_action="block"`` is allowlist
mode: a tool call that no ``allow`` rule matches is blocked.

Model-based checks (a judge, a safety model) are journaled: inside a ``Runner`` each verdict
is recorded (for a durable run, committed to its journal) before it is acted on, and a
resumed or recovered run reuses it instead of asking the model again
(``nexura_agents.verdict_journal``). Errors, timeouts and malformed verdicts follow
``on_error`` (``"block"`` by default: fail closed).

Guardrail checks reduce risk and leave an audit trail; they are not a security boundary.
The structural controls are a tool allowlist, human approval for side effects and a sandbox.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import logging
import time
import weakref
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Literal

from nexura_agents.errors import GuardrailTripwireTriggered, RunnerError, SDKValidationError
from nexura_agents.guardrails import GuardrailFn, GuardrailResult
from nexura_agents.hooks import AgentHooks
from nexura_agents.policy import set_default_hooks
from nexura_agents.run_context import RunContext
from nexura_agents.tools import ToolInfo
from nexura_agents.types import Message, ModelResponse, ToolCall
from nexura_agents.verdict_journal import (
    emit_decision,
    occurrence,
    record_verdict,
    recorded_verdict,
    step_ref,
    verdict_identity,
    verdict_key,
)

from nexura_guardrails.checks import Check, is_hard_error, rejection_message, run_check, value_text
from nexura_guardrails.compose import parallel, tiered
from nexura_guardrails.policy.audit import AuditRecord, AuditSink
from nexura_guardrails.policy.rules import (
    POLICY_STAGES,
    SEVERITY,
    PolicyError,
    Rule,
)
from nexura_guardrails.verdict import CheckInput, Role, Verdict

__all__ = ["GuardrailPolicy", "PolicyAuditError", "StageOutcome"]

logger = logging.getLogger("nexura_guardrails.policy")

#: The ``CheckInput`` stage and author used for each policy stage.
_CHECK_STAGE: Mapping[str, tuple[str, Role]] = {
    "input": ("input", "user"),
    "model_input": ("input", "user"),
    "model_output": ("output", "assistant"),
    "tool_input": ("tool_input", "assistant"),
    "tool_output": ("tool_output", "tool"),
    "output": ("output", "assistant"),
}
_STAGE_WORDS = {
    "input": "input",
    "model_input": "model input",
    "model_output": "model output",
    "tool_input": "tool input",
    "tool_output": "tool output",
    "output": "output",
}


class PolicyAuditError(RunnerError):
    """An audit sink failed while the policy has ``audit_required=True`` (fail closed).

    A ``RunnerError``, so the run fails with it as it is (not as a tool failure)."""


@dataclasses.dataclass(frozen=True)
class StageOutcome:
    """What the policy decided for one stage item."""

    action: Literal["allow", "block", "scrub", "fail"]
    rule: str | None = None
    verdict: Verdict | None = None
    replacement: str | None = None
    source: str = "live"
    error_type: str | None = None

    @property
    def check(self) -> str | None:
        """The deciding check's name, when a check decided."""
        return self.verdict.check if self.verdict is not None else None


def _verdict_record(verdict: Verdict, identity: Mapping[str, Any]) -> dict[str, Any]:
    """The journaled form of a verdict (JSON-safe; no usage: a replay costs nothing), with
    the identity it is bound to (reused only on an exact match)."""
    record: dict[str, Any] = {
        "identity": dict(identity),
        "decision": verdict.decision,
        "categories": list(verdict.categories),
        "score": verdict.score,
        "reason": verdict.reason,
        "check": verdict.check,
        "backend": verdict.backend,
    }
    if verdict.replacement is not None:
        record["replacement"] = verdict.replacement
    error = verdict.evidence.get("error_type")
    if isinstance(error, str):
        record["error_type"] = error
    if verdict.evidence.get("hard") is True:
        record["hard"] = True
    return record


def _verdict_from_record(record: Mapping[str, Any]) -> Verdict:
    evidence: dict[str, Any] = {"source": "journal"}
    if isinstance(record.get("error_type"), str):
        evidence["error_type"] = record["error_type"]
    if record.get("hard") is True:
        evidence["hard"] = True
    score = record.get("score")
    return Verdict(
        decision=record["decision"],
        categories=tuple(c for c in record.get("categories", ()) if isinstance(c, str)),
        score=float(score)
        if isinstance(score, (int, float)) and not isinstance(score, bool)
        else None,
        reason=str(record.get("reason", "")),
        replacement=record.get("replacement") if record["decision"] == "rewrite" else None,
        check=str(record.get("check", "")),
        backend=str(record.get("backend", "")),
        cached=True,
        evidence=evidence,
    )


class _Uncoercible:
    """A view that stands for "the tool's validation could not build the arguments"."""

    def __repr__(self) -> str:
        return "UNCOERCIBLE"


UNCOERCIBLE: Any = _Uncoercible()


def argument_views(tool_object: Any, arguments: Any) -> list[Any]:
    """The raw ``arguments`` and, when ``tool_object`` validates and coerces them
    (``Tool.coerced_arguments``) to something different, also the coerced arguments. When
    the tool cannot build them (``None``), :data:`UNCOERCIBLE`: restricting argument rules
    then count as matching (fail closed), ``allow`` argument rules as not matching."""
    views = [arguments]
    coerce = getattr(tool_object, "coerced_arguments", None)
    if callable(coerce) and isinstance(arguments, Mapping):
        try:
            coerced = coerce(arguments)
        except Exception:
            coerced = None
        if coerced is None:
            views.append(UNCOERCIBLE)
        elif coerced != arguments:
            views.append(coerced)
    return views


def _check_identity(check: Any) -> str:
    identity = getattr(check, "cache_identity", None)
    if isinstance(identity, str) and identity:
        return identity
    return f"{type(check).__module__}.{type(check).__qualname__}:{getattr(check, 'name', '')}"


def _model_input_text(messages: Sequence[Message]) -> str:
    """What is new in an outgoing request: the messages after the last assistant message
    (the user's turn, tool results), system messages left out."""
    tail: list[str] = []
    for message in reversed(messages):
        if message.role == "assistant":
            break
        if message.role != "system" and message.content:
            tail.append(message.content)
    return "\n\n".join(reversed(tail))


def _reply_text(response: ModelResponse, *, with_calls: bool) -> str:
    message = response.message
    text = message.content or ""
    if with_calls and message.tool_calls:
        calls = [{"name": c.name, "arguments": c.arguments} for c in message.tool_calls]
        text = f"{text}\n{value_text(calls)}" if text else value_text(calls)
    return text


class GuardrailPolicy(AgentHooks):
    """A declarative guardrail policy (see the module docstring).

    ``checks`` maps names to checks (pre-filters, ``JudgeCheck``, safety models,
    compositions). ``rules`` are evaluated as described in :class:`Rule`. ``on_error``
    (``"block"``, default, or ``"allow"``) decides what a check error, timeout or malformed
    verdict does. ``default_tool_action`` is ``"allow"`` (default) or ``"block"``
    (allowlist mode). ``check_timeout`` bounds each check and ``stage_timeout`` each stage
    (seconds; ``None`` for no extra bound, the run's timeout still applies). ``audit``:
    one sink or a list (see ``nexura_guardrails.policy.audit``); ``audit_allows`` also
    audits ``allow`` decisions; ``audit_required`` fails the run when a sink fails.
    ``digest``/``source`` are set by the loader (file digest and path).
    """

    def __init__(
        self,
        *,
        name: str = "policy",
        checks: Mapping[str, Check] | None = None,
        rules: Sequence[Rule] = (),
        on_error: Literal["block", "allow"] = "block",
        default_tool_action: Literal["allow", "block"] = "allow",
        check_timeout: float | None = None,
        stage_timeout: float | None = None,
        audit: AuditSink | Sequence[AuditSink] | None = None,
        audit_allows: bool = False,
        audit_required: bool = False,
        digest: str | None = None,
        source: str | None = None,
        resolved_source: str | None = None,
    ) -> None:
        if not isinstance(name, str) or not name.strip():
            raise PolicyError("name must be non-empty text.", "name")
        self.name = name
        self.checks: dict[str, Check] = dict(checks or {})
        for check_name, check in self.checks.items():
            if not isinstance(check_name, str) or not check_name:
                raise PolicyError("check names must be non-empty strings.", "checks")
            if not callable(check) or not isinstance(getattr(check, "stages", None), frozenset):
                raise PolicyError(
                    f"check {check_name!r} is not a Check (a name, stages and an async call).",
                    f"checks.{check_name}",
                )
        self.rules: tuple[Rule, ...] = tuple(rules)
        seen: set[str] = set()
        for rule in self.rules:
            if not isinstance(rule, Rule):
                raise PolicyError("rules must be Rule objects.", "rules")
            if rule.id in seen:
                raise PolicyError(f"rule id {rule.id!r} is used twice.", "rules")
            seen.add(rule.id)
            for check_name in rule.checks:
                if check_name not in self.checks:
                    raise PolicyError(
                        f"rule {rule.id!r} names unknown check {check_name!r}.", f"rules[{rule.id}]"
                    )
                check = self.checks[check_name]
                for stage in rule.when.stage:
                    wanted = _CHECK_STAGE[stage][0]
                    if wanted not in check.stages:
                        raise PolicyError(
                            f"check {check_name!r} cannot run on stage {stage!r} (it supports "
                            f"{sorted(check.stages)}).",
                            f"rules[{rule.id}]",
                        )
        if on_error not in ("block", "allow"):
            raise PolicyError("on_error must be 'block' or 'allow'.", "on_error")
        if default_tool_action not in ("allow", "block"):
            raise PolicyError(
                "default_tool_action must be 'allow' or 'block'.", "default_tool_action"
            )
        for label, value in (("check_timeout", check_timeout), ("stage_timeout", stage_timeout)):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, (int, float)) or not value > 0
            ):
                raise PolicyError(f"{label} must be a positive number of seconds.", "timeouts")
        self.on_error = on_error
        self.default_tool_action = default_tool_action
        self.check_timeout = None if check_timeout is None else float(check_timeout)
        self.stage_timeout = None if stage_timeout is None else float(stage_timeout)
        if audit is None:
            sinks: tuple[Any, ...] = ()
        elif isinstance(audit, (list, tuple)):
            sinks = tuple(audit)
        else:
            sinks = (audit,)
        for sink in sinks:
            if not callable(getattr(sink, "write", None)):
                raise PolicyError("audit sinks need an async write(record) method.", "audit")
        self.audit: tuple[Any, ...] = sinks
        self.audit_allows = bool(audit_allows)
        self.audit_required = bool(audit_required)
        self.audit_errors = 0
        self.source = source
        self.resolved_source = resolved_source
        # One composed check per rule (built once: compositions keep no per-call state).
        self._rule_checks: dict[str, Check] = {}
        for rule in self.rules:
            if not rule.checks:
                continue
            members = [self.checks[n] for n in rule.checks]
            if len(members) == 1:
                self._rule_checks[rule.id] = members[0]
            elif rule.mode == "parallel":
                self._rule_checks[rule.id] = parallel(*members, name=rule.id)
            else:
                self._rule_checks[rule.id] = tiered(*members, name=rule.id)
        self.digest = digest or self._compute_digest()
        self._stages = frozenset(s for r in self.rules for s in r.when.stage)
        self._input_guard = self._agent_guardrail("input")
        # The output stage plus the model_output rules, so streamed text is held or checked
        # in windows like any output guardrail (H1); a chunk-safe twin for "auto".
        self._output_guard = self._agent_guardrail("output")
        self._output_guard_chunked = self._agent_guardrail("output", chunk_safe=True)
        # Reply texts after_model already produced, per run (skip re-applying model_output).
        self._model_output_done: weakref.WeakKeyDictionary[Any, set[str]] = (
            weakref.WeakKeyDictionary()
        )

    # ------------------------------------------------------------------ identity

    def _compute_digest(self) -> str:
        document = {
            "name": self.name,
            "on_error": self.on_error,
            "default_tool_action": self.default_tool_action,
            "timeouts": [self.check_timeout, self.stage_timeout],
            "checks": {name: _check_identity(c) for name, c in sorted(self.checks.items())},
            "rules": [r.describe() for r in self.rules],
        }
        encoded = json.dumps(document, sort_keys=True, default=str).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @property
    def short_digest(self) -> str:
        """The first 16 hex characters of :attr:`digest` (audit records, events)."""
        return self.digest[:16]

    def __repr__(self) -> str:
        return (
            f"GuardrailPolicy(name={self.name!r}, rules={len(self.rules)}, "
            f"digest={self.short_digest})"
        )

    def describe(self) -> dict[str, Any]:
        """A JSON-safe summary without secrets (for the service API and Studio)."""
        checks = []
        for check_name, check in sorted(self.checks.items()):
            checks.append(
                {
                    "name": check_name,
                    "type": type(check).__name__,
                    "backend": str(getattr(check, "backend", "")),
                    "deterministic": bool(getattr(check, "deterministic", False)),
                }
            )
        return {
            "name": self.name,
            "digest": self.digest,
            "source": self.source,
            "resolved_source": self.resolved_source,
            "on_error": self.on_error,
            "default_tool_action": self.default_tool_action,
            "check_timeout": self.check_timeout,
            "stage_timeout": self.stage_timeout,
            "checks": checks,
            "rules": [r.describe() for r in self.rules],
            "audit": [type(s).__name__ for s in self.audit],
        }

    def install(self, *, locked: bool = False) -> GuardrailPolicy:
        """Make this policy the process default (``set_default_hooks``); ``locked=True``
        stops code in the same process from removing or replacing it. Returns ``self``."""
        set_default_hooks(self, locked=locked)
        return self

    def requires_model_stages(self) -> bool:
        """True when a rule names ``input``, ``model_input``, ``model_output`` or ``output``:
        code that calls models without ``before_model``/``after_model`` and the agent
        input/output guardrails must refuse to run under this policy when it is the locked
        default (``nexura_agents.policy.check_model_stages``)."""
        return bool(self._stages & {"input", "model_input", "model_output", "output"})

    def text_rules(self, agent: str | None) -> list[Rule]:
        """The ``model_output``/``output`` rules that can change or stop reply text of
        ``agent`` (scrub, block, fail): with any of them, streamed text must be held or
        checked in windows, never passed through."""
        return [
            r
            for r in self.rules
            if r.action in ("scrub", "block", "fail")
            and any(
                stage in r.when.stage and r.when.matches(stage, agent=agent)
                for stage in ("model_output", "output")
            )
        ]

    def chunk_safe(self, agent: str | None) -> bool:
        """True when every text rule for ``agent`` is a scrub by deterministic (regex) checks:
        ``stream_guard="auto"`` may then check streamed text in windows instead of holding."""
        rules = self.text_rules(agent)
        return bool(rules) and all(
            r.action == "scrub"
            and all(getattr(self.checks[c], "deterministic", False) for c in r.checks)
            for r in rules
        )

    def _refuse_passthrough(self, agent: Any) -> None:
        guard = getattr(agent, "stream_guard", None)
        name = getattr(agent, "name", None)
        if getattr(guard, "mode", None) == "passthrough" and self.text_rules(name):
            raise SDKValidationError(
                f"Agent '{name}' uses stream_guard='passthrough', but guardrail policy "
                f"'{self.name}' has model_output or output rules for it: streamed text would "
                "reach the client before they apply. Use stream_guard='auto' (the default), "
                "'hold' or 'chunked'."
            )

    def has_stage(self, stage: str) -> bool:
        """True when a rule names ``stage`` (or, for ``tool_input``, allowlist mode)."""
        if stage == "tool_input" and self.default_tool_action == "block":
            return True
        return stage in self._stages

    # ------------------------------------------------------------------ evaluation

    async def _run_one(
        self, ctx: RunContext[Any], rule: Rule, stage: str, text: str, context: str | None
    ) -> tuple[Verdict, str]:
        """Run ``rule``'s check on ``text``: a journaled verdict when there is one."""
        check = self._rule_checks[rule.id]
        check_stage, role = _CHECK_STAGE[stage]
        journaled = not getattr(check, "deterministic", False)
        key = None
        identity: dict[str, Any] = {}
        if journaled:
            on_tool = stage in ("tool_input", "tool_output")
            info = getattr(ctx, "tool", None)
            parts: dict[str, Any] = {
                "policy_digest": self.digest,
                "rule_id": rule.id,
                "check": str(getattr(check, "name", rule.id)),
                "stage": stage,
                "step": step_ref(ctx, stage),
                "text": text,
                "agent": ctx.agent_name,
                "tool": ctx.tool_name if on_tool else None,
                "origin": getattr(info, "origin", None) if on_tool else None,
                "occurrence": occurrence(ctx) if on_tool else 0,
            }
            identity = verdict_identity(**parts)
            key = verdict_key(**parts)
            stored = recorded_verdict(ctx, key, identity)
            if stored is not None:
                try:
                    return _verdict_from_record(stored), "journal"
                except (KeyError, ValueError, TypeError):
                    pass  # an unreadable record is checked afresh (and recorded again)
        item = CheckInput(
            stage=check_stage,  # type: ignore[arg-type]
            text=text,
            role=role,
            tool_name=ctx.tool_name if stage in ("tool_input", "tool_output") else None,
            context=context,
        )
        if self.check_timeout is not None:
            try:
                verdict = await asyncio.wait_for(run_check(check, ctx, item), self.check_timeout)
            except TimeoutError:
                verdict = Verdict(
                    decision="error",
                    reason=f"no verdict within {self.check_timeout}s",
                    check=str(getattr(check, "name", rule.id)),
                    evidence={"error_type": "TimeoutError"},
                )
        else:
            verdict = await run_check(check, ctx, item)
        if verdict.usage is not None and not verdict.cached:
            ctx.add_usage(verdict.usage)  # the run's usage counts the check's model calls
        if journaled and key is not None:
            # Recorded (and, in a durable run, committed) before anything acts on it.
            await record_verdict(ctx, key, _verdict_record(verdict, identity))
        return verdict, "cache" if verdict.cached else "live"

    def _triggered(self, rule: Rule, verdict: Verdict) -> bool:
        if verdict.decision == "allow":
            return False
        if verdict.decision == "error":
            on_error = rule.on_error or self.on_error
            return on_error == "block" or is_hard_error(verdict)
        return True  # block, review and rewrite all count as findings

    async def evaluate(
        self,
        ctx: RunContext[Any],
        stage: str,
        text: str,
        *,
        tool: ToolInfo | None = None,
        arguments: Any = None,
        context: str | None = None,
        scrub_text: str | None = None,
    ) -> StageOutcome:
        """Apply the rules for ``stage`` to ``text`` (bounded by ``stage_timeout``).

        ``scrub_text`` is what scrub rules check and redact when it differs from ``text``
        (the model reply's text without its tool calls).
        """
        if stage not in POLICY_STAGES:
            raise ValueError(f"unknown stage {stage!r}")
        views = argument_views(getattr(ctx, "_tool_object", None), arguments)
        work = self._evaluate(ctx, stage, text, tool, views, context, scrub_text)
        if self.stage_timeout is None:
            return await work
        try:
            return await asyncio.wait_for(work, self.stage_timeout)
        except TimeoutError:
            if self.on_error == "allow":
                return StageOutcome("allow", error_type="TimeoutError")
            return StageOutcome("block", rule="stage_timeout", error_type="TimeoutError")

    async def _evaluate(
        self,
        ctx: RunContext[Any],
        stage: str,
        text: str,
        tool: ToolInfo | None,
        views: Sequence[Any],
        context: str | None,
        scrub_text: str | None,
    ) -> StageOutcome:
        matched = self._matched(stage, ctx.agent_name, tool, views)
        allowlisted = any(r.action == "allow" for r in matched)
        if stage == "tool_input" and self.default_tool_action == "block" and not allowlisted:
            return StageOutcome("block", rule="default_tool_action")
        # Rules without checks act on every match: the most severe one wins.
        unconditional = [r for r in matched if not r.checks and r.action in ("fail", "block")]
        if unconditional:
            top = max(unconditional, key=lambda r: r.severity)  # first of the highest rank
            return StageOutcome(top.action, rule=top.id)  # type: ignore[arg-type]
        # Rules with checks, most severe first, so a cheaper finding never hides a fail.
        conditional = sorted(
            (r for r in matched if r.checks and r.action in ("fail", "block", "scrub")),
            key=lambda r: -SEVERITY[r.action],
        )
        current = scrub_text if scrub_text is not None else text
        scrubbed: StageOutcome | None = None
        for rule in conditional:
            if rule.action == "scrub":
                verdict, source = await self._run_one(ctx, rule, stage, current, context)
                if not self._triggered(rule, verdict):
                    continue
                if verdict.decision == "rewrite" and verdict.replacement is not None:
                    current = verdict.replacement
                    scrubbed = StageOutcome(
                        "scrub", rule=rule.id, verdict=verdict, replacement=current, source=source
                    )
                    continue
                # A scrub check that blocks (or errors) cannot redact: block, fail closed.
                return self._finding("block", rule, verdict, source)
            verdict, source = await self._run_one(ctx, rule, stage, text, context)
            if self._triggered(rule, verdict):
                return self._finding(rule.action, rule, verdict, source)  # type: ignore[arg-type]
        return scrubbed or StageOutcome("allow")

    def _matched(
        self, stage: str, agent: str | None, tool: ToolInfo | None, views: Sequence[Any]
    ) -> list[Rule]:
        """The rules that match on ``stage``. ``views`` are the raw arguments and, when the
        tool coerces them, the arguments it will receive (H2): a restricting rule matches
        when it matches either; an ``allow`` rule only when it matches both (the most severe
        result wins)."""
        found = []
        for rule in self.rules:
            allow = rule.action == "allow"
            hits = []
            for view in views:
                if view is UNCOERCIBLE:
                    # Arguments the tool cannot build: an argument condition counts as met for
                    # a restricting rule (fail closed) and as unmet for an allow rule.
                    hit = rule.when.matches(
                        stage, agent=agent, tool=tool, allow_rule=allow, ignore_args=True
                    ) and (not allow or not rule.when.args)
                else:
                    hit = rule.when.matches(
                        stage, agent=agent, tool=tool, arguments=view, allow_rule=allow
                    )
                hits.append(hit)
            if all(hits) if allow else any(hits):
                found.append(rule)
        return found

    @staticmethod
    def _finding(
        action: Literal["block", "fail"], rule: Rule, verdict: Verdict, source: str
    ) -> StageOutcome:
        error = verdict.evidence.get("error_type")
        return StageOutcome(
            action,
            rule=rule.id,
            verdict=verdict,
            source=source,
            error_type=error if isinstance(error, str) else None,
        )

    # ------------------------------------------------------------------ audit and events

    def _info(self, stage: str, outcome: StageOutcome) -> dict[str, Any]:
        info: dict[str, Any] = {
            "policy_digest": self.short_digest,
            "source": outcome.source,
        }
        if outcome.rule is not None:
            info["rule"] = outcome.rule
        verdict = outcome.verdict
        if verdict is not None:
            info.update(
                {
                    "check": verdict.check or None,
                    "backend": verdict.backend or None,
                    "decision": verdict.decision,
                    "categories": list(verdict.categories),
                    "score": verdict.score,
                    "cached": verdict.cached,
                }
            )
        if outcome.error_type is not None:
            info["error_type"] = outcome.error_type
        return {k: v for k, v in info.items() if v is not None}

    async def _audit(
        self,
        ctx: RunContext[Any],
        stage: str,
        action: str,
        outcome: StageOutcome,
        started: float,
        *,
        event: bool,
    ) -> None:
        """Record one decision: run event and trace (tool and model stages) and the sinks."""
        if action == "allow" and not self.audit_allows:
            return
        from nexura_agents.guardrails import decision_record

        latency = (time.monotonic() - started) * 1000
        on_tool = stage in ("tool_input", "tool_output")
        if event:
            emit_decision(
                ctx,
                decision_record(
                    stage,
                    f"policy:{self.name}",
                    action,
                    info=self._info(stage, outcome),
                    latency_ms=latency,
                    agent=ctx.agent_name,
                    tool_name=ctx.tool_name if on_tool else None,
                    tool_call_id=ctx.tool_call_id if on_tool else None,
                ),
            )
        if not self.audit:
            return
        verdict = outcome.verdict
        workspace = ctx.metadata.get("workspace_id") if isinstance(ctx.metadata, dict) else None
        record = AuditRecord(
            stage=stage,
            action=action,
            rule_id=outcome.rule,
            check=verdict.check if verdict is not None and verdict.check else None,
            backend=verdict.backend if verdict is not None and verdict.backend else None,
            decision=verdict.decision if verdict is not None else None,
            categories=tuple(verdict.categories) if verdict is not None else (),
            score=verdict.score if verdict is not None else None,
            latency_ms=round(latency, 3),
            cached=bool(verdict.cached) if verdict is not None else False,
            source=outcome.source,
            error_type=outcome.error_type,
            run_id=ctx.run_id,
            workspace_id=workspace if isinstance(workspace, str) else None,
            agent=ctx.agent_name,
            tool=ctx.tool_name if on_tool else None,
            tool_call_id=ctx.tool_call_id if on_tool else None,
            policy=self.name,
            policy_digest=self.short_digest,
        )
        for sink in self.audit:
            try:
                await sink.write(record)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.audit_errors += 1
                logger.warning(
                    "guardrail policy %r: audit sink %s failed (%s)",
                    self.name,
                    type(sink).__name__,
                    type(exc).__name__,
                )
                if self.audit_required:
                    raise PolicyAuditError(
                        f"Audit sink {type(sink).__name__} failed and the policy requires "
                        "auditing (audit_required=True)."
                    ) from exc

    def _trip(self, stage: str, outcome: StageOutcome, tool: str | None = None) -> Exception:
        return GuardrailTripwireTriggered(
            f"policy:{self.name}:{outcome.rule or 'policy'}",
            _STAGE_WORDS[stage],
            self._info(stage, outcome),
            tool=tool,
        )

    @staticmethod
    def _rule_word(outcome: StageOutcome) -> str:
        return outcome.rule or "policy"

    def _notice(self, outcome: StageOutcome) -> str:
        """Fixed words, the rule id, check name and category ids; never free text."""
        if outcome.rule == "default_tool_action":
            return "it is not on the tool allowlist"
        if outcome.verdict is not None:
            return rejection_message(outcome.verdict)
        if outcome.error_type is not None:
            return f"the check could not decide ({outcome.error_type})"
        return "the policy does not allow it"

    # ------------------------------------------------------------------ agent stages

    async def _output_outcome(self, ctx: RunContext[Any], text: str) -> tuple[str, StageOutcome]:
        """``model_output`` rules (unless after_model already produced exactly this text),
        then ``output`` rules on the result. Returns the deciding stage and its outcome; a
        scrub's replacement covers both stages."""
        first = StageOutcome("allow")
        run = getattr(ctx, "_verdicts", None)
        done = self._model_output_done.get(run, set()) if run is not None else set()
        if self.has_stage("model_output") and _digest(text) not in done:
            first = await self.evaluate(ctx, "model_output", text, scrub_text=text)
            if first.action in ("block", "fail"):
                return "model_output", first
            if first.action == "scrub" and first.replacement is not None:
                text = first.replacement
        if self.has_stage("output"):
            second = await self.evaluate(ctx, "output", text, context=_latest_user(ctx))
            if second.action != "allow":
                return "output", second
        if first.action == "scrub":
            return "model_output", first
        return "output", StageOutcome("allow")

    def _agent_guardrail(
        self, stage: Literal["input", "output"], *, chunk_safe: bool = False
    ) -> GuardrailFn:
        async def guardrail(ctx: RunContext[Any], value: Any) -> GuardrailResult:
            started = time.monotonic()
            if stage == "output":
                where, outcome = await self._output_outcome(ctx, value_text(value))
            else:
                where, outcome = stage, await self.evaluate(ctx, stage, value_text(value))
            if outcome.action == "allow":
                await self._audit(ctx, where, "allow", outcome, started, event=False)
                return GuardrailResult.passed(self._info(where, outcome))
            if outcome.action == "scrub":
                if isinstance(value, str) and outcome.replacement is not None:
                    await self._audit(ctx, where, "scrub", outcome, started, event=False)
                    return GuardrailResult.rewritten(
                        outcome.replacement, self._info(where, outcome)
                    )
                outcome = dataclasses.replace(outcome, action="block")
            action = "fail" if outcome.action == "fail" else "block"
            await self._audit(ctx, where, action, outcome, started, event=False)
            return GuardrailResult.tripped(self._info(where, outcome))

        guardrail.__name__ = guardrail.__qualname__ = f"policy:{self.name}"
        # Read by the runner: "auto" may check this guardrail in windows (regex scrubs only).
        guardrail.nexura_chunk_safe = chunk_safe  # type: ignore[attr-defined]
        return guardrail

    def input_guardrails(self, ctx: RunContext[Any], agent: Any) -> Sequence[Any]:
        """The policy's ``input`` stage, as one input guardrail of every agent."""
        return (self._input_guard,) if self.has_stage("input") else ()

    def output_guardrails(self, ctx: RunContext[Any], agent: Any) -> Sequence[Any]:
        """The policy's ``output`` stage and its ``model_output`` rules, as one output
        guardrail of every agent they apply to, so streamed text goes through the same
        holding path as any output guardrail (``hold``, or ``chunked`` windows when every
        rule is a regex scrub). ``stream_guard="passthrough"`` is refused for such an agent."""
        name = getattr(agent, "name", None)
        if not self.text_rules(name):
            return ()
        self._refuse_passthrough(agent)
        return (self._output_guard_chunked if self.chunk_safe(name) else self._output_guard,)

    async def on_agent_start(self, ctx: RunContext[Any], agent: Any) -> None:
        """Refuse ``stream_guard="passthrough"`` at run start (and at a handoff) when this
        policy has text rules for the agent: a clear error, not a silent downgrade."""
        self._refuse_passthrough(agent)

    # ------------------------------------------------------------------ model stages

    async def before_model(
        self, ctx: RunContext[Any], messages: list[Message]
    ) -> ModelResponse | None:
        """``model_input``: block answers with a refusal (no model call); fail raises."""
        if not self.has_stage("model_input"):
            return None
        started = time.monotonic()
        outcome = await self.evaluate(ctx, "model_input", _model_input_text(messages))
        await self._audit(ctx, "model_input", outcome.action, outcome, started, event=True)
        if outcome.action == "fail":
            raise self._trip("model_input", outcome)
        if outcome.action == "block":
            return ModelResponse(
                message=Message(
                    role="assistant",
                    content=f"Request blocked by policy rule '{self._rule_word(outcome)}'.",
                )
            )
        return None

    async def after_model(
        self, ctx: RunContext[Any], response: ModelResponse
    ) -> ModelResponse | None:
        """``model_output``: scrub redacts the text, block replaces the reply (and drops its
        tool calls) with a refusal, fail raises."""
        if not self.has_stage("model_output"):
            return None
        started = time.monotonic()
        outcome = await self.evaluate(
            ctx,
            "model_output",
            _reply_text(response, with_calls=True),
            scrub_text=_reply_text(response, with_calls=False),
        )
        await self._audit(ctx, "model_output", outcome.action, outcome, started, event=True)
        if outcome.action == "fail":
            raise self._trip("model_output", outcome)
        if outcome.action == "block":
            refusal = f"Reply withheld by policy rule '{self._rule_word(outcome)}'."
            self._remember_output(ctx, refusal)
            return dataclasses.replace(response, message=Message(role="assistant", content=refusal))
        if outcome.action == "scrub" and outcome.replacement is not None:
            message = dataclasses.replace(response.message, content=outcome.replacement)
            self._remember_output(ctx, outcome.replacement)
            return dataclasses.replace(response, message=message)
        self._remember_output(ctx, response.message.content or "")
        return None

    def _remember_output(self, ctx: RunContext[Any], text: str) -> None:
        """This run's reply text after model_output rules (the output guardrail skips them)."""
        run = getattr(ctx, "_verdicts", None)
        if run is None:
            return
        done = self._model_output_done.setdefault(run, set())
        if len(done) > 256:
            done.clear()
        done.add(_digest(text))

    # ------------------------------------------------------------------ tool stages

    def _tool_info(self, ctx: RunContext[Any], call: ToolCall) -> ToolInfo:
        info = getattr(ctx, "tool", None)
        return info if isinstance(info, ToolInfo) else ToolInfo(name=call.name)

    async def before_tool(self, ctx: RunContext[Any], call: ToolCall) -> str | None:
        """``tool_input``: block skips the tool and tells the model the rule; fail raises."""
        if not self.has_stage("tool_input"):
            return None
        started = time.monotonic()
        outcome = await self.evaluate(
            ctx,
            "tool_input",
            value_text(call.arguments),
            tool=self._tool_info(ctx, call),
            arguments=call.arguments,
        )
        await self._audit(ctx, "tool_input", outcome.action, outcome, started, event=True)
        if outcome.action == "fail":
            raise self._trip("tool_input", outcome, tool=call.name)
        if outcome.action == "block":
            return (
                f"Tool call blocked by policy rule '{self._rule_word(outcome)}': "
                f"{self._notice(outcome)}."
            )
        return None

    async def after_tool(self, ctx: RunContext[Any], call: ToolCall, result: str) -> str | None:
        """``tool_output``: scrub redacts the result, block withholds it, fail raises."""
        if not self.has_stage("tool_output"):
            return None
        started = time.monotonic()
        outcome = await self.evaluate(
            ctx,
            "tool_output",
            result,
            tool=self._tool_info(ctx, call),
            arguments=call.arguments,
        )
        await self._audit(ctx, "tool_output", outcome.action, outcome, started, event=True)
        if outcome.action == "fail":
            raise self._trip("tool_output", outcome, tool=call.name)
        if outcome.action == "block":
            return (
                f"Tool result withheld by policy rule '{self._rule_word(outcome)}': "
                f"{self._notice(outcome)}."
            )
        if outcome.action == "scrub":
            return outcome.replacement
        return None

    def tool_needs_approval(
        self, ctx: RunContext[Any], tool: Any, arguments: Mapping[str, Any]
    ) -> bool | None:
        """``require_approval`` rules (deterministic: tool facts and arguments only)."""
        info = tool.info() if callable(getattr(tool, "info", None)) else ToolInfo(name=tool.name)
        needed = self.call_needs_approval(ctx, info, arguments, tool_object=tool)
        return True if needed else None

    def call_needs_approval(
        self, ctx: Any, info: ToolInfo, arguments: Any, *, tool_object: Any = None
    ) -> bool:
        """True when a ``require_approval`` rule matches this call, on the raw arguments or
        on those the tool will receive after validation (``tool_object``). False when the
        call is refused anyway: allowlist mode without an ``allow`` rule, or a check-less
        ``block``/``fail`` rule (a person is never asked to approve a refused call). Sync and
        deterministic; also for calls outside a ``Runner``."""
        views = argument_views(tool_object, arguments)
        matched = self._matched("tool_input", getattr(ctx, "agent_name", None), info, views)
        if self.default_tool_action == "block" and not any(r.action == "allow" for r in matched):
            return False  # refused in before_tool: nothing to approve
        if any(not r.checks and r.action in ("block", "fail") for r in matched):
            return False  # refused in before_tool (L4)
        return any(r.action == "require_approval" for r in matched)

    async def check_call(
        self,
        ctx: RunContext[Any],
        stage: Literal["tool_input", "tool_output"],
        value: Any,
        *,
        tool: ToolInfo,
        arguments: Any = None,
    ) -> StageOutcome:
        """Apply the ``tool_input`` or ``tool_output`` rules to a call made outside a
        ``Runner`` (a workflow module node), with the decision event and audit record.

        Returns the outcome; the caller acts on it (``block``/``fail``: do not run the call
        or do not keep its result; ``scrub``: use ``outcome.replacement``). ``arguments``
        default to ``value`` on ``tool_input``. ``ctx.tool`` is not needed: ``tool`` gives
        the facts rules match. Approval is :meth:`call_needs_approval`'s.
        """
        if stage not in ("tool_input", "tool_output"):
            raise ValueError("check_call checks tool_input or tool_output.")
        if not self.has_stage(stage):
            return StageOutcome("allow")
        started = time.monotonic()
        outcome = await self.evaluate(
            ctx,
            stage,
            value_text(value),
            tool=tool,
            arguments=value if arguments is None and stage == "tool_input" else arguments,
        )
        await self._audit(ctx, stage, outcome.action, outcome, started, event=True)
        return outcome

    # ------------------------------------------------------------------ helpers

    def rules_for(self, stage: str) -> list[Rule]:
        """The rules that name ``stage``, in order."""
        return [r for r in self.rules if stage in r.when.stage]

    @classmethod
    def from_file(cls, path: Any, **kwargs: Any) -> GuardrailPolicy:
        """Load a ``.json``/``.yaml`` policy file (``nexura_guardrails.policy.load_policy``)."""
        from nexura_guardrails.policy.loader import load_policy

        return load_policy(path, **kwargs)

    @classmethod
    def from_dict(cls, data: Any, **kwargs: Any) -> GuardrailPolicy:
        """Build a policy from a parsed policy document (``version: 1``)."""
        from nexura_guardrails.policy.loader import policy_from_dict

        return policy_from_dict(data, **kwargs)

    @classmethod
    def from_json(cls, text: str, **kwargs: Any) -> GuardrailPolicy:
        """Build a policy from JSON text."""
        from nexura_guardrails.policy.loader import policy_from_json

        return policy_from_json(text, **kwargs)

    @classmethod
    def from_yaml(cls, text: str, **kwargs: Any) -> GuardrailPolicy:
        """Build a policy from YAML text (needs ``nexura-guardrails[yaml]``)."""
        from nexura_guardrails.policy.loader import policy_from_yaml

        return policy_from_yaml(text, **kwargs)

    @classmethod
    def build(
        cls,
        *,
        rules: Iterable[Rule | Mapping[str, Any]],
        **kwargs: Any,
    ) -> GuardrailPolicy:
        """Like the constructor, but rules may also be dicts (``{"id": ..., "when": {...},
        "action": ...}``), as in a policy file."""
        from nexura_guardrails.policy.loader import rule_from_dict

        built = [r if isinstance(r, Rule) else rule_from_dict(r, i) for i, r in enumerate(rules)]
        return cls(rules=built, **kwargs)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def _latest_user(ctx: RunContext[Any]) -> str | None:
    for message in reversed(ctx.messages or []):
        if getattr(message, "role", None) == "user" and isinstance(message.content, str):
            return message.content
    return None
