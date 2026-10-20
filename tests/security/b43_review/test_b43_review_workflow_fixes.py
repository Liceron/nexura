# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""B43 review regression (promoted from .autopilot/b43/repros/test_b43_review_workflow.py).

Adversarial review repros for the workflow engine approval path. Each test asserts the correct
behaviour; all failed before the B43 review fixes. In-memory stores only.
"""

from __future__ import annotations

import asyncio
import dataclasses
import time
from typing import Any

import pytest
from nexura_agents.contracts.result import ModuleResult

from nexura_runtime.runtime.approvals import ApprovalExpiredError
from nexura_runtime.workflow import (
    MemoryWorkflowStore,
    ModuleRegistry,
    WorkflowEngine,
    WorkflowNode,
    WorkflowSpec,
    interrupt,
)


def _gated_fn_engine(store: Any, calls: list[dict[str, Any]], **kw: Any) -> WorkflowEngine:
    reg = ModuleRegistry()

    def send(state: dict[str, Any]) -> dict[str, Any]:
        calls.append(dict(state))
        return {"sent": True}

    reg.register("send", send)
    spec = WorkflowSpec(
        name="w",
        entry_node="send",
        nodes={"send": WorkflowNode(id="send", handler="send", requires_approval=True)},
    )
    return WorkflowEngine(spec, reg, store=store, **kw)


class _SlowClaimStore(MemoryWorkflowStore):
    """A store whose claim is slow (lock contention, busy SQLite, GC pause)."""

    async def claim_approval(self, workflow_run_id: str, approval_id: str) -> bool:
        await asyncio.sleep(0.5)
        return await super().claim_approval(workflow_run_id, approval_id)


async def test_workflow_approval_expiry_is_enforced_at_dispatch_not_before_claim() -> None:
    """workflow.py resume(): expiry checked (l.1973) before the claim (l.2009), never again."""
    calls: list[dict[str, Any]] = []
    eng = _gated_fn_engine(_SlowClaimStore(), calls, approval_ttl=0.3)
    p = await eng.run({"to": "a"})
    assert p.status == "paused"
    with pytest.raises(ApprovalExpiredError):
        await eng.resume(p.workflow_run_id, p.pending_approval.approval_id)
    assert calls == []  # dispatched ~0.2 s after expires_at on current code


async def test_old_pending_row_without_expiry_is_not_resumable_forever() -> None:
    """PendingApproval.from_dict: a stored row with no expires_at never expires, even when
    the engine has approval_ttl and the row was created long ago (cf. gate identity_missing)."""
    calls: list[dict[str, Any]] = []
    store = MemoryWorkflowStore()
    eng = _gated_fn_engine(store, calls, approval_ttl=3600.0)
    p = await eng.run({"to": "a"})
    cp = await store.get(p.workflow_run_id)
    assert cp is not None and cp.pending_approval is not None
    # Simulate a row stored before approval expiry existed, created 30 days ago.
    cp.pending_approval = dataclasses.replace(
        cp.pending_approval, expires_at=None, created_at=time.time() - 30 * 86400
    )
    await store.save(cp)
    with pytest.raises(ApprovalExpiredError):
        await eng.resume(p.workflow_run_id, p.pending_approval.approval_id)
    assert calls == []


async def test_state_update_int_key_collision_regates_function_node() -> None:
    """canonical_digest collapses {1: x} into "1": an edit the handler sees keeps the approval."""
    calls: list[dict[str, Any]] = []
    eng = _gated_fn_engine(MemoryWorkflowStore(), calls)
    p = await eng.run({"1": "approved-recipient"})
    res = await eng.resume(
        p.workflow_run_id,
        p.pending_approval.approval_id,
        state_update={1: "attacker@example.com"},  # type: ignore[dict-item]
    )
    assert res.status == "paused", "an edit the handler reads must need a fresh approval"
    assert calls == []


class _AskingModule:
    """A module with no write operations (never gated) that asks a question."""

    def __init__(self) -> None:
        self.granted: list[bool] = []

    async def execute(self, ctx: Any, inputs: dict[str, Any], config: dict[str, Any]) -> Any:
        self.granted.append(ctx.approval_granted)
        answer = interrupt("which account?")
        return ModuleResult(outputs={"answer": answer})


async def test_resume_interrupt_does_not_grant_approval_to_an_ungated_node() -> None:
    """workflow.py resume_interrupt(): node_already_approved=True for every node, so a node
    nobody approved runs with ExecutionContext.approval_granted=True."""
    mod = _AskingModule()
    reg = ModuleRegistry()
    reg.register("ask", mod)
    spec = WorkflowSpec(
        name="i", entry_node="ask", nodes={"ask": WorkflowNode(id="ask", handler="ask")}
    )
    eng = WorkflowEngine(spec, reg, store=MemoryWorkflowStore())
    p = await eng.run({"x": 1})
    assert p.status == "paused" and p.interrupt is not None
    done = await eng.resume_interrupt(p.workflow_run_id, p.interrupt["interrupt_id"], "acct-1")
    assert done.status == "completed"
    assert mod.granted == [False, False]
