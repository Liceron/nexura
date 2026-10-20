"""SMTP and IMAP connector tests (in-memory fake clients; no sockets)."""

from __future__ import annotations

from connector_helpers import Sleeps, invoke, make_context
from nexura_connectors import (
    AuthFailedError,
    FakeImapClient,
    FakeSmtpClient,
    ImapConnector,
    SmtpConnector,
)
from nexura_connectors.errors import (
    ConnectorTransportError,
    IndeterminateEffectError,
)

PASSWORD = "p4ssw0rd_Distinct_Value"
LOGIN = f"user@example.com:{PASSWORD}"
CFG = {"host": "8.8.8.8"}  # public IP literal: passes SSRF policy without DNS
MSG = {"sender": "a@example.com", "to": ["b@example.com"], "subject": "Hi", "text": "body"}


def smtp(fake: FakeSmtpClient | None = None) -> tuple[SmtpConnector, FakeSmtpClient]:
    fake = fake or FakeSmtpClient()
    return SmtpConnector(client=fake, sleep=Sleeps()), fake


def imap(fake: FakeImapClient | None = None) -> tuple[ImapConnector, FakeImapClient]:
    fake = fake or FakeImapClient()
    return ImapConnector(client=fake, sleep=Sleeps()), fake


async def test_manifests() -> None:
    s, i = SmtpConnector().manifest, ImapConnector().manifest
    assert s.capabilities["writes"] and s.capabilities["requires_approval"]
    assert i.capabilities["reads"] and not i.capabilities["writes"]
    assert s.required_credential_types == ["smtp_login"]
    assert i.required_credential_types == ["imap_login"]


async def test_smtp_sends_after_approval_with_resolved_login() -> None:
    mod, fake = smtp()
    res = await invoke(
        mod,
        "send_message",
        {**MSG, "html": "<b>x</b>"},
        cfg=CFG,
        approved=True,
        secrets={"cred": LOGIN},
    )
    assert res.status == "success", res.error_message
    assert res.outputs["data"] == {"accepted": 1}
    sent = fake.sent[0]
    assert sent["Subject"] == "Hi" and sent.is_multipart()
    assert fake.logins[0].username == "user@example.com" and fake.logins[0].port == 465


async def test_smtp_write_requires_approval() -> None:
    mod, fake = smtp()
    for approved in (None, False):
        res = await invoke(
            mod, "send_message", MSG, cfg=CFG, approved=approved, secrets={"cred": LOGIN}
        )
        assert res.error_code == "APPROVAL_REQUIRED"
    assert fake.sent == [] and fake.logins == []


async def test_smtp_header_injection_and_bad_addresses() -> None:
    mod, fake = smtp()
    bad = [
        {**MSG, "subject": "Hi\r\nBcc: evil@example.com"},
        {**MSG, "to": ["b@example.com\r\nBcc: e@example.com"]},
        {**MSG, "sender": "not-an-address"},
        {**MSG, "to": []},
    ]
    for params in bad:
        res = await invoke(
            mod, "send_message", params, cfg=CFG, approved=True, secrets={"cred": LOGIN}
        )
        assert res.error_code == "INVALID_PARAMS", params
    assert fake.sent == []


async def test_credentials_by_reference_only() -> None:
    mod, fake = smtp()
    ctx = make_context({"cred": LOGIN}, approval_granted=True)
    inputs = {"operation": "send_message", "params": MSG, "approved": True}
    res = await mod.execute(ctx, inputs, dict(CFG))
    assert res.error_code == "MISSING_CREDENTIAL"
    res = await mod.execute(ctx, inputs, {**CFG, "credential_ref": LOGIN})
    assert res.error_code == "MISSING_CREDENTIAL" and PASSWORD not in (res.error_message or "")
    res = await invoke(mod, "send_message", MSG, cfg=CFG, approved=True, secrets={})
    assert res.error_code == "MISSING_CREDENTIAL"
    res = await invoke(
        mod, "send_message", MSG, cfg=CFG, approved=True, secrets={"cred": "nocolon"}
    )
    assert res.error_code == "MISSING_CREDENTIAL" and fake.sent == []


async def test_ssrf_on_mail_host() -> None:
    mod, fake = smtp()
    for host in ("169.254.169.254", "127.0.0.1", "10.0.0.8", "192.168.1.9", "::1"):
        res = await invoke(
            mod, "send_message", MSG, cfg={"host": host}, approved=True, secrets={"cred": LOGIN}
        )
        assert res.error_code == "SSRF_BLOCKED", host
    res = await invoke(mod, "send_message", MSG, cfg={}, approved=True, secrets={"cred": LOGIN})
    assert res.error_code == "INVALID_CONFIG"
    assert fake.logins == []


async def test_smtp_errors_are_typed_and_redacted() -> None:
    cases = [
        (AuthFailedError(f"rejected {PASSWORD}"), "failure", "AUTH_FAILED"),
        (ConnectorTransportError(f"down {PASSWORD}"), "failure", "UPSTREAM_UNAVAILABLE"),
    ]
    for exc, status, code in cases:
        mod, fake = smtp(FakeSmtpClient(error=exc))
        res = await invoke(
            mod, "send_message", MSG, cfg=CFG, approved=True, secrets={"cred": LOGIN}
        )
        assert res.status == status and res.error_code == code
        assert PASSWORD not in (res.error_message or "")
        assert len(fake.logins) == 1, "writes must never be retried"
    mod, fake = smtp(FakeSmtpClient(error=IndeterminateEffectError(f"timeout {PASSWORD}")))
    res = await invoke(mod, "send_message", MSG, cfg=CFG, approved=True, secrets={"cred": LOGIN})
    assert res.status == "indeterminate" and PASSWORD not in (res.error_message or "")
    assert len(fake.logins) == 1


async def test_imap_lists_newest_messages_read_only_no_approval_needed() -> None:
    box = [{"uid": str(n), "subject": f"s{n}"} for n in range(5)]
    mod, fake = imap(FakeImapClient(mailboxes={"INBOX": box}))
    res = await invoke(mod, "list_messages", {"limit": 2}, cfg=CFG, secrets={"cred": LOGIN})
    assert res.status == "success" and res.outputs["write"] is False
    assert [m["uid"] for m in res.outputs["data"]] == ["3", "4"]
    assert res.outputs["truncated"] is True
    assert fake.calls == [("INBOX", "UNSEEN")]


async def test_imap_param_validation() -> None:
    mod, fake = imap()
    for params in (
        {"limit": 500},
        {"criteria": "ALL; DELETE"},
        {"mailbox": 'INBOX"\r\nA1 DELETE'},
        {"expunge": True},
    ):
        res = await invoke(mod, "list_messages", params, cfg=CFG, secrets={"cred": LOGIN})
        assert res.error_code == "INVALID_PARAMS", params
    assert fake.calls == []


async def test_imap_retries_reads_then_succeeds_and_exhausts() -> None:
    class Flaky(FakeImapClient):
        async def fetch(self, *a, **k):  # type: ignore[no-untyped-def]
            self.calls.append(("x", "y"))
            if len(self.calls) < 3:
                raise ConnectorTransportError("blip")
            return [{"uid": "1"}]

    mod, fake = imap(Flaky())
    res = await invoke(mod, "list_messages", {}, cfg=CFG, secrets={"cred": LOGIN})
    assert res.status == "success" and len(fake.calls) == 3
    mod, fake = imap(FakeImapClient(error=ConnectorTransportError(f"x {PASSWORD}")))
    res = await invoke(mod, "list_messages", {}, cfg=CFG, secrets={"cred": LOGIN})
    assert res.error_code == "UPSTREAM_UNAVAILABLE" and len(fake.calls) == 3
    assert PASSWORD not in (res.error_message or "")


async def test_imap_auth_failure_not_retried_and_ssrf() -> None:
    mod, fake = imap(FakeImapClient(error=AuthFailedError("bad login")))
    res = await invoke(mod, "list_messages", {}, cfg=CFG, secrets={"cred": LOGIN})
    assert res.error_code == "AUTH_FAILED" and len(fake.calls) == 1
    mod, fake = imap()
    res = await invoke(
        mod, "list_messages", {}, cfg={"host": "169.254.169.254"}, secrets={"cred": LOGIN}
    )
    assert res.error_code == "SSRF_BLOCKED" and fake.calls == []
