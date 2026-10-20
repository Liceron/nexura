"""Executable keyless example for O09 — the order-support release scenario.

One SQLite file holds workflow checkpoints, the usage ledger and the refund stub's
effect table. The scenario:

1. Fetches order and policy evidence (one policy is deleted, one belongs to another
   workspace) and selects context; required evidence must survive selection.
2. Proposes a refund through a budgeted provider call (reserve before dispatch).
3. Pauses for exact-action approval, then "restarts" by reopening every store.
4. Resumes, executes the local refund stub exactly once and inspects usage.
5. Backs up and restores the paused state, and exports the workflow.

Variants exercise budget exhaustion and a lost approval (duplicate resume).
The provider is an explicitly labelled local fake; no network, keys or real refunds.
"""

from __future__ import annotations

import asyncio
import json
import socket
import sqlite3
import tempfile
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from nexura_runtime.context import ContextItem, ContextPolicy, ContextSelector
from nexura_runtime.contracts.context import ExecutionContext
from nexura_runtime.errors import (
    ApprovalAlreadyClaimedError,
    WorkflowExecutionError,
    WorkflowTerminalError,
)
from nexura_runtime.providers.base import BaseProvider
from nexura_runtime.providers.types import (
    EmbeddingResult,
    GenerationRequest,
    ProviderCapabilities,
    StreamDelta,
)
from nexura_runtime.types import Message, ModelResponse, Usage
from nexura_runtime.usage.ledger import UsageLedger
from nexura_runtime.usage.manager import BudgetManager, RunBudget
from nexura_runtime.usage.models import BudgetConfig, ModelPricing
from nexura_runtime.usage.pricing import PricingTable

from nexura_runtime.release import BackupEngine
from nexura_runtime.service.api.export import WorkflowExporter
from nexura_runtime.workflow import (
    ModuleRegistry,
    SQLiteWorkflowStore,
    WorkflowEngine,
    WorkflowNode,
    WorkflowSpec,
)

WORKSPACE = "ws-orders"
MODEL = "local-fake-refund-model"
ORDER = {"order_id": "ORD-DELHI-501", "customer": "Amit Verma", "amount": 4500}
POLICIES = [
    ("policy-current", WORKSPACE, False, "Refunds above 2000 INR need manager approval."),
    ("policy-retired", WORKSPACE, True, "Retired: refund everything automatically."),
    ("policy-other-team", "ws-other", False, "Another tenant's private refund rules."),
]


class LocalRefundModel(BaseProvider):
    """Explicitly labelled local fake provider; counts every dispatch."""

    def __init__(self) -> None:
        super().__init__(
            model_id=MODEL,
            capabilities=ProviderCapabilities(can_generate=True, reports_usage=True),
        )
        self.dispatches = 0

    async def _generate_internal(
        self, request: GenerationRequest, context: ExecutionContext | None
    ) -> ModelResponse:
        self.dispatches += 1
        proposal = {"action": "refund", "order_id": ORDER["order_id"], "amount": ORDER["amount"]}
        return ModelResponse(
            message=Message(role="assistant", content=json.dumps(proposal)),
            usage=Usage(prompt_tokens=120, completion_tokens=30, total_tokens=150),
        )

    async def _stream_internal(
        self, request: GenerationRequest, context: ExecutionContext | None
    ) -> AsyncIterator[StreamDelta]:
        raise NotImplementedError("final-only fixture")
        yield  # pragma: no cover

    async def _embed_internal(
        self, texts: list[str], context: ExecutionContext | None
    ) -> EmbeddingResult:
        raise NotImplementedError("no embeddings in this fixture")


def pricing() -> PricingTable:
    table = PricingTable(version="o09-fixture", date="2026-10-07", source="fixture")
    table.add_model_pricing(
        ModelPricing(
            model_id=MODEL,
            input_cost_per_million=Decimal("2.50"),
            output_cost_per_million=Decimal("10.00"),
            effective_date="2026-10-07",
        )
    )
    return table


def refund_count(db: Path) -> int:
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS stub_refunds (idempotency_key TEXT PRIMARY KEY, amount INT)"
        )
        return int(conn.execute("SELECT COUNT(*) FROM stub_refunds").fetchone()[0])


@dataclass
class Runtime:
    """Everything a process would rebuild on start, all bound to one SQLite file."""

    db: Path
    budget_usd: Decimal
    run_id: str = "o09-run"
    model: LocalRefundModel = field(default_factory=LocalRefundModel)

    def __post_init__(self) -> None:
        self.ledger = UsageLedger(db_path=str(self.db))
        self.manager = BudgetManager(pricing_table=pricing(), ledger=self.ledger)
        self.budget: RunBudget = self.manager.create_run_budget(
            workspace_id=WORKSPACE,
            run_id=self.run_id,
            config=BudgetConfig(max_cost_usd=self.budget_usd, deny_unknown_pricing=True),
            default_model=MODEL,
        )
        self.store = SQLiteWorkflowStore(self.db)
        self.registry = self._registry()
        self.engine = WorkflowEngine(SPEC, self.registry, store=self.store, workspace_id=WORKSPACE)

    def _registry(self) -> ModuleRegistry:
        registry = ModuleRegistry()

        def fetch_evidence(state: dict[str, Any]) -> dict[str, Any]:
            return {"order": dict(ORDER), "policies": [list(p) for p in POLICIES]}

        def select_evidence(state: dict[str, Any]) -> dict[str, Any]:
            items = [
                ContextItem(
                    item_id="instructions",
                    item_type="system_prompt",
                    content="Follow the refund policy; never refund without approval.",
                    role="system",
                ),
                ContextItem(
                    item_id="order",
                    item_type="structured_json",
                    content=state["order"],
                    workspace_id=WORKSPACE,
                    is_required_evidence=True,
                ),
            ] + [
                ContextItem(
                    item_id=pid,
                    item_type="retrieval_chunk",
                    content=text,
                    workspace_id=ws,
                    is_deleted=deleted,
                    source_provenance=f"policies/{pid}",
                )
                for pid, ws, deleted, text in state["policies"]
            ]
            selector = ContextSelector(
                ContextPolicy(
                    enabled=True, max_context_tokens=2000, short_context_threshold_tokens=0
                )
            )
            record = selector.select(items, current_workspace_id=WORKSPACE).decision_record
            return {
                "evidence_included": sorted(
                    i.item_id for i in record.items if i.decision != "omitted"
                ),
                "evidence_omitted": {
                    i.item_id: i.reason for i in record.items if i.decision == "omitted"
                },
            }

        async def propose_action(state: dict[str, Any]) -> dict[str, Any]:
            ctx = ExecutionContext(
                workspace_id=WORKSPACE, run_id=self.run_id, node_id="propose", budget=self.budget
            )
            prompt = f"Evidence: {state['evidence_included']}. Propose an action."
            response = await self.model.generate(
                [Message(role="user", content=prompt)], context=ctx
            )
            return {"proposal": json.loads(response.message.content or "{}")}

        def execute_refund(state: dict[str, Any]) -> dict[str, Any]:
            proposal = state["proposal"]
            key = f"refund:{proposal['order_id']}"
            with sqlite3.connect(self.db) as conn:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS stub_refunds "
                    "(idempotency_key TEXT PRIMARY KEY, amount INT)"
                )
                inserted = conn.execute(
                    "INSERT OR IGNORE INTO stub_refunds VALUES (?, ?)", (key, proposal["amount"])
                ).rowcount
            return {"refund_key": key, "refund_newly_executed": bool(inserted)}

        registry.register("fetch_evidence", fetch_evidence)
        registry.register("select_evidence", select_evidence)
        registry.register("propose_action", propose_action)
        registry.register("execute_refund", execute_refund)
        return registry

    async def close(self) -> None:
        await self.store.close()


SPEC = WorkflowSpec(
    name="O09OrderSupport",
    entry_node="fetch",
    nodes={
        "fetch": WorkflowNode(id="fetch", handler="fetch_evidence", default_transition="select"),
        "select": WorkflowNode(
            id="select", handler="select_evidence", default_transition="propose"
        ),
        "propose": WorkflowNode(
            id="propose", handler="propose_action", default_transition="refund"
        ),
        "refund": WorkflowNode(id="refund", handler="execute_refund", requires_approval=True),
    },
    terminal_nodes=["refund"],
)


@contextmanager
def offline() -> Iterator[None]:
    """Fail loudly if anything tries to open a non-loopback connection."""
    original = socket.socket.connect

    def guarded(sock: socket.socket, address: Any) -> None:
        host = address[0] if isinstance(address, tuple) else str(address)
        if host not in ("127.0.0.1", "::1", "localhost"):
            raise RuntimeError(f"Offline scenario attempted external connection to {host}")
        return original(sock, address)

    socket.socket.connect = guarded  # type: ignore[method-assign,assignment]
    try:
        yield
    finally:
        socket.socket.connect = original  # type: ignore[method-assign]


async def reference_scenario(tmp: Path) -> dict[str, Any]:
    """Happy path with restart, backup/restore, duplicate-resume and export."""
    db = tmp / "orders.sqlite3"
    first = Runtime(db, budget_usd=Decimal("0.05"))
    paused = await first.engine.run({"order_id": ORDER["order_id"]})
    assert paused.pending_approval is not None
    approval_id = paused.pending_approval.approval_id
    spent_before_restart = first.budget.get_status().total_spent_usd
    dispatches_before_restart = first.model.dispatches
    await first.close()

    # Back up the paused state before approving, then restore it to a new file.
    archive = tmp / "paused-backup.tar.gz"
    BackupEngine().create_backup(db, target_archive_path=archive)
    restored_db = tmp / "restored.sqlite3"
    BackupEngine().restore_backup(archive, target_db_path=restored_db)
    restored = Runtime(restored_db, budget_usd=Decimal("0.05"))
    restored_spent = restored.budget.get_status().total_spent_usd
    restored_done = await restored.engine.resume(
        paused.workflow_run_id, approval_id, decision="approve"
    )
    await restored.close()

    # "Restart": every store is reopened from the original file.
    second = Runtime(db, budget_usd=Decimal("0.05"))
    spent_after_restart = second.budget.get_status().total_spent_usd
    done = await second.engine.resume(paused.workflow_run_id, approval_id, decision="approve")
    duplicate_rejected = False
    try:
        await second.engine.resume(paused.workflow_run_id, approval_id, decision="approve")
    except (ApprovalAlreadyClaimedError, WorkflowTerminalError):
        duplicate_rejected = True
    status = second.budget.get_status()
    await second.close()

    bundle = WorkflowExporter().export_bundle(SPEC, tmp / "bundle", registry=second.registry)
    exported_spec = json.loads((tmp / "bundle" / "workflow.json").read_text(encoding="utf-8"))
    successes = 1 if done.status == "completed" else 0

    return {
        "paused_status": paused.status,
        "paused_at": paused.pending_approval.node_id,
        "final_status": done.status,
        "evidence_included": done.state["evidence_included"],
        "evidence_omitted": done.state["evidence_omitted"],
        "refunds_executed": refund_count(db),
        "duplicate_resume_rejected": duplicate_rejected,
        "provider_dispatches": dispatches_before_restart + second.model.dispatches,
        "spent_before_restart": str(spent_before_restart),
        "spent_after_restart": str(spent_after_restart),
        "total_spent_usd": str(status.total_spent_usd),
        "unknown_usage_count": status.unknown_usage_count,
        "cost_per_success": str(status.total_spent_usd / successes) if successes else None,
        "restored_resume_status": restored_done.status,
        "restored_refunds_executed": refund_count(restored_db),
        "restored_spent_usd": str(restored_spent),
        "export_spec_hash_matches": bundle.spec_hash == SPEC.canonical_hash(),
        "export_nodes": sorted(exported_spec["nodes"]),
    }


async def budget_exhaustion_scenario(tmp: Path) -> dict[str, Any]:
    """A budget too small for one call must stop before the provider is dispatched."""
    db = tmp / "tiny-budget.sqlite3"
    runtime = Runtime(db, budget_usd=Decimal("0.0000001"))
    error = ""
    try:
        await runtime.engine.run({"order_id": ORDER["order_id"]}, workflow_run_id="o09-tiny")
    except WorkflowExecutionError as exc:
        error = str(exc)
    checkpoint = await runtime.store.get("o09-tiny")
    await runtime.close()
    return {
        "status": checkpoint.status if checkpoint else "missing",
        "error": error,
        "provider_dispatches": runtime.model.dispatches,
        "refunds_executed": refund_count(db),
        "pending_approval": bool(checkpoint and checkpoint.pending_approval),
    }


async def main() -> None:
    with offline(), tempfile.TemporaryDirectory(prefix="nexura-o09-") as tmp_name:
        tmp = Path(tmp_name)
        reference = await reference_scenario(tmp)
        exhausted = await budget_exhaustion_scenario(tmp)
    print(json.dumps({"reference": reference, "budget_exhaustion": exhausted}, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
