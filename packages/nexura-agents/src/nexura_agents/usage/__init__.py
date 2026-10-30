# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Usage, cost, and budget governance for Nexura AgentKit.

Provides hierarchical budget enforcement, concurrency-safe reservations,
conservative reconciliation, dated operator pricing tables, and FX estimates.
"""

from __future__ import annotations

from nexura_agents.usage.errors import (
    BudgetExceededError,
    CurrencyConversionError,
    InvalidPricingError,
    ReservationExpiredError,
    ReservationNotFoundError,
    UsageError,
)
from nexura_agents.usage.ledger import UsageLedger
from nexura_agents.usage.manager import BudgetManager, RunBudget
from nexura_agents.usage.models import (
    BudgetConfig,
    BudgetStatus,
    CostEstimate,
    FXRate,
    LedgerEntry,
    ModelPricing,
    Reservation,
    TokenUsage,
    UsageState,
)
from nexura_agents.usage.module import UsageBudgetModule
from nexura_agents.usage.pricing import (
    CurrencyConverter,
    PricingTable,
    create_operator_standard_pricing_table,
)

__all__ = [
    # Errors
    "UsageError",
    "BudgetExceededError",
    "ReservationNotFoundError",
    "ReservationExpiredError",
    "InvalidPricingError",
    "CurrencyConversionError",
    # Models
    "TokenUsage",
    "ModelPricing",
    "FXRate",
    "CostEstimate",
    "Reservation",
    "LedgerEntry",
    "BudgetConfig",
    "BudgetStatus",
    "UsageState",
    # Pricing & Currency
    "PricingTable",
    "CurrencyConverter",
    "create_operator_standard_pricing_table",
    # Ledger & Management
    "UsageLedger",
    "RunBudget",
    "BudgetManager",
    # Module
    "UsageBudgetModule",
]
