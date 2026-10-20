# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""B00/B01 module contract wrappers for Email and WhatsApp channel adapters."""

from __future__ import annotations

import json
from typing import Any

from nexura_agents.contracts.base import BaseModule
from nexura_agents.contracts.context import ExecutionContext
from nexura_agents.contracts.errors import ContractError
from nexura_agents.contracts.manifest import EffectPolicy, ModuleManifest
from nexura_agents.contracts.result import ModuleResult

from nexura_runtime.integrations.channels.email import EmailAdapter
from nexura_runtime.integrations.channels.errors import ChannelError
from nexura_runtime.integrations.channels.models import ChannelEnvelope
from nexura_runtime.integrations.channels.whatsapp import WhatsAppAdapter


class EmailChannelModule(BaseModule):
    """B00/B01 module for Email channel parsing, webhook verification, and outbound sending."""

    def __init__(
        self,
        adapter: EmailAdapter | None = None,
        *,
        module_id: str = "nexura.channel.email",
        version: str = "1.0.0",
        effect_policy: EffectPolicy = "non_idempotent_write",
    ) -> None:
        self.adapter = adapter or EmailAdapter(signing_key="default_mock_key")

        self._manifest = ModuleManifest(
            schema_version=1,
            module_id=module_id,
            version=version,
            kind="integration",
            title="Email Channel Adapter",
            description=(
                "Parses inbound MIME emails, verifies webhook authenticity, "
                "and dispatches outbound emails with dry-run outbox support."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "operation": {
                        "type": "string",
                        "enum": ["parse_inbound", "verify_webhook", "send_email"],
                    },
                    "raw_mime": {"type": "string"},
                    "timestamp": {"type": ["number", "string"]},
                    "token": {"type": "string"},
                    "signature": {"type": "string"},
                    "signing_key": {"type": "string"},
                    "sender": {"type": "string"},
                    "recipient": {"type": "string"},
                    "subject": {"type": "string"},
                    "text_content": {"type": "string"},
                    "html_content": {"type": "string"},
                    "dry_run": {"type": "boolean"},
                    "approved": {"type": "boolean", "description": "Ignored; see approval."},
                    "idempotency_key": {"type": "string"},
                },
                "required": ["operation"],
            },
            output_schema={
                "type": "object",
                "properties": {
                    "status": {"type": "string"},
                    "result": {"type": "object"},
                },
                "required": ["status"],
            },
            config_schema={"type": "object", "properties": {}, "additionalProperties": True},
            capabilities={"network": False, "streaming": False},
            effect_policy=effect_policy,
            write_operations=["send_email"],
        )

    @property
    def manifest(self) -> ModuleManifest:
        return self._manifest

    async def invoke(
        self,
        context: ExecutionContext,
        inputs: dict[str, Any],
        config: dict[str, Any],
    ) -> ModuleResult:
        operation = inputs.get("operation")
        try:
            if operation == "parse_inbound":
                raw_mime = inputs.get("raw_mime", "")
                envelope = self.adapter.parse_inbound(raw_mime)
                return ModuleResult.success(
                    {
                        "status": "success",
                        "result": envelope.model_dump(),
                    }
                )

            if operation == "verify_webhook":
                timestamp = inputs.get("timestamp", 0)
                token = inputs.get("token", "")
                signature = inputs.get("signature", "")
                verified = self.adapter.verify_webhook(
                    timestamp=timestamp,
                    token=token,
                    signature=signature,
                )
                return ModuleResult.success(
                    {
                        "status": "success",
                        "result": {"verified": verified},
                    }
                )

            if operation == "send_email":
                context.require_write_approval("sending an email")
                envelope = ChannelEnvelope(
                    channel="email",
                    direction="outbound",
                    message_id=inputs.get("idempotency_key", f"msg_{context.run_id}"),
                    sender=inputs.get("sender", "noreply@nexura.local"),
                    recipient=inputs.get("recipient", "customer@example.com"),
                    subject=inputs.get("subject", "Notification"),
                    text_content=inputs.get("text_content", ""),
                    html_content=inputs.get("html_content"),
                    dry_run=inputs.get("dry_run", True),
                    approved=True,  # approval is context.approval_granted, checked above
                    idempotency_key=inputs.get("idempotency_key"),
                )
                receipt = self.adapter.send(envelope)
                return ModuleResult.success(
                    {
                        "status": "success",
                        "result": receipt.model_dump(),
                    }
                )

            return ModuleResult.failure(
                error_code="INVALID_OPERATION",
                error_message=f"Unsupported operation '{operation}'.",
            )
        except ContractError as err:
            return ModuleResult.failure(error_code=err.code, error_message=str(err))
        except ChannelError as err:
            return ModuleResult.failure(
                error_code=err.__class__.__name__,
                error_message=str(err),
            )
        except Exception as err:
            return ModuleResult.failure(
                error_code="UNEXPECTED_ERROR",
                error_message=str(err),
            )


class WhatsAppChannelModule(BaseModule):
    """B00/B01 module for WhatsApp Cloud API webhooks, 24h window enforcement, and dispatch."""

    def __init__(
        self,
        adapter: WhatsAppAdapter | None = None,
        *,
        module_id: str = "nexura.channel.whatsapp",
        version: str = "1.0.0",
        effect_policy: EffectPolicy = "non_idempotent_write",
    ) -> None:
        self.adapter = adapter or WhatsAppAdapter(
            app_secret="mock_secret_key",
            verify_token="mock_token_123",
        )

        self._manifest = ModuleManifest(
            schema_version=1,
            module_id=module_id,
            version=version,
            kind="integration",
            title="WhatsApp Cloud API Adapter",
            description=(
                "Handles Meta WhatsApp Cloud API webhooks, 24h customer window enforcement, "
                "and template message dispatch with dry-run outbox support."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "operation": {
                        "type": "string",
                        "enum": [
                            "verify_challenge",
                            "verify_signature",
                            "parse_inbound",
                            "send_message",
                        ],
                    },
                    "mode": {"type": "string"},
                    "token": {"type": "string"},
                    "challenge": {"type": "string"},
                    "payload": {"type": "object"},
                    "payload_text": {"type": "string"},
                    "signature_header": {"type": "string"},
                    "recipient": {"type": "string"},
                    "text_content": {"type": "string"},
                    "template_name": {"type": "string"},
                    "template_language": {"type": "string"},
                    "template_components": {"type": "array"},
                    "dry_run": {"type": "boolean"},
                    "approved": {"type": "boolean", "description": "Ignored; see approval."},
                    "consent_verified": {"type": "boolean"},
                    "is_service_window_active": {"type": "boolean"},
                    "idempotency_key": {"type": "string"},
                },
                "required": ["operation"],
            },
            output_schema={
                "type": "object",
                "properties": {
                    "status": {"type": "string"},
                    "result": {"type": "object"},
                },
                "required": ["status"],
            },
            config_schema={"type": "object", "properties": {}, "additionalProperties": True},
            capabilities={"network": False, "streaming": False},
            effect_policy=effect_policy,
            write_operations=["send_message"],
        )

    @property
    def manifest(self) -> ModuleManifest:
        return self._manifest

    async def invoke(
        self,
        context: ExecutionContext,
        inputs: dict[str, Any],
        config: dict[str, Any],
    ) -> ModuleResult:
        operation = inputs.get("operation")
        try:
            if operation == "verify_challenge":
                challenge = self.adapter.verify_challenge(
                    mode=inputs.get("mode", ""),
                    token=inputs.get("token", ""),
                    challenge=inputs.get("challenge", ""),
                )
                return ModuleResult.success(
                    {
                        "status": "success",
                        "result": {"challenge": challenge},
                    }
                )

            if operation == "verify_signature":
                payload_text = inputs.get("payload_text")
                if payload_text is not None:
                    payload_bytes = payload_text.encode("utf-8")
                else:
                    payload_bytes = json.dumps(inputs.get("payload", {})).encode("utf-8")
                verified = self.adapter.verify_signature(
                    payload_bytes=payload_bytes,
                    signature_header=inputs.get("signature_header", ""),
                )
                return ModuleResult.success(
                    {
                        "status": "success",
                        "result": {"verified": verified},
                    }
                )

            if operation == "parse_inbound":
                payload = inputs.get("payload", {})
                envelopes = self.adapter.parse_inbound(payload)
                return ModuleResult.success(
                    {
                        "status": "success",
                        "result": {"envelopes": [e.model_dump() for e in envelopes]},
                    }
                )

            if operation == "send_message":
                context.require_write_approval("sending a WhatsApp message")
                envelope = ChannelEnvelope(
                    channel="whatsapp",
                    direction="outbound",
                    message_id=inputs.get("idempotency_key", f"wamsg_{context.run_id}"),
                    sender="system",
                    recipient=inputs.get("recipient", "15551234567"),
                    text_content=inputs.get("text_content", ""),
                    template_name=inputs.get("template_name"),
                    template_language=inputs.get("template_language"),
                    template_components=inputs.get("template_components"),
                    dry_run=inputs.get("dry_run", True),
                    approved=True,  # approval is context.approval_granted, checked above
                    consent_verified=inputs.get("consent_verified", False),
                    is_service_window_active=inputs.get("is_service_window_active", False),
                    idempotency_key=inputs.get("idempotency_key"),
                )
                receipt = self.adapter.send(envelope)
                return ModuleResult.success(
                    {
                        "status": "success",
                        "result": receipt.model_dump(),
                    }
                )

            return ModuleResult.failure(
                error_code="INVALID_OPERATION",
                error_message=f"Unsupported operation '{operation}'.",
            )
        except ContractError as err:
            return ModuleResult.failure(error_code=err.code, error_message=str(err))
        except ChannelError as err:
            return ModuleResult.failure(
                error_code=err.__class__.__name__,
                error_message=str(err),
            )
        except Exception as err:
            return ModuleResult.failure(
                error_code="UNEXPECTED_ERROR",
                error_message=str(err),
            )
