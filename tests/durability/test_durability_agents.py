"""A6 S0: SDK ``Runner(checkpoint_store=...)`` under real SIGKILL / SIGSTOP.

Rows of the design §4 matrix for the agent path, on a SQLite file and on Postgres
(``nexura_postgres.PostgresCheckpointStore``, A6 S5; skipped without
``NEXURA_TEST_POSTGRES_URI``). The S5 rows that were ``xfail(strict=True)`` now pass.

Effects go to the external effect service; ``ledger.applied`` is how often the effect
really happened. ``charge`` is a plain (non-idempotent) tool; ``reserve`` is
``@tool(idempotent=True)`` and sends a key derived from ``ctx.run_id`` and
``ctx.tool_call_id``, as docs/durable-runs.md suggests today.
"""

from __future__ import annotations

from typing import Any

import pytest
from durability_harness import Backend, Harness

pytestmark = pytest.mark.durability

ON_BACKENDS = pytest.mark.parametrize("backend", ["sqlite", "postgres"], indirect=True)


def _crash_run(harness: Harness, tool: str, point: str, mode: str = "forever") -> tuple[Any, str]:
    """Start a run that blocks at ``point``; return the child and the run id."""
    run = harness.spawn("agent", "run", mode="run", tool=tool, block={point: mode})
    run_id = str(run.wait_marker(point)["run_id"])
    return run, run_id


def _recover(harness: Harness, run_id: str, name: str = "recover", **params: Any) -> dict[str, Any]:
    return harness.spawn("agent", name, mode="recover", run_id=run_id, **params).result()


@ON_BACKENDS
def test_kill_during_non_idempotent_tool_pauses_outcome_unknown(
    harness: Harness, backend: Backend
) -> None:
    """K3': the remote committed, then the process died. Recovery must not repeat it."""
    backend.require("agent")
    run, run_id = _crash_run(harness, "charge", "in_tool")
    run.kill()
    assert harness.ledger.applied("charge") == 1

    result = _recover(harness, run_id)
    assert result["status"] == "paused", result
    assert result["reason"] == "outcome_unknown_after_crash"
    assert result["pending"] == ["charge"]
    assert harness.ledger.applied("charge") == 1


@ON_BACKENDS
def test_kill_during_idempotent_tool_reruns_and_remote_deduplicates(
    harness: Harness, backend: Backend
) -> None:
    """K3' for ``idempotent=True``: re-run with the same (tool-derived) key; one effect."""
    backend.require("agent")
    run, run_id = _crash_run(harness, "reserve", "in_tool")
    run.kill()

    result = _recover(harness, run_id)
    assert result["status"] == "completed", result
    assert result["final_output"] == "done"
    assert harness.ledger.requests("reserve") == 2  # the tool really ran twice
    assert harness.ledger.applied("reserve") == 1  # the remote de-duplicated on the key
    keys = [row["key"] for row in harness.ledger.request_rows("reserve")]
    assert keys[0] == keys[1] == f"{run_id}:c1"


@ON_BACKENDS
def test_idempotent_rerun_gets_the_same_runtime_idempotency_key(
    harness: Harness, backend: Backend
) -> None:
    """Contract §2.1: the runtime supplies the key; it is the same on the re-run."""
    backend.require("agent")
    run, run_id = _crash_run(harness, "reserve", "in_tool")
    run.kill()
    assert _recover(harness, run_id)["status"] == "completed"
    runtime_keys = [row["extra"]["runtime_key"] for row in harness.ledger.request_rows("reserve")]
    assert len(runtime_keys) == 2
    assert runtime_keys[0] is not None, "today the runtime supplies no idempotency key"
    assert runtime_keys[0] == runtime_keys[1]


@ON_BACKENDS
def test_kill_before_non_idempotent_dispatch_runs_it_once(
    harness: Harness, backend: Backend
) -> None:
    """K1: killed after the batch checkpoint but before the tool started.

    Today the ``tools_pending`` checkpoint is written at the start of the batch, so a call
    that never started looks uncertain: recovery pauses with ``outcome_unknown_after_crash``
    although nothing happened. Target: nothing was dispatched, so it runs (count 0 -> 1).
    """
    backend.require("agent")
    run, run_id = _crash_run(harness, "charge", "after_tools_pending")
    run.kill()
    assert harness.ledger.applied("charge") == 0

    result = _recover(harness, run_id)
    assert result["status"] == "completed", f"today: {result}"
    assert harness.ledger.applied("charge") == 1


@ON_BACKENDS
def test_kill_after_tool_result_commit_continues_without_repeating(
    harness: Harness, backend: Backend
) -> None:
    """K5: the tool result is committed; recovery continues from it."""
    backend.require("agent")
    run, run_id = _crash_run(harness, "charge", "after_tool_result")
    run.kill()
    assert harness.ledger.applied("charge") == 1

    result = _recover(harness, run_id)
    assert result["status"] == "completed", result
    assert result["final_output"] == "done"
    assert harness.ledger.applied("charge") == 1


@ON_BACKENDS
def test_stale_original_run_is_fenced_after_takeover(harness: Harness, backend: Backend) -> None:
    """K6: the original process is frozen (SIGSTOP) before the tool, another process takes
    over, then the original wakes up (SIGCONT).

    Today the original run writes without a lease, so the takeover succeeds at once and the
    woken original still performs the charge and completes. Target: it gets a lease-lost
    error before dispatch and performs nothing.
    """
    backend.require("agent")
    run, run_id = _crash_run(harness, "charge", "after_tools_pending", mode="go")
    run.stop()
    taker = _recover(harness, run_id, name="taker", claim_wait=10)
    assert "status" in taker or "error_type" in taker

    run.go("after_tools_pending")
    run.cont()
    stale = run.result()
    assert harness.ledger.applied("charge", who="run") == 0, f"today the stale run charged: {stale}"
    assert "LeaseLost" in str(stale.get("error_type")), stale


@ON_BACKENDS
def test_two_recoverers_race_and_one_wins(harness: Harness, backend: Backend) -> None:
    """K9: two processes recover the same run at once; the lease admits one."""
    backend.require("agent")
    run, run_id = _crash_run(harness, "reserve", "after_tools_pending")
    run.kill()
    racers = [
        harness.spawn(
            "agent",
            name,
            mode="recover",
            run_id=run_id,
            tool_delay=1.0,
            block={"ready": "go"},
        )
        for name in ("racer-1", "racer-2")
    ]
    for racer in racers:
        racer.wait_marker("ready")
    for racer in racers:
        racer.go("ready")
    results = [racer.result() for racer in racers]
    assert sorted(str(r.get("status")) for r in results) == ["None", "completed"], results
    loser = next(r for r in results if "error_type" in r)
    assert loser["error_type"] == "RunRecoveryError"
    assert "already being recovered" in loser["error"]
    assert harness.ledger.requests("reserve") == 1
