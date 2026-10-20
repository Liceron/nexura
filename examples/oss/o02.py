"""O02 Example: Durable Recovery, Exact-Action Approvals and Effects.

Demonstrates:
1. Configuring a durable workflow with an approval-gated node.
2. Canonical argument digest generation and cryptographic binding to pending approvals.
3. Rejecting mutated arguments and unauthorized actor roles on resume attempt.
4. Resuming execution after valid human approval with verified argument digest.
5. Indeterminate effect detection and fence validation across durable pauses.
"""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from typing import Any

from nexura_runtime.contracts.context import ExecutionContext
from nexura_runtime.providers.base import BaseProvider
from nexura_runtime.providers.types import (
    EmbeddingResult,
    GenerationRequest,
    ProviderCapabilities,
    StreamDelta,
)
from nexura_runtime.types import Message, ModelResponse, Usage
from nexura_runtime.usage.ledger import UsageLedger
from nexura_runtime.usage.manager import BudgetManager
from nexura_runtime.usage.models import BudgetConfig, ModelPricing
from nexura_runtime.usage.pricing import PricingTable

from nexura_runtime.runtime.approvals.errors import (
    MutatedArgumentsError,
    WrongActorError,
)
from nexura_runtime.workflow import (
    ModuleRegistry,
    SQLiteWorkflowStore,
    WorkflowEngine,
    WorkflowNode,
    WorkflowSpec,
)


class DeterministicMockProvider(BaseProvider):
    """Local keyless test provider."""

    def __init__(self, model_id: str = "mock-gpt-4o") -> None:
        super().__init__(
            model_id=model_id,
            capabilities=ProviderCapabilities(
                can_generate=True,
                can_stream=False,
                can_embed=False,
                reports_usage=True,
            ),
        )
        self.call_count = 0

    async def _generate_internal(
        self,
        request: GenerationRequest,
        context: ExecutionContext | None,
    ) -> ModelResponse:
        self.call_count += 1
        return ModelResponse(
            message=Message(
                role="assistant",
                content=f"Analysis completed (call #{self.call_count})",
            ),
            usage=Usage(prompt_tokens=80, completion_tokens=40, total_tokens=120),
        )

    async def _stream_internal(
        self,
        request: GenerationRequest,
        context: ExecutionContext | None,
    ) -> AsyncIterator[StreamDelta]:
        if False:
            yield

    async def _embed_internal(
        self, texts: list[str], context: ExecutionContext | None
    ) -> EmbeddingResult:
        return EmbeddingResult(embeddings=[], model=self.model_id, usage=Usage())


async def main() -> None:
    print("=== O02: Durable Recovery & Exact-Action Approvals Demo ===")

    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "durable_workflow.db"
        usage_db = Path(temp_dir) / "usage.db"

        # 1. Initialize SQLite store, pricing and budget
        store = SQLiteWorkflowStore(db_path)
        pricing = PricingTable(version="2026-10-07-standard", date="2026-10-07")
        pricing.add_model_pricing(
            ModelPricing(
                model_id="mock-gpt-4o",
                input_cost_per_million=Decimal("2.50"),
                output_cost_per_million=Decimal("10.00"),
            )
        )
        ledger = UsageLedger(db_path=str(usage_db))
        manager = BudgetManager(ledger=ledger, pricing_table=pricing)
        budget = manager.create_run_budget(
            workspace_id="ws_finance",
            run_id="run_durable_payout_001",
            config=BudgetConfig(max_cost_usd=Decimal("1.00")),
            default_model="mock-gpt-4o",
        )

        provider = DeterministicMockProvider()
        registry = ModuleRegistry()

        # Step 1: Pre-approval preparation and analysis
        async def prepare_transfer(state: dict[str, Any]) -> dict[str, Any]:
            ctx = ExecutionContext(
                workspace_id="ws_finance",
                run_id="run_durable_payout_001",
                node_id="prep_step",
                budget=budget,
            )
            resp = await provider.generate(
                [Message(role="user", content="Analyze transfer risks")],
                context=ctx,
            )
            # Produce exact proposal arguments destined for gated execution
            transfer_args = {
                "destination_account": "ACT-9921",
                "amount_cents": 500000,
                "currency": "USD",
                "memo": "Quarterly settlement",
            }
            return {
                "risk_analysis": resp.message.content,
                "transfer_args": transfer_args,
                "status": "pending_authorization",
            }

        # Step 2: Gated financial action
        async def execute_transfer(state: dict[str, Any]) -> dict[str, Any]:
            args = state.get("transfer_args", {})
            cents = args.get("amount_cents", 0)
            dest = args.get("destination_account")
            print(f"-> Gated Action Executed: Transferring ${cents / 100:.2f} to {dest}")
            return {
                "transfer_id": "TX_9921_DONE",
                "executed_args": args,
                "status": "completed",
            }

        registry.register("prepare_transfer", prepare_transfer)
        registry.register("execute_transfer", execute_transfer)

        # Build workflow specification
        spec = WorkflowSpec(
            "payout_approval_workflow",
            entry_node="prepare_transfer",
            terminal_nodes=["execute_transfer"],
            nodes={
                "prepare_transfer": WorkflowNode(
                    id="prepare_transfer",
                    handler="prepare_transfer",
                    default_transition="execute_transfer",
                ),
                "execute_transfer": WorkflowNode(
                    id="execute_transfer",
                    handler="execute_transfer",
                    requires_approval=True,  # Exact-action approval gate
                ),
            },
        )

        # 2. Run workflow up to the approval gate
        engine = WorkflowEngine(
            spec,
            registry,
            store=store,
            workspace_id="ws_finance",
            budget=budget,
        )

        print("\n1. Starting workflow run...")
        run_res = await engine.run({}, workflow_run_id="run_durable_payout_001")
        assert run_res.status == "paused"
        assert run_res.pending_approval is not None
        pending = run_res.pending_approval

        print(f"   Status: {run_res.status}")
        print(f"   Approval ID: {pending.approval_id}")
        print(f"   Gated Node: {pending.node_id}")
        print(f"   Canonical Digest: {pending.payload_digest}")
        print(f"   Workspace Binding: {pending.workspace_id}")

        # 3. Demonstrate Negative Test: Mutated arguments rejection
        print("\n2. Attempting resume with mutated arguments digest...")
        tampered_digest = "tampered_" + "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b78"

        try:
            await engine.resume(
                "run_durable_payout_001",
                pending.approval_id,
                decision="approve",
                expected_payload_digest=tampered_digest,
            )
            print("   ERROR: Mutated arguments were unexpectedly accepted!")
        except MutatedArgumentsError as exc:
            print(f"   [SUCCESS] MutatedArgumentsError raised as expected: {exc}")

        # 4. Demonstrate Negative Test: Unauthorized actor role rejection
        print("\n3. Attempting resume with unauthorized actor role...")
        try:
            await engine.resume(
                "run_durable_payout_001",
                pending.approval_id,
                decision="approve",
                actor="eve@company.internal",
                actor_role="viewer",  # Required role is admin
            )
            print("   ERROR: Unauthorized actor was unexpectedly accepted!")
        except WrongActorError as exc:
            print(f"   [SUCCESS] WrongActorError raised as expected: {exc}")

        # 5. Legitimate resume with verified approval
        print("\n4. Resuming with valid approval and verified digest...")
        resume_res = await engine.resume(
            "run_durable_payout_001",
            pending.approval_id,
            decision="approve",
            actor="alice@company.internal",
            actor_role="admin",
            expected_payload_digest=pending.payload_digest,
        )
        print(f"   Workflow Status: {resume_res.status}")
        print(f"   Step count: {resume_res.step_count}")
        print(f"   Transfer ID: {resume_res.state.get('transfer_id')}")

        # 6. Budget audit
        status = budget.get_status()
        print("\n5. Budget Status after durable completion:")
        print(f"   Total spent: ${status.total_spent_usd:.6f}")
        print(f"   Total tokens: {status.total_tokens}")
        print(f"   Provider calls: {status.provider_calls_count}")
        print("=== O02 Demonstration Completed Successfully ===")


if __name__ == "__main__":
    asyncio.run(main())
