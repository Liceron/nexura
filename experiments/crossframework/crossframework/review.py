"""Trusted local operator command using Nexura's Python approval API, outside MCP."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Literal

from nexura_runtime.runtime.approvals import ApprovalDecisionRecord, ApprovalPolicyManager

from .state import WORKSPACE, append_record


def decide(
    state_dir: Path,
    approval_id: str,
    reviewed_digest: str,
    decision: Literal["approved", "rejected"] = "approved",
) -> dict[str, str]:
    manager = ApprovalPolicyManager(state_dir / "approvals.sqlite3")
    request = manager.submit_decision(
        WORKSPACE,
        approval_id,
        ApprovalDecisionRecord(
            approval_id=approval_id,
            workspace_id=WORKSPACE,
            decided_by_principal_id="local-demo-operator",
            decided_by_role="refund-reviewer",
            decision=decision,
            expected_payload_digest=reviewed_digest,
        ),
    )
    append_record(
        state_dir / "lifecycle.jsonl",
        {
            "event": decision,
            "run_id": request.run_id,
            "approval_id": approval_id,
            "payload_digest": request.payload_digest,
            "policy_digest": request.spec_digest,
            "reviewer": "local-demo-operator",
        },
    )
    return {"approval_id": approval_id, "status": request.status}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--approval-id", required=True)
    parser.add_argument("--decision", choices=["approved", "rejected"])
    parser.add_argument("--expected-digest")
    args = parser.parse_args()
    if args.decision:
        if not args.expected_digest:
            parser.error("--decision requires --expected-digest from the reviewed action")
        print(
            json.dumps(
                decide(args.state_dir, args.approval_id, args.expected_digest, args.decision)
            )
        )
    else:
        manager = ApprovalPolicyManager(args.state_dir / "approvals.sqlite3")
        request = manager.get_request(WORKSPACE, args.approval_id)
        if request is None:
            parser.error("approval not found")
        print(request.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
