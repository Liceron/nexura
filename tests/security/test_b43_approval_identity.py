"""B43: a standalone ApprovalGate approval authorises one exact action, once, before expiry.

Regression tests for the Codex review of 2026-10-18 (codex reviews/
NEXURA_CODEX_REVIEWS_RESEARCH_AND_CHAT.md, "Findings that change the safety conclusion"):
an approved request used to be accepted for a different run, node, spec digest, action kind
or action name as long as the payload matched, and could be claimed after ``expires_at``.
Everything here uses a disposable local SQLite database; nothing external is called.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from nexura_runtime.contracts import ExecutionContext
from nexura_runtime.contracts.canonical import canonical_digest

from nexura_runtime.runtime.approvals import (
    ApprovalClaimConflictError,
    ApprovalDecisionRecord,
    ApprovalError,
    ApprovalExpiredError,
    ApprovalGate,
    ApprovalIdentityMismatchError,
    ApprovalIdentityMissingError,
    ApprovalPolicyManager,
    ApprovalRequest,
    ApprovalRequiredError,
    MutatedArgumentsError,
    dispatch_identity_digest,
)
from nexura_runtime.storage.migrations import MIGRATIONS

PAYLOAD = {"value": 1}
ORIGINAL: dict[str, Any] = {
    "workspace_id": "local",
    "run_id": "original-run",
    "node_id": "original-node",
    "spec_digest": "original-spec",
    "action_kind": "original-kind",
    "action_name": "original-action",
}


@pytest.fixture
def manager(tmp_path: Path) -> ApprovalPolicyManager:
    return ApprovalPolicyManager(tmp_path / "approval.db")


@pytest.fixture
def gate(manager: ApprovalPolicyManager) -> ApprovalGate:
    return ApprovalGate(manager)


def _ctx(**overrides: Any) -> ExecutionContext:
    fields = {**ORIGINAL, **overrides}
    return ExecutionContext(
        workspace_id=fields["workspace_id"],
        principal_id="caller",
        run_id=fields["run_id"],
        node_id=fields["node_id"],
    )


def _call(gate: ApprovalGate, approval_id: str | None, **overrides: Any) -> ApprovalRequest:
    fields = {**ORIGINAL, **overrides}
    return gate.check_or_request_approval(
        _ctx(**overrides),
        action_name=fields["action_name"],
        payload=overrides.get("payload", PAYLOAD),
        approval_id=approval_id,
        spec_digest=fields["spec_digest"],
        action_kind=fields["action_kind"],
    )


def _approve(manager: ApprovalPolicyManager, approval_id: str, workspace: str = "local") -> None:
    req = manager.get_request(workspace, approval_id)
    assert req is not None
    manager.submit_decision(
        workspace,
        approval_id,
        ApprovalDecisionRecord(
            approval_id=approval_id,
            workspace_id=workspace,
            decided_by_principal_id="reviewer",
            decided_by_role=req.required_role,
            decision="approved",
            expected_payload_digest=req.payload_digest,
        ),
    )


def _approved_via_gate(gate: ApprovalGate, manager: ApprovalPolicyManager) -> str:
    """Pause through the gate (the normal path), then approve as a reviewer."""
    with pytest.raises(ApprovalRequiredError) as paused:
        _call(gate, None)
    approval_id = paused.value.approval_id
    _approve(manager, approval_id)
    return approval_id


def _unclaimed(manager: ApprovalPolicyManager, approval_id: str) -> bool:
    req = manager.get_request("local", approval_id)
    return req is not None and req.claimed_by is None


def test_request_records_full_dispatch_identity(
    gate: ApprovalGate, manager: ApprovalPolicyManager
) -> None:
    approval_id = _approved_via_gate(gate, manager)
    req = manager.get_request("local", approval_id)
    assert req is not None
    assert req.action_name == "original-action"
    assert req.identity_digest == dispatch_identity_digest(req.dispatch_identity())
    assert req.dispatch_identity() == {**ORIGINAL, "payload_digest": req.payload_digest}


@pytest.mark.parametrize(
    "field", ["run_id", "node_id", "spec_digest", "action_kind", "action_name"]
)
def test_substituting_an_identity_field_is_refused(
    gate: ApprovalGate, manager: ApprovalPolicyManager, field: str
) -> None:
    approval_id = _approved_via_gate(gate, manager)
    with pytest.raises(ApprovalIdentityMismatchError) as refused:
        _call(gate, approval_id, **{field: f"different-{field}"})
    assert refused.value.mismatched_fields == [field]
    assert refused.value.details["reason"] == "identity_mismatch"
    assert _unclaimed(manager, approval_id), "a refused claim must not consume the approval"
    # The original action still claims it, exactly once.
    assert _call(gate, approval_id).approval_id == approval_id
    with pytest.raises(ApprovalClaimConflictError):
        _call(gate, approval_id)


def test_substituting_the_workspace_is_refused(
    gate: ApprovalGate, manager: ApprovalPolicyManager
) -> None:
    approval_id = _approved_via_gate(gate, manager)
    with pytest.raises(ApprovalError, match="not found"):
        _call(gate, approval_id, workspace_id="other-workspace")
    # The stored identity also covers the workspace: a digest for another workspace fails.
    req = manager.get_request("local", approval_id)
    assert req is not None
    forged = dispatch_identity_digest({**req.dispatch_identity(), "workspace_id": "other"})
    outcome = manager.claim_for_dispatch("local", approval_id, "w", identity_digest=forged)
    assert outcome == "identity_mismatch"
    assert _unclaimed(manager, approval_id)


def test_substituting_the_payload_is_refused(
    gate: ApprovalGate, manager: ApprovalPolicyManager
) -> None:
    approval_id = _approved_via_gate(gate, manager)
    with pytest.raises(MutatedArgumentsError):
        _call(gate, approval_id, payload={"value": 2})
    assert _unclaimed(manager, approval_id)


def test_codex_probe_case_different_action_and_run_is_refused(
    gate: ApprovalGate, manager: ApprovalPolicyManager
) -> None:
    """The exact probe: request created directly on the manager, then claimed elsewhere."""
    req = manager.create_request(
        ApprovalRequest(
            approval_id="different_action_and_run",
            workspace_id="local",
            run_id="original-run",
            node_id="original-node",
            action_payload=PAYLOAD,
            required_role="admin",
            spec_digest="original-spec",
            expires_at=time.time() + 600,
        )
    )
    _approve(manager, req.approval_id)
    with pytest.raises(ApprovalIdentityMismatchError) as refused:
        gate.check_or_request_approval(
            ExecutionContext(
                workspace_id="local",
                principal_id="caller",
                run_id="different-run",
                node_id="different-node",
            ),
            action_name="different-action",
            payload=PAYLOAD,
            approval_id=req.approval_id,
            spec_digest="different-spec",
            action_kind="different-kind",
        )
    assert set(refused.value.mismatched_fields) == {
        "run_id",
        "node_id",
        "spec_digest",
        "action_kind",
        "action_name",
    }
    assert _unclaimed(manager, req.approval_id)


def test_expired_after_approval_is_refused(
    gate: ApprovalGate, manager: ApprovalPolicyManager
) -> None:
    approval_id = _approved_via_gate(gate, manager)
    # Simulate time passing after the approval (deterministic, no sleep).
    conn = manager._get_connection()
    try:
        conn.execute(
            "UPDATE approval_requests SET expires_at = ? WHERE approval_id = ?",
            (time.time() - 1, approval_id),
        )
    finally:
        conn.close()
    with pytest.raises(ApprovalExpiredError):
        _call(gate, approval_id)
    assert _unclaimed(manager, approval_id)


async def test_expired_after_approval_dispatches_nothing(
    gate: ApprovalGate, manager: ApprovalPolicyManager
) -> None:
    approval_id = _approved_via_gate(gate, manager)
    ran: list[str] = []

    async def handler() -> str:
        ran.append("sent")
        return "sent"

    req = manager.get_request("local", approval_id)
    assert req is not None
    # The claim compares the current time inside its UPDATE: a clock past expires_at fails.
    outcome = manager.claim_for_dispatch(
        "local", approval_id, "w", identity_digest=req.identity_digest, now=req.expires_at
    )
    assert outcome == "expired"
    conn = manager._get_connection()
    try:
        conn.execute(
            "UPDATE approval_requests SET expires_at = ? WHERE approval_id = ?",
            (time.time() - 1, approval_id),
        )
    finally:
        conn.close()
    with pytest.raises(ApprovalExpiredError):
        await gate.execute_gated_action(
            _ctx(),
            action_name=ORIGINAL["action_name"],
            payload=PAYLOAD,
            handler=handler,
            approval_id=approval_id,
            spec_digest=ORIGINAL["spec_digest"],
            action_kind=ORIGINAL["action_kind"],
        )
    assert ran == []


def test_normal_claim_works_once(gate: ApprovalGate, manager: ApprovalPolicyManager) -> None:
    approval_id = _approved_via_gate(gate, manager)
    claimed = _call(gate, approval_id)
    assert claimed.approval_id == approval_id
    stored = manager.get_request("local", approval_id)
    assert stored is not None and stored.claimed_by is not None
    with pytest.raises(ApprovalClaimConflictError):
        _call(gate, approval_id)


def test_concurrent_double_claim_is_single_use(tmp_path: Path) -> None:
    db = tmp_path / "race.db"
    manager = ApprovalPolicyManager(db)
    approval_id = _approved_via_gate(ApprovalGate(manager), manager)
    workers = 12
    barrier = threading.Barrier(workers)
    outcomes: list[str] = []
    lock = threading.Lock()

    def worker() -> None:
        own_gate = ApprovalGate(ApprovalPolicyManager(db))  # own connection per worker
        barrier.wait()
        try:
            _call(own_gate, approval_id)
            result = "claimed"
        except ApprovalClaimConflictError:
            result = "conflict"
        with lock:
            outcomes.append(result)

    threads = [threading.Thread(target=worker) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert sorted(outcomes) == ["claimed"] + ["conflict"] * (workers - 1)


def test_create_request_recomputes_a_forged_identity_digest(
    manager: ApprovalPolicyManager,
) -> None:
    req = manager.create_request(
        ApprovalRequest(
            approval_id="forged",
            workspace_id="local",
            run_id="r",
            action_payload=PAYLOAD,
            identity_digest="f" * 64,
            expires_at=time.time() + 60,
        )
    )
    assert req.identity_digest == dispatch_identity_digest(req.dispatch_identity())
    stored = manager.get_request("local", "forged")
    assert stored is not None and stored.identity_digest == req.identity_digest


def _legacy_database(path: Path, approval_id: str) -> None:
    """A database written before B43: schema versions 1-4 and an approved, unclaimed row."""
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, "
            "applied_at REAL NOT NULL)"
        )
        for m in MIGRATIONS:
            if m.version <= 4:
                conn.executescript(m.up_sql)
                conn.execute(
                    "INSERT INTO schema_migrations VALUES (?, ?, ?)",
                    (m.version, m.name, time.time()),
                )
        now = time.time()
        conn.execute(
            """
            INSERT INTO approval_requests (
                workspace_id, approval_id, run_id, node_id, attempt_id, action_kind,
                required_role, action_payload_json, payload_digest, spec_digest,
                policy_version, status, claimed_by, claimed_at, expires_at, decision_json,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'att_1', ?, 'admin', '{"value":1}', ?, ?, '1.0.0',
                      'approved', NULL, NULL, ?, NULL, ?, ?)
            """,
            (
                "local",
                approval_id,
                ORIGINAL["run_id"],
                ORIGINAL["node_id"],
                ORIGINAL["action_kind"],
                canonical_digest(PAYLOAD),
                ORIGINAL["spec_digest"],
                now + 600,
                now,
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def test_legacy_rows_without_identity_are_refused(tmp_path: Path) -> None:
    db = tmp_path / "legacy.db"
    _legacy_database(db, "legacy-approved")
    manager = ApprovalPolicyManager(db)  # runs migration 5
    ApprovalPolicyManager(db)  # and re-opening is idempotent
    conn = sqlite3.connect(db)
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(approval_requests)")}
        versions = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
    finally:
        conn.close()
    assert {"action_name", "identity_digest"} <= columns
    assert 5 in versions

    legacy = manager.get_request("local", "legacy-approved")
    assert legacy is not None and legacy.status == "approved" and legacy.identity_digest == ""

    gate = ApprovalGate(manager)
    with pytest.raises(ApprovalIdentityMissingError) as refused:
        _call(gate, "legacy-approved")
    assert refused.value.details["reason"] == "identity_missing"
    # Even a caller presenting the digest the row would have had cannot claim it.
    would_be = dispatch_identity_digest(legacy.dispatch_identity())
    assert (
        manager.claim_for_dispatch("local", "legacy-approved", "w", identity_digest=would_be)
        == "identity_missing"
    )
    assert not manager.claim_approval_for_dispatch(
        "local", "legacy-approved", "w", identity_digest=would_be
    )
    still = manager.get_request("local", "legacy-approved")
    assert still is not None and still.claimed_by is None


def test_wrapped_tool_without_context_resumes_only_the_same_tool(
    gate: ApprovalGate, manager: ApprovalPolicyManager
) -> None:
    from nexura_runtime.tools import tool

    ran: list[str] = []

    @tool(name="pay_invoice", description="Pay")
    def pay(invoice: str) -> str:
        ran.append(invoice)
        return invoice

    @tool(name="delete_invoice", description="Delete")
    def delete(invoice: str) -> str:
        ran.append(f"deleted {invoice}")
        return invoice

    gated_pay = gate.wrap_tool(pay)
    gated_delete = gate.wrap_tool(delete)
    with pytest.raises(ApprovalRequiredError) as paused:
        gated_pay.run(invoice="INV-1")
    approval_id = paused.value.approval_id
    _approve(manager, approval_id, workspace="default")

    with pytest.raises(ApprovalIdentityMismatchError):
        gated_delete.run(invoice="INV-1", approval_id=approval_id)
    assert ran == []
    assert gated_pay.run(invoice="INV-1", approval_id=approval_id) == "INV-1"
    assert ran == ["INV-1"]
