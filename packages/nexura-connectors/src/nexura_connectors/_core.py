# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Shared machinery for connector modules: credentials, approval, safe HTTP, redaction."""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import json as _json
import os
import random
import re
import socket
import ssl
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Any, ClassVar, Literal
from urllib.parse import quote, urlsplit

import httpcore
import httpx
from nexura_agents.contracts.base import BaseModule
from nexura_agents.contracts.context import CredentialReference, ExecutionContext
from nexura_agents.contracts.manifest import DisplayMetadata, ModuleManifest
from nexura_agents.contracts.result import ModuleResult
from nexura_agents.network.policy import DestinationPolicy
from pydantic import BaseModel, ConfigDict, ValidationError

from nexura_connectors.errors import (
    ApprovalRequiredError,
    AuthFailedError,
    ConnectorError,
    ConnectorTimeoutError,
    ConnectorTransportError,
    IndeterminateEffectError,
    InvalidConfigError,
    InvalidParamsError,
    MissingCredentialError,
    RateLimitedError,
    ResponseTooLargeError,
    SsrfBlockedError,
    UpstreamError,
)
from nexura_runtime.credentials.store import redact_secrets
from nexura_runtime.integrations.channels.redaction import redact_text

DEFAULT_MAX_RESPONSE_BYTES = 2_000_000
HARD_MAX_RESPONSE_BYTES = 10_000_000
DEFAULT_MAX_PAGES = 3
HARD_MAX_PAGES = 10
HARD_MAX_RETRIES = 3
MAX_RETRY_AFTER_SECONDS = 5.0

_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")
_TOKEN_PREFIXES = (
    "xox",
    "ghp_",
    "gho_",
    "ghs_",
    "github_pat_",
    "secret_",
    "ntn_",
    "sk-",
    "sk_",
    "akia",
    "asia",
    "ya29",
    "pat",
    "eyj",
)
ALLOWED_HOSTS_ENV = "NEXURA_CONNECTOR_ALLOWED_HOSTS"
CONTEXT_ALLOWED_HOSTS_KEY = "connector_allowed_hosts"
MIN_TIMEOUT_SECONDS = 0.1
MAX_TIMEOUT_SECONDS = 120.0
_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")

SleepFn = Callable[[float], Awaitable[None]]


class StrictParams(BaseModel):
    """Base for operation parameter models: unknown fields are rejected."""

    model_config = ConfigDict(extra="forbid")


@dataclass(frozen=True)
class Op:
    """One connector operation: its name, read/write class and parameter model."""

    name: str
    write: bool
    model: type[StrictParams]
    description: str = ""
    effect_class: Literal["pure", "idempotent", "non_idempotent"] | None = None


MAX_REQUEST_BYTES = 262_144


def check_request_size(obj: Any, what: str, limit: int = MAX_REQUEST_BYTES) -> None:
    """Raise InvalidParamsError when ``obj`` serialises to more than ``limit`` bytes of JSON."""
    try:
        size = len(_json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"))
    except (TypeError, ValueError):
        raise InvalidParamsError(f"{what} is not JSON serialisable") from None
    if size > limit:
        raise InvalidParamsError(f"{what} is {size} bytes; the request cap is {limit} bytes")


def safe_id(value: str, what: str = "identifier") -> str:
    """Validate and URL-quote a path segment so it cannot inject extra path parts."""
    if not _ID_RE.match(value) or value in {".", ".."}:
        raise InvalidParamsError(f"invalid {what}")
    return quote(value, safe="")


def pick_cursor(cursor: str | None, alias: str | None, alias_name: str) -> str | None:
    """Return the resume cursor from ``cursor`` or its legacy alias; both given must agree."""
    if cursor is not None and alias is not None and cursor != alias:
        raise InvalidParamsError(f"cursor and {alias_name} disagree; pass only cursor")
    return cursor if cursor is not None else alias


def _parse_hosts(raw: Any) -> list[str]:
    if isinstance(raw, str):
        raw = raw.split(",")
    if not isinstance(raw, (list, tuple, set, frozenset)):
        return []
    return [h.strip().lower() for h in raw if isinstance(h, str) and h.strip()]


def scrub(obj: Any, secrets: list[str]) -> Any:
    """Recursively redact known secret values from any JSON-like structure."""
    if isinstance(obj, str):
        return redact(obj, secrets)
    if isinstance(obj, list):
        return [scrub(item, secrets) for item in obj]
    if isinstance(obj, dict):
        return {k: scrub(v, secrets) for k, v in obj.items()}
    return obj


def redact(text: str, secrets: list[str]) -> str:
    """Redact credentials, bearer tokens and token-like values from text."""
    ordered = sorted({s for s in secrets if s}, key=len, reverse=True)
    return redact_text(redact_secrets(text, ordered))


SECRET_FIELDS = frozenset(
    {
        "access_token",
        "refresh_token",
        "client_secret",
        "app_jwt",
        "jwt",
        "private_key",
        "token",
        "id_token",
    }
)


def secret_variants(secret: str, *, split: bool = True) -> list[str]:
    """Return the secret plus derived forms (parts, base64) that must never leak."""
    forms = {secret, base64.b64encode(secret.encode()).decode()}
    if secret.lstrip().startswith("{"):
        try:
            data = _json.loads(secret)
        except ValueError:
            return sorted(forms)
        if isinstance(data, dict):
            for key in SECRET_FIELDS:
                value = data.get(key)
                if isinstance(value, str) and value:
                    forms.update(secret_variants(value, split=False))
        return sorted(forms)
    if not split:
        return sorted(forms)
    if "://" in secret:
        # webhook-style URL: the path segments and userinfo carry the token
        parts = urlsplit(secret)
        forms.update(seg for seg in parts.path.split("/") if len(seg) >= 8)
        forms.update(x for x in (parts.password, parts.username) if x and len(x) >= 4)
    else:
        forms.update(part for part in secret.split(":") if len(part) >= 4)
    return sorted(forms)


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def build_policy(
    url: str, allowed_hosts: list[str], *, transport_injected: bool
) -> DestinationPolicy:
    """Build the destination policy: https only; private, loopback and metadata blocked.

    Hosts listed in ``allowed_hosts`` skip the IP checks (an explicit operator allow-list),
    except the cloud metadata/link-local range, which is never allowed.
    """
    host = urlsplit(url).hostname or ""
    allowed = [h for h in allowed_hosts if not _is_ip(h)]
    if transport_injected and host and not _is_ip(host):
        allowed.append(host)
    return DestinationPolicy(allowed_hosts=allowed, allowed_schemes=["https"])


_ALWAYS_BLOCKED = tuple(
    ipaddress.ip_network(n)
    for n in (
        "0.0.0.0/8",
        "100.64.0.0/10",  # CGNAT, includes Alibaba metadata 100.100.100.200
        "169.254.0.0/16",
        "224.0.0.0/4",
        "240.0.0.0/4",
        "fe80::/10",
        "fec0::/10",
        "ff00::/8",
        "fd00:ec2::/64",
        "64:ff9b:1::/48",
    )
)
_INTERNAL = tuple(
    ipaddress.ip_network(n)
    for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "::1/128", "fc00::/7")
)
_METADATA_ADDRESSES = frozenset(
    ipaddress.ip_address(a) for a in ("169.254.169.254", "fd00:ec2::254", "100.100.100.200")
)
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_V4_COMPATIBLE = ipaddress.ip_network("::/96")


def _embedded_addresses(
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """The IPv4 addresses embedded in a wrapped IPv6 form (mapped, NAT64, ...), else ``ip``."""
    if not isinstance(ip, ipaddress.IPv6Address):
        return [ip]
    found: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    if ip.ipv4_mapped is not None:
        found.append(ip.ipv4_mapped)
    if ip.sixtofour is not None:
        found.append(ip.sixtofour)
    if ip.teredo is not None:
        found.extend(ip.teredo)
    if ip in _NAT64 or (ip in _V4_COMPATIBLE and not ip.is_loopback and not ip.is_unspecified):
        found.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
    return found or [ip]


def _classify(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str:
    """Return 'ok' (global), 'internal' (RFC1918/loopback style) or 'never' (not allow-listable)."""
    verdict = "ok"
    for addr in _embedded_addresses(ip):
        if (
            addr in _METADATA_ADDRESSES
            or addr.is_link_local
            or addr.is_multicast
            or addr.is_unspecified
            or any(addr in net for net in _ALWAYS_BLOCKED if net.version == addr.version)
        ):
            return "never"
        if any(addr in net for net in _INTERNAL if net.version == addr.version):
            verdict = "internal"
        elif addr.is_reserved or not addr.is_global:
            return "never"
    return verdict


def resolve_host_addresses(
    host: str,
) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Resolve ``host`` (literal or DNS) to every address; empty when it does not resolve."""
    try:
        return [ipaddress.ip_address(host)]
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError):
        return []
    return [ipaddress.ip_address(str(info[4][0]).split("%", 1)[0]) for info in infos]


def check_host_addresses(host: str, allowed_hosts: list[str], *, resolve: bool = True) -> None:
    """Raise SsrfBlockedError unless every address of ``host`` is public.

    Allow-listed host names may additionally reach RFC1918/loopback addresses, but never
    link-local, metadata, CGNAT, multicast, reserved or otherwise non-global ranges.
    """
    if not _is_ip(host) and not resolve:
        return
    validate_addresses(host, resolve_host_addresses(host), allowed_hosts)


def validate_addresses(
    host: str,
    addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address],
    allowed_hosts: list[str],
) -> None:
    """Raise SsrfBlockedError unless every resolved address is permitted for ``host``."""
    literal = _is_ip(host)
    if not addresses:
        raise SsrfBlockedError(f"destination host did not resolve: {host}")
    allowed = not literal and host.lower() in {h.lower() for h in allowed_hosts if not _is_ip(h)}
    for ip in addresses:
        verdict = _classify(ip)
        if verdict == "never":
            raise SsrfBlockedError("destination is a link-local, metadata or non-public address")
        if verdict == "internal" and not allowed:
            raise SsrfBlockedError("destination is a private or loopback address")


def check_destination(url: str, allowed_hosts: list[str], *, transport_injected: bool) -> None:
    """Raise SsrfBlockedError unless ``url`` passes the outbound policy."""
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        parts.port  # noqa: B018 - validates the port
    except ValueError:
        raise SsrfBlockedError("destination URL is malformed") from None
    if parts.scheme.lower() != "https":
        raise SsrfBlockedError("destination blocked by outbound policy: https is required")
    if not host:
        raise SsrfBlockedError("destination URL is malformed")
    # an injected (mock) transport never touches DNS, so named hosts are not resolved
    check_host_addresses(host, allowed_hosts, resolve=not transport_injected)


def resolve_validated_address(
    host: str, port: int, allowed_hosts: list[str]
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """Resolve ``host`` once, validate every address with the outbound policy, return the first.

    Callers must connect to the returned address, never to the name again (no re-resolution).
    """
    if not host:
        raise SsrfBlockedError("destination host is missing")
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        except (socket.gaierror, UnicodeError):
            infos = []
        addresses = [ipaddress.ip_address(str(i[4][0]).split("%", 1)[0]) for i in infos]
    validate_addresses(host, addresses, allowed_hosts)
    return addresses[0]


class PinnedHostnameSSLContext(ssl.SSLContext):
    """SSLContext that verifies the certificate against ``server_name`` whatever host is passed.

    The socket is connected to a validated IP address; the stdlib clients then ask TLS to verify
    that address, so this context substitutes the original host name for SNI and verification.
    """

    server_name: str | None = None

    def wrap_socket(self, sock: socket.socket, *args: Any, **kwargs: Any) -> ssl.SSLSocket:
        """Wrap ``sock`` verifying against the pinned original host name."""
        if self.server_name and kwargs.get("server_hostname"):
            kwargs["server_hostname"] = self.server_name
        return super().wrap_socket(sock, *args, **kwargs)

    def wrap_bio(self, *args: Any, **kwargs: Any) -> ssl.SSLObject:
        """Wrap a memory BIO verifying against the pinned original host name."""
        if self.server_name and kwargs.get("server_hostname"):
            kwargs["server_hostname"] = self.server_name
        return super().wrap_bio(*args, **kwargs)


def pinned_ssl_context(server_name: str, *, verify_hostname: bool = True) -> ssl.SSLContext:
    """Return a verifying client TLS context that checks certificates against ``server_name``."""
    context = PinnedHostnameSSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.server_name = server_name
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.verify_mode = ssl.CERT_REQUIRED
    context.check_hostname = verify_hostname
    context.load_default_certs()
    return context


class _PinnedBackend(httpcore.AsyncNetworkBackend):
    """Network backend that resolves, validates and connects to the same IP (no re-resolution)."""

    def __init__(self, allowed_hosts: list[str]) -> None:
        self._inner: httpcore.AsyncNetworkBackend = httpcore.AnyIOBackend()
        self._allowed = list(allowed_hosts)

    @staticmethod
    def _resolve(host: str, port: int) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
        try:
            return [ipaddress.ip_address(host)]
        except ValueError:
            pass
        try:
            infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        except (socket.gaierror, UnicodeError):
            return []
        return [ipaddress.ip_address(str(i[4][0]).split("%", 1)[0]) for i in infos]

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        """Resolve once, validate every address, then connect to a validated address."""
        addresses = await asyncio.to_thread(self._resolve, host, port)
        validate_addresses(host, addresses, self._allowed)
        last: Exception | None = None
        for ip in addresses:
            try:
                return await self._inner.connect_tcp(
                    str(ip), port, timeout, local_address, socket_options
                )
            except httpcore.ConnectError as exc:
                last = exc
        raise last or httpcore.ConnectError("no address")

    async def connect_unix_socket(
        self, path: str, timeout: float | None = None, socket_options: Iterable[Any] | None = None
    ) -> httpcore.AsyncNetworkStream:
        """Unix sockets are never used for connector traffic."""
        raise httpcore.ConnectError("unix sockets are not permitted")

    async def sleep(self, seconds: float) -> None:
        """Delegate to the inner backend's sleep."""
        await self._inner.sleep(seconds)


def pinned_transport(allowed_hosts: list[str]) -> httpx.AsyncBaseTransport:
    """An httpx transport whose TCP connections go to the address that passed the SSRF check."""
    transport = httpx.AsyncHTTPTransport()
    pool = transport._pool  # httpx exposes no public hook for a custom network backend
    pool._network_backend = _PinnedBackend(allowed_hosts)
    return transport


@dataclass
class HttpResult:
    """A bounded upstream response."""

    status: int
    headers: httpx.Headers = field(repr=False)
    body: bytes = field(repr=False)

    def json(self) -> Any:
        """Parse the body as JSON or raise UpstreamError."""
        try:
            import json

            return json.loads(self.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise UpstreamError("upstream returned a non-JSON body") from None


@dataclass
class ConnectorHttp:
    """Bounded, SSRF-guarded HTTP with retries restricted to idempotent reads."""

    transport: httpx.AsyncBaseTransport | None
    allowed_hosts: list[str]
    timeout: float
    max_bytes: int
    max_retries: int
    secrets: list[str] = field(repr=False)
    sleep: SleepFn = asyncio.sleep
    base_origin: str | None = None
    default_host: str | None = None
    attempts_made: int = field(default=0)
    #: Sent as ``Idempotency-Key`` on writes that do not set one (the workflow step's key,
    #: stable across a crash recovery; see ``ExecutionContext.idempotency_key()``).
    idempotency_key: str | None = None
    rate_profile: str | None = None
    indeterminate_status: bool = False
    refresh_auth: Callable[[], Awaitable[dict[str, str]]] | None = field(default=None, repr=False)
    started_at: float = field(default_factory=time.monotonic, repr=False)
    retry_waited: float = 0.0
    retry_budget: float = 30.0

    def remaining_budget(self) -> float:
        """One invocation budget shared by pages, refreshes, requests and retry waits."""
        return max(
            0.0, self.retry_budget - (time.monotonic() - self.started_at) - self.retry_waited
        )

    def rate_limited(self, result: HttpResult) -> bool:
        """Recognise vendor throttling, including throttling returned with HTTP 403/200."""
        if result.status == 429:
            return True
        if self.rate_profile == "github" and result.status == 403:
            body = result.body.lower()
            return (
                "retry-after" in result.headers
                or result.headers.get("x-ratelimit-remaining") == "0"
                or b"secondary rate limit" in body
                or b"abuse detection" in body
                or b"api rate limit exceeded" in body
            )
        if (
            (self.rate_profile == "google" and result.status == 403)
            or (self.rate_profile == "slack" and result.status in {200, 403})
        ) and "json" in result.headers.get("content-type", "").lower():
            try:
                data = result.json()
            except UpstreamError:
                return False
            if not isinstance(data, dict):
                return False
            if self.rate_profile == "slack":
                return data.get("ok") is False and data.get("error") == "ratelimited"
            if result.status == 403:
                error = data.get("error")
                if isinstance(error, dict):
                    errors = error.get("errors", [])
                    return isinstance(errors, list) and any(
                        isinstance(e, dict)
                        and e.get("reason") in {"rateLimitExceeded", "userRateLimitExceeded"}
                        for e in errors
                    )
        return False

    async def wait_retry(self, delay: float) -> None:
        """Defer to the caller when a retry cannot fit within this invocation."""
        if delay >= self.remaining_budget():
            error = RateLimitedError(
                "retry wait exceeds the remaining operation timeout; defer this read"
            )
            error.retry_after_seconds = delay
            raise error
        while delay > 0:
            interval = min(delay, MAX_RETRY_AFTER_SECONDS)
            before = time.monotonic()
            await self.sleep(interval)
            # Custom/mock sleepers may advance no real time; charge the promised wait
            # without double counting time spent in a real sleep.
            self.retry_waited += max(0.0, interval - (time.monotonic() - before))
            delay -= interval

    def same_origin(self, url: str) -> bool:
        """True when ``url`` has the same scheme and host as the configured base."""
        if self.base_origin is None:
            return True
        a, b = urlsplit(url), urlsplit(self.base_origin)
        return (a.scheme, a.hostname, a.port) == (b.scheme, b.hostname, b.port)

    def check_base_url(self, url: str) -> None:
        """Refuse a base URL whose host is neither the connector default nor operator-allowed."""
        host = (urlsplit(url).hostname or "").lower()
        if host and host == self.default_host:
            return
        if host and host in {h.lower() for h in self.allowed_hosts}:
            return
        raise SsrfBlockedError(
            "base_url host is not the connector's default API host or an operator-allowed host"
        )

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json: Any = None,
        content: bytes | None = None,
        write: bool = False,
    ) -> HttpResult:
        """Send one logical request; retry only when it is a read (``write=False``)."""
        if not self.same_origin(url):
            raise SsrfBlockedError("refusing to send credentials to a different origin")
        if (
            write
            and self.idempotency_key
            and not any(k.lower() == "idempotency-key" for k in (headers or {}))
        ):
            headers = {**(headers or {}), "Idempotency-Key": self.idempotency_key}
        retry_read = not write and (not self.rate_profile or method.upper() in {"GET", "HEAD"})
        total = (self.max_retries if retry_read else 0) + 1
        for attempt in range(total):
            last = attempt == total - 1
            if attempt and self.refresh_auth is not None:
                headers = {**(headers or {}), **await self.refresh_auth()}
            self.attempts_made += 1
            try:
                remaining = self.remaining_budget() if self.rate_profile else self.timeout
                if remaining <= 0:
                    raise ConnectorTimeoutError("operation timeout budget exhausted")
                try:
                    result = await asyncio.wait_for(
                        self._once(
                            method, url, headers=headers, params=params, json=json, content=content
                        ),
                        timeout=min(self.timeout, remaining),
                    )
                except TimeoutError:
                    raise ConnectorTimeoutError("operation timeout budget exhausted") from None
            except (ConnectorTimeoutError, ConnectorTransportError) as exc:
                if write:
                    raise IndeterminateEffectError(
                        f"write outcome unknown after {type(exc).__name__}: {exc.message}"
                    ) from None
                if last:
                    raise
                await self.wait_retry(self._retry_delay(None, attempt))
                continue
            status = result.status
            if write and self.indeterminate_status and (status >= 500 or status == 408):
                raise IndeterminateEffectError(
                    f"write outcome unknown after HTTP {status}; reconcile before retrying"
                )
            if (self.rate_limited(result) or status >= 500) and not write and not last:
                await self.wait_retry(self._retry_delay(result, attempt))
                continue
            self._raise_for_status(result)
            return result
        raise UpstreamError("retries exhausted")  # pragma: no cover

    def _retry_delay(self, result: HttpResult | None, attempt: int) -> float:
        raw = result.headers.get("retry-after", "") if result else ""
        if self.rate_profile:
            delays: list[float] = []
            if raw:
                try:
                    value = float(raw)
                except ValueError:
                    try:
                        value = parsedate_to_datetime(raw).timestamp() - time.time()
                    except (ValueError, TypeError, OverflowError):
                        value = 0.0
                if 0 <= value < float("inf"):
                    delays.append(value)
            if result and self.rate_profile == "github":
                if result.headers.get("x-ratelimit-remaining") == "0":
                    try:
                        value = float(result.headers["x-ratelimit-reset"]) - time.time()
                        if 0 <= value < float("inf"):
                            delays.append(value)
                        elif value < 0:
                            delays.append(random.uniform(0.1, 0.5))
                    except (KeyError, ValueError):
                        pass
                if result.status == 403 and self.rate_limited(result) and not delays:
                    delays.append(60.0)  # GitHub secondary-limit minimum without Retry-After
            if delays:
                delay = max(delays)
                return delay + min(delay * random.uniform(0.0, 0.1), 3.0)
            return random.uniform(0.5, 1.0) * min(2.0**attempt, 30.0)
        try:
            return float(max(0.0, min(float(raw), MAX_RETRY_AFTER_SECONDS)))
        except ValueError:
            return float(min(0.25 * (2**attempt), MAX_RETRY_AFTER_SECONDS))

    def _raise_for_status(self, result: HttpResult) -> None:
        status = result.status
        if self.rate_limited(result):
            error = RateLimitedError(
                "upstream rate limit reached; wait for Retry-After/reset before retrying reads; "
                "writes were not retried"
            )
            error.retry_after_seconds = self._retry_delay(result, 0)
            raise error
        if 200 <= status < 300:
            return
        snippet = redact(result.body[:300].decode("utf-8", errors="replace"), self.secrets)
        if 300 <= status < 400:
            raise UpstreamError(f"upstream redirect ({status}) refused")
        if status in (401, 403):
            auth_error = AuthFailedError(
                f"upstream rejected access (HTTP {status}); check token expiry, scopes, "
                "resource permissions and installation access"
            )
            auth_error.status = status
            raise auth_error
        if self.rate_profile:
            advice = {
                400: "check operation parameters and resource identifiers",
                404: "check the resource identifier and credential access to it",
                409: "resource conflict; reconcile the existing resource before another write",
                422: "check field values and repository/resource validation rules",
            }.get(status, "check upstream availability and operation parameters")
            raise UpstreamError(f"upstream error (HTTP {status}); {advice}")
        if status == 429:
            raise RateLimitedError("upstream rate limit reached (HTTP 429)")
        raise UpstreamError(f"upstream error (HTTP {status}): {snippet}")

    async def _once(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None,
        params: dict[str, Any] | None,
        json: Any,
        content: bytes | None,
    ) -> HttpResult:
        await asyncio.to_thread(
            check_destination,
            url,
            self.allowed_hosts,
            transport_injected=self.transport is not None,
        )
        try:
            async with httpx.AsyncClient(
                transport=self.transport or pinned_transport(self.allowed_hosts),
                timeout=httpx.Timeout(self.timeout),
                follow_redirects=False,
            ) as client:
                request = client.build_request(
                    method, url, headers=headers, params=params, json=json, content=content
                )
                response = await client.send(request, stream=True)
                try:
                    declared = response.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > self.max_bytes:
                        raise ResponseTooLargeError(
                            f"response larger than the {self.max_bytes} byte cap"
                        )
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > self.max_bytes:
                            raise ResponseTooLargeError(
                                f"response larger than the {self.max_bytes} byte cap"
                            )
                        chunks.append(chunk)
                    return HttpResult(response.status_code, response.headers, b"".join(chunks))
                finally:
                    await response.aclose()
        except httpx.TimeoutException:
            raise ConnectorTimeoutError("upstream timed out") from None
        except httpx.InvalidURL:
            raise SsrfBlockedError("destination URL is malformed") from None
        except httpx.HTTPError as exc:
            raise ConnectorTransportError(f"connection failed ({type(exc).__name__})") from None


@dataclass
class Call:
    """Everything an operation handler needs, resolved once per invocation."""

    secret: str = field(repr=False)
    http: ConnectorHttp
    cfg: dict[str, Any] = field(repr=False)
    context: ExecutionContext = field(repr=False)
    auth_retried: bool = False

    @property
    def max_pages(self) -> int:
        """Page cap (config ``max_pages``, clamped to 1..HARD_MAX_PAGES)."""
        return max(1, min(int(self.cfg.get("max_pages") or DEFAULT_MAX_PAGES), HARD_MAX_PAGES))

    def base_url(self, default: str | None) -> str:
        """Resolve the API base URL (config override or the connector default)."""
        override = self.cfg.get("base_url")
        url = override or default
        if not url:
            raise InvalidConfigError("config 'base_url' is required for this connector")
        if override:
            self.http.check_base_url(str(override))
        return str(url).rstrip("/")


def result_data(
    data: Any, *, next_cursor: str | None = None, truncated: bool = False
) -> dict[str, Any]:
    """Shape an operation's output payload."""
    out: dict[str, Any] = {"data": data, "truncated": truncated}
    if next_cursor is not None:
        out["next_cursor"] = next_cursor
    return out


class ConnectorModule(BaseModule):
    """Base class for connectors: one module per service with a table of operations."""

    MODULE_ID: ClassVar[str]
    TITLE: ClassVar[str]
    DESCRIPTION: ClassVar[str]
    CREDENTIAL_TYPE: ClassVar[str]
    OPS: ClassVar[dict[str, Op]]
    SOURCE: ClassVar[str] = "packages/nexura-connectors/src/nexura_connectors/"
    DEFAULT_BASE_URL: ClassVar[str | None] = None
    NEEDS_CREDENTIAL: ClassVar[bool] = True
    #: Writes carry the workflow step's idempotency key as ``Idempotency-Key`` (APIs that
    #: ignore the header are unaffected); a connector that signs its headers opts out.
    SENDS_IDEMPOTENCY_KEY: ClassVar[bool] = True

    def __init__(
        self,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: SleepFn | None = None,
        allowed_hosts: Iterable[str] | None = None,
    ) -> None:
        self.transport = transport
        self.sleep: SleepFn = sleep or asyncio.sleep
        self.operator_hosts: list[str] = _parse_hosts(list(allowed_hosts or []))

    def effective_allowed_hosts(self, context: ExecutionContext | None = None) -> list[str]:
        """Operator allow-list: constructor hosts, execution-context settings and the env var.

        Flow config never contributes: only the deployer controls these.
        """
        hosts = list(self.operator_hosts)
        if context is not None:
            hosts += _parse_hosts(getattr(context, "attributes", {}).get(CONTEXT_ALLOWED_HOSTS_KEY))
        hosts += _parse_hosts(os.environ.get(ALLOWED_HOSTS_ENV, ""))
        return sorted(set(hosts))

    # -- manifest ---------------------------------------------------------------------------
    @classmethod
    def build_manifest(cls) -> ModuleManifest:
        """Build the manifest, marking each operation as read or write."""
        has_writes = any(op.write for op in cls.OPS.values())
        has_reads = any(not op.write for op in cls.OPS.values())
        return ModuleManifest(
            schema_version=1,
            module_id=cls.MODULE_ID,
            version="1.0.0",
            kind="integration",
            title=cls.TITLE,
            description=cls.DESCRIPTION
            + " Operations: "
            + ", ".join(f"{n} ({'write' if o.write else 'read'})" for n, o in cls.OPS.items())
            + ". Writes need a human approval (the 'approved' input is ignored).",
            input_schema={
                "type": "object",
                "properties": {
                    "operation": {"type": "string", "enum": sorted(cls.OPS)},
                    "params": {"type": "object"},
                    "approved": {"type": "boolean"},
                },
                "required": ["operation"],
            },
            output_schema={
                "type": "object",
                "properties": {
                    "operation": {"type": "string"},
                    "write": {"type": "boolean"},
                    "data": {},
                    "truncated": {"type": "boolean"},
                    "next_cursor": {"type": "string"},
                },
                "required": ["operation", "write"],
            },
            config_schema={
                "type": "object",
                "properties": {
                    "credential_ref": {"type": "string"},
                    "base_url": {"type": "string"},
                    "timeout_seconds": {"type": "number", "minimum": 0.1, "maximum": 120},
                    "max_response_bytes": {"type": "integer", "minimum": 1},
                    "max_retries": {"type": "integer", "minimum": 0, "maximum": HARD_MAX_RETRIES},
                    "max_pages": {"type": "integer", "minimum": 1, "maximum": HARD_MAX_PAGES},
                },
            },
            capabilities={
                "network": True,
                "streaming": False,
                "reads": has_reads,
                "writes": has_writes,
                "requires_approval": has_writes,
            },
            effect_policy="non_idempotent_write" if has_writes else "read",
            write_operations=sorted(n for n, o in cls.OPS.items() if o.write),
            timeout_seconds=30.0,
            output_limit_bytes=HARD_MAX_RESPONSE_BYTES,
            required_credential_types=[cls.CREDENTIAL_TYPE] if cls.NEEDS_CREDENTIAL else [],
            supported_modalities=["text"],
            supported_locales=["all"],
            source_reference=cls.SOURCE,
            verification_status="mock_verified",
            display=DisplayMetadata(
                label=cls.TITLE,
                group="connectors",
                examples=[
                    {
                        "operation": n,
                        "write": o.write,
                        **({"effect_class": o.effect_class} if o.effect_class else {}),
                    }
                    for n, o in cls.OPS.items()
                ],
                secret_hints=["credential_ref"],
            ),
        )

    @property
    def manifest(self) -> ModuleManifest:
        """The (cached) manifest for this connector."""
        cached = self.__class__.__dict__.get("_manifest_cache")
        if cached is None:
            cached = self.build_manifest()
            self.__class__._manifest_cache = cached  # type: ignore[attr-defined]
        return cached

    # -- credentials ------------------------------------------------------------------------
    def resolve_secret(self, context: ExecutionContext, cfg: dict[str, Any]) -> str:
        """Resolve ``config.credential_ref`` through the context's credential resolver."""
        ref = cfg.get("credential_ref")
        if not ref or not isinstance(ref, str):
            raise MissingCredentialError("config 'credential_ref' is required")
        if not _REF_RE.match(ref) or ref.lower().startswith(_TOKEN_PREFIXES):
            raise MissingCredentialError(
                "credential_ref must be a credential id from the secrets store, not a literal token"
            )
        resolver = context.credential_resolver
        if resolver is None:
            raise MissingCredentialError("no credential resolver is available in this context")
        self._check_credential_type(resolver, ref)
        try:
            value = resolver.resolve(
                CredentialReference(credential_id=ref, credential_type=self.CREDENTIAL_TYPE)
            )
        except Exception:
            value = None
        revealer = getattr(value, "reveal", None)
        secret = revealer() if callable(revealer) else value
        if not secret or not isinstance(secret, str):
            raise MissingCredentialError(f"credential '{ref}' did not resolve")
        return secret

    def _check_credential_type(self, resolver: Any, ref: str) -> None:
        """Refuse a credential whose stored type is not this connector's CREDENTIAL_TYPE.

        The resolver protocol returns only the secret, so the type comes from the store's
        metadata lookup (``get_metadata`` on the resolver, or on a workspace resolver's store).
        A resolver that exposes no metadata cannot be checked and is passed through.
        """
        if not self.NEEDS_CREDENTIAL and not self.CREDENTIAL_TYPE:
            return
        getter = getattr(resolver, "get_metadata", None)
        store = getattr(resolver, "store", None)
        workspace = getattr(resolver, "workspace_id", None)
        try:
            if callable(getter):
                meta = getter(ref)
            elif store is not None and workspace is not None and hasattr(store, "get_metadata"):
                meta = store.get_metadata(workspace, ref)
            else:
                return
        except Exception:
            return  # unknown credential: resolve() reports it uniformly
        actual = getattr(meta, "credential_type", None)
        if actual is not None and actual != self.CREDENTIAL_TYPE:
            raise MissingCredentialError(
                f"credential '{ref}' has type '{actual}' but this connector needs "
                f"'{self.CREDENTIAL_TYPE}'"
            )

    # -- execution --------------------------------------------------------------------------
    def build_http(
        self, cfg: dict[str, Any], secret: str, context: ExecutionContext | None = None
    ) -> ConnectorHttp:
        """Create the bounded HTTP helper; limits are clamped to the manifest's maxima.

        ``allowed_hosts`` comes only from the operator (constructor, context settings, env);
        a flow-config ``base_url`` is refused unless its host is the default API host or
        operator-allowed.
        """
        try:
            timeout = float(cfg.get("timeout_seconds") or 30.0)
            max_bytes = int(cfg.get("max_response_bytes") or DEFAULT_MAX_RESPONSE_BYTES)
            retries = int(cfg.get("max_retries", 2))
        except (TypeError, ValueError):
            raise InvalidConfigError(
                "timeout_seconds, max_response_bytes, max_retries must be numbers"
            ) from None
        default_host = (
            (urlsplit(self.DEFAULT_BASE_URL).hostname or "").lower()
            if self.DEFAULT_BASE_URL
            else None
        )
        override = cfg.get("base_url")
        base = override or self.DEFAULT_BASE_URL
        http = ConnectorHttp(
            transport=self.transport,
            allowed_hosts=self.effective_allowed_hosts(context),
            timeout=clamp_float(timeout, MIN_TIMEOUT_SECONDS, MAX_TIMEOUT_SECONDS),
            max_bytes=clamp(max_bytes, 1, HARD_MAX_RESPONSE_BYTES),
            max_retries=clamp(retries, 0, HARD_MAX_RETRIES),
            retry_budget=min(
                clamp_float(timeout, MIN_TIMEOUT_SECONDS, MAX_TIMEOUT_SECONDS),
                self.manifest.timeout_seconds or 30.0,
            ),
            secrets=secret_variants(secret) if secret else [],
            sleep=self.sleep,
            base_origin=str(base) if base else None,
            default_host=default_host,
            idempotency_key=(
                context.idempotency_key()
                if context is not None and self.SENDS_IDEMPOTENCY_KEY
                else None
            ),
        )
        if override:
            http.check_base_url(str(override))
        return http

    async def run(self, op: Op, params: Any, call: Call) -> dict[str, Any]:
        """Execute one validated operation; subclasses implement this."""
        raise NotImplementedError

    async def invoke(
        self,
        context: ExecutionContext,
        inputs: dict[str, Any],
        config: dict[str, Any] | None = None,
    ) -> ModuleResult:
        """Validate, gate on approval, resolve the credential, run, and redact."""
        cfg = config or {}
        secrets: list[str] = []
        op = self.OPS.get(str(inputs.get("operation")))
        if op is None:
            return ModuleResult.failure(InvalidParamsError.code, "unknown operation")
        try:
            try:
                params = op.model.model_validate(inputs.get("params") or {})
            except ValidationError as exc:
                where = "; ".join(
                    f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
                )
                raise InvalidParamsError(f"invalid params ({where})") from None
            # The input flag is ignored on purpose: inputs come from specs and model output.
            # Only a trusted caller (engine after human approval, operator) sets the context.
            if op.write and not context.approval_granted:
                raise ApprovalRequiredError(
                    f"operation '{op.name}' writes to an external system and needs human "
                    "approval granted by the workflow engine or an operator; the 'approved' "
                    "input grants nothing"
                )
            wants_secret = self.NEEDS_CREDENTIAL or bool(cfg.get("credential_ref"))
            secret = self.resolve_secret(context, cfg) if wants_secret else ""
            secrets = secret_variants(secret) if secret else []
            call = Call(
                secret=secret, http=self.build_http(cfg, secret, context), cfg=cfg, context=context
            )
            # Refresh adds secrets to the same list used for output/error redaction.
            secrets = call.http.secrets
            payload = await self.run(op, params, call)
            payload = scrub(payload, secrets)
            payload.update({"operation": op.name, "write": op.write})
            return ModuleResult.success(payload, metadata={"attempts": call.http.attempts_made})
        except IndeterminateEffectError as exc:
            return ModuleResult.indeterminate(redact(exc.message, secrets))
        except ConnectorError as exc:
            failure = ModuleResult.failure(
                exc.code,
                redact(exc.message, secrets),
                metadata={
                    "retryable": exc.retryable,
                    **(
                        {"retry_after_seconds": exc.retry_after_seconds}
                        if isinstance(exc, RateLimitedError) and exc.retry_after_seconds is not None
                        else {}
                    ),
                },
            )
            if exc.partial_outputs is not None:
                failure.outputs = {
                    **scrub(exc.partial_outputs, secrets),
                    "operation": op.name,
                    "write": op.write,
                }
            return failure
        except Exception as exc:
            return ModuleResult.failure(
                UpstreamError.code, redact(f"unexpected {type(exc).__name__}", secrets)
            )


def bearer(secret: str) -> dict[str, str]:
    """Authorization header for a bearer token."""
    return {"Authorization": f"Bearer {secret}"}


def basic(secret: str) -> dict[str, str]:
    """Authorization header for ``user:password`` basic auth."""
    return {"Authorization": "Basic " + base64.b64encode(secret.encode()).decode()}


def clamp_float(value: float, low: float, high: float) -> float:
    """Clamp ``value`` into ``[low, high]``."""
    return max(low, min(value, high))


def clamp(value: int, low: int, high: int) -> int:
    """Clamp ``value`` into ``[low, high]``."""
    return max(low, min(value, high))
