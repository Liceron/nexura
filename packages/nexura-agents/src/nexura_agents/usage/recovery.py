# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Budget reservations across a takeover (Execution Contract v1 §2.7, A6 S11).

A process that dies between admission and settlement leaves its reservation ``active``:
it keeps counting against the allowance and nothing ends it. When a run is taken over
(``Runner.recover``, ``WorkflowEngine.recover``, ``resume``, a trigger job continuing
its run) the new owner ends the run's open reservations before
any step runs, and every runtime does the same when a run reaches a terminal status:

- the step was dispatched, or may have been, so it may have been billed: the reservation
  is **settled as unknown usage at its estimate** (``usage_state="unknown"``). That is a
  step whose intent is in the journal (with or without an outcome), a model call or pure
  step (they write no intent: their reservation is taken right before the provider
  request), a step of a run without a journal, and an unlinked reservation of the run's
  budget scope;
- the step writes an intent before it dispatches and the journal has none: it never
  dispatched, so the reservation is **released** without charge.

Nothing is refunded on time alone, and a settlement is idempotent: a stale owner that
settles the same reservation later gets the first settlement back.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from nexura_agents.usage.errors import ReservationNotFoundError
from nexura_agents.usage.ledger import UsageLedger
from nexura_agents.usage.linkage import DispatchFrame, frames_of
from nexura_agents.usage.models import Reservation

__all__ = [
    "ReservationSettlement",
    "budget_scope",
    "ledgers_of",
    "restore_run_budget",
    "settle_run_reservations",
    "settle_run_reservations_async",
]


@dataclass(frozen=True)
class ReservationSettlement:
    """What recovery (or a run's end) did with one open reservation."""

    reservation_id: str
    action: Literal["settled_unknown", "released"]
    step_key: str | None
    reason: str


def _frame(res: Reservation, workspace_id: str, run_id: str) -> DispatchFrame | None:
    for frame in frames_of(res.metadata):
        if (frame.workspace_id, frame.run_id) == (workspace_id, run_id):
            return frame
    return None


def _decide(
    res: Reservation, workspace_id: str, run_id: str, steps: Mapping[str, Any] | None
) -> tuple[Literal["settled_unknown", "released"], str | None, str]:
    frame = _frame(res, workspace_id, run_id)
    if frame is None:
        return "settled_unknown", None, "unlinked reservation of the run's budget scope"
    if not frame.tracked:
        return (
            "settled_unknown",
            frame.step_key,
            "taken at admission right before dispatch (model call or pure step, no intent)",
        )
    if steps is None:
        return "settled_unknown", frame.step_key, "no execution journal to prove no dispatch"
    step = steps.get(frame.step_key)
    if step is not None and getattr(step, "intent", None) is not None:
        if getattr(step, "outcome", None) is None:
            return "settled_unknown", frame.step_key, "intent dispatched without an outcome"
        return "settled_unknown", frame.step_key, "step finished but its settlement was lost"
    return "released", frame.step_key, "no step intent: never dispatched"


def settle_run_reservations(
    ledger: UsageLedger,
    *,
    workspace_id: str,
    run_id: str,
    steps: Mapping[str, Any] | None,
    include_unlinked: bool = False,
    context: str = "takeover",
    max_fencing_token: int | None = None,
    unlinked_before: float | None = None,
) -> list[ReservationSettlement]:
    """End every open reservation of the execution ``(workspace_id, run_id)``.

    ``steps`` maps step keys to the run's journal steps (objects with ``intent`` and
    ``outcome``), or is ``None`` for a run without an execution journal. ``context`` is
    recorded on the ledger entry (``"takeover"`` or ``"terminal"``).

    ``max_fencing_token`` (a terminal sweep by a lease holder): only reservations made
    under a lease token no newer than it are ended. A stale owner whose run was taken over
    therefore never ends the new owner's in-flight reservations. Unlinked reservations
    (``include_unlinked``) are ended only when created before ``unlinked_before``, the
    moment the sweep's owner confirmed its lease: a newer owner could only have made them
    after that. A takeover passes ``None`` for both and ends them all.
    """
    done: list[ReservationSettlement] = []
    for res in ledger.open_reservations(workspace_id, run_id, include_unlinked=include_unlinked):
        if max_fencing_token is not None:
            frame = _frame(res, workspace_id, run_id)
            if frame is None:
                if unlinked_before is None or res.created_at >= unlinked_before:
                    continue  # left to a takeover
            elif frame.fencing_token is not None and frame.fencing_token > max_fencing_token:
                continue  # made by a newer owner of the run: not ours to end
        action, step_key, reason = _decide(res, workspace_id, run_id, steps)
        note = {
            "settled_by": f"execution_{context}",
            "settlement_reason": reason,
            "execution_run_id": run_id,
        }
        try:
            if action == "released":
                entry = ledger.reconcile(
                    res.reservation_id,
                    is_cancelled=True,
                    is_pre_dispatch=True,
                    description=f"run {run_id} {context}: {reason}",
                    metadata=note,
                )
            else:
                # No usage: unknown, at the reservation's estimate (cost and tokens).
                entry = ledger.reconcile(
                    res.reservation_id,
                    usage=None,
                    description=f"Unknown usage after run {run_id} {context}: {reason}",
                    metadata=note,
                )
        except ReservationNotFoundError:
            continue
        if entry.metadata.get("settled_by") != note["settled_by"]:
            continue  # settled meanwhile (by its original owner): the first settlement stands
        done.append(ReservationSettlement(res.reservation_id, action, step_key, reason))
    return done


def ledgers_of(*budgets: Any) -> list[UsageLedger]:
    """The distinct usage ledgers behind budgets (combined scopes are looked through)."""
    seen: list[UsageLedger] = []
    paths: set[str] = set()
    pending = list(budgets)
    while pending:
        budget = pending.pop(0)
        if budget is None:
            continue
        inner = getattr(budget, "budgets", None)
        if isinstance(inner, tuple):
            pending.extend(inner)
            continue
        ledger = getattr(getattr(budget, "manager", None), "ledger", None)
        if not isinstance(ledger, UsageLedger) or any(ledger is held for held in seen):
            continue
        if ledger.db_path:
            path = str(Path(ledger.db_path).resolve())
            if path in paths:
                continue
            paths.add(path)
        seen.append(ledger)
    return seen


async def settle_run_reservations_async(
    budgets: Iterable[Any],
    *,
    workspace_id: str,
    run_id: str,
    steps: Mapping[str, Any] | None,
    include_unlinked: bool = False,
    context: str = "takeover",
    max_fencing_token: int | None = None,
    unlinked_before: float | None = None,
) -> list[ReservationSettlement]:
    """:func:`settle_run_reservations` over every ledger of ``budgets``, off the loop."""
    done: list[ReservationSettlement] = []
    for ledger in ledgers_of(*budgets):
        done.extend(
            await asyncio.to_thread(
                settle_run_reservations,
                ledger,
                workspace_id=workspace_id,
                run_id=run_id,
                steps=steps,
                include_unlinked=include_unlinked,
                context=context,
                max_fencing_token=max_fencing_token,
                unlinked_before=unlinked_before,
            )
        )
    return done


def budget_scope(budget: Any) -> dict[str, str] | None:
    """``{"workspace_id", "run_id"}`` of a ``RunBudget`` (``None`` for anything else)."""
    from nexura_agents.usage.manager import RunBudget

    if isinstance(budget, RunBudget):
        return {"workspace_id": budget.workspace_id, "run_id": budget.run_id}
    return None


def restore_run_budget(budget: Any) -> Any:
    """The persisted handle of a ``RunBudget``'s scope (``manager.get_run_budget``).

    With a durable ledger the manager restores the scope's named policy, hierarchy and
    model; the attached handle is returned when the manager has no record of the scope.
    """
    from nexura_agents.usage.manager import RunBudget

    if not isinstance(budget, RunBudget):
        return budget
    restored = None
    with contextlib.suppress(Exception):
        restored = budget.manager.get_run_budget(budget.run_id)
    if isinstance(restored, RunBudget) and restored.workspace_id == budget.workspace_id:
        return restored
    return budget
