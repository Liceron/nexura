"""A6 S0: trigger worker jobs and the Studio store sweep under real SIGKILL.

Each child builds the service app (``create_api_app``) on one SQLite file and runs a
``TriggerWorker`` with a 1 s lease, like ``nexura-runtime worker``. The job's workflow is
``a`` (``note``) -> ``b`` (``pay``); both effects are undeclared, so ``non_idempotent``
(founder decision 1).

Today a reclaimed job starts a NEW run from the entry node, so every effect repeats, and a
job whose run completed just before ``complete_job`` runs the whole workflow again. Target
(founder decision 3, A6 S10): the reclaim continues the same run and a completed run is
never run again. Postgres rows wait for ``PostgresTriggerStore`` (A6 S10b).
"""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest
from durability_harness import POSTGRES_STORES, Backend, Harness

pytestmark = pytest.mark.durability

LEASE = 1.0

ON_BACKENDS = pytest.mark.parametrize(
    "backend",
    [
        "sqlite",
        pytest.param(
            "postgres",
            marks=pytest.mark.xfail(
                strict=True, raises=AssertionError, reason=POSTGRES_STORES["trigger"][1]
            ),
        ),
    ],
    indirect=True,
)

S10_SAME_RUN = pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="A6 S10: a reclaimed job continues its run (engine.recover); no new run",
)


def _job_run_id(harness: Harness) -> str | None:
    db = sqlite3.connect(str(harness.db_path))
    try:
        row = db.execute(
            "SELECT run_id FROM durable_trigger_jobs WHERE job_id = 'job-1'"
        ).fetchone()
    finally:
        db.close()
    return None if row is None else row[0]


def _first_worker_killed_at(harness: Harness, point: str) -> str:
    worker = harness.spawn(
        "trigger", "worker-1", setup=True, lease_seconds=LEASE, block={point: "forever"}
    )
    worker.wait_marker(point, timeout=30)
    run_id = _job_run_id(harness)
    worker.kill()
    assert run_id is not None
    return run_id


def _second_worker(harness: Harness) -> dict[str, Any]:
    return harness.spawn("trigger", "worker-2", lease_seconds=LEASE).result(timeout=40)


def _effects(harness: Harness) -> tuple[int, int]:
    return harness.ledger.applied("note"), harness.ledger.applied("pay")


@ON_BACKENDS
@S10_SAME_RUN
def test_kill_mid_run_reclaim_continues_the_same_run(harness: Harness, backend: Backend) -> None:
    """Worker killed inside ``pay``; another worker reclaims the expired job.

    Today: the old run is marked interrupted and a new run starts from ``a``, so ``note``
    and ``pay`` both happen twice. Target: the same run continues; ``note`` is not
    repeated and ``pay`` (outcome unknown) is not repeated either.
    """
    backend.require("trigger")
    first_run = _first_worker_killed_at(harness, "in_pay")
    assert _effects(harness) == (1, 1)

    result = _second_worker(harness)
    job = result["job"]
    assert _effects(harness) == (1, 1), f"today: effects {_effects(harness)}, {result}"
    assert job["run_id"] == first_run


@ON_BACKENDS
@S10_SAME_RUN
def test_kill_after_run_completed_before_complete_job_does_not_rerun(
    harness: Harness, backend: Backend
) -> None:
    """Worker killed after the run completed but before ``complete_job`` committed.

    Today: the reclaim runs the whole workflow again. Target: the job is completed with
    the run it already has; no effect repeats.
    """
    backend.require("trigger")
    first_run = _first_worker_killed_at(harness, "before_complete_job")
    assert _effects(harness) == (1, 1)

    result = _second_worker(harness)
    job = result["job"]
    assert _effects(harness) == (1, 1), f"today: effects {_effects(harness)}, {result}"
    assert job["status"] == "completed"
    assert job["run_id"] == first_run


def test_studio_store_open_spares_a_live_owner_and_sweeps_a_dead_one(harness: Harness) -> None:
    """B46 (commit 1e47d0a): opening ``StudioStore`` from another process (as
    ``nexura-runtime worker`` does next to a live ``serve``) leaves a live process's
    running run alone; once that process is killed, the next open marks it interrupted.

    SQLite only: ``StudioStore`` has no other backend. A6 S9 retires this sweep in favour
    of lease expiry; this test then moves to the service's two-process test.
    """
    from nexura_runtime.studio.store import StudioStore

    def status(run_id: str) -> str | None:
        import asyncio

        store = StudioStore(harness.db_path)  # the open-time sweep runs here
        try:
            return asyncio.run(store.get_run_status(run_id))
        finally:
            store._close_sync()

    worker = harness.spawn(
        "trigger", "worker-1", setup=True, lease_seconds=LEASE, block={"in_pay": "forever"}
    )
    worker.wait_marker("in_pay", timeout=30)
    run_id = _job_run_id(harness)
    assert run_id is not None
    assert status(run_id) == "running"  # live owner: not swept
    assert worker.alive()

    worker.kill()
    assert status(run_id) == "interrupted"  # owner gone: swept
