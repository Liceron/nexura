# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Pricing tables, cost computation with Decimal arithmetic, and FX conversions."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from nexura_agents.usage.errors import CurrencyConversionError, InvalidPricingError
from nexura_agents.usage.models import CostEstimate, FXRate, ModelPricing, TokenUsage, UsageState

# Standard 8-decimal micro-precision for token accounting
_MICRO_CENT = Decimal("0.00000001")
_ONE_MILLION = Decimal("1000000")


class PricingTable:
    """Versioned operator-supplied pricing table.

    Never relies on unversioned or scraped defaults. All pricing decisions
    derive from explicit operator-configured rates with date and source metadata.
    """

    def __init__(
        self,
        *,
        version: str,
        date: str,
        source: str = "operator_config",
        models: dict[str, ModelPricing] | None = None,
    ) -> None:
        self.version = version
        self.date = date
        self.source = source
        self._models: dict[str, ModelPricing] = {}
        if models:
            for pricing in models.values():
                self.add_model_pricing(pricing)

    def add_model_pricing(self, pricing: ModelPricing) -> None:
        """Register pricing for a model."""
        self._models[pricing.model_id] = pricing

    def get_pricing(self, model_id: str) -> ModelPricing | None:
        """Get model pricing, or None if unconfigured."""
        return self._models.get(model_id)

    def calculate_cost(
        self,
        model_id: str,
        usage: TokenUsage | None,
        *,
        conservative_fallback_cost: Decimal | None = None,
    ) -> CostEstimate:
        """Compute exact cost using Decimal arithmetic.

        Guarantees that unknown or partial usage is never silently zeroed out.
        """
        pricing = self.get_pricing(model_id)
        if pricing is None:
            raise InvalidPricingError(model_id, f"No pricing model defined in table {self.version}")

        if usage is None or not usage.is_reported:
            # Completely unreported/unknown usage
            fallback = (
                conservative_fallback_cost
                if conservative_fallback_cost is not None
                else Decimal("0")
            )
            return CostEstimate(
                amount_usd=fallback.quantize(_MICRO_CENT, rounding=ROUND_HALF_UP),
                currency=pricing.currency,
                state="unknown",
                is_estimate=True,
                details={
                    "model_id": model_id,
                    "pricing_version": self.version,
                    "pricing_source": self.source,
                    "reason": "Token usage unreported by provider; conservative fallback applied",
                },
            )

        prompt_tokens = usage.prompt_tokens
        completion_tokens = usage.completion_tokens
        total_tokens = usage.total_tokens
        cached_tokens = (
            usage.cached_prompt_tokens
            if usage.cached_prompt_tokens is not None
            else (usage.cache_read_input_tokens or 0)
        )
        cache_write_tokens = usage.cache_creation_input_tokens or 0

        def _calc_input_cost(p_tokens: int) -> tuple[Decimal, int, int]:
            # prompt_tokens is the whole prompt: it includes the tokens read from the cache
            # and the tokens written to it (adapters report it that way for every provider).
            # Cache writes are priced separately at the write rate, so they are taken out of
            # the uncached remainder here instead of being charged twice.
            written = min(cache_write_tokens, p_tokens)
            if cached_tokens > 0 and pricing.cached_prompt_cost_per_million is not None:
                actual_cached = min(cached_tokens, p_tokens - written)
                uncached = max(0, p_tokens - written - actual_cached)
                c_cost = (
                    (Decimal(uncached) * pricing.input_cost_per_million)
                    + (Decimal(actual_cached) * pricing.cached_prompt_cost_per_million)
                ) / _ONE_MILLION
                return c_cost, uncached, actual_cached
            uncached = p_tokens - written
            return (Decimal(uncached) * pricing.input_cost_per_million) / _ONE_MILLION, uncached, 0

        write_cost = Decimal("0")
        if cache_write_tokens > 0:
            write_rate = (
                pricing.cache_write_cost_per_million
                if pricing.cache_write_cost_per_million is not None
                else pricing.input_cost_per_million
            )
            write_cost = (Decimal(cache_write_tokens) * write_rate) / _ONE_MILLION

        # Check if both prompt and completion are reported
        if prompt_tokens is not None and completion_tokens is not None:
            input_cost, uncached_p, cached_p = _calc_input_cost(prompt_tokens)
            output_cost = (
                Decimal(completion_tokens) * pricing.output_cost_per_million
            ) / _ONE_MILLION
            total_cost = input_cost + output_cost + pricing.per_request_cost + write_cost
            state: UsageState = usage.state

            return CostEstimate(
                amount_usd=total_cost.quantize(_MICRO_CENT, rounding=ROUND_HALF_UP),
                currency=pricing.currency,
                state=state,
                is_estimate=(state != "reported"),
                details={
                    "model_id": model_id,
                    "prompt_tokens": prompt_tokens,
                    "cached_prompt_tokens": cached_p,
                    "uncached_prompt_tokens": uncached_p,
                    "cache_creation_input_tokens": cache_write_tokens,
                    "cache_write_cost_usd": str(
                        write_cost.quantize(_MICRO_CENT, rounding=ROUND_HALF_UP)
                    ),
                    "completion_tokens": completion_tokens,
                    "input_cost_usd": str(input_cost.quantize(_MICRO_CENT, rounding=ROUND_HALF_UP)),
                    "output_cost_usd": str(
                        output_cost.quantize(_MICRO_CENT, rounding=ROUND_HALF_UP)
                    ),
                    "per_request_cost_usd": str(pricing.per_request_cost),
                    "pricing_version": self.version,
                },
            )

        # Partial usage: only prompt or only total is reported
        if prompt_tokens is not None and completion_tokens is None:
            # Prompt reported, completion unknown -> partial usage
            input_cost, uncached_p, cached_p = _calc_input_cost(prompt_tokens)
            conservative_output = conservative_fallback_cost or Decimal("0.01")
            total_cost = input_cost + conservative_output + pricing.per_request_cost + write_cost
            return CostEstimate(
                amount_usd=total_cost.quantize(_MICRO_CENT, rounding=ROUND_HALF_UP),
                currency=pricing.currency,
                state="estimated",
                is_estimate=True,
                details={
                    "model_id": model_id,
                    "prompt_tokens": prompt_tokens,
                    "cached_prompt_tokens": cached_p,
                    "uncached_prompt_tokens": uncached_p,
                    "completion_tokens": None,
                    "conservative_output_usd": str(conservative_output),
                    "pricing_version": self.version,
                },
            )

        if total_tokens is not None and prompt_tokens is None and completion_tokens is None:
            # Only total tokens reported: conservatively price using higher of input/output rate
            higher_rate = max(pricing.input_cost_per_million, pricing.output_cost_per_million)
            total_cost = (
                Decimal(total_tokens) * higher_rate
            ) / _ONE_MILLION + pricing.per_request_cost
            return CostEstimate(
                amount_usd=total_cost.quantize(_MICRO_CENT, rounding=ROUND_HALF_UP),
                currency=pricing.currency,
                state="estimated",
                is_estimate=True,
                details={
                    "model_id": model_id,
                    "total_tokens": total_tokens,
                    "blended_rate_per_million": str(higher_rate),
                    "pricing_version": self.version,
                },
            )

        # Fallback for any other partial case
        fallback = (
            conservative_fallback_cost
            if conservative_fallback_cost is not None
            else Decimal("0.02")
        )
        return CostEstimate(
            amount_usd=fallback.quantize(_MICRO_CENT, rounding=ROUND_HALF_UP),
            currency=pricing.currency,
            state="unknown",
            is_estimate=True,
            details={"model_id": model_id, "pricing_version": self.version},
        )


def create_operator_standard_pricing_table() -> PricingTable:
    """Build a standard operator-supplied pricing table with explicit date and source."""
    table = PricingTable(
        version="2026-10-01",
        date="2026-10-01",
        source="operator_reference_schedule_v1",
    )
    # GPT-4o
    table.add_model_pricing(
        ModelPricing(
            model_id="gpt-4o",
            input_cost_per_million=Decimal("2.50"),
            output_cost_per_million=Decimal("10.00"),
            effective_date="2026-10-01",
            source="operator_reference_schedule_v1",
        )
    )
    # GPT-4o-mini
    table.add_model_pricing(
        ModelPricing(
            model_id="gpt-4o-mini",
            input_cost_per_million=Decimal("0.15"),
            output_cost_per_million=Decimal("0.60"),
            effective_date="2026-10-01",
            source="operator_reference_schedule_v1",
        )
    )
    # Claude 3.5 Sonnet
    table.add_model_pricing(
        ModelPricing(
            model_id="claude-3-5-sonnet",
            input_cost_per_million=Decimal("3.00"),
            output_cost_per_million=Decimal("15.00"),
            effective_date="2026-10-01",
            source="operator_reference_schedule_v1",
        )
    )
    # Gemini 1.5 Pro
    table.add_model_pricing(
        ModelPricing(
            model_id="gemini-1.5-pro",
            input_cost_per_million=Decimal("1.25"),
            output_cost_per_million=Decimal("5.00"),
            effective_date="2026-10-01",
            source="operator_reference_schedule_v1",
        )
    )
    # Gemini 1.5 Flash
    table.add_model_pricing(
        ModelPricing(
            model_id="gemini-1.5-flash",
            input_cost_per_million=Decimal("0.075"),
            output_cost_per_million=Decimal("0.30"),
            effective_date="2026-10-01",
            source="operator_reference_schedule_v1",
        )
    )
    # Fake / Mock test model
    table.add_model_pricing(
        ModelPricing(
            model_id="mock-model",
            input_cost_per_million=Decimal("1.00"),
            output_cost_per_million=Decimal("2.00"),
            effective_date="2026-10-01",
            source="operator_reference_schedule_v1",
        )
    )
    return table


class CurrencyConverter:
    """Handles operator-supplied FX conversions to INR or other target currencies.

    All currency conversions are explicitly flagged as estimates with dated FX references.
    """

    def __init__(self, rates: list[FXRate] | None = None) -> None:
        self._rates: dict[tuple[str, str], FXRate] = {}
        if rates:
            for rate in rates:
                self.set_rate(rate)

    def set_rate(self, rate: FXRate) -> None:
        """Register an operator-configured FX rate."""
        self._rates[(rate.from_currency.upper(), rate.to_currency.upper())] = rate

    def get_rate(self, from_currency: str, to_currency: str) -> FXRate | None:
        """Get FX rate for currency pair."""
        return self._rates.get((from_currency.upper(), to_currency.upper()))

    def convert_usd_to_inr(self, amount_usd: Decimal) -> tuple[Decimal, FXRate]:
        """Convert USD to INR using operator rate.

        Returns tuple of (converted_amount, fx_rate_used).
        Raises CurrencyConversionError if rate is not configured.
        """
        rate = self.get_rate("USD", "INR")
        if rate is None:
            raise CurrencyConversionError(
                "USD",
                "INR",
                "No dated FX rate configured by operator for USD -> INR conversion",
            )
        converted = rate.convert(amount_usd)
        return converted, rate

    def attach_inr_estimate(self, estimate: CostEstimate) -> CostEstimate:
        """Enrich a CostEstimate with an INR estimate if USD->INR FX rate is available."""
        rate = self.get_rate(estimate.currency, "INR")
        if rate is None:
            return estimate

        inr_val = rate.convert(estimate.amount_usd)
        return CostEstimate(
            amount_usd=estimate.amount_usd,
            currency=estimate.currency,
            state=estimate.state,
            is_estimate=True,  # FX conversion is always an estimate
            inr_estimate=inr_val,
            fx_rate_used=rate.rate,
            fx_date=rate.date,
            details={
                **estimate.details,
                "inr_source": rate.source,
                "inr_date": rate.date,
            },
        )
