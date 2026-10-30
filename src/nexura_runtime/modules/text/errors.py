# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Typed errors for prompt template rendering and text transforms."""

from __future__ import annotations

from nexura_agents.errors import NexuraError


class TemplateError(NexuraError):
    """Base class for template processing errors."""


class TemplateVariableMissingError(TemplateError):
    """Raised when a template variable is referenced but not supplied."""

    def __init__(self, variable_name: str, message: str | None = None) -> None:
        self.variable_name = variable_name
        msg = message or f"Template missing required variable: '{variable_name}'"
        super().__init__(msg)


class TemplateExpansionLimitError(TemplateError):
    """Raised when rendered template output exceeds the allowed character limit."""

    def __init__(self, current_length: int, max_length: int) -> None:
        self.current_length = current_length
        self.max_length = max_length
        super().__init__(
            f"Rendered template length ({current_length} chars) exceeds maximum allowed limit "
            f"of {max_length} chars."
        )


class TemplateSyntaxError(TemplateError):
    """Raised when template syntax is malformed (unclosed tags, unmatched blocks)."""


class TemplateSecurityError(TemplateError):
    """Raised when template attempts unsafe attribute access (dunders, private attributes)."""
