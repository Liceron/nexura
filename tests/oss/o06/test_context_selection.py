"""Tests for O06 — Inspectable conservative context selection and overflow boundaries."""

from __future__ import annotations

import copy
from decimal import Decimal
from typing import Any

import pytest
from nexura_runtime.context import (
    ContextBundle,
    ContextDecisionRecord,
    ContextItem,
    ContextOverflowError,
    ContextPolicy,
    ContextSelector,
    MalformedStructuredDataError,
    bound_logs,
    compact_table,
    inspect_item_invariants,
    project_json,
)
from nexura_runtime.providers.fake import StreamingFakeProvider
from nexura_runtime.providers.types import (
    GenerationRequest,
    ProviderMessage,
    ToolCallPart,
    ToolResultPart,
)
from nexura_runtime.types import Message
from nexura_runtime.usage.ledger import UsageLedger
from nexura_runtime.usage.manager import BudgetManager
from nexura_runtime.usage.models import BudgetConfig


class TestImmutableAuthoritativeStateAndBundles:
    """Verifies authoritative execution history immutability and ContextBundle typing."""

    def test_authoritative_history_immutable_under_projection(self) -> None:
        original_messages = [
            Message(role="system", content="You are a strict compliance auditor."),
            Message(role="user", content="Audit transfer txn_1001 for $5,000 to acc_9999."),
            Message(
                role="assistant",
                content="Preparing audit report with extensive logs...",
            ),
        ]
        # Keep deep copy for comparison
        messages_snapshot = copy.deepcopy(original_messages)

        policy = ContextPolicy(
            enabled=True,
            max_context_tokens=100,
            short_context_threshold_tokens=10,  # force active selection
        )
        selector = ContextSelector(policy=policy)
        bundle = selector.select_messages(original_messages)

        assert isinstance(bundle, ContextBundle)
        assert isinstance(bundle.decision_record, ContextDecisionRecord)

        # Authoritative original messages list and objects MUST NOT be mutated
        assert len(original_messages) == len(messages_snapshot)
        for orig, snap in zip(original_messages, messages_snapshot, strict=False):
            assert orig.content == snap.content
            assert orig.role == snap.role

    def test_default_policy_is_opt_in_and_bypassed(self) -> None:
        messages = [
            Message(role="user", content="Hello there!"),
        ]
        policy = ContextPolicy()  # default enabled=False
        selector = ContextSelector(policy=policy)
        bundle = selector.select_messages(messages)

        assert bundle.decision_record.status == "bypassed_disabled"
        assert bundle.is_reduced is False
        assert len(bundle.messages) == 1
        assert bundle.decision_record.items[0].reason == "policy_disabled"


class TestProtectedInvariants:
    """Verifies invariant protection for instructions, negations, IDs, amounts and approvals."""

    def test_system_instruction_protection(self) -> None:
        policy = ContextPolicy(enabled=True)
        item = ContextItem(
            item_id="sys_1",
            item_type="system_prompt",
            role="system",
            content="Always enforce dual-key authorization.",
        )
        is_prot, reasons = inspect_item_invariants(item, policy)
        assert is_prot is True
        assert "protected_system_instruction" in reasons

    def test_negations_and_corrections_protection(self) -> None:
        policy = ContextPolicy(enabled=True)

        # Explicit negation directive
        neg_item = ContextItem(
            item_id="neg_1",
            role="user",
            content="Under no circumstance approve this. Do not transfer funds.",
        )
        is_prot, reasons = inspect_item_invariants(neg_item, policy)
        assert is_prot is True
        assert "protected_negation" in reasons

        # Changed user intent / correction
        corr_item = ContextItem(
            item_id="corr_1",
            role="user",
            content="Wait, scratch that, cancel earlier request and disregard previous amount.",
        )
        is_prot, reasons = inspect_item_invariants(corr_item, policy)
        assert is_prot is True
        assert "protected_correction" in reasons

    def test_identifiers_and_financial_amounts_protection(self) -> None:
        policy = ContextPolicy(enabled=True)

        # UUID and financial amounts
        item = ContextItem(
            item_id="fin_1",
            role="user",
            content="Pay $12,500.50 for user_9876 (123e4567-e89b-12d3-a456-426614174000).",
        )
        is_prot, reasons = inspect_item_invariants(item, policy)
        assert is_prot is True
        assert "protected_identifier" in reasons
        assert "protected_financial_amount" in reasons

    def test_approval_arguments_protection(self) -> None:
        policy = ContextPolicy(enabled=True)
        approval_item = ContextItem(
            item_id="app_1",
            item_type="approval",
            content={
                "approval_id": "app_555",
                "action": "execute_wire_transfer",
                "state_update": {"recipient": "acc_333", "amount": 50000},
            },
        )
        is_prot, reasons = inspect_item_invariants(approval_item, policy)
        assert is_prot is True
        assert "protected_approval_args" in reasons

    def test_unreferenced_fields_not_automatically_dropped(self) -> None:
        # Without an explicit projection spec, all structured fields remain intact
        raw_payload = {
            "account_id": "acc_111",
            "unreferenced_custom_tag": "metadata_xyz",
            "beneficiary_notes": "Urgent vendor batch",
        }
        projected = project_json(raw_payload, projection_spec=None)
        assert projected == raw_payload
        assert "unreferenced_custom_tag" in projected


class TestCapacityOverflowAndActionableFailure:
    """Verifies that capacity overflow on protected invariants fails with ContextOverflowError."""

    def test_protected_material_overflow_fails_explicitly(self) -> None:
        # Create items with protected invariants whose tokens exceed max_context_tokens
        items = [
            ContextItem(
                item_id="sys_prompt",
                item_type="system_prompt",
                role="system",
                content="Mandatory base instruction " * 50,  # ~150 tokens
            ),
            ContextItem(
                item_id="mandatory_negation",
                role="user",
                content="Do not proceed! " * 50,  # ~150 tokens
            ),
        ]

        policy = ContextPolicy(
            enabled=True,
            max_context_tokens=100,  # Capacity too small for protected items (~300 tokens)
            short_context_threshold_tokens=20,
            overflow_strategy="fail",
        )
        selector = ContextSelector(policy=policy)

        with pytest.raises(ContextOverflowError) as exc_info:
            selector.select(items)

        err = exc_info.value
        assert err.required_tokens > policy.max_context_tokens
        assert err.max_context_tokens == 100
        assert "sys_prompt" in err.overflow_items
        assert "mandatory_negation" in err.overflow_items
        assert "Mandatory instructions and evidence cannot be silently discarded" in str(err)

    def test_compact_tables_overflow_strategy(self) -> None:
        # With compact_tables, table compaction is attempted first
        large_table = [{"id": f"row_{i}", "val": f"data_{i}"} for i in range(100)]
        items = [
            ContextItem(
                item_id="table_1",
                item_type="table",
                content=large_table,
            ),
        ]

        # max_table_rows=5 reduces table size significantly
        policy = ContextPolicy(
            enabled=True,
            max_context_tokens=200,
            short_context_threshold_tokens=10,
            overflow_strategy="compact_tables",
            max_table_rows=5,
        )
        selector = ContextSelector(policy=policy)
        bundle = selector.select(items)

        assert bundle.decision_record.status == "applied"
        assert bundle.is_reduced is True


class TestExplicitProjectionsAndBoundedSelection:
    """Verifies explicit JSON projections, bounded tables, and log windowing."""

    def test_json_projection_preserves_protected_keys(self) -> None:
        raw_data = {
            "internal_debug_trace": "0xDEADBEEF",
            "scratchpad": "temporary notes",
            "user_id": "usr_9999",  # Protected identifier
            "amount_usd": 250.75,  # Protected amount
            "allowed_field": "summary text",
        }
        # Projection spec only requests allowed_field
        projected = project_json(
            raw_data,
            projection_spec=["allowed_field"],
            protected_keys={"user_id", "amount_usd"},
        )

        assert "allowed_field" in projected
        assert "user_id" in projected  # Kept because it is a protected key
        assert "amount_usd" in projected  # Kept because it is a protected key
        assert "internal_debug_trace" not in projected
        assert "scratchpad" not in projected

    def test_malformed_json_fallback_verbatim(self) -> None:
        malformed = '{"user_id": "usr_1", "broken: json, '
        # Should not crash; returns verbatim
        result = project_json(malformed, projection_spec=["user_id"], allow_malformed=True)
        assert result == malformed

        # Strict mode raises MalformedStructuredDataError
        with pytest.raises(MalformedStructuredDataError):
            project_json(malformed, projection_spec=["user_id"], allow_malformed=False)

    def test_table_compaction_with_summary_marker(self) -> None:
        markdown_table = "| col1 | col2 |\n|---|---|\n" + "\n".join(
            f"| val_{i} | num_{i} |" for i in range(20)
        )
        compacted, kept, omitted = compact_table(markdown_table, max_rows=5)

        assert kept == 5
        assert omitted == 15
        assert "[Table compacted: 5 of 20 rows included; 15 omitted by policy]" in compacted
        assert "| val_0 | num_0 |" in compacted
        assert "| val_4 | num_4 |" in compacted
        assert "| val_19 | num_19 |" not in compacted

    def test_log_bounding_with_summary_marker(self) -> None:
        logs = "\n".join(f"2026-10-07 12:00:{i:02d} INFO step {i}" for i in range(50))
        bounded, kept, omitted = bound_logs(logs, max_lines=10)

        assert kept == 10
        assert omitted == 40
        assert (
            "[Logs bounded: latest 10 lines preserved; 40 earlier lines omitted by policy]"
            in bounded
        )
        assert "step 49" in bounded
        assert "step 0" not in bounded


class TestAccessAuthorizationAndSourceProvenance:
    """Verifies that revoked/deleted sources and cross-workspace items are omitted."""

    def test_revoked_and_deleted_sources_omitted(self) -> None:
        items = [
            ContextItem(
                item_id="chunk_valid",
                item_type="retrieval_chunk",
                source_provenance="doc_1",
                content="Valid policy terms.",
            ),
            ContextItem(
                item_id="chunk_deleted",
                item_type="retrieval_chunk",
                source_provenance="doc_2",
                is_deleted=True,
                content="Deleted sensitive policy terms.",
            ),
            ContextItem(
                item_id="chunk_revoked",
                item_type="retrieval_chunk",
                source_provenance="doc_3",
                is_revoked=True,
                content="Revoked compliance terms.",
            ),
        ]

        policy = ContextPolicy(
            enabled=True, max_context_tokens=1000, short_context_threshold_tokens=5
        )
        selector = ContextSelector(policy=policy)
        bundle = selector.select(items)

        item_map = {rec.item_id: rec for rec in bundle.decision_record.items}
        assert item_map["chunk_valid"].decision == "included"
        assert item_map["chunk_deleted"].decision == "omitted"
        assert item_map["chunk_deleted"].reason == "source_deleted"
        assert item_map["chunk_revoked"].decision == "omitted"
        assert item_map["chunk_revoked"].reason == "source_revoked"

    def test_workspace_isolation_omits_foreign_items(self) -> None:
        items = [
            ContextItem(
                item_id="ws_current",
                workspace_id="ws_finance",
                content="Finance workspace document.",
            ),
            ContextItem(
                item_id="ws_foreign",
                workspace_id="ws_hr",
                content="HR confidential payroll data.",
            ),
        ]

        policy = ContextPolicy(
            enabled=True, max_context_tokens=1000, short_context_threshold_tokens=5
        )
        selector = ContextSelector(policy=policy)
        bundle = selector.select(items, current_workspace_id="ws_finance")

        item_map = {rec.item_id: rec for rec in bundle.decision_record.items}
        assert item_map["ws_current"].decision == "included"
        assert item_map["ws_foreign"].decision == "omitted"
        assert item_map["ws_foreign"].reason == "unauthorized_workspace"


class TestShortContextBypassAndToolAssociations:
    """Verifies that short contexts bypass selection, and tool pairing is kept."""

    def test_short_context_bypasses_selection(self) -> None:
        short_messages = [
            Message(role="user", content="Ping"),
        ]
        policy = ContextPolicy(
            enabled=True,
            short_context_threshold_tokens=500,  # ~2 tokens input is well below threshold
        )
        selector = ContextSelector(policy=policy)
        bundle = selector.select_messages(short_messages)

        assert bundle.decision_record.status == "bypassed_short_context"
        assert bundle.is_reduced is False
        assert bundle.decision_record.items[0].reason == "bypassed_short_context"

    def test_tool_association_preservation(self) -> None:
        # If a tool result has protected content, its corresponding tool call is kept
        call_part = ToolCallPart(
            id="call_99", name="query_ledger", arguments={"account": "acc_123"}
        )
        result_part = ToolResultPart(
            tool_call_id="call_99",
            name="query_ledger",
            content="Account balance: $1,000,000 (protected financial invariant)",
        )

        msg_assistant = ProviderMessage(role="assistant", parts=[call_part])
        msg_tool = ProviderMessage(role="tool", parts=[result_part])

        policy = ContextPolicy(
            enabled=True,
            max_context_tokens=500,
            short_context_threshold_tokens=10,
        )
        selector = ContextSelector(policy=policy)
        bundle = selector.select_messages([msg_assistant, msg_tool])

        included_ids = [
            it.tool_call_id for it in bundle.decision_record.items if it.decision == "included"
        ]
        assert "call_99" in included_ids


class TestProviderIntegrationAndSpendingInvariance:
    """Verifies provider integration, spending invariance, and credential scrubbing."""

    @pytest.mark.asyncio
    async def test_provider_dispatch_attaches_context_decision(self) -> None:
        provider = StreamingFakeProvider(
            model_id="fake-streaming-v1",
            canned_responses=["Acknowledged."],
        )
        req = GenerationRequest(
            messages=[
                Message(role="system", content="Compliance auditor."),
                Message(role="user", content="Transfer $500 to acc_123. Do not alter."),
            ],
            context_policy=ContextPolicy(
                enabled=True,
                max_context_tokens=1000,
                short_context_threshold_tokens=5,
            ),
        )

        resp = await provider.generate(req)
        assert resp.message.content == "Acknowledged."
        assert req.context_decision is not None
        assert req.context_decision.status == "applied"
        assert req.context_bundle is not None

    @pytest.mark.asyncio
    async def test_spending_policy_and_budget_limits_not_altered(self, tmp_path: Any) -> None:
        # Context selection must not bypass or alter workspace budget limits
        db_path = str(tmp_path / "budget_test.db")
        ledger = UsageLedger(db_path=db_path)
        mgr = BudgetManager(ledger)
        from nexura_runtime.usage.models import ModelPricing

        mgr.pricing_table.add_model_pricing(
            ModelPricing(
                model_id="fake-streaming-v1",
                input_cost_per_million=Decimal("1.0"),
                output_cost_per_million=Decimal("2.0"),
            )
        )

        # Strict cost limit of $0.0001
        budget = mgr.create_run_budget(
            workspace_id="ws_strict",
            run_id="run_strict",
            config=BudgetConfig(max_cost_usd=Decimal("0.0001"), default_model="fake-streaming-v1"),
        )

        provider = StreamingFakeProvider(
            model_id="fake-streaming-v1",
            canned_responses=["OK"],
        )
        req = GenerationRequest(
            messages=[
                Message(role="user", content="Transfer funds under strict compliance rules."),
            ],
            # Even if context policy allows 5000 tokens, workspace budget cannot be bypassed
            context_policy=ContextPolicy(enabled=True, max_context_tokens=5000),
        )

        from nexura_runtime.contracts.context import ExecutionContext

        ctx = ExecutionContext(
            workspace_id="ws_strict", run_id="run_strict", node_id="node_1", budget=budget
        )

        # Pre-dispatch budget admission rejects the call because estimated tokens > 100
        from nexura_runtime.usage.errors import BudgetExceededError

        with pytest.raises(BudgetExceededError):
            await provider.generate(req, context=ctx)

    def test_credential_scrubbing_in_decision_records(self) -> None:
        # Sensitive credentials in content must be redacted in content_preview
        secret_item = ContextItem(
            item_id="sec_1",
            content="Authorization: Bearer sk-live-secret-key-12345; password=supersecret",
        )
        policy = ContextPolicy(
            enabled=True, max_context_tokens=1000, short_context_threshold_tokens=5
        )
        selector = ContextSelector(policy=policy)
        bundle = selector.select([secret_item])

        rec = bundle.decision_record.items[0]
        assert rec.content_preview is not None
        assert "sk-live-secret-key-12345" not in rec.content_preview
        assert "supersecret" not in rec.content_preview
        assert "[REDACTED]" in rec.content_preview
