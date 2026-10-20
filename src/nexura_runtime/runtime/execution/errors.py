# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Structured errors for durable execution and worker lifecycle."""

from __future__ import annotations

from typing import Any

from nexura_agents.contracts.execution import DefinitionMismatchError
from nexura_agents.errors import NexuraError


class DurableExecutionError(NexuraError):
    """Base error for durable execution engine failures."""

    def __init__(
        self,
        message: str,
        details: dict[str, Any] | None = None,
        original_error: BaseException | None = None,
    ) -> None:
        super().__init__(message, original_error=original_error)
        self.details = details or {}


class FencingConflictError(DurableExecutionError):
    """Raised when a worker attempts an operation with a stale fencing token."""

    def __init__(
        self,
        run_id: str,
        expected_token: int,
        actual_token: int,
        worker_id: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        msg = (
            f"Fencing conflict on run '{run_id}': worker '{worker_id}' held "
            f"token {expected_token}, but current token is {actual_token}."
        )

        merged = {
            "run_id": run_id,
            "worker_id": worker_id,
            "expected_token": expected_token,
            "actual_token": actual_token,
            **(details or {}),
        }
        super().__init__(msg, details=merged)
        self.run_id = run_id
        self.worker_id = worker_id
        self.expected_token = expected_token
        self.actual_token = actual_token


class LeaseExpiredError(DurableExecutionError):
    """Raised when a worker's lease has expired before completing a step."""

    def __init__(
        self,
        run_id: str,
        worker_id: str,
        expires_at: float,
        current_time: float,
    ) -> None:
        msg = (
            f"Lease expired on run '{run_id}' for worker '{worker_id}': "
            f"expired at {expires_at:.2f}, current time is {current_time:.2f}."
        )
        super().__init__(
            msg,
            details={
                "run_id": run_id,
                "worker_id": worker_id,
                "expires_at": expires_at,
                "current_time": current_time,
            },
        )
        self.run_id = run_id
        self.worker_id = worker_id
        self.expires_at = expires_at
        self.current_time = current_time


class DurableDefinitionMismatchError(DurableExecutionError, DefinitionMismatchError):
    """The workflow spec differs from the stored run's digest (refused before dispatch).

    Catchable as the legacy ``DurableExecutionError`` and as the Execution Contract's
    ``DefinitionMismatchError``.
    """
