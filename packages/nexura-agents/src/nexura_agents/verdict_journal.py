# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Journaled guardrail verdicts and policy decision events (P39 G6).

A model-based guardrail verdict (an LLM judge, a safety model) is not deterministic: asking
again after a crash could allow a call that was blocked before, and it costs money again.
A policy therefore records every model-based verdict under a key made of the policy digest,
the rule, the check, the stage, the agent, the tool (name and origin), the step it belongs to
with the call's occurrence in the model's reply, and a digest of the exact text judged
(:func:`verdict_key`), and looks the key up before it asks the model again. The record keeps
those parts too: a stored verdict is reused only when every part matches exactly
(:func:`recorded_verdict`); otherwise the check runs again.

Inside a ``Runner`` the verdicts live in the run (``RunContext``) and, for a durable run
with a journal-capable checkpoint store, in the run's execution journal: :func:`record_verdict`
commits the verdict (a fenced journal write, like every other one) **before** the caller acts
on it, so a recovered or resumed run finds it (:func:`recorded_verdict`) and does not call
the model again. A verdict recorded before the run has a journal (the input stage) is kept
in the run and written with the journal's next commit. A changed text is a different key and
is checked afresh. Outside a ``Runner`` both functions do nothing.

:func:`emit_decision` sends a ``GUARDRAIL_DECISION`` run event (and the
``nexura.guardrail.decision`` trace event) from a hook, so a policy's decisions show up
next to the agent's own guardrail decisions. The record must already be redacted
(``nexura_agents.guardrails.decision_record``).
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Callable, Mapping
from typing import Any

__all__ = [
    "GUARD_VERDICTS_KEY",
    "MAX_VERDICTS",
    "RunVerdicts",
    "emit_decision",
    "is_decision_payload",
    "record_verdict",
    "recorded_verdict",
    "occurrence",
    "step_ref",
    "verdict_identity",
    "verdict_key",
]

#: Where the verdicts live in the journal's run state.
GUARD_VERDICTS_KEY = "guard_verdicts"
#: Verdicts kept per run (oldest dropped first); recovery only replays the current batch.
MAX_VERDICTS = 256

_MARK_KEY = "__nexura_guardrail_decision__"
_MARK = object()


def step_ref(ctx: Any, stage: str) -> str:
    """The step a check belongs to: ``input``, ``final``, ``turn:<n>/model`` for the model
    stages, or the tool call's first step key ``turn:<n>/call:<id>`` for tool stages."""
    if stage == "input":
        return "input"
    if stage == "output":
        return "final"
    turn = int(getattr(ctx, "turn", 0) or 0)
    call_id = getattr(ctx, "tool_call_id", None)
    if stage in ("tool_input", "tool_output") and isinstance(call_id, str):
        from nexura_agents._agent_journal import call_step_key

        return str(call_step_key(turn, call_id))
    return f"turn:{turn}/model"


def occurrence(ctx: Any) -> int:
    """Inside a tool call: how many earlier calls of the latest model reply share this
    call's id (the model chooses ids and may repeat one), else 0."""
    call_id = getattr(ctx, "tool_call_id", None)
    name = getattr(ctx, "tool_name", None)
    if not isinstance(call_id, str):
        return 0
    for message in reversed(getattr(ctx, "messages", None) or []):
        calls = getattr(message, "tool_calls", None)
        if getattr(message, "role", None) != "assistant" or not calls:
            continue
        same = [c for c in calls if getattr(c, "id", None) == call_id]
        for index, call in enumerate(same):
            if getattr(call, "name", None) == name:
                return index
        return 0
    return 0


def verdict_identity(
    *,
    policy_digest: str,
    rule_id: str,
    check: str,
    stage: str,
    step: str,
    text: str,
    agent: str | None = None,
    tool: str | None = None,
    origin: str | None = None,
    occurrence: int = 0,
) -> dict[str, Any]:
    """Every part a journaled verdict is bound to (stored with it, compared on reuse)."""
    return {
        "policy_digest": policy_digest,
        "rule_id": rule_id,
        "check": check,
        "stage": stage,
        "step": step,
        "occurrence": int(occurrence),
        "agent": agent,
        "tool": tool,
        "origin": origin,
        "text_sha256": hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest(),
    }


def verdict_key(
    *,
    policy_digest: str,
    rule_id: str,
    check: str,
    stage: str,
    step: str,
    text: str,
    agent: str | None = None,
    tool: str | None = None,
    origin: str | None = None,
    occurrence: int = 0,
) -> str:
    """sha256 of :func:`verdict_identity` (policy digest, rule, check, stage, step,
    occurrence, agent, tool name and origin, sha256 of the text) as hex."""
    identity = verdict_identity(
        policy_digest=policy_digest,
        rule_id=rule_id,
        check=check,
        stage=stage,
        step=step,
        text=text,
        agent=agent,
        tool=tool,
        origin=origin,
        occurrence=occurrence,
    )
    payload = json.dumps(identity, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class RunVerdicts:
    """The verdicts of one run: in memory, and in the run's journal when it has one.

    ``journal`` returns the run's ``AgentJournal`` (or None): the runner passes a getter,
    because the journal is opened after the input guardrails ran.
    """

    def __init__(self, journal: Callable[[], Any]) -> None:
        self._journal = journal
        self.local: dict[str, dict[str, Any]] = {}

    def _stored(self) -> dict[str, Any] | None:
        journal = self._journal()
        if journal is None:
            return None
        stored = journal.state.get(GUARD_VERDICTS_KEY)
        return stored if isinstance(stored, dict) else None

    def get(self, key: str) -> dict[str, Any] | None:
        """The recorded verdict for ``key`` (a copy), or None."""
        found = self.local.get(key)
        if found is None:
            stored = self._stored()
            value = stored.get(key) if stored is not None else None
            found = value if isinstance(value, dict) else None
        return copy.deepcopy(found) if found is not None else None

    def pending(self) -> dict[str, dict[str, Any]]:
        """Verdicts recorded in this process (for a journal opened after them)."""
        return dict(self.local)

    async def put(self, key: str, record: Mapping[str, Any]) -> None:
        """Keep ``record``; with a journal, commit it before returning (fenced)."""
        value = json.loads(json.dumps(dict(record)))  # JSON-safe, and a private copy
        self.local[key] = value
        _prune(self.local)
        journal = self._journal()
        if journal is None:
            return
        attach(journal, {key: value})
        await journal._commit(events=[{"type": "guard_verdict", "key": key}])


def attach(journal: Any, verdicts: Mapping[str, Any]) -> None:
    """Put ``verdicts`` in the journal's state (written with its next commit)."""
    if not verdicts:
        return
    stored = journal.state.get(GUARD_VERDICTS_KEY)
    merged = dict(stored) if isinstance(stored, dict) else {}
    merged.update(verdicts)
    _prune(merged)
    journal.state[GUARD_VERDICTS_KEY] = merged


def _prune(verdicts: dict[str, Any]) -> None:
    while len(verdicts) > MAX_VERDICTS:
        del verdicts[next(iter(verdicts))]


def recorded_verdict(
    ctx: Any, key: str, identity: Mapping[str, Any] | None = None
) -> dict[str, Any] | None:
    """The verdict recorded for ``key`` in this run (or its journal), else None.

    With ``identity`` (:func:`verdict_identity`), a record is returned only when the
    identity stored with it is exactly equal: anything else (a missing, older or different
    identity) is treated as no record, so the check runs again.
    """
    verdicts = getattr(ctx, "_verdicts", None)
    if verdicts is None:
        return None
    found: dict[str, Any] | None = verdicts.get(key)
    if found is None or identity is None:
        return found
    if found.get("identity") != json.loads(json.dumps(dict(identity))):
        return None
    return found


async def record_verdict(ctx: Any, key: str, record: Mapping[str, Any]) -> bool:
    """Record a verdict for ``key``; True when the run keeps it (inside a ``Runner``).

    With a durable journal the verdict is committed before this returns; a lost lease
    raises (the caller must not act on an unrecorded verdict of a run it no longer owns).
    """
    verdicts = getattr(ctx, "_verdicts", None)
    if verdicts is None:
        return False
    await verdicts.put(key, record)
    return True


def emit_decision(ctx: Any, record: Mapping[str, Any]) -> None:
    """Send a redacted decision record as a ``GUARDRAIL_DECISION`` run event (at the run's
    next step, or while a tool batch runs) and as a ``nexura.guardrail.decision`` trace
    event on the current span. Outside a run only the trace event is recorded."""
    from nexura_agents import tracing

    data = dict(record)
    run_id = str(getattr(ctx, "run_id", "") or "")
    tracing.guardrail_decision(None, data, run_id=run_id)
    channel = getattr(ctx, "_events", None)
    if channel is not None:
        channel.put({_MARK_KEY: _MARK, "record": data})


def is_decision_payload(payload: Mapping[str, Any]) -> bool:
    """True for a payload :func:`emit_decision` put on the event channel."""
    return payload.get(_MARK_KEY) is _MARK
