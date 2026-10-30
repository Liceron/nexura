# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Outbound webhooks: generic JSON webhook, Discord webhook and Microsoft Teams incoming webhook.

The webhook URL is itself a secret (it grants posting rights), so for Discord and Teams it is
held in the secrets store and referenced by ``credential_ref``. The generic webhook may take a
non-secret ``url`` parameter instead. Every URL goes through the outbound SSRF policy.
"""

from __future__ import annotations

from typing import Any, ClassVar
from urllib.parse import urlsplit

from pydantic import Field

from nexura_connectors._core import (
    Call,
    ConnectorModule,
    Op,
    StrictParams,
    check_request_size,
    result_data,
)
from nexura_connectors.errors import (
    InvalidParamsError,
    MissingCredentialError,
)

_FORBIDDEN_HEADERS = {"authorization", "cookie", "host", "content-length", "proxy-authorization"}


class WebhookSend(StrictParams):
    """Parameters for the generic webhook."""

    payload: dict[str, Any]
    url: str | None = Field(default=None, max_length=2048)
    headers: dict[str, str] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, max_length=128)


class DiscordSend(StrictParams):
    """Parameters for a Discord webhook message."""

    content: str = Field(min_length=1, max_length=2000)
    username: str | None = Field(default=None, max_length=80)


class TeamsSend(StrictParams):
    """Parameters for a Teams incoming-webhook message."""

    text: str = Field(min_length=1, max_length=20000)
    title: str | None = Field(default=None, max_length=256)


class _WebhookConnector(ConnectorModule):
    """Webhook connectors POST once to a URL; the URL comes from the credential."""

    HOST_SUFFIXES: ClassVar[tuple[str, ...]] = ()

    def webhook_url(self, call: Call, params: Any) -> str:
        """Return the target URL (the resolved credential)."""
        return call.secret

    def check_host(self, url: str, call: Call) -> None:
        """Restrict to the vendor's webhook hosts unless the operator allow-listed others."""
        if not self.HOST_SUFFIXES:
            return
        host = (urlsplit(url).hostname or "").lower()
        allowed = [h.lower() for h in call.http.allowed_hosts]
        if host in allowed or any(host == s or host.endswith("." + s) for s in self.HOST_SUFFIXES):
            return
        raise InvalidParamsError("webhook host is not a recognised vendor host")

    def body(self, params: Any) -> dict[str, Any]:
        """Build the JSON body to send."""
        raise NotImplementedError

    def extra_headers(self, params: Any) -> dict[str, str]:
        """Additional request headers."""
        return {}

    async def run(self, op: Op, params: Any, call: Call) -> dict[str, Any]:
        """POST the body to the webhook URL; never retried (write)."""
        url = self.webhook_url(call, params)
        self.check_host(url, call)
        # a webhook secret is the whole URL: pin the origin so it cannot be redirected elsewhere
        call.http.base_origin = url
        resp = await call.http.request(
            "POST",
            url,
            headers={"Content-Type": "application/json", **self.extra_headers(params)},
            json=self.body(params),
            write=True,
        )
        return result_data({"status_code": resp.status})


class WebhookConnector(_WebhookConnector):
    """POST a JSON payload to a webhook URL."""

    MODULE_ID: ClassVar[str] = "nexura.connectors.webhook"
    TITLE: ClassVar[str] = "Webhook (outbound)"
    DESCRIPTION: ClassVar[str] = (
        "POST JSON to a webhook URL. The URL is the credential (credential_ref) or a "
        "non-secret 'url' param; both pass the SSRF policy."
    )
    CREDENTIAL_TYPE: ClassVar[str] = "webhook_url"
    NEEDS_CREDENTIAL: ClassVar[bool] = False
    OPS: ClassVar[dict[str, Op]] = {"send": Op("send", True, WebhookSend)}

    def webhook_url(self, call: Call, params: WebhookSend) -> str:
        """Prefer the url param; otherwise use the credential reference."""
        if params.url:
            return params.url
        return call.secret

    async def run(self, op: Op, params: Any, call: Call) -> dict[str, Any]:
        """Resolve the URL lazily so a missing credential is reported precisely."""
        if not params.url and not call.secret:
            raise MissingCredentialError("provide config 'credential_ref' or a 'url' param")
        return await super().run(op, params, call)

    def body(self, params: WebhookSend) -> dict[str, Any]:
        """The caller's payload, capped at MAX_REQUEST_BYTES of JSON."""
        check_request_size(params.payload, "webhook payload")
        return params.payload

    def extra_headers(self, params: WebhookSend) -> dict[str, str]:
        """Caller headers minus auth-bearing ones, plus the idempotency key."""
        headers = {k: v for k, v in params.headers.items() if k.lower() not in _FORBIDDEN_HEADERS}
        if params.idempotency_key:
            headers["Idempotency-Key"] = params.idempotency_key
        return headers

    def resolve_secret(self, context: Any, cfg: dict[str, Any]) -> str:
        """Credential is optional here: a ``url`` param may stand in for it."""
        if not cfg.get("credential_ref"):
            return ""
        return super().resolve_secret(context, cfg)


class DiscordConnector(_WebhookConnector):
    """Send a message through a Discord webhook."""

    MODULE_ID: ClassVar[str] = "nexura.connectors.discord"
    TITLE: ClassVar[str] = "Discord (webhook)"
    DESCRIPTION: ClassVar[str] = "Discord webhook; the webhook URL is held in the secrets store."
    CREDENTIAL_TYPE: ClassVar[str] = "discord_webhook_url"
    HOST_SUFFIXES: ClassVar[tuple[str, ...]] = ("discord.com", "discordapp.com")
    OPS: ClassVar[dict[str, Op]] = {"send_message": Op("send_message", True, DiscordSend)}

    def body(self, params: DiscordSend) -> dict[str, Any]:
        """Discord execute-webhook body with mentions disabled."""
        body: dict[str, Any] = {
            "content": params.content,
            "allowed_mentions": {"parse": []},
        }
        if params.username:
            body["username"] = params.username
        return body


class TeamsConnector(_WebhookConnector):
    """Send a message through a Microsoft Teams incoming webhook."""

    MODULE_ID: ClassVar[str] = "nexura.connectors.teams"
    TITLE: ClassVar[str] = "Microsoft Teams (incoming webhook)"
    DESCRIPTION: ClassVar[str] = "Teams incoming webhook; the URL is held in the secrets store."
    CREDENTIAL_TYPE: ClassVar[str] = "teams_webhook_url"
    HOST_SUFFIXES: ClassVar[tuple[str, ...]] = (
        "webhook.office.com",
        "logic.azure.com",
        "office.com",
    )
    OPS: ClassVar[dict[str, Op]] = {"send_message": Op("send_message", True, TeamsSend)}

    def body(self, params: TeamsSend) -> dict[str, Any]:
        """MessageCard body."""
        card: dict[str, Any] = {
            "@type": "MessageCard",
            "@context": "https://schema.org/extensions",
            "text": params.text,
        }
        if params.title:
            card["title"] = params.title
        return card
