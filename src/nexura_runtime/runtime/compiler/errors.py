# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Structured errors for workflow compilation and port validation."""

from __future__ import annotations

from typing import Any

from nexura_agents.contracts.errors import ContractError, ContractErrorCode


class CompilerError(ContractError):
    """Base error for workflow compilation and validation failures."""

    def __init__(
        self,
        message: str,
        *,
        code: ContractErrorCode | str = ContractErrorCode.INVALID_SCHEMA,
        details: dict[str, Any] | None = None,
        original_error: BaseException | None = None,
    ) -> None:
        super().__init__(
            message,
            code=code,
            details=details,
            original_error=original_error,
        )


class UnresolvedReferenceError(CompilerError):
    """Raised when a node references an unknown module, target node, or port."""

    def __init__(
        self,
        message: str,
        *,
        reference_kind: str,
        reference_value: str,
        referencing_node: str | None = None,
    ) -> None:
        super().__init__(
            message,
            code=ContractErrorCode.INVALID_SCHEMA,
            details={
                "reference_kind": reference_kind,
                "reference_value": reference_value,
                "referencing_node": referencing_node,
            },
        )


class IncompatiblePortError(CompilerError):
    """Raised when an output port is bound to an incompatible or missing input port."""

    def __init__(
        self,
        message: str,
        *,
        source_node: str,
        source_port: str,
        target_node: str,
        target_port: str,
    ) -> None:
        super().__init__(
            message,
            code=ContractErrorCode.INVALID_SCHEMA,
            details={
                "source_node": source_node,
                "source_port": source_port,
                "target_node": target_node,
                "target_port": target_port,
            },
        )


class FanoutLimitExceededError(CompilerError):
    """Raised when parallel fanout concurrency exceeds the bounded system limit."""

    def __init__(
        self,
        message: str,
        *,
        node_id: str,
        requested_concurrency: int,
        limit: int,
    ) -> None:
        super().__init__(
            message,
            code=ContractErrorCode.BUDGET_EXHAUSTED,
            details={
                "node_id": node_id,
                "requested_concurrency": requested_concurrency,
                "limit": limit,
            },
        )


class InvalidCachePolicyError(CompilerError):
    """Raised when caching is enabled on a non-pure effect node."""

    def __init__(
        self,
        message: str,
        *,
        node_id: str,
        effect_policy: str,
    ) -> None:
        super().__init__(
            message,
            code=ContractErrorCode.POLICY_DENIED,
            details={"node_id": node_id, "effect_policy": effect_policy},
        )
