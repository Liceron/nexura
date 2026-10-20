"""A6 S0: ``WorkflowEngine`` with ``SQLiteWorkflowStore`` under real SIGKILL.

The workflow is ``a`` (handler ``note``) -> ``b`` (handler ``pay``, optionally behind an
approval gate). Each handler performs one effect through the external effect service.
Neither declares an effect class, so by founder decision 1 (design §6) both count as
``non_idempotent``: after a crash they must not repeat silently.

Today no public API continues a top-level ``running`` run; ``fork`` is the only way on,
and it re-runs the node. Tests asserting the target (``WorkflowEngine.recover``, A6 S6)
are ``xfail(strict=True)``. Postgres rows wait for ``PostgresWorkflowStore`` (A6 S8).
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from durability_harness import POSTGRES_STORES, Backend, Harness

pytestmark = pytest.mark.durability

ON_BACKENDS = pytest.mark.parametrize(
    "backend",
    [
        "sqlite",
        pytest.param(
            "postgres",
            marks=pytest.mark.xfail(
                strict=True, raises=AssertionError, reason=POSTGRES_STORES["workflow"][1]
            ),
        ),
    ],
    indirect=True,
)

S6_RECOVER = pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="A6 S6: public WorkflowEngine.recover() with per-node intents and leases",
)


def _run_id() -> str:
    return f"wf-{uuid.uuid4().hex[:12]}"


def _checkpoint(harness: Harness, run_id: str) -> Any:
    from nexura_runtime.workflow import SQLiteWorkflowStore

    store = SQLiteWorkflowStore(harness.db_path)
    try:
        return asyncio.run(store.get(run_id))
    finally:
        store.close_sync()


def _resume_in_parent(harness: Harness, run_id: str, approval_id: str, gated: bool) -> str:
    """Resume in the test process (only for refusals: no handler runs); the error type."""
    from durability_child import WORKSPACE, workflow_registry, workflow_spec

    from nexura_runtime.workflow import SQLiteWorkflowStore, WorkflowEngine

    store = SQLiteWorkflowStore(harness.db_path)
    engine = WorkflowEngine(
        workflow_spec(gated), workflow_registry(), store=store, workspace_id=WORKSPACE
    )
    try:
        asyncio.run(engine.resume(run_id, approval_id))
    except Exception as exc:
        return type(exc).__name__
    finally:
        store.close_sync()
    return "no error"


def _step(harness: Harness, name: str, run_id: str, mode: str, **params: Any) -> dict[str, Any]:
    return harness.spawn("workflow", name, run_id=run_id, mode=mode, **params).result()


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
@S6_RECOVER
def test_mid_node_kill_recover_pauses_effect_unknown(harness: Harness, backend: Backend) -> None:
    """K3': target is ``paused`` with ``pause_reason="effect_unknown"``, one payment."""
    backend.require("workflow")
    run_id = _run_id()
    _kill_at(harness, run_id, "in_pay")

    result = _step(harness, "recover", run_id, "recover")
    assert result.get("status") == "paused", f"today: {result}"
    assert result["pause_reason"] == "effect_unknown"
    assert result["pending_effect"] is True
    assert (harness.ledger.applied("note"), harness.ledger.applied("pay")) == (1, 1)


@ON_BACKENDS
@S6_RECOVER
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
    _kill_at(harness, run_id, "after_save_paused_b", gated=True)
    assert (harness.ledger.applied("note"), harness.ledger.applied("pay")) == (1, 0)

    result = _step(harness, "approve", run_id, "resume", gated=True)
    assert result["status"] == "completed", result
    assert (harness.ledger.applied("note"), harness.ledger.applied("pay")) == (1, 1)


def _claim_then_kill(harness: Harness, run_id: str) -> str:
    paused = _step(harness, "run", run_id, "run", gated=True)
    assert paused["status"] == "paused", paused
    approve = harness.spawn(
        "workflow",
        "approve",
        run_id=run_id,
        mode="resume",
        gated=True,
        block={"after_claim_approval": "forever"},
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
@S6_RECOVER
def test_approval_claimed_then_killed_recover_dispatches_once_under_that_approval(
    harness: Harness, backend: Backend
) -> None:
    """K8 target (§2.7): claimed but no intent written -> the new owner dispatches under
    the same approval (after re-checking digest and expiry): one payment, run completed."""
    backend.require("workflow")
    run_id = _run_id()
    _claim_then_kill(harness, run_id)

    result = _step(harness, "recover", run_id, "recover", gated=True)
    assert result.get("status") == "completed", f"today: {result}"
    assert harness.ledger.applied("pay") == 1
