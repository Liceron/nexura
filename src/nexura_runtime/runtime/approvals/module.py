# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""B00/B01 Module wrapper for Approval Policy Manager."""

from __future__ import annotations

import time
from typing import Any

from nexura_agents.contracts.base import BaseModule
from nexura_agents.contracts.context import ExecutionContext
from nexura_agents.contracts.manifest import ModuleManifest
from nexura_agents.contracts.result import ModuleResult

from nexura_runtime.runtime.approvals.errors import ApprovalError, WrongWorkspaceError
from nexura_runtime.runtime.approvals.manager import ApprovalPolicyManager
from nexura_runtime.runtime.approvals.models import (
    ApprovalDecisionRecord,
    ApprovalRequest,
)

#: ``ExecutionContext.attributes`` key holding the role of the principal running the node.
#: ``decide`` takes the deciding role from here, never from the module inputs.
PRINCIPAL_ROLE_ATTRIBUTE = "principal_role"


class ApprovalModule(BaseModule):
    """Module wrapper exposing approval management through standard B00/B01 contracts.

    Every operation runs in the caller's ``context.workspace_id``; a ``workspace_id`` input
    naming another workspace is refused. ``create`` binds the request to ``context.run_id``
    and ``context.node_id`` (a ``run_id`` input is ignored). ``decide`` records
    ``context.principal_id`` as the decider and takes the role from
    ``context.attributes["principal_role"]``; it is refused when the context carries no role
    or when ``decided_by_principal_id`` / ``decided_by_role`` inputs disagree with the context.
    """

    manifest = ModuleManifest(
        schema_version=1,
        module_id="nexura.runtime.approvals",
        version="1.0.0",
        kind="approval_manager",
        internal=True,
        title="Approval Policy Manager",
        description=(
            "Persistent approval request and decision manager with role and digest validation."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "operation": {"type": "string", "enum": ["create", "decide", "get"]},
                "workspace_id": {"type": "string"},
                "approval_id": {"type": "string"},
                "run_id": {"type": "string"},
                "action_name": {"type": "string"},
                "required_role": {"type": "string"},
                "action_payload": {"type": "object"},
                "decision": {"type": "string", "enum": ["approved", "rejected"]},
                "decided_by_principal_id": {"type": "string"},
                "decided_by_role": {"type": "string"},
                "reason": {"type": "string"},
                "expected_payload_digest": {"type": "string"},
                "expires_in_seconds": {"type": "number"},
            },
            "required": ["operation"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {
                "approval_id": {"type": "string"},
                "approval_status": {"type": "string"},
                "workspace_id": {"type": "string"},
            },
            "required": ["approval_id", "approval_status", "workspace_id"],
            "additionalProperties": False,
        },
        effect_policy="idempotent_write",
    )

    def __init__(self, manager: ApprovalPolicyManager | None = None) -> None:
        self.manager = manager or ApprovalPolicyManager(":memory:")

    async def invoke(
        self,
        context: ExecutionContext,
        inputs: dict[str, Any],
        config: dict[str, Any] | None = None,
    ) -> ModuleResult:
        operation = inputs["operation"]
        ws_id = context.workspace_id
        requested_ws = inputs.get("workspace_id")
        if requested_ws and requested_ws != ws_id:
            raise WrongWorkspaceError(
                f"ApprovalModule runs in workspace '{ws_id}' and cannot act on workspace "
                f"'{requested_ws}'.",
                request_workspace=str(requested_ws),
                decision_workspace=ws_id,
                approval_id=str(inputs.get("approval_id", "")),
            )

        if operation == "create":
            import uuid

            appr_id = inputs.get("approval_id") or f"appr_{uuid.uuid4().hex[:12]}"
            ttl = float(inputs.get("expires_in_seconds", 3600.0))
            req = ApprovalRequest(
                approval_id=appr_id,
                workspace_id=ws_id,
                # Bound to the caller's run: a ``run_id`` input cannot choose another run.
                run_id=context.run_id,
                node_id=context.node_id,
                attempt_id=context.attempt_id,
                action_kind="workflow_node",
                action_name=str(inputs.get("action_name", "")),
                required_role=inputs.get("required_role", "admin"),
                action_payload=inputs.get("action_payload", {}),
                expires_at=time.time() + ttl,
            )
            created = self.manager.create_request(req)
            return ModuleResult(
                outputs={
                    "approval_id": created.approval_id,
                    "approval_status": created.status,
                    "workspace_id": created.workspace_id,
                }
            )

        elif operation == "decide":
            appr_id = inputs["approval_id"]
            role = context.attributes.get(PRINCIPAL_ROLE_ATTRIBUTE)
            if not isinstance(role, str) or not role:
                raise ApprovalError(
                    "ApprovalModule 'decide' needs the deciding principal's role in "
                    f"context.attributes['{PRINCIPAL_ROLE_ATTRIBUTE}']; module inputs cannot "
                    "supply it.",
                    details={"approval_id": appr_id, "reason": "no_principal_role"},
                )
            for key, actual in (
                ("decided_by_principal_id", context.principal_id),
                ("decided_by_role", role),
            ):
                claimed = inputs.get(key)
                if claimed is not None and claimed != actual:
                    raise ApprovalError(
                        f"ApprovalModule 'decide' input {key}={claimed!r} does not match the "
                        f"execution context ({actual!r}).",
                        details={"approval_id": appr_id, "reason": "decider_mismatch"},
                    )
            decision_rec = ApprovalDecisionRecord(
                approval_id=appr_id,
                workspace_id=ws_id,
                decided_by_principal_id=context.principal_id,
                decided_by_role=role,
                decision=inputs["decision"],
                reason=inputs.get("reason", ""),
                expected_payload_digest=inputs.get("expected_payload_digest", ""),
            )
            decided = self.manager.submit_decision(ws_id, appr_id, decision_rec)
            return ModuleResult(
                outputs={
                    "approval_id": decided.approval_id,
                    "approval_status": decided.status,
                    "workspace_id": decided.workspace_id,
                }
            )

        elif operation == "get":
            appr_id = inputs["approval_id"]
            fetched = self.manager.get_request(ws_id, appr_id)
            if not fetched:
                raise ValueError(f"Approval request '{appr_id}' not found.")
            return ModuleResult(
                outputs={
                    "approval_id": fetched.approval_id,
                    "approval_status": fetched.status,
                    "workspace_id": fetched.workspace_id,
                }
            )

        raise ValueError(f"Unsupported operation '{operation}'.")
