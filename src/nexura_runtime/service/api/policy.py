# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Server-owned approval requirements cannot be disabled by a workflow document."""

from fastapi import HTTPException, Request

from nexura_runtime.workflow import WorkflowSpec

# Egress policy is operator-owned: per-node config supplied through the API must not widen it.
DESTINATION_POLICY_KEYS = frozenset({"allow_loopback", "allow_private_ranges", "allowed_hosts"})


def enforce_approval_policy(request: Request, spec: WorkflowSpec) -> None:
    check_approval_policy(request.app.state.approval_required_handlers, spec)


def check_approval_policy(required: frozenset[str], spec: WorkflowSpec) -> None:
    """Reject gated handlers without an approval gate, including in sub-workflows and branches."""
    for node in spec.nodes.values():
        overrides = sorted(DESTINATION_POLICY_KEYS & set(node.config or {}))
        if overrides:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "DESTINATION_POLICY_OVERRIDE_FORBIDDEN",
                    "message": (
                        f"Node '{node.id}' config may not set {', '.join(overrides)}; "
                        "network destination policy is controlled by the server operator."
                    ),
                },
            )
        gated = [node.handler] if node.handler in required else []
        if node.module_id and node.module_id in required:
            gated.append(node.module_id)
        gated += [name for name in node.parallel if name in required]
        if gated and not node.requires_approval:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "APPROVAL_POLICY_REQUIRED",
                    "message": f"Node '{node.id}' must retain the server-required approval gate.",
                },
            )
        if node.subgraph is not None:
            check_approval_policy(required, node.subgraph)
