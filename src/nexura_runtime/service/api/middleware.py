# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Middleware for request ID generation, request safety limits and workspace/principal scoping.

Security-relevant behaviour implemented here (all server-side, never trusting client claims):

* ``BodySizeLimitMiddleware``: global request-body cap (declared and streamed bytes).
* ``HostOriginMiddleware``: DNS-rebinding / CSRF defence (Host allow-list, Origin allow-list).
* ``SecurityHeadersMiddleware``: nosniff / frame / referrer headers (and optional CSP).
* ``SecurityScopingMiddleware``: calls the installed ``AuthProvider`` (``app.state.auth_provider``)
  for every ``/api/v1`` request and stores the principal, workspace and role on
  ``request.state``. In the open-source edition that is ``LocalOwnerAuthProvider``: one
  principal, ``local-owner``, role ``admin``; no header names another principal. Under the
  shared profile a principal is always treated as ``authenticated-shared-service``, whatever
  its ``profile`` field says (fail closed).

The per-session token, loopback-client rule and JSON-only rule of the local profile are in
``local_session`` (``LocalSessionMiddleware``).

The shared-service authentication (bearer JWT or API key, membership re-validated on every
request) moved to the closed edition in
EDITIONS_PLAN step 7.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import uuid
from collections.abc import Awaitable, Callable, Iterable
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from fastapi import HTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from nexura_runtime.service.auth.models import Principal
from nexura_runtime.service.auth.profile import ProfileManager
from nexura_runtime.service.workspaces.repository import WorkspaceRepository

if TYPE_CHECKING:
    from nexura_runtime.service.extensions import AuthProvider

logger = logging.getLogger("nexura.service.security")

DEFAULT_MAX_BODY_BYTES = 10 * 1024 * 1024
#: Larger cap for archive imports (25 MiB zip, base64 encoded inside JSON).
DEFAULT_MAX_IMPORT_BODY_BYTES = 40 * 1024 * 1024
LARGE_BODY_PATHS = ("/api/v1/workflows/import",)

#: Maximum JSON container nesting accepted in request bodies. Deeper documents are rejected
#: centrally (422) so they can never be persisted and later break list/serialisation endpoints.
MAX_JSON_DEPTH = 64
_JSON_TOKEN = re.compile(rb'["\\\[\]{}]')

LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
#: Host names accepted in local mode without configuration. Tests pass ``http://127.0.0.1``
#: as their client base URL; ``testserver`` is not accepted outside an explicit configuration.
DEFAULT_ALLOWED_HOSTS = LOOPBACK_HOSTS
DOC_PATHS = frozenset({"/docs", "/openapi.json", "/redoc", "/docs/oauth2-redirect"})


def route_path(scope: Scope) -> str:
    """The request path as this app's routes see it: ``scope["path"]`` minus ``root_path``.

    Starlette keeps the full path in ``scope["path"]`` and the mount prefix in
    ``root_path`` when the app is mounted (``parent.mount("/svc", app)``) or served with
    ``--root-path``; its router matches on the remainder (``starlette._utils.get_route_path``,
    reimplemented here because that module is private). Every security decision taken on a
    path (authentication, the session token, size limits, cache headers) uses this value, so
    a mount prefix can never move a route out of what those checks cover.
    """
    path: str = scope.get("path", "")
    root = scope.get("root_path", "") or ""
    if not root or not path.startswith(root):
        return path
    if path == root:
        return ""
    if path[len(root)] == "/":
        return path[len(root) :]
    return path


async def require_shared_principal(request: Request) -> None:
    """Fail closed at the API route boundary if shared authentication was bypassed."""
    manager = getattr(request.app.state, "profile_manager", None)
    if (
        manager is not None
        and manager.profile == "authenticated-shared-service"
        and not isinstance(getattr(request.state, "principal", None), Principal)
    ):
        raise HTTPException(
            status_code=401,
            detail={
                "code": "UNAUTHENTICATED",
                "message": "An authenticated principal is required.",
            },
        )


def _env_list(name: str) -> list[str]:
    return [p.strip() for p in os.environ.get(name, "").split(",") if p.strip()]


def _split_host(host_header: str) -> str:
    """Extract the lower-cased hostname (no port, no brackets) from a Host header value."""
    raw = host_header.strip()
    if raw.startswith("[") and "]" in raw:
        return raw[1 : raw.index("]")].lower()
    if raw.count(":") == 1:
        raw = raw.split(":", 1)[0]
    return raw.lower()


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _is_loopback(host: str) -> bool:
    if host in LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _error(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code, content={"detail": {"code": code, "message": message}}
    )


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Enforces request tracing by ensuring every request and response has an X-Request-ID."""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        incoming_id = request.headers.get("x-request-id")
        request_id = incoming_id if incoming_id else f"req_{uuid.uuid4().hex[:16]}"
        request.state.request_id = request_id

        # Pass through to next middleware/app
        response = await call_next(request)
        response.headers["x-request-id"] = request_id
        return response


class _JsonDepthScanner:
    """Incremental, non-recursive scan of raw JSON bytes tracking bracket nesting depth."""

    def __init__(self, max_depth: int) -> None:
        self.max_depth = max_depth
        self.depth = 0
        self.in_string = False
        self.escaped = False

    def feed(self, chunk: bytes) -> bool:
        """Return True when the nesting depth has exceeded the maximum."""
        pos = 0
        end = len(chunk)
        while pos < end:
            if self.in_string:
                if self.escaped:
                    self.escaped = False
                    pos += 1
                    continue
                m = _JSON_TOKEN.search(chunk, pos)
                if m is None:
                    return False
                ch = chunk[m.start()]
                pos = m.end()
                if ch == 0x5C:  # backslash
                    self.escaped = True
                elif ch == 0x22:
                    self.in_string = False
                continue
            m = _JSON_TOKEN.search(chunk, pos)
            if m is None:
                return False
            ch = chunk[m.start()]
            pos = m.end()
            if ch == 0x22:
                self.in_string = True
            elif ch in (0x5B, 0x7B):
                self.depth += 1
                if self.depth > self.max_depth:
                    return True
            elif ch in (0x5D, 0x7D) and self.depth > 0:
                self.depth -= 1
        return False


class BodySizeLimitMiddleware:
    """Reject oversized request bodies before they are buffered or parsed.

    Enforces both the declared ``Content-Length`` and the number of bytes actually streamed
    (chunked uploads), answering ``413`` and ceasing to read once the cap is exceeded.
    """

    def __init__(
        self,
        app: ASGIApp,
        max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
        large_body_bytes: int = DEFAULT_MAX_IMPORT_BODY_BYTES,
        large_body_paths: Iterable[str] = LARGE_BODY_PATHS,
    ) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes
        self.large_body_bytes = max(large_body_bytes, max_body_bytes)
        self.large_body_paths = tuple(large_body_paths)

    def _limit_for(self, path: str) -> int:
        if any(path == p or path.rstrip("/") == p for p in self.large_body_paths):
            return self.large_body_bytes
        return self.max_body_bytes

    @staticmethod
    async def _reject(send: Send, limit: int, *, too_deep: bool = False) -> None:
        if too_deep:
            status, code = 422, "JSON_TOO_DEEP"
            message = f"JSON body nesting exceeds the maximum depth of {MAX_JSON_DEPTH}."
        else:
            status, code = 413, "REQUEST_TOO_LARGE"
            message = f"Request body exceeds the {limit} byte limit."
        body = json.dumps({"detail": {"code": code, "message": message}}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"connection", b"close"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        limit = self._limit_for(route_path(scope))
        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    declared = -1
                if declared < 0 or declared > limit:
                    await self._reject(send, limit)
                    return
                break

        content_type = b""
        for name, value in scope.get("headers", []):
            if name == b"content-type":
                content_type = value.lower()
                break
        scanner = (
            _JsonDepthScanner(MAX_JSON_DEPTH)
            if b"multipart/" not in content_type and b"octet-stream" not in content_type
            else None
        )
        state = {"seen": 0, "overflow": False, "started": False, "deep": False}

        async def limited_receive() -> Message:
            if state["overflow"]:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                state["seen"] += len(message.get("body", b""))
                if state["seen"] > limit:
                    state["overflow"] = True
                    return {"type": "http.disconnect"}
                if scanner is not None and scanner.feed(message.get("body", b"")):
                    state["overflow"] = True
                    state["deep"] = True
                    return {"type": "http.disconnect"}
            return message

        async def guarded_send(message: Message) -> None:
            if state["overflow"]:
                # Whatever the downstream app produced after we cut the body off is replaced.
                if not state["started"]:
                    state["started"] = True
                    await self._reject(send, limit, too_deep=bool(state["deep"]))
                return
            await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except BaseException:
            if not state["overflow"]:
                raise
        if state["overflow"] and not state["started"]:
            state["started"] = True
            await self._reject(send, limit, too_deep=bool(state["deep"]))


class SecurityHeadersMiddleware:
    """Adds defensive response headers (and an optional Content-Security-Policy)."""

    def __init__(self, app: ASGIApp, content_security_policy: str | None = None) -> None:
        self.app = app
        self.csp = content_security_policy

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = route_path(scope)

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                existing = {k.lower() for k, _ in headers}
                extra = {
                    b"x-content-type-options": b"nosniff",
                    b"x-frame-options": b"DENY",
                    b"referrer-policy": b"no-referrer",
                }
                if path.startswith("/api/v1"):
                    # Responses may carry secrets (signing secrets, API keys): never cache.
                    extra[b"cache-control"] = b"no-store"
                    extra[b"pragma"] = b"no-cache"
                if self.csp and path not in DOC_PATHS:
                    extra[b"content-security-policy"] = self.csp.encode()
                for k, v in extra.items():
                    if k not in existing:
                        headers.append((k, v))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)


class HostOriginMiddleware(BaseHTTPMiddleware):
    """DNS-rebinding and cross-origin protection for the REST service.

    * ``local-owner`` profile (ambient admin authority): the ``Host`` header must be
      ``localhost``, a loopback address (``127.0.0.1``, ``[::1]``), ``bound_host`` or listed in
      ``allowed_hosts`` / ``NEXURA_ALLOWED_HOSTS``, with any port. Any other name, including a
      dotless one (resolvable on a LAN through the search domain, LLMNR or NetBIOS, so usable
      for DNS rebinding), is refused. Other IP addresses are accepted only with the explicit
      insecure network opt-in. If an ``Origin`` header is present it must be a loopback
      origin on any port, or match ``allowed_origins`` / the host of ``allowed_hosts``.
      State-changing requests also need the per-session token (``local_session``).
    * ``authenticated-shared-service`` profile: ``Host`` is only validated when ``allowed_hosts``
      is configured; a browser ``Origin`` must be in ``allowed_origins`` (non-browser clients
      send no ``Origin`` and are unaffected).
    * Remote single-operator mode (``local-owner`` profile behind the operator token, see
      ``operator_token``): like the shared profile, and an ``Origin`` equal to the request's own
      origin (its ``host[:port]`` matches the ``Host`` header) is also accepted, so Studio served
      from the same origin as the API (a reverse proxy) works. Every request carries the
      operator token, which a browser never sends on its own, so DNS rebinding and CSRF gain
      nothing.
    """

    def __init__(
        self,
        app: ASGIApp,
        allowed_hosts: Iterable[str] | None = None,
        allowed_origins: Iterable[str] | None = None,
        bound_host: str | None = None,
    ) -> None:
        super().__init__(app)
        configured = {h.strip().lower() for h in (allowed_hosts or []) if h.strip()}
        configured |= {h.lower() for h in _env_list("NEXURA_ALLOWED_HOSTS")}
        self.configured_hosts = {_split_host(h) for h in configured}
        self.bound_host = _split_host(bound_host) if bound_host else None
        origins = [o.rstrip("/").lower() for o in (allowed_origins or []) if o.strip()]
        origins += [o.rstrip("/").lower() for o in _env_list("NEXURA_ALLOWED_ORIGINS")]
        self.allowed_origins = set(origins)

    def _host_allowed(self, host: str, *, local_mode: bool, network_opt_in: bool) -> bool:
        if _is_loopback(host) or host in self.configured_hosts:
            return True
        if self.bound_host and self.bound_host not in ("0.0.0.0", "::") and host == self.bound_host:
            return True
        if not local_mode:
            return not self.configured_hosts  # unconfigured shared mode: no Host restriction
        # With the explicit insecure network opt-in, a browser on the LAN may name this machine
        # by IP address. An IP literal is never a DNS-rebinding vector; names still need
        # configuring (allowed_hosts / NEXURA_ALLOWED_HOSTS).
        return network_opt_in and _is_ip_literal(host)

    def _origin_allowed(self, origin: str, *, local_mode: bool) -> bool:
        normalized = origin.rstrip("/").lower()
        if normalized in self.allowed_origins:
            return True
        parsed = urlparse(origin)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return False
        if not local_mode:
            return False
        host = parsed.hostname.lower()
        return _is_loopback(host) or host in self.configured_hosts

    @staticmethod
    def _same_origin(origin: str, host_header: str) -> bool:
        parsed = urlparse(origin)
        if parsed.scheme not in ("http", "https") or not parsed.netloc or not host_header:
            return False
        return parsed.netloc.lower() == host_header.strip().lower()

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        from nexura_runtime.service.api.operator_token import single_operator_mode

        pm: ProfileManager | None = getattr(request.app.state, "profile_manager", None)
        remote_operator = single_operator_mode(request.app)
        local_mode = (pm is None or pm.profile == "local-owner") and not remote_operator

        host_header = request.headers.get("host", "")
        if local_mode or self.configured_hosts:
            if not host_header:
                return _error(400, "INVALID_HOST", "Missing Host header.")
            policy = getattr(request.app.state, "local_session", None)
            network_opt_in = bool(policy is not None and policy.network_allowed())
            if not self._host_allowed(
                _split_host(host_header), local_mode=local_mode, network_opt_in=network_opt_in
            ):
                return _error(400, "INVALID_HOST", "Host is not authorized for this service.")

        origin = request.headers.get("origin")
        if (
            origin is not None
            and not self._origin_allowed(origin, local_mode=local_mode)
            and not (remote_operator and self._same_origin(origin, host_header))
        ):
            return _error(403, "CROSS_ORIGIN_FORBIDDEN", "Cross-origin request is forbidden.")
        return await call_next(request)


class AuthFailure(Exception):
    """Internal: authentication/authorization failure carrying a safe client response."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message

    def response(self) -> JSONResponse:
        return _error(self.status_code, self.code, self.message)


_VALID_ROLES = frozenset({"admin", "editor", "viewer"})


def invalid_identity_reason(identity: object, *, without_membership: bool) -> str | None:
    """Why an ``AuthProvider`` result is unusable, or ``None`` when it is a valid ``Identity``.

    ``principal`` must be a ``Principal`` with a non-empty id. ``workspace_id`` must be a
    non-empty string and ``role`` one of ``admin``/``editor``/``viewer``; both may be ``None``
    together only when ``without_membership`` is true: the request is one of the routes the
    provider lists in ``routes_without_membership``.
    """
    from nexura_runtime.service.extensions import Identity

    if not isinstance(identity, Identity):
        return f"expected an Identity, got {type(identity).__name__}"
    principal = identity.principal
    if not isinstance(principal, Principal):
        return f"principal must be a Principal, got {type(principal).__name__}"
    if not isinstance(principal.principal_id, str) or not principal.principal_id:
        return "principal_id must be a non-empty string"
    ws, role = identity.workspace_id, identity.role
    if ws is None and role is None and without_membership:
        return None
    if not isinstance(ws, str) or not ws:
        return "workspace_id must be a non-empty string outside routes_without_membership"
    if role not in _VALID_ROLES:
        return f"role must be one of admin, editor, viewer, got {role!r}"
    return None


def route_without_membership(provider: object, method: str, path: str) -> bool:
    """Whether ``provider`` declares ``(method, path)`` in ``routes_without_membership``.

    An ``AuthProvider`` may list exact ``(method, path)`` pairs on which an authenticated
    principal with no workspace membership is admitted (``Identity`` with ``workspace_id`` and
    ``role`` both ``None``); the closed edition lists organisation invite acceptance. A missing
    or malformed declaration means none.
    """
    declared = getattr(provider, "routes_without_membership", ())
    try:
        return (method, path) in frozenset(declared)
    except TypeError:
        return False


class SecurityScopingMiddleware(BaseHTTPMiddleware):
    """Derives principal, workspace scoping, and role server-side, enforcing security boundaries."""

    #: Only liveness/readiness are public (and sanitised); API docs require auth in shared mode.
    PUBLIC_PATHS = {"/healthz", "/readyz"}

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        # The route path, not request.url.path: under a mount the URL keeps the prefix
        # (/svc/api/v1/...) while the routes still match, which used to skip authentication.
        path = route_path(request.scope)

        profile_manager: ProfileManager | None = getattr(request.app.state, "profile_manager", None)
        if profile_manager is None:
            profile_manager = ProfileManager("local-owner")
            request.app.state.profile_manager = profile_manager

        workspace_repo: WorkspaceRepository | None = getattr(
            request.app.state, "workspace_repo", None
        )
        if workspace_repo is None:
            workspace_repo = WorkspaceRepository()
            request.app.state.workspace_repo = workspace_repo

        shared = profile_manager.profile == "authenticated-shared-service"
        # Unowned legacy resources are accessible to nobody once the service is shared.
        workspace_repo.fail_closed_unowned = shared

        is_doc_path = path in DOC_PATHS
        needs_auth = path.startswith("/api/v1") or (shared and is_doc_path)
        if not needs_auth:
            # Set default workspace state for unauthenticated paths
            if not hasattr(request.state, "workspace_id"):
                request.state.workspace_id = "default"
            return await call_next(request)

        # Extract target workspace from scoped path if present:
        # e.g. /api/v1/workspaces/{target_ws}/...
        path_ws: str | None = None
        if path.startswith("/api/v1/workspaces/"):
            rest = path[len("/api/v1/workspaces/") :]
            parts = rest.split("/")
            if parts and parts[0]:
                path_ws = parts[0]

        incoming_ws = request.headers.get("x-workspace-id")
        target_ws = path_ws or incoming_ws or None

        # The installed AuthProvider decides who the caller is (see service.extensions).
        provider: AuthProvider | None = getattr(request.app.state, "auth_provider", None)
        if provider is None and not shared:
            from nexura_runtime.service.api.auth_providers import LocalOwnerAuthProvider

            provider = LocalOwnerAuthProvider()
        if provider is None or (shared and not provider.is_authenticating):
            # The shared profile never falls back to unauthenticated access.
            return _error(
                503, "AUTH_PROVIDER_REQUIRED", "Authentication is not configured for this service."
            )
        try:
            identity = provider.authenticate(request, target_ws)
        except AuthFailure as failure:
            return failure.response()
        invalid = invalid_identity_reason(
            identity, without_membership=route_without_membership(provider, request.method, path)
        )
        if invalid is not None:
            # A provider bug must never turn into access: many routes read a missing principal
            # as the local owner, so a malformed identity is refused before any route runs.
            logger.error(
                "auth provider %r returned an invalid identity: %s",
                getattr(provider, "name", type(provider).__name__),
                invalid,
            )
            return _error(
                500, "AUTH_PROVIDER_INVALID_IDENTITY", "The authentication provider failed."
            )
        principal = identity.principal
        if shared and principal.profile != "authenticated-shared-service":
            # Fail closed: routes grant local-owner behaviour from ``principal.profile``, and the
            # model default is "local-owner". A shared-service principal never gets it.
            logger.warning(
                "auth provider %r returned a principal with profile %r under the shared profile;"
                " treating it as authenticated-shared-service",
                getattr(provider, "name", type(provider).__name__),
                principal.profile,
            )
            principal = principal.model_copy(update={"profile": "authenticated-shared-service"})
        request.state.principal = principal
        request.state.workspace_id = identity.workspace_id
        request.state.role = identity.role

        response = await call_next(request)
        if request.state.workspace_id is not None:
            response.headers["x-workspace-id"] = request.state.workspace_id
        return response
