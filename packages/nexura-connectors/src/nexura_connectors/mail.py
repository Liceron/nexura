# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""SMTP send and IMAP read connectors over small protocols (stdlib real clients, fakes for tests).

Credentials are ``username:password`` held in the secrets store and referenced by id. The mail
server host goes through the outbound SSRF policy; hosts on private networks need an explicit
``allowed_hosts`` entry. Real clients use the standard library (``smtplib``/``imaplib``) over TLS.
The host is resolved once, every address is validated, and the client connects to that IP while
TLS still verifies the certificate against the original host name (no DNS rebinding gap).
"""

from __future__ import annotations

import asyncio
import imaplib
import re
import smtplib
import ssl
from dataclasses import dataclass, field
from email import message_from_bytes
from email.message import EmailMessage
from email.policy import default as default_policy
from typing import Any, ClassVar, Protocol

from pydantic import Field

from nexura_connectors._core import (
    Call,
    ConnectorModule,
    Op,
    StrictParams,
    pinned_ssl_context,
    resolve_validated_address,
    result_data,
)
from nexura_connectors.errors import (
    AuthFailedError,
    ConnectorTimeoutError,
    ConnectorTransportError,
    IndeterminateEffectError,
    InvalidConfigError,
    InvalidParamsError,
    MissingCredentialError,
)

_ADDR = re.compile(r"^[^\s<>,;\r\n]+@[^\s<>,;\r\n]+$")
MAX_BODY_BYTES = 200_000
MAX_FETCH_BYTES = 1_000_000
MAX_LIST = 50


@dataclass
class MailLogin:
    """Resolved server settings and login for one invocation."""

    host: str
    port: int
    username: str
    password: str
    timeout: float
    address: str | None = None  # validated IP to connect to; ``host`` stays the TLS name


class SmtpClient(Protocol):
    """Protocol for sending one message."""

    async def send(self, login: MailLogin, message: EmailMessage) -> None:
        """Deliver the message or raise a ConnectorError."""
        ...


class ImapClient(Protocol):
    """Protocol for reading messages from a mailbox (read-only)."""

    async def fetch(
        self, login: MailLogin, mailbox: str, criteria: str, limit: int, max_bytes: int
    ) -> list[dict[str, Any]]:
        """Return up to ``limit`` newest messages as header/summary dicts."""
        ...


class StdlibSmtpClient:
    """Real SMTP client (implicit TLS on 465, STARTTLS otherwise) run in a worker thread."""

    async def send(self, login: MailLogin, message: EmailMessage) -> None:
        """Send via smtplib; auth failure and timeouts map to typed errors."""
        await asyncio.to_thread(self._send, login, message)

    def _send(self, login: MailLogin, message: EmailMessage) -> None:
        target = login.address or login.host
        context: ssl.SSLContext = (
            pinned_ssl_context(login.host) if login.address else ssl.create_default_context()
        )
        try:
            if login.port == 465:
                server: smtplib.SMTP = smtplib.SMTP_SSL(
                    target, login.port, timeout=login.timeout, context=context
                )
            else:
                server = smtplib.SMTP(target, login.port, timeout=login.timeout)
                server.starttls(context=context)
            with server:
                server.login(login.username, login.password)
                server.send_message(message)
        except smtplib.SMTPAuthenticationError:
            raise AuthFailedError("SMTP server rejected the credential") from None
        except TimeoutError:
            raise IndeterminateEffectError("SMTP timed out; delivery outcome unknown") from None
        except (smtplib.SMTPException, OSError) as exc:
            raise ConnectorTransportError(f"SMTP failed ({type(exc).__name__})") from None


class StdlibImapClient:
    """Real IMAP client (TLS) that selects the mailbox read-only and peeks headers."""

    async def fetch(
        self, login: MailLogin, mailbox: str, criteria: str, limit: int, max_bytes: int
    ) -> list[dict[str, Any]]:
        """Search and fetch headers via imaplib in a worker thread."""
        return await asyncio.to_thread(self._fetch, login, mailbox, criteria, limit, max_bytes)

    def _fetch(
        self, login: MailLogin, mailbox: str, criteria: str, limit: int, max_bytes: int
    ) -> list[dict[str, Any]]:
        try:
            target = login.address or login.host
            context: ssl.SSLContext = (
                pinned_ssl_context(login.host) if login.address else ssl.create_default_context()
            )
            with imaplib.IMAP4_SSL(
                target, login.port, ssl_context=context, timeout=login.timeout
            ) as imap:
                imap.login(login.username, login.password)
                imap.select(f'"{mailbox}"', readonly=True)
                _, data = imap.uid("SEARCH", None, criteria)  # type: ignore[arg-type]
                uids = (data[0] or b"").split()[-limit:]
                out: list[dict[str, Any]] = []
                used = 0
                for uid in reversed(uids):
                    _, parts = imap.uid(
                        "FETCH", uid.decode(), "(BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE)])"
                    )
                    raw = next((p[1] for p in parts if isinstance(p, tuple)), b"")
                    used += len(raw)
                    if used > max_bytes:
                        break
                    msg = message_from_bytes(raw, policy=default_policy)
                    out.append(
                        {
                            "uid": uid.decode(),
                            "from": str(msg["from"] or ""),
                            "to": str(msg["to"] or ""),
                            "subject": str(msg["subject"] or ""),
                            "date": str(msg["date"] or ""),
                        }
                    )
                return out
        except imaplib.IMAP4.error:
            raise AuthFailedError("IMAP server rejected the login or request") from None
        except TimeoutError:
            raise ConnectorTimeoutError("IMAP timed out") from None
        except OSError as exc:
            raise ConnectorTransportError(f"IMAP failed ({type(exc).__name__})") from None


@dataclass
class FakeSmtpClient:
    """Test fake: records messages instead of sending them."""

    sent: list[EmailMessage] = field(default_factory=list)
    logins: list[MailLogin] = field(default_factory=list)
    error: Exception | None = None

    async def send(self, login: MailLogin, message: EmailMessage) -> None:
        """Record the message or raise the configured error."""
        self.logins.append(login)
        if self.error is not None:
            raise self.error
        self.sent.append(message)


@dataclass
class FakeImapClient:
    """Test fake: serves canned messages per mailbox."""

    mailboxes: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    calls: list[tuple[str, str]] = field(default_factory=list)
    error: Exception | None = None

    async def fetch(
        self, login: MailLogin, mailbox: str, criteria: str, limit: int, max_bytes: int
    ) -> list[dict[str, Any]]:
        """Return the newest ``limit`` canned messages."""
        self.calls.append((mailbox, criteria))
        if self.error is not None:
            raise self.error
        return list(self.mailboxes.get(mailbox, []))[-limit:]


class SendMessage(StrictParams):
    """Parameters for sending an email."""

    sender: str = Field(max_length=320)
    to: list[str] = Field(min_length=1, max_length=50)
    subject: str = Field(max_length=500)
    text: str = Field(max_length=MAX_BODY_BYTES)
    html: str | None = Field(default=None, max_length=MAX_BODY_BYTES)


class ListMessages(StrictParams):
    """Parameters for listing messages."""

    mailbox: str = Field(default="INBOX", min_length=1, max_length=100, pattern=r"^[^\r\n\"]+$")
    criteria: str = Field(default="UNSEEN", pattern=r"^(ALL|UNSEEN|SEEN|RECENT|FLAGGED|ANSWERED)$")
    limit: int = Field(default=20, ge=1, le=MAX_LIST)


def _login(call: Call, default_port: int) -> MailLogin:
    host = call.cfg.get("host")
    if not host or not isinstance(host, str):
        raise InvalidConfigError("config 'host' is required")
    user, sep, password = call.secret.partition(":")
    if not sep or not user or not password:
        raise MissingCredentialError("credential must be 'username:password'")
    return MailLogin(
        host=host,
        port=int(call.cfg.get("port") or default_port),
        username=user,
        password=password,
        timeout=call.http.timeout,
    )


async def _check_host(call: Call, login: MailLogin) -> None:
    """Resolve once, validate every address, and pin the login to the first validated IP."""
    ip = await asyncio.to_thread(
        resolve_validated_address, login.host, login.port, call.http.allowed_hosts
    )
    login.address = str(ip)


class SmtpConnector(ConnectorModule):
    """Send email over SMTP with TLS."""

    MODULE_ID: ClassVar[str] = "nexura.connectors.smtp"
    TITLE: ClassVar[str] = "Email (SMTP send)"
    DESCRIPTION: ClassVar[str] = (
        "SMTP over TLS. Credential 'username:password'; config 'host' (and optional 'port')."
    )
    CREDENTIAL_TYPE: ClassVar[str] = "smtp_login"
    OPS: ClassVar[dict[str, Op]] = {"send_message": Op("send_message", True, SendMessage)}

    def __init__(self, client: SmtpClient | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.client: SmtpClient = client or StdlibSmtpClient()

    async def run(self, op: Op, params: SendMessage, call: Call) -> dict[str, Any]:
        """Build a header-injection-safe message and hand it to the client (never retried)."""
        for addr in [params.sender, *params.to]:
            if not _ADDR.match(addr):
                raise InvalidParamsError("invalid email address")
        if "\r" in params.subject or "\n" in params.subject:
            raise InvalidParamsError("subject must be a single line")
        login = _login(call, 465)
        await _check_host(call, login)
        message = EmailMessage()
        message["From"] = params.sender
        message["To"] = ", ".join(params.to)
        message["Subject"] = params.subject
        message.set_content(params.text)
        if params.html:
            message.add_alternative(params.html, subtype="html")
        await self.client.send(login, message)
        return result_data({"accepted": len(params.to)})


class ImapConnector(ConnectorModule):
    """Read message headers over IMAP (read-only mailbox selection)."""

    MODULE_ID: ClassVar[str] = "nexura.connectors.imap"
    TITLE: ClassVar[str] = "Email (IMAP read)"
    DESCRIPTION: ClassVar[str] = (
        "IMAP over TLS, mailbox opened read-only. Credential 'username:password'; config 'host'."
    )
    CREDENTIAL_TYPE: ClassVar[str] = "imap_login"
    OPS: ClassVar[dict[str, Op]] = {"list_messages": Op("list_messages", False, ListMessages)}

    def __init__(self, client: ImapClient | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.client: ImapClient = client or StdlibImapClient()

    async def run(self, op: Op, params: ListMessages, call: Call) -> dict[str, Any]:
        """Fetch up to ``limit`` messages (capped bytes), retrying once on transport errors."""
        login = _login(call, 993)
        await _check_host(call, login)
        last: Exception | None = None
        for attempt in range(min(call.http.max_retries, 2) + 1):
            try:
                messages = await self.client.fetch(
                    login, params.mailbox, params.criteria, params.limit, MAX_FETCH_BYTES
                )
                return result_data(messages, truncated=len(messages) >= params.limit)
            except (ConnectorTransportError, ConnectorTimeoutError) as exc:
                last = exc
                await self.sleep(0.25 * (2**attempt))
        assert last is not None
        raise last
