"""Generic webhook, Discord and Teams connector tests (httpx.MockTransport; no network)."""

from __future__ import annotations

import httpx
import pytest
from connector_helpers import TOKEN, Recorder, Sleeps, invoke, make_context
from nexura_agents.contracts.result import ModuleResult
from nexura_connectors import (
    DiscordConnector,
    TeamsConnector,
    WebhookConnector,
)

DISCORD_URL = f"https://discord.com/api/webhooks/123456/{TOKEN}"
TEAMS_URL = f"https://contoso.webhook.office.com/webhookb2/{TOKEN}/IncomingWebhook"
GENERIC = {"payload": {"a": 1}}


def ok(_: httpx.Request) -> httpx.Response:
    return httpx.Response(204)


@pytest.mark.parametrize(
    ("factory", "params"),
    [
        (DiscordConnector, {"content": "hi"}),
        (TeamsConnector, {"text": "hi"}),
    ],
)
async def test_vendor_webhook_sends_to_secret_url(factory: type, params: dict[str, str]) -> None:
    url = DISCORD_URL if factory is DiscordConnector else TEAMS_URL
    rec = Recorder(ok)
    res = await invoke(
        factory(transport=rec.transport),
        "send_message",
        params,
        approved=True,
        secrets={"cred": url},
    )
    assert res.status == "success", res.error_message
    assert str(rec.last.url) == url and rec.last.method == "POST"
    assert TOKEN not in str(res.outputs)


async def test_discord_disables_mentions_and_teams_builds_card() -> None:
    rec = Recorder(ok)
    await invoke(
        DiscordConnector(transport=rec.transport),
        "send_message",
        {"content": "@everyone", "username": "bot"},
        approved=True,
        secrets={"cred": DISCORD_URL},
    )
    assert rec.body() == {
        "content": "@everyone",
        "allowed_mentions": {"parse": []},
        "username": "bot",
    }
    await invoke(
        TeamsConnector(transport=rec.transport),
        "send_message",
        {"text": "t", "title": "T"},
        approved=True,
        secrets={"cred": TEAMS_URL},
    )
    assert rec.body()["@type"] == "MessageCard" and rec.body()["title"] == "T"


@pytest.mark.parametrize(
    ("factory", "params", "url"),
    [
        (DiscordConnector, {"content": "hi"}, DISCORD_URL),
        (TeamsConnector, {"text": "hi"}, TEAMS_URL),
        (WebhookConnector, GENERIC, "https://hooks.example.com/x/" + TOKEN),
    ],
)
class TestNegatives:
    def op(self, factory: type) -> str:
        return "send" if factory is WebhookConnector else "send_message"

    async def test_write_needs_approval(self, factory: type, params: dict, url: str) -> None:
        rec = Recorder(ok)
        for approved in (None, False):
            res = await invoke(
                factory(transport=rec.transport),
                self.op(factory),
                params,
                approved=approved,
                secrets={"cred": url},
            )
            assert res.error_code == "APPROVAL_REQUIRED"
        assert rec.requests == []

    async def test_missing_and_literal_credential(
        self, factory: type, params: dict, url: str
    ) -> None:
        rec = Recorder(ok)
        mod = factory(transport=rec.transport)
        res = await mod.execute(
            make_context({}, approval_granted=True),
            {"operation": self.op(factory), "params": params, "approved": True},
            {},
        )
        assert res.error_code == "MISSING_CREDENTIAL"
        res = await mod.execute(
            make_context({"x": "v"}, approval_granted=True),
            {"operation": self.op(factory), "params": params, "approved": True},
            {"credential_ref": "https://evil.example/hook"},
        )
        assert res.error_code == "MISSING_CREDENTIAL" and rec.requests == []

    async def test_ssrf_urls_in_secret_blocked(self, factory: type, params: dict, url: str) -> None:
        rec = Recorder(ok)
        for bad in (
            "https://169.254.169.254/latest/meta-data",
            "https://127.0.0.1/hook",
            "https://10.1.1.1/hook",
            "http://hooks.example.com/hook",
        ):
            res = await invoke(
                factory(transport=rec.transport),
                self.op(factory),
                params,
                approved=True,
                secrets={"cred": bad},
            )
            assert res.status == "failure" and res.error_code in {
                "SSRF_BLOCKED",
                "INVALID_PARAMS",
            }, bad
        assert rec.requests == []

    async def test_status_errors_never_leak_token_and_writes_not_retried(
        self, factory: type, params: dict, url: str
    ) -> None:
        for status, code in ((401, "AUTH_FAILED"), (429, "RATE_LIMITED"), (500, "UPSTREAM_ERROR")):
            rec = Recorder(lambda r, s=status: httpx.Response(s, text=f"bad {url} {TOKEN}"))
            sleeps = Sleeps()
            res = await invoke(
                factory(transport=rec.transport, sleep=sleeps),
                self.op(factory),
                params,
                approved=True,
                secrets={"cred": url},
            )
            assert res.error_code == code
            assert len(rec.requests) == 1 and sleeps.delays == []
            assert TOKEN not in f"{res.error_message} {res.metadata}"

    async def test_oversized_response_and_timeout(
        self, factory: type, params: dict, url: str
    ) -> None:
        rec = Recorder(lambda r: httpx.Response(200, content=b"x" * 5000))
        res = await invoke(
            factory(transport=rec.transport),
            self.op(factory),
            params,
            cfg={"max_response_bytes": 100},
            approved=True,
            secrets={"cred": url},
        )
        assert res.error_code == "RESPONSE_TOO_LARGE"

        def boom(r: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("t", request=r)

        rec2 = Recorder(boom)
        res = await invoke(
            factory(transport=rec2.transport),
            self.op(factory),
            params,
            approved=True,
            secrets={"cred": url},
        )
        assert res.status == "indeterminate" and len(rec2.requests) == 1

    async def test_redirect_is_not_followed(self, factory: type, params: dict, url: str) -> None:
        rec = Recorder(lambda r: httpx.Response(302, headers={"location": "https://127.0.0.1/"}))
        res = await invoke(
            factory(transport=rec.transport),
            self.op(factory),
            params,
            approved=True,
            secrets={"cred": url},
        )
        assert res.status == "failure" and len(rec.requests) == 1


async def test_vendor_webhooks_reject_foreign_hosts() -> None:
    rec = Recorder(ok)
    for factory, params in ((DiscordConnector, {"content": "x"}), (TeamsConnector, {"text": "x"})):
        res = await invoke(
            factory(transport=rec.transport),
            "send_message",
            params,
            approved=True,
            secrets={"cred": "https://evil.example.com/hook"},
        )
        assert res.error_code == "INVALID_PARAMS"
    assert rec.requests == []


async def test_vendor_webhook_foreign_host_allowed_only_when_listed() -> None:
    rec = Recorder(ok)
    res = await invoke(
        DiscordConnector(transport=rec.transport, allowed_hosts=["chat.corp.example.com"]),
        "send_message",
        {"content": "x"},
        approved=True,
        secrets={"cred": "https://chat.corp.example.com/hook/abc12345"},
    )
    assert res.status == "success"


async def test_validation_limits() -> None:
    rec = Recorder(ok)
    res = await invoke(
        DiscordConnector(transport=rec.transport),
        "send_message",
        {"content": "x" * 2001},
        approved=True,
        secrets={"cred": DISCORD_URL},
    )
    assert res.error_code == "INVALID_PARAMS" and rec.requests == []


async def _send_without_credential(mod: WebhookConnector, params: dict) -> ModuleResult:
    inputs = {"operation": "send", "params": params, "approved": True}
    return await mod.execute(make_context({}, approval_granted=True), inputs, {})


async def test_generic_webhook_url_param_headers_and_idempotency() -> None:
    rec = Recorder(ok)
    mod = WebhookConnector(transport=rec.transport)
    res = await _send_without_credential(
        mod,
        {
            "url": "https://hooks.example.com/in",
            "payload": {"k": "v"},
            "headers": {"X-Custom": "1", "Authorization": "Bearer stolen", "Host": "evil"},
            "idempotency_key": "abc",
        },
    )
    assert res.status == "success", res.error_message
    h = rec.last.headers
    assert h["x-custom"] == "1" and h["idempotency-key"] == "abc"
    assert "authorization" not in h and h["host"] == "hooks.example.com"
    assert rec.body() == {"k": "v"}


async def test_generic_webhook_needs_url_or_credential_and_blocks_private_url() -> None:
    rec = Recorder(ok)
    mod = WebhookConnector(transport=rec.transport)
    res = await _send_without_credential(mod, GENERIC)
    assert res.error_code == "MISSING_CREDENTIAL"
    for url in ("https://169.254.169.254/x", "https://192.168.0.1/x", "https://[::1]/x"):
        res = await _send_without_credential(mod, {**GENERIC, "url": url})
        assert res.error_code == "SSRF_BLOCKED", url
    assert rec.requests == []


async def test_manifests_mark_writes() -> None:
    for cls in (WebhookConnector, DiscordConnector, TeamsConnector):
        m = cls().manifest
        assert m.capabilities["writes"] and m.capabilities["requires_approval"]
        assert not m.capabilities["reads"]


async def test_generic_webhook_payload_is_capped() -> None:
    rec = Recorder(ok)
    mod = WebhookConnector(transport=rec.transport)
    big = {"payload": {"blob": "x" * 300_000}, "url": "https://hooks.example.com/x"}
    res = await invoke(mod, "send", big, approved=True)
    assert res.error_code == "INVALID_PARAMS"
    assert not rec.requests
