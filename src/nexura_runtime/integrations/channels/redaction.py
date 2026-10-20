# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Redaction, PII sanitization, and attachment limits for business channels."""

from __future__ import annotations

import hashlib
import re

from nexura_runtime.credentials.store import redact_secrets
from nexura_runtime.integrations.channels.errors import AttachmentSizeExceededError
from nexura_runtime.integrations.channels.models import ChannelAttachment

# Regular expressions for common PII and sensitive tokens
_CREDIT_CARD_PATTERN = re.compile(r"\b(?:\d{4}[ -]?){3}\d{4}\b")
_SSN_PATTERN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_BEARER_PATTERN = re.compile(r"(?i)\bBearer\s+([A-Za-z0-9_\-\.]{8,})\b")
_API_KEY_PATTERN = re.compile(r"\b(?:nxk|sk|key)_[A-Za-z0-9_]{16,}\b")
_PASSWORD_PATTERN = re.compile(r"(?i)\b(password|secret|api_key|token)\s*[:=]\s*([^\s,;]+)")


def redact_text(text: str, custom_secrets: list[str] | None = None) -> str:
    """Sanitize and redact sensitive PII and secrets from message content."""
    if not text:
        return ""

    sanitized = text

    # Redact explicit custom secrets first if provided
    if custom_secrets:
        sanitized = redact_secrets(sanitized, custom_secrets)

    # Redact standard credit card numbers
    sanitized = _CREDIT_CARD_PATTERN.sub("[REDACTED_CARD]", sanitized)

    # Redact SSNs
    sanitized = _SSN_PATTERN.sub("[REDACTED_SSN]", sanitized)

    # Redact Bearer tokens
    sanitized = _BEARER_PATTERN.sub("Bearer [REDACTED]", sanitized)

    # Redact API keys
    sanitized = _API_KEY_PATTERN.sub("[REDACTED_KEY]", sanitized)

    # Redact passwords/secrets in key-value pairs
    sanitized = _PASSWORD_PATTERN.sub(r"\1: [REDACTED]", sanitized)

    return sanitized


def enforce_attachment_limits(
    attachments: list[ChannelAttachment],
    max_single_bytes: int = 10 * 1024 * 1024,  # 10 MB default per attachment
    max_total_bytes: int = 25 * 1024 * 1024,  # 25 MB default total
) -> None:
    """Validate that attachments do not exceed single or aggregate size limits."""
    total_bytes = 0
    for att in attachments:
        if att.size_bytes > max_single_bytes:
            raise AttachmentSizeExceededError(
                f"Attachment '{att.filename}' exceeds maximum allowed size "
                f"({att.size_bytes} > {max_single_bytes} bytes)."
            )
        total_bytes += att.size_bytes

    if total_bytes > max_total_bytes:
        raise AttachmentSizeExceededError(
            f"Aggregate attachments size exceeds maximum allowed limit "
            f"({total_bytes} > {max_total_bytes} bytes)."
        )


def create_attachment(
    filename: str,
    mime_type: str,
    data: bytes,
    max_single_bytes: int = 10 * 1024 * 1024,
) -> ChannelAttachment:
    """Create and validate a ChannelAttachment with computed SHA-256 digest."""
    size_bytes = len(data)
    if size_bytes > max_single_bytes:
        raise AttachmentSizeExceededError(
            f"Attachment '{filename}' exceeds maximum allowed size "
            f"({size_bytes} > {max_single_bytes} bytes)."
        )

    digest = hashlib.sha256(data).hexdigest()
    return ChannelAttachment(
        filename=filename,
        mime_type=mime_type,
        size_bytes=size_bytes,
        data=data,
        digest=digest,
    )
