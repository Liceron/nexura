# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Data models for business channels (Email and WhatsApp) envelope and receipts."""

from __future__ import annotations

import time
from typing import Any, Literal

from pydantic import BaseModel, Field

ChannelType = Literal["email", "whatsapp"]
MessageDirection = Literal["inbound", "outbound"]
DeliveryStatus = Literal["delivered", "dry_run", "failed", "deduplicated", "queued"]


class ChannelAttachment(BaseModel):
    """Normalized attachment representation for channel messages."""

    filename: str
    mime_type: str
    size_bytes: int
    data: bytes | None = None
    artifact_id: str | None = None
    digest: str | None = None
    redacted: bool = False


class ChannelEnvelope(BaseModel):
    """Unified cross-channel envelope normalizing inbound and outbound communications."""

    channel: ChannelType
    direction: MessageDirection
    message_id: str
    sender: str
    recipient: str
    subject: str | None = None
    text_content: str = ""
    html_content: str | None = None
    template_name: str | None = None
    template_language: str | None = None
    template_components: list[dict[str, Any]] | None = None
    attachments: list[ChannelAttachment] = Field(default_factory=list)
    raw_metadata: dict[str, Any] = Field(default_factory=dict)
    timestamp: float = Field(default_factory=time.time)
    idempotency_key: str | None = None
    dry_run: bool = False
    approved: bool = True
    consent_verified: bool = False
    is_service_window_active: bool = False


class OutboxReceipt(BaseModel):
    """Receipt tracking outbound channel dispatches and dry-run outbox entries."""

    receipt_id: str
    message_id: str
    channel: ChannelType
    recipient: str
    status: DeliveryStatus
    dry_run: bool
    rendered_content: dict[str, Any] = Field(default_factory=dict)
    attempt_count: int = 1
    created_at: float = Field(default_factory=time.time)
    metadata: dict[str, Any] = Field(default_factory=dict)
    # Owning workspace. ``None`` for receipts written by single-tenant/local callers.
    workspace_id: str | None = None


class DeduplicationRecord(BaseModel):
    """Record storing deduplication status of inbound message IDs or outbound keys."""

    key: str
    processed_at: float = Field(default_factory=time.time)
    status: str = "processed"
