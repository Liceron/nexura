# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Approval gate for enforcing human-in-the-loop policies across tools, nodes, and agents."""

from __future__ import annotations

import functools
import inspect
import time
import uuid
from collections.abc import Callable, Coroutine
from typing import Any

from nexura_agents.contracts.canonical import canonical_digest
from nexura_agents.contracts.context import ExecutionContext

from nexura_runtime.runtime.approvals.errors import (
    ApprovalClaimConflictError,
    ApprovalError,
    ApprovalExpiredError,
    ApprovalIdentityMismatchError,
    ApprovalIdentityMissingError,
    ApprovalRequiredError,
    MutatedArgumentsError,
)
from nexura_runtime.runtime.approvals.manager import ApprovalPolicyManager
from nexura_runtime.runtime.approvals.models import (
    DISPATCH_IDENTITY_FIELDS,
    ApprovalRequest,
    dispatch_identity,
    dispatch_identity_digest,
)


def _direct_context(tool: Any, wrap_identity: str) -> ExecutionContext:
    """Context for a wrapped tool called without one.

    Stable for one wrapped callable, so an approval can be resumed on it, and unique to it:
    the run id carries the token minted by :meth:`ApprovalGate.wrap_tool`, so two different
    tools with the same name never share approvals through this fallback.
    """
    name = getattr(tool, "name", "tool")
    return ExecutionContext(
        workspace_id="default",
        principal_id="unknown",
        run_id=f"direct_tool:{name}:{wrap_identity}",
        node_id="tool_node",
    )


def _bound_payload(fn: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
    """Every argument of one call, keyed by parameter name: what the approval covers.

    Positional arguments are bound to ``fn``'s signature, so ``run(1)`` and ``run(amount=1)``
    produce the same payload and a positional argument can never reach the tool without
    being shown to the reviewer and covered by the digest. Extra keyword arguments
    (``**kwargs``) are merged in by name; extra positional ones (``*args``) are kept as a
    list under ``"*<name>"`` (a key no keyword argument can take), so ``run(5)`` and
    ``run(args=[5])`` never share a payload. A call that cannot be bound, or whose payload
    cannot be canonicalised, is refused before any request or claim is made.
    """
    try:
        signature: inspect.Signature | None = inspect.signature(fn)
    except (TypeError, ValueError):
        signature = None
    if signature is None:
        if args:
            raise ApprovalError(
                "Positional arguments cannot be bound to this tool's signature for approval; "
                "call it with keyword arguments. Zero side effects dispatched.",
                details={"reason": "unbindable_arguments"},
            )
        payload = dict(kwargs)
    else:
        try:
            bound = signature.bind_partial(*args, **kwargs)
        except TypeError as exc:
            raise ApprovalError(
                f"Tool arguments do not match its signature: {exc}. Zero side effects dispatched.",
                details={"reason": "unbindable_arguments"},
            ) from exc
        payload = {}
        extra: dict[str, Any] = {}
        for name, value in bound.arguments.items():
            kind = signature.parameters[name].kind
            if kind is inspect.Parameter.VAR_KEYWORD:
                extra.update(value)
            elif kind is inspect.Parameter.VAR_POSITIONAL:
                payload[f"*{name}"] = list(value)
            else:
                payload[name] = value
        for name, value in extra.items():
            if name in payload or name.startswith("*"):
                raise ApprovalError(
                    f"Tool argument '{name}' is ambiguous for approval. Zero side effects "
                    "dispatched.",
                    details={"reason": "unbindable_arguments"},
                )
            payload[name] = value
    try:
        canonical_digest(payload)
    except (TypeError, ValueError) as exc:
        raise ApprovalError(
            f"Tool arguments cannot be canonicalised for approval: {exc}. Zero side effects "
            "dispatched.",
            details={"reason": "uncanonical_arguments"},
        ) from exc
    return payload


class ApprovalGate:
    """Runtime enforcement gate ensuring actions cannot execute without valid, claimed approval.

    An approval authorises one exact action: its dispatch identity is the workspace, run,
    node, spec digest, action kind, action name and payload digest recorded when the request
    was created. Presenting the approval id for anything else raises
    :class:`ApprovalIdentityMismatchError`; a request past ``expires_at`` raises
    :class:`ApprovalExpiredError` even if it was approved in time; a request stored before
    identity binding existed raises :class:`ApprovalIdentityMissingError`. The claim is
    atomic and single use.
    """

    def __init__(
        self,
        manager: ApprovalPolicyManager,
        *,
        default_expiry_seconds: float = 3600.0,
        policy_version: str = "1.0.0",
    ) -> None:
        self.manager = manager
        self.default_expiry_seconds = default_expiry_seconds
        self.policy_version = policy_version

    def check_or_request_approval(
        self,
        context: ExecutionContext,
        action_name: str,
        payload: dict[str, Any],
        *,
        required_role: str = "admin",
        action_kind: str = "tool_call",
        approval_id: str | None = None,
        spec_digest: str = "",
        expiry_seconds: float | None = None,
        request_id: str | None = None,
    ) -> ApprovalRequest:
        """Validate an existing approval or pause execution by creating a new pending approval.

        ``request_id`` is a stable id for a caller that re-runs the same action after the
        approval (for example the agent coordinator, which derives it from the run, node and
        tool call): if a request with that id exists it is checked and claimed as if passed
        as ``approval_id``, otherwise the new pending request is stored under that id.

        A payload that cannot be canonicalised (a non-string mapping key, a non-finite
        float, an unsupported type) is refused with :class:`ApprovalError` before any
        request is created or claimed.
        """
        try:
            current_digest = canonical_digest(payload)
        except (TypeError, ValueError) as exc:
            raise ApprovalError(
                f"Payload for '{action_name}' cannot be canonicalised for approval: {exc}. "
                "Zero side effects dispatched.",
                details={"reason": "uncanonical_arguments"},
            ) from exc
        if (
            not approval_id
            and request_id
            and self.manager.get_request(context.workspace_id, request_id) is not None
        ):
            approval_id = request_id
        identity = dispatch_identity(
            workspace_id=context.workspace_id,
            run_id=context.run_id,
            node_id=context.node_id,
            spec_digest=spec_digest,
            action_kind=action_kind,
            action_name=action_name,
            payload_digest=current_digest,
        )
        identity_digest = dispatch_identity_digest(identity)

        # 1. If an approval_id is provided, verify and atomically claim it
        if approval_id:
            req = self.manager.get_request(context.workspace_id, approval_id)
            if not req:
                raise ApprovalError(
                    f"Approval request '{approval_id}' not found in workspace "
                    f"'{context.workspace_id}'."
                )

            # Check if arguments were mutated since approval request
            if req.payload_digest != current_digest:
                raise MutatedArgumentsError(
                    f"Action arguments for '{action_name}' have been modified since "
                    f"approval request. Expected digest '{req.payload_digest}', "
                    f"got '{current_digest}'.",
                    expected_digest=req.payload_digest,
                    actual_digest=current_digest,
                    approval_id=approval_id,
                )

            # The approval authorises one exact action: refuse any other run, node, spec,
            # action kind or action name, and refuse rows that never recorded an identity.
            if not req.identity_digest:
                raise ApprovalIdentityMissingError(
                    f"Approval request '{approval_id}' has no recorded dispatch identity "
                    "(stored before identity binding); it cannot be dispatched. Request a "
                    "fresh approval. Zero side effects dispatched.",
                    approval_id=approval_id,
                )
            if req.identity_digest != identity_digest:
                stored = req.dispatch_identity()
                mismatched = [f for f in DISPATCH_IDENTITY_FIELDS if stored[f] != identity[f]]
                raise ApprovalIdentityMismatchError(
                    f"Approval request '{approval_id}' authorises a different action "
                    f"(mismatched: {', '.join(mismatched) or 'identity digest'}). "
                    "Zero side effects dispatched.",
                    approval_id=approval_id,
                    mismatched_fields=mismatched or ["identity_digest"],
                )

            if req.status == "rejected":
                raise ApprovalError(
                    f"Approval request '{approval_id}' was rejected. Zero side effects dispatched.",
                    details={"approval_id": approval_id, "decision": "rejected"},
                )

            if req.status == "expired":
                raise ApprovalExpiredError(
                    f"Approval request '{approval_id}' has expired. Zero side effects dispatched.",
                    approval_id=approval_id,
                    expired_at=req.expires_at,
                )

            if req.status == "pending":
                raise ApprovalRequiredError(
                    f"Approval request '{approval_id}' is still pending approval by "
                    f"'{req.required_role}'.",
                    approval_id=approval_id,
                    action_name=action_name,
                    required_role=req.required_role,
                    payload_digest=current_digest,
                )

            if req.status == "approved":
                # Atomically claim for dispatch (at-most-once execution). The claim itself
                # re-checks status, prior claim, expiry and identity in one UPDATE.
                worker_claim_id = (
                    f"{context.run_id}:{context.node_id or 'tool'}:{context.attempt_id}"
                )
                outcome = self.manager.claim_for_dispatch(
                    context.workspace_id,
                    approval_id,
                    worker_claim_id,
                    identity_digest=identity_digest,
                )
                if outcome == "claimed":
                    return req
                if outcome == "expired":
                    raise ApprovalExpiredError(
                        f"Approval request '{approval_id}' expired at {req.expires_at} "
                        "before it was dispatched. Zero side effects dispatched.",
                        approval_id=approval_id,
                        expired_at=req.expires_at,
                    )
                if outcome == "identity_missing":
                    raise ApprovalIdentityMissingError(
                        f"Approval request '{approval_id}' has no recorded dispatch identity; "
                        "it cannot be dispatched.",
                        approval_id=approval_id,
                    )
                if outcome == "identity_mismatch":
                    raise ApprovalIdentityMismatchError(
                        f"Approval request '{approval_id}' authorises a different action.",
                        approval_id=approval_id,
                        mismatched_fields=["identity_digest"],
                    )
                raise ApprovalClaimConflictError(
                    f"Approval request '{approval_id}' was already claimed or "
                    f"dispatched by another worker.",
                    approval_id=approval_id,
                    claimed_by=req.claimed_by or "unknown",
                )

        # 2. No approval_id provided: create a pending request and pause execution
        new_id = request_id or f"appr_{uuid.uuid4().hex[:12]}"
        ttl = expiry_seconds if expiry_seconds is not None else self.default_expiry_seconds
        expires_at = time.time() + ttl

        req = ApprovalRequest(
            approval_id=new_id,
            workspace_id=context.workspace_id,
            run_id=context.run_id,
            node_id=context.node_id,
            attempt_id=context.attempt_id,
            action_kind=action_kind,
            required_role=required_role,
            action_payload=payload,
            payload_digest=current_digest,
            spec_digest=spec_digest,
            action_name=action_name,
            policy_version=self.policy_version,
            status="pending",
            expires_at=expires_at,
        )
        self.manager.create_request(req)

        raise ApprovalRequiredError(
            f"Action '{action_name}' requires approval from '{required_role}'.",
            approval_id=new_id,
            action_name=action_name,
            required_role=required_role,
            payload_digest=current_digest,
        )

    def wrap_tool(
        self,
        tool: Any,
        *,
        required_role: str = "admin",
        expiry_seconds: float | None = None,
    ) -> Any:
        """Wrap a tool so that invoking it enforces the approval gate unconditionally.

        The approved payload is every argument of the call: positional arguments are bound
        to the tool's signature by name (see :func:`_bound_payload`), so the reviewer sees
        them and the digest covers them. ``context`` and ``approval_id`` keyword arguments
        are consumed by the gate and not passed to the tool.
        """
        wrap_identity = uuid.uuid4().hex[:12]
        # 1. Wrap execute if present
        if hasattr(tool, "execute") and callable(tool.execute):
            orig_execute = tool.execute

            async def gated_execute(arguments: dict[str, Any]) -> str:
                context = arguments.get("context")
                approval_id = arguments.get("approval_id")
                call_args = {
                    k: v for k, v in arguments.items() if k not in ("context", "approval_id")
                }
                if not isinstance(context, ExecutionContext):
                    context = _direct_context(tool, wrap_identity)
                self.check_or_request_approval(
                    context,
                    action_name=getattr(tool, "name", "tool"),
                    payload=call_args,
                    required_role=required_role,
                    action_kind="tool_call",
                    approval_id=approval_id,
                    expiry_seconds=expiry_seconds,
                )
                res = await orig_execute(call_args)
                return str(res)

            tool.execute = gated_execute

        # 2. Wrap run / _run_internal
        original_run = getattr(tool, "_run_internal", None) or getattr(tool, "run", None)
        if original_run is None:
            return tool

        if inspect.iscoroutinefunction(original_run):

            @functools.wraps(original_run)
            async def async_wrapped(*args: Any, **kwargs: Any) -> Any:
                context = kwargs.get("context")
                approval_id = kwargs.get("approval_id")
                call_kwargs = {
                    k: v for k, v in kwargs.items() if k not in ("context", "approval_id")
                }
                payload = _bound_payload(original_run, args, call_kwargs)
                if not isinstance(context, ExecutionContext):
                    context = _direct_context(tool, wrap_identity)

                self.check_or_request_approval(
                    context,
                    action_name=getattr(tool, "name", "tool"),
                    payload=payload,
                    required_role=required_role,
                    action_kind="tool_call",
                    approval_id=approval_id,
                    expiry_seconds=expiry_seconds,
                )
                return await original_run(*args, **call_kwargs)

            if hasattr(tool, "_run_internal"):
                tool._run_internal = async_wrapped
            else:
                tool.run = async_wrapped

        else:

            @functools.wraps(original_run)
            def sync_wrapped(*args: Any, **kwargs: Any) -> Any:
                context = kwargs.get("context")
                approval_id = kwargs.get("approval_id")
                call_kwargs = {
                    k: v for k, v in kwargs.items() if k not in ("context", "approval_id")
                }
                payload = _bound_payload(original_run, args, call_kwargs)
                if not isinstance(context, ExecutionContext):
                    context = _direct_context(tool, wrap_identity)

                self.check_or_request_approval(
                    context,
                    action_name=getattr(tool, "name", "tool"),
                    payload=payload,
                    required_role=required_role,
                    action_kind="tool_call",
                    approval_id=approval_id,
                    expiry_seconds=expiry_seconds,
                )
                return original_run(*args, **call_kwargs)

            if hasattr(tool, "_run_internal"):
                tool._run_internal = sync_wrapped
            else:
                tool.run = sync_wrapped

        return tool

    async def execute_gated_action(
        self,
        context: ExecutionContext,
        action_name: str,
        payload: dict[str, Any],
        handler: Callable[[], Coroutine[Any, Any, Any]],
        *,
        required_role: str = "admin",
        action_kind: str = "workflow_node",
        approval_id: str | None = None,
        spec_digest: str = "",
        expiry_seconds: float | None = None,
    ) -> Any:
        """Execute an action strictly guarded by the approval policy."""
        self.check_or_request_approval(
            context,
            action_name=action_name,
            payload=payload,
            required_role=required_role,
            action_kind=action_kind,
            approval_id=approval_id,
            spec_digest=spec_digest,
            expiry_seconds=expiry_seconds,
        )
        return await handler()
