# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""P32c: a pending approval carries the exact canonical JSON its payload digest covers."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from nexura_runtime.workflow import (
    ModuleRegistry,
    PendingApproval,
    SQLiteWorkflowStore,
    WorkflowEngine,
    WorkflowNode,
    WorkflowSpec,
)


def _engine(tmp_path: Path) -> WorkflowEngine:
    reg = ModuleRegistry()

    def pay(state: dict[str, Any]) -> dict[str, Any]:
        return {"paid": True}

    reg.register("pay", pay)
    return WorkflowEngine(
        WorkflowSpec(
            name="pay",
            entry_node="pay",
            nodes={"pay": WorkflowNode(id="pay", handler="pay", requires_approval=True)},
        ),
        reg,
        store=SQLiteWorkflowStore(tmp_path / "pay.db"),
    )


async def test_canonical_payload_json_is_what_the_digest_covers(tmp_path: Path) -> None:
    paused = await _engine(tmp_path).run({"amount": 1_000_000, "to": "acct-<script>"})
    pending = paused.pending_approval
    assert pending is not None
    text = pending.canonical_payload_json
    assert hashlib.sha256(text.encode("utf-8")).hexdigest() == pending.payload_digest
    assert json.loads(text) == {"amount": 1_000_000, "to": "acct-<script>"}


async def test_canonical_payload_json_survives_storage_and_api_form(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    paused = await engine.run({"amount": 7})
    pending = paused.pending_approval
    assert pending is not None
    restored = PendingApproval.from_dict(json.loads(json.dumps(pending.to_dict())))
    assert restored.canonical_payload_json == pending.canonical_payload_json
    assert pending.to_dict()["canonical_payload_json"] == pending.canonical_payload_json


def test_rows_recorded_before_the_field_load_as_empty() -> None:
    legacy = {"approval_id": "a", "workflow_run_id": "r", "node_id": "n", "input_state": {}}
    assert PendingApproval.from_dict(legacy).canonical_payload_json == ""


async def test_edited_inputs_create_a_request_that_names_the_one_it_replaces(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    first = await engine.run({"amount": 10, "to": "a"})
    assert first.pending_approval is not None
    assert first.pending_approval.supersedes_approval_id is None
    edited = await engine.resume(
        first.workflow_run_id, first.pending_approval.approval_id, state_update={"amount": 999}
    )
    fresh = edited.pending_approval
    assert edited.status == "paused" and fresh is not None
    assert fresh.approval_id != first.pending_approval.approval_id
    assert fresh.supersedes_approval_id == first.pending_approval.approval_id
    assert fresh.regate_reason == "inputs_edited"
    assert fresh.previous_input_state == {"amount": 10, "to": "a"}
    assert json.loads(fresh.canonical_payload_json)["amount"] == 999
    # The provenance is stored, so a reviewer who never saw the first request sees it too.
    stored = PendingApproval.from_dict(json.loads(json.dumps(fresh.to_dict())))
    assert stored.supersedes_approval_id == first.pending_approval.approval_id
    assert stored.previous_input_state == {"amount": 10, "to": "a"}


async def test_unedited_resume_and_its_next_gate_carry_no_provenance(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    first = await engine.run({"amount": 10})
    assert first.pending_approval is not None
    done = await engine.resume(first.workflow_run_id, first.pending_approval.approval_id)
    assert done.status == "completed"
    again = await _engine(tmp_path / "second").run({"amount": 11})
    assert again.pending_approval is not None
    assert again.pending_approval.supersedes_approval_id is None
    assert again.pending_approval.regate_reason is None
