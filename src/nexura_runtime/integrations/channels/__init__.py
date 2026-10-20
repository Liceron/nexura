# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Business channel adapters for Email and WhatsApp Cloud API."""

from __future__ import annotations

from nexura_runtime.integrations.channels.email import (
    EmailAdapter,
    parse_inbound_mime,
    render_mime_message,
    verify_email_webhook_signature,
)
from nexura_runtime.integrations.channels.errors import (
    ApprovalRequiredError,
    AttachmentSizeExceededError,
    ChannelDeliveryError,
    ChannelError,
    ChannelWebhookError,
    ConsentRequiredError,
    CredentialRevokedError,
    DuplicateDeliveryError,
    ForgedWebhookError,
    NonIdempotentRetryForbiddenError,
    TemplateRequiredError,
)
from nexura_runtime.integrations.channels.models import (
    ChannelAttachment,
    ChannelEnvelope,
    ChannelType,
    DeduplicationRecord,
    DeliveryStatus,
    MessageDirection,
    OutboxReceipt,
)
from nexura_runtime.integrations.channels.module import (
    EmailChannelModule,
    WhatsAppChannelModule,
)
from nexura_runtime.integrations.channels.outbox import (
    ChannelStore,
    MemoryChannelStore,
    SqliteChannelStore,
)
from nexura_runtime.integrations.channels.redaction import (
    create_attachment,
    enforce_attachment_limits,
    redact_text,
)
from nexura_runtime.integrations.channels.whatsapp import (
    WhatsAppAdapter,
    parse_inbound_whatsapp_webhook,
    render_whatsapp_payload,
    verify_whatsapp_webhook_challenge,
    verify_whatsapp_webhook_signature,
)
from nexura_runtime.modules.registry import default_registry

__all__ = [
    "ChannelError",
    "ChannelWebhookError",
    "ForgedWebhookError",
    "DuplicateDeliveryError",
    "CredentialRevokedError",
    "TemplateRequiredError",
    "ConsentRequiredError",
    "AttachmentSizeExceededError",
    "ApprovalRequiredError",
    "ChannelDeliveryError",
    "NonIdempotentRetryForbiddenError",
    "ChannelType",
    "MessageDirection",
    "DeliveryStatus",
    "ChannelAttachment",
    "ChannelEnvelope",
    "OutboxReceipt",
    "DeduplicationRecord",
    "redact_text",
    "enforce_attachment_limits",
    "create_attachment",
    "ChannelStore",
    "MemoryChannelStore",
    "SqliteChannelStore",
    "EmailAdapter",
    "verify_email_webhook_signature",
    "parse_inbound_mime",
    "render_mime_message",
    "WhatsAppAdapter",
    "verify_whatsapp_webhook_challenge",
    "verify_whatsapp_webhook_signature",
    "parse_inbound_whatsapp_webhook",
    "render_whatsapp_payload",
    "EmailChannelModule",
    "WhatsAppChannelModule",
]

_email_module = EmailChannelModule()
if not default_registry.has(_email_module.manifest.module_id):
    default_registry.register(_email_module)

_whatsapp_module = WhatsAppChannelModule()
if not default_registry.has(_whatsapp_module.manifest.module_id):
    default_registry.register(_whatsapp_module)
