# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Typed exception hierarchy for the observability and tracing engine."""

from __future__ import annotations


class ObservabilityError(Exception):
    """Base exception for all observability and tracing errors."""

    def __init__(self, message: str, details: dict[str, object] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class ExporterFailedError(ObservabilityError):
    """Exception raised when an event/trace exporter fails under strict failure policy."""


class InvalidSpanError(ObservabilityError):
    """Exception raised when a span lifecycle transition is invalid or corrupt."""


class SinkClosedError(ObservabilityError):
    """Exception raised when attempting to emit events into a closed event sink."""
