"""Destination address policy tests (getaddrinfo is faked; no network)."""

from __future__ import annotations

import socket

import pytest
from nexura_connectors._core import check_destination
from nexura_connectors.errors import SsrfBlockedError

TABLE = {
    "ok.example": "93.184.216.34",
    "meta.example": "169.254.169.254",
    "cgnat.example": "100.100.100.200",
    "lan.example": "10.1.2.3",
    "loop.example": "127.0.0.1",
    "nat64.example": "64:ff9b::a9fe:a9fe",
    "v6meta.example": "fd00:ec2::254",
}


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake(host, port, *a, **k):  # type: ignore[no-untyped-def]
        if host in TABLE:
            return [(2, 1, 6, "", (TABLE[host], port or 443))]
        raise socket.gaierror("no such host")

    monkeypatch.setattr(socket, "getaddrinfo", fake)


@pytest.mark.parametrize(
    "url",
    [
        "https://100.100.100.200/",
        "https://100.64.0.1/",
        "https://0.0.0.0/",
        "https://0.1.2.3/",
        "https://224.0.0.1/",
        "https://240.0.0.1/",
        "https://192.0.2.1/",
        "https://169.254.169.254/",
        "https://[64:ff9b::a9fe:a9fe]/",  # NAT64 -> 169.254.169.254
        "https://[64:ff9b::7f00:1]/",  # NAT64 -> loopback
        "https://[2002:a9fe:a9fe::1]/",  # 6to4 -> metadata
        "https://[::ffff:169.254.169.254]/",
        "https://[::ffff:100.64.0.1]/",
        "https://[::a9fe:a9fe]/",  # IPv4-compatible
        "https://[fd00:ec2::254]/",
        "https://[ff02::1]/",
        "https://[fe80::1]/",
        "https://[::1]/",
        "https://10.0.0.1/",
    ],
)
def test_non_global_addresses_blocked(url: str) -> None:
    with pytest.raises(SsrfBlockedError):
        check_destination(url, [], transport_injected=False)


def test_teredo_embedded_metadata_blocked() -> None:
    # Teredo server 169.254.169.254 (a9fe:a9fe) in bits 32..63
    with pytest.raises(SsrfBlockedError):
        check_destination("https://[2001:0:a9fe:a9fe::1]/", [], transport_injected=False)


@pytest.mark.parametrize(
    "url", ["https://93.184.216.34/", "https://[::ffff:8.8.8.8]/", "https://ok.example/"]
)
def test_public_addresses_allowed(url: str) -> None:
    check_destination(url, [], transport_injected=False)


@pytest.mark.parametrize(
    "host", ["meta.example", "cgnat.example", "nat64.example", "v6meta.example"]
)
def test_allowlist_never_opens_metadata_like_ranges(host: str) -> None:
    with pytest.raises(SsrfBlockedError):
        check_destination(f"https://{host}/", [host], transport_injected=False)


@pytest.mark.parametrize("host", ["lan.example", "loop.example"])
def test_allowlist_opens_only_rfc1918_and_loopback_for_that_host(host: str) -> None:
    with pytest.raises(SsrfBlockedError):
        check_destination(f"https://{host}/", [], transport_injected=False)
    check_destination(f"https://{host}/", [host], transport_injected=False)


def test_allowlisted_host_that_does_not_resolve_is_refused() -> None:
    with pytest.raises(SsrfBlockedError):
        check_destination("https://gone.example/", ["gone.example"], transport_injected=False)


def test_http_scheme_refused() -> None:
    with pytest.raises(SsrfBlockedError):
        check_destination("http://ok.example/", [], transport_injected=False)


async def test_connection_is_validated_at_connect_time_not_only_before() -> None:
    from nexura_connectors._core import _PinnedBackend

    backend = _PinnedBackend([])
    with pytest.raises(SsrfBlockedError):  # a name that now resolves to loopback/metadata
        await backend.connect_tcp("loop.example", 443)
    with pytest.raises(SsrfBlockedError):
        await backend.connect_tcp("meta.example", 443)
    with pytest.raises(SsrfBlockedError):
        await _PinnedBackend(["meta.example"]).connect_tcp("meta.example", 443)
