# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Structured error codes and exceptions for module contracts."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from nexura_agents.errors import NexuraError


class ContractErrorCode(StrEnum):
    """Stable error codes for module execution and validation."""

    INVALID_SCHEMA = "INVALID_SCHEMA"
    INVALID_CONFIG = "INVALID_CONFIG"
    MISSING_DEPENDENCY = "MISSING_DEPENDENCY"
    POLICY_DENIED = "POLICY_DENIED"
    DEADLINE_EXCEEDED = "DEADLINE_EXCEEDED"
    EXECUTION_CANCELLED = "EXECUTION_CANCELLED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    UPSTREAM_FAILURE = "UPSTREAM_FAILURE"
    INDETERMINATE_EFFECT = "INDETERMINATE_EFFECT"
    AUTHENTICATION_FAILED = "AUTHENTICATION_FAILED"
    ARTIFACT_NOT_FOUND = "ARTIFACT_NOT_FOUND"
    CAPABILITY_UNSUPPORTED = "CAPABILITY_UNSUPPORTED"


class ContractError(NexuraError):
    """Base exception for contract-level errors with stable codes and details."""

    def __init__(
        self,
        message: str,
        *,
        code: ContractErrorCode | str,
        details: dict[str, Any] | None = None,
        original_error: BaseException | None = None,
    ) -> None:
        super().__init__(message, original_error=original_error)
        self.code = code if isinstance(code, str) else code.value
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        """Convert error to a structured dictionary."""
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }


class SchemaValidationError(ContractError):
    """Raised when module inputs or outputs fail schema validation."""

    def __init__(
        self,
        message: str,
        *,
        schema_kind: str = "input",
        details: dict[str, Any] | None = None,
        original_error: BaseException | None = None,
    ) -> None:
        merged_details = {"schema_kind": schema_kind, **(details or {})}
        super().__init__(
            message,
            code=ContractErrorCode.INVALID_SCHEMA,
            details=merged_details,
            original_error=original_error,
        )


class ConfigValidationError(ContractError):
    """Raised when module configuration fails schema validation."""

    def __init__(
        self,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        original_error: BaseException | None = None,
    ) -> None:
        super().__init__(
            message,
            code=ContractErrorCode.INVALID_CONFIG,
            details=details,
            original_error=original_error,
        )


class MissingDependencyError(ContractError):
    """Raised when an optional dependency required by a module is not installed."""

    def __init__(
        self,
        dependency_name: str,
        *,
        extra_name: str | None = None,
        message: str | None = None,
        original_error: BaseException | None = None,
    ) -> None:
        self.dependency_name = dependency_name
        self.extra_name = extra_name
        if message is not None:
            msg = message
        elif extra_name:
            msg = (
                f"Required dependency '{dependency_name}' is not installed. "
                f"Install it via 'pip install nexura-runtime[{extra_name}]'."
            )
        else:
            msg = f"Required dependency '{dependency_name}' is not installed."
        details = {"dependency": dependency_name, "extra": extra_name}
        super().__init__(
            msg,
            code=ContractErrorCode.MISSING_DEPENDENCY,
            details=details,
            original_error=original_error,
        )


class PolicyDeniedError(ContractError):
    """Raised when an operation is denied by security, outbound, or effect policy."""

    def __init__(
        self,
        message: str,
        *,
        policy_name: str,
        details: dict[str, Any] | None = None,
        original_error: BaseException | None = None,
    ) -> None:
        merged = {"policy": policy_name, **(details or {})}
        super().__init__(
            message,
            code=ContractErrorCode.POLICY_DENIED,
            details=merged,
            original_error=original_error,
        )


class DeadlineExceededError(ContractError):
    """Raised when execution exceeds the allocated deadline."""

    def __init__(
        self,
        message: str = "Execution deadline exceeded",
        *,
        timeout_seconds: float | None = None,
        elapsed_seconds: float | None = None,
        original_error: BaseException | None = None,
    ) -> None:
        details: dict[str, Any] = {}
        if timeout_seconds is not None:
            details["timeout_seconds"] = timeout_seconds
        if elapsed_seconds is not None:
            details["elapsed_seconds"] = elapsed_seconds
        super().__init__(
            message,
            code=ContractErrorCode.DEADLINE_EXCEEDED,
            details=details,
            original_error=original_error,
        )


class BudgetExhaustedError(ContractError):
    """Raised when token or currency budget is exhausted."""

    def __init__(
        self,
        message: str = "Execution budget exhausted",
        *,
        budget_kind: str,
        consumed: Any = None,
        limit: Any = None,
        original_error: BaseException | None = None,
    ) -> None:
        details = {"budget_kind": budget_kind, "consumed": consumed, "limit": limit}
        super().__init__(
            message,
            code=ContractErrorCode.BUDGET_EXHAUSTED,
            details=details,
            original_error=original_error,
        )


class IndeterminateEffectError(ContractError):
    """Raised when an external effect's outcome cannot be confirmed."""

    def __init__(
        self,
        message: str,
        *,
        node_id: str,
        action: str,
        details: dict[str, Any] | None = None,
        original_error: BaseException | None = None,
    ) -> None:
        merged = {"node_id": node_id, "action": action, **(details or {})}
        super().__init__(
            message,
            code=ContractErrorCode.INDETERMINATE_EFFECT,
            details=merged,
            original_error=original_error,
        )


class CapabilityUnsupportedError(ContractError):
    """Raised when a requested capability is not supported by the module or provider."""

    def __init__(
        self,
        capability: str,
        module_id: str,
        *,
        message: str | None = None,
        original_error: BaseException | None = None,
    ) -> None:
        msg = message or f"Capability '{capability}' is not supported by module '{module_id}'."
        super().__init__(
            msg,
            code=ContractErrorCode.CAPABILITY_UNSUPPORTED,
            details={"capability": capability, "module_id": module_id},
            original_error=original_error,
        )
