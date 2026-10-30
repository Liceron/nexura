# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""One bounded trigger pass on demand: the trigger tick (MVP WP9).

A service that scales to zero (Cloud Run with ``--min-instances 0``) has no process polling for
due schedules. An external scheduler (Cloud Scheduler) calls an authenticated endpoint instead,
and the endpoint calls :func:`run_trigger_tick`. Authentication belongs to the endpoint (the
closed edition's ``POST /internal/triggers/tick``); this module only runs the pass.

A pass ticks due schedules, reclaims expired jobs and runs pending jobs for at most
``budget_seconds`` (``TriggerWorker.run_pass``). It runs under the trigger store's named lease
``trigger-tick``, so one pass runs at a time across every instance sharing the store; a call
that finds the lease held returns ``{"status": "busy"}`` at once. The lease outlives the budget
by ``LEASE_MARGIN_SECONDS``, so a pass whose instance died is taken over once it expires. Jobs
themselves stay individually leased, so a job still running when the budget ends keeps running
in the background (on Cloud Run with CPU always allocated) and is never run twice.

The in-process poller (``create_api_app(run_trigger_worker=True)``) is independent and keeps
working: every step of a pass is a compare-and-swap in the store.
"""

from __future__ import annotations

import asyncio
import math
import socket
import time
import uuid
from typing import Any

#: The lease that keeps one tick pass at a time across instances.
TICK_LEASE = "trigger-tick"
#: Default time budget of one pass (Cloud Scheduler's attempt deadline is 60 s at most here).
DEFAULT_TICK_BUDGET_SECONDS = 45.0
#: Upper bound of a configured budget.
MAX_TICK_BUDGET_SECONDS = 300.0
#: How long the tick lease outlives the budget (so a dead instance's pass is taken over).
LEASE_MARGIN_SECONDS = 15.0


def check_tick_budget(budget_seconds: float | str) -> float:
    """``budget_seconds`` as a float, or ``ValueError`` unless 0 < budget <= 300."""
    try:
        budget = float(budget_seconds)
    except (TypeError, ValueError):
        raise ValueError("The trigger tick budget must be a number of seconds") from None
    if not math.isfinite(budget) or not 0 < budget <= MAX_TICK_BUDGET_SECONDS:
        raise ValueError(
            f"The trigger tick budget must be more than 0 and at most "
            f"{MAX_TICK_BUDGET_SECONDS:g} seconds"
        )
    return budget


def tick_worker(app_state: Any) -> Any:
    """The app's tick worker (created once): a ``TriggerWorker`` of its own, never the poller."""
    worker = getattr(app_state, "trigger_tick_worker", None)
    if worker is None:
        from nexura_runtime.service.cloud import shutdown_grace
        from nexura_runtime.service.triggers.worker import TriggerWorker

        worker = TriggerWorker(
            app_state,
            worker_id=f"tick-{socket.gethostname()}-{uuid.uuid4().hex[:8]}",
            shutdown_grace_seconds=shutdown_grace(20.0),
        )
        app_state.trigger_tick_worker = worker
    return worker


async def run_trigger_tick(
    app_state: Any, *, budget_seconds: float = DEFAULT_TICK_BUDGET_SECONDS
) -> dict[str, Any]:
    """Run one bounded trigger pass unless another instance is running one.

    Returns a small JSON-ready summary: ``status`` (``"ok"`` or ``"busy"``), the counts of
    ``TriggerWorker.run_pass`` (``"ok"`` only), ``elapsed_seconds`` and ``budget_seconds``.
    """
    budget = check_tick_budget(budget_seconds)
    started = time.monotonic()
    worker = tick_worker(app_state)
    store = worker.manager.store
    # A fresh holder per pass: two calls on one instance are two contenders too.
    holder = f"{worker.worker_id}/{uuid.uuid4().hex[:12]}"
    acquired = await asyncio.to_thread(
        store.acquire_lease, TICK_LEASE, holder, ttl_seconds=budget + LEASE_MARGIN_SECONDS
    )
    if not acquired:
        return {
            "status": "busy",
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "budget_seconds": budget,
        }
    try:
        counts = await worker.run_pass(max(0.001, budget - (time.monotonic() - started)))
    finally:
        await asyncio.to_thread(store.release_lease, TICK_LEASE, holder)
    return {
        "status": "ok",
        **counts,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "budget_seconds": budget,
    }


__all__ = [
    "DEFAULT_TICK_BUDGET_SECONDS",
    "LEASE_MARGIN_SECONDS",
    "MAX_TICK_BUDGET_SECONDS",
    "TICK_LEASE",
    "check_tick_budget",
    "run_trigger_tick",
    "tick_worker",
]
