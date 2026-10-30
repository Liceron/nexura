# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""WhatsApp Cloud API adapter conforming to Meta contracts and B26 requirements."""

from __future__ import annotations

import hashlib
import hmac
import time
import uuid
from collections.abc import Callable
from typing import Any

from nexura_runtime.integrations.channels.errors import (
    ApprovalRequiredError,
    AttachmentSizeExceededError,
    ConsentRequiredError,
    CredentialRevokedError,
    ForgedWebhookError,
    NonIdempotentRetryForbiddenError,
    TemplateRequiredError,
)
from nexura_runtime.integrations.channels.models import (
    ChannelAttachment,
    ChannelEnvelope,
    OutboxReceipt,
)
from nexura_runtime.integrations.channels.outbox import ChannelStore, MemoryChannelStore
from nexura_runtime.integrations.channels.redaction import (
    enforce_attachment_limits,
    redact_text,
)


def verify_whatsapp_webhook_challenge(
    mode: str,
    token: str,
    challenge: str,
    verify_token: str,
) -> str:
    """Verify incoming Meta GET webhook verification challenge.

    Returns challenge string on success or raises ForgedWebhookError.
    """
    if mode != "subscribe" or not hmac.compare_digest(token, verify_token):
        raise ForgedWebhookError("Invalid WhatsApp webhook challenge mode or verification token.")
    return challenge


def verify_whatsapp_webhook_signature(
    payload_bytes: bytes,
    signature_header: str,
    app_secret: str,
) -> bool:
    """Verify incoming Meta POST X-Hub-Signature-256 HMAC-SHA256 header.

    Header format: sha256=<hex_digest>
    """
    if not signature_header or not signature_header.startswith("sha256="):
        raise ForgedWebhookError("Missing or malformed X-Hub-Signature-256 header.")

    sig_hex = signature_header[len("sha256=") :]
    expected_hmac = hmac.new(
        app_secret.encode("utf-8"),
        payload_bytes,
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected_hmac.lower(), sig_hex.lower()):
        raise ForgedWebhookError("Forged or invalid WhatsApp X-Hub-Signature-256 signature.")

    return True


def parse_inbound_whatsapp_webhook(
    payload: dict[str, Any],
    custom_secrets: list[str] | None = None,
    max_single_attachment_bytes: int = 16 * 1024 * 1024,
) -> list[ChannelEnvelope]:
    """Parse Meta WhatsApp Cloud API webhook JSON payload into normalized ChannelEnvelopes."""
    envelopes: list[ChannelEnvelope] = []
    entries = payload.get("entry", [])
    for entry in entries:
        changes = entry.get("changes", [])
        for change in changes:
            val = change.get("value", {})
            metadata = val.get("metadata", {})
            phone_id = metadata.get("phone_number_id", "")
            recipient_phone = metadata.get("display_phone_number") or phone_id
            contacts = {
                c.get("wa_id"): c.get("profile", {}).get("name") for c in val.get("contacts", [])
            }
            messages = val.get("messages", [])

            for msg in messages:
                msg_id = msg.get("id", f"wamid_{uuid.uuid4().hex[:12]}")
                sender_phone = msg.get("from", "unknown")
                msg_type = msg.get("type", "text")
                msg_ts = float(msg.get("timestamp", time.time()))

                body_text = ""
                attachments: list[ChannelAttachment] = []

                if msg_type == "text":
                    body_text = msg.get("text", {}).get("body", "")
                elif msg_type in ("image", "document", "audio", "video"):
                    media_obj = msg.get(msg_type, {})
                    media_id = media_obj.get("id", "")
                    mime_type = media_obj.get("mime_type", f"{msg_type}/octet-stream")
                    caption = media_obj.get("caption", "")
                    filename = media_obj.get("filename", f"{msg_type}_{media_id}.bin")
                    file_size = int(media_obj.get("file_size", 0))

                    if file_size > max_single_attachment_bytes:
                        raise AttachmentSizeExceededError(
                            f"Inbound WhatsApp media '{filename}' exceeds maximum allowed size "
                            f"({file_size} > {max_single_attachment_bytes} bytes)."
                        )

                    attachments.append(
                        ChannelAttachment(
                            filename=filename,
                            mime_type=mime_type,
                            size_bytes=file_size,
                            artifact_id=media_id,
                            digest=media_obj.get("sha256"),
                        )
                    )
                    body_text = caption

                # Redact sensitive PII and secrets
                redacted_body = redact_text(body_text, custom_secrets=custom_secrets)

                envelope = ChannelEnvelope(
                    channel="whatsapp",
                    direction="inbound",
                    message_id=msg_id,
                    sender=sender_phone,
                    recipient=recipient_phone,
                    text_content=redacted_body,
                    attachments=attachments,
                    timestamp=msg_ts,
                    is_service_window_active=True,  # Inbound customer message starts 24h window
                    raw_metadata={
                        "contact_name": contacts.get(sender_phone, ""),
                        "phone_number_id": metadata.get("phone_number_id", ""),
                        "type": msg_type,
                    },
                )
                envelopes.append(envelope)

    return envelopes


def render_whatsapp_payload(envelope: ChannelEnvelope) -> dict[str, Any]:
    """Render WhatsApp Cloud API JSON payload conforming to Meta Graph API v17.0 contracts."""
    if envelope.template_name:
        return {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": envelope.recipient,
            "type": "template",
            "template": {
                "name": envelope.template_name,
                "language": {"code": envelope.template_language or "en_US"},
                "components": envelope.template_components or [],
            },
        }

    return {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": envelope.recipient,
        "type": "text",
        "text": {
            "preview_url": False,
            "body": envelope.text_content,
        },
    }


class WhatsAppAdapter:
    """Adapter for WhatsApp Cloud API with webhook verification, 24h window, and outbox."""

    def __init__(
        self,
        store: ChannelStore | None = None,
        app_secret: str | None = None,
        verify_token: str | None = None,
    ) -> None:
        self.store = store or MemoryChannelStore()
        self.app_secret = app_secret
        self.verify_token = verify_token

    def verify_challenge(self, mode: str, token: str, challenge: str) -> str:
        if not self.verify_token:
            raise ForgedWebhookError("Verify token not configured for WhatsApp webhook.")
        return verify_whatsapp_webhook_challenge(
            mode=mode,
            token=token,
            challenge=challenge,
            verify_token=self.verify_token,
        )

    def verify_signature(self, payload_bytes: bytes, signature_header: str) -> bool:
        if not self.app_secret:
            raise ForgedWebhookError("App secret not configured for WhatsApp signature check.")
        return verify_whatsapp_webhook_signature(
            payload_bytes=payload_bytes,
            signature_header=signature_header,
            app_secret=self.app_secret,
        )

    def parse_inbound(
        self,
        payload: dict[str, Any],
        custom_secrets: list[str] | None = None,
        max_single_attachment_bytes: int = 16 * 1024 * 1024,
    ) -> list[ChannelEnvelope]:
        return parse_inbound_whatsapp_webhook(
            payload=payload,
            custom_secrets=custom_secrets,
            max_single_attachment_bytes=max_single_attachment_bytes,
        )

    def send(
        self,
        envelope: ChannelEnvelope,
        credentials: dict[str, Any] | None = None,
        transport: Callable[[dict[str, Any]], Any] | None = None,
        max_retries: int = 3,
    ) -> OutboxReceipt:
        """Dispatch an outbound WhatsApp message or record in dry-run outbox.

        Enforces:
        - Effect approval gate
        - Credential revocation
        - WhatsApp 24-hour customer service window & template requirement
        - Customer opt-in / consent verification for proactive messages
        - Attachment size limits
        - Deduplication
        - Dry-run zero network dispatch
        - Safe retry bounds
        """
        # 1. Approval check
        if not envelope.approved:
            raise ApprovalRequiredError(
                f"Outbound WhatsApp message '{envelope.message_id}' requires approval."
            )

        # 2. Credential revocation check
        if credentials and (
            credentials.get("revoked") is True or credentials.get("is_revoked") is True
        ):
            raise CredentialRevokedError(
                f"WhatsApp API credential '{credentials.get('credential_id', 'unknown')}' "
                "has been revoked."
            )

        # 3. Customer Service Window & Template Enforcement
        # Freeform text outside 24h window is rejected
        if not envelope.is_service_window_active and not envelope.template_name:
            raise TemplateRequiredError(
                "WhatsApp Cloud API policy forbids freeform text messages outside "
                "the 24-hour customer service window. An approved template is required."
            )

        # 4. Proactive / Business-Initiated Consent Check
        # Outbound template messages require explicit customer consent verification
        if not envelope.is_service_window_active and not envelope.consent_verified:
            raise ConsentRequiredError(
                "Proactive outbound WhatsApp notification requires explicit customer "
                "consent verification."
            )

        # 5. Attachment limits
        enforce_attachment_limits(envelope.attachments, max_single_bytes=16 * 1024 * 1024)

        # 6. Deduplication
        dedup_key = envelope.idempotency_key or envelope.message_id
        if self.store.is_duplicate(dedup_key):
            existing = self.store.get_receipt_by_message_id(envelope.message_id)
            if existing:
                return existing
            return OutboxReceipt(
                receipt_id=f"rcpt_{uuid.uuid4().hex[:12]}",
                message_id=envelope.message_id,
                channel="whatsapp",
                recipient=envelope.recipient,
                status="deduplicated",
                dry_run=envelope.dry_run,
                rendered_content={},
                attempt_count=0,
            )

        # 7. Render WhatsApp Graph API JSON payload
        rendered_payload = render_whatsapp_payload(envelope)

        # 8. Dry-run outbox check: PERFORMS ZERO NETWORK REQUESTS
        if envelope.dry_run:
            receipt = OutboxReceipt(
                receipt_id=f"rcpt_{uuid.uuid4().hex[:12]}",
                message_id=envelope.message_id,
                channel="whatsapp",
                recipient=envelope.recipient,
                status="dry_run",
                dry_run=True,
                rendered_content=rendered_payload,
                attempt_count=1,
            )
            self.store.record_receipt(receipt)
            self.store.record_dedup(dedup_key, status="dry_run")
            return receipt

        # 9. Live / Mock Send with Retries
        receipt_id = f"rcpt_{uuid.uuid4().hex[:12]}"
        attempts = 0
        is_idempotent = bool(envelope.idempotency_key)
        effective_max_retries = max_retries if is_idempotent else 1

        for attempt in range(1, effective_max_retries + 1):
            attempts = attempt
            try:
                if transport:
                    transport(rendered_payload)
                break
            except Exception as exc:
                if not is_idempotent and attempt < max_retries:
                    raise NonIdempotentRetryForbiddenError(
                        "Automatic retry on WhatsApp dispatch is forbidden without idempotency."
                    ) from exc
                if attempt >= effective_max_retries:
                    raise exc

        receipt = OutboxReceipt(
            receipt_id=receipt_id,
            message_id=envelope.message_id,
            channel="whatsapp",
            recipient=envelope.recipient,
            status="delivered",
            dry_run=False,
            rendered_content=rendered_payload,
            attempt_count=attempts,
        )
        self.store.record_receipt(receipt)
        self.store.record_dedup(dedup_key, status="delivered")
        return receipt
