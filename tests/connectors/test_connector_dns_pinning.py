"""DNS rebinding: mail and SQL connectors connect to the validated IP (fakes, no real network)."""

from __future__ import annotations

import socket
import ssl
from typing import Any

import pytest
from connector_helpers import Sleeps, invoke
from nexura_connectors import (
    ImapConnector,
    InMemorySqlDriver,
    MySqlConnector,
    PostgresConnector,
    SmtpConnector,
)
from nexura_connectors._core import (
    pinned_ssl_context,
    resolve_validated_address,
)
from nexura_connectors.errors import SsrfBlockedError
from nexura_connectors.sql import asyncpg_driver_factory

PUBLIC = "93.184.216.34"
NAME = "mail.rebind.example"
LOGIN = "user@example.com:p4ssw0rd_Distinct_Value"
MSG = {"sender": "a@example.com", "to": ["b@example.com"], "subject": "Hi", "text": "body"}


class Rebinder:
    """getaddrinfo fake: public address on the first call, loopback on every later call."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, host: str, port: Any, *args: Any, **kwargs: Any) -> list[Any]:
        self.calls += 1
        ip = PUBLIC if self.calls == 1 else "127.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]


@pytest.fixture
def rebinder(monkeypatch: pytest.MonkeyPatch) -> Rebinder:
    fake = Rebinder()
    monkeypatch.setattr(socket, "getaddrinfo", fake)
    return fake


@pytest.fixture
def tls_trace(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    """Fake TCP connect and TLS wrap: record the address and TLS name, abort the handshake."""
    trace: dict[str, list[Any]] = {"connect": [], "tls_name": []}

    def create_connection(address: tuple[str, int], *a: Any, **k: Any) -> Any:
        trace["connect"].append(address)
        return object()

    def wrap_socket(self: ssl.SSLContext, sock: Any, *a: Any, **k: Any) -> Any:
        trace["tls_name"].append(k.get("server_hostname"))
        raise ssl.SSLError("fake handshake stops here")

    monkeypatch.setattr(socket, "create_connection", create_connection)
    monkeypatch.setattr(ssl.SSLContext, "wrap_socket", wrap_socket)
    return trace


def test_resolve_once_and_validate_every_address(rebinder: Rebinder) -> None:
    assert str(resolve_validated_address(NAME, 993, [])) == PUBLIC
    assert rebinder.calls == 1
    with pytest.raises(SsrfBlockedError):  # second answer is loopback
        resolve_validated_address(NAME, 993, [])


def test_pinned_context_verifies_original_name(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str | None] = []

    def wrap_socket(self: ssl.SSLContext, sock: Any, *a: Any, **k: Any) -> Any:
        seen.append(k.get("server_hostname"))
        return sock

    monkeypatch.setattr(ssl.SSLContext, "wrap_socket", wrap_socket)
    ctx = pinned_ssl_context(NAME)
    assert ctx.check_hostname is True
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    ctx.wrap_socket(object(), server_hostname=PUBLIC)  # type: ignore[arg-type]
    assert seen == [NAME]


@pytest.mark.parametrize("port", [465, 587])
async def test_smtp_connects_to_validated_ip(
    rebinder: Rebinder, tls_trace: dict[str, list[Any]], port: int
) -> None:
    res = await invoke(
        SmtpConnector(),
        "send_message",
        MSG,
        cfg={"host": NAME, "port": port},
        approved=True,
        secrets={"cred": LOGIN},
    )
    assert res.status != "success"  # the fake handshake aborts; nothing was sent
    assert tls_trace["connect"][0][0] == PUBLIC
    assert all(addr[0] == PUBLIC for addr in tls_trace["connect"])
    assert rebinder.calls == 1
    if port == 465:  # implicit TLS: handshake name is the original host
        assert tls_trace["tls_name"] == [NAME]


async def test_imap_connects_to_validated_ip_with_original_tls_name(
    rebinder: Rebinder, tls_trace: dict[str, list[Any]]
) -> None:
    mod = ImapConnector(sleep=Sleeps())
    res = await invoke(mod, "list_messages", {}, cfg={"host": NAME}, secrets={"cred": LOGIN})
    assert res.status != "success"
    assert {addr[0] for addr in tls_trace["connect"]} == {PUBLIC}
    assert set(tls_trace["tls_name"]) == {NAME}
    assert rebinder.calls == 1


async def test_mail_rebinding_to_loopback_at_preflight_is_blocked(
    monkeypatch: pytest.MonkeyPatch, tls_trace: dict[str, list[Any]]
) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda h, p, *a, **k: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC, p)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", p)),
        ],
    )
    res = await invoke(
        ImapConnector(sleep=Sleeps()),
        "list_messages",
        {},
        cfg={"host": NAME},
        secrets={"cred": LOGIN},
    )
    assert res.status != "success"
    assert tls_trace["connect"] == []


async def test_asyncpg_connects_to_validated_ip(
    rebinder: Rebinder, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    import asyncpg  # type: ignore[import-untyped]

    captured: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    class Boom(Exception):
        pass

    async def fake_connect(*args: Any, **kwargs: Any) -> Any:
        captured.append((args, kwargs))
        raise Boom

    monkeypatch.setattr(asyncpg, "connect", fake_connect)
    # verify-full needs a root certificate, exactly as in asyncpg and libpq (no system-CA fallback).
    ca, _, _, _ = _ca_and_crl(tmp_path)
    dsn = f"postgresql://app:pw_Distinct_1@{NAME}:5432/db?sslmode=verify-full&sslrootcert={ca}"
    res = await invoke(PostgresConnector(), "query", {"sql": "select 1"}, secrets={"cred": dsn})
    assert res.status != "success"
    ((args, kwargs),) = captured
    assert kwargs["host"] == PUBLIC
    ctx = kwargs["ssl"]
    assert ctx.check_hostname is True and ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.server_name == NAME
    assert rebinder.calls == 1


async def test_asyncpg_factory_without_pinning_keeps_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncpg  # type: ignore[import-untyped]

    captured: list[dict[str, Any]] = []

    async def fake_connect(*args: Any, **kwargs: Any) -> Any:
        captured.append(kwargs)
        raise RuntimeError("stop")

    monkeypatch.setattr(asyncpg, "connect", fake_connect)
    with pytest.raises(RuntimeError):
        await asyncpg_driver_factory("postgresql://u:p@8.8.8.8/db")
    assert "host" not in captured[0]


async def test_mysql_factory_receives_validated_ip_and_name(rebinder: Rebinder) -> None:
    got: list[tuple[str, str | None, str | None]] = []

    async def factory(dsn: str, *, pinned_ip: str, server_name: str) -> InMemorySqlDriver:
        got.append((dsn, pinned_ip, server_name))
        return InMemorySqlDriver()

    dsn = f"mysql://app:pw_Distinct_2@{NAME}:3306/db"
    res = await invoke(
        MySqlConnector(driver_factory=factory), "query", {"sql": "select 1"}, secrets={"cred": dsn}
    )
    assert res.status == "success"
    assert got == [(dsn, PUBLIC, NAME)]
    assert rebinder.calls == 1


async def test_sql_rebinding_to_loopback_blocked_before_driver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda h, p, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", p))],
    )
    opened: list[str] = []

    async def factory(dsn: str, *, pinned_ip: str, server_name: str) -> InMemorySqlDriver:
        opened.append(pinned_ip)
        return InMemorySqlDriver()

    dsn = f"mysql://app:pw_Distinct_2@{NAME}/db"
    res = await invoke(
        MySqlConnector(driver_factory=factory), "query", {"sql": "select 1"}, secrets={"cred": dsn}
    )
    assert res.status != "success"
    assert opened == []


# --- the pinned Postgres TLS context honours the same libpq inputs as asyncpg (B27 review) ---


def _ca_and_crl(tmp_path: Any) -> tuple[Any, Any, Any, Any]:
    """A self-signed CA (PEM), its key (PEM), and an empty CRL it signed (PEM)."""
    import datetime as dt

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-ca")])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    crl = (
        x509.CertificateRevocationListBuilder()
        .issuer_name(name)
        .last_update(now - dt.timedelta(days=1))
        .next_update(now + dt.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    ca_path = tmp_path / "root.crt"
    ca_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path = tmp_path / "client.key"
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    crl_path = tmp_path / "root.crl"
    crl_path.write_bytes(crl.public_bytes(serialization.Encoding.PEM))
    return ca_path, key_path, crl_path, cert


def test_pinned_pg_context_enables_crl_checking_from_dsn(tmp_path: Any) -> None:
    import ssl

    from nexura_connectors.sql import _asyncpg_ssl

    ca, _, crl, _ = _ca_and_crl(tmp_path)
    ctx = _asyncpg_ssl(
        f"postgresql://u:p@db.example/d?sslmode=verify-full&sslrootcert={ca}&sslcrl={crl}",
        "db.example",
    )
    assert ctx is not None
    assert ctx.verify_flags & ssl.VERIFY_CRL_CHECK_CHAIN == ssl.VERIFY_CRL_CHECK_CHAIN
    assert ctx.check_hostname is True and ctx.verify_mode == ssl.CERT_REQUIRED


def test_pinned_pg_context_loads_client_certificate(tmp_path: Any) -> None:
    from nexura_connectors.sql import _asyncpg_ssl

    ca, key, _, _ = _ca_and_crl(tmp_path)
    ctx = _asyncpg_ssl(
        f"postgresql://u:p@db.example/d?sslmode=verify-ca&sslrootcert={ca}"
        f"&sslcert={ca}&sslkey={key}",
        "db.example",
    )
    assert ctx is not None and ctx.check_hostname is False


def test_pinned_pg_context_reads_libpq_environment(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ssl

    from nexura_connectors.sql import _asyncpg_ssl

    ca, _, crl, _ = _ca_and_crl(tmp_path)
    monkeypatch.setenv("PGSSLROOTCERT", str(ca))
    monkeypatch.setenv("PGSSLCRL", str(crl))
    ctx = _asyncpg_ssl("postgresql://u:p@db.example/d?sslmode=verify-full", "db.example")
    assert ctx is not None
    assert ctx.verify_flags & ssl.VERIFY_CRL_CHECK_CHAIN == ssl.VERIFY_CRL_CHECK_CHAIN


def test_pinned_pg_context_fails_closed_without_a_root_certificate(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ssl

    from nexura_connectors.errors import InvalidConfigError
    from nexura_connectors.sql import _asyncpg_ssl

    monkeypatch.delenv("PGSSLROOTCERT", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))  # no ~/.postgresql/root.crt
    with pytest.raises(InvalidConfigError, match="root certificate"):
        _asyncpg_ssl("postgresql://u:p@db.example/d?sslmode=verify-full", "db.example")
    bad = tmp_path / "bad.crl"
    bad.write_text("not a crl")
    ca, _, _, _ = _ca_and_crl(tmp_path)
    with pytest.raises(ssl.SSLError):
        _asyncpg_ssl(
            f"postgresql://u:p@db.example/d?sslmode=verify-full&sslrootcert={ca}&sslcrl={bad}",
            "db.example",
        )
