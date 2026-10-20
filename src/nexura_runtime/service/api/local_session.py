# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Per-session local token, loopback-client rule and JSON-only rule for local servers.

``nexura-runtime serve`` (the REST service, ``local-owner`` profile) and ``nexura-studio`` give
every caller on this machine the local owner's authority. A web page in the same browser (any
site, or another local tool on another port) can send requests to them, so state-changing
requests carry a secret the page cannot know:

* **Session token.** Each process start generates ``secrets.token_urlsafe(32)``, or uses
  ``NEXURA_STUDIO_TOKEN`` when it is set (``create_api_app(session_token=...)`` and
  ``create_app(api_token=...)`` take one too). It is on ``app.state.session_token``. Every
  request whose method is not GET/HEAD/OPTIONS must send it in ``X-Nexura-Session``; a missing
  or wrong value is ``403`` ``SESSION_TOKEN_REQUIRED`` / ``SESSION_TOKEN_INVALID``. Studio reads
  it from its own origin (``GET /api/v1/local-session`` or the served index page); the endpoint
  refuses requests a browser marks cross-site or same-site (``Sec-Fetch-Site``) and requests
  whose ``Origin`` is not the server's own, and responses carry no CORS headers, so another
  site's page cannot read it.
* **Loopback clients only.** Unless the operator opted in to network access
  (``allow_unauthenticated_network`` / ``NEXURA_INSECURE_ALLOW_UNAUTHENTICATED_NETWORK``), a
  request whose peer address (ASGI ``client``) is not loopback is ``403``
  ``NON_LOOPBACK_CLIENT``. This covers embedding (``uvicorn.run(create_api_app(),
  host="0.0.0.0")``), where the startup bind guard never sees the real bind.
* **JSON only.** In the REST service, a state-changing request with a body type a browser can
  send without a CORS preflight (``text/plain``, form, multipart) is ``415``
  ``UNSUPPORTED_MEDIA_TYPE``. Exceptions: signed webhook deliveries, and raw ``text/plain``
  Python source sent to ``/workflows/import/python`` by a non-browser client (no ``Origin`` or
  ``Sec-Fetch-Site`` header).

Signed webhook deliveries and routes an extension declared in ``public_paths`` are not
checked for the token: their senders are other services, authenticated by their own signature.
The ``authenticated-shared-service`` profile is unaffected: it authenticates every request.
"""

from __future__ import annotations

import hmac
import ipaddress
import os
import re
import secrets
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from nexura_runtime.service.api.bind_guard import insecure_network_opt_in
from nexura_runtime.service.api.middleware import route_path

#: Header that carries the session token on state-changing requests.
SESSION_HEADER = "X-Nexura-Session"
#: Environment variable that fixes the token (otherwise a random one per process start).
SESSION_TOKEN_ENV = "NEXURA_STUDIO_TOKEN"
#: Path of the same-origin endpoint Studio reads the token from.
SESSION_TOKEN_PATH = "/api/v1/local-session"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
#: Body types a browser sends cross-origin without a CORS preflight.
SIMPLE_BODY_TYPES = ("text/plain", "application/x-www-form-urlencoded", "multipart/form-data")

_WEBHOOK_DELIVERY = re.compile(r"^/api/v1/triggers/webhooks/[^/]+/deliver/?$")
_PYTHON_IMPORT = re.compile(r"^/api/v1/workflows/import/python/?$")

ErrorBody = Callable[[str, str], dict[str, Any]]


def detail_error_body(code: str, message: str) -> dict[str, Any]:
    """Error body of the REST service (``{"detail": {"code", "message"}}``)."""
    return {"detail": {"code": code, "message": message}}


def studio_error_body(code: str, message: str) -> dict[str, Any]:
    """Error body of the built-in Studio (``{"error": {"code", "message"}}``)."""
    return {"error": {"code": code, "message": message}}


def resolve_session_token(explicit: str | None = None) -> tuple[str, bool]:
    """Return ``(token, configured)``: ``explicit``, else ``NEXURA_STUDIO_TOKEN``, else random.

    An empty or whitespace-only value counts as not set. ``configured`` is true when the token
    came from the caller or the environment rather than ``secrets.token_urlsafe(32)``.
    """
    for candidate in (explicit, os.environ.get(SESSION_TOKEN_ENV)):
        if candidate is not None and candidate.strip():
            return candidate, True
    return secrets.token_urlsafe(32), False


def is_loopback_client(scope: Scope) -> bool:
    """True when the ASGI peer address is loopback (or absent, as on a Unix socket)."""
    client = scope.get("client")
    if not client:
        return True
    host = str(client[0]).strip().lower()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def token_read_refusal(headers: Headers, scheme: str) -> str | None:
    """Why the session token must not be handed to this request, or ``None`` when it may.

    Browsers send ``Sec-Fetch-Site`` (a header page scripts cannot set); only ``same-origin``
    and ``none`` (typed in the address bar) are accepted. Without it (older browsers, curl,
    scripts), an ``Origin`` header must name this server (``scheme://Host``).
    """
    site = headers.get("sec-fetch-site")
    if site is not None:
        if site.strip().lower() in ("same-origin", "none"):
            return None
        return f"Sec-Fetch-Site '{site}' is not same-origin"
    origin = headers.get("origin")
    if origin is None:
        return None
    host = headers.get("host", "")
    if origin.rstrip("/").lower() == f"{scheme}://{host}".lower():
        return None
    return "Origin is not this server"


def _content_type(headers: Headers) -> str:
    return headers.get("content-type", "").split(";", 1)[0].strip().lower()


@dataclass
class LocalSessionPolicy:
    """What :class:`LocalSessionMiddleware` enforces for one app (kept on ``app.state``)."""

    token: str
    #: True when the token came from the caller or ``NEXURA_STUDIO_TOKEN``.
    configured: bool = False
    #: The explicit insecure network opt-in given when the app was built.
    allow_network: bool = False
    #: Loopback-client rule on (off when the server authenticates every request).
    require_loopback_client: bool = True
    #: Refuse preflight-free body types on state-changing requests (REST service).
    require_json: bool = False
    #: Exact paths (or prefixes ending in ``/``) that are not checked for the token.
    exempt_paths: set[str] = field(default_factory=set)

    def network_allowed(self) -> bool:
        """The operator opted in to unauthenticated network access."""
        return insecure_network_opt_in(self.allow_network)

    def token_matches(self, supplied: str | None) -> bool:
        """Constant-time comparison of ``supplied`` with the session token."""
        if not supplied:
            return False
        return hmac.compare_digest(supplied.encode(), self.token.encode())

    def is_exempt(self, path: str) -> bool:
        """Signed webhook deliveries and declared public paths skip the token."""
        if _WEBHOOK_DELIVERY.match(path):
            return True
        return any(
            path == p or (p.endswith("/") and path.startswith(p)) or path.startswith(p + "/")
            for p in self.exempt_paths
        )

    def add_exempt_paths(self, paths: Iterable[str]) -> None:
        """Exempt extension ``public_paths`` (outside ``/api/v1``) from the token."""
        for p in paths:
            if p and not p.startswith("/api/"):
                self.exempt_paths.add(p.rstrip("/") or "/")


class LocalSessionMiddleware:
    """Enforce :class:`LocalSessionPolicy` (``app.state.local_session``) on local servers.

    ``is_local`` decides per request whether the app is in its unauthenticated local mode (the
    REST service passes "profile is ``local-owner``"; Studio is always local). Outside local
    mode every request passes through unchanged.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        is_local: Callable[[Any], bool] | None = None,
        error_body: ErrorBody = detail_error_body,
    ) -> None:
        self.app = app
        self.is_local = is_local
        self.error_body = error_body

    async def _refuse(
        self, scope: Scope, receive: Receive, send: Send, status: int, code: str, message: str
    ) -> None:
        response = JSONResponse(status_code=status, content=self.error_body(code, message))
        await response(scope, receive, send)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        app = scope.get("app")
        state = getattr(app, "state", None)
        policy: LocalSessionPolicy | None = getattr(state, "local_session", None)
        if policy is None or (self.is_local is not None and not self.is_local(app)):
            await self.app(scope, receive, send)
            return

        if (
            policy.require_loopback_client
            and not is_loopback_client(scope)
            and not policy.network_allowed()
        ):
            await self._refuse(
                scope,
                receive,
                send,
                403,
                "NON_LOOPBACK_CLIENT",
                "This local server only accepts connections from this machine. Bind it to "
                "127.0.0.1, or see SECURITY_MODEL.md for the explicit network opt-in.",
            )
            return

        method = scope.get("method", "GET").upper()
        path = route_path(scope)
        if scope["type"] != "http" or method in SAFE_METHODS or policy.is_exempt(path):
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        supplied = headers.get(SESSION_HEADER)
        if supplied is None:
            await self._refuse(
                scope,
                receive,
                send,
                403,
                "SESSION_TOKEN_REQUIRED",
                f"State-changing requests need the {SESSION_HEADER} header. Studio reads it "
                f"from GET {SESSION_TOKEN_PATH} on its own origin; see "
                "docs/studio-and-service.md.",
            )
            return
        if not policy.token_matches(supplied):
            await self._refuse(
                scope,
                receive,
                send,
                403,
                "SESSION_TOKEN_INVALID",
                f"The {SESSION_HEADER} header does not match this server's session token "
                "(it changes when the server restarts).",
            )
            return

        if policy.require_json:
            ctype = _content_type(headers)
            if ctype.startswith(SIMPLE_BODY_TYPES):
                from_browser = "origin" in headers or "sec-fetch-site" in headers
                raw_python = (
                    ctype == "text/plain" and _PYTHON_IMPORT.match(path) and not from_browser
                )
                if not raw_python:
                    await self._refuse(
                        scope,
                        receive,
                        send,
                        415,
                        "UNSUPPORTED_MEDIA_TYPE",
                        "State-changing requests must send Content-Type: application/json.",
                    )
                    return
        await self.app(scope, receive, send)


def build_session_token_router(*, is_local: Callable[[Any], bool] | None = None) -> APIRouter:
    """``GET /api/v1/local-session``: the session token, for Studio on the same origin.

    Refused (403 ``SESSION_TOKEN_CROSS_SITE``) when the browser marks the request cross-site
    or same-site, or when its ``Origin`` is not this server; outside local mode it is 404.
    """
    router = APIRouter(tags=["local-session"])

    @router.get("/local-session", summary="Read the per-session local token (same origin only)")
    async def read_session_token(request: Request) -> dict[str, str]:
        """Return ``{"token", "header"}`` to a same-origin page or a local non-browser client."""
        app = request.app
        policy: LocalSessionPolicy | None = getattr(app.state, "local_session", None)
        if policy is None or (is_local is not None and not is_local(app)):
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "SESSION_TOKEN_NOT_USED",
                    "message": "This server authenticates requests; it has no session token.",
                },
            )
        refusal = token_read_refusal(request.headers, request.url.scheme)
        if refusal is not None:
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "SESSION_TOKEN_CROSS_SITE",
                    "message": f"The session token is only served to this server's own pages "
                    f"({refusal}).",
                },
            )
        return {"token": policy.token, "header": SESSION_HEADER}

    return router


__all__ = [
    "SAFE_METHODS",
    "SESSION_HEADER",
    "SESSION_TOKEN_ENV",
    "SESSION_TOKEN_PATH",
    "LocalSessionMiddleware",
    "LocalSessionPolicy",
    "build_session_token_router",
    "detail_error_body",
    "is_loopback_client",
    "resolve_session_token",
    "studio_error_body",
    "token_read_refusal",
]
