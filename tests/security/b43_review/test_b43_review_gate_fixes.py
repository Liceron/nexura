# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""B43 review regression (promoted from .autopilot/b43/repros/test_b43_review_gate.py).

Adversarial review repros for the ApprovalGate / ApprovalPolicyManager / ApprovalModule.
Each test asserts the correct behaviour; all failed before the B43 review fixes. Local SQLite only.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from nexura_runtime.contracts import ExecutionContext

from nexura_runtime.runtime.approvals import (
    ApprovalDecisionRecord,
    ApprovalError,
    ApprovalGate,
    ApprovalModule,
    ApprovalPolicyManager,
    ApprovalRequest,
    ApprovalRequiredError,
)


def _approve(manager: ApprovalPolicyManager, workspace: str, approval_id: str) -> None:
    req = manager.get_request(workspace, approval_id)
    assert req is not None
    manager.submit_decision(
        workspace,
        approval_id,
        ApprovalDecisionRecord(
            approval_id=approval_id,
            workspace_id=workspace,
            decided_by_principal_id="reviewer",
            decided_by_role="admin",
            decision="approved",
            expected_payload_digest=req.payload_digest,
        ),
    )


class _SyncTool:
    name = "transfer"

    def __init__(self) -> None:
        self.calls: list[Any] = []

    def run(self, amount: Any = None, **kw: Any) -> str:
        self.calls.append(amount)
        return f"sent {amount}"


class _AsyncTool:
    name = "transfer_async"

    def __init__(self) -> None:
        self.calls: list[Any] = []

    async def run(self, amount: Any = None, **kw: Any) -> str:
        self.calls.append(amount)
        return f"sent {amount}"


CTX = ExecutionContext(workspace_id="local", principal_id="p", run_id="r1", node_id="n1")


def test_wrapped_sync_tool_positional_args_are_bound_to_the_approval(tmp_path: Path) -> None:
    """gate.py sync_wrapped: payload = kwargs only; *args reach the tool unapproved."""
    manager = ApprovalPolicyManager(tmp_path / "a.db")
    gate = ApprovalGate(manager)
    tool = gate.wrap_tool(_SyncTool())
    with pytest.raises(ApprovalRequiredError) as paused:
        tool.run(1, context=CTX)  # reviewer is shown payload {} for "transfer 1"
    approval_id = paused.value.details["approval_id"]
    _approve(manager, "local", approval_id)
    # Same approval, different positional argument: must be refused, nothing sent.
    with pytest.raises(ApprovalError):
        tool.run(1_000_000, context=CTX, approval_id=approval_id)
    assert tool.calls == []


async def test_wrapped_async_tool_positional_args_are_bound_to_the_approval(
    tmp_path: Path,
) -> None:
    """gate.py async_wrapped: same gap for coroutine tools."""
    manager = ApprovalPolicyManager(tmp_path / "a.db")
    gate = ApprovalGate(manager)
    tool = gate.wrap_tool(_AsyncTool())
    with pytest.raises(ApprovalRequiredError) as paused:
        await tool.run(1, context=CTX)
    approval_id = paused.value.details["approval_id"]
    _approve(manager, "local", approval_id)
    with pytest.raises(ApprovalError):
        await tool.run(1_000_000, context=CTX, approval_id=approval_id)
    assert tool.calls == []


def test_claim_cannot_be_backdated_past_expiry(tmp_path: Path) -> None:
    """manager.claim_for_dispatch(now=...) trusts a caller-supplied clock."""
    manager = ApprovalPolicyManager(tmp_path / "a.db")
    gate = ApprovalGate(manager)
    with pytest.raises(ApprovalRequiredError) as paused:
        gate.check_or_request_approval(CTX, "act", {"v": 1}, expiry_seconds=0.2)
    approval_id = paused.value.details["approval_id"]
    _approve(manager, "local", approval_id)
    req = manager.get_request("local", approval_id)
    assert req is not None
    time.sleep(0.4)  # now past expires_at
    outcome = manager.claim_for_dispatch(
        "local",
        approval_id,
        "worker",
        identity_digest=req.identity_digest,
        now=req.expires_at - 100,  # stale / skewed clock
    )
    assert outcome == "expired"


async def test_approval_module_cannot_decide_another_workspaces_approval(tmp_path: Path) -> None:
    """module.py: inputs['workspace_id'] overrides the caller's context workspace."""
    manager = ApprovalPolicyManager(tmp_path / "a.db")
    gate = ApprovalGate(manager)
    victim = ExecutionContext(workspace_id="ws-b", principal_id="p", run_id="rb", node_id="nb")
    with pytest.raises(ApprovalRequiredError) as paused:
        gate.check_or_request_approval(victim, "wire", {"amount": 10})
    approval_id = paused.value.details["approval_id"]

    module = ApprovalModule(manager)
    attacker = ExecutionContext(
        workspace_id="ws-a", principal_id="mallory", run_id="ra", node_id="na"
    )
    with pytest.raises(Exception):  # noqa: B017 - any refusal is correct
        await module.invoke(
            attacker,
            {
                "operation": "decide",
                "workspace_id": "ws-b",
                "approval_id": approval_id,
                "decision": "approved",
            },
        )
    req = manager.get_request("ws-b", approval_id)
    assert req is not None and req.status == "pending"


async def test_approval_module_create_binds_to_the_callers_run(tmp_path: Path) -> None:
    """module.py: inputs['run_id'] lets a node mint a request bound to another run."""
    manager = ApprovalPolicyManager(tmp_path / "a.db")
    module = ApprovalModule(manager)
    ctx = ExecutionContext(workspace_id="local", principal_id="p", run_id="my-run", node_id="n")
    out = await module.invoke(
        ctx, {"operation": "create", "run_id": "someone-elses-run", "action_payload": {"x": 1}}
    )
    req: ApprovalRequest | None = manager.get_request("local", out.outputs["approval_id"])
    assert req is not None
    assert req.run_id == "my-run"
