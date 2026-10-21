# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Hierarchical budget manager governing workspace, run, and child agent quotas."""

from __future__ import annotations

import json
from decimal import ROUND_CEILING, Decimal
from threading import Lock
from typing import Any

from nexura_agents.contracts.context import BudgetInterface
from nexura_agents.types import Usage
from nexura_agents.usage.errors import BudgetExceededError, InvalidPricingError
from nexura_agents.usage.ledger import UsageLedger
from nexura_agents.usage.models import (
    BudgetConfig,
    BudgetStatus,
    CostEstimate,
    LedgerEntry,
    Reservation,
    TokenUsage,
)
from nexura_agents.usage.pricing import (
    CurrencyConverter,
    PricingTable,
    create_operator_standard_pricing_table,
)

_ZERO = Decimal("0")


class RunBudget(BudgetInterface):
    """Execution run budget enforcing token, cost, turn, and tool limits.

    Implements the SDK's BudgetInterface protocol while maintaining hierarchical
    linkage to parent agent runs and workspace envelopes.
    """

    def __init__(
        self,
        manager: BudgetManager,
        *,
        workspace_id: str,
        run_id: str,
        parent_run_id: str | None = None,
        config: BudgetConfig | None = None,
        default_model: str = "gpt-4o",
    ) -> None:
        self.manager = manager
        self.workspace_id = workspace_id
        self.run_id = run_id
        self.parent_run_id = parent_run_id
        self.config = config or BudgetConfig()
        self.default_model = default_model
        self._current_turns: int = 0
        self._current_tool_calls: int = 0
        self._retry_count: int = 0

    @property
    def parent_config(self) -> BudgetConfig | None:
        if self.parent_run_id:
            parent_run = self.manager.get_run_budget(self.parent_run_id)
            if parent_run:
                return parent_run.config
        return None

    @property
    def workspace_config(self) -> BudgetConfig | None:
        return self.manager.get_workspace_config(self.workspace_id)

    def create_child_budget(
        self,
        child_run_id: str,
        config: BudgetConfig | None = None,
        default_model: str | None = None,
    ) -> RunBudget:
        """Spawn a child run budget inheriting workspace and parent limits."""
        return self.manager.create_run_budget(
            workspace_id=self.workspace_id,
            run_id=child_run_id,
            parent_run_id=self.run_id,
            config=config,
            default_model=default_model or self.default_model,
        )

    @property
    def ancestor_configs(self) -> dict[str, BudgetConfig]:
        ancestors: dict[str, BudgetConfig] = {}
        parent_id = self.parent_run_id
        while parent_id and parent_id not in ancestors:
            parent = self.manager.get_run_budget(parent_id)
            if parent is None:
                raise ValueError(f"Missing ancestor budget: {parent_id}")
            if parent.workspace_id != self.workspace_id:
                raise ValueError("Budget ancestors must share a workspace")
            ancestors[parent_id] = parent.config
            parent_id = parent.parent_run_id
        return ancestors

    # --- Turn & Tool call limits enforced independently of tokens ---

    def record_turn(self) -> int:
        """Increment turns and verify turn limit independently of token counts."""
        if self.config.max_turns is not None and self._current_turns >= self.config.max_turns:
            raise BudgetExceededError(
                "turns",
                self._current_turns + 1,
                self.config.max_turns,
                workspace_id=self.workspace_id,
                run_id=self.run_id,
                message=f"Turn limit exceeded for run '{self.run_id}': max={self.config.max_turns}",
            )
        self._current_turns += 1
        return self._current_turns

    def record_tool_call(self, tool_name: str | None = None) -> int:
        """Increment tool calls and verify limit independently of token counts."""
        if (
            self.config.max_tool_calls is not None
            and self._current_tool_calls >= self.config.max_tool_calls
        ):
            tool_suffix = f" on tool '{tool_name}'" if tool_name else ""
            msg = (
                f"Tool call limit exceeded for run '{self.run_id}': "
                f"max={self.config.max_tool_calls}{tool_suffix}"
            )
            raise BudgetExceededError(
                "tool_calls",
                self._current_tool_calls + 1,
                self.config.max_tool_calls,
                workspace_id=self.workspace_id,
                run_id=self.run_id,
                message=msg,
            )
        self._current_tool_calls += 1
        return self._current_tool_calls

    def record_retry(self, attempt_num: int, reason: str = "") -> None:
        """Record retry attempt accounting against run execution limits."""
        self._retry_count += 1
        # Retries also count as execution turn
        self.record_turn()

    # --- Pre-dispatch Reservation & Post-response Reconciliation ---

    def reserve(
        self,
        estimated_tokens: int,
        *,
        model_id: str | None = None,
        node_id: str | None = None,
        timeout_seconds: float = 300.0,
        metadata: dict[str, Any] | None = None,
    ) -> Reservation:
        """Pre-dispatch synchronous reservation against hierarchical bounds."""
        target_model = model_id or self.default_model
        self._check_pricing_policy(target_model)
        pricing = self.manager.pricing_table.get_pricing(target_model)
        if pricing is not None:
            # Estimate cost assuming half input, half output for reservation buffer
            half_tokens = estimated_tokens // 2
            est_cost = (
                (Decimal(half_tokens) * pricing.input_cost_per_million)
                + (Decimal(estimated_tokens - half_tokens) * pricing.output_cost_per_million)
            ) / Decimal("1000000") + pricing.per_request_cost
        else:
            est_cost = self._fallback_cost()

        return self.manager.ledger.reserve(
            workspace_id=self.workspace_id,
            run_id=self.run_id,
            parent_run_id=self.parent_run_id,
            node_id=node_id,
            model_id=target_model,
            estimated_tokens=estimated_tokens,
            estimated_cost_usd=est_cost,
            timeout_seconds=timeout_seconds,
            config=self.config,
            parent_config=self.parent_config,
            ancestor_configs=self.ancestor_configs,
            workspace_config=self.workspace_config,
            metadata=metadata,
        )

    async def reserve_async(
        self,
        estimated_tokens: int,
        *,
        model_id: str | None = None,
        node_id: str | None = None,
        timeout_seconds: float = 300.0,
        metadata: dict[str, Any] | None = None,
    ) -> Reservation:
        """Pre-dispatch async reservation preventing concurrent overspend."""
        target_model = model_id or self.default_model
        self._check_pricing_policy(target_model)
        pricing = self.manager.pricing_table.get_pricing(target_model)
        if pricing is not None:
            half_tokens = estimated_tokens // 2
            est_cost = (
                (Decimal(half_tokens) * pricing.input_cost_per_million)
                + (Decimal(estimated_tokens - half_tokens) * pricing.output_cost_per_million)
            ) / Decimal("1000000") + pricing.per_request_cost
        else:
            est_cost = self._fallback_cost()

        return await self.manager.ledger.reserve_async(
            workspace_id=self.workspace_id,
            run_id=self.run_id,
            parent_run_id=self.parent_run_id,
            node_id=node_id,
            model_id=target_model,
            estimated_tokens=estimated_tokens,
            estimated_cost_usd=est_cost,
            timeout_seconds=timeout_seconds,
            config=self.config,
            parent_config=self.parent_config,
            ancestor_configs=self.ancestor_configs,
            workspace_config=self.workspace_config,
            metadata=metadata,
        )

    def reconcile(
        self,
        reservation_id: str,
        *,
        usage: Usage | TokenUsage | None = None,
        model_id: str | None = None,
        is_cancelled: bool = False,
        is_pre_dispatch: bool = False,
        description: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> CostEstimate:
        """Reconcile reservation post-turn or post-cancellation."""
        reservation = self.manager.ledger.get_reservation(reservation_id, self.workspace_id)
        target_model = reservation.model_id or model_id or self.default_model
        tok_usage: TokenUsage | None = None
        try:
            if isinstance(usage, TokenUsage):
                tok_usage = TokenUsage.model_validate(usage.model_dump())
            elif isinstance(usage, Usage):
                tok_usage = TokenUsage.from_sdk_usage(usage)
        except ValueError:
            # Reject invalid provider numbers as evidence, retaining the commitment.
            tok_usage = None

        cost_est = self._calculate_cost(target_model, tok_usage)
        if self.manager.pricing_table.get_pricing(target_model) is None:
            tok_usage = tok_usage.model_copy(update={"state": "estimated"}) if tok_usage else None
        entry = self.manager.ledger.reconcile(
            reservation_id,
            workspace_id=self.workspace_id,
            actual_cost_usd=cost_est.amount_usd,
            usage=tok_usage,
            is_cancelled=is_cancelled,
            is_pre_dispatch=is_pre_dispatch,
            conservative_fallback_cost=self._fallback_cost(),
            conservative_fallback_tokens=(
                self.config.conservative_unreported_tokens
                if self.manager.pricing_table.get_pricing(target_model) is not None
                else 0
            ),
            description=description,
            metadata=metadata,
        )
        # The persisted first settlement is authoritative, even for conflicting duplicates.
        cost_est = CostEstimate(
            amount_usd=entry.amount_usd,
            state=entry.usage_state,
            is_estimate=entry.usage_state != "reported",
            details={**cost_est.details, "settlement_entry_id": entry.entry_id},
        )

        # Attach FX conversion estimate if available
        if self.manager.fx_converter:
            cost_est = self.manager.fx_converter.attach_inr_estimate(cost_est)

        return cost_est

    def release(self, reservation_id: str, description: str = "") -> LedgerEntry:
        """Explicitly release an unused reservation."""
        return self.manager.ledger.release(
            reservation_id,
            description=description,
            workspace_id=self.workspace_id,
        )

    # --- O01 Provider-call admission & settlement boundary ---

    def _fallback_cost(self) -> Decimal:
        policies = [self.config, self.workspace_config, *self.ancestor_configs.values()]
        return max(p.conservative_unreported_cost_usd for p in policies if p is not None)

    def _calculate_cost(self, model_id: str, usage: TokenUsage | None) -> CostEstimate:
        """Price observed counts without losing positive subprecision commitments."""
        pricing = self.manager.pricing_table.get_pricing(model_id)
        if pricing is None:
            return CostEstimate(
                amount_usd=self._fallback_cost(),
                state="unknown",
                is_estimate=True,
                details={"model_id": model_id, "reason": "Unpriced model; retained commitment"},
            )
        cost = self.manager.pricing_table.calculate_cost(
            model_id, usage, conservative_fallback_cost=self._fallback_cost()
        )
        # The public pricing calculator rounds half-up. Preserve its state/details,
        # but calculate a full-precision lower bound before ledger quantization.
        exact = self._fallback_cost()
        if usage is not None and usage.is_reported:
            prompt = usage.prompt_tokens or 0
            # The prompt includes cache writes, which are priced at the write rate below.
            written = min(prompt, usage.cache_creation_input_tokens or 0)
            cached = min(
                prompt - written, usage.cached_prompt_tokens or usage.cache_read_input_tokens or 0
            )
            cached_rate = pricing.cached_prompt_cost_per_million
            if cached_rate is None:
                cached_rate = pricing.input_cost_per_million
            write_rate = pricing.cache_write_cost_per_million
            if write_rate is None:
                write_rate = pricing.input_cost_per_million
            exact = (
                Decimal(prompt - written - cached) * pricing.input_cost_per_million
                + Decimal(cached) * cached_rate
                + Decimal(usage.completion_tokens or 0) * pricing.output_cost_per_million
                + Decimal(usage.cache_creation_input_tokens or 0) * write_rate
            ) / Decimal(1_000_000) + pricing.per_request_cost
            if usage.prompt_tokens is None and usage.completion_tokens is None:
                exact = (
                    Decimal(usage.total_tokens or 0)
                    * max(pricing.input_cost_per_million, pricing.output_cost_per_million)
                    / Decimal(1_000_000)
                    + pricing.per_request_cost
                )
            elif usage.prompt_tokens is not None and usage.completion_tokens is None:
                exact += self._fallback_cost() or Decimal("0.01")
        cost.amount_usd = max(cost.amount_usd, exact).quantize(
            Decimal("0.00000001"), rounding=ROUND_CEILING
        )
        return cost

    def _check_pricing_policy(self, model_id: str) -> None:
        policies = [self.config, self.workspace_config, *self.ancestor_configs.values()]
        capped = [p for p in policies if p is not None and p.max_cost_usd is not None]
        deny = capped and (
            self.config.deny_unknown_pricing or any(p.deny_unknown_pricing for p in capped)
        )
        if self.manager.pricing_table.get_pricing(model_id) is None and deny:
            raise InvalidPricingError(
                model_id,
                f"Unknown pricing for model '{model_id}' denied admission under monetary cap",
            )

    def admit_provider_call(
        self,
        model_id: str | None = None,
        estimated_tokens: int = 1000,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Atomically reserve allowance before provider dispatch.

        Under a monetary cap, unknown model pricing denies admission immediately.
        """
        target_model = model_id or self.default_model
        self._check_pricing_policy(target_model)

        res = self.reserve(
            estimated_tokens=estimated_tokens,
            model_id=target_model,
            metadata=metadata,
        )
        return res.reservation_id

    async def admit_provider_call_async(
        self,
        model_id: str | None = None,
        estimated_tokens: int = 1000,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Async variant of admit_provider_call."""
        target_model = model_id or self.default_model
        self._check_pricing_policy(target_model)

        res = await self.reserve_async(
            estimated_tokens=estimated_tokens,
            model_id=target_model,
            metadata=metadata,
        )
        return res.reservation_id

    def settle_provider_call(
        self,
        reservation_id: str,
        *,
        usage: Usage | TokenUsage | None = None,
        model_id: str | None = None,
        is_cancelled: bool = False,
        is_pre_dispatch: bool = False,
        description: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> CostEstimate:
        """Idempotently settle a provider call post-dispatch or on cancellation."""
        return self.reconcile(
            reservation_id,
            usage=usage,
            model_id=model_id,
            is_cancelled=is_cancelled,
            is_pre_dispatch=is_pre_dispatch,
            description=description,
            metadata=metadata,
        )

    async def settle_provider_call_async(
        self,
        reservation_id: str,
        *,
        usage: Usage | TokenUsage | None = None,
        model_id: str | None = None,
        is_cancelled: bool = False,
        is_pre_dispatch: bool = False,
        description: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> CostEstimate:
        """Async variant of settle_provider_call."""
        return self.settle_provider_call(
            reservation_id,
            usage=usage,
            model_id=model_id,
            is_cancelled=is_cancelled,
            is_pre_dispatch=is_pre_dispatch,
            description=description,
            metadata=metadata,
        )

    # --- BudgetInterface protocol implementation ---

    def record_usage(
        self,
        usage: Usage | TokenUsage,
        *,
        model_id: str | None = None,
        entry_category: str = "module_aggregate",
    ) -> None:
        """Record usage from completed turn or module execution.

        The default ``module_aggregate`` category re-reports spend already settled
        through provider admission and is excluded from totals. Callers whose usage
        is not settled elsewhere must pass ``entry_category="direct"`` so it counts.
        """
        target_model = model_id or self.default_model
        tok_usage = usage if isinstance(usage, TokenUsage) else TokenUsage.from_sdk_usage(usage)
        cost_est = self._calculate_cost(target_model, tok_usage)
        self.manager.ledger.record_commit(
            workspace_id=self.workspace_id,
            run_id=self.run_id,
            parent_run_id=self.parent_run_id,
            amount_usd=cost_est.amount_usd,
            tokens=max(
                tok_usage.total_tokens or 0,
                (tok_usage.prompt_tokens or 0) + (tok_usage.completion_tokens or 0),
            ),
            prompt_tokens=tok_usage.prompt_tokens,
            completion_tokens=tok_usage.completion_tokens,
            usage_state=tok_usage.state,
            model_id=target_model,
            turns=1,
            entry_category=entry_category,
            description="Direct usage commit via BudgetInterface",
            metadata={"budget_ancestors": list(self.ancestor_configs)},
        )

    def is_exhausted(self) -> bool:
        """Check if run or workspace budget has been exhausted."""
        status = self.get_status()
        if status.is_exhausted:
            return True
        # Check workspace status
        ws_status = self.manager.get_workspace_status(self.workspace_id)
        return ws_status.is_exhausted or any(
            self.manager.ledger.get_status(self.workspace_id, run_id, config=config).is_exhausted
            for run_id, config in self.ancestor_configs.items()
        )

    def remaining_tokens(self) -> int | None:
        """Return remaining tokens if configured."""
        statuses = [self.get_status(), self.manager.get_workspace_status(self.workspace_id)]
        statuses.extend(
            self.manager.ledger.get_status(self.workspace_id, run_id, config=config)
            for run_id, config in self.ancestor_configs.items()
        )
        remaining = [s.remaining_tokens for s in statuses if s.remaining_tokens is not None]
        return min(remaining) if remaining else None

    # --- Status inspection ---

    def get_status(self) -> BudgetStatus:
        """Get live status for this run."""
        return self.manager.ledger.get_status(
            self.workspace_id,
            self.run_id,
            config=self.config,
        )

    def to_dict(self) -> dict[str, Any]:
        """Typed status and policy contract for Studio and external consumers."""
        status = self.get_status()
        return {
            "workspace_id": self.workspace_id,
            "run_id": self.run_id,
            "parent_run_id": self.parent_run_id,
            "default_model": self.default_model,
            "pricing_version": self.manager.pricing_table.version,
            "status": {
                "total_spent_usd": f"{status.total_spent_usd:f}",
                "total_reserved_usd": f"{status.total_reserved_usd:f}",
                "effective_cost_usd": f"{status.effective_cost_usd:f}",
                "committed_cost_usd": f"{status.committed_cost_usd:f}",
                "reported_cost_usd": f"{status.reported_cost_usd:f}",
                "estimated_cost_usd": f"{status.estimated_cost_usd:f}",
                "uncertain_cost_usd": f"{status.uncertain_cost_usd:f}",
                "total_tokens": status.total_tokens,
                "total_reserved_tokens": status.total_reserved_tokens,
                "total_turns": status.total_turns,
                "total_tool_calls": status.total_tool_calls,
                "provider_calls_count": status.provider_calls_count,
                "remaining_cost_usd": (
                    f"{status.remaining_cost_usd:f}"
                    if status.remaining_cost_usd is not None
                    else None
                ),
                "remaining_tokens": status.remaining_tokens,
                "remaining_turns": status.remaining_turns,
                "remaining_tool_calls": status.remaining_tool_calls,
                "is_exhausted": status.is_exhausted,
                "soft_limit_reached": status.soft_limit_reached,
                "unknown_usage_count": status.unknown_usage_count,
            },
            "config": {
                "max_cost_usd": (
                    f"{self.config.max_cost_usd:f}"
                    if self.config.max_cost_usd is not None
                    else None
                ),
                "max_tokens": self.config.max_tokens,
                "max_turns": self.config.max_turns,
                "max_tool_calls": self.config.max_tool_calls,
                "soft_limit_percent": f"{self.config.soft_limit_percent:f}",
                "deny_unknown_pricing": self.config.deny_unknown_pricing,
                "pricing_version": self.config.pricing_version,
            },
        }


class BudgetManager:
    """Orchestrates workspace and run budgets with versioned pricing and FX conversion."""

    def __init__(
        self,
        ledger: UsageLedger | None = None,
        pricing_table: PricingTable | None = None,
        fx_converter: CurrencyConverter | None = None,
    ) -> None:
        self.ledger = ledger or UsageLedger()
        self.pricing_table = pricing_table or create_operator_standard_pricing_table()
        self.fx_converter = fx_converter
        self._workspace_configs: dict[str, BudgetConfig] = {}
        self._run_budgets: dict[str, RunBudget] = {}
        self._registry_lock = Lock()

    def set_workspace_budget(self, workspace_id: str, config: BudgetConfig) -> None:
        """Configure budget envelopes for a workspace."""
        self.ledger.budget_policy("workspace", workspace_id, config.model_dump_json(), replace=True)
        self._workspace_configs[workspace_id] = config

    def get_workspace_config(self, workspace_id: str) -> BudgetConfig | None:
        """Get configured workspace budget."""
        raw = self.ledger.budget_policy("workspace", workspace_id)
        if raw is not None:
            return BudgetConfig.model_validate_json(raw)
        return self._workspace_configs.get(workspace_id)

    def create_run_budget(
        self,
        workspace_id: str,
        run_id: str,
        parent_run_id: str | None = None,
        config: BudgetConfig | None = None,
        default_model: str = "gpt-4o",
    ) -> RunBudget:
        """Create or retrieve a RunBudget; existing run policy is retained.

        Repeated creation returns the same handle, ignoring new config/model values.
        A run ID cannot be retrieved from another workspace or reparented.
        """
        with self._registry_lock:
            existing = self._run_budgets.get(run_id)
            if existing is None:
                raw = self.ledger.budget_policy(
                    "run",
                    run_id,
                    json.dumps(
                        {
                            "workspace_id": workspace_id,
                            "parent_run_id": parent_run_id,
                            "config": (config or BudgetConfig()).model_dump(mode="json"),
                            "default_model": default_model,
                        }
                    ),
                )
                if raw is not None:
                    saved = json.loads(raw)
                    existing = RunBudget(
                        self,
                        run_id=run_id,
                        **{**saved, "config": BudgetConfig.model_validate(saved["config"])},
                    )
                    self._run_budgets[run_id] = existing
            if existing is not None:
                if existing.workspace_id != workspace_id or (
                    parent_run_id is not None and existing.parent_run_id != parent_run_id
                ):
                    raise ValueError("Existing run budget has different workspace or parent")
                return existing
            budget = RunBudget(
                self,
                workspace_id=workspace_id,
                run_id=run_id,
                parent_run_id=parent_run_id,
                config=config,
                default_model=default_model,
            )
            self._run_budgets[run_id] = budget
            return budget

    def get_run_budget(self, run_id: str) -> RunBudget | None:
        """Retrieve existing run budget by run_id."""
        raw = self.ledger.budget_policy("run", run_id)
        if run_id not in self._run_budgets and raw is not None:
            saved = json.loads(raw)
            return self.create_run_budget(saved["workspace_id"], run_id)
        return self._run_budgets.get(run_id)

    def get_workspace_status(self, workspace_id: str) -> BudgetStatus:
        """Compute live workspace-wide status."""
        ws_config = self.get_workspace_config(workspace_id)
        return self.ledger.get_status(workspace_id, None, config=ws_config)
