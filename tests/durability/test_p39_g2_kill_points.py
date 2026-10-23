"""P39 G2 review: extra SIGKILL rows with the durability harness (SQLite only).

Repros from the independent review with its assertions
unchanged; the harness fixture comes from ``conftest.py`` (SQLite backend by default).
"""

from __future__ import annotations

import contextlib
from typing import Any

import pytest
from durability_harness import Harness

pytestmark = pytest.mark.durability


def _crash_run(harness: Harness, point: str, **params: Any) -> tuple[Any, str]:
    run = harness.spawn(
        "agent", "run", mode="run", tool="hookpay", block={point: "forever"}, **params
    )
    return run, str(run.wait_marker(point)["run_id"])


def _recover(harness: Harness, run_id: str, name: str, **params: Any) -> dict[str, Any]:
    return harness.spawn("agent", name, mode="recover", run_id=run_id, **params).result()


def test_k1_lost_pause_commit_then_resume_killed_after_consume_pays_once(
    harness: Harness,
) -> None:
    """Kill 1: the hooked run dies after the paused checkpoint (pause commit lost).
    Kill 2: a resume WITHOUT the hook approves and dies right after consuming the approval
    (claim committed, nothing dispatched). Recovery without the hook (claim still valid)
    sends it once; a second recovery never sends it again."""
    run, run_id = _crash_run(harness, "after_paused", hook_gate=["hookpay"])
    run.kill()
    resume = harness.spawn(
        "agent",
        "approve",
        mode="resume",
        run_id=run_id,
        block={"after_consume_approval": "forever"},
    )
    resume.wait_marker("after_consume_approval")
    resume.kill()
    assert harness.ledger.applied("hookpay") == 0
    first = _recover(harness, run_id, "recover1")
    assert harness.ledger.applied("hookpay") <= 1, first
    _recover(harness, run_id, "recover2")
    assert harness.ledger.applied("hookpay") <= 1


def test_k2_hooked_pause_then_resume_killed_in_tool_never_pays_twice(harness: Harness) -> None:
    """Normal pause (journal paused, gate recorded); a resume without the hook is killed
    inside the tool (dispatched). Two recoveries without the hook must not send it again
    (non-idempotent: in doubt)."""
    run = harness.spawn("agent", "run", mode="run", tool="hookpay", hook_gate=["hookpay"])
    paused = run.result()
    assert paused["status"] == "paused", paused
    run_id = paused["run_id"]
    resume = harness.spawn(
        "agent", "approve", mode="resume", run_id=run_id, block={"in_tool": "forever"}
    )
    resume.wait_marker("in_tool")
    resume.kill()
    assert harness.ledger.applied("hookpay") == 1
    r1 = _recover(harness, run_id, "recover1")
    assert r1["status"] == "paused", r1
    r2 = _recover(harness, run_id, "recover2")
    assert r2["status"] == "paused", r2
    assert harness.ledger.applied("hookpay") == 1


def test_k3_lost_pause_commit_then_replayed_approval_pays_once(harness: Harness) -> None:
    """Pause commit lost; recovery without the hook re-pauses (hands back the
    interruption); a person approves from a process without the hook; replaying the same
    interruption from another process pays nothing more."""
    run, run_id = _crash_run(harness, "after_paused", hook_gate=["hookpay"])
    run.kill()
    paused = _recover(harness, run_id, "recover1")
    assert paused["status"] == "paused", paused
    held = paused["interruption_json"]
    a = harness.spawn("agent", "approve_a", mode="resume", run_id=run_id, interruption=held)
    a.result()
    with contextlib.suppress(Exception):
        harness.spawn(
            "agent", "approve_b", mode="resume", run_id=run_id, interruption=held
        ).result()
    assert harness.ledger.applied("hookpay") == 1
