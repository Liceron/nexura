# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""A built-in web fetch tool for agents, built on the project's safe HTTP client.

``web_fetch_tool()`` returns a read-only ``Tool`` the model can call with a URL. Every
request goes through ``SafeHttpClient`` and ``DestinationPolicy``. This module adds the
checks a model-controlled URL needs on top: no userinfo, odd IP spellings (decimal, octal,
hex), IPv4-mapped IPv6, and multicast, reserved and unspecified addresses are all refused.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Sequence
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit

import httpx

from nexura_agents.network.client import SafeHttpClient
from nexura_agents.network.errors import DestinationPolicyDeniedError
from nexura_agents.network.policy import _METADATA_IPS, DestinationPolicy
from nexura_agents.tools import Tool

_IP = ipaddress.IPv4Address | ipaddress.IPv6Address
_SITE_LOCAL = ipaddress.IPv6Network("fec0::/10")
_REDIRECT_CODES = (301, 302, 303, 307, 308)
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_EXTRA_METADATA = {ipaddress.ip_address("100.100.100.200"), ipaddress.ip_address("192.0.0.192")}
_TEXT_TYPES = ("text/plain", "text/markdown", "text/x-markdown", "application/json")
_DEFAULT_TYPES = ("text/html", "application/xhtml+xml", *_TEXT_TYPES)
# Not "head": browsers end it implicitly, so a page that omits </head> would lose its body.
_SKIP_TAGS = frozenset({"script", "style", "noscript", "template", "svg"})
_BLOCK_TAGS = frozenset(
    {"p", "div", "br", "li", "ul", "ol", "tr", "table", "section", "article", "header"}
    | {"footer", "nav", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "blockquote", "hr"}
)


class WebFetchRefused(Exception):
    """A fetch was refused; the message is safe to show to the model."""


class _TextExtractor(HTMLParser):
    """Collect visible text, skipping scripts, styles and other non-content tags."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\x00")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCK_TAGS:
            self.parts.append("\x00")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def _html_to_text(html: str) -> str:
    """Strip tags, scripts and styles from ``html`` and collapse whitespace."""
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    lines = (" ".join(line.split()) for line in "".join(parser.parts).split("\x00"))
    return "\n".join(line for line in lines if line)


def _loose_ipv4(host: str) -> ipaddress.IPv4Address | None:
    """Parse the decimal, octal, hex and short spellings that ``inet_aton`` accepts."""
    parts = host.split(".")
    if not 1 <= len(parts) <= 4 or not all(parts):
        return None
    numbers: list[int] = []
    for part in parts:
        try:
            if part[:2].lower() == "0x":
                numbers.append(int(part[2:] or "0", 16))
            elif len(part) > 1 and part[0] == "0":
                numbers.append(int(part, 8))
            else:
                numbers.append(int(part, 10))
        except ValueError:
            return None
    *head, last = numbers
    if any(n > 255 for n in head) or last >= 256 ** (4 - len(head)):
        return None
    value = last
    for i, n in enumerate(head):
        value |= n << (8 * (3 - i))
    return ipaddress.IPv4Address(value)


def _unwrap(ip: _IP) -> _IP:
    """The IPv4 address hidden inside a mapped, 6to4 or NAT64 IPv6 address, if any."""
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return ip.ipv4_mapped
        if ip.sixtofour is not None:
            return ip.sixtofour
        if ip in _NAT64:
            return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return ip


def _resolve(host: str, port: int) -> list[_IP]:
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError) as exc:  # UnicodeError: empty or overlong label
        raise WebFetchRefused("The host name could not be resolved.") from exc
    return [ipaddress.ip_address(info[4][0]) for info in infos]


def _check_ip(ip: _IP, policy: DestinationPolicy) -> None:
    ip = _unwrap(ip)
    if not policy.allow_metadata_service and (
        ip in _METADATA_IPS or ip in _EXTRA_METADATA or ip.is_link_local
    ):
        raise WebFetchRefused("Link-local and cloud metadata addresses are blocked.")
    if ip.is_loopback and not policy.allow_loopback:
        raise WebFetchRefused("Loopback addresses are blocked.")
    if ip.is_loopback or ip.is_link_local:
        return  # explicitly allowed by the policy flags above
    if ip in _SITE_LOCAL:
        raise WebFetchRefused("Deprecated site-local IPv6 addresses (fec0::/10) are blocked.")
    if ip.is_multicast or ip.is_unspecified or ip.is_reserved:
        raise WebFetchRefused("Multicast, reserved and unspecified addresses are blocked.")
    if not ip.is_global and not policy.allow_private_ranges:
        raise WebFetchRefused("Private network addresses are blocked.")


def _in_domains(host: str, domains: tuple[str, ...]) -> bool:
    host = host.lower().rstrip(".")
    return any(host == d or host.endswith("." + d) for d in domains)


def _check_destination(url: str, policy: DestinationPolicy, domains: tuple[str, ...]) -> None:
    """Refuse a URL the model must not reach. Blocking: runs DNS, so call from a thread."""
    if any(ord(c) < 0x21 or ord(c) == 0x7F for c in url):
        raise WebFetchRefused("The URL contains spaces or control characters.")
    try:
        parsed = urlsplit(url)
        host, port = parsed.hostname, parsed.port
    except ValueError as exc:
        raise WebFetchRefused("The URL is not valid.") from exc
    if (parsed.scheme or "").lower() not in ("http", "https") or not host:
        raise WebFetchRefused("Only http and https URLs with a host name are allowed.")
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        raise WebFetchRefused("URLs with a user name or password are not allowed.")
    if domains and not _in_domains(host, domains):
        raise WebFetchRefused("This site is not on the tool's list of allowed domains.")
    if host.lower().rstrip(".") in {h.lower().rstrip(".") for h in policy.allowed_hosts}:
        return
    port = port or (443 if parsed.scheme.lower() == "https" else 80)
    literal: _IP | None = _loose_ipv4(host)
    if literal is None:
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
    for ip in [literal] if literal is not None else _resolve(host, port):
        _check_ip(ip, policy)


def _decode(data: bytes, content_type: str, header: str) -> str:
    charset = "utf-8"
    for piece in header.split(";")[1:]:
        key, _, value = piece.partition("=")
        if key.strip().lower() == "charset" and value.strip():
            charset = value.strip().strip("\"'")
    try:
        return data.decode(charset, errors="replace")
    except (LookupError, UnicodeError):  # unknown charset, or one that cannot decode at all
        return data.decode("utf-8", errors="replace")


def web_fetch_tool(
    *,
    policy: DestinationPolicy | None = None,
    max_bytes: int = 200_000,
    timeout: float = 15.0,
    max_redirects: int = 5,
    allowed_content_types: Sequence[str] = _DEFAULT_TYPES,
    allowed_domains: Sequence[str] = (),
    name: str = "web_fetch",
    transport: httpx.AsyncBaseTransport | None = None,
) -> Tool:
    """Build a tool that fetches one http(s) page with GET and returns readable text.

    ``policy`` defaults to ``DestinationPolicy()``, which blocks private, loopback,
    link-local, multicast, reserved, unspecified and cloud-metadata addresses.
    ``allowed_domains=["example.com"]`` restricts the tool to those domains and their
    subdomains (every other site is refused); the address checks still apply.
    ``DestinationPolicy(allowed_hosts=[...])`` is different: it does not restrict anything,
    it lets the listed hosts skip the address checks (for a trusted internal host).
    ``transport`` is a hook for tests and local use (for example ``httpx.MockTransport``).
    Failures come back to the model as ``{"error": ..., "message": ...}`` with no headers.
    """
    if max_bytes < 1 or timeout <= 0 or max_redirects < 0:
        raise ValueError("max_bytes and timeout must be positive; max_redirects not negative.")
    active = policy or DestinationPolicy()
    allowed = tuple(t.lower() for t in allowed_content_types)
    domains = tuple(d.lower().strip().rstrip(".").lstrip(".") for d in allowed_domains)
    if any(not d for d in domains):
        raise ValueError("allowed_domains entries must be non-empty domain names.")

    async def _fetch(url: str) -> dict[str, Any]:
        client = SafeHttpClient(active, transport=transport, timeout=timeout)
        headers = {"User-Agent": "nexura-agents-web-fetch", "Accept": ", ".join(allowed)}
        current = url
        async with client:
            for _ in range(max_redirects + 1):
                await asyncio.to_thread(_check_destination, current, active, domains)
                async with client.stream(
                    "GET", current, headers=headers, refuse_redirect=False
                ) as response:
                    if response.status_code in _REDIRECT_CODES:
                        location = response.headers.get("location")
                        if not location:
                            raise WebFetchRefused("The server sent a redirect with no target.")
                        current = str(response.request.url.join(location))
                        continue
                    if not 200 <= response.status_code < 300:
                        raise WebFetchRefused(f"The server answered HTTP {response.status_code}.")
                    header = response.headers.get("content-type", "")
                    ctype = header.split(";")[0].strip().lower()
                    if ctype not in allowed:
                        raise WebFetchRefused(
                            f"Content type '{ctype or 'unknown'}' is not supported. "
                            f"Allowed: {', '.join(allowed)}."
                        )
                    buf = bytearray()
                    truncated = False
                    async for chunk in response.aiter_bytes():
                        room = max_bytes - len(buf)
                        if len(chunk) > room:  # more data than fits: keep what fits, stop
                            buf += chunk[:room]
                            truncated = True
                            break
                        buf += chunk
                    text = _decode(bytes(buf), ctype, header)
                    if "html" in ctype:
                        text = _html_to_text(text)
                    return {
                        "url": current,
                        "status": response.status_code,
                        "content_type": ctype,
                        "truncated": truncated,
                        "text": text,
                    }
        raise WebFetchRefused("Too many redirects.")

    async def fetch(url: str) -> dict[str, Any]:
        """Fetch a public web page with GET and return its text (HTML is stripped to text)."""
        try:
            return await asyncio.wait_for(_fetch(url), timeout=timeout)
        except WebFetchRefused as exc:
            return {"error": "refused", "message": str(exc)}
        except DestinationPolicyDeniedError as exc:
            return {"error": "refused", "message": f"Blocked by policy: {exc.reason}"}
        except (httpx.InvalidURL, ValueError):  # the URL comes from the model
            return {"error": "refused", "message": "The URL is not valid."}
        except (TimeoutError, httpx.TimeoutException):
            return {"error": "timeout", "message": f"The request timed out after {timeout:g}s."}
        except httpx.HTTPError as exc:
            return {"error": "request_failed", "message": f"Request failed ({type(exc).__name__})."}

    return Tool(
        name,
        fetch,
        description=(
            "Fetch a public http(s) web page with GET and return its readable text. "
            "Private, local and metadata addresses are refused. Large pages are truncated."
        ),
        idempotent=True,
        origin="builtin:web",
    )


__all__ = ["WebFetchRefused", "web_fetch_tool"]
