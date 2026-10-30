# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Typed exception hierarchy for isolated sandbox execution."""

from __future__ import annotations

from typing import Any


class SandboxError(Exception):
    """Base exception for all isolated execution and sandbox failures."""

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class CapabilityUnavailableError(SandboxError):
    """Raised when required process/container isolation is unsupported or unconfigured.

    Strict Security Rule:
    Never silently fall back to insecure in-process eval/exec when isolation is unavailable.
    """

    def __init__(
        self,
        reason: str,
        *,
        backend: str = "gvisor_oci",
        platform_info: str | None = None,
    ) -> None:
        super().__init__(
            f"Sandbox isolation capability unavailable for backend '{backend}': {reason}. "
            "Refusing to execute untrusted code without genuine boundary isolation.",
            details={"backend": backend, "reason": reason, "platform_info": platform_info},
        )
        self.backend = backend
        self.reason = reason


class ExecutionTimeoutError(SandboxError):
    """Raised when untrusted code execution exceeds the allocated deadline."""

    def __init__(self, timeout_seconds: float, *, worker_id: str | None = None) -> None:
        super().__init__(
            f"Execution timed out after {timeout_seconds:.2f}s "
            f"in worker '{worker_id or 'unknown'}'",
            details={"timeout_seconds": timeout_seconds, "worker_id": worker_id},
        )
        self.timeout_seconds = timeout_seconds
        self.worker_id = worker_id


class MemoryLimitExceededError(SandboxError):
    """Raised when untrusted code violates allocated memory boundaries."""

    def __init__(self, limit_mb: int, *, worker_id: str | None = None) -> None:
        super().__init__(
            f"Execution exceeded memory quota ({limit_mb}MB) in worker '{worker_id or 'unknown'}'",
            details={"limit_mb": limit_mb, "worker_id": worker_id},
        )
        self.limit_mb = limit_mb
        self.worker_id = worker_id


class OutputLimitExceededError(SandboxError):
    """Raised when stdout or stderr output exceeds configured size bounds."""

    def __init__(self, max_bytes: int, actual_bytes: int) -> None:
        super().__init__(
            f"Output limit exceeded: generated {actual_bytes} bytes (cap is {max_bytes} bytes)",
            details={"max_bytes": max_bytes, "actual_bytes": actual_bytes},
        )
        self.max_bytes = max_bytes
        self.actual_bytes = actual_bytes


class SecurityViolationError(SandboxError):
    """Raised when untrusted code attempts an unauthorized security action.

    Includes fork bombing, network socket creation, path escape, or accessing host credentials.
    """

    def __init__(self, violation_type: str, message: str) -> None:
        super().__init__(
            f"Sandbox security violation [{violation_type}]: {message}",
            details={"violation_type": violation_type, "message": message},
        )
        self.violation_type = violation_type


class WorkerCrashError(SandboxError):
    """Raised when disposable worker process exits unexpectedly or via signal."""

    def __init__(self, exit_code: int, signal_name: str | None = None) -> None:
        sig_str = f" (signal {signal_name})" if signal_name else ""
        super().__init__(
            f"Sandbox worker terminated abnormally with exit code {exit_code}{sig_str}",
            details={"exit_code": exit_code, "signal": signal_name},
        )
        self.exit_code = exit_code
        self.signal_name = signal_name


class UnsupportedFeatureError(SandboxError):
    """Raised when code uses a language feature the selected backend does not run.

    Monty runs a Python subset (no class inheritance, no generators, no third-party
    packages, a partial standard library, no stdin). This is a clear, recoverable error for
    the caller, not a worker crash and not a capability or security outcome.
    """

    def __init__(self, feature: str, *, backend: str) -> None:
        super().__init__(
            f"Backend '{backend}' does not support this code: {feature}. It runs a Python "
            "subset; rewrite the code without it, or ask the operator for a full-Python backend.",
            details={"feature": feature, "backend": backend},
        )
        self.feature = feature
        self.backend = backend


class InputLimitExceededError(SandboxError):
    """Raised before any worker starts when code, stdin and files exceed the operator caps."""

    def __init__(self, reason: str, *, limit: int, actual: int) -> None:
        super().__init__(
            f"Sandbox input limit exceeded: {reason} ({actual} > {limit})",
            details={"reason": reason, "limit": limit, "actual": actual},
        )
        self.reason = reason
        self.limit = limit
        self.actual = actual
