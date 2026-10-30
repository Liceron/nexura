# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Outbound destination security policy."""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

from nexura_agents.network.errors import DestinationPolicyDeniedError

_IP = ipaddress.IPv4Address | ipaddress.IPv6Address

# Cloud metadata addresses to strictly block (AWS/GCP/Azure, AWS IPv6, Alibaba Cloud)
_METADATA_IPS = {
    ipaddress.ip_address("169.254.169.254"),
    ipaddress.ip_address("fd00:ec2::254"),
    ipaddress.ip_address("100.100.100.200"),
}
_NAT64_WELL_KNOWN = ipaddress.ip_network("64:ff9b::/96")  # RFC 6052: IPv4 in the low 32 bits
#: RFC 8215 local-use NAT64. Not globally routable, and the IPv4 is laid out per RFC 6052
#: section 2.2 (not in the low 32 bits), so the whole /48 is refused as private.
_NAT64_LOCAL_USE = ipaddress.ip_network("64:ff9b:1::/48")
_SITE_LOCAL = ipaddress.ip_network("fec0::/10")  # deprecated, but still routed internally


def embedded_ipv4(ip: _IP) -> ipaddress.IPv4Address | None:
    """The IPv4 address an IPv6 address carries (mapped, compatible, NAT64, 6to4, Teredo)."""
    if not isinstance(ip, ipaddress.IPv6Address):
        return None
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if ip.sixtofour is not None:
        return ip.sixtofour
    if ip.teredo is not None:
        return ip.teredo[1]
    if ip in _NAT64_WELL_KNOWN:
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    if int(ip) >> 32 == 0 and int(ip) > 1:  # deprecated IPv4-compatible ::a.b.c.d
        return ipaddress.IPv4Address(int(ip))
    return None


def classify_address(ip: _IP) -> str | None:
    """Why ``ip`` is not a public destination ("metadata", "loopback", "private"), else None.

    "metadata" covers link-local and cloud metadata addresses; "private" covers every other
    non-global range (RFC 1918, ULA, carrier-grade NAT 100.64/10, 0.0.0.0/8, multicast,
    reserved, unspecified, documentation). IPv4 embedded in IPv6 is checked as IPv4.
    """
    inner = embedded_ipv4(ip)
    if inner is not None:
        return classify_address(inner)
    if isinstance(ip, ipaddress.IPv6Address) and ip in _NAT64_LOCAL_USE:
        return "private"
    if ip in _METADATA_IPS or ip.is_link_local:
        return "metadata"
    if ip.is_loopback:
        return "loopback"
    if ip.is_multicast or ip.is_unspecified or ip.is_reserved or not ip.is_global:
        return "private"
    if isinstance(ip, ipaddress.IPv6Address) and ip in _SITE_LOCAL:
        return "private"
    return None


def _norm_host(host: str) -> str:
    return host.strip().rstrip(".").lower()


class DestinationPolicy(BaseModel):
    """Enforces boundaries on outbound HTTP/HTTPS destinations.

    Guarantees:
    - Blocks every non-public address (RFC 1918, ULA, carrier-grade NAT, 0.0.0.0/8, multicast,
      reserved, unspecified) unless ``allow_private_ranges`` is set.
    - Blocks loopback/localhost unless explicitly enabled.
    - Strictly blocks link-local and cloud metadata addresses (169.254.169.254, fd00:ec2::254,
      100.100.100.200), even for hosts in ``allowed_hosts``.
    - Checks IPv4 embedded in IPv6 (mapped, NAT64, 6to4, Teredo) as IPv4.
    - Validates every resolved IP address, including for allow-listed hosts. An allow-listed name
      that does not resolve at check time passes ``check_url``; ``SafeHttpClient`` connects
      through ``pinned_transport``, which validates the addresses again at connect time and
      connects only to an address that passed (no DNS-rebinding window).
    - Re-evaluates destination policy on every HTTP redirect.
    """

    allow_loopback: bool = False
    allow_private_ranges: bool = False
    allowed_hosts: list[str] = Field(default_factory=list)
    allowed_schemes: list[str] = Field(default_factory=lambda: ["http", "https"])

    @property
    def allow_metadata_service(self) -> bool:
        """Always False: metadata and link-local addresses cannot be allowed.

        The old ``allow_metadata_service`` option was removed; passing it is ignored.
        """
        return False

    def is_allow_listed(self, host: str) -> bool:
        """Whether ``host`` is in ``allowed_hosts`` (case-insensitive, trailing dot ignored)."""
        return _norm_host(host) in {_norm_host(h) for h in self.allowed_hosts}

    def check_addresses(self, url: str, host: str, addresses: list[_IP]) -> None:
        """Raise ``DestinationPolicyDeniedError`` unless every address of ``host`` is allowed."""
        if not addresses:
            raise DestinationPolicyDeniedError(url, f"No IP addresses resolved for '{host}'.")
        allow_listed = self.is_allow_listed(host)
        for ip in addresses:
            kind = classify_address(ip)
            if kind == "metadata":
                raise DestinationPolicyDeniedError(
                    url,
                    f"Access to link-local / cloud metadata address '{ip}' is strictly blocked.",
                )
            if kind == "loopback" and not (self.allow_loopback or allow_listed):
                raise DestinationPolicyDeniedError(
                    url,
                    f"Access to loopback address '{ip}' is blocked by policy.",
                )
            if kind == "private" and not (self.allow_private_ranges or allow_listed):
                raise DestinationPolicyDeniedError(
                    url,
                    f"Access to private network address '{ip}' is blocked by policy.",
                )

    @staticmethod
    def resolve(host: str, port: int) -> list[_IP]:
        """Every address ``host`` resolves to (an IP literal resolves to itself); [] if none."""
        try:
            return [ipaddress.ip_address(host.strip("[]").split("%", 1)[0])]
        except ValueError:
            pass
        try:
            infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        except (socket.gaierror, UnicodeError, OSError):  # UnicodeError: bad IDNA label
            return []
        return [ipaddress.ip_address(str(info[4][0]).split("%", 1)[0]) for info in infos]

    def check_url(self, url: str) -> None:
        """Validate URL against outbound access rules.

        Raises:
            DestinationPolicyDeniedError: If destination violates policy.
        """
        try:
            parsed = urlsplit(url)
            host = parsed.hostname
            port = parsed.port
        except ValueError as exc:  # bad port, malformed IPv6 literal
            raise DestinationPolicyDeniedError(url, f"Malformed URL: {exc}") from None

        if not parsed.scheme or parsed.scheme.lower() not in self.allowed_schemes:
            raise DestinationPolicyDeniedError(
                url,
                f"Scheme '{parsed.scheme}' is not allowed (must be one of {self.allowed_schemes}).",
            )
        if not host:
            raise DestinationPolicyDeniedError(url, "URL has missing or empty hostname.")

        addresses = self.resolve(host, port or (443 if parsed.scheme == "https" else 80))
        if not addresses and self.is_allow_listed(host):
            # An operator-listed name that does not resolve here (an offline test host,
            # split-horizon DNS) is allowed; a pinned connection validates it at connect time.
            return
        self.check_addresses(url, host, addresses)
