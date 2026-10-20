"""P28 step 7 OSS security review: fixes for findings R1-R5.

R1 (MEDIUM) per-session local token on every state-changing request, plus a JSON-only rule.
R2 (MEDIUM) programmatic ``create_api_app()`` refuses non-loopback peers without the opt-in.
R3 (LOW) the Host check accepts only loopback names and configured hosts.
R4 (LOW) a shared-profile principal never gets local-owner behaviour.
R5 (LOW) the single-user Docker image starts only with ``NEXURA_STUDIO_TOKEN``.

Requests that must arrive without the token go straight to the ASGI app (``_asgi``), past the
shared test-client helper, so these tests do not depend on how other tests reach the app.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import httpx
import pytest
from nexura_studio.cli import _build_app
from nexura_studio.server import create_app as create_studio_app
from starlette.requests import Request

from nexura_runtime.secrets import generate_master_key
from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.service.api.bind_guard import INSECURE_ENV_VALUE, INSECURE_ENV_VAR
from nexura_runtime.service.api.client import NexuraClient
from nexura_runtime.service.api.local_session import SESSION_HEADER, SESSION_TOKEN_ENV
from nexura_runtime.service.api.middleware import AuthFailure
from nexura_runtime.service.auth.errors import UnauthenticatedNetworkError
from nexura_runtime.service.auth.models import Principal
from nexura_runtime.service.extensions import Identity

ROOT = Path(__file__).resolve().parents[2]
LOCAL = ("127.0.0.1", 50123)
LAN_PEER = ("192.168.1.77", 51515)
OTHER_LOCAL_PAGE = "http://127.0.0.1:4444"
CRED = {"credential_id": "k1", "credential_type": "api_key", "secret": "sk-test-1234"}


async def _asgi(
    app: Any,
    method: str,
    path: str,
    *,
    headers: Iterable[tuple[str, str]] = (),
    body: bytes = b"",
    host: str = "127.0.0.1:8765",
    client: tuple[str, int] | None = LOCAL,
) -> tuple[int, Any, dict[str, str]]:
    """Call the ASGI app directly (no test-client helper): status, JSON body, headers."""
    path_only, _, query = path.partition("?")
    raw_headers = [(b"host", host.encode())]
    raw_headers += [(k.lower().encode(), v.encode()) for k, v in headers]
    if body:
        raw_headers.append((b"content-length", str(len(body)).encode()))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path_only,
        "raw_path": path_only.encode(),
        "query_string": query.encode(),
        "root_path": "",
        "headers": raw_headers,
        "client": client,
        "server": ("127.0.0.1", 8765),
    }
    inbox = [{"type": "http.request", "body": body, "more_body": False}]
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return inbox.pop(0) if inbox else {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    data = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    resp_headers = {k.decode().lower(): v.decode() for k, v in start.get("headers", [])}
    try:
        parsed: Any = json.loads(data) if data else None
    except ValueError:
        parsed = data.decode("utf-8", "replace")
    return start["status"], parsed, resp_headers


def _token(app: Any) -> list[tuple[str, str]]:
    return [(SESSION_HEADER, app.state.session_token)]


def _json(payload: Any) -> tuple[list[tuple[str, str]], bytes]:
    return [("content-type", "application/json")], json.dumps(payload).encode()


def _code(body: Any) -> str:
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, dict):
        return str(detail.get("code"))
    error = body.get("error") if isinstance(body, dict) else None
    return str(error.get("code")) if isinstance(error, dict) else ""


class _RawTransport(httpx.AsyncBaseTransport):
    """An httpx transport onto ``_asgi`` (the Python client exactly as on the wire)."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self.seen: list[tuple[str, str, str | None]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = request.read()
        self.seen.append((request.method, request.url.path, request.headers.get(SESSION_HEADER)))
        status, data, headers = await _asgi(
            self.app,
            request.method,
            request.url.raw_path.decode(),
            headers=[(k, v) for k, v in request.headers.items() if k.lower() != "host"],
            body=body,
        )
        return httpx.Response(status, json=data, headers={"x-test": "raw"})


@pytest.fixture
def api_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.delenv(SESSION_TOKEN_ENV, raising=False)
    monkeypatch.delenv(INSECURE_ENV_VAR, raising=False)
    return create_api_app(db_path=str(tmp_path / "svc.sqlite3"))


# --- R1: per-session token --------------------------------------------------------------------


async def test_r1_other_local_page_cannot_change_state(api_app: Any) -> None:
    """The review repro: CORS-simple requests from another loopback origin change nothing."""
    h, b = _json(CRED)
    status, body, _ = await _asgi(
        api_app, "POST", "/api/v1/credentials", headers=h + _token(api_app), body=b
    )
    assert status == 201, body
    _, exported, _ = await _asgi(api_app, "GET", "/api/v1/workflows/wf-order-support/export/python")
    _, before, _ = await _asgi(api_app, "GET", "/api/v1/workflows")

    page = [("origin", OTHER_LOCAL_PAGE), ("content-type", "text/plain")]
    revoked = await _asgi(api_app, "POST", "/api/v1/credentials/k1/revoke", headers=page)
    imported = await _asgi(
        api_app,
        "POST",
        "/api/v1/workflows/import/python",
        headers=page,
        body=exported["source"].encode(),
    )
    form = [("origin", OTHER_LOCAL_PAGE), ("content-type", "application/x-www-form-urlencoded")]
    session = await _asgi(api_app, "POST", "/api/v1/sessions", headers=form, body=b"a=1")

    for status, body, _ in (revoked, imported, session):
        assert status == 403 and _code(body) == "SESSION_TOKEN_REQUIRED", (status, body)
    _, cred, _ = await _asgi(api_app, "GET", "/api/v1/credentials/k1")
    _, after, _ = await _asgi(api_app, "GET", "/api/v1/workflows")
    assert cred["status"] == "active"
    assert after["total"] == before["total"]


async def test_r1_token_is_random_per_app_and_compared(api_app: Any, tmp_path: Path) -> None:
    token = api_app.state.session_token
    assert isinstance(token, str) and len(token) >= 43
    other = create_api_app(db_path=str(tmp_path / "other.sqlite3"))
    assert other.state.session_token != token

    h, b = _json(CRED)
    missing = await _asgi(api_app, "POST", "/api/v1/credentials", headers=h, body=b)
    wrong = await _asgi(api_app, "POST", "/api/v1/credentials", headers=h + _token(other), body=b)
    empty = await _asgi(
        api_app, "POST", "/api/v1/credentials", headers=h + [(SESSION_HEADER, "")], body=b
    )
    assert (missing[0], _code(missing[1])) == (403, "SESSION_TOKEN_REQUIRED")
    assert (wrong[0], _code(wrong[1])) == (403, "SESSION_TOKEN_INVALID")
    assert (empty[0], _code(empty[1])) == (403, "SESSION_TOKEN_INVALID")
    assert token not in json.dumps(wrong[1])
    for method, path in (("DELETE", "/api/v1/credentials/k1"), ("PUT", "/api/v1/workflows/x")):
        status, body, _ = await _asgi(api_app, method, path)
        assert (status, _code(body)) == (403, "SESSION_TOKEN_REQUIRED"), (method, path)
    ok = await _asgi(api_app, "POST", "/api/v1/credentials", headers=h + _token(api_app), body=b)
    assert ok[0] == 201, ok[1]
    # Reads need no token.
    assert (await _asgi(api_app, "GET", "/api/v1/credentials"))[0] == 200


def test_r1_explicit_token_from_env_or_argument(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(SESSION_TOKEN_ENV, "fixed-env-token")
    assert (
        create_api_app(db_path=str(tmp_path / "a.sqlite3")).state.session_token == "fixed-env-token"
    )
    assert (
        create_studio_app(db_path=tmp_path / "s.sqlite3").state.session_token == "fixed-env-token"
    )
    explicit = create_api_app(db_path=str(tmp_path / "b.sqlite3"), session_token="from-arg")
    assert explicit.state.session_token == "from-arg"
    monkeypatch.setenv(SESSION_TOKEN_ENV, "   ")
    assert create_api_app(db_path=str(tmp_path / "c.sqlite3")).state.session_token.strip()


async def test_r1_json_only_rule_for_state_changing_requests(api_app: Any) -> None:
    tok = _token(api_app)
    for ctype in ("text/plain", "application/x-www-form-urlencoded", "multipart/form-data; b=x"):
        status, body, _ = await _asgi(
            api_app, "POST", "/api/v1/sessions", headers=tok + [("content-type", ctype)], body=b"{}"
        )
        assert (status, _code(body)) == (415, "UNSUPPORTED_MEDIA_TYPE"), ctype
    _, exported, _ = await _asgi(api_app, "GET", "/api/v1/workflows/wf-order-support/export/python")
    source = exported["source"].encode()
    plain = tok + [("content-type", "text/plain")]
    # Raw Python source from a non-browser client (curl, scripts) stays supported...
    status, body, _ = await _asgi(
        api_app, "POST", "/api/v1/workflows/import/python", headers=plain, body=source
    )
    assert status == 201, body
    # ...but not from a browser page, even one that somehow had the token.
    for marker in (("origin", "http://127.0.0.1:8765"), ("sec-fetch-site", "same-origin")):
        status, body, _ = await _asgi(
            api_app,
            "POST",
            "/api/v1/workflows/import/python",
            headers=plain + [marker],
            body=source,
        )
        assert (status, _code(body)) == (415, "UNSUPPORTED_MEDIA_TYPE"), marker
    h, b = _json({"title": "t"})
    assert (await _asgi(api_app, "POST", "/api/v1/sessions", headers=h + tok, body=b))[0] in (
        200,
        201,
    )


async def test_r1_signed_webhook_delivery_needs_no_session_token(api_app: Any) -> None:
    status, body, _ = await _asgi(
        api_app,
        "POST",
        "/api/v1/triggers/webhooks/orders/deliver",
        headers=[("content-type", "application/json")],
        body=b"{}",
    )
    assert status == 401 and _code(body) == "MISSING_SIGNATURE_HEADERS", (status, body)


async def test_r1_token_endpoint_serves_same_origin_only(api_app: Any) -> None:
    path = "/api/v1/local-session"
    token = api_app.state.session_token
    for headers in (
        [],
        [("sec-fetch-site", "same-origin")],
        [("sec-fetch-site", "none")],
        [("origin", "http://127.0.0.1:8765")],
    ):
        status, body, resp_headers = await _asgi(api_app, "GET", path, headers=headers)
        assert status == 200 and body == {"token": token, "header": SESSION_HEADER}, headers
        assert resp_headers.get("cache-control") == "no-store"
        assert "access-control-allow-origin" not in resp_headers
    for headers in (
        [("sec-fetch-site", "cross-site")],
        [("sec-fetch-site", "same-site")],
        [("origin", OTHER_LOCAL_PAGE)],
        [("origin", "http://127.0.0.1:8765"), ("sec-fetch-site", "same-site")],
    ):
        status, body, _ = await _asgi(api_app, "GET", path, headers=headers)
        assert status == 403 and _code(body) == "SESSION_TOKEN_CROSS_SITE", headers
        assert token not in json.dumps(body)
    status, body, _ = await _asgi(api_app, "GET", path, headers=[("origin", "http://evil.example")])
    assert status == 403 and token not in json.dumps(body)


async def test_r1_python_client_reads_the_token_and_retries_once(api_app: Any) -> None:
    transport = _RawTransport(api_app)
    async with httpx.AsyncClient(transport=transport) as http:
        client = NexuraClient("http://127.0.0.1:8765", http_client=http)
        created = await client.credentials.create(
            credential_id="k2", credential_type="api_key", secret="sk-test-5678"
        )
        assert created["credential_id"] == "k2"
        assert client.session_token == api_app.state.session_token
        assert [s[:2] for s in transport.seen] == [
            ("POST", "/api/v1/credentials"),
            ("GET", "/api/v1/local-session"),
            ("POST", "/api/v1/credentials"),
        ]
        assert transport.seen[0][2] is None and transport.seen[2][2] == client.session_token

        manual = NexuraClient("http://127.0.0.1:8765", http_client=http, auto_session_token=False)
        with pytest.raises(Exception, match="SESSION_TOKEN_REQUIRED"):
            await manual.credentials.revoke("k2")


async def test_r1_builtin_studio_requires_the_token_and_gives_it_to_its_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(SESSION_TOKEN_ENV, raising=False)
    monkeypatch.delenv(INSECURE_ENV_VAR, raising=False)
    studio = create_studio_app(db_path=tmp_path / "studio.sqlite3")
    token = studio.state.session_token
    h, b = _json({"spec": {}})
    status, body, _ = await _asgi(studio, "POST", "/api/v1/workflows/validate", headers=h, body=b)
    assert (status, _code(body)) == (403, "SESSION_TOKEN_REQUIRED")
    status, body, _ = await _asgi(
        studio,
        "POST",
        "/api/v1/workflows/validate",
        headers=h + [("origin", OTHER_LOCAL_PAGE)],
        body=b,
    )
    assert (status, _code(body)) == (403, "SESSION_TOKEN_REQUIRED")
    status, _, _ = await _asgi(
        studio, "POST", "/api/v1/workflows/validate", headers=h + _token(studio), body=b
    )
    assert status != 403

    status, page, headers = await _asgi(studio, "GET", "/")
    assert status == 200 and f'<meta name="nexura-session" content="{token}">' in page
    assert "frame-ancestors 'none'" in headers["content-security-policy"]
    _, cross, _ = await _asgi(studio, "GET", "/", headers=[("sec-fetch-site", "cross-site")])
    assert token not in cross
    assert (await _asgi(studio, "GET", "/api/v1/local-session"))[1]["token"] == token
    status, body, _ = await _asgi(
        studio, "GET", "/api/v1/local-session", headers=[("sec-fetch-site", "same-site")]
    )
    assert status == 403 and token not in json.dumps(body)


async def test_r1_studio_api_token_is_never_handed_to_remote_browsers(tmp_path: Path) -> None:
    studio = create_studio_app(db_path=tmp_path / "studio.sqlite3", api_token="configured-token")
    assert studio.state.session_token == "configured-token"
    status, page, _ = await _asgi(studio, "GET", "/", client=("172.17.0.1", 40000))
    assert status == 200 and "configured-token" not in page
    status, _, _ = await _asgi(studio, "GET", "/api/v1/local-session", client=("172.17.0.1", 40000))
    assert status == 401
    bearer = [("authorization", "Bearer configured-token")]
    h, b = _json({"spec": {}})
    status, body, _ = await _asgi(
        studio,
        "POST",
        "/api/v1/workflows/validate",
        headers=h + bearer,
        body=b,
        client=("172.17.0.1", 40000),
    )
    assert (status, _code(body)) == (403, "SESSION_TOKEN_REQUIRED")
    status, _, _ = await _asgi(
        studio,
        "POST",
        "/api/v1/workflows/validate",
        headers=h + bearer + _token(studio),
        body=b,
        client=("172.17.0.1", 40000),
    )
    assert status not in (401, 403)


def test_r1_client_and_ui_send_the_token() -> None:
    ts = (ROOT / "clients" / "typescript" / "src" / "client.ts").read_text(encoding="utf-8")
    assert 'SESSION_HEADER = "X-Nexura-Session"' in ts and "withSessionHeader(method" in ts
    studio_js = (
        ROOT / "packages" / "nexura-studio" / "src" / "nexura_studio" / "static" / "app.js"
    ).read_text(encoding="utf-8")
    assert 'opts.headers["X-Nexura-Session"] = sessionToken' in studio_js
    web = (ROOT / "studio-web" / "src" / "api" / "client.ts").read_text(encoding="utf-8")
    assert "autoSessionToken: true" in web


# --- R2: programmatic use is loopback-only ----------------------------------------------------


async def test_r2_non_loopback_peer_is_refused_without_the_opt_in(api_app: Any) -> None:
    status, body, _ = await _asgi(api_app, "GET", "/api/v1/workflows", client=LAN_PEER)
    assert (status, _code(body)) == (403, "NON_LOOPBACK_CLIENT")
    h, b = _json(CRED)
    status, body, _ = await _asgi(
        api_app, "POST", "/api/v1/credentials", headers=h + _token(api_app), body=b, client=LAN_PEER
    )
    assert (status, _code(body)) == (403, "NON_LOOPBACK_CLIENT")
    # The repro: a LAN peer naming the LAN address as Host.
    status, _, _ = await _asgi(
        api_app, "GET", "/api/v1/workflows", host="192.168.1.5:8765", client=LAN_PEER
    )
    assert status == 400
    for peer in (("127.0.0.1", 1), ("::1", 1), ("127.8.9.10", 1), None):
        assert (await _asgi(api_app, "GET", "/api/v1/workflows", client=peer))[0] == 200, peer
    for peer in (("testclient", 50000), ("0.0.0.0", 1), ("::ffff:192.168.1.77", 1)):
        assert (await _asgi(api_app, "GET", "/api/v1/workflows", client=peer))[0] == 403, peer


async def test_r2_explicit_opt_in_allows_network_peers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(INSECURE_ENV_VAR, raising=False)
    opted = create_api_app(db_path=str(tmp_path / "o.sqlite3"), allow_unauthenticated_network=True)
    assert (await _asgi(opted, "GET", "/api/v1/workflows", client=LAN_PEER))[0] == 200
    # With the opt-in a LAN browser may use the machine's IP address, never a dotless name.
    lan = await _asgi(opted, "GET", "/api/v1/workflows", host="192.168.1.5:8765", client=LAN_PEER)
    assert lan[0] == 200
    named = await _asgi(opted, "GET", "/api/v1/workflows", host="nexura-box:8765", client=LAN_PEER)
    assert named[0] == 400
    plain = create_api_app(db_path=str(tmp_path / "p.sqlite3"))
    monkeypatch.setenv(INSECURE_ENV_VAR, "yes")  # only the exact value counts
    assert (await _asgi(plain, "GET", "/api/v1/workflows", client=LAN_PEER))[0] == 403
    monkeypatch.setenv(INSECURE_ENV_VAR, INSECURE_ENV_VALUE)
    assert (await _asgi(plain, "GET", "/api/v1/workflows", client=LAN_PEER))[0] == 200
    # The token still applies to network peers.
    status, body, _ = await _asgi(plain, "DELETE", "/api/v1/credentials/x", client=LAN_PEER)
    assert (status, _code(body)) == (403, "SESSION_TOKEN_REQUIRED")


async def test_r2_programmatic_studio_is_loopback_only_without_a_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(SESSION_TOKEN_ENV, raising=False)
    monkeypatch.delenv(INSECURE_ENV_VAR, raising=False)
    studio = create_studio_app(db_path=tmp_path / "s.sqlite3")
    status, body, _ = await _asgi(studio, "GET", "/api/v1/modules", client=LAN_PEER)
    assert (status, _code(body)) == (403, "NON_LOOPBACK_CLIENT")
    assert (await _asgi(studio, "GET", "/api/v1/modules"))[0] == 200


def test_r2_cli_bind_guard_is_kept(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(INSECURE_ENV_VAR, raising=False)
    with pytest.raises(UnauthenticatedNetworkError):
        create_api_app(db_path=str(tmp_path / "g.sqlite3"), listen_host="0.0.0.0")


# --- R3: Host allow-list -----------------------------------------------------------------------


async def test_r3_only_loopback_and_configured_hosts(api_app: Any, tmp_path: Path) -> None:
    for host in (
        "nexura-attacker:8765",
        "testserver",
        "intranet",
        "test",
        "192.168.1.5:8765",
        "10.0.0.1",
        "0.0.0.0:8765",
        "localhost.",
        "127.0.0.1.nip.io",
    ):
        status, body, _ = await _asgi(api_app, "GET", "/api/v1/workflows", host=host)
        assert (status, _code(body)) == (400, "INVALID_HOST"), host
    for host in ("localhost", "localhost:8765", "127.0.0.1", "127.0.0.1:9", "[::1]:8765", "[::1]"):
        assert (await _asgi(api_app, "GET", "/api/v1/workflows", host=host))[0] == 200, host
    status, body, _ = await _asgi(
        api_app, "GET", "/api/v1/workflows", headers=[("origin", "http://testserver")]
    )
    assert (status, _code(body)) == (403, "CROSS_ORIGIN_FORBIDDEN")
    configured = create_api_app(db_path=str(tmp_path / "h.sqlite3"), allowed_hosts=["intranet"])
    assert (await _asgi(configured, "GET", "/api/v1/workflows", host="intranet:8765"))[0] == 200
    assert (await _asgi(configured, "GET", "/api/v1/workflows", host="other"))[0] == 400


def test_r3_shipped_defaults_have_no_testserver() -> None:
    """Production defaults (the test helper adds ``testserver`` to Studio's set at run time)."""
    for rel in ("service/api/middleware.py", "studio/security.py"):
        source = (ROOT / "src" / "nexura_runtime" / rel).read_text(encoding="utf-8")
        tree = ast.parse(source)
        literals = [
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        ]
        assert "testserver" not in literals, rel
        assert '"." not in host' not in source, rel


# --- R4: shared-profile principals ----------------------------------------------------------


class _CarelessProvider:
    """Authenticates, but builds ``Principal`` without ``profile=`` (model default local-owner)."""

    name = "careless-token"
    is_authenticating = True

    def __init__(self, profile: str | None = None) -> None:
        self.profile = profile

    def authenticate(self, request: Request, target_workspace_id: str | None) -> Identity:
        if request.headers.get("authorization") != "Bearer alice-token":
            raise AuthFailure(401, "UNAUTHENTICATED", "Authentication required.")
        principal = (
            Principal(principal_id="alice", profile=self.profile)  # type: ignore[arg-type]
            if self.profile
            else Principal(principal_id="alice")
        )
        return Identity(principal, target_workspace_id or "default", "editor")

    def is_operator(self, request: Request) -> bool:
        return False


@pytest.mark.parametrize("claimed", [None, "local-owner"])
async def test_r4_shared_principal_never_gets_local_owner_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, claimed: str | None
) -> None:
    monkeypatch.setenv("NEXURA_MASTER_KEY", generate_master_key())
    source_app = create_api_app(db_path=str(tmp_path / "src.sqlite3"))
    _, exported, _ = await _asgi(
        source_app, "GET", "/api/v1/workflows/wf-order-support/export/python"
    )
    app = create_api_app(
        db_path=str(tmp_path / "svc.sqlite3"),
        profile="authenticated-shared-service",
        auth_provider=_CarelessProvider(claimed),
    )
    auth = [("authorization", "Bearer alice-token")]
    status, made, _ = await _asgi(
        app,
        "POST",
        "/api/v1/workflows/import/python",
        headers=auth + [("content-type", "text/plain")],
        body=exported["source"].encode(),
    )
    assert status == 201, made  # the shared profile authenticates: no session token, no 415
    outside = tmp_path / "server-home" / "chosen-by-remote-user"
    h, b = _json({"target_dir": str(outside)})
    status, body, _ = await _asgi(
        app, "POST", f"/api/v1/workflows/{made['workflow_id']}/export", headers=auth + h, body=b
    )
    assert not outside.exists(), body
    assert status in (400, 403, 500), body
    # The token endpoint is a local-profile feature.
    assert (await _asgi(app, "GET", "/api/v1/local-session", headers=auth))[0] == 404


# --- R5: Docker image ----------------------------------------------------------------------


def _docker_files() -> tuple[list[str], str]:
    dockerfile = (ROOT / "deployment" / "Dockerfile").read_text(encoding="utf-8")
    compose = (ROOT / "deployment" / "docker-compose.yml").read_text(encoding="utf-8")
    match = re.search(r"^CMD (\[.*\])$", dockerfile, re.M)
    assert match is not None
    return json.loads(match.group(1)), compose


def test_r5_docker_image_binds_all_interfaces_only_with_a_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cmd, compose = _docker_files()
    assert cmd[0] == "nexura-studio" and cmd[cmd.index("--host") + 1] == "0.0.0.0"
    assert "--insecure-allow-unauthenticated-network" not in cmd
    assert re.search(r'"127\.0\.0\.1:8765:8765"', compose)
    assert not re.search(r'^\s*-\s*"?(0\.0\.0\.0:)?\d+:\d+"?\s*$', compose, re.M)
    assert "NEXURA_STUDIO_TOKEN=${NEXURA_STUDIO_TOKEN:?" in compose
    assert "/healthz" in compose  # public even with a token (readyz needs it)
    monkeypatch.delenv(INSECURE_ENV_VAR, raising=False)
    ns = argparse.Namespace(
        host="0.0.0.0",
        db=str(tmp_path / "studio.sqlite3"),
        insecure_allow_unauthenticated_network=False,
    )
    monkeypatch.delenv(SESSION_TOKEN_ENV, raising=False)
    with pytest.raises(UnauthenticatedNetworkError):
        _build_app(ns, create_studio_app)
    monkeypatch.setenv(SESSION_TOKEN_ENV, "docker-run-token")
    app = _build_app(ns, create_studio_app)
    assert app.state.session_token == "docker-run-token"
    assert app.state.local_session.require_loopback_client is False
