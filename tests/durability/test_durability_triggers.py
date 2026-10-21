"""A6 S0/S10: trigger worker jobs and the Studio store sweep under real SIGKILL.

Each child builds the service app (``create_api_app``) on one SQLite file and runs a
``TriggerWorker`` with a 1 s lease, like ``nexura-runtime worker``. The job's workflow is
``a`` (``note``) -> ``b`` (``pay``); both effects are undeclared, so ``non_idempotent``
(founder decision 1).

Before A6 S10 a reclaimed job started a NEW run from the entry node, so every effect
repeated, and a job whose run completed just before ``complete_job`` ran the whole
workflow again. Since S10 (founder decision 3) the reclaim continues the same run
(``WorkflowEngine.recover``) and a completed run is never run again. The Postgres rows
keep the workflow state, its journal and the trigger queue (``PostgresTriggerStore``,
A6 S10b) in Postgres, each test in its own schema; Studio run records stay in SQLite.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from durability_harness import POSTGRES_ENV, Backend, Harness

pytestmark = pytest.mark.durability

LEASE = 1.0

ON_BACKENDS = pytest.mark.parametrize("backend", ["sqlite", "postgres"], indirect=True)


async def _admin(uri: str, statement: str) -> None:
    import asyncpg  # type: ignore[import-untyped]

    conn = await asyncpg.connect(uri)
    try:
        await conn.execute(statement)
    finally:
        await conn.close()


@pytest.fixture
def backend(request: pytest.FixtureRequest) -> Iterator[Backend]:
    """The conftest backends; a Postgres one gets a throwaway schema per test (fixed job
    ids, and a stale claimed job of one test must never be reclaimed by another)."""
    name = getattr(request, "param", "sqlite")
    if name != "postgres":
        yield request.getfixturevalue(f"{name}_backend")
        return
    uri = os.environ.get(POSTGRES_ENV)
    if not uri:
        pytest.skip(f"{POSTGRES_ENV} is not set; Postgres durability rows need a disposable DB")
    schema = f"trig_{uuid.uuid4().hex[:12]}"
    asyncio.run(_admin(uri, f'CREATE SCHEMA "{schema}"'))
    try:
        yield Backend("postgres", f"{uri}{'&' if '?' in uri else '?'}search_path={schema}")
    finally:
        asyncio.run(_admin(uri, f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))


def _job_run_id(harness: Harness) -> str | None:
    if harness.backend.name == "postgres":  # the trigger queue is in Postgres (A6 S10b)
        from nexura_postgres import PostgresTriggerStore

        store = PostgresTriggerStore(str(harness.backend.dsn))
        try:
            job = store.get_job("job-1")
        finally:
            store.close()
        return None if job is None else job.run_id
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
def test_kill_mid_run_reclaim_continues_the_same_run(harness: Harness, backend: Backend) -> None:
    """Worker killed inside ``pay``; another worker reclaims the expired job.

    Before S10: the old run was marked interrupted and a new run started from ``a``, so
    ``note`` and ``pay`` both happened twice. Now the same run continues; ``note`` is not
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
def test_kill_after_run_completed_before_complete_job_does_not_rerun(
    harness: Harness, backend: Backend
) -> None:
    """Worker killed after the run completed but before ``complete_job`` committed.

    Before S10: the reclaim ran the whole workflow again. Now the job is completed with
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


def test_service_start_spares_a_live_owner_and_interrupts_a_dead_one(harness: Harness) -> None:
    """Starting another service on the database (as ``nexura-runtime worker`` does next to
    a live ``serve``) leaves a live process's running run alone; once that process is
    killed, the next start marks it interrupted.

    B46 (commit 1e47d0a) did this with the ``StudioStore`` open-time sweep. A6 S9: the run
    is journaled, so the service decides from its run lease (the killed worker's lease
    owner is a process of this host that has exited). SQLite only here (trigger jobs);
    the two-process service test in ``test_durability_service.py`` also runs on Postgres.
    """
    from fastapi.testclient import TestClient

    from nexura_runtime.service.api.app import create_api_app

    def status(run_id: str) -> str | None:
        app = create_api_app(db_path=harness.db_path, profile="local-owner")
        with TestClient(app):  # startup: run statuses are reconciled with the journal
            pass
        db = sqlite3.connect(str(harness.db_path))
        try:
            row = db.execute(
                "SELECT status FROM nexura_studio_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        finally:
            db.close()
        return None if row is None else str(row[0])

    worker = harness.spawn(
        "trigger", "worker-1", setup=True, lease_seconds=LEASE, block={"in_pay": "forever"}
    )
    worker.wait_marker("in_pay", timeout=30)
    run_id = _job_run_id(harness)
    assert run_id is not None
    assert status(run_id) == "running"  # live owner: not interrupted
    assert worker.alive()

    worker.kill()
    assert status(run_id) == "interrupted"  # owner gone: interrupted
