"""Acceptance test suite for O02: Durable recovery, exact-action approvals and effects.

Verifies:
1. Approval exact-action bindings: canonical argument digests, workspace, actor role,
   expiration, and duplicate resume prevention.
2. Real OS process kill (multiprocessing.Process + SIGKILL) testing of lost remote responses,
   ensuring non-idempotent effects stay indeterminate without phantom double execution.
3. Safe recovery from pre-dispatch worker crash.
4. Stale worker process fencing via CAS tokens across concurrent processes.
5. Carrying O01 budget reservations across checkpoint pause and resumption without double charging.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import multiprocessing
import os
import signal
import sqlite3
import time
import urllib.error
import urllib.request
from collections.abc import AsyncIterator
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest
from nexura_runtime.contracts.canonical import canonical_digest
from nexura_runtime.contracts.context import ExecutionContext
from nexura_runtime.contracts.errors import IndeterminateEffectError
from nexura_runtime.errors import (
    ApprovalAlreadyClaimedError,
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
from nexura_runtime.usage.manager import BudgetManager
from nexura_runtime.usage.models import BudgetConfig, ModelPricing
from nexura_runtime.usage.pricing import PricingTable

from nexura_runtime.runtime.approvals.errors import (
    ApprovalExpiredError,
    MutatedArgumentsError,
    WrongActorError,
    WrongWorkspaceError,
)
from nexura_runtime.runtime.execution import (
    DurableExecutionEngine,
    DurableNodeSpec,
    DurableWorkflowSpec,
    FencingConflictError,
)
from nexura_runtime.storage import SQLiteRepository
from nexura_runtime.workflow import (
    ModuleRegistry,
    SQLiteWorkflowStore,
    WorkflowEngine,
    WorkflowNode,
    WorkflowSpec,
)

# ============================================================================
# Helpers & Fixtures for Multi-Process OS Tests
# ============================================================================


def _run_http_effect_server(db_path: str, port_queue: multiprocessing.Queue[int]) -> None:
    """Independent durable effect HTTP service recording calls into SQLite."""

    class EffectHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            content_len = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_len)
            data = json.loads(body.decode("utf-8"))

            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS remote_effects ("
                    "  effect_id TEXT PRIMARY KEY, run_id TEXT, action TEXT, amount INTEGER"
                    ");"
                )
                conn.execute(
                    "INSERT INTO remote_effects (effect_id, run_id, action, amount) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        data.get("effect_id", "eff_1"),
                        data.get("run_id", "run_1"),
                        data.get("action", "transfer"),
                        data.get("amount", 0),
                    ),
                )

            # Check if simulation requests connection drop without response
            if data.get("drop_response", False):
                self.close_connection = True
                return

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            resp_body = json.dumps({"status": "SUCCESS", "id": data.get("effect_id")}).encode()
            self.wfile.write(resp_body)

        def log_message(self, format: str, *args: Any) -> None:
            pass  # Suppress stderr logging in tests

    server = HTTPServer(("127.0.0.1", 0), EffectHandler)
    port_queue.put(server.server_port)
    server.serve_forever()


def _worker_process_target(
    db_path: str,
    run_id: str,
    worker_id: str,
    effect_url: str,
    phase_queue: multiprocessing.Queue[str],
    spec_dict: dict[str, Any],
    hold_after_claim: bool = False,
) -> None:
    """Worker child process executing a durable workflow step.

    ``hold_after_claim`` parks the worker right after its claim, so the parent can kill
    it deterministically before the step records any dispatch intent.
    """

    async def _execute() -> None:
        repo = SQLiteRepository(db_path)
        engine = DurableExecutionEngine(repo)

        async def bank_transfer_handler(state: dict[str, Any]) -> dict[str, Any]:
            phase_queue.put("pre_http_dispatch")
            req = urllib.request.Request(
                effect_url,
                data=json.dumps(
                    {
                        "effect_id": state.get("effect_id", "eff_test"),
                        "run_id": run_id,
                        "action": "transfer",
                        "amount": state.get("amount", 500),
                        "drop_response": state.get("drop_response", False),
                    }
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            with (
                contextlib.suppress(urllib.error.URLError, OSError),
                urllib.request.urlopen(req, timeout=5) as resp,
            ):
                resp.read()

            phase_queue.put("remote_accepted")
            # Sleep to allow parent process to issue SIGKILL while in-flight before checkpoint
            await asyncio.sleep(10.0)
            return {"transferred": True}

        engine.register_handler("bank_transfer", bank_transfer_handler)
        spec = DurableWorkflowSpec.model_validate(spec_dict)

        try:
            await engine.claim_run("ws_durable", run_id, worker_id, lease_duration=0.2)
            phase_queue.put("claimed")
            if hold_after_claim:
                await asyncio.sleep(30.0)  # killed here by the parent
            await engine.execute_step("ws_durable", run_id, worker_id, spec)
            phase_queue.put("completed")
        except Exception as exc:
            phase_queue.put(f"error:{type(exc).__name__}")
        finally:
            await repo.close()

    asyncio.run(_execute())


# ============================================================================
# Test Classes
# ============================================================================


class TestApprovalExactActionBindings:
    """Deliverable 2: Bind approval to canonical arguments, workspace, role, and expiry."""

    @pytest.mark.asyncio
    async def test_mutated_arguments_rejected_before_gated_effect(self, tmp_path: Path) -> None:
        """Mutated arguments between request and approval raise MutatedArgumentsError.

        Zero side effects should be dispatched.
        """
        db_path = tmp_path / "wf_approval.db"
        store = SQLiteWorkflowStore(db_path)
        registry = ModuleRegistry()

        effect_calls = []

        def secure_action(state: dict[str, Any]) -> dict[str, Any]:
            effect_calls.append(state)
            return {"executed": True}

        registry.register("secure_action", secure_action)

        spec = WorkflowSpec(
            "wf_wire",
            entry_node="gate",
            terminal_nodes=["gate"],
            nodes={
                "gate": WorkflowNode(
                    id="gate",
                    handler="secure_action",
                    requires_approval=True,
                    required_role="admin",
                )
            },
        )
        engine = WorkflowEngine(spec, registry, store=store, workspace_id="ws_finance")

        # 1. Initial run pauses at gate
        init_state = {"action": "wire_transfer", "amount": 100_000, "recipient": "ACME_CORP"}
        result = await engine.run(init_state, workflow_run_id="run_wire_01")
        assert result.status == "paused"
        assert result.pending_approval is not None
        appr_id = result.pending_approval.approval_id
        original_digest = result.pending_approval.payload_digest
        assert original_digest == canonical_digest(init_state)
        assert len(effect_calls) == 0

        # 2. Attempt resume with mutated argument digest
        tampered_digest = canonical_digest({"action": "wire_transfer", "amount": 999_999})
        with pytest.raises(MutatedArgumentsError) as exc_info:
            await engine.resume(
                "run_wire_01",
                appr_id,
                expected_payload_digest=tampered_digest,
            )
        assert exc_info.value.expected_digest == tampered_digest
        assert exc_info.value.actual_digest == original_digest
        assert len(effect_calls) == 0, "No gated effects must be dispatched on mutated arguments!"

    @pytest.mark.asyncio
    async def test_cross_workspace_approval_rejected(self, tmp_path: Path) -> None:
        """Approval resolution from a foreign workspace raises WrongWorkspaceError."""
        db_path = tmp_path / "wf_approval.db"
        store = SQLiteWorkflowStore(db_path)
        registry = ModuleRegistry()
        registry.register("action", lambda state: {"ok": True})

        spec = WorkflowSpec(
            "wf_alpha",
            entry_node="node",
            terminal_nodes=["node"],
            nodes={"node": WorkflowNode(id="node", handler="action", requires_approval=True)},
        )
        engine = WorkflowEngine(spec, registry, store=store, workspace_id="ws_corp_alpha")
        res = await engine.run({"test": 1}, workflow_run_id="run_alpha_01")
        assert res.pending_approval is not None
        appr_id = res.pending_approval.approval_id

        # Attempt to resume specifying workspace 'ws_corp_beta'
        with pytest.raises(WrongWorkspaceError) as exc_info:
            await engine.resume("run_alpha_01", appr_id, workspace_id="ws_corp_beta")

        assert exc_info.value.request_workspace == "ws_corp_alpha"
        assert exc_info.value.decision_workspace == "ws_corp_beta"

    @pytest.mark.asyncio
    async def test_expired_approval_rejected_and_marked_failed(self, tmp_path: Path) -> None:
        """Expired approvals cannot be approved and result in ApprovalExpiredError."""
        db_path = tmp_path / "wf_approval.db"
        store = SQLiteWorkflowStore(db_path)
        registry = ModuleRegistry()
        registry.register("action", lambda state: {"done": True})

        spec = WorkflowSpec(
            "wf_expire",
            entry_node="node",
            terminal_nodes=["node"],
            nodes={"node": WorkflowNode(id="node", handler="action", requires_approval=True)},
        )
        # Configure very short approval TTL (100ms)
        engine = WorkflowEngine(spec, registry, store=store, approval_ttl=0.1)
        res = await engine.run({}, workflow_run_id="run_expire_01")
        assert res.pending_approval is not None
        appr_id = res.pending_approval.approval_id

        # Wait for expiration
        await asyncio.sleep(0.15)

        with pytest.raises(ApprovalExpiredError):
            await engine.resume("run_expire_01", appr_id)

        # Checkpoint is marked failed in store
        cp = await store.get("run_expire_01")
        assert cp is not None
        assert cp.status == "failed"

    @pytest.mark.asyncio
    async def test_foreign_actor_role_rejected(self, tmp_path: Path) -> None:
        """Decisions by actors lacking required role raise WrongActorError."""
        db_path = tmp_path / "wf_approval.db"
        store = SQLiteWorkflowStore(db_path)
        registry = ModuleRegistry()
        registry.register("action", lambda state: {"ok": True})

        spec = WorkflowSpec(
            "wf_role",
            entry_node="node",
            terminal_nodes=["node"],
            nodes={
                "node": WorkflowNode(
                    id="node",
                    handler="action",
                    requires_approval=True,
                    required_role="admin",
                )
            },
        )
        engine = WorkflowEngine(spec, registry, store=store)
        res = await engine.run({}, workflow_run_id="run_role_01")
        assert res.pending_approval is not None
        appr_id = res.pending_approval.approval_id

        with pytest.raises(WrongActorError) as exc_info:
            await engine.resume(
                "run_role_01",
                appr_id,
                actor="user_intern",
                actor_role="viewer",
            )
        assert exc_info.value.required_role == "admin"
        assert exc_info.value.deciding_role == "viewer"

    @pytest.mark.asyncio
    async def test_rejected_decision_is_terminal_with_zero_effects(self, tmp_path: Path) -> None:
        """Rejecting an approval records status='rejected' and 0 effects.

        Forbids further resume attempts.
        """
        db_path = tmp_path / "wf_approval.db"
        store = SQLiteWorkflowStore(db_path)
        registry = ModuleRegistry()
        dispatched = []

        def _action(state: dict[str, Any]) -> dict[str, Any]:
            dispatched.append(1)
            return {"done": True}

        registry.register("action", _action)

        spec = WorkflowSpec(
            "wf_rej",
            entry_node="node",
            terminal_nodes=["node"],
            nodes={"node": WorkflowNode(id="node", handler="action", requires_approval=True)},
        )
        engine = WorkflowEngine(spec, registry, store=store)
        res = await engine.run({}, workflow_run_id="run_rej_01")
        assert res.pending_approval is not None
        appr_id = res.pending_approval.approval_id

        # Reject decision
        reject_res = await engine.resume("run_rej_01", appr_id, decision="reject")
        assert reject_res.status == "rejected"
        assert len(dispatched) == 0

        # Attempt to resume again raises WorkflowTerminalError
        with pytest.raises(WorkflowTerminalError):
            await engine.resume("run_rej_01", appr_id, decision="approve")
        assert len(dispatched) == 0

    @pytest.mark.asyncio
    async def test_duplicate_resume_claim_rejected(self, tmp_path: Path) -> None:
        """Repeated resume on already-claimed approval raises ApprovalAlreadyClaimedError."""
        db_path = tmp_path / "wf_approval.db"
        store = SQLiteWorkflowStore(db_path)
        registry = ModuleRegistry()
        calls = 0

        def action(state: dict[str, Any]) -> dict[str, Any]:
            nonlocal calls
            calls += 1
            return {"calls": calls}

        registry.register("action", action)
        spec = WorkflowSpec(
            "wf_dup",
            entry_node="node",
            terminal_nodes=["node"],
            nodes={"node": WorkflowNode(id="node", handler="action", requires_approval=True)},
        )
        engine = WorkflowEngine(spec, registry, store=store)
        res = await engine.run({}, workflow_run_id="run_dup_01")
        assert res.pending_approval is not None
        appr_id = res.pending_approval.approval_id

        # First resume claims and succeeds
        res1 = await engine.resume("run_dup_01", appr_id, decision="approve")
        assert res1.status == "completed"
        assert calls == 1

        # Second resume on the same approval raises terminal or claimed error
        with pytest.raises((ApprovalAlreadyClaimedError, WorkflowTerminalError)):
            await engine.resume("run_dup_01", appr_id, decision="approve")
        assert calls == 1


class TestDurableOSProcessKillAndRecovery:
    """Deliverable 3 & 4: Kill workers with OS SIGKILL and recover without duplicate effects."""

    def test_worker_os_process_kill_after_remote_acceptance_stays_indeterminate(
        self, tmp_path: Path
    ) -> None:
        """When an OS worker process is killed via SIGKILL after remote acceptance,
        fresh recovery marks the non-idempotent effect indeterminate without extra dispatch.
        """
        db_path = str(tmp_path / "durable_runs.db")
        effects_db = str(tmp_path / "effects.db")

        # 1. Start background HTTP server
        port_queue: multiprocessing.Queue[int] = multiprocessing.Queue()
        server_proc = multiprocessing.Process(
            target=_run_http_effect_server,
            args=(effects_db, port_queue),
            daemon=True,
        )
        server_proc.start()
        port = port_queue.get(timeout=5)
        effect_url = f"http://127.0.0.1:{port}/transfer"

        # 2. Initialize run in SQLite store
        run_id = "run_kill_remote_01"
        spec = DurableWorkflowSpec(
            workflow_id="wf_money",
            entry_node="transfer",
            terminal_nodes=["transfer"],
            nodes={
                "transfer": DurableNodeSpec(
                    node_id="transfer",
                    handler="bank_transfer",
                    effect_policy="non_idempotent_write",
                )
            },
        )
        spec_dict = spec.model_dump()

        async def _init() -> None:
            repo = SQLiteRepository(db_path)
            engine = DurableExecutionEngine(repo)
            await engine.init_run(
                "ws_durable",
                run_id,
                spec,
                initial_state={"effect_id": "eff_wire_99", "amount": 1000},
            )
            await repo.close()

        asyncio.run(_init())

        # 3. Launch worker OS process
        phase_queue: multiprocessing.Queue[str] = multiprocessing.Queue()
        worker1 = multiprocessing.Process(
            target=_worker_process_target,
            args=(db_path, run_id, "worker_proc_1", effect_url, phase_queue, spec_dict),
        )
        worker1.start()

        # Wait until remote effect is accepted
        phase = ""
        while phase != "remote_accepted":
            phase = phase_queue.get(timeout=10)

        # 4. KILL worker1 with SIGKILL while waiting before checkpoint commit
        assert worker1.pid is not None
        os.kill(worker1.pid, signal.SIGKILL)
        worker1.join(timeout=3)
        assert not worker1.is_alive()
        # Wait for worker1's 0.2s lease to expire
        time.sleep(0.3)

        # Verify effect was recorded in remote effect server
        with sqlite3.connect(effects_db) as conn:
            query = "SELECT COUNT(*) FROM remote_effects WHERE effect_id = 'eff_wire_99'"
            cur = conn.execute(query)
            count = cur.fetchone()[0]
        assert count == 1, "Remote effect must have been accepted by server"

        # 5. Recovery with a fresh worker process
        # Fresh worker must detect pre-dispatch intent and raise IndeterminateEffectError
        async def _recover() -> None:
            repo = SQLiteRepository(db_path)
            engine = DurableExecutionEngine(repo)
            with pytest.raises(IndeterminateEffectError) as exc_info:
                # Use fresh worker_id; claim with lease
                await engine.claim_run("ws_durable", run_id, "worker_proc_2", lease_duration=5.0)
                await engine.execute_step("ws_durable", run_id, "worker_proc_2", spec)
            assert "Indeterminate remote outcome" in str(exc_info.value)
            await repo.close()

        asyncio.run(_recover())

        # CRUCIAL CHECK: Remote effect was NOT called a second time!
        with sqlite3.connect(effects_db) as conn:
            query = "SELECT COUNT(*) FROM remote_effects WHERE effect_id = 'eff_wire_99'"
            cur = conn.execute(query)
            final_count = cur.fetchone()[0]
        assert final_count == 1, "Remote effect must NOT be re-executed on indeterminate recovery!"

        # Clean up HTTP server
        server_proc.terminate()
        server_proc.join(timeout=2)

    def test_worker_killed_before_dispatch_safely_retried(self, tmp_path: Path) -> None:
        """When a worker process crashes before dispatch, recovery safely retries.

        The effect executes exactly once.
        """
        db_path = str(tmp_path / "durable_runs.db")
        effects_db = str(tmp_path / "effects.db")

        port_queue: multiprocessing.Queue[int] = multiprocessing.Queue()
        server_proc = multiprocessing.Process(
            target=_run_http_effect_server,
            args=(effects_db, port_queue),
            daemon=True,
        )
        server_proc.start()
        port = port_queue.get(timeout=5)
        effect_url = f"http://127.0.0.1:{port}/transfer"

        run_id = "run_kill_pre_dispatch_01"
        spec = DurableWorkflowSpec(
            workflow_id="wf_money",
            entry_node="transfer",
            terminal_nodes=["transfer"],
            nodes={
                "transfer": DurableNodeSpec(
                    node_id="transfer",
                    handler="bank_transfer",
                    effect_policy="non_idempotent_write",
                )
            },
        )
        spec_dict = spec.model_dump()

        async def _init() -> None:
            repo = SQLiteRepository(db_path)
            engine = DurableExecutionEngine(repo)
            await engine.init_run(
                "ws_durable",
                run_id,
                spec,
                initial_state={"effect_id": "eff_wire_77", "amount": 250},
            )
            await repo.close()

        asyncio.run(_init())

        # Launch worker and kill it immediately after claim
        phase_queue: multiprocessing.Queue[str] = multiprocessing.Queue()
        worker1 = multiprocessing.Process(
            target=_worker_process_target,
            args=(db_path, run_id, "worker_proc_1", effect_url, phase_queue, spec_dict, True),
        )
        worker1.start()

        # The worker parks after its claim, so the kill always lands before dispatch. (It
        # used to race on into execute_step, and recovery then rightly reported the
        # non-idempotent effect as indeterminate.)
        phase = phase_queue.get(timeout=5)
        assert phase == "claimed"
        # Kill worker before remote acceptance
        if worker1.pid is not None:
            os.kill(worker1.pid, signal.SIGKILL)
        worker1.join(timeout=3)
        # Wait for worker1's 0.2s lease to expire
        time.sleep(0.3)

        # Fresh worker executes step successfully
        async def _recover_and_execute() -> None:
            repo = SQLiteRepository(db_path)
            engine = DurableExecutionEngine(repo)

            def bank_transfer_handler(state: dict[str, Any]) -> dict[str, Any]:
                req = urllib.request.Request(
                    effect_url,
                    data=json.dumps(
                        {
                            "effect_id": state["effect_id"],
                            "run_id": run_id,
                            "action": "transfer",
                            "amount": state["amount"],
                        }
                    ).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    resp.read()
                return {"transferred": True}

            engine.register_handler("bank_transfer", bank_transfer_handler)
            # Reclaim run with fresh worker
            await engine.claim_run("ws_durable", run_id, "worker_proc_2", lease_duration=5.0)
            res = await engine.execute_step("ws_durable", run_id, "worker_proc_2", spec)
            assert res.status == "completed"
            await repo.close()

        asyncio.run(_recover_and_execute())

        # Remote effect must have been called exactly once
        with sqlite3.connect(effects_db) as conn:
            query = "SELECT COUNT(*) FROM remote_effects WHERE effect_id = 'eff_wire_77'"
            cur = conn.execute(query)
            count = cur.fetchone()[0]
        assert count == 1

        server_proc.terminate()
        server_proc.join(timeout=2)


class TestStaleWorkerFencing:
    """Deliverable 5: Stale workers cannot commit after a newer fenced lease."""

    @pytest.mark.asyncio
    async def test_stale_worker_cannot_commit_after_newer_lease(self, tmp_path: Path) -> None:
        """A stale worker attempting to execute step after lease expiry is rejected."""
        db_path = tmp_path / "fencing.db"
        repo = SQLiteRepository(db_path)
        engine = DurableExecutionEngine(repo)
        engine.register_handler("node_1", lambda state: {"ok": True})

        spec = DurableWorkflowSpec(
            workflow_id="wf_fence",
            entry_node="node_1",
            terminal_nodes=["node_1"],
            nodes={"node_1": DurableNodeSpec(node_id="node_1", handler="node_1")},
        )
        await engine.init_run("ws_fence", "run_fence_01", spec)

        # Worker 1 claims run with very short lease (100ms)
        claim1 = await engine.claim_run("ws_fence", "run_fence_01", "worker_1", lease_duration=0.1)
        assert claim1.fencing_token == 1

        # Wait for Worker 1 lease to expire
        await asyncio.sleep(0.15)

        # Worker 2 claims run, advancing fencing token to 2
        claim2 = await engine.claim_run("ws_fence", "run_fence_01", "worker_2", lease_duration=5.0)
        assert claim2.fencing_token == 2

        # Worker 1 wakes up and attempts to execute step: FencingConflictError!
        with pytest.raises(FencingConflictError) as exc_info:
            await engine.execute_step("ws_fence", "run_fence_01", "worker_1", spec)

        assert exc_info.value.worker_id == "worker_1"

        # Worker 2 executes step successfully
        res2 = await engine.execute_step("ws_fence", "run_fence_01", "worker_2", spec)
        assert res2.status == "completed"
        await repo.close()


class TestBudgetReservationRecovery:
    """Deliverable 6: Carry O01 reservations across recovery.

    Ensures spending is not reset or double charged.
    """

    @pytest.mark.asyncio
    async def test_budget_reservations_carried_across_workflow_pause_and_resume(
        self, tmp_path: Path
    ) -> None:
        """Budget spend and reservations carry across workflow pauses and SQLite reopens."""
        db_path = tmp_path / "wf_budget.db"
        usage_db = tmp_path / "usage_ledger.db"
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
            run_id="run_budgeted_wf",
            config=BudgetConfig(max_cost_usd=Decimal("0.10")),
            default_model="mock-gpt-4o",
        )

        class MockBillingProvider(BaseProvider):
            def __init__(self) -> None:
                super().__init__(
                    "mock-gpt-4o",
                    ProviderCapabilities(can_stream=False, can_tools=False, can_embed=False),
                )
                self.call_count = 0

            async def _generate_internal(
                self, request: GenerationRequest, context: ExecutionContext | None
            ) -> ModelResponse:
                self.call_count += 1
                return ModelResponse(
                    message=Message(role="assistant", content="Result"),
                    usage=Usage(prompt_tokens=100, completion_tokens=50, total_tokens=150),
                )

            async def _stream_internal(
                self, request: GenerationRequest, context: ExecutionContext | None
            ) -> AsyncIterator[StreamDelta]:
                if False:
                    yield  # pragma: no cover

            async def _embed_internal(
                self, texts: list[str], context: ExecutionContext | None
            ) -> EmbeddingResult:
                return EmbeddingResult(embeddings=[], model=self.model_id, usage=Usage())

        provider = MockBillingProvider()
        registry = ModuleRegistry()

        active_budget = budget

        async def step_1(state: dict[str, Any]) -> dict[str, Any]:
            ctx = ExecutionContext(
                workspace_id="ws_finance",
                run_id="run_budgeted_wf",
                node_id="step_1",
                budget=active_budget,
            )
            resp = await provider.generate([Message(role="user", content="Prompt 1")], context=ctx)
            return {"step1_content": resp.message.content}

        async def step_2(state: dict[str, Any]) -> dict[str, Any]:
            ctx = ExecutionContext(
                workspace_id="ws_finance",
                run_id="run_budgeted_wf",
                node_id="step_2",
                budget=active_budget,
            )
            resp = await provider.generate([Message(role="user", content="Prompt 2")], context=ctx)
            return {"step2_content": resp.message.content}

        registry.register("step_1", step_1)
        registry.register("step_2", step_2)

        spec = WorkflowSpec(
            "wf_budget",
            entry_node="step_1",
            terminal_nodes=["step_2"],
            nodes={
                "step_1": WorkflowNode(
                    id="step_1",
                    handler="step_1",
                    default_transition="step_2",
                ),
                "step_2": WorkflowNode(
                    id="step_2",
                    handler="step_2",
                    requires_approval=True,  # Pauses here
                ),
            },
        )

        engine1 = WorkflowEngine(
            spec,
            registry,
            store=store,
            workspace_id="ws_finance",
            budget=budget,
        )

        # 1. Execute run: runs step_1, then pauses before step_2
        res1 = await engine1.run({}, workflow_run_id="run_budgeted_wf")
        assert res1.status == "paused"
        assert res1.pending_approval is not None
        assert provider.call_count == 1

        # Verify step 1 spend
        status1 = budget.get_status()
        assert status1.provider_calls_count == 1
        assert status1.total_spent_usd > Decimal("0")
        spend_after_step1 = status1.total_spent_usd

        # 2. Simulate process restart: fresh manager from SQLite ledger
        ledger2 = UsageLedger(db_path=str(usage_db))
        manager2 = BudgetManager(ledger=ledger2, pricing_table=pricing)
        budget2 = manager2.create_run_budget(
            workspace_id="ws_finance",
            run_id="run_budgeted_wf",
            config=BudgetConfig(max_cost_usd=Decimal("0.10")),
            default_model="mock-gpt-4o",
        )
        assert budget2.get_status().total_spent_usd == spend_after_step1
        active_budget = budget2

        # Fresh engine instance
        engine2 = WorkflowEngine(
            spec,
            registry,
            store=store,
            workspace_id="ws_finance",
            budget=budget2,
        )

        # 3. Resume after approval
        appr_id = res1.pending_approval.approval_id
        res2 = await engine2.resume(
            "run_budgeted_wf",
            appr_id,
            decision="approve",
        )
        assert res2.status == "completed"
        assert provider.call_count == 2

        # 4. Spending verification: spending accumulated without reset or double charge
        status2 = budget2.get_status()
        assert status2.provider_calls_count == 2
        assert status2.total_spent_usd == spend_after_step1 * 2
        assert status2.total_tokens == 300
