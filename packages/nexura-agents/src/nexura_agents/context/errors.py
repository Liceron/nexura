# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Structured exception definitions for context selection and capacity bounds."""

from __future__ import annotations

from nexura_agents.errors import NexuraError


class ContextError(NexuraError):
    """Base exception for all context selection and packing failures."""


class ContextOverflowError(ContextError):
    """Raised when protected context material exceeds the allocated token capacity.

    Mandatory invariants and evidence cannot be silently discarded.
    """

    def __init__(
        self,
        message: str,
        *,
        required_tokens: int,
        max_context_tokens: int,
        protected_tokens: int,
        overflow_items: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.required_tokens = required_tokens
        self.max_context_tokens = max_context_tokens
        self.protected_tokens = protected_tokens
        self.overflow_items = overflow_items or []


class UnauthorizedSourceError(ContextError):
    """Raised when an item originates from an unauthorized workspace or source."""

    def __init__(self, source_id: str, workspace_id: str | None = None) -> None:
        msg = f"Source '{source_id}' is unauthorized for workspace '{workspace_id or 'unknown'}'."
        super().__init__(msg)
        self.source_id = source_id
        self.workspace_id = workspace_id


class ProtectedInvariantViolationError(ContextError):
    """Raised when a projection or reduction attempts to mutate a protected field."""


class MalformedStructuredDataError(ContextError):
    """Raised when structured input data cannot be parsed according to the expected schema."""
