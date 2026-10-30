# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""The execution journal of one durable agent run (Execution Contract v1, A6 S5).

Internal to ``Runner``. A checkpoint store that offers ``open_execution_store()`` (the
memory, SQLite and Postgres stores) gets, next to its checkpoints:

- a lease on the run from its first write, renewed by a heartbeat every ``ttl / 3``,
  with a fencing token that only goes up; a stale owner's journal writes raise
  ``LeaseLostError``;
- one ``StepIntent`` plus a ``pending`` effect record per non-pure tool call, committed
  just before the call starts, and the call's ``StepOutcome`` after its result is saved;
- the run status (``paused`` with ``pause_reason``, ``completed``, ``failed``).

The store's own recovery claim (``claim`` / ``release`` / fenced ``save``) is taken by the
same owner, so checkpoints stay fenced as before. The checkpoint transcript remains the
truth for what the model saw; the journal records what was dispatched.
"""

from __future__ import annotations

import contextlib
import dataclasses
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any

from nexura_agents import tracing
from nexura_agents._lazy import LazyModule
from nexura_agents.contracts.execution import (
    EffectRecord,
    EffectResolution,
    EffectStatus,
    ExecutionId,
    LeaseLostError,
    StepIntent,
    StepKey,
    StepOutcome,
)
from nexura_agents.errors import RunRecoveryError

if TYPE_CHECKING:
    import asyncio

    from nexura_agents.execution_store import (
        ExecutionLease,
        ExecutionSnapshot,
        ExecutionStep,
        ExecutionStore,
    )
else:
    asyncio = LazyModule("asyncio")

_TERMINAL = ("completed", "failed")


def journal_capable(checkpoints: Any) -> bool:
    """A checkpoint store the runner can keep a journal for (and fence by owner)."""
    from nexura_agents.durable import accepts_fencing

    return (
        checkpoints is not None
        and callable(getattr(checkpoints, "open_execution_store", None))
        and callable(getattr(checkpoints, "claim", None))
        and callable(getattr(checkpoints, "release", None))
        and accepts_fencing(checkpoints.save)
    )


def _escape_call_id(call_id: str) -> str:
    # The call id is opaque (provider-made); '#' ends it, so '%' and '#' inside are escaped.
    return (call_id or "-").replace("%", "%25").replace("#", "%23")


def call_step_key(turn: int, call_id: str, attempt: int = 1) -> StepKey:
    """``turn:<n>/call:<id>`` for the first dispatch of a call, ``...#<k>`` for attempt k.

    ``%`` and ``#`` in the call id are written as ``%25`` and ``%23``, so the attempt suffix
    is unambiguous: attempt 2 of ``c1`` (``call:c1#2``) differs from attempt 1 of a call
    whose id is ``c1#2`` (``call:c1%232``). Ids without those characters keep their key.
    """
    base = f"turn:{max(int(turn), 0)}/call:{_escape_call_id(call_id)}"
    return StepKey(base if attempt <= 1 else f"{base}#{attempt}")


def parse_call_step_key(key: str) -> tuple[int, str, int]:
    """``(turn, call_id, attempt)`` of a tool-call step key, current or pre-escaping form.

    A key written before ids were escaped parses as it did then (a trailing ``#<digits>``
    is the attempt). Raises ``ValueError`` for other step keys.
    """
    head, sep, rest = str(key).partition("/call:")
    if not sep or not head.startswith("turn:") or not head[5:].isdigit():
        raise ValueError(f"Not a tool-call step key: {key!r}")
    attempt = 1
    body, hash_, suffix = rest.rpartition("#")
    if hash_ and suffix.isdigit() and int(suffix) >= 2:
        rest, attempt = body, int(suffix)
    call_id = rest.replace("%23", "#").replace("%25", "%")
    return int(head[5:]), call_id, attempt


class AgentJournal:
    """Lease, heartbeat, intents and outcomes of one run; one instance per runner attempt."""

    def __init__(
        self, checkpoints: Any, store: ExecutionStore, workspace_id: str, run_id: str, ttl: float
    ) -> None:
        self.checkpoints = checkpoints
        self.store = store
        self.run_id = run_id
        self.eid = ExecutionId(workspace_id, run_id)
        self.ttl = ttl
        self.owner: str | None = None
        self.lease: ExecutionLease | None = None
        self.lost: LeaseLostError | None = None
        self.status = "running"
        self.stored_digest: str | None = None
        self.state: dict[str, Any] = {}
        self.steps: dict[str, ExecutionStep] = {}
        self.effects: dict[str, EffectRecord] = {}  # by step key (one effect per step)
        self._heartbeat: asyncio.Task[None] | None = None
        self._closed = False
        # Called once when the lease is found lost (the runner stops its in-flight work).
        self.on_lost: Callable[[], None] | None = None
        # The run's tracing span handle: committed effects are recorded on it (opt-in).
        self.trace_span: Any = None
        # Telemetry-only lookup: journal encoding loses the distinction between "" and "-".
        # Rebuilt from raw call ids during dispatch/recovery; never persisted in the journal.
        self._trace_call_ids: dict[str, str] = {}

    @classmethod
    async def open(cls, checkpoints: Any, run_id: str, ttl: float) -> AgentJournal:
        store = await checkpoints.open_execution_store()
        workspace = getattr(checkpoints, "workspace_id", None) or "default"
        return cls(checkpoints, store, str(workspace), run_id, ttl)

    # ------------------------------------------------------------------ taking the run

    @property
    def held(self) -> bool:
        return self.lease is not None and self.lost is None

    @property
    def fencing_token(self) -> int | None:
        return self.lease.fencing_token if self.lease is not None else None

    async def _snapshot(self) -> ExecutionSnapshot | None:
        try:
            return await self.store.get(self.eid)
        except KeyError:
            return None

    def _load(self, snapshot: ExecutionSnapshot) -> None:
        self.stored_digest = snapshot.run.definition_digest
        self.status = snapshot.run.status
        self.state = dict(snapshot.run.state)
        self.steps = dict(snapshot.steps)
        self.effects = {str(e.intent.step_key): e for e in snapshot.effects.values()}

    def _initial_state(
        self,
        agent_name: str,
        pre_journal_turn: int | None = None,
        budget_scope: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        state: dict[str, Any] = {"agent": agent_name, "pause_reason": None}
        if budget_scope is not None:
            # The RunBudget scope the run started under (A6 S11): continuing the run (resume,
            # recover) needs a budget again.
            state["budget_scope"] = dict(budget_scope)
        if pre_journal_turn is not None:
            # The run was checkpointed before it had a journal (a pre-S5 file, or a store
            # that was not journal capable): calls of turns up to this one may have been
            # sent without an intent, so a missing intent does not prove "never sent".
            state["pre_journal_turn"] = pre_journal_turn
        return state

    def unjournaled_dispatch_possible(self, turn: int) -> bool:
        """True when a call of ``turn`` without an intent may still have been sent."""
        limit = self.state.get("pre_journal_turn")
        return isinstance(limit, int) and not isinstance(limit, bool) and turn <= limit

    def _mark_lost(self, exc: LeaseLostError) -> None:
        first = self.lost is None
        self.lost = exc
        if first and self.on_lost is not None:
            with contextlib.suppress(Exception):
                self.on_lost()

    async def _claim_existing(self, snapshot: ExecutionSnapshot, owner: str) -> bool:
        from nexura_agents.execution_store import ExecutionConflictError

        if snapshot.run.status in _TERMINAL:
            raise RunRecoveryError(f"Run '{self.run_id}' has already finished.")
        try:
            # The stored digest: a changed definition is a warning in this release (the
            # runner compares digests itself), so the claim must not refuse it.
            lease = await self.store.claim(
                self.eid, owner, self.ttl, definition_digest=snapshot.run.definition_digest
            )
        except ExecutionConflictError as exc:
            raise RunRecoveryError(
                f"Run '{self.run_id}' has already finished.", original_error=exc
            ) from exc
        if lease is None:
            return False
        self._load(snapshot)
        self.owner, self.lease = owner, lease
        return True

    async def begin(
        self,
        owner: str,
        digest: str,
        agent_name: str,
        *,
        pre_journal_turn: int | None = None,
        budget_scope: dict[str, str] | None = None,
    ) -> None:
        """A new run (lease from its first write) or ``resume`` of a stored one.

        ``pre_journal_turn``: the turn of a resumed interruption; recorded when the run has
        no journal row yet (it was checkpointed before it had one).
        """
        if pre_journal_turn is not None:
            # resume: an owner that died on this host between its paused checkpoint and
            # the journal's pause commit still holds the run; give it back on its behalf.
            await self._release_dead_owner(await self._snapshot())
        if not await self.checkpoints.claim(self.run_id, owner, self.ttl):
            raise RunRecoveryError(f"Run '{self.run_id}' is being executed by another caller.")
        try:
            snapshot = await self._snapshot()
            if snapshot is None:
                run = await self.store.create(
                    self.eid,
                    owner=owner,
                    definition_digest=digest,
                    state=self._initial_state(agent_name, pre_journal_turn, budget_scope),
                    ttl=self.ttl,
                    kind="agent",
                )
                self.stored_digest = digest
                self.state = dict(run.state)
                self.owner, self.lease = owner, run.lease
            elif not await self._claim_existing(snapshot, owner):
                raise RunRecoveryError(f"Run '{self.run_id}' is being executed by another caller.")
        except BaseException:
            with contextlib.suppress(Exception):
                await self.checkpoints.release(self.run_id, owner)
            raise

    async def _release_dead_owner(self, snapshot: ExecutionSnapshot | None) -> None:
        """A lease owner whose process exited on this host: release its leases for it."""
        from nexura_agents.durable import lease_owner_is_dead
        from nexura_agents.execution_store import ExecutionLease

        if snapshot is None:
            return
        run = snapshot.run
        dead = run.lease_owner
        if dead is not None and run.lease_expires_at is not None and lease_owner_is_dead(dead):
            handle = ExecutionLease(self.eid, dead, run.fencing_token, run.lease_expires_at)
            with contextlib.suppress(Exception):
                await self.store.release(handle)
            with contextlib.suppress(Exception):
                await self.checkpoints.release(self.run_id, dead)

    async def takeover(self, owner: str, digest: str, agent_name: str) -> bool:
        """Recovery: take the run, at once if its owner process is gone; False when busy."""
        from nexura_agents.execution_store import ExecutionConflictError

        await self._release_dead_owner(await self._snapshot())
        if not await self.checkpoints.claim(self.run_id, owner, self.ttl):
            return False
        try:
            snapshot = await self._snapshot()
            if snapshot is None:
                self.owner = owner
                raw = await self.checkpoints.load(self.run_id)
                if raw is None:
                    return True  # nothing to recover (or another workspace's): no journal row
                # A checkpoint written before the journal existed: journal it now, keeping
                # the old rule that its unanswered calls may have been sent.
                try:
                    run_row = await self.store.create(
                        self.eid,
                        owner=owner,
                        definition_digest=digest,
                        state=self._initial_state(agent_name, _checkpoint_turn(raw)),
                        ttl=self.ttl,
                        kind="agent",
                    )
                except ExecutionConflictError:
                    await self.checkpoints.release(self.run_id, owner)
                    return False
                self.stored_digest = digest
                self.state = dict(run_row.state)
                self.lease = run_row.lease
                return True
            if snapshot.run.status in _TERMINAL:
                # Finished in the journal but its checkpoint cleanup was lost: recovery may
                # only replay a "completed" checkpoint (the runner checks), without a lease.
                self._load(snapshot)
                self.owner = owner
                return True
            if not await self._claim_existing(snapshot, owner):
                await self.checkpoints.release(self.run_id, owner)
                return False
            return True
        except BaseException:
            with contextlib.suppress(Exception):
                await self.checkpoints.release(self.run_id, owner)
            raise

    # ------------------------------------------------------------------ lease upkeep

    def check(self) -> None:
        """Raise ``LeaseLostError`` once the lease is known to be lost."""
        if self.lost is not None:
            raise LeaseLostError(
                f"Run '{self.run_id}': the run lease was lost (another worker took the run "
                "over); this worker stops without writing."
            )

    async def renew(self) -> None:
        if self.lease is None or self.owner is None:
            return
        self.check()
        try:
            self.lease = await self.store.renew(self.lease, self.ttl)
        except LeaseLostError as exc:
            self._mark_lost(exc)
            raise
        # Only after the fenced renewal: the checkpoint claim is renewed by owner alone.
        if not await self.checkpoints.claim(self.run_id, self.owner, self.ttl):
            lost = LeaseLostError(f"Run '{self.run_id}': the checkpoint claim was lost.")
            self._mark_lost(lost)
            raise lost

    async def _beat(self) -> None:
        while True:
            await asyncio.sleep(self.ttl / 3)
            try:
                await self.renew()
            except LeaseLostError:
                return
            except Exception:  # noqa: S112 - a transient store error: try again next beat
                continue

    def start_heartbeat(self) -> None:
        if self._heartbeat is None and self.held:
            self._heartbeat = asyncio.get_running_loop().create_task(self._beat())

    async def stop_heartbeat(self) -> None:
        task, self._heartbeat = self._heartbeat, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(BaseException):
                await task

    async def close(self) -> None:
        """Stop the heartbeat and give both leases back (best effort, idempotent)."""
        if self._closed:
            return
        self._closed = True
        await self.stop_heartbeat()
        lease, self.lease = self.lease, None
        if lease is not None:
            with contextlib.suppress(Exception):
                await self.store.release(lease)
        if self.owner is not None:
            with contextlib.suppress(Exception):
                await self.checkpoints.release(self.run_id, self.owner)

    # ------------------------------------------------------------------ journal writes

    async def _commit(
        self,
        *,
        status: str = "running",
        steps: Sequence[StepIntent | StepOutcome] = (),
        effects: Sequence[EffectRecord] = (),
        events: Sequence[dict[str, Any]] = (),
    ) -> None:
        self.check()
        if self.lease is None:
            raise LeaseLostError(f"Run '{self.run_id}': no run lease is held.")
        try:
            await self.store.commit(
                self.lease,
                state=self.state,
                status=status,
                steps=steps,
                effects=effects,
                events=events,
            )
        except LeaseLostError as exc:
            self._mark_lost(exc)
            raise
        from nexura_agents.execution_store import ExecutionStep

        for step in steps:
            key = str(step.step_key)
            old = self.steps.get(key, ExecutionStep())
            if isinstance(step, StepIntent):
                self.steps[key] = dataclasses.replace(old, intent=step)
            else:
                self.steps[key] = dataclasses.replace(old, outcome=step)
        for effect in effects:
            self.effects[str(effect.intent.step_key)] = effect
        self.status = status
        if status in ("paused", *_TERMINAL):
            self.lease = None  # the store released it with this commit
        tracing.effects_committed(
            effects, run_id=self.run_id, on=self.trace_span, raw_call_ids=self._trace_call_ids
        )

    async def ensure_running(self) -> None:
        """A resumed or recovered run is ``running`` again before it does anything."""
        if self.held and self.status != "running":
            self.state["pause_reason"] = None
            await self._commit(events=[{"type": "run_resumed"}])

    def latest(self, turn: int, call_id: str) -> tuple[int, StepKey, ExecutionStep | None]:
        """The newest attempt of a call: (attempt, step key, journal step or None)."""
        base = str(call_step_key(turn, call_id))
        if base not in self.steps and _escape_call_id(call_id) != call_id:
            legacy = f"turn:{max(int(turn), 0)}/call:{call_id}"
            if legacy in self.steps:  # journaled before ids were escaped
                base = legacy

        def key_of(attempt: int) -> StepKey:
            return StepKey(base if attempt <= 1 else f"{base}#{attempt}")

        attempt = 1
        if base not in self.steps:
            key = key_of(1)
            self._trace_call_ids[str(key)] = call_id
            return attempt, key, None
        while str(key_of(attempt + 1)) in self.steps:
            attempt += 1
        key = key_of(attempt)
        self._trace_call_ids[str(key)] = call_id
        return attempt, key, self.steps[str(key)]

    def dispatch_key(self, turn: int, call_id: str) -> StepKey:
        """The step key for the next dispatch of a call.

        The same key (and so the same idempotency key) while the newest attempt is still
        open (a crash re-run of a pure, idempotent or reconcilable call); a new attempt once
        it is indeterminate or resolved (a person decided to send it again).
        """
        attempt, key, step = self.latest(turn, call_id)
        if step is None or step.intent is None:
            return key
        effect = self.effects.get(str(key))
        if step.outcome is None and (effect is None or effect.status == EffectStatus.PENDING):
            return key
        key = StepKey(f"{str(key).rsplit('#', 1)[0] if attempt > 1 else key}#{attempt + 1}")
        self._trace_call_ids[str(key)] = call_id
        return key

    async def record_intent(self, intent: StepIntent, tool_name: str) -> None:
        """Commit the intent and a pending effect just before the call starts."""
        self.check()
        step = self.steps.get(str(intent.step_key))
        if step is not None and step.intent is not None:
            # A re-run of the same attempt keeps its intent and key; there is no new journal
            # write, so assert the lease against the store (fenced renew) before sending.
            if self.lease is None:
                lost = LeaseLostError(f"Run '{self.run_id}': no run lease is held.")
                self._mark_lost(lost)
                raise lost
            await self.renew()
            return
        event: dict[str, Any] = {
            "type": "tool_dispatched",
            "step_key": str(intent.step_key),
            "tool": tool_name,
        }
        if intent.approval_id is not None:
            # Claim for dispatch (§2.7): the approval is consumed by this dispatch, recorded
            # in the same fenced commit as the intent that carries it.
            consumed = list(self.state.get("consumed_approvals") or [])
            consumed.append({"approval_id": intent.approval_id, "step_key": str(intent.step_key)})
            self.state["consumed_approvals"] = consumed
            event["approval_id"] = intent.approval_id
        await self._commit(steps=[intent], effects=[EffectRecord(self.eid, intent)], events=[event])

    def approval_used(self, approval_id: str) -> bool:
        """True when a dispatch was made under this approval (an intent carries it)."""
        return any(
            isinstance(item, dict) and item.get("approval_id") == approval_id
            for item in self.state.get("consumed_approvals") or []
        ) or any(
            step.intent is not None and step.intent.approval_id == approval_id
            for step in self.steps.values()
        )

    def unused_claim(self, approval_id: str) -> dict[str, Any] | None:
        """The recorded claim of this approval when no dispatch was made under it yet."""
        claim = self.state.get("approval_claim")
        if (
            isinstance(claim, dict)
            and claim.get("approval_id") == approval_id
            and not self.approval_used(approval_id)
        ):
            return claim
        return None

    async def peek(self) -> ExecutionSnapshot | None:
        """The run's journal as stored, read without taking or changing anything."""
        return await self._snapshot()

    async def restore_claim(self, claim: dict[str, Any] | None) -> None:
        """Put back the claim a refused resume replaced (the run's status is kept)."""
        self.state["approval_claim"] = dict(claim) if claim is not None else None
        await self._commit(
            status=self.status,
            events=[
                {"type": "approval_claim_restored", "approval_id": (claim or {}).get("approval_id")}
            ],
        )

    async def record_claim(self, claim: dict[str, Any]) -> None:
        """A resume claims an approval: its decisions, before the approval is consumed.

        The run is ``running`` from this commit; a takeover that finds the claim without a
        dispatch under it continues with these decisions (or asks again once expired).
        """
        self.state["approval_claim"] = dict(claim)
        self.state["pause_reason"] = None
        await self._commit(
            events=[{"type": "approval_claimed", "approval_id": claim.get("approval_id")}]
        )

    async def record_outcome(self, key: StepKey, output: str, *, uncertain: bool = False) -> None:
        """Commit the call's outcome once its result is in the checkpoint."""
        step = self.steps.get(str(key))
        if step is None or step.intent is None or step.outcome is not None:
            return
        effects: list[EffectRecord] = []
        effect = self.effects.get(str(key))
        if effect is not None and effect.status == EffectStatus.PENDING:
            if uncertain:  # e.g. a timed-out sync tool may still be running
                effects.append(dataclasses.replace(effect, status=EffectStatus.INDETERMINATE))
            else:
                effects.append(
                    dataclasses.replace(effect, status=EffectStatus.CONFIRMED, result=output)
                )
        await self._commit(
            steps=[StepOutcome(key, "failed" if uncertain else "completed", output)],
            effects=effects,
            events=[{"type": "tool_completed", "step_key": str(key)}],
        )

    def _indeterminate(self, keys: Sequence[StepKey | str]) -> list[EffectRecord]:
        changed = []
        for key in keys:
            effect = self.effects.get(str(key))
            if effect is not None and effect.status == EffectStatus.PENDING:
                changed.append(dataclasses.replace(effect, status=EffectStatus.INDETERMINATE))
        return changed

    async def mark_indeterminate(self, keys: Sequence[StepKey | str]) -> None:
        effects = self._indeterminate(keys)
        if effects:
            await self._commit(
                effects=effects,
                events=[{"type": "effect_unknown", "step_keys": [str(k) for k in keys]}],
            )

    async def pause(self, pause_reason: str, keys: Sequence[StepKey | str]) -> None:
        """``paused`` with ``pause_reason``; in-doubt effects become indeterminate."""
        effects = self._indeterminate(keys)
        self.state["pause_reason"] = pause_reason
        self.state["pending_effects"] = [
            self.effects[str(k)].effect_id for k in keys if str(k) in self.effects
        ]
        await self._commit(
            status="paused",
            effects=effects,
            events=[{"type": "run_paused", "pause_reason": pause_reason}],
        )
        # A paused run holds no lease (§2.7): the store released the journal lease with the
        # commit; the checkpoint claim goes too, so a process that dies now does not keep
        # the run from being resumed elsewhere until its claim expires.
        if self.owner is not None:
            with contextlib.suppress(Exception):
                await self.checkpoints.release(self.run_id, self.owner)

    async def finish(self, status: str) -> None:
        """Terminal status; a failed run's dispatched-but-unfinished effects are indeterminate."""
        effects: list[EffectRecord] = []
        if status == "failed":
            open_keys = [k for k, s in self.steps.items() if s.intent and s.outcome is None]
            effects = self._indeterminate(open_keys)
        self.state["pause_reason"] = None
        self.state.pop("pending_effects", None)
        await self._commit(status=status, effects=effects, events=[{"type": f"run_{status}"}])

    async def resolve(self, key: StepKey, resolution: str, *, actor: str, result: Any) -> None:
        """Record a person's resolution of an in-doubt effect (once)."""
        from nexura_agents.execution_store import EffectAlreadyResolvedError

        wanted = EffectResolution(resolution)
        effect = self.effects.get(str(key))
        if effect is None:
            return  # never dispatched under a journal (an older checkpoint): nothing to record
        if effect.status == EffectStatus.PENDING:
            await self.mark_indeterminate([key])
            effect = self.effects[str(key)]
        if effect.status == EffectStatus.RESOLVED:
            if effect.resolution == wanted:
                return
            raise EffectAlreadyResolvedError(
                f"Effect of '{key}' is already resolved as {effect.resolution}."
            )
        if effect.status == EffectStatus.CONFIRMED:
            if wanted == EffectResolution.CONFIRMED:
                return
            raise EffectAlreadyResolvedError(f"Effect of '{key}' already completed.")
        self.check()
        if self.lease is None:
            raise LeaseLostError(f"Run '{self.run_id}': no run lease is held.")
        try:
            record = await self.store.resolve_effect(
                self.lease, effect.effect_id, wanted, actor=actor, result=result
            )
        except LeaseLostError as exc:
            self._mark_lost(exc)
            raise
        self.effects[str(key)] = record


def _checkpoint_turn(raw: str) -> int:
    """The turn stored in a checkpoint envelope (a large number when it cannot be read)."""
    import json

    try:
        turn = json.loads(json.loads(raw)["interruption"]).get("turn", 0)
    except Exception:
        return 2**31  # unreadable: recovery reports the corrupt checkpoint anyway
    return turn if isinstance(turn, int) and not isinstance(turn, bool) and turn >= 0 else 2**31
