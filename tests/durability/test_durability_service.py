"""A6 S9: two service processes on one database, under real SIGKILL.

Each child is one ``serve`` process: ``create_api_app`` on the test's SQLite file (Studio
run records, always SQLite) with its workflow state and execution journal in the same
file, or in Postgres (``workflow_state_url``) when ``NEXURA_TEST_POSTGRES_URI`` is set. The
workflow is ``a`` (``note``) -> ``b`` (``pay``); each performs one effect through the
external effect service, whose own ledger counts what really happened.

What S9 promises:

- a second service starting on the same database never marks a run that a live process
  executes ``interrupted`` (B46 did this with an owner-process check on SQLite only; now the
  run lease decides, on either backend);
- once the owner is killed and its lease expired, the run is shown ``interrupted`` and
  ``POST /runs/{id}/recover`` from another process continues it: an idempotent step runs
  again with the same key, a non-idempotent one pauses ``effect_unknown`` until a person
  resolves it; no effect happens twice.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any

import pytest
from durability_harness import Child, Harness

pytestmark = pytest.mark.durability

LEASE = 2.0

ON_BACKENDS = pytest.mark.parametrize("backend", ["sqlite", "postgres"], indirect=True)


def _studio_run(harness: Harness) -> tuple[str, str]:
    """The one Studio run of the test: (run id, status)."""
    db = sqlite3.connect(str(harness.db_path))
    try:
        rows = db.execute("SELECT run_id, status FROM nexura_studio_runs").fetchall()
    finally:
        db.close()
    assert len(rows) == 1, rows
    return str(rows[0][0]), str(rows[0][1])


def _serve(harness: Harness, name: str, mode: str, **params: Any) -> Child:
    return harness.spawn("service", name, mode=mode, lease_seconds=LEASE, **params)


def _started_and_killed_at(harness: Harness, point: str, **effects: Any) -> str:
    """serve-1 starts the run and is SIGKILLed at ``point``; serve-2 starts meanwhile."""
    owner = _serve(harness, "serve-1", "start", block={point: "forever"}, **effects)
    owner.wait_marker(point, timeout=60)
    run_id, status = _studio_run(harness)
    assert status == "running"

    # A second service starting on the same database (a `worker` next to `serve`).
    observer = _serve(harness, "serve-2", "observe", run_id=run_id, **effects).result(timeout=60)
    assert observer["seen_status"] == "running"
    assert _studio_run(harness) == (run_id, "running")
    assert owner.alive()

    owner.kill()
    time.sleep(LEASE + 0.5)  # its run lease expires (nobody renews it)
    return run_id


@ON_BACKENDS
def test_recover_from_another_service_reruns_an_idempotent_step_with_its_key(
    harness: Harness,
) -> None:
    run_id = _started_and_killed_at(harness, "in_note", note_effect="idempotent")
    assert harness.ledger.applied("note") == 1

    result = _serve(harness, "serve-3", "recover", run_id=run_id, note_effect="idempotent").result(
        timeout=60
    )
    assert result["seen_status"] == "interrupted"
    assert result["code"] == 200, result
    assert result["status"] == "completed"
    assert _studio_run(harness) == (run_id, "completed")
    # The note step ran again with the same key; the remote de-duplicated it.
    assert harness.ledger.requests("note") == 2
    assert harness.ledger.applied("note") == 1
    keys = {row["key"] for row in harness.ledger.request_rows("note")}
    assert len(keys) == 1 and None not in keys
    assert harness.ledger.applied("pay") == 1


@ON_BACKENDS
def test_recover_pauses_a_non_idempotent_step_until_a_person_resolves_it(
    harness: Harness,
) -> None:
    run_id = _started_and_killed_at(harness, "in_pay", note_effect="pure")
    assert harness.ledger.applied("pay") == 1

    paused = _serve(harness, "serve-3", "recover", run_id=run_id, note_effect="pure").result(
        timeout=60
    )
    assert paused["seen_status"] == "interrupted"
    assert paused["code"] == 200, paused
    assert paused["status"] == "paused"
    assert paused["pause_reason"] == "effect_unknown"
    assert paused["effect_id"]
    assert _studio_run(harness) == (run_id, "paused")
    assert harness.ledger.requests("pay") == 1  # never repeated silently

    done = _serve(
        harness,
        "serve-4",
        "resolve",
        run_id=run_id,
        note_effect="pure",
        effect_id=paused["effect_id"],
        resolution="confirmed",
        result={"payd": "confirmed"},
    ).result(timeout=60)
    assert done["resolved"] == 200, done
    assert done["status"] == "completed", done
    assert done["state"]["payd"] == "confirmed"
    assert _studio_run(harness) == (run_id, "completed")
    assert harness.ledger.requests("pay") == 1
    assert harness.ledger.applied("note") == 1
