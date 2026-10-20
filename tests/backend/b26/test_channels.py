"""Acceptance tests for Module B26: Business Channels (Email and WhatsApp Adapters)."""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any

import pytest
from nexura_runtime.contracts.context import ExecutionContext

from nexura_runtime.integrations.channels.email import (
    EmailAdapter,
    parse_inbound_mime,
    verify_email_webhook_signature,
)
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
from nexura_runtime.integrations.channels.module import (
    EmailChannelModule,
    WhatsAppChannelModule,
)
from nexura_runtime.integrations.channels.outbox import (
    MemoryChannelStore,
    SqliteChannelStore,
)
from nexura_runtime.integrations.channels.redaction import (
    create_attachment,
    enforce_attachment_limits,
)
from nexura_runtime.integrations.channels.whatsapp import (
    WhatsAppAdapter,
    parse_inbound_whatsapp_webhook,
    verify_whatsapp_webhook_challenge,
    verify_whatsapp_webhook_signature,
)
from nexura_runtime.modules.registry import default_registry


class TestEmailWebhookAndParsing:
    """Tests covering email webhook validation, parsing, redaction, and limits."""

    def test_signed_email_webhook_verification_and_forgery_rejection(self) -> None:
        key = "secret_signing_key_123"
        ts = str(int(time.time()))
        token = "token_abc123"
        valid_sig = hmac.new(
            key.encode("utf-8"),
            f"{ts}{token}".encode(),
            hashlib.sha256,
        ).hexdigest()

        # Valid signature succeeds
        assert verify_email_webhook_signature(ts, token, valid_sig, key) is True

        # Forged signature raises ForgedWebhookError
        with pytest.raises(ForgedWebhookError, match="Forged or invalid"):
            verify_email_webhook_signature(ts, token, "forged_hex_signature", key)

        # Expired replay window raises ForgedWebhookError
        stale_ts = str(int(time.time()) - 400)
        stale_sig = hmac.new(
            key.encode("utf-8"),
            f"{stale_ts}{token}".encode(),
            hashlib.sha256,
        ).hexdigest()
        with pytest.raises(ForgedWebhookError, match="outside tolerance replay window"):
            verify_email_webhook_signature(stale_ts, token, stale_sig, key, tolerance_seconds=300.0)

    def test_parse_inbound_mime_with_redaction_and_attachments(self) -> None:
        raw_mime = (
            "From: alice@example.com\r\n"
            "To: support@nexura.local\r\n"
            "Subject: Order Issue\r\n"
            "Message-ID: <msg_test_001@example.com>\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            "\r\n"
            "Hello, my credit card is 4532-1234-5678-9012 "
            "and token is Bearer secret_token_xyz987.\r\n"
        )
        envelope = parse_inbound_mime(raw_mime)
        assert envelope.channel == "email"
        assert envelope.direction == "inbound"
        assert envelope.sender == "alice@example.com"
        assert envelope.recipient == "support@nexura.local"
        assert envelope.subject == "Order Issue"
        # Verify PII was redacted
        assert "4532-1234-5678-9012" not in envelope.text_content
        assert "[REDACTED_CARD]" in envelope.text_content
        assert "secret_token_xyz987" not in envelope.text_content
        assert "Bearer [REDACTED]" in envelope.text_content


class TestWhatsAppWebhookAndParsing:
    """Tests covering WhatsApp Cloud API challenge, signature, and inbound parsing."""

    def test_whatsapp_challenge_verification_and_forgery(self) -> None:
        verify_token = "meta_verify_token_999"
        challenge = "random_challenge_string"

        # Valid challenge succeeds
        res = verify_whatsapp_webhook_challenge(
            mode="subscribe",
            token="meta_verify_token_999",
            challenge=challenge,
            verify_token=verify_token,
        )
        assert res == challenge

        # Bad mode or token rejected
        with pytest.raises(ForgedWebhookError):
            verify_whatsapp_webhook_challenge(
                mode="unsubscribe",
                token="meta_verify_token_999",
                challenge=challenge,
                verify_token=verify_token,
            )

        with pytest.raises(ForgedWebhookError):
            verify_whatsapp_webhook_challenge(
                mode="subscribe",
                token="wrong_token",
                challenge=challenge,
                verify_token=verify_token,
            )

    def test_whatsapp_x_hub_signature_verification_and_forgery(self) -> None:
        app_secret = "meta_app_secret_555"
        payload = b'{"object":"whatsapp_business_account"}'
        sig = hmac.new(app_secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
        valid_header = f"sha256={sig}"

        assert verify_whatsapp_webhook_signature(payload, valid_header, app_secret) is True

        # Malformed header
        with pytest.raises(ForgedWebhookError, match="Missing or malformed"):
            verify_whatsapp_webhook_signature(payload, "invalid_header", app_secret)

        # Forged signature
        with pytest.raises(ForgedWebhookError, match="Forged or invalid"):
            verify_whatsapp_webhook_signature(payload, "sha256=deadbeefcafe", app_secret)

    def test_parse_inbound_whatsapp_payload(self) -> None:
        payload = {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "WABA_ID_100",
                    "changes": [
                        {
                            "value": {
                                "messaging_product": "whatsapp",
                                "metadata": {
                                    "display_phone_number": "15550001111",
                                    "phone_number_id": "PN_ID_200",
                                },
                                "contacts": [
                                    {"wa_id": "919876543210", "profile": {"name": "Priya"}}
                                ],
                                "messages": [
                                    {
                                        "from": "919876543210",
                                        "id": "wamid.AB12CD34",
                                        "timestamp": "1770000000",
                                        "type": "text",
                                        "text": {"body": "नमस्ते, मेरा SSN 123-45-6789 है।"},
                                    }
                                ],
                            },
                            "field": "messages",
                        }
                    ],
                }
            ],
        }

        envelopes = parse_inbound_whatsapp_webhook(payload)
        assert len(envelopes) == 1
        env = envelopes[0]
        assert env.channel == "whatsapp"
        assert env.sender == "919876543210"
        assert env.recipient == "15550001111"
        assert env.is_service_window_active is True
        # Verify PII redacted
        assert "123-45-6789" not in env.text_content
        assert "[REDACTED_SSN]" in env.text_content


class TestDeduplicationAndDryRunGuarantees:
    """Tests covering deduplication, dry-run outbox, and zero external sends."""

    def test_dry_run_performs_zero_external_sends_email(self) -> None:
        store = MemoryChannelStore()
        adapter = EmailAdapter(store=store)

        call_count = 0

        def mock_transport(raw: bytes) -> None:
            nonlocal call_count
            call_count += 1

        env = ChannelEnvelope(
            channel="email",
            direction="outbound",
            message_id="msg_dry_run_01",
            sender="billing@nexura.local",
            recipient="client@example.com",
            subject="Invoice #1024",
            text_content="Your monthly invoice is attached.",
            dry_run=True,
            approved=True,
        )

        receipt = adapter.send(env, transport=mock_transport)
        assert receipt.status == "dry_run"
        assert receipt.dry_run is True
        # Zero external transport calls made!
        assert call_count == 0

        # Verified in store outbox
        outbox = store.list_outbox(dry_run=True)
        assert len(outbox) == 1
        assert outbox[0].message_id == "msg_dry_run_01"

    def test_dry_run_performs_zero_external_sends_whatsapp(self) -> None:
        store = MemoryChannelStore()
        adapter = WhatsAppAdapter(store=store)

        call_count = 0

        def mock_transport(payload: dict[str, Any]) -> None:
            nonlocal call_count
            call_count += 1

        env = ChannelEnvelope(
            channel="whatsapp",
            direction="outbound",
            message_id="wamsg_dry_run_02",
            sender="system",
            recipient="919876543210",
            text_content="नमस्ते! आपकी सहायता के लिए धन्यवाद।",
            is_service_window_active=True,
            dry_run=True,
            approved=True,
        )

        receipt = adapter.send(env, transport=mock_transport)
        assert receipt.status == "dry_run"
        assert receipt.dry_run is True
        assert call_count == 0

        # Outbox verifies rendered payload
        outbox = store.list_outbox(channel="whatsapp", dry_run=True)
        assert len(outbox) == 1
        assert outbox[0].rendered_content["messaging_product"] == "whatsapp"

    def test_duplicate_delivery_deduplicated_email(self) -> None:
        store = MemoryChannelStore()
        adapter = EmailAdapter(store=store)

        send_count = 0

        def mock_transport(raw: bytes) -> None:
            nonlocal send_count
            send_count += 1

        env = ChannelEnvelope(
            channel="email",
            direction="outbound",
            message_id="msg_idemp_100",
            sender="ops@nexura.local",
            recipient="dev@example.com",
            subject="Alert",
            text_content="System warning",
            idempotency_key="idemp_key_email_abc",
            dry_run=False,
            approved=True,
        )

        # First send
        r1 = adapter.send(env, transport=mock_transport)
        assert r1.status == "delivered"
        assert send_count == 1

        # Duplicate send with same idempotency key
        r2 = adapter.send(env, transport=mock_transport)
        assert r2.status in ("delivered", "deduplicated")
        # Ensure transport was NOT called a second time
        assert send_count == 1

    def test_duplicate_delivery_deduplicated_whatsapp(self) -> None:
        store = MemoryChannelStore()
        adapter = WhatsAppAdapter(store=store)

        send_count = 0

        def mock_transport(payload: dict[str, Any]) -> None:
            nonlocal send_count
            send_count += 1

        env = ChannelEnvelope(
            channel="whatsapp",
            direction="outbound",
            message_id="wamsg_idemp_200",
            sender="system",
            recipient="919876543210",
            text_content="Order confirmation",
            is_service_window_active=True,
            idempotency_key="idemp_key_wa_xyz",
            dry_run=False,
            approved=True,
        )

        r1 = adapter.send(env, transport=mock_transport)
        assert r1.status == "delivered"
        assert send_count == 1

        r2 = adapter.send(env, transport=mock_transport)
        assert r2.status in ("delivered", "deduplicated")
        assert send_count == 1


class TestRevocationAndApprovalPolicies:
    """Tests covering credential revocation and approval gates."""

    def test_revoked_credentials_explicit_email(self) -> None:
        adapter = EmailAdapter()
        env = ChannelEnvelope(
            channel="email",
            direction="outbound",
            message_id="msg_revoked_01",
            sender="a@nexura.local",
            recipient="b@example.com",
            text_content="Hello",
            approved=True,
        )

        revoked_creds = {
            "credential_id": "cred_smtp_prod",
            "revoked": True,
        }

        with pytest.raises(CredentialRevokedError, match="has been revoked"):
            adapter.send(env, credentials=revoked_creds)

    def test_revoked_credentials_explicit_whatsapp(self) -> None:
        adapter = WhatsAppAdapter()
        env = ChannelEnvelope(
            channel="whatsapp",
            direction="outbound",
            message_id="wamsg_revoked_02",
            sender="system",
            recipient="15551234567",
            text_content="Hello",
            is_service_window_active=True,
            approved=True,
        )

        revoked_creds = {
            "credential_id": "cred_wa_token",
            "is_revoked": True,
        }

        with pytest.raises(CredentialRevokedError, match="has been revoked"):
            adapter.send(env, credentials=revoked_creds)

    def test_unapproved_outbound_message_rejected(self) -> None:
        email_adapter = EmailAdapter()
        email_env = ChannelEnvelope(
            channel="email",
            direction="outbound",
            message_id="msg_unapproved_01",
            sender="a@nexura.local",
            recipient="b@example.com",
            text_content="Hello",
            approved=False,
        )
        with pytest.raises(ApprovalRequiredError, match="requires explicit approval"):
            email_adapter.send(email_env)

        wa_adapter = WhatsAppAdapter()
        wa_env = ChannelEnvelope(
            channel="whatsapp",
            direction="outbound",
            message_id="wamsg_unapproved_02",
            sender="system",
            recipient="15551234567",
            text_content="Hello",
            is_service_window_active=True,
            approved=False,
        )
        with pytest.raises(ApprovalRequiredError, match="requires approval"):
            wa_adapter.send(wa_env)


class TestWhatsAppPolicyAndConsentRequirements:
    """Tests covering WhatsApp 24h service window, templates, and consent verification."""

    def test_freeform_text_outside_24h_window_requires_template(self) -> None:
        adapter = WhatsAppAdapter()
        env = ChannelEnvelope(
            channel="whatsapp",
            direction="outbound",
            message_id="wamsg_outside_window",
            sender="system",
            recipient="15551234567",
            text_content="Hey, are you still interested?",
            is_service_window_active=False,  # Window closed!
            template_name=None,  # Missing template!
            approved=True,
        )

        with pytest.raises(TemplateRequiredError, match="An approved template is required"):
            adapter.send(env)

    def test_proactive_template_message_requires_consent(self) -> None:
        adapter = WhatsAppAdapter()
        env = ChannelEnvelope(
            channel="whatsapp",
            direction="outbound",
            message_id="wamsg_no_consent",
            sender="system",
            recipient="15551234567",
            template_name="order_status_update",
            template_language="en_US",
            is_service_window_active=False,
            consent_verified=False,  # Consent missing!
            approved=True,
        )

        with pytest.raises(ConsentRequiredError, match="explicit customer consent"):
            adapter.send(env)

    def test_proactive_template_with_consent_succeeds(self) -> None:
        adapter = WhatsAppAdapter()
        env = ChannelEnvelope(
            channel="whatsapp",
            direction="outbound",
            message_id="wamsg_valid_template",
            sender="system",
            recipient="15551234567",
            template_name="order_status_update",
            template_language="hi",
            template_components=[
                {"type": "body", "parameters": [{"type": "text", "text": "ORD-1234"}]}
            ],
            is_service_window_active=False,
            consent_verified=True,
            dry_run=True,
            approved=True,
        )

        receipt = adapter.send(env)
        assert receipt.status == "dry_run"
        assert receipt.rendered_content["template"]["name"] == "order_status_update"
        assert receipt.rendered_content["template"]["language"]["code"] == "hi"


class TestAttachmentLimitsAndDeliveryGuarantees:
    """Tests covering attachment byte bounding and delivery guarantees."""

    def test_attachment_size_exceeded_rejected(self) -> None:
        # Create attachment larger than limit
        big_data = b"0" * (12 * 1024 * 1024)  # 12 MB
        with pytest.raises(AttachmentSizeExceededError, match="exceeds maximum allowed size"):
            create_attachment(
                "large_file.pdf",
                "application/pdf",
                big_data,
                max_single_bytes=10 * 1024 * 1024,
            )

    def test_aggregate_attachments_size_exceeded_rejected(self) -> None:
        att1 = ChannelAttachment(
            filename="f1.pdf", mime_type="application/pdf", size_bytes=15 * 1024 * 1024
        )
        att2 = ChannelAttachment(
            filename="f2.pdf", mime_type="application/pdf", size_bytes=15 * 1024 * 1024
        )
        with pytest.raises(AttachmentSizeExceededError, match="Aggregate attachments size exceeds"):
            enforce_attachment_limits(
                [att1, att2],
                max_single_bytes=20 * 1024 * 1024,
                max_total_bytes=25 * 1024 * 1024,
            )

    def test_non_idempotent_retry_forbidden_without_idempotency_key(self) -> None:
        adapter = EmailAdapter()
        env = ChannelEnvelope(
            channel="email",
            direction="outbound",
            message_id="msg_no_idemp_retry",
            sender="a@nexura.local",
            recipient="b@example.com",
            text_content="Hello",
            idempotency_key=None,  # No idempotency key!
            dry_run=False,
            approved=True,
        )

        attempts = 0

        def failing_transport(raw: bytes) -> None:
            nonlocal attempts
            attempts += 1
            raise RuntimeError("Transient connection reset")

        with pytest.raises(
            NonIdempotentRetryForbiddenError, match="Automatic retry .* is forbidden"
        ):
            adapter.send(env, transport=failing_transport, max_retries=3)

        assert attempts == 1  # Stopped after 1 attempt, did not blindly retry!

    def test_retry_with_idempotency_key_succeeds_on_transient_failure(self) -> None:
        adapter = EmailAdapter()
        env = ChannelEnvelope(
            channel="email",
            direction="outbound",
            message_id="msg_with_idemp_retry",
            sender="a@nexura.local",
            recipient="b@example.com",
            text_content="Hello",
            idempotency_key="idemp_retry_success_key",
            dry_run=False,
            approved=True,
        )

        attempts = 0

        def flaky_transport(raw: bytes) -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("503 Service Unavailable")

        receipt = adapter.send(env, transport=flaky_transport, max_retries=3)
        assert receipt.status == "delivered"
        assert receipt.attempt_count == 2
        assert attempts == 2


class TestPersistentSqliteChannelStore:
    """Tests covering persistent SQLite channel store durability."""

    def test_sqlite_store_dedup_and_outbox_reopen(self, tmp_path: Any) -> None:
        db_path = str(tmp_path / "channels.db")
        store1 = SqliteChannelStore(db_path)

        # Record dedup key
        assert store1.record_dedup("key_123") is True
        assert store1.is_duplicate("key_123") is True
        assert store1.record_dedup("key_123") is False

        # Record receipt
        receipt = OutboxReceipt(
            receipt_id="rcpt_sqlite_01",
            message_id="msg_sqlite_01",
            channel="whatsapp",
            recipient="919876543210",
            status="dry_run",
            dry_run=True,
            rendered_content={"body": "Test message"},
            attempt_count=1,
        )
        store1.record_receipt(receipt)
        store1.close()

        # Reopen with fresh instance
        store2 = SqliteChannelStore(db_path)
        assert store2.is_duplicate("key_123") is True
        reopened_rcpt = store2.get_receipt("rcpt_sqlite_01")
        assert reopened_rcpt is not None
        assert reopened_rcpt.message_id == "msg_sqlite_01"
        assert reopened_rcpt.status == "dry_run"
        store2.close()


class TestModuleContractInvocation:
    """Tests covering BaseModule execution contracts and registry inclusion."""

    @pytest.mark.asyncio
    async def test_email_channel_module_execute(self) -> None:
        mod = EmailChannelModule()
        ctx = ExecutionContext(
            run_id="run_b26_email_test", node_id="node_email", approval_granted=True
        )

        # Test parse_inbound
        res = await mod.execute(
            ctx,
            {
                "operation": "parse_inbound",
                "raw_mime": (
                    "From: test@nexura.local\r\n"
                    "To: user@example.com\r\n"
                    "Subject: Hi\r\n\r\nHello there!"
                ),
            },
            {},
        )
        assert res.status == "success"
        assert res.outputs["result"]["sender"] == "test@nexura.local"

        # Test send_email (dry_run)
        res_send = await mod.execute(
            ctx,
            {
                "operation": "send_email",
                "sender": "noreply@nexura.local",
                "recipient": "user@example.com",
                "subject": "Greetings",
                "text_content": "Welcome to Nexura AgentKit",
                "dry_run": True,
            },
            {},
        )
        assert res_send.status == "success"
        assert res_send.outputs["result"]["status"] == "dry_run"

    @pytest.mark.asyncio
    async def test_whatsapp_channel_module_execute(self) -> None:
        mod = WhatsAppChannelModule()
        ctx = ExecutionContext(run_id="run_b26_wa_test", node_id="node_wa", approval_granted=True)

        # Test verify_challenge
        res = await mod.execute(
            ctx,
            {
                "operation": "verify_challenge",
                "mode": "subscribe",
                "token": "mock_token_123",
                "challenge": "challenge_456",
            },
            {},
        )
        assert res.status == "success"
        assert res.outputs["result"]["challenge"] == "challenge_456"

        # Test send_message (dry_run with active service window)
        res_send = await mod.execute(
            ctx,
            {
                "operation": "send_message",
                "recipient": "919876543210",
                "text_content": "नमस्ते! आपका स्वागत है।",
                "is_service_window_active": True,
                "dry_run": True,
            },
            {},
        )
        assert res_send.status == "success"
        assert res_send.outputs["result"]["status"] == "dry_run"

    def test_default_registry_contains_channel_modules(self) -> None:
        assert default_registry.has("nexura.channel.email")
        assert default_registry.has("nexura.channel.whatsapp")
