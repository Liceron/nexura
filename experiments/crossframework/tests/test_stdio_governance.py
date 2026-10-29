"""Official MCP 2.3 client -> Nexura child processes, with local fakes and no listeners."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

import pytest
from crossframework.client import call_refund, connect
from crossframework.demo import run_demo
from crossframework.review import decide
from crossframework.state import WORKSPACE
from crossframework.stdio import serve
from mcp import Client, StdioServerParameters

from nexura_runtime.runtime.approvals import (
    ApprovalPolicyManager,
    ApprovalRequest,
    MutatedArgumentsError,
)


def rows(state_dir: Path, filename: str) -> list[dict[str, Any]]:
    path = state_dir / filename
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def action(amount: int = 200, **overrides: Any) -> dict[str, Any]:
    return {"request_id": "R-1", "order_id": "ORDER-1", "amount": amount, **overrides}


def request(state_dir: Path, approval_id: str) -> ApprovalRequest:
    result = ApprovalPolicyManager(state_dir / "approvals.sqlite3").get_request(
        WORKSPACE, approval_id
    )
    assert result is not None
    return result


def query(state_dir: Path, sql: str) -> list[tuple]:
    conn = sqlite3.connect(state_dir / "approvals.sqlite3")
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def approve(state_dir: Path, pending: dict[str, Any]) -> None:
    # The reviewer reads the stored action; the agent is not the approval authority.
    stored = request(state_dir, pending["approval_id"])
    decide(state_dir, stored.approval_id, stored.payload_digest)


def test_experiment_does_not_load_repository_conftest() -> None:
    assert "nexura_tests_local_client_helper" not in sys.modules
    assert "fastapi" not in sys.modules


async def test_real_reader_is_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NEXURA_CROSSFRAMEWORK_PIPE_READER", raising=False)
    calls = []

    class Probe:
        async def serve_stdio(self, **kwargs: Any) -> None:
            calls.append(kwargs)

    await serve(Probe())
    assert calls == [{}]


async def test_scripted_agent_demo_and_correlated_audit(tmp_path: Path) -> None:
    transcript = await run_demo(tmp_path)
    assert [(r["amount"], r["status"], r["executed"]) for r in transcript] == [
        (50, "completed", True),
        (200, "pending_approval", False),
        (200, "completed", True),
        (900, "blocked", False),
    ]
    assert [r["amount"] for r in rows(tmp_path, "fake-refunds.jsonl")] == [50, 200]
    stored = request(tmp_path, transcript[1]["approval_id"])
    assert stored.action_payload == action(request_id="demo-200")
    assert stored.claimed_by and stored.decision.decision == "approved"
    lifecycle = rows(tmp_path, "lifecycle.jsonl")
    assert [r["event"] for r in lifecycle] == [
        "dispatch",
        "completed",
        "pending_approval",
        "approved",
        "approval_claimed",
        "dispatch",
        "completed",
        "blocked",
    ]
    assert all(r["policy_digest"] == stored.spec_digest for r in lifecycle)
    policy = rows(tmp_path, "policy.jsonl")
    assert [(r["action"], r.get("rule_id")) for r in policy] == [
        ("allow", None),
        ("allow", None),
        ("allow", None),
        ("block", "refund-ceiling"),
    ]
    assert all("amount" not in r and "order_id" not in r for r in policy + lifecycle)


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_wire_results_are_honest_for_unmodified_clients(tmp_path: Path, mode: str) -> None:
    async with connect(tmp_path, mode=mode) as session:
        for amount, status in [(200, "pending_approval"), (900, "blocked")]:
            raw = await session.call_tool("refund", action(amount, request_id=f"wire-{amount}"))
            assert raw.is_error
            first, envelope = raw.content[0].text.split("\n", 1)
            assert first.startswith("NOT EXECUTED:") and first.endswith("No money has moved.")
            assert json.loads(envelope)["executed"] is False
            assert json.loads(envelope)["status"] == status
        raw = await session.call_tool("refund", action(50))
        assert not raw.is_error and json.loads(raw.content[0].text)["executed"] is True


async def test_reviewers_double_proposal_repro(tmp_path: Path) -> None:
    async with connect(tmp_path) as session:
        first = await call_refund(session, action())
        second = await call_refund(session, action())  # Lost proposal reply / model re-plan.
        assert first["approval_id"] == second["approval_id"] == "refund-R-1"
        assert query(tmp_path, "SELECT COUNT(*) FROM approval_requests") == [(1,)]
        approve(tmp_path, first)
        done = await call_refund(session, action())  # Retry without approval_id also finds it.
        assert done["executed"] is True
        assert await call_refund(session, action()) == done
        assert len(rows(tmp_path, "fake-refunds.jsonl")) == 1


async def test_pause_poll_and_exact_binding(tmp_path: Path) -> None:
    async with connect(tmp_path) as session:
        pending = await call_refund(session, action())
        retry = action(approval_id=pending["approval_id"])
        assert await call_refund(session, retry) == pending
        approve(tmp_path, pending)
        for modified in [{"amount": 201}, {"amount": 50}, {"order_id": "ORDER-2"}]:
            outcome = await call_refund(session, {**retry, **modified})
            assert outcome == {
                "status": "request_id_conflict",
                "executed": False,
                "prior_phase": "ready",
            }
            assert request(tmp_path, pending["approval_id"]).claimed_by is None
        outcome = await call_refund(session, {**retry, "request_id": "another-action"})
        assert outcome["status"] == "approval_refused" and not outcome["executed"]
        assert request(tmp_path, pending["approval_id"]).claimed_by is None
        over_ceiling = await call_refund(session, {**retry, "amount": 900})
        assert over_ceiling["status"] == "request_id_conflict" and over_ceiling["rule"]
        done = await call_refund(session, retry)
        assert done["executed"] and await call_refund(session, retry) == done
        assert len(rows(tmp_path, "fake-refunds.jsonl")) == 1


async def test_repeated_ungated_action_and_restart_replay(tmp_path: Path) -> None:
    async with connect(tmp_path) as session:
        done = await call_refund(session, action(50))
        assert await call_refund(session, action(50)) == done
        for amount in (60, 900):
            raw = await session.call_tool("refund", action(amount))
            first_line = raw.content[0].text.splitlines()[0]
            assert raw.is_error and "money has moved for it" in first_line
            assert "No money has moved" not in first_line
            conflict = await call_refund(session, action(amount))
            assert conflict["status"] == "request_id_conflict" and not conflict["executed"]
            assert conflict["prior_phase"] == "completed"
    async with connect(tmp_path) as session:
        assert await call_refund(session, action(50)) == done
    assert len(rows(tmp_path, "fake-refunds.jsonl")) == 1


async def test_lost_approved_response_returns_recorded_success(tmp_path: Path) -> None:
    async with connect(tmp_path) as session:
        pending = await call_refund(session, action())
        approve(tmp_path, pending)
        raw = await session.call_tool("refund", action(approval_id=pending["approval_id"]))
        lost = json.loads(raw.content[0].text)  # Host loses/ignores this successful reply.
    async with connect(tmp_path) as session:
        assert await call_refund(session, action()) == lost
        assert await call_refund(session, action(approval_id=pending["approval_id"])) == lost
    assert len(rows(tmp_path, "fake-refunds.jsonl")) == 1
    assert query(tmp_path, "SELECT phase FROM crossframework_dispatches") == [("completed",)]


@pytest.mark.parametrize("amount", [50, 200])
async def test_interrupted_effect_never_runs_again(tmp_path: Path, amount: int) -> None:
    async with connect(
        tmp_path, server_module="tests.fault_server", extra_args=("--fault", "half-effect")
    ) as session:
        if amount > 100:
            approve(tmp_path, await call_refund(session, action(amount)))
        outcome = await call_refund(session, action(amount))
        assert outcome == {"status": "outcome_unknown", "executed": False}
        assert len(rows(tmp_path, "fake-refunds.jsonl")) == 1
    async with connect(tmp_path) as session:
        raw = await session.call_tool("refund", action(amount))
        assert raw.is_error and "money may have moved" in raw.content[0].text
        assert await call_refund(session, action(amount)) == outcome
        changed = action(amount + 1)
        raw = await session.call_tool("refund", changed)
        first_line = raw.content[0].text.splitlines()[0]
        assert raw.is_error and "money may have moved" in first_line
        assert "No money has moved" not in first_line
        conflict = await call_refund(session, changed)
        assert conflict["status"] == "request_id_conflict" and not conflict["executed"]
        assert conflict["prior_phase"] == "dispatching"
    assert len(rows(tmp_path, "fake-refunds.jsonl")) == 1
    assert query(tmp_path, "SELECT phase FROM crossframework_dispatches") == [("dispatching",)]


async def test_completed_audit_failure_returns_success_and_warning(tmp_path: Path) -> None:
    async with connect(
        tmp_path, server_module="tests.fault_server", extra_args=("--fault", "completion-audit")
    ) as session:
        approve(tmp_path, await call_refund(session, action()))
        raw = await session.call_tool("refund", action())
        assert not raw.is_error
        done = json.loads(raw.content[0].text)
        assert done["executed"] and done["status"] == "completed" and done["audit_warning"]
    async with connect(tmp_path) as session:
        assert await call_refund(session, action()) == done
    assert len(rows(tmp_path, "fake-refunds.jsonl")) == 1


@pytest.mark.parametrize("amount", [50, 200])
async def test_separate_process_race_dispatches_once(tmp_path: Path, amount: int) -> None:
    # Four independent server processes, each with its own gate/SQLite connections.
    async with AsyncExitStack() as stack:
        sessions = [await stack.enter_async_context(connect(tmp_path)) for _ in range(4)]
        if amount > 100:
            pending = await call_refund(sessions[0], action(amount))
            approve(tmp_path, pending)
        results = await asyncio.gather(*(call_refund(s, action(amount)) for s in sessions))
        assert any(r["executed"] for r in results)
        assert all(r["status"] in {"completed", "outcome_unknown"} for r in results)
        final = await asyncio.gather(*(call_refund(s, action(amount)) for s in sessions))
        assert all(r == final[0] and r["executed"] for r in final)
    assert len(rows(tmp_path, "fake-refunds.jsonl")) == 1
    assert query(tmp_path, "SELECT COUNT(*) FROM crossframework_dispatches") == [(1,)]


async def test_reviewer_rejection_is_explicit_and_terminal_for_request_id(tmp_path: Path) -> None:
    async with connect(tmp_path) as session:
        pending = await call_refund(session, action())
        decide(tmp_path, pending["approval_id"], pending["payload_digest"], "rejected")
        for arguments in (action(), action(approval_id=pending["approval_id"])):
            raw = await session.call_tool("refund", arguments)
            assert raw.is_error and "reviewer rejected" in raw.content[0].text
            assert json.loads(raw.content[0].text.split("\n", 1)[1])["status"] == "rejected"
        assert rows(tmp_path, "fake-refunds.jsonl") == []


async def test_approved_then_expired_before_atomic_claim(tmp_path: Path) -> None:
    async with connect(tmp_path, approval_ttl=0.5) as session:
        pending = await call_refund(session, action())
        approve(tmp_path, pending)
        assert request(tmp_path, pending["approval_id"]).status == "approved"
        await asyncio.sleep(0.6)
        outcome = await call_refund(session, action())
        assert outcome["status"] == "expired" and not outcome["executed"]
        assert request(tmp_path, pending["approval_id"]).claimed_by is None
        assert rows(tmp_path, "fake-refunds.jsonl") == []


async def test_unknown_id_and_wrong_review_digest_cannot_dispatch(tmp_path: Path) -> None:
    async with connect(tmp_path) as session:
        pending = await call_refund(session, action())
        with pytest.raises(MutatedArgumentsError):
            decide(tmp_path, pending["approval_id"], "0" * 64)
        assert request(tmp_path, pending["approval_id"]).status == "pending"
        result = await call_refund(session, action(approval_id="unknown"))
        assert result["status"] == "approval_refused" and not result["executed"]
        assert rows(tmp_path, "fake-refunds.jsonl") == []


async def test_required_audit_failure_stops_before_dispatch(tmp_path: Path) -> None:
    (tmp_path / "policy.jsonl").mkdir()
    async with connect(tmp_path) as session:
        for amount in (50, 200):
            outcome = await call_refund(session, action(amount, request_id=f"audit-{amount}"))
            assert outcome["status"] == "error" and not outcome["executed"]
        assert rows(tmp_path, "fake-refunds.jsonl") == []
        assert rows(tmp_path, "lifecycle.jsonl") == []


@pytest.mark.parametrize(
    "amount,expected",
    [(100, "completed"), (101, "pending_approval"), (500, "pending_approval"), (501, "blocked")],
)
async def test_exact_thresholds(tmp_path: Path, amount: int, expected: str) -> None:
    async with connect(tmp_path) as session:
        assert (await call_refund(session, action(amount)))["status"] == expected
    assert len(rows(tmp_path, "fake-refunds.jsonl")) == (1 if expected == "completed" else 0)


@pytest.mark.parametrize(
    "invalid",
    [
        {"amount": "50"},
        {"amount": True},
        {"amount": -1},
        {"amount": 0},
        {"amount": 200.5},
        {"unexpected": "not-approved"},
        {"order_id": ""},
    ],
)
async def test_invalid_arguments_return_explicit_non_execution(
    tmp_path: Path, invalid: dict[str, Any]
) -> None:
    async with connect(tmp_path) as session:
        raw = await session.call_tool("refund", {**action(), **invalid})
        assert raw.is_error and "NOT EXECUTED:" in raw.content[0].text
        outcome = await call_refund(session, {**action(), **invalid})
        assert outcome["status"] == "invalid" and not outcome["executed"]
        assert rows(tmp_path, "fake-refunds.jsonl") == []
        assert query(tmp_path, "SELECT COUNT(*) FROM crossframework_dispatches") == [(0,)]


async def test_stock_server_approval_is_an_error_without_resume_identity() -> None:
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "tests.stock_server"],
        cwd=str(Path(__file__).resolve().parents[1]),
        env={
            "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
            "PYTHONDONTWRITEBYTECODE": "1",
            "NEXURA_CROSSFRAMEWORK_PIPE_READER": os.environ.get(
                "NEXURA_CROSSFRAMEWORK_PIPE_READER", ""
            ),
        },
    )
    async with Client(parameters, read_timeout_seconds=15.0) as session:
        result = await session.call_tool("issue_refund", {"order_id": "A1", "amount": 200})
        assert result.is_error
        assert "needs human approval" in result.content[0].text
        assert "Nothing was executed" in result.content[0].text
        assert result.structured_content is None
