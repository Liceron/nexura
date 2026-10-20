# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Typed errors for usage, cost, and budget enforcement."""

from __future__ import annotations

from typing import Any

from nexura_agents.errors import NexuraError


class UsageError(NexuraError):
    """Base exception for usage, pricing, and budget enforcement errors."""


class BudgetExceededError(UsageError):
    """Raised when a budget limit (cost, tokens, turns, tool calls) is exceeded."""

    def __init__(
        self,
        limit_type: str,
        current_value: Any,
        max_value: Any,
        *,
        workspace_id: str | None = None,
        run_id: str | None = None,
        message: str | None = None,
    ) -> None:
        base_msg = (
            f"Budget limit exceeded for '{limit_type}': "
            f"current={current_value!r} > max={max_value!r}"
        )
        msg = message or base_msg + (f" (workspace={workspace_id})" if workspace_id else "") + (
            f" (run={run_id})" if run_id else ""
        )
        super().__init__(msg)
        self.limit_type = limit_type
        self.current_value = current_value
        self.max_value = max_value
        self.workspace_id = workspace_id
        self.run_id = run_id


class ReservationNotFoundError(UsageError):
    """Raised when a reservation ID is unknown or has already been closed."""

    def __init__(self, reservation_id: str) -> None:
        super().__init__(
            f"Reservation '{reservation_id}' was not found or has already been finalized."
        )
        self.reservation_id = reservation_id


class ReservationExpiredError(UsageError):
    """Raised when attempting to reconcile or commit an expired reservation."""

    def __init__(self, reservation_id: str) -> None:
        super().__init__(f"Reservation '{reservation_id}' has expired and was released.")
        self.reservation_id = reservation_id


class InvalidPricingError(UsageError):
    """Raised when model pricing is missing, invalid, or malformed."""

    def __init__(self, model_id: str, reason: str) -> None:
        super().__init__(f"Pricing error for model '{model_id}': {reason}")
        self.model_id = model_id
        self.reason = reason


class CurrencyConversionError(UsageError):
    """Raised when currency conversion fails due to missing FX rate or unsupported currency."""

    def __init__(self, from_currency: str, to_currency: str, reason: str) -> None:
        super().__init__(f"Cannot convert from '{from_currency}' to '{to_currency}': {reason}")
        self.from_currency = from_currency
        self.to_currency = to_currency
        self.reason = reason


class CrossWorkspaceAccessError(UsageError):
    """Raised when an operation attempts to access or settle reservations across workspaces."""

    def __init__(self, resource_id: str, expected_workspace: str, actual_workspace: str) -> None:
        super().__init__(
            f"Cross-workspace access denied for resource '{resource_id}': "
            f"expected '{expected_workspace}', got '{actual_workspace}'."
        )
        self.resource_id = resource_id
        self.expected_workspace = expected_workspace
        self.actual_workspace = actual_workspace
