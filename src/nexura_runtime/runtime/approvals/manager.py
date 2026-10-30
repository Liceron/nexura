# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Approval policy manager providing atomic approval claims, decisions, and checks."""

from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Literal

from nexura_agents.contracts.canonical import canonical_digest, canonical_json

from nexura_runtime.runtime.approvals.errors import (
    ApprovalAlreadyDecidedError,
    ApprovalError,
    ApprovalExpiredError,
    MutatedArgumentsError,
    WrongActorError,
    WrongWorkspaceError,
)
from nexura_runtime.runtime.approvals.models import (
    ApprovalDecisionRecord,
    ApprovalRequest,
    ApprovalStatus,
    dispatch_identity_digest,
)
from nexura_runtime.storage.migrations import run_sqlite_migrations

#: Outcome of :meth:`ApprovalPolicyManager.claim_for_dispatch`. Only ``"claimed"`` permits
#: the caller to dispatch the action.
ClaimOutcome = Literal[
    "claimed",
    "not_found",
    "not_approved",
    "already_claimed",
    "expired",
    "identity_missing",
    "identity_mismatch",
]


class ApprovalPolicyManager:
    """Manages persistent approval requests, role-based decisions, and atomic dispatch claims."""

    def __init__(self, db_path_or_conn: str | Path | sqlite3.Connection = ":memory:") -> None:
        # Serialises use of a shared connection (``":memory:"`` or one the caller passed).
        self._lock = threading.RLock()
        if isinstance(db_path_or_conn, (str, Path)):
            self.db_path = str(db_path_or_conn)
            self._shared_conn: sqlite3.Connection | None = None
            if self.db_path == ":memory:" or self.db_path.startswith("file::memory:"):
                # Each sqlite3.connect(":memory:") is a new, empty database: keep one
                # connection for the manager's lifetime so its requests persist.
                self._shared_conn = self._open(self.db_path, check_same_thread=False)
                run_sqlite_migrations(self._shared_conn)
            else:
                conn = self._get_connection()
                try:
                    run_sqlite_migrations(conn)
                finally:
                    conn.close()
        else:
            self.db_path = ":memory:"
            self._shared_conn = db_path_or_conn
            run_sqlite_migrations(self._shared_conn)

    @staticmethod
    def _open(path: str, *, check_same_thread: bool = True) -> sqlite3.Connection:
        conn = sqlite3.connect(
            path,
            timeout=30.0,
            autocommit=True,
            check_same_thread=check_same_thread,
            uri=path.startswith("file:"),
        )
        conn.row_factory = sqlite3.Row
        return conn

    def _get_connection(self) -> sqlite3.Connection:
        if self._shared_conn is not None:
            return self._shared_conn
        return self._open(self.db_path)

    @contextlib.contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """A connection for one operation: the shared one under the lock, else a fresh one."""
        if self._shared_conn is not None:
            with self._lock:
                yield self._shared_conn
            return
        conn = self._open(self.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def create_request(self, request: ApprovalRequest) -> ApprovalRequest:
        """Create a new approval request in durable storage.

        A new request is always ``pending``, unclaimed and undecided: a request carrying
        another status, a claim or a decision is refused, so no caller can store an
        already-approved row. Decisions go through :meth:`submit_decision`.
        """
        if request.status != "pending" or request.decision is not None:
            raise ApprovalError(
                f"Approval request '{request.approval_id}' must be created as 'pending' with no "
                f"decision (got status '{request.status}'); decide it with submit_decision()."
            )
        if request.claimed_by is not None or request.claimed_at is not None:
            raise ApprovalError(
                f"Approval request '{request.approval_id}' cannot be created already claimed."
            )
        with self._connection() as conn:
            return self._insert_request(conn, request)

    def _insert_request(
        self, conn: sqlite3.Connection, request: ApprovalRequest
    ) -> ApprovalRequest:
        try:
            req_copy = request.model_copy()
            if not req_copy.payload_digest:
                req_copy.payload_digest = canonical_digest(req_copy.action_payload)
            # Bind the request to the exact action it authorises; recomputed here so a
            # caller cannot store an identity digest that does not match the fields.
            req_copy.identity_digest = dispatch_identity_digest(req_copy.dispatch_identity())

            now = time.time()
            req_copy.created_at = req_copy.created_at or now
            req_copy.updated_at = now

            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO approval_requests (
                    workspace_id, approval_id, run_id, node_id, attempt_id,
                    action_kind, required_role, action_payload_json, payload_digest,
                    spec_digest, policy_version, status, claimed_by, claimed_at,
                    expires_at, decision_json, created_at, updated_at,
                    action_name, identity_digest
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    req_copy.workspace_id,
                    req_copy.approval_id,
                    req_copy.run_id,
                    req_copy.node_id,
                    req_copy.attempt_id,
                    req_copy.action_kind,
                    req_copy.required_role,
                    canonical_json(req_copy.action_payload),
                    req_copy.payload_digest,
                    req_copy.spec_digest,
                    req_copy.policy_version,
                    req_copy.status,
                    req_copy.claimed_by,
                    req_copy.claimed_at,
                    req_copy.expires_at,
                    (
                        canonical_json(req_copy.decision.model_dump(mode="json"))
                        if req_copy.decision
                        else None
                    ),
                    req_copy.created_at,
                    req_copy.updated_at,
                    req_copy.action_name,
                    req_copy.identity_digest,
                ),
            )
            conn.execute("COMMIT")
            return req_copy
        except Exception:
            with contextlib.suppress(Exception):
                conn.execute("ROLLBACK")
            raise

    def get_request(self, workspace_id: str, approval_id: str) -> ApprovalRequest | None:
        """Retrieve an approval request by workspace and approval ID, updating expiry if needed."""
        with self._connection() as conn:
            return self._get_request(conn, workspace_id, approval_id)

    def _get_request(
        self, conn: sqlite3.Connection, workspace_id: str, approval_id: str
    ) -> ApprovalRequest | None:
        try:
            cur = conn.execute(
                """
                SELECT * FROM approval_requests
                WHERE workspace_id = ? AND approval_id = ?
                """,
                (workspace_id, approval_id),
            )
            row = cur.fetchone()
            if not row:
                return None

            req = self._row_to_request(row)
            now = time.time()
            if req.status == "pending" and now > req.expires_at:
                # Mark expired
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """
                    UPDATE approval_requests
                    SET status = 'expired', updated_at = ?
                    WHERE workspace_id = ? AND approval_id = ? AND status = 'pending'
                    """,
                    (now, workspace_id, approval_id),
                )
                conn.execute("COMMIT")
                req.status = "expired"
                req.updated_at = now

            return req
        except Exception:
            with contextlib.suppress(Exception):
                conn.execute("ROLLBACK")
            raise

    def find_request_across_workspaces(self, approval_id: str) -> ApprovalRequest | None:
        """Search for an approval request across all workspaces."""
        with self._connection() as conn:
            cur = conn.execute(
                "SELECT * FROM approval_requests WHERE approval_id = ?",
                (approval_id,),
            )
            row = cur.fetchone()
            return self._row_to_request(row) if row else None

    def submit_decision(
        self,
        workspace_id: str,
        approval_id: str,
        decision: ApprovalDecisionRecord,
    ) -> ApprovalRequest:
        """Atomically record an approval decision, validating workspace, role, and digest."""
        with self._connection() as conn:
            return self._submit_decision(conn, workspace_id, approval_id, decision)

    def _submit_decision(
        self,
        conn: sqlite3.Connection,
        workspace_id: str,
        approval_id: str,
        decision: ApprovalDecisionRecord,
    ) -> ApprovalRequest:
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                """
                SELECT * FROM approval_requests
                WHERE workspace_id = ? AND approval_id = ?
                """,
                (workspace_id, approval_id),
            )
            row = cur.fetchone()

            if not row:
                # Check cross-workspace existence
                cross_cur = conn.execute(
                    "SELECT workspace_id FROM approval_requests WHERE approval_id = ?",
                    (approval_id,),
                )
                cross_row = cross_cur.fetchone()
                if cross_row:
                    raise WrongWorkspaceError(
                        f"Approval request '{approval_id}' belongs to workspace "
                        f"'{cross_row['workspace_id']}', not '{workspace_id}'.",
                        request_workspace=cross_row["workspace_id"],
                        decision_workspace=workspace_id,
                        approval_id=approval_id,
                    )
                raise ApprovalError(
                    f"Approval request '{approval_id}' not found in workspace '{workspace_id}'."
                )

            req = self._row_to_request(row)
            now = time.time()

            # 1. Check if already decided
            if req.status in ("approved", "rejected"):
                raise ApprovalAlreadyDecidedError(
                    f"Approval request '{approval_id}' was already decided as '{req.status}'.",
                    approval_id=approval_id,
                    current_status=req.status,
                )

            # 2. Check expiry
            if now > req.expires_at or req.status == "expired":
                conn.execute(
                    """
                    UPDATE approval_requests
                    SET status = 'expired', updated_at = ?
                    WHERE workspace_id = ? AND approval_id = ?
                    """,
                    (now, workspace_id, approval_id),
                )
                conn.execute("COMMIT")
                raise ApprovalExpiredError(
                    f"Approval request '{approval_id}' expired at {req.expires_at} "
                    f"(current time {now}).",
                    approval_id=approval_id,
                    expired_at=req.expires_at,
                )

            # 3. Check role authorization
            if decision.decided_by_role != req.required_role:
                raise WrongActorError(
                    f"Principal '{decision.decided_by_principal_id}' has role "
                    f"'{decision.decided_by_role}', but required role is '{req.required_role}'.",
                    deciding_role=decision.decided_by_role,
                    required_role=req.required_role,
                    principal_id=decision.decided_by_principal_id,
                )

            # 4. Check argument digest match
            if (
                decision.expected_payload_digest
                and decision.expected_payload_digest != req.payload_digest
            ):
                raise MutatedArgumentsError(
                    f"Decision payload digest '{decision.expected_payload_digest}' does not match "
                    f"the original requested payload digest '{req.payload_digest}'.",
                    expected_digest=decision.expected_payload_digest,
                    actual_digest=req.payload_digest,
                    approval_id=approval_id,
                )

            # 5. Commit decision atomically
            new_status: ApprovalStatus = (
                "approved" if decision.decision == "approved" else "rejected"
            )
            decision_json = canonical_json(decision.model_dump(mode="json"))

            conn.execute(
                """
                UPDATE approval_requests
                SET status = ?, decision_json = ?, updated_at = ?
                WHERE workspace_id = ? AND approval_id = ? AND status = 'pending'
                """,
                (new_status, decision_json, now, workspace_id, approval_id),
            )
            conn.execute("COMMIT")

            req.status = new_status
            req.decision = decision
            req.updated_at = now
            return req
        except Exception:
            with contextlib.suppress(Exception):
                conn.execute("ROLLBACK")
            raise

    def claim_approval_for_dispatch(
        self,
        workspace_id: str,
        approval_id: str,
        worker_id: str,
        *,
        identity_digest: str,
        now: float | None = None,
    ) -> bool:
        """Atomically claim an approved request for one-time dispatch of one exact action.

        Returns True only when the request is approved, unclaimed, not past ``expires_at`` and
        its stored dispatch identity equals ``identity_digest``. Use :meth:`claim_for_dispatch`
        to learn why a claim was refused. ``now`` can only move the claim time forward (see
        :meth:`claim_for_dispatch`).
        """
        outcome = self.claim_for_dispatch(
            workspace_id, approval_id, worker_id, identity_digest=identity_digest, now=now
        )
        return outcome == "claimed"

    def claim_for_dispatch(
        self,
        workspace_id: str,
        approval_id: str,
        worker_id: str,
        *,
        identity_digest: str,
        now: float | None = None,
    ) -> ClaimOutcome:
        """Claim an approved request for dispatch and report the outcome.

        The claim is one ``UPDATE`` whose ``WHERE`` clause requires status ``approved``, no
        prior claim, ``expires_at`` later than the current time and the stored identity
        digest equal to ``identity_digest``; the classification read before it runs in the
        same ``BEGIN IMMEDIATE`` transaction. A row with no stored identity (stored before
        identity binding) is never claimed.

        The current time is the manager's own clock (``time.time()``). ``now`` exists only so
        tests can simulate a later time: the claim uses ``max(now, time.time())``, so a
        caller-supplied time can never backdate a claim to before ``expires_at``.
        """
        with self._connection() as conn:
            return self._claim(conn, workspace_id, approval_id, worker_id, identity_digest, now)

    def _claim(
        self,
        conn: sqlite3.Connection,
        workspace_id: str,
        approval_id: str,
        worker_id: str,
        identity_digest: str,
        now: float | None,
    ) -> ClaimOutcome:
        clock = time.time()
        claim_time = clock if now is None else max(float(now), clock)
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                """
                SELECT status, claimed_by, expires_at, identity_digest FROM approval_requests
                WHERE workspace_id = ? AND approval_id = ?
                """,
                (workspace_id, approval_id),
            )
            row = cur.fetchone()
            outcome: ClaimOutcome
            if not row:
                outcome = "not_found"
            elif not row["identity_digest"]:
                outcome = "identity_missing"
            elif row["identity_digest"] != identity_digest:
                outcome = "identity_mismatch"
            elif row["status"] != "approved":
                outcome = "not_approved"
            elif row["claimed_by"] is not None:
                outcome = "already_claimed"
            elif not row["expires_at"] > claim_time:
                outcome = "expired"
            else:
                update = conn.execute(
                    """
                    UPDATE approval_requests
                    SET claimed_by = ?, claimed_at = ?, updated_at = ?
                    WHERE workspace_id = ? AND approval_id = ?
                      AND status = 'approved' AND claimed_by IS NULL
                      AND expires_at > ?
                      AND identity_digest != '' AND identity_digest = ?
                    """,
                    (
                        worker_id,
                        claim_time,
                        claim_time,
                        workspace_id,
                        approval_id,
                        claim_time,
                        identity_digest,
                    ),
                )
                outcome = "claimed" if update.rowcount == 1 else "already_claimed"
            conn.execute("COMMIT")
            return outcome
        except Exception:
            with contextlib.suppress(Exception):
                conn.execute("ROLLBACK")
            raise

    def _row_to_request(self, row: Any) -> ApprovalRequest:
        decision = None
        if row["decision_json"]:
            decision_dict = json.loads(row["decision_json"])
            decision = ApprovalDecisionRecord(**decision_dict)

        payload_dict = json.loads(row["action_payload_json"]) if row["action_payload_json"] else {}

        return ApprovalRequest(
            workspace_id=row["workspace_id"],
            approval_id=row["approval_id"],
            run_id=row["run_id"],
            node_id=row["node_id"],
            attempt_id=row["attempt_id"],
            action_kind=row["action_kind"],
            required_role=row["required_role"],
            action_payload=payload_dict,
            payload_digest=row["payload_digest"],
            spec_digest=row["spec_digest"],
            action_name=row["action_name"] or "",
            identity_digest=row["identity_digest"] or "",
            policy_version=row["policy_version"],
            status=row["status"],
            claimed_by=row["claimed_by"],
            claimed_at=row["claimed_at"],
            expires_at=row["expires_at"],
            decision=decision,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
