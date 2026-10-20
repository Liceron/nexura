# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Data structures and domain models for usage tracking and budget governance."""

from __future__ import annotations

import time
import uuid
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field

from nexura_agents.types import Usage

UsageState = Literal["reported", "estimated", "unknown"]
ReservationStatus = Literal["active", "committed", "released", "expired"]
LedgerEntryType = Literal["reservation", "reconcile", "commit", "release", "adjustment"]


class TokenUsage(BaseModel):
    """Token metrics tracking whether values were reported by provider or estimated.

    Never synthesizes zero when provider values are omitted or unknown.
    """

    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    cached_prompt_tokens: int | None = Field(default=None, ge=0)
    cache_creation_input_tokens: int | None = Field(default=None, ge=0)
    cache_read_input_tokens: int | None = Field(default=None, ge=0)
    state: UsageState = "reported"
    is_complete: bool = True

    @property
    def is_reported(self) -> bool:
        """True if any token metric was reported by the provider."""
        return (
            self.prompt_tokens is not None
            or self.completion_tokens is not None
            or self.total_tokens is not None
            or self.cached_prompt_tokens is not None
            or self.cache_creation_input_tokens is not None
            or self.cache_read_input_tokens is not None
        )

    @classmethod
    def from_sdk_usage(
        cls,
        usage: Usage | None,
        *,
        default_state: UsageState = "reported",
    ) -> TokenUsage:
        """Convert standard SDK Usage object into TokenUsage."""
        if usage is None:
            return cls(
                prompt_tokens=None,
                completion_tokens=None,
                total_tokens=None,
                cached_prompt_tokens=None,
                cache_creation_input_tokens=None,
                cache_read_input_tokens=None,
                state="unknown",
                is_complete=False,
            )
        state: UsageState = default_state
        if not usage.is_reported:
            state = "unknown"
        elif not usage.is_complete:
            state = "estimated"
        return cls(
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            total_tokens=usage.total_tokens,
            cached_prompt_tokens=usage.cached_prompt_tokens,
            cache_creation_input_tokens=usage.cache_creation_input_tokens,
            cache_read_input_tokens=usage.cache_read_input_tokens,
            state=state,
            is_complete=usage.is_complete,
        )

    def to_sdk_usage(self) -> Usage:
        """Convert TokenUsage to standard SDK Usage object."""
        return Usage(
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
            total_tokens=self.total_tokens,
            is_complete=self.is_complete,
            cached_prompt_tokens=self.cached_prompt_tokens,
            cache_creation_input_tokens=self.cache_creation_input_tokens,
            cache_read_input_tokens=self.cache_read_input_tokens,
        )

    def add(self, other: TokenUsage | None) -> TokenUsage:
        """Accumulate token counts across steps without fabricating zeros."""
        if other is None:
            return TokenUsage(
                prompt_tokens=self.prompt_tokens,
                completion_tokens=self.completion_tokens,
                total_tokens=self.total_tokens,
                cached_prompt_tokens=self.cached_prompt_tokens,
                cache_creation_input_tokens=self.cache_creation_input_tokens,
                cache_read_input_tokens=self.cache_read_input_tokens,
                state=self.state if self.state != "reported" else "estimated",
                is_complete=False,
            )

        def _sum_opt(a: int | None, b: int | None) -> int | None:
            if a is None and b is None:
                return None
            return (a or 0) + (b or 0)

        new_prompt = _sum_opt(self.prompt_tokens, other.prompt_tokens)
        new_completion = _sum_opt(self.completion_tokens, other.completion_tokens)
        new_total = _sum_opt(self.total_tokens, other.total_tokens)
        new_cached = _sum_opt(self.cached_prompt_tokens, other.cached_prompt_tokens)
        new_cache_creation = _sum_opt(
            self.cache_creation_input_tokens, other.cache_creation_input_tokens
        )
        new_cache_read = _sum_opt(self.cache_read_input_tokens, other.cache_read_input_tokens)

        # State determination
        if self.state == "unknown" and other.state == "unknown":
            new_state: UsageState = "unknown"
        elif self.state in ("unknown", "estimated") or other.state in ("unknown", "estimated"):
            new_state = "estimated"
        else:
            new_state = "reported"

        has_prompt_or_total = (
            self.prompt_tokens is not None
            or other.prompt_tokens is not None
            or (
                self.prompt_tokens is None
                and other.prompt_tokens is None
                and self.total_tokens is not None
            )
        )
        new_is_complete = self.is_complete and other.is_complete and has_prompt_or_total

        return TokenUsage(
            prompt_tokens=new_prompt,
            completion_tokens=new_completion,
            total_tokens=new_total,
            cached_prompt_tokens=new_cached,
            cache_creation_input_tokens=new_cache_creation,
            cache_read_input_tokens=new_cache_read,
            state=new_state,
            is_complete=new_is_complete,
        )


class ModelPricing(BaseModel):
    """Model token pricing configured with source and date.

    Prices are expressed in Decimal to avoid floating-point rounding errors.
    Rate units: cost per 1,000,000 tokens (industry standard).
    """

    model_id: str
    input_cost_per_million: Decimal
    output_cost_per_million: Decimal
    cached_prompt_cost_per_million: Decimal | None = None
    cache_write_cost_per_million: Decimal | None = None
    cache_storage_cost_per_million_per_hour: Decimal | None = None
    per_request_cost: Decimal = Decimal("0")
    currency: str = "USD"
    effective_date: str | None = None
    source: str = "operator_config"


class FXRate(BaseModel):
    """Dated foreign exchange conversion rate supplied by operator.

    Conversions using this rate are always flagged as estimates.
    """

    from_currency: str = "USD"
    to_currency: str = "INR"
    rate: Decimal
    date: str
    source: str = "operator_config"

    def convert(self, amount_usd: Decimal) -> Decimal:
        """Convert USD amount to target currency with standard round-half-up to 2 decimals."""
        converted = (amount_usd * self.rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return converted


class CostEstimate(BaseModel):
    """Computed cost breakdown with explicit certainty status."""

    amount_usd: Decimal
    currency: str = "USD"
    state: UsageState = "reported"
    is_estimate: bool = False
    inr_estimate: Decimal | None = None
    fx_rate_used: Decimal | None = None
    fx_date: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class Reservation(BaseModel):
    """Pre-dispatch resource reservation against a budget."""

    reservation_id: str = Field(default_factory=lambda: f"res_{uuid.uuid4().hex[:12]}")
    workspace_id: str
    run_id: str
    parent_run_id: str | None = None
    node_id: str | None = None
    model_id: str | None = None
    estimated_tokens: int = 0
    estimated_cost_usd: Decimal = Decimal("0")
    status: ReservationStatus = "active"
    created_at: float = Field(default_factory=time.time)
    timeout_seconds: float = 300.0
    provider_call_id: str | None = None
    pricing_version: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_expired(self) -> bool:
        """Check if active reservation exceeded its timeout."""
        return self.status == "active" and (time.time() - self.created_at > self.timeout_seconds)


class LedgerEntry(BaseModel):
    """Immutable ledger record capturing financial and invocation events."""

    entry_id: str = Field(default_factory=lambda: f"ent_{uuid.uuid4().hex[:12]}")
    timestamp: float = Field(default_factory=time.time)
    workspace_id: str
    run_id: str
    parent_run_id: str | None = None
    node_id: str | None = None
    entry_type: LedgerEntryType
    entry_category: str = "provider_call"
    amount_usd: Decimal
    tokens: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    usage_state: UsageState = "reported"
    model_id: str | None = None
    turns: int = 0
    tool_calls: int = 0
    reservation_id: str | None = None
    provider_call_id: str | None = None
    pricing_version: str | None = None
    description: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class BudgetConfig(BaseModel):
    """Budget limits and safety bounds for workspaces and runs."""

    max_cost_usd: Decimal | None = None
    max_tokens: int | None = None
    max_turns: int | None = None
    max_tool_calls: int | None = None
    soft_limit_percent: Decimal = Decimal("0.80")
    conservative_unreported_tokens: int = 1000
    conservative_unreported_cost_usd: Decimal = Decimal("0.05")
    deny_unknown_pricing: bool = True
    pricing_version: str | None = None


class BudgetStatus(BaseModel):
    """Current expenditure, reserved resources, and remaining quotas."""

    workspace_id: str
    run_id: str | None = None
    total_spent_usd: Decimal
    total_reserved_usd: Decimal
    effective_cost_usd: Decimal
    committed_cost_usd: Decimal = Decimal("0")
    reported_cost_usd: Decimal = Decimal("0")
    estimated_cost_usd: Decimal = Decimal("0")
    uncertain_cost_usd: Decimal = Decimal("0")
    total_tokens: int
    total_reserved_tokens: int = 0
    total_turns: int
    total_tool_calls: int
    provider_calls_count: int = 0
    pricing_version: str | None = None
    remaining_cost_usd: Decimal | None = None
    remaining_tokens: int | None = None
    remaining_turns: int | None = None
    remaining_tool_calls: int | None = None
    is_exhausted: bool = False
    soft_limit_reached: bool = False
    unknown_usage_count: int = 0
