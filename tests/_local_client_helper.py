"""Central in-process test-client setup for the local server's session token (P28 step 7 review).

The local REST service and Studio now (R1-R3 of the P28 step 7 security review):

* require the per-session token (``X-Nexura-Session``) on every state-changing request;
* refuse peers whose address is not loopback (Starlette's ``TestClient`` reports
  ``"testclient"``);
* accept only loopback ``Host`` names (Starlette's default ``testserver`` and placeholders such
  as ``http://test`` are dotless names a LAN could resolve).

Rather than edit every test, :func:`install` adjusts the two in-process transports
(``starlette.testclient.TestClient`` and ``httpx.ASGITransport``) for apps that have a session
token (``app.state.session_token``):

* a request to the REST service at a dotless placeholder host (``testserver``, ``test``,
  ``t``, ...) whose ``Host`` header was not set explicitly goes to ``http://127.0.0.1`` with
  the same port, like a client built with ``base_url="http://127.0.0.1"``;
* ``TestClient``'s default peer ``("testclient", 50000)`` becomes ``("127.0.0.1", 50000)``;
* a state-changing request gets the app's token, the way Studio sends it, unless it already
  has an ``X-Nexura-Session`` header or it imitates another site's page: an ``Origin`` other
  than the client's own, ``http://testserver`` or the Vite dev Studio (port 5173). Such a
  page cannot read the token, so it is sent without one.

Explicit ``Host`` headers and explicit peers (``client=``) are left as the test wrote them, so
negative tests keep testing what they say. Studio keeps its test host: the helper adds
``testserver`` to Studio's ``DEFAULT_ALLOWED_HOSTS`` in the test process only (several Studio
tests name it, some through ``allowed_hosts={"testserver"}``).
"""

from __future__ import annotations

import ipaddress
from typing import Any
from urllib.parse import urlsplit

import httpx

SESSION_HEADER = "x-nexura-session"
_MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_STUDIO_DEV_PORTS = frozenset({5173})
_LOOPBACK_NAMES = frozenset({"localhost", "127.0.0.1", "::1"})
_installed = False


def app_session_token(app: Any) -> str | None:
    """The session token of a Nexura app, looking through simple ASGI wrappers."""
    seen = 0
    while app is not None and seen < 8:
        token = getattr(getattr(app, "state", None), "session_token", None)
        if isinstance(token, str) and token:
            return token
        app = getattr(app, "app", None)
        seen += 1
    return None


def _is_rest_service(app: Any) -> bool:
    """The REST service (it has a profile manager); Studio tests may name ``testserver``."""
    seen = 0
    while app is not None and seen < 8:
        if getattr(getattr(app, "state", None), "profile_manager", None) is not None:
            return True
        app = getattr(app, "app", None)
        seen += 1
    return False


def _is_placeholder_host(host: str) -> bool:
    host = host.strip("[]").lower()
    if not host or host in _LOOPBACK_NAMES or "." in host or ":" in host:
        return False
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return True
    return False


def _holds_token(request: httpx.Request) -> bool:
    origin = request.headers.get("origin")
    if origin is None:
        return True
    parts = urlsplit(origin)
    host = (parts.hostname or "").lower()
    if host == "testserver":
        return True
    if f"{parts.scheme}://{parts.netloc}".lower() == (
        f"{request.url.scheme}://{request.url.netloc.decode()}".lower()
    ):
        return True
    return host in _LOOPBACK_NAMES and parts.port in _STUDIO_DEV_PORTS


def prepare_request(app: Any, request: httpx.Request) -> None:
    """Point placeholder hosts at 127.0.0.1 and add the token the way Studio sends it."""
    token = app_session_token(app)
    if token is None:
        return
    # Decided against the URL the test wrote, before any host rewrite.
    inject = (
        request.method.upper() in _MUTATING
        and SESSION_HEADER not in request.headers
        and _holds_token(request)
    )
    if (
        _is_rest_service(app)
        and _is_placeholder_host(request.url.host)
        and request.headers.get("host") == request.url.netloc.decode()
    ):
        request.url = request.url.copy_with(host="127.0.0.1")
        request.headers["host"] = request.url.netloc.decode()
    if inject:
        request.headers[SESSION_HEADER] = token


def install() -> None:
    """Patch the in-process transports once per interpreter (idempotent)."""
    global _installed
    if _installed:
        return
    _installed = True

    from starlette import testclient

    transport_cls = testclient._TestClientTransport
    original_sync = transport_cls.handle_request

    def handle_request(self: Any, request: httpx.Request) -> httpx.Response:
        if app_session_token(self.app) is not None:
            client = getattr(self, "client", None)
            if client and client[0] == "testclient":
                self.client = ("127.0.0.1", client[1])
            prepare_request(self.app, request)
        return original_sync(self, request)

    transport_cls.handle_request = handle_request  # type: ignore[method-assign]

    original_async = httpx.ASGITransport.handle_async_request

    async def handle_async_request(self: Any, request: httpx.Request) -> httpx.Response:
        prepare_request(self.app, request)
        return await original_async(self, request)

    httpx.ASGITransport.handle_async_request = handle_async_request  # type: ignore[method-assign]

    from nexura_runtime.studio import security as studio_security

    studio_security.DEFAULT_ALLOWED_HOSTS.add("testserver")
