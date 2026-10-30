# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Email channel adapter conforming to B26 requirements."""

from __future__ import annotations

import email
import email.policy
import hashlib
import hmac
import time
import uuid
from collections.abc import Callable
from email.message import EmailMessage
from typing import Any

from nexura_runtime.integrations.channels.errors import (
    ApprovalRequiredError,
    CredentialRevokedError,
    ForgedWebhookError,
    NonIdempotentRetryForbiddenError,
)
from nexura_runtime.integrations.channels.models import (
    ChannelAttachment,
    ChannelEnvelope,
    OutboxReceipt,
)
from nexura_runtime.integrations.channels.outbox import ChannelStore, MemoryChannelStore
from nexura_runtime.integrations.channels.redaction import (
    create_attachment,
    enforce_attachment_limits,
    redact_text,
)


def verify_email_webhook_signature(
    timestamp: str | int | float,
    token: str,
    signature: str,
    signing_key: str,
    tolerance_seconds: float = 300.0,
    reference_timestamp: float | None = None,
) -> bool:
    """Verify inbound signed email webhook (e.g. Mailgun/SendGrid style HMAC-SHA256).

    Rejects forged signatures or replays exceeding tolerance window.
    """
    ref_ts = time.time() if reference_timestamp is None else reference_timestamp
    try:
        ts_float = float(timestamp)
    except (ValueError, TypeError) as err:
        raise ForgedWebhookError("Invalid or non-numeric webhook timestamp.") from err

    if abs(ref_ts - ts_float) > tolerance_seconds:
        raise ForgedWebhookError(
            f"Webhook timestamp {timestamp} is outside tolerance replay window of "
            f"{tolerance_seconds}s (skew: {abs(ref_ts - ts_float):.1f}s)."
        )

    # HMAC-SHA256 of timestamp + token
    expected_hmac = hmac.new(
        signing_key.encode("utf-8"),
        f"{timestamp}{token}".encode(),
        hashlib.sha256,
    ).hexdigest()

    # Compare constant-time
    if not hmac.compare_digest(expected_hmac.lower(), signature.lower()):
        raise ForgedWebhookError("Forged or invalid email webhook signature.")

    return True


def parse_inbound_mime(
    raw_mime: bytes | str,
    custom_secrets: list[str] | None = None,
    max_single_attachment_bytes: int = 10 * 1024 * 1024,
    max_total_attachment_bytes: int = 25 * 1024 * 1024,
) -> ChannelEnvelope:
    """Parse raw MIME email into normalized ChannelEnvelope with redaction and size checks."""
    if isinstance(raw_mime, str):
        msg = email.message_from_string(raw_mime, policy=email.policy.default)
    else:
        msg = email.message_from_bytes(raw_mime, policy=email.policy.default)

    msg_id = str(msg.get("Message-ID", f"msg_{uuid.uuid4().hex[:12]}"))
    sender = str(msg.get("From", "unknown@nexura.local"))
    recipient = str(msg.get("To", "inbox@nexura.local"))
    subject = str(msg.get("Subject", ""))

    text_parts: list[str] = []
    html_parts: list[str] = []
    attachments: list[ChannelAttachment] = []

    for part in msg.walk():
        content_disposition = str(part.get("Content-Disposition", ""))
        content_type = part.get_content_type()
        filename = part.get_filename()

        if filename or "attachment" in content_disposition:
            # Attachment handling
            payload = part.get_payload(decode=True)
            if not isinstance(payload, (bytes, bytearray)):
                continue
            att = create_attachment(
                filename=filename or f"attachment_{len(attachments) + 1}.bin",
                mime_type=content_type or "application/octet-stream",
                data=bytes(payload),
                max_single_bytes=max_single_attachment_bytes,
            )
            attachments.append(att)
        elif content_type == "text/plain":
            body = part.get_content()
            if isinstance(body, str):
                text_parts.append(body)
        elif content_type == "text/html":
            body = part.get_content()
            if isinstance(body, str):
                html_parts.append(body)

    # Check aggregate attachment limits
    enforce_attachment_limits(
        attachments,
        max_single_bytes=max_single_attachment_bytes,
        max_total_bytes=max_total_attachment_bytes,
    )

    full_text = "\n".join(text_parts).strip()
    full_html = "\n".join(html_parts).strip() if html_parts else None

    # Redact sensitive PII and secrets
    redacted_text = redact_text(full_text, custom_secrets=custom_secrets)
    redacted_html = redact_text(full_html, custom_secrets=custom_secrets) if full_html else None

    return ChannelEnvelope(
        channel="email",
        direction="inbound",
        message_id=msg_id,
        sender=sender,
        recipient=recipient,
        subject=subject,
        text_content=redacted_text,
        html_content=redacted_html,
        attachments=attachments,
        raw_metadata={"headers": {k: str(v) for k, v in msg.items()}},
    )


def render_mime_message(envelope: ChannelEnvelope) -> bytes:
    """Render a ChannelEnvelope into a standard RFC 2822 MIME message as bytes."""
    msg = EmailMessage()
    msg["From"] = envelope.sender
    msg["To"] = envelope.recipient
    if envelope.subject:
        msg["Subject"] = envelope.subject
    msg["Message-ID"] = (
        envelope.message_id
        if envelope.message_id.startswith("<")
        else f"<{envelope.message_id}@nexura.local>"
    )

    msg.set_content(envelope.text_content)
    if envelope.html_content:
        msg.add_alternative(envelope.html_content, subtype="html")

    for att in envelope.attachments:
        if att.data:
            maintype, _, subtype = att.mime_type.partition("/")
            msg.add_attachment(
                att.data,
                maintype=maintype or "application",
                subtype=subtype or "octet-stream",
                filename=att.filename,
            )

    return msg.as_bytes()


class EmailAdapter:
    """Adapter for sending, receiving, and verifying emails with outbox and deduplication."""

    def __init__(
        self,
        store: ChannelStore | None = None,
        signing_key: str | None = None,
    ) -> None:
        self.store = store or MemoryChannelStore()
        self.signing_key = signing_key

    def verify_webhook(
        self,
        timestamp: str | int | float,
        token: str,
        signature: str,
        tolerance_seconds: float = 300.0,
        reference_timestamp: float | None = None,
    ) -> bool:
        if not self.signing_key:
            raise ForgedWebhookError("Signing key not configured for email webhook verification.")
        return verify_email_webhook_signature(
            timestamp=timestamp,
            token=token,
            signature=signature,
            signing_key=self.signing_key,
            tolerance_seconds=tolerance_seconds,
            reference_timestamp=reference_timestamp,
        )

    def parse_inbound(
        self,
        raw_mime: bytes | str,
        custom_secrets: list[str] | None = None,
        max_single_attachment_bytes: int = 10 * 1024 * 1024,
        max_total_attachment_bytes: int = 25 * 1024 * 1024,
    ) -> ChannelEnvelope:
        envelope = parse_inbound_mime(
            raw_mime=raw_mime,
            custom_secrets=custom_secrets,
            max_single_attachment_bytes=max_single_attachment_bytes,
            max_total_attachment_bytes=max_total_attachment_bytes,
        )
        return envelope

    def send(
        self,
        envelope: ChannelEnvelope,
        credentials: dict[str, Any] | None = None,
        transport: Callable[[bytes], Any] | None = None,
        max_retries: int = 3,
    ) -> OutboxReceipt:
        """Send an outbound email or record in dry-run outbox.

        Enforces:
        - Effect approval gate
        - Credential revocation
        - Attachment byte limits
        - Idempotent deduplication
        - Zero sends during dry-run
        - Safe retry bounds
        """
        # 1. Approval check
        if not envelope.approved:
            raise ApprovalRequiredError(
                f"Outbound email '{envelope.message_id}' requires explicit approval."
            )

        # 2. Credential revocation check
        if credentials and (
            credentials.get("revoked") is True or credentials.get("is_revoked") is True
        ):
            raise CredentialRevokedError(
                f"Email credential '{credentials.get('credential_id', 'unknown')}' "
                "has been revoked."
            )

        # 3. Attachment limits check
        enforce_attachment_limits(envelope.attachments)

        # 4. Deduplication check
        dedup_key = envelope.idempotency_key or envelope.message_id
        if self.store.is_duplicate(dedup_key):
            existing = self.store.get_receipt_by_message_id(envelope.message_id)
            if existing:
                return existing
            receipt_id = f"rcpt_{uuid.uuid4().hex[:12]}"
            return OutboxReceipt(
                receipt_id=receipt_id,
                message_id=envelope.message_id,
                channel="email",
                recipient=envelope.recipient,
                status="deduplicated",
                dry_run=envelope.dry_run,
                rendered_content={},
                attempt_count=0,
            )

        # 5. Render message
        raw_mime = render_mime_message(envelope)
        rendered_meta = {
            "from": envelope.sender,
            "to": envelope.recipient,
            "subject": envelope.subject,
            "text": envelope.text_content,
            "html": bool(envelope.html_content),
            "attachments_count": len(envelope.attachments),
            "raw_mime_size_bytes": len(raw_mime),
        }

        # 6. Dry-run outbox check: PERFORMS ZERO SENDS
        if envelope.dry_run:
            receipt = OutboxReceipt(
                receipt_id=f"rcpt_{uuid.uuid4().hex[:12]}",
                message_id=envelope.message_id,
                channel="email",
                recipient=envelope.recipient,
                status="dry_run",
                dry_run=True,
                rendered_content=rendered_meta,
                attempt_count=1,
            )
            self.store.record_receipt(receipt)
            self.store.record_dedup(dedup_key, status="dry_run")
            return receipt

        # 7. Live / Mock Send with Retries
        receipt_id = f"rcpt_{uuid.uuid4().hex[:12]}"
        attempts = 0

        # Check non-idempotent retry rule
        is_idempotent = bool(envelope.idempotency_key)
        effective_max_retries = max_retries if is_idempotent else 1

        for attempt in range(1, effective_max_retries + 1):
            attempts = attempt
            try:
                if transport:
                    transport(raw_mime)
                break
            except Exception as exc:
                # Check if retry is forbidden without idempotency
                if not is_idempotent and attempt < max_retries:
                    raise NonIdempotentRetryForbiddenError(
                        "Automatic retry on email send is forbidden without an idempotency key."
                    ) from exc
                if attempt >= effective_max_retries:
                    raise exc

        receipt = OutboxReceipt(
            receipt_id=receipt_id,
            message_id=envelope.message_id,
            channel="email",
            recipient=envelope.recipient,
            status="delivered",
            dry_run=False,
            rendered_content=rendered_meta,
            attempt_count=attempts,
        )
        self.store.record_receipt(receipt)
        self.store.record_dedup(dedup_key, status="delivered")
        return receipt
