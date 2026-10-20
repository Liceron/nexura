# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Typed exception hierarchy for business channel adapters (Email and WhatsApp)."""

from __future__ import annotations


class ChannelError(Exception):
    """Base exception for all business channel integration failures."""

    def __init__(self, message: str, details: dict[str, object] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class ChannelWebhookError(ChannelError):
    """Exception raised when an incoming webhook payload fails validation."""


class ForgedWebhookError(ChannelWebhookError):
    """Exception raised when an incoming webhook signature or challenge is invalid or forged."""


class DuplicateDeliveryError(ChannelError):
    """Exception raised when a duplicate message or event delivery is received."""


class CredentialRevokedError(ChannelError):
    """Exception raised when channel API credentials or access tokens are revoked or expired."""


class TemplateRequiredError(ChannelError):
    """Exception raised when sending outside the WhatsApp 24h customer window without a template."""


class ConsentRequiredError(ChannelError):
    """Exception raised when customer opt-in/consent has not been verified for outbound sends."""


class AttachmentSizeExceededError(ChannelError):
    """Exception raised when an attachment exceeds provider or policy byte size limits."""


class ApprovalRequiredError(ChannelError):
    """Exception raised when an unapproved effectful outbound message attempts to send."""


class ChannelDeliveryError(ChannelError):
    """Exception raised when outbound message transport fails permanently or exhausts retries."""


class NonIdempotentRetryForbiddenError(ChannelError):
    """Exception raised when automated retries are requested on non-idempotent operations
    without an explicit idempotency key.
    """
