# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Safe HTTP client enforcing DestinationPolicy on all outbound requests and redirects."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from typing import Any

import httpcore
import httpx

from nexura_agents.network.errors import DestinationPolicyDeniedError
from nexura_agents.network.policy import DestinationPolicy

#: Well-known credential headers (kept for reference; a cross-origin redirect forwards only
#: ``SAFE_REDIRECT_HEADERS``, so every other caller-supplied header is dropped).
CREDENTIAL_HEADERS = frozenset(
    {"authorization", "proxy-authorization", "cookie", "x-api-key", "api-key", "x-auth-token"}
)

#: Headers forwarded on a cross-origin redirect. ``content-type`` only when the body is kept.
SAFE_REDIRECT_HEADERS = frozenset(
    {"accept", "accept-encoding", "accept-language", "content-type", "user-agent"}
)


class _PinnedBackend(httpcore.AsyncNetworkBackend):
    """Resolve once, validate every address with the policy, connect to a validated address."""

    def __init__(self, policy: DestinationPolicy) -> None:
        self._inner: httpcore.AsyncNetworkBackend = httpcore.AnyIOBackend()
        self._policy = policy

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        """Connect only to an address of ``host`` that passes the destination policy."""
        addresses = await asyncio.to_thread(self._policy.resolve, host, port)
        try:
            self._policy.check_addresses(f"tcp://{host}:{port}", host, addresses)
        except DestinationPolicyDeniedError as exc:
            raise httpcore.ConnectError(str(exc)) from exc
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
        """Unix sockets are never used for outbound tool traffic."""
        raise httpcore.ConnectError("unix sockets are not permitted")

    async def sleep(self, seconds: float) -> None:
        """Delegate to the inner backend."""
        await self._inner.sleep(seconds)


def pinned_transport(policy: DestinationPolicy) -> httpx.AsyncBaseTransport:
    """An httpx transport whose connections go only to addresses that pass ``policy``.

    TLS still verifies the certificate against the URL's host name; only the TCP target is
    pinned. Relies on httpx's private ``_pool`` attribute (there is no public hook).
    """
    transport = httpx.AsyncHTTPTransport()
    transport._pool._network_backend = _PinnedBackend(policy)
    return transport


class SafeHttpClient:
    """Outbound HTTP client that validates target URLs and redirects against DestinationPolicy."""

    def __init__(
        self,
        policy: DestinationPolicy | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.policy = policy or DestinationPolicy()
        self._client = httpx.AsyncClient(
            transport=transport if transport is not None else pinned_transport(self.policy),
            timeout=timeout,
            follow_redirects=False,  # We manually manage redirects to inspect each hop
        )

    async def __aenter__(self) -> SafeHttpClient:
        await self._client.__aenter__()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self._client.__aexit__(exc_type, exc_val, exc_tb)

    async def close(self) -> None:
        """Close underlying HTTP client connections."""
        await self._client.aclose()

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        content: Any = None,
        data: Any = None,
        json: Any = None,
        params: Any = None,
        follow_redirects: bool = True,
        max_redirects: int = 10,
    ) -> httpx.Response:
        """Execute request with strict policy verification on original and redirect URLs."""
        current_url = url
        await asyncio.to_thread(self.policy.check_url, current_url)  # DNS lookups block

        request = self._client.build_request(
            method,
            current_url,
            headers=headers,
            content=content,
            data=data,
            json=json,
            params=params,
        )

        redirects_followed = 0
        response = await self._client.send(request)

        while follow_redirects and response.is_redirect:
            location = response.headers.get("location")
            if not location:
                break
            if redirects_followed >= max_redirects:
                await response.aclose()
                raise httpx.TooManyRedirects(
                    f"Exceeded {max_redirects} redirects.", request=response.request
                )
            redirects_followed += 1

            # Resolve relative redirect URLs against the request URL
            redirect_url = str(response.request.url.copy_with().join(location))

            # Strictly re-evaluate policy on redirect target!
            await asyncio.to_thread(self.policy.check_url, redirect_url)

            previous = response.request
            next_url = httpx.URL(redirect_url)
            same_origin = (previous.url.scheme, previous.url.host, previous.url.port) == (
                next_url.scheme,
                next_url.host,
                next_url.port,
            )
            # 307/308 keep the method and body. 303 becomes GET (HEAD stays HEAD); 301/302 turn
            # only POST into GET (RFC 9110, as httpx does). The body goes with a kept method.
            if response.status_code in (307, 308) or previous.method == "HEAD":
                next_method = previous.method
            elif response.status_code == 303 or (
                response.status_code in (301, 302) and previous.method == "POST"
            ):
                next_method = "GET"
            else:
                next_method = previous.method
            keep_body = next_method == previous.method
            next_headers = {
                k: v
                for k, v in previous.headers.items()
                if k.lower() not in ("host", "content-length")
                and (same_origin or k.lower() in SAFE_REDIRECT_HEADERS)
                and (keep_body or k.lower() != "content-type")
            }
            request = self._client.build_request(
                next_method,
                redirect_url,
                headers=next_headers,
                content=previous.content if keep_body else None,
            )
            await response.aclose()
            response = await self._client.send(request)

        return response

    @asynccontextmanager
    async def stream(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json: Any = None,
        refuse_redirect: bool = True,
    ) -> AsyncIterator[httpx.Response]:
        """Open a streaming response (e.g. server-sent events) after the policy check.

        Redirects are never followed: a stream endpoint that redirects is an error
        (``httpx.HTTPStatusError``). A caller that checks and follows each hop itself (web
        fetch) passes ``refuse_redirect=False`` to receive the 3xx response.
        """
        await asyncio.to_thread(self.policy.check_url, url)  # DNS lookups block
        async with self._client.stream(method, url, headers=headers, json=json) as response:
            if refuse_redirect and response.is_redirect:
                raise httpx.HTTPStatusError(
                    f"Stream endpoint answered with a redirect (HTTP {response.status_code}); "
                    "redirects are not followed.",
                    request=response.request,
                    response=response,
                )
            yield response

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        """Send a GET request."""
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        """Send a POST request."""
        return await self.request("POST", url, **kwargs)
