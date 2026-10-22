"""A6 S0: ``WorkflowEngine`` on SQLite and Postgres workflow stores under real SIGKILL.

The workflow is ``a`` (handler ``note``) -> ``b`` (handler ``pay``, optionally behind an
approval gate). Each handler performs one effect through the external effect service.
Neither declares an effect class, so by founder decision 1 (design §6) both count as
``non_idempotent``: after a crash they must not repeat silently.

``WorkflowEngine.recover`` (A6 S6) continues a top-level ``running`` run under the
execution contract; ``fork`` still re-runs the node (a new run). The Postgres rows run the
same scenarios on ``nexura_postgres.PostgresWorkflowStore`` (A6 S8; skipped without
``NEXURA_TEST_POSTGRES_URI``).

Budgets (A6 S11): the K3/K7/K8 rows run with a ``RunBudget`` on a SQLite usage ledger
shared by every process (scope = the run id, as in the service); each handler reserves a
model call before its effect and settles it after. The rows assert the ledger: no
reservation is left ``active`` once the run is terminal (or paused after a takeover), a
reservation whose step was dispatched when the process died is settled as unknown usage at
its estimate, and later processes restore the run's budget policy from the ledger.

The composite rows (A6 S7, design §4 "sub-workflow child / map item") run one step over
three parts: a map over items 0-2, a parallel step with branches a-c, or a sub-workflow
child running that map. The process is killed inside part 1, after its effect: part 0
finished, part 2 never started.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from durability_harness import Backend, Harness

pytestmark = pytest.mark.durability

ON_BACKENDS = pytest.mark.parametrize("backend", ["sqlite", "postgres"], indirect=True)


def _run_id() -> str:
    return f"wf-{uuid.uuid4().hex[:12]}"


def _with_store(harness: Harness, use: Callable[[Any], Awaitable[Any]]) -> Any:
    """Open the backend's workflow store in the test process, use it, close it."""

    async def go() -> Any:
        store: Any
        if harness.backend.name == "sqlite":
            from nexura_runtime.workflow import SQLiteWorkflowStore

            store = SQLiteWorkflowStore(harness.db_path)
        else:
            from nexura_postgres import PostgresWorkflowStore

            store = PostgresWorkflowStore(harness.backend.dsn)
        try:
            return await use(store)
        finally:
            await store.close()

    return asyncio.run(go())


def _checkpoint(harness: Harness, run_id: str) -> Any:
    return _with_store(harness, lambda store: store.get(run_id))


def _resume_in_parent(harness: Harness, run_id: str, approval_id: str, gated: bool) -> str:
    """Resume in the test process (only for refusals: no handler runs); the error type."""
    from durability_child import WORKSPACE, workflow_registry, workflow_spec

    from nexura_runtime.workflow import WorkflowEngine

    async def resume(store: Any) -> str:
        engine = WorkflowEngine(
            workflow_spec(gated), workflow_registry(), store=store, workspace_id=WORKSPACE
        )
        try:
            await engine.resume(run_id, approval_id)
        except Exception as exc:
            return type(exc).__name__
        return "no error"

    result: str = _with_store(harness, resume)
    return result


def _step(harness: Harness, name: str, run_id: str, mode: str, **params: Any) -> dict[str, Any]:
    return harness.spawn("workflow", name, run_id=run_id, mode=mode, **params).result()


def _reservations(harness: Harness, run_id: str) -> list[dict[str, Any]]:
    from durability_child import ledger_reservations

    return ledger_reservations(harness.db_path, run_id)


def _assert_none_active(harness: Harness, run_id: str) -> list[dict[str, Any]]:
    rows = _reservations(harness, run_id)
    assert rows, "the handlers reserved nothing"
    assert [r for r in rows if r["status"] in ("active", "expired")] == [], rows
    return rows


def _settled_normally(row: dict[str, Any]) -> bool:
    settlement = row["settlement"]
    return (
        row["status"] == "committed"
        and settlement is not None
        and settlement["usage_state"] != "unknown"
        and settlement["settled_by"] is None
    )


def _kill_at(harness: Harness, run_id: str, point: str, **params: Any) -> None:
    run = harness.spawn(
        "workflow", "run", run_id=run_id, mode="run", block={point: "forever"}, **params
    )
    run.wait_marker(point)
    run.kill()


@ON_BACKENDS
def test_mid_node_kill_leaves_run_stuck_and_fork_reruns_the_node(
    harness: Harness, backend: Backend
) -> None:
    """Today's behaviour (K3' on a top-level node), documented.

    The run stays ``running`` with its cursor on the node; ``resume`` refuses it (it is
    not paused); the only way on is ``fork``, which runs the node again: two payments.
    """
    backend.require("workflow")
    run_id = _run_id()
    _kill_at(harness, run_id, "in_pay")
    assert (harness.ledger.applied("note"), harness.ledger.applied("pay")) == (1, 1)

    checkpoint = _checkpoint(harness, run_id)
    assert (checkpoint.status, checkpoint.current_node_id) == ("running", "b")

    assert _resume_in_parent(harness, run_id, "none", gated=False) == "ApprovalError"

    forked = _step(harness, "fork", run_id, "fork")
    assert forked["status"] == "completed", forked
    assert harness.ledger.applied("note") == 1  # completed node not repeated
    assert harness.ledger.applied("pay") == 2  # the killed node ran again
    assert _checkpoint(harness, run_id).status == "running"  # the source run never ends


@ON_BACKENDS
def test_mid_node_kill_recover_pauses_effect_unknown(harness: Harness, backend: Backend) -> None:
    """K3': target is ``paused`` with ``pause_reason="effect_unknown"``, one payment.

    Ledger (A6 S11): the killed node's reservation is left ``active``; the takeover settles
    it as unknown usage at its estimate (its intent was dispatched) before anything runs.
    """
    backend.require("workflow")
    run_id = _run_id()
    _kill_at(harness, run_id, "in_pay", budget=True)
    before = _reservations(harness, run_id)
    assert [(r["for"], r["status"], r["step_key"], r["tracked"]) for r in before] == [
        ("note", "committed", "a#1", True),
        ("pay", "active", "b#2", True),
    ], before

    result = _step(harness, "recover", run_id, "recover", budget=True)
    assert result.get("status") == "paused", f"today: {result}"
    assert result["pause_reason"] == "effect_unknown"
    assert result["pending_effect"] is True
    assert (harness.ledger.applied("note"), harness.ledger.applied("pay")) == (1, 1)
    assert result["budget_max_tokens"] == 1_000_000  # policy restored from the ledger

    note, pay = _assert_none_active(harness, run_id)
    assert _settled_normally(note), note
    assert pay["status"] == "committed", pay
    assert pay["settlement"]["usage_state"] == "unknown"
    assert pay["settlement"]["settled_by"] == "execution_takeover"
    assert pay["settlement"]["amount_usd"] == pay["estimated_cost_usd"]
    assert pay["settlement"]["tokens"] == pay["estimated_tokens"]


@ON_BACKENDS
def test_kill_after_node_commit_recover_continues_from_the_cursor(
    harness: Harness, backend: Backend
) -> None:
    """K5 for ``a`` (= K1 for ``b``): continue at ``b`` without repeating ``a``."""
    backend.require("workflow")
    run_id = _run_id()
    _kill_at(harness, run_id, "after_save_running_b")
    assert (harness.ledger.applied("note"), harness.ledger.applied("pay")) == (1, 0)

    result = _step(harness, "recover", run_id, "recover")
    assert result.get("status") == "completed", f"today: {result}"
    assert (harness.ledger.applied("note"), harness.ledger.applied("pay")) == (1, 1)


@ON_BACKENDS
def test_paused_for_approval_killed_then_approved_from_another_process(
    harness: Harness, backend: Backend
) -> None:
    """K7: a paused run holds nothing in memory; another process resumes it."""
    backend.require("workflow")
    run_id = _run_id()
    _kill_at(harness, run_id, "after_save_paused_b", gated=True, budget=True)
    assert (harness.ledger.applied("note"), harness.ledger.applied("pay")) == (1, 0)

    result = _step(harness, "approve", run_id, "resume", gated=True, budget=True)
    assert result["status"] == "completed", result
    assert (harness.ledger.applied("note"), harness.ledger.applied("pay")) == (1, 1)
    # Ledger (A6 S11): the policy was restored in the approving process and the
    # completed run leaves no reservation active; each was settled by its own call.
    assert result["budget_max_tokens"] == 1_000_000
    rows = _assert_none_active(harness, run_id)
    assert [(r["for"], r["step_key"]) for r in rows] == [("note", "a#1"), ("pay", "b#2")]
    assert all(_settled_normally(r) for r in rows), rows


def _claim_then_kill(harness: Harness, run_id: str, **params: Any) -> str:
    paused = _step(harness, "run", run_id, "run", gated=True, **params)
    assert paused["status"] == "paused", paused
    approve = harness.spawn(
        "workflow",
        "approve",
        run_id=run_id,
        mode="resume",
        gated=True,
        block={"after_claim_approval": "forever"},
        **params,
    )
    approve.wait_marker("after_claim_approval")
    approve.kill()
    return str(paused["approval_id"])


@ON_BACKENDS
def test_approval_claimed_then_killed_is_consumed_and_nothing_dispatched(
    harness: Harness, backend: Backend
) -> None:
    """K8, today's behaviour documented: the approval is used up, the gated node never ran,
    and the run is left ``running``; a second resume with the same approval is refused."""
    backend.require("workflow")
    run_id = _run_id()
    approval_id = _claim_then_kill(harness, run_id)
    assert harness.ledger.applied("pay") == 0
    assert _checkpoint(harness, run_id).status == "running"

    assert _resume_in_parent(harness, run_id, approval_id, gated=True) == "ApprovalError"
    assert harness.ledger.applied("pay") == 0


@ON_BACKENDS
def test_approval_claimed_then_killed_recover_dispatches_once_under_that_approval(
    harness: Harness, backend: Backend
) -> None:
    """K8 target (§2.7): claimed but no intent written -> the new owner dispatches under
    the same approval (after re-checking digest and expiry): one payment, run completed."""
    backend.require("workflow")
    run_id = _run_id()
    _claim_then_kill(harness, run_id, budget=True)
    # Killed after the claim, before the gated node dispatched: it reserved nothing.
    assert [r["for"] for r in _reservations(harness, run_id)] == ["note"]

    result = _step(harness, "recover", run_id, "recover", gated=True, budget=True)
    assert result.get("status") == "completed", f"today: {result}"
    assert harness.ledger.applied("pay") == 1
    # Ledger (A6 S11): one reservation for the one dispatch, settled by that call; none
    # left active by the completed run; the policy came from the ledger.
    assert result["budget_max_tokens"] == 1_000_000
    rows = _assert_none_active(harness, run_id)
    assert [(r["for"], r["step_key"]) for r in rows] == [("note", "a#1"), ("pay", "b#2")]
    assert all(_settled_normally(r) for r in rows), rows


# ------------------------------------------------------------- composite steps (A6 S7)

COMPOSITE = pytest.mark.parametrize("shape", ["map", "parallel", "subworkflow"])


def _parts(shape: str) -> list[str]:
    """Effect names of parts 0, 1 and 2 of a composite shape."""
    if shape == "parallel":
        return ["branch_a", "branch_b", "branch_c"]
    return ["item0", "item1", "item2"]


def _kill_in_part_one(harness: Harness, run_id: str, shape: str, **params: Any) -> None:
    point = "in_branch_b" if shape == "parallel" else "in_item_1"
    _kill_at(harness, run_id, point, shape=shape, **params)


@ON_BACKENDS
@COMPOSITE
def test_composite_kill_mid_non_idempotent_part_pauses_naming_it(
    harness: Harness, backend: Backend, shape: str
) -> None:
    """K3' per part: finished parts are not repeated, the unstarted one runs, the run
    pauses ``effect_unknown`` on the part in doubt; a person confirms it from another
    process and the run completes. Every part's effect happened exactly once."""
    backend.require("workflow")
    run_id = _run_id()
    parts = _parts(shape)
    _kill_in_part_one(harness, run_id, shape)
    assert [harness.ledger.applied(p) for p in parts] == [1, 1, 0]

    paused = _step(harness, "recover", run_id, "recover", shape=shape)
    assert paused["status"] == "paused", paused
    assert paused["pause_reason"] == "effect_unknown"
    pending = paused["pending"]
    if shape == "parallel":
        assert (pending["part"], pending["branch"]) == ("branch", "b")
    elif shape == "map":
        assert (pending["part"], pending["item"], pending["step_key"]) == ("item", 1, "m#1[1]")
    else:
        assert pending["part"] == "subworkflow" and pending["run_id"] == f"{run_id}/m#1"
        assert pending["node_id"] == "m/m" and pending["step_key"] == "m#1[1]"
    assert [harness.ledger.applied(p) for p in parts] == [1, 1, 1]

    result = {"b": True} if shape == "parallel" else {"paid": 1}
    done = _step(
        harness, "resolve", run_id, "resolve", shape=shape, resolution="confirmed", result=result
    )
    assert done["status"] == "completed", done
    if shape == "parallel":
        assert done["state"] == {"items": [0, 1, 2], "a": True, "b": True, "c": True}
    else:
        assert done["state"]["paid"] == [0, 1, 2]
    assert [harness.ledger.applied(p) for p in parts] == [1, 1, 1]
    assert [harness.ledger.requests(p) for p in parts] == [1, 1, 1]


@ON_BACKENDS
@COMPOSITE
def test_composite_kill_mid_idempotent_part_reruns_it_with_the_same_key(
    harness: Harness, backend: Backend, shape: str
) -> None:
    """K3' per part, idempotent: only the part in doubt is sent again, with the key it had;
    the effect service de-duplicates it, and the run completes."""
    backend.require("workflow")
    run_id = _run_id()
    parts = _parts(shape)
    _kill_in_part_one(harness, run_id, shape, effect="idempotent")

    done = _step(harness, "recover", run_id, "recover", shape=shape, effect="idempotent")
    assert done["status"] == "completed", done
    assert [harness.ledger.applied(p) for p in parts] == [1, 1, 1]
    assert [harness.ledger.requests(p) for p in parts] == [1, 2, 1]
    first, second = (row["key"] for row in harness.ledger.request_rows(parts[1]))
    assert first == second and first.startswith("nx-")


# ------------------------------------------- the same rows without a budget (S11 review m6)


def test_mid_node_kill_recover_pauses_effect_unknown_without_a_budget(
    harness: Harness, backend: Backend
) -> None:
    """K3' as before S11 (SQLite, no budget attached): one payment, effect_unknown."""
    backend.require("workflow")
    run_id = _run_id()
    _kill_at(harness, run_id, "in_pay")
    result = _step(harness, "recover", run_id, "recover")
    assert result.get("status") == "paused", result
    assert result["pause_reason"] == "effect_unknown"
    assert result["pending_effect"] is True
    assert (harness.ledger.applied("note"), harness.ledger.applied("pay")) == (1, 1)
    assert "budget_max_tokens" not in result


def test_paused_for_approval_killed_then_approved_without_a_budget(
    harness: Harness, backend: Backend
) -> None:
    """K7 as before S11 (SQLite, no budget attached)."""
    backend.require("workflow")
    run_id = _run_id()
    _kill_at(harness, run_id, "after_save_paused_b", gated=True)
    result = _step(harness, "approve", run_id, "resume", gated=True)
    assert result["status"] == "completed", result
    assert (harness.ledger.applied("note"), harness.ledger.applied("pay")) == (1, 1)


def test_approval_claimed_then_killed_recover_dispatches_once_without_a_budget(
    harness: Harness, backend: Backend
) -> None:
    """K8 as before S11 (SQLite, no budget attached)."""
    backend.require("workflow")
    run_id = _run_id()
    _claim_then_kill(harness, run_id)
    result = _step(harness, "recover", run_id, "recover", gated=True)
    assert result.get("status") == "completed", result
    assert harness.ledger.applied("pay") == 1
