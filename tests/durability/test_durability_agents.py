"""A6 S0: SDK ``Runner(checkpoint_store=...)`` under real SIGKILL / SIGSTOP.

Rows of the design §4 matrix for the agent path, on a SQLite file and on Postgres
(``nexura_postgres.PostgresCheckpointStore``, A6 S5; skipped without
``NEXURA_TEST_POSTGRES_URI``). The S5 rows that were ``xfail(strict=True)`` now pass.

Effects go to the external effect service; ``ledger.applied`` is how often the effect
really happened. ``charge`` is a plain (non-idempotent) tool; ``reserve`` is
``@tool(idempotent=True)`` and sends a key derived from ``ctx.run_id`` and
``ctx.tool_call_id``, as docs/durable-runs.md suggests today. ``pay`` is a plain tool
that requires approval (K7/K8 rows, A6 S11).

Budgets (A6 S11): rows run with ``budget=True`` use a ``RunBudget`` on a SQLite usage
ledger shared by every process; tools reserve a model call before their effect. The rows
assert that no reservation is left ``active`` after the run is terminal (or paused after a
takeover), that a dispatched step's reservation is settled as unknown usage at its
estimate, and that each process restores the budget policy from the ledger.
"""

from __future__ import annotations

from typing import Any

import pytest
from durability_harness import Backend, Harness

pytestmark = pytest.mark.durability

ON_BACKENDS = pytest.mark.parametrize("backend", ["sqlite", "postgres"], indirect=True)


def _crash_run(
    harness: Harness, tool: str, point: str, mode: str = "forever", **params: Any
) -> tuple[Any, str]:
    """Start a run that blocks at ``point``; return the child and the run id."""
    run = harness.spawn("agent", "run", mode="run", tool=tool, block={point: mode}, **params)
    run_id = str(run.wait_marker(point)["run_id"])
    return run, run_id


def _recover(harness: Harness, run_id: str, name: str = "recover", **params: Any) -> dict[str, Any]:
    return harness.spawn("agent", name, mode="recover", run_id=run_id, **params).result()


@ON_BACKENDS
def test_kill_during_non_idempotent_tool_pauses_outcome_unknown(
    harness: Harness, backend: Backend
) -> None:
    """K3': the remote committed, then the process died. Recovery must not repeat it.

    Ledger (A6 S11, the run has a ``RunBudget`` on a shared SQLite usage ledger): the
    tool's reservation is left ``active`` by the kill; the takeover settles it as unknown
    usage at its estimate (its intent was dispatched) before anything runs.
    """
    from durability_child import ledger_reservations

    backend.require("agent")
    run, run_id = _crash_run(harness, "charge", "in_tool", budget=True)
    run.kill()
    assert harness.ledger.applied("charge") == 1
    before = ledger_reservations(harness.db_path, run_id)
    assert [(r["for"], r["status"], r["step_key"], r["tracked"]) for r in before] == [
        ("charge", "active", "turn:1/call:c1", True),
        (None, "committed", "turn:1/model", False),
    ], before

    result = _recover(harness, run_id, budget=True)
    assert result["status"] == "paused", result
    assert result["reason"] == "outcome_unknown_after_crash"
    assert result["pending"] == ["charge"]
    assert harness.ledger.applied("charge") == 1

    rows = ledger_reservations(harness.db_path, run_id)
    assert [r for r in rows if r["status"] in ("active", "expired")] == [], rows
    charge = next(r for r in rows if r["for"] == "charge")
    assert charge["settlement"]["usage_state"] == "unknown"
    assert charge["settlement"]["settled_by"] == "execution_takeover"
    assert charge["settlement"]["amount_usd"] == charge["estimated_cost_usd"]
    assert charge["settlement"]["tokens"] == charge["estimated_tokens"]


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


def _no_active_reservation(harness: Harness, run_id: str) -> list[dict[str, Any]]:
    from durability_child import ledger_reservations

    rows = ledger_reservations(harness.db_path, run_id)
    assert rows, "nothing was reserved"
    assert [r for r in rows if r["status"] in ("active", "expired")] == [], rows
    return rows


@ON_BACKENDS
def test_paused_for_approval_killed_then_approved_from_another_process(
    harness: Harness, backend: Backend
) -> None:
    """K7: paused for approval, the process is killed, another process approves: one
    payment, the run completes, the budget policy comes from the ledger and the completed
    run leaves no reservation active."""
    backend.require("agent")
    run, run_id = _crash_run(harness, "pay", "after_paused", budget=True)
    run.kill()
    assert harness.ledger.applied("pay") == 0

    done = harness.spawn("agent", "approve", mode="resume", run_id=run_id, budget=True).result()
    assert done["status"] == "completed", done
    assert done["budget_max_tokens"] == 1_000_000
    assert harness.ledger.applied("pay") == 1
    rows = _no_active_reservation(harness, run_id)
    pay = [r for r in rows if r["for"] == "pay"]
    assert len(pay) == 1 and pay[0]["settlement"]["usage_state"] != "unknown", rows


@ON_BACKENDS
def test_approval_claimed_then_killed_recover_dispatches_once_under_that_approval(
    harness: Harness, backend: Backend
) -> None:
    """K8 (§2.7): the approval was claimed (recorded in the run's journal and consumed),
    then the process died before dispatching. Recovery dispatches under the same approval:
    one payment, the run completes; the approval cannot be used again; no reservation is
    left active."""
    backend.require("agent")
    paused = harness.spawn("agent", "run", mode="run", tool="pay", budget=True).result()
    assert paused["status"] == "paused", paused
    run_id = paused["run_id"]
    approve = harness.spawn(
        "agent",
        "approve",
        mode="resume",
        run_id=run_id,
        budget=True,
        block={"after_consume_approval": "forever"},
    )
    approve.wait_marker("after_consume_approval")
    approve.kill()
    assert harness.ledger.applied("pay") == 0

    done = _recover(harness, run_id, budget=True)
    assert done["status"] == "completed", done
    assert harness.ledger.applied("pay") == 1

    again = harness.spawn(
        "agent",
        "again",
        mode="resume",
        run_id=run_id,
        budget=True,
        interruption=paused["interruption_json"],
    ).result()
    assert again.get("error_type") == "ApprovalDecisionError", again
    assert harness.ledger.applied("pay") == 1
    rows = _no_active_reservation(harness, run_id)
    assert len([r for r in rows if r["for"] == "pay"]) == 1, rows


def test_kill_during_non_idempotent_tool_pauses_without_a_budget(
    harness: Harness, backend: Backend
) -> None:
    """K3' as before S11 (SQLite, no budget attached; S11 review m6)."""
    backend.require("agent")
    run, run_id = _crash_run(harness, "charge", "in_tool")
    run.kill()
    assert harness.ledger.applied("charge") == 1
    result = _recover(harness, run_id)
    assert result["status"] == "paused", result
    assert result["reason"] == "outcome_unknown_after_crash"
    assert result["pending"] == ["charge"]
    assert result["budget_max_tokens"] is None
    assert harness.ledger.applied("charge") == 1
