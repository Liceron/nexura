"""One fake refund tool, served by Nexura over stdio. No payment/provider calls."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import uuid
from pathlib import Path
from typing import Any

from nexura_agents import AgentMCPServer, RunContext
from nexura_agents.contracts.canonical import canonical_digest
from nexura_agents.contracts.context import ExecutionContext
from nexura_agents.tools import ToolInfo
from nexura_guardrails import CallbackAuditSink, GuardrailPolicy, Rule, When
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from nexura_runtime.runtime.approvals import (
    ApprovalClaimConflictError,
    ApprovalError,
    ApprovalExpiredError,
    ApprovalGate,
    ApprovalPolicyManager,
    ApprovalRequiredError,
)

from .journal import UNKNOWN, DispatchJournal
from .state import WORKSPACE, append_record
from .stdio import serve
from .wire import NotExecuted


class RefundInput(BaseModel):
    """USD whole dollars only; every business argument is covered by approval."""

    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    order_id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    amount: int = Field(gt=0)
    approval_id: str | None = Field(default=None, min_length=1, max_length=100)


def build_server(state_dir: Path, *, approval_ttl: float = 900) -> AgentMCPServer:
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    database = state_dir / "approvals.sqlite3"
    manager = ApprovalPolicyManager(database)
    gate = ApprovalGate(manager, default_expiry_seconds=approval_ttl)
    journal = DispatchJournal(database)
    policy = GuardrailPolicy(
        name="crossframework-refunds",
        rules=[
            Rule(
                "refund-ceiling",
                When(stage="tool_input", tools="refund", args={"$.amount": {"gt": 500}}),
                action="block",
            ),
            Rule(
                "refund-review",
                When(stage="tool_input", tools="refund", args={"$.amount": {"gt": 100}}),
                action="require_approval",
            ),
        ],
        audit=CallbackAuditSink(
            lambda record: append_record(state_dir / "policy.jsonl", record.to_dict())
        ),
        audit_allows=True,
        audit_required=True,
    )
    info = ToolInfo(name="refund", origin="python:crossframework.refund", effect="non_idempotent")

    async def execute(action: RefundInput) -> dict[str, Any]:
        payload = action.model_dump(exclude={"approval_id"})
        run_id = f"refund:{action.request_id}"
        ctx: RunContext[Any] = RunContext(
            deps=None,
            run_id=run_id,
            agent_name="external-mcp-client",
            tool_name="refund",
            tool_call_id=uuid.uuid4().hex,
            tool=info,
        )

        def audit(event: str, **fields: Any) -> None:
            append_record(
                state_dir / "lifecycle.jsonl",
                {
                    "event": event,
                    "run_id": run_id,
                    "tool_call_id": ctx.tool_call_id,
                    "policy_digest": policy.digest,
                    **fields,
                },
            )

        # Replays consult the journal before policy/audit I/O: a historical success must
        # never become a failure implying that money did not move. No new dispatch occurs.
        recorded = journal.reserve(action.request_id, canonical_digest(payload))
        if recorded is not None:
            if recorded["status"] == "request_id_conflict":
                # Still evaluate the ceiling, but keep the conflict status: its wording
                # depends on what the id already did, and blocked would say no money moved.
                outcome = await policy.check_call(ctx, "tool_input", payload, tool=info)
                if outcome.action in {"block", "fail"}:
                    return {**recorded, "rule": outcome.rule}
            return recorded

        def finish(status: str, **fields: Any) -> dict[str, Any]:
            result = {"status": status, "executed": False, **fields}
            journal.finish(action.request_id, result)
            return result

        approval_id = action.approval_id
        effect_started = False
        try:
            # The strict model is load-bearing for negative amounts and look-alike keys.
            outcome = await policy.check_call(ctx, "tool_input", payload, tool=info)
            if outcome.action in {"block", "fail"}:
                audit("blocked", rule=outcome.rule)
                return finish("blocked", rule=outcome.rule)

            if approval_id is not None or policy.call_needs_approval(ctx, info, payload):
                try:
                    request = gate.check_or_request_approval(
                        ExecutionContext(
                            workspace_id=WORKSPACE,
                            principal_id="external-client",
                            run_id=run_id,
                            node_id="refund",
                        ),
                        action_name="refund",
                        action_kind="tool_call",
                        payload=payload,
                        spec_digest=policy.digest,
                        required_role="refund-reviewer",
                        approval_id=approval_id,
                        request_id=f"refund-{action.request_id}",
                    )
                except ApprovalRequiredError as pending:
                    audit(
                        "pending_approval",
                        approval_id=pending.approval_id,
                        payload_digest=pending.payload_digest,
                    )
                    return finish(
                        "pending_approval",
                        approval_id=pending.approval_id,
                        payload_digest=pending.payload_digest,
                        retry="Repeat the exact arguments after operator review.",
                    )
                except ApprovalClaimConflictError:
                    # A legacy/pre-journal claim or interruption is not proof of refusal.
                    # Leave dispatching in place; subsequent retries must not send again.
                    return dict(UNKNOWN)
                except ApprovalError as refused:
                    status = (
                        "expired"
                        if isinstance(refused, ApprovalExpiredError)
                        else "rejected"
                        if refused.details.get("decision") == "rejected"
                        else "approval_refused"
                    )
                    audit(status, approval_id=approval_id, reason=type(refused).__name__)
                    return finish(status, reason=type(refused).__name__)
                approval_id = request.approval_id
                audit(
                    "approval_claimed",
                    approval_id=approval_id,
                    payload_digest=request.payload_digest,
                )

            audit("dispatch", approval_id=approval_id)
            effect_started = True
            receipt = {"refund_id": uuid.uuid4().hex, "currency": "USD", **payload}
            append_record(state_dir / "fake-refunds.jsonl", receipt)
        except Exception as error:
            if effect_started:
                # Even a failed append/fsync may have applied the fake effect. No replay.
                return dict(UNKNOWN)
            return finish("error", reason=type(error).__name__)

        result = {"status": "completed", "executed": True, "receipt": receipt}
        try:
            journal.finish(action.request_id, result)
        except Exception:
            # We have a receipt, but future retries may only see dispatching. Be honest now.
            return {**result, "audit_warning": "Receipt saved; dispatch journal update failed."}
        try:
            audit("completed", approval_id=approval_id, refund_id=receipt["refund_id"])
        except Exception:
            result["audit_warning"] = "Refund completed; completion audit write failed."
            with contextlib.suppress(Exception):
                journal.finish(action.request_id, result)
        return result

    async def refund(arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            action = RefundInput.model_validate(arguments)
        except ValidationError as invalid:
            raise NotExecuted({"status": "invalid", "reason": "invalid_arguments"}) from invalid
        try:
            result = await execute(action)
        except Exception:
            # A store outage can hide an earlier receipt. Never claim that no money
            # moved, or permit another dispatch, just because the journal is unreadable.
            result = dict(UNKNOWN)
        if not result["executed"]:
            raise NotExecuted(result)
        return result

    server = AgentMCPServer("crossframework-governance")
    server.session.register_tool(
        "refund",
        "FAKE USD refund. Over $100 needs operator approval; over $500 is blocked. "
        "Reuse request_id and exact arguments to retry. Non-execution is an MCP error.",
        RefundInput.model_json_schema(),
        refund,
    )
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--approval-ttl", type=float, default=900)
    args = parser.parse_args()
    asyncio.run(serve(build_server(args.state_dir, approval_ttl=args.approval_ttl)))


if __name__ == "__main__":
    main()
