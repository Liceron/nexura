# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Permanent P32b review regressions; all providers are local fakes."""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
from nexura_agents import FakeModel, Usage
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
from nexura_agents.usage.models import ModelPricing
from nexura_agents.usage.pricing import PricingTable


class LocalFakeProvider(BaseProvider):
    """Explicitly labelled LOCAL FAKE provider behind the real admission boundary."""

    def __init__(self, model_id: str = "gpt-4o", action: str = "ok") -> None:
        super().__init__(
            model_id=model_id,
            capabilities=ProviderCapabilities(can_generate=True, can_tools=True),
        )
        self.action = action
        self.calls = 0
        self.fake = FakeModel(
            responses=["ok"] * 1000,
            default_usage=Usage(prompt_tokens=10, completion_tokens=10, total_tokens=20),
        )

    def _estimate_tokens(self, req: GenerationRequest) -> int:
        return 100

    async def _generate_internal(
        self, request: GenerationRequest, context: ExecutionContext | None
    ) -> ModelResponse:
        self.calls += 1
        if self.action == "error":
            raise RuntimeError("scripted local failure after dispatch")
        return await self.fake.generate(request.messages)

    async def _stream_internal(
        self, request: GenerationRequest, context: ExecutionContext | None
    ) -> AsyncIterator[StreamDelta]:
        raise NotImplementedError
        yield  # pragma: no cover

    async def _embed_internal(
        self, texts: list[str], context: ExecutionContext | None
    ) -> EmbeddingResult:
        raise NotImplementedError


def test_child_reports_exhausted_when_root_cap_is_consumed() -> None:
    root = BudgetManager().create_run_budget("ws", "root", config=BudgetConfig(max_tokens=100))
    child = root.create_child_budget("eval-child")
    child.record_usage(
        Usage(prompt_tokens=250, completion_tokens=250, total_tokens=500),
        model_id="gpt-4o",
        entry_category="direct",
    )
    assert root.is_exhausted()  # sanity: the shared allowance is gone
    assert child.is_exhausted()
    assert child.remaining_tokens() == 0


def test_retrieving_root_does_not_drop_child_ancestor_cap() -> None:
    manager = BudgetManager()
    root = manager.create_run_budget("ws", "root", config=BudgetConfig(max_tokens=100))
    child = root.create_child_budget("child")
    manager.create_run_budget("ws", "root")  # documented as "Create or retrieve"
    admitted = 0
    for _ in range(10):
        try:
            child.admit_provider_call("gpt-4o", estimated_tokens=100)
            admitted += 1
        except BudgetExceededError:
            break
    assert admitted <= 1, f"{admitted} x 100 tokens admitted under a 100-token root cap"


def test_threads_with_own_event_loops_share_ledger() -> None:
    ledger = UsageLedger()
    errors: list[str] = []
    finished: list[int] = []

    def worker(i: int) -> None:
        async def main() -> None:
            for _ in range(2000):
                await ledger.reserve_async(
                    workspace_id="ws", run_id=f"r{i}", estimated_cost_usd=Decimal("0")
                )

        try:
            asyncio.run(main())
            finished.append(i)
        except Exception as exc:  # noqa: BLE001 - collected for the assertion
            errors.append(repr(exc))

    threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    hung = [t.name for t in threads if t.is_alive()]
    assert not errors, errors[:2]
    assert not hung, f"threads hung: {hung}"
    assert sorted(finished) == [0, 1, 2, 3]


def test_grandchild_direct_usage_counts_against_root() -> None:
    root = BudgetManager().create_run_budget("ws", "root", config=BudgetConfig(max_tokens=100))
    grandchild = root.create_child_budget("child").create_child_budget("grandchild")
    grandchild.record_usage(
        Usage(prompt_tokens=500, completion_tokens=500, total_tokens=1000),
        model_id="gpt-4o",
        entry_category="direct",
    )
    assert root.get_status().total_tokens == 1000
    with pytest.raises(BudgetExceededError):
        root.admit_provider_call("gpt-4o", estimated_tokens=1)


def test_public_reserve_respects_unknown_pricing_refusal() -> None:
    root = BudgetManager().create_run_budget(
        "ws", "root", config=BudgetConfig(max_cost_usd=Decimal("1"))
    )
    with pytest.raises(InvalidPricingError):
        root.reserve(100, model_id="unpriced")


def test_child_cannot_zero_ancestor_unknown_price_charge() -> None:
    root = BudgetManager().create_run_budget(
        "ws",
        "root",
        config=BudgetConfig(max_cost_usd=Decimal("0.10"), deny_unknown_pricing=False),
    )
    child = root.create_child_budget(
        "child",
        config=BudgetConfig(
            deny_unknown_pricing=False, conservative_unreported_cost_usd=Decimal("0")
        ),
    )
    admitted = 0
    for _ in range(100):
        try:
            child.admit_provider_call("unpriced", estimated_tokens=100)
            admitted += 1
        except BudgetExceededError:
            break
    # Root's own fallback is $0.05/call, so at most 2 unpriced calls fit $0.10.
    assert admitted <= 2, f"{admitted} unpriced calls admitted against a $0.10 root cap"


def test_settlement_defaults_to_reserved_model_pricing() -> None:
    budget = BudgetManager().create_run_budget(
        "ws", "run", config=BudgetConfig(max_cost_usd=Decimal("5")), default_model="gpt-4o-mini"
    )
    res = budget.admit_provider_call("claude-3-5-sonnet", estimated_tokens=1000)
    cost = budget.settle_provider_call(
        res,
        usage=Usage(prompt_tokens=1_000_000, completion_tokens=1_000_000, total_tokens=2_000_000),
    )
    assert cost.amount_usd == Decimal("18.00000000"), cost.amount_usd
    assert budget.get_status().is_exhausted


def test_tiny_settlements_do_not_round_cap_away() -> None:
    table = PricingTable(
        version="local-test",
        date="2026-10-18",
        models={
            "tiny": ModelPricing(
                model_id="tiny",
                input_cost_per_million=Decimal("0.004"),
                output_cost_per_million=Decimal("0.004"),
            )
        },
    )
    cap = Decimal("0.00000005")
    budget = BudgetManager(pricing_table=table).create_run_budget(
        "ws", "run", config=BudgetConfig(max_cost_usd=cap), default_model="tiny"
    )
    real_cost_per_call = Decimal("0.004") / Decimal(1_000_000)
    admitted = 0
    for _ in range(100):
        try:
            res = budget.admit_provider_call("tiny", estimated_tokens=1)
        except BudgetExceededError:
            break
        budget.settle_provider_call(
            res, model_id="tiny", usage=Usage(prompt_tokens=1, completion_tokens=0, total_tokens=1)
        )
        admitted += 1
    assert admitted * real_cost_per_call <= cap, (
        f"{admitted} calls admitted; real spend {admitted * real_cost_per_call} > cap {cap}; "
        f"ledger spent={budget.get_status().total_spent_usd}"
    )


def test_pre_dispatch_release_of_unpriced_model_succeeds() -> None:
    budget = BudgetManager().create_run_budget("ws", "run", config=BudgetConfig(max_tokens=2000))
    reservation = budget.admit_provider_call("local-llama", estimated_tokens=1000)
    # Regression vs HEAD: this used to release cleanly.
    cost = budget.settle_provider_call(
        reservation, model_id="local-llama", is_cancelled=True, is_pre_dispatch=True
    )
    assert cost.amount_usd == 0
    assert budget.get_status().total_reserved_tokens == 0


def test_in_flight_cancellation_of_unpriced_model_settles_conservatively() -> None:
    budget = BudgetManager().create_run_budget("ws", "run", config=BudgetConfig(max_tokens=2000))
    reservation = budget.admit_provider_call("local-llama", estimated_tokens=1000)
    budget.settle_provider_call(reservation, model_id="local-llama", is_cancelled=True)
    status = budget.get_status()
    assert status.total_reserved_tokens == 0
    assert status.total_tokens >= 1000


@pytest.mark.asyncio
async def test_generate_with_unpriced_model_does_not_leak_reservation() -> None:
    budget = BudgetManager().create_run_budget("ws", "run", config=BudgetConfig(max_tokens=250))
    provider = LocalFakeProvider(model_id="local-llama")  # local fake, no transport
    ctx = ExecutionContext(run_id="run", node_id="n", budget=budget)
    for _ in range(2):
        with contextlib.suppress(Exception):
            await provider.generate([], context=ctx)
    status = budget.get_status()
    # Two dispatched calls; neither may remain an unresolved 'active' reservation.
    assert provider.calls == 2
    assert status.total_reserved_tokens == 0, (
        f"leaked reservations hold {status.total_reserved_tokens} tokens forever"
    )


def test_negative_reported_usage_does_not_refund() -> None:
    budget = BudgetManager().create_run_budget(
        "ws", "run", config=BudgetConfig(max_tokens=1000, max_cost_usd=Decimal("1"))
    )
    res = budget.admit_provider_call("gpt-4o", estimated_tokens=10)
    budget.settle_provider_call(
        res, usage=Usage(prompt_tokens=-10_000_000, completion_tokens=0, total_tokens=-10_000_000)
    )
    status = budget.get_status()
    assert status.total_tokens >= 0
    assert status.total_spent_usd >= 0
    assert status.remaining_tokens is not None and status.remaining_tokens <= 1000


def test_inconsistent_total_does_not_undercount_tokens() -> None:
    budget = BudgetManager().create_run_budget("ws", "run", config=BudgetConfig(max_tokens=1000))
    res = budget.admit_provider_call("gpt-4o", estimated_tokens=1000)
    budget.settle_provider_call(
        res, usage=Usage(prompt_tokens=500, completion_tokens=500, total_tokens=0)
    )
    assert budget.get_status().total_tokens >= 1000


def test_create_and_retrieve_retains_original_policy_even_with_conflicting_config() -> None:
    manager = BudgetManager()
    config = BudgetConfig(max_tokens=100)
    root = manager.create_run_budget("ws", "root", config=config, default_model="gpt-4o-mini")
    assert manager.create_run_budget("ws", "root") is root
    assert manager.create_run_budget("ws", "root", config=BudgetConfig(max_tokens=999)) is root
    assert root.config == config
    assert root.default_model == "gpt-4o-mini"
    with pytest.raises(ValueError, match="workspace or parent"):
        manager.create_run_budget("other", "root")


@pytest.mark.asyncio
async def test_async_reserve_and_usage_module_reject_unknown_pricing() -> None:
    from nexura_agents.usage.module import UsageBudgetModule

    manager = BudgetManager()
    root = manager.create_run_budget("ws", "root", config=BudgetConfig(max_cost_usd=Decimal("1")))
    with pytest.raises(InvalidPricingError):
        await root.reserve_async(100, model_id="unpriced")
    result = await UsageBudgetModule(manager).invoke(
        ExecutionContext(workspace_id="ws", run_id="root", node_id="usage"),
        {"operation": "reserve", "model_id": "unpriced", "estimated_tokens": 100},
        {},
    )
    assert result.status == "failure"
    assert result.error_code == "USAGE_ERROR"
    assert root.get_status().total_reserved_tokens == 0


@pytest.mark.asyncio
async def test_unpriced_provider_cancellation_preserves_cancelled_error() -> None:
    class BlockingLocalFakeProvider(LocalFakeProvider):
        async def _generate_internal(
            self, request: GenerationRequest, context: ExecutionContext | None
        ) -> ModelResponse:
            started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    started = asyncio.Event()
    budget = BudgetManager().create_run_budget("ws", "run", config=BudgetConfig(max_tokens=250))
    provider = BlockingLocalFakeProvider(model_id="unpriced")
    task = asyncio.create_task(
        provider.generate(
            [],
            context=ExecutionContext(workspace_id="ws", run_id="run", node_id="n", budget=budget),
        )
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    status = budget.get_status()
    assert status.total_reserved_tokens == 0
    assert status.total_tokens == 100
    assert status.total_spent_usd >= Decimal("0.05")


@pytest.mark.parametrize(
    "field",
    [
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "cached_prompt_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    ],
)
def test_token_usage_rejects_negative_counts(field: str) -> None:
    from nexura_agents.usage.models import TokenUsage

    with pytest.raises(ValueError):
        TokenUsage.model_validate({field: -1})


@pytest.mark.parametrize("total", [0, None])
def test_zero_report_retains_reservation(total: int | None) -> None:
    budget = BudgetManager().create_run_budget("ws", "run", config=BudgetConfig(max_tokens=100))
    reservation = budget.reserve(100)
    budget.reconcile(
        reservation.reservation_id,
        usage=Usage(prompt_tokens=0, completion_tokens=0, total_tokens=total),
    )
    status = budget.get_status()
    assert status.total_tokens >= 100
    assert status.total_spent_usd >= reservation.estimated_cost_usd


def test_workspace_and_deep_ancestor_status_limits_are_visible() -> None:
    manager = BudgetManager()
    manager.set_workspace_budget("ws", BudgetConfig(max_tokens=50))
    root = manager.create_run_budget("ws", "root", config=BudgetConfig(max_tokens=100))
    child = root.create_child_budget("child").create_child_budget("grandchild")
    assert child.remaining_tokens() == 50
    child.record_usage(
        Usage(prompt_tokens=50, completion_tokens=0, total_tokens=50), entry_category="direct"
    )
    assert child.is_exhausted()
    assert child.remaining_tokens() == 0
