"""Executable keyless example for O06 — Inspectable conservative context selection.

Demonstrates:
1. Typed ContextBundle and versioned decision records preserving authoritative state.
2. Invariant protection: instructions, negations, financial amounts, corrections, and approvals.
3. Explicit JSON projection and bounded table/log selection with summary markers.
4. Access verification and source deletion: unauthorized or deleted items omitted.
5. Capacity overflow failure: actionable ContextOverflowError when mandatory items exceed budget.
6. Opt-in default reduction and short-context zero-overhead bypass.
7. Inspectable ContextDecisionRecord audit trail with credential scrubbing.
"""

from __future__ import annotations

import copy

from nexura_runtime.context import (
    ContextItem,
    ContextOverflowError,
    ContextPolicy,
    ContextSelector,
    bound_logs,
    compact_table,
    project_json,
)
from nexura_runtime.types import Message


def stage_1_authoritative_state_immutability() -> None:
    print("\n--- Stage 1: Authoritative State Immutability & Opt-In Defaults ---")
    original_messages = [
        Message(role="system", content="You are a strict compliance auditor."),
        Message(role="user", content="Audit transaction txn_888 for $5,000 to acc_999."),
        Message(
            role="assistant", content="Analyzing transaction ledger and compliance policies..."
        ),
    ]
    snapshot = copy.deepcopy(original_messages)

    # Default policy: reductions are opt-in (enabled=False)
    default_policy = ContextPolicy()
    selector = ContextSelector(policy=default_policy)
    bundle = selector.select_messages(original_messages)

    assert bundle.decision_record.status == "bypassed_disabled"
    assert bundle.is_reduced is False
    assert original_messages == snapshot
    print(f"Default policy status: {bundle.decision_record.status}")
    print(f"Authoritative history preserved exactly: {len(original_messages)} messages unchanged")


def stage_2_protected_invariants() -> None:
    print("\n--- Stage 2: Protected Invariant Preservation ---")
    policy = ContextPolicy(
        enabled=True,
        max_context_tokens=1000,
        short_context_threshold_tokens=10,
    )
    selector = ContextSelector(policy=policy)

    items = [
        ContextItem(
            item_id="sys_1",
            item_type="system_prompt",
            role="system",
            content="Base safety policy: require dual approvals for wire transfers.",
        ),
        ContextItem(
            item_id="neg_1",
            role="user",
            content="Under no circumstance approve without signed contract. Do not proceed.",
        ),
        ContextItem(
            item_id="corr_1",
            role="user",
            content="Wait, scratch that, change beneficiary to acc_777 and amount to $25,000.",
        ),
        ContextItem(
            item_id="approval_1",
            item_type="approval",
            content={
                "approval_id": "app_wire_42",
                "action": "execute_wire",
                "state_update": {"recipient": "acc_777", "amount": 25000},
            },
        ),
    ]

    bundle = selector.select(items)
    print(f"Decision record status: {bundle.decision_record.status}")
    print(f"Total projected tokens: {bundle.total_tokens} (all protected items preserved)")
    for rec in bundle.decision_record.items:
        print(f"  • Item [{rec.item_id}]: decision={rec.decision}, reasons={rec.protected_reasons}")
        assert rec.decision == "included"
        assert rec.is_protected is True


def stage_3_projections_and_bounded_compaction() -> None:
    print("\n--- Stage 3: Explicit Projections & Bounded Compaction ---")

    # 1. JSON projection preserving protected keys
    raw_json = {
        "user_id": "usr_alpha_123",  # Protected ID
        "amount_usd": 1500.0,  # Protected financial amount
        "internal_cache_key": "k_999",
        "debug_trace": "0xDEADBEEF",
        "description": "Standard invoice payout",
    }
    projected = project_json(raw_json, projection_spec=["description"])
    print(f"Projected JSON keys: {list(projected.keys())}")
    assert "description" in projected
    assert "user_id" in projected  # Kept because ID is protected
    assert "amount_usd" in projected  # Kept because amount is protected
    assert "debug_trace" not in projected

    # 2. Bounded table selection
    table_lines = [f"| row_{i} | val_{i} |" for i in range(1, 21)]
    table_text = "| col_a | col_b |\n|---|---|\n" + "\n".join(table_lines)
    compacted, kept, omitted = compact_table(table_text, max_rows=5)
    print(f"Table compaction: {kept} rows kept, {omitted} rows omitted")
    assert "[Table compacted: 5 of 20 rows included; 15 omitted by policy]" in compacted

    # 3. Bounded log selection
    raw_logs = "\n".join(f"2026-10-07 10:{i:02d}:00 INFO event {i}" for i in range(25))
    bounded, kept_l, omitted_l = bound_logs(raw_logs, max_lines=5)
    print(f"Log bounding: {kept_l} lines kept, {omitted_l} lines omitted")
    assert "[Logs bounded: latest 5 lines preserved; 20 earlier lines omitted by policy]" in bounded


def stage_4_access_rechecks_and_source_deletion() -> None:
    print("\n--- Stage 4: Access Verification & Source Deletion Rechecks ---")
    items = [
        ContextItem(
            item_id="doc_valid",
            workspace_id="ws_finance",
            source_provenance="doc_101",
            content="Active finance policy handbook.",
        ),
        ContextItem(
            item_id="doc_deleted",
            workspace_id="ws_finance",
            source_provenance="doc_102",
            is_deleted=True,
            content="Obsolete deleted policy draft.",
        ),
        ContextItem(
            item_id="doc_foreign",
            workspace_id="ws_hr",
            source_provenance="doc_103",
            content="HR confidential compensation figures.",
        ),
    ]

    policy = ContextPolicy(enabled=True, max_context_tokens=1000, short_context_threshold_tokens=5)
    selector = ContextSelector(policy=policy)
    bundle = selector.select(items, current_workspace_id="ws_finance")

    for rec in bundle.decision_record.items:
        print(f"  • Source [{rec.item_id}]: {rec.decision} (reason: {rec.reason})")

    rec_map = {r.item_id: r for r in bundle.decision_record.items}
    assert rec_map["doc_valid"].decision == "included"
    assert rec_map["doc_deleted"].decision == "omitted"
    assert rec_map["doc_deleted"].reason == "source_deleted"
    assert rec_map["doc_foreign"].decision == "omitted"
    assert rec_map["doc_foreign"].reason == "unauthorized_workspace"


def stage_5_capacity_overflow_boundary() -> None:
    print("\n--- Stage 5: Capacity Overflow Boundary Enforcement ---")
    mandatory_items = [
        ContextItem(
            item_id="mandatory_instruction",
            item_type="system_prompt",
            role="system",
            content="Mandatory compliance directive requiring continuous dual approval. " * 30,
        ),
        ContextItem(
            item_id="mandatory_negation",
            role="user",
            content="Do not execute without signed authorization from compliance officer! " * 30,
        ),
    ]

    policy = ContextPolicy(
        enabled=True,
        max_context_tokens=80,  # Small limit to force overflow on protected items
        short_context_threshold_tokens=10,
        overflow_strategy="fail",
    )
    selector = ContextSelector(policy=policy)

    try:
        selector.select(mandatory_items)
        raise RuntimeError("Expected ContextOverflowError was not raised!")
    except ContextOverflowError as exc:
        print("Explicit ContextOverflowError caught successfully:")
        print(f"  • Required tokens: {exc.required_tokens}")
        print(f"  • Capacity limit: {exc.max_context_tokens}")
        print(f"  • Overflow items: {exc.overflow_items}")
        print("  • Mandatory evidence/instructions were NOT silently discarded.")


def stage_6_credential_scrubbing_in_audit_records() -> None:
    print("\n--- Stage 6: Credential Scrubbing in Audit Decision Previews ---")
    secret_item = ContextItem(
        item_id="confidential_call",
        content=(
            "Request headers: Authorization: Bearer sk-live-secret-key-supersecret99; "
            "api_key=topsecret;"
        ),
    )
    policy = ContextPolicy(enabled=True, max_context_tokens=1000, short_context_threshold_tokens=5)
    selector = ContextSelector(policy=policy)
    bundle = selector.select([secret_item])

    preview = bundle.decision_record.items[0].content_preview
    print(f"Sanitized audit preview: {preview}")
    assert preview is not None
    assert "sk-live-secret-key-supersecret99" not in preview
    assert "topsecret" not in preview
    assert "[REDACTED]" in preview


def main() -> None:
    print("=" * 70)
    print("O06: Inspectable Conservative Context Selection Demo")
    print("=" * 70)

    stage_1_authoritative_state_immutability()
    stage_2_protected_invariants()
    stage_3_projections_and_bounded_compaction()
    stage_4_access_rechecks_and_source_deletion()
    stage_5_capacity_overflow_boundary()
    stage_6_credential_scrubbing_in_audit_records()

    print("\n" + "=" * 70)
    print("O06 Verification Complete: All 6 stages passed cleanly.")
    print("=" * 70)


if __name__ == "__main__":
    main()
