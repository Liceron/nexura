# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Offline shared-allowance races and failure lifecycle regressions."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from threading import Barrier

import pytest
from nexura_agents import Agent, FakeModel, Runner, ToolCall, Usage
from nexura_agents.contracts.context import ExecutionContext
from nexura_agents.providers.base import BaseProvider
from nexura_agents.providers.types import (
    EmbeddingResult,
    GenerationRequest,
    ProviderCapabilities,
    StreamDelta,
)
from nexura_agents.types import ModelResponse
from nexura_agents.usage import BudgetConfig, BudgetManager
from nexura_agents.usage.errors import BudgetExceededError, InvalidPricingError
from nexura_agents.usage.ledger import UsageLedger


@pytest.mark.parametrize("durable", [False, True])
@pytest.mark.parametrize("tokens", [False, True])
def test_threads_share_admission(tmp_path: Path, durable: bool, tokens: bool) -> None:
    path = str(tmp_path / "budget.db") if durable else None
    ledgers = [UsageLedger(path)] if not durable else [UsageLedger(path), UsageLedger(path)]
    config = BudgetConfig(max_tokens=30) if tokens else BudgetConfig(max_cost_usd=Decimal("3"))
    barrier = Barrier(12)

    def admit(i: int) -> bool:
        barrier.wait(timeout=10)
        try:
            ledgers[i % len(ledgers)].reserve(
                workspace_id="ws",
                run_id="shared",
                estimated_tokens=10,
                estimated_cost_usd=Decimal("1"),
                config=config,
            )
            return True
        except BudgetExceededError:
            return False

    with ThreadPoolExecutor(max_workers=12) as pool:
        assert sum(pool.map(admit, range(12))) == 3
    status = ledgers[0].get_status("ws", "shared", config=config)
    assert status.total_reserved_usd == 3
    if tokens:
        assert status.remaining_tokens == 0


def test_lost_worker_does_not_refund_on_expiry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = str(tmp_path / "lost.db")
    ledger = UsageLedger(path)
    reservation = ledger.reserve(
        workspace_id="ws",
        run_id="run",
        estimated_tokens=10,
        estimated_cost_usd=Decimal("1"),
        timeout_seconds=1,
    )
    monkeypatch.setattr("nexura_agents.usage.ledger.time.time", lambda: reservation.created_at + 10)
    restarted = UsageLedger(path)
    assert restarted.get_status("ws", "run").total_reserved_usd == 1
    with pytest.raises(BudgetExceededError):
        restarted.reserve(
            workspace_id="ws",
            run_id="run",
            estimated_cost_usd=Decimal("1"),
            config=BudgetConfig(max_cost_usd=Decimal("1")),
        )
    entry = restarted.reconcile(reservation.reservation_id, is_cancelled=True)
    assert entry.amount_usd == 1
    assert restarted.get_status("ws", "run").total_reserved_usd == 0
    assert restarted.reconcile(reservation.reservation_id).entry_id == entry.entry_id


def test_duplicate_settlement_across_handles(tmp_path: Path) -> None:
    path = str(tmp_path / "idem.db")
    managers = [BudgetManager(ledger=UsageLedger(path)) for _ in range(2)]
    budgets = [m.create_run_budget("ws", "run") for m in managers]
    reservation = budgets[0].admit_provider_call("gpt-4o", estimated_tokens=100)
    barrier = Barrier(2)

    def settle(i: int) -> Decimal:
        barrier.wait(timeout=10)
        return (
            budgets[i]
            .settle_provider_call(
                reservation,
                usage=Usage(prompt_tokens=10, completion_tokens=10, total_tokens=20),
            )
            .amount_usd
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        costs = list(pool.map(settle, range(2)))
    # A contradictory duplicate must return the original authoritative settlement too.
    duplicate = budgets[0].settle_provider_call(reservation, usage=None)
    assert duplicate.amount_usd == costs[0] == costs[1]
    status = budgets[1].get_status()
    assert status.provider_calls_count == 1
    assert status.total_tokens == 20
    assert status.total_reserved_usd == 0
    assert status.total_spent_usd == costs[0]


@pytest.mark.parametrize("usage", [None, Usage(), Usage(total_tokens=3)])
def test_unknown_usage_preserves_token_commitment(usage: Usage | None) -> None:
    budget = BudgetManager().create_run_budget(
        "ws",
        "run",
        config=BudgetConfig(max_tokens=10, conservative_unreported_tokens=10),
    )
    reservation = budget.admit_provider_call("gpt-4o", estimated_tokens=10)
    cost = budget.settle_provider_call(reservation, usage=usage)
    assert cost.is_estimate
    assert cost.amount_usd > 0
    status = budget.get_status()
    assert status.remaining_tokens == 0
    assert status.total_reserved_usd == 0
    with pytest.raises(BudgetExceededError):
        budget.admit_provider_call("gpt-4o", estimated_tokens=1)


class ScriptedFakeProvider(BaseProvider):
    """Local FakeModel behind the real provider admission boundary; no transport."""

    def __init__(self, action: str = "ok", fake: FakeModel | None = None) -> None:
        super().__init__(
            model_id="gpt-4o", capabilities=ProviderCapabilities(can_generate=True, can_tools=True)
        )
        self.fake = fake or FakeModel(
            responses=["ok"],
            default_usage=Usage(
                prompt_tokens=128,
                completion_tokens=128,
                total_tokens=256,
            ),
        )
        self.action = action
        self.started = asyncio.Event()
        self.finish = asyncio.Event()
        self.calls = 0

    def _estimate_tokens(self, req: GenerationRequest) -> int:
        """Scripted admission size; these tests exercise accounting, not token estimation."""
        return 256

    async def _generate_internal(
        self,
        request: GenerationRequest,
        context: ExecutionContext | None,
    ) -> ModelResponse:
        self.calls += 1
        self.started.set()
        if self.action == "error":
            raise RuntimeError("scripted worker failure")
        await self.finish.wait()
        return await self.fake.generate(request.messages)

    async def _stream_internal(
        self,
        request: GenerationRequest,
        context: ExecutionContext | None,
    ) -> AsyncIterator[StreamDelta]:
        raise NotImplementedError("generation-only local fake")
        yield  # pragma: no cover

    async def _embed_internal(
        self,
        texts: list[str],
        context: ExecutionContext | None,
    ) -> EmbeddingResult:
        raise NotImplementedError("generation-only local fake")


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["error", "cancel"])
async def test_worker_failure_settles_once(action: str) -> None:
    budget = BudgetManager().create_run_budget("ws", "run")
    provider = ScriptedFakeProvider(action)
    task = asyncio.create_task(
        provider.generate([], context=ExecutionContext(run_id="run", node_id="call", budget=budget))
    )
    await provider.started.wait()
    if action == "cancel":
        task.cancel()
    with pytest.raises((RuntimeError, asyncio.CancelledError)):
        await task
    status = budget.get_status()
    assert status.total_reserved_usd == 0
    assert status.total_spent_usd > 0
    assert status.provider_calls_count == 1


@pytest.mark.asyncio
async def test_async_agents_children_and_retries_share_tokens() -> None:
    budget = BudgetManager().create_run_budget("ws", "root", config=BudgetConfig(max_tokens=768))
    children = [budget.create_child_budget(f"child-{i}") for i in range(10)]
    providers = [ScriptedFakeProvider() for _ in children]
    agents = [Agent(name=f"agent-{i}", instructions="x", model=p) for i, p in enumerate(providers)]

    async def worker(i: int) -> bool:
        try:
            await Runner(budget=children[i]).run_async(agents[i], "")
            # Explicit application retry uses the same parent allowance.
            children[i].record_retry(1)
            await Runner(budget=children[i]).run_async(agents[i], "")
            return True
        except BudgetExceededError:
            providers[i].started.set()
            return False

    tasks = [asyncio.create_task(worker(i)) for i in range(len(agents))]
    # All admissions run before any response, exposing reservation races.
    await asyncio.wait_for(asyncio.gather(*(p.started.wait() for p in providers)), timeout=5)
    try:
        assert sum(p.calls for p in providers) == 3
        assert budget.get_status().remaining_tokens == 0
    finally:
        for provider in providers:
            provider.finish.set()
    assert not any(await asyncio.gather(*tasks))
    status = budget.get_status()
    assert status.total_tokens == 768
    assert status.total_reserved_usd == 0


def test_pre_dispatch_release_is_idempotent() -> None:
    budget = BudgetManager().create_run_budget("ws", "run")
    reservation = budget.admit_provider_call("gpt-4o", estimated_tokens=10)
    for _ in range(2):
        cost = budget.settle_provider_call(reservation, is_cancelled=True, is_pre_dispatch=True)
        assert cost.amount_usd == 0
    assert budget.get_status().effective_cost_usd == 0
    assert budget.get_status().provider_calls_count == 0


def test_grandchild_shares_root_allowance() -> None:
    root = BudgetManager().create_run_budget("ws", "root", config=BudgetConfig(max_tokens=10))
    grandchild = root.create_child_budget("child").create_child_budget("grandchild")
    grandchild.admit_provider_call("gpt-4o", estimated_tokens=10)
    with pytest.raises(BudgetExceededError):
        root.admit_provider_call("gpt-4o", estimated_tokens=1)
    assert root.get_status().remaining_tokens == 0


def test_process_death_keeps_durable_commitment(tmp_path: Path) -> None:
    path = str(tmp_path / "crashed.db")
    process = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import os, sys
from decimal import Decimal
from nexura_agents.usage.ledger import UsageLedger
UsageLedger(sys.argv[1]).reserve(
    workspace_id='ws', run_id='run', estimated_tokens=10,
    estimated_cost_usd=Decimal('1'),
)
os._exit(17)  # no Python cleanup or settlement
""",
            path,
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert process.returncode == 17, process.stderr
    ledger = UsageLedger(path)
    assert ledger.get_status("ws", "run").total_reserved_usd == 1
    reservation = ledger.get_entries("ws", "run")[0].reservation_id
    assert reservation is not None
    ledger.reconcile(reservation, is_cancelled=True)
    status = UsageLedger(path).get_status("ws", "run")
    assert status.total_reserved_usd == 0
    assert status.total_spent_usd == 1
    assert status.provider_calls_count == 1


def test_independent_processes_share_sqlite_admission(tmp_path: Path) -> None:
    path = str(tmp_path / "workers.db")
    script = """
import sys
from decimal import Decimal
from nexura_agents.usage.ledger import UsageLedger
from nexura_agents.usage.models import BudgetConfig
from nexura_agents.usage.errors import BudgetExceededError
ledger = UsageLedger(sys.argv[1])
print('ready', flush=True)
sys.stdin.readline()
admitted = 0
for _ in range(10):
    try:
        ledger.reserve(workspace_id='ws', run_id='run', estimated_cost_usd=Decimal('1'),
                       config=BudgetConfig(max_cost_usd=Decimal('3')))
        admitted += 1
    except BudgetExceededError:
        pass
print(admitted, flush=True)
"""
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", script, path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    try:
        for process in processes:
            assert process.stdout is not None
            assert process.stdout.readline().strip() == "ready"
        for process in processes:
            assert process.stdin is not None
            process.stdin.write("go\n")
            process.stdin.flush()
        counts = []
        for process in processes:
            out, err = process.communicate(timeout=10)
            assert process.returncode == 0, err
            counts.append(int(out.strip()))
        assert sum(counts) == 3
        assert UsageLedger(path).get_status("ws", "run").total_reserved_usd == 3
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.communicate()


def test_failed_settlement_transaction_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = str(tmp_path / "atomic.db")
    ledger = UsageLedger(path)
    reservation = ledger.reserve(workspace_id="ws", run_id="run", estimated_cost_usd=Decimal("1"))
    original = ledger._save_entry_to_sqlite

    def fail_write(entry: object) -> None:
        raise OSError("scripted disk write failure")

    monkeypatch.setattr(ledger, "_save_entry_to_sqlite", fail_write)
    with pytest.raises(OSError, match="scripted disk"):
        ledger.reconcile(reservation.reservation_id, is_cancelled=True)
    monkeypatch.setattr(ledger, "_save_entry_to_sqlite", original)
    assert ledger.get_status("ws", "run").total_reserved_usd == 1
    assert UsageLedger(path).get_status("ws", "run").total_spent_usd == 0
    ledger.reconcile(reservation.reservation_id, is_cancelled=True)
    assert UsageLedger(path).get_status("ws", "run").provider_calls_count == 1


@pytest.mark.asyncio
async def test_agent_as_tool_explicitly_shares_allowance() -> None:
    budget = BudgetManager().create_run_budget("ws", "root", config=BudgetConfig(max_tokens=512))
    usage = Usage(prompt_tokens=128, completion_tokens=128, total_tokens=256)
    child_provider = ScriptedFakeProvider()
    child_provider.finish.set()
    child = Agent(name="child", instructions="x", model=child_provider)
    parent_provider = ScriptedFakeProvider(
        fake=FakeModel(
            responses=[[ToolCall(id="1", name="delegate", arguments={"request": "x"})], "done"],
            default_usage=usage,
        )
    )
    parent_provider.finish.set()
    parent = Agent(
        name="parent",
        instructions="x",
        model=parent_provider,
        tools=[
            child.as_tool(
                name="delegate", runner=Runner(budget=budget.create_child_budget("child"))
            ),
        ],
    )
    with pytest.raises(BudgetExceededError):
        await Runner(budget=budget).run_async(parent, "x")
    assert parent_provider.calls == child_provider.calls == 1
    assert budget.get_status().total_tokens == 512
    assert budget.get_status().total_reserved_tokens == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_unknown_price_cannot_bypass_ancestor_policy(asynchronous: bool) -> None:
    root = BudgetManager().create_run_budget(
        "ws",
        "root",
        config=BudgetConfig(max_cost_usd=Decimal("1")),
    )
    child = root.create_child_budget("child").create_child_budget(
        "grandchild",
        config=BudgetConfig(deny_unknown_pricing=False),
    )
    with pytest.raises(InvalidPricingError):
        if asynchronous:
            await child.admit_provider_call_async("unpriced")
        else:
            child.admit_provider_call("unpriced")
    assert root.get_status().total_reserved_usd == 0


def test_fractional_reservation_rounds_up_before_admission() -> None:
    ledger = UsageLedger()
    config = BudgetConfig(max_cost_usd=Decimal("0.00000001"))
    ledger.reserve(
        workspace_id="ws", run_id="run", config=config, estimated_cost_usd=Decimal("0.000000004")
    )
    with pytest.raises(BudgetExceededError):
        ledger.reserve(
            workspace_id="ws",
            run_id="run",
            config=config,
            estimated_cost_usd=Decimal("0.000000004"),
        )
    assert ledger.get_status("ws", "run").total_reserved_usd == Decimal("0.00000001")
