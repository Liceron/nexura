# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Structured errors for effect management and reconciliation."""

from __future__ import annotations

from typing import Any

from nexura_agents.contracts.errors import ContractError, ContractErrorCode


class EffectError(ContractError):
    """Base error for effect dispatch and reconciliation failures."""

    def __init__(
        self,
        message: str,
        *,
        code: ContractErrorCode | str = ContractErrorCode.INDETERMINATE_EFFECT,
        details: dict[str, Any] | None = None,
        original_error: BaseException | None = None,
    ) -> None:
        super().__init__(
            message,
            code=code,
            details=details,
            original_error=original_error,
        )


class EffectIndeterminateError(EffectError):
    """Raised when an external effect mutation dispatch loses response or is in an unknown state."""

    def __init__(
        self,
        message: str,
        *,
        effect_id: str,
        action_name: str,
        idempotency_key: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        merged = {
            "effect_id": effect_id,
            "action_name": action_name,
            "idempotency_key": idempotency_key,
        }
        if details:
            merged.update(details)
        super().__init__(
            message,
            code=ContractErrorCode.INDETERMINATE_EFFECT,
            details=merged,
        )


class ReconciliationError(EffectError):
    """Raised when reconciliation fails or invalid status transition is attempted."""

    def __init__(
        self,
        message: str,
        *,
        effect_id: str,
        current_status: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        merged = {
            "effect_id": effect_id,
            "current_status": current_status,
        }
        if details:
            merged.update(details)
        super().__init__(
            message,
            code=ContractErrorCode.INVALID_SCHEMA,
            details=merged,
        )
