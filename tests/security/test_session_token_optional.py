"""The optional session token (NEXURA_REQUIRE_SESSION_TOKEN=off, --no-session-token), local ASGI.

The clients here act like curl: the test suite's helper adds the token to requests sent straight
to an app, so they go through a plain ASGI wrapper the helper does not recognise.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from nexura_runtime.cli.main import build_parser
from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.service.api.bind_guard import INSECURE_ENV_VALUE, INSECURE_ENV_VAR
from nexura_runtime.service.api.local_session import (
    SESSION_TOKEN_REQUIRED_ENV,
    session_token_required,
)

LOCAL = ("127.0.0.1", 5000)


def _client(app, client=LOCAL) -> httpx.AsyncClient:
    async def plain(scope, receive, send):  # type: ignore[no-untyped-def]
        await app(scope, receive, send)

    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=plain, client=client), base_url="http://127.0.0.1"
    )


@pytest.fixture(autouse=True)
def _no_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(SESSION_TOKEN_REQUIRED_ENV, raising=False)
    monkeypatch.delenv(INSECURE_ENV_VAR, raising=False)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("", True), ("on", True), ("1", True), ("TRUE", True), ("off", False), (" no ", False)],
)
def test_env_switch(monkeypatch: pytest.MonkeyPatch, value: str, expected: bool) -> None:
    monkeypatch.setenv(SESSION_TOKEN_REQUIRED_ENV, value)
    assert session_token_required() is expected


def test_env_switch_rejects_unknown_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SESSION_TOKEN_REQUIRED_ENV, "disabled")
    with pytest.raises(ValueError, match="'on' or 'off'"):
        session_token_required()


def test_explicit_argument_wins_over_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SESSION_TOKEN_REQUIRED_ENV, "off")
    assert session_token_required(True) is True


@pytest.mark.asyncio
async def test_token_is_required_by_default(tmp_path: Path) -> None:
    app = create_api_app(db_path=tmp_path / "local.sqlite3")
    assert app.state.local_session.require_token is True
    async with app.router.lifespan_context(app), _client(app) as client:
        response = await client.post("/api/v1/workflows", json={})
        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "SESSION_TOKEN_REQUIRED"


@pytest.mark.asyncio
@pytest.mark.parametrize("via_env", [False, True])
async def test_token_off_accepts_plain_requests_from_this_machine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, via_env: bool
) -> None:
    if via_env:
        monkeypatch.setenv(SESSION_TOKEN_REQUIRED_ENV, "off")
        app = create_api_app(db_path=tmp_path / "local.sqlite3")
    else:
        app = create_api_app(db_path=tmp_path / "local.sqlite3", require_session_token=False)
    assert app.state.local_session.require_token is False
    async with app.router.lifespan_context(app), _client(app) as client:
        # Passes the session check and reaches the route's own body validation.
        response = await client.post("/api/v1/workflows", json={})
        assert response.status_code == 422, response.text
        assert "SESSION_TOKEN" not in response.text
        # A stale token from before a restart is ignored, not refused.
        stale = await client.post(
            "/api/v1/workflows", json={}, headers={"X-Nexura-Session": "stale"}
        )
        assert stale.status_code == 422, stale.text
        # Studio's token endpoint keeps working, so existing clients are unaffected.
        assert (await client.get("/api/v1/local-session")).status_code == 200


@pytest.mark.asyncio
async def test_token_off_keeps_the_other_local_rules(tmp_path: Path) -> None:
    app = create_api_app(db_path=tmp_path / "local.sqlite3", require_session_token=False)
    async with app.router.lifespan_context(app):
        async with _client(app) as client:
            # JSON only: a body type a cross-site form could send is still refused.
            form = await client.post(
                "/api/v1/workflows",
                content=b"a=1",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            assert form.status_code == 415
            assert form.json()["detail"]["code"] == "UNSUPPORTED_MEDIA_TYPE"
            # Host rule (DNS rebinding): a foreign Host is still refused.
            rebound = await client.post("/api/v1/workflows", json={}, headers={"Host": "evil.test"})
            assert rebound.status_code == 400
            # Another site's page cannot read the token either.
            cross = await client.get(
                "/api/v1/local-session", headers={"Sec-Fetch-Site": "cross-site"}
            )
            assert cross.status_code == 403
        # Loopback only: another machine is still refused.
        async with _client(app, client=("192.0.2.1", 5000)) as remote:
            refused = await remote.post("/api/v1/workflows", json={})
            assert refused.status_code == 403
            assert refused.json()["detail"]["code"] == "NON_LOOPBACK_CLIENT"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        # A page on another local port (no-cors fetch): Origin names the other port.
        {"Origin": "http://localhost:3000"},
        {"Origin": "http://127.0.0.1:3000", "Sec-Fetch-Site": "cross-site"},
        # Same site, other port, as modern browsers mark it.
        {"Origin": "http://127.0.0.1:3000", "Sec-Fetch-Site": "same-site"},
        {"Origin": "https://evil.example"},
    ],
)
async def test_token_off_refuses_other_pages(tmp_path: Path, headers: dict[str, str]) -> None:
    # Review H1: with the token off, the Origin/Sec-Fetch-Site check is what keeps pages on other
    # sites and other local ports from changing state, including bodyless and no-Content-Type
    # requests that need no CORS preflight.
    app = create_api_app(db_path=tmp_path / "local.sqlite3", require_session_token=False)
    async with app.router.lifespan_context(app), _client(app) as client:
        source = b"from nexura_runtime import *\n"
        imported = await client.post(
            "/api/v1/workflows/import/python", content=source, headers=headers
        )
        # A non-loopback origin is already refused by the Host/Origin middleware.
        codes = {"CROSS_ORIGIN_REQUEST", "CROSS_ORIGIN_FORBIDDEN"}
        assert imported.status_code == 403, imported.text
        assert imported.json()["detail"]["code"] in codes
        bodyless = await client.post("/api/v1/runs/run-x/cancel", headers=headers)
        assert bodyless.status_code == 403
        assert bodyless.json()["detail"]["code"] in codes
        if "evil" not in headers["Origin"]:
            # Reads stay open to local pages; a foreign site is refused even for reads.
            assert (await client.get("/api/v1/workflows", headers=headers)).status_code == 200


@pytest.mark.asyncio
async def test_token_off_accepts_the_servers_own_pages(tmp_path: Path) -> None:
    app = create_api_app(db_path=tmp_path / "local.sqlite3", require_session_token=False)
    async with app.router.lifespan_context(app), _client(app) as client:
        for headers in (
            {"Origin": "http://127.0.0.1", "Sec-Fetch-Site": "same-origin"},
            {"Origin": "http://127.0.0.1"},
            {"Sec-Fetch-Site": "none"},
        ):
            response = await client.post("/api/v1/workflows", json={}, headers=headers)
            assert response.status_code == 422, (headers, response.text)


@pytest.mark.asyncio
async def test_token_off_refuses_a_body_without_content_type(tmp_path: Path) -> None:
    app = create_api_app(db_path=tmp_path / "local.sqlite3", require_session_token=False)
    async with app.router.lifespan_context(app), _client(app) as client:
        response = await client.post("/api/v1/workflows", content=b'{"a": 1}')
        assert response.status_code == 415
        assert response.json()["detail"]["code"] == "UNSUPPORTED_MEDIA_TYPE"


@pytest.mark.asyncio
async def test_network_opt_in_set_after_start_does_not_open_a_token_off_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_api_app(db_path=tmp_path / "local.sqlite3", require_session_token=False)
    monkeypatch.setenv(INSECURE_ENV_VAR, INSECURE_ENV_VALUE)
    async with app.router.lifespan_context(app), _client(app, client=("192.0.2.1", 5000)) as c:
        refused = await c.post("/api/v1/workflows", json={})
        assert refused.status_code == 403
        assert refused.json()["detail"]["code"] == "NON_LOOPBACK_CLIENT"


def test_bad_env_value_is_not_echoed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SESSION_TOKEN_REQUIRED_ENV, "s3cret-token-pasted-here")
    with pytest.raises(ValueError) as caught:
        session_token_required()
    assert "s3cret" not in str(caught.value)


def test_token_off_is_refused_with_the_network_opt_in(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="this machine only"):
        create_api_app(
            db_path=tmp_path / "local.sqlite3",
            require_session_token=False,
            allow_unauthenticated_network=True,
        )


def test_token_off_is_refused_with_the_network_opt_in_from_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(SESSION_TOKEN_REQUIRED_ENV, "off")
    monkeypatch.setenv(INSECURE_ENV_VAR, INSECURE_ENV_VALUE)
    with pytest.raises(ValueError, match="this machine only"):
        create_api_app(db_path=tmp_path / "local.sqlite3")


def test_serve_flag_is_parsed() -> None:
    args = build_parser().parse_args(["serve", "--no-session-token"])
    assert args.no_session_token is True
    assert build_parser().parse_args(["serve"]).no_session_token is False


@pytest.mark.parametrize(
    "extra",
    [["--auth", "token"], ["--insecure-allow-unauthenticated-network", "--host", "0.0.0.0"]],
)
def test_serve_refuses_invalid_combinations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    extra: list[str],
) -> None:
    import uvicorn

    from nexura_runtime.cli.main import main

    monkeypatch.setenv("NEXURA_OPERATOR_TOKEN", "x" * 40)
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: pytest.fail("server must not start"))
    code = main(["serve", "--db", str(tmp_path / "s.sqlite3"), "--no-session-token", *extra])
    assert code == 2
    assert "Error:" in capsys.readouterr().err


def test_serve_refuses_the_env_switch_with_operator_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import uvicorn

    from nexura_runtime.cli.main import main

    monkeypatch.setenv(SESSION_TOKEN_REQUIRED_ENV, "off")
    monkeypatch.setenv("NEXURA_OPERATOR_TOKEN", "x" * 40)
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: pytest.fail("server must not start"))
    assert main(["serve", "--db", str(tmp_path / "s.sqlite3"), "--auth", "token"]) == 2
    assert "--auth token" in capsys.readouterr().err


def test_serve_refuses_a_bad_env_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import uvicorn

    from nexura_runtime.cli.main import main

    monkeypatch.setenv(SESSION_TOKEN_REQUIRED_ENV, "maybe")
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: pytest.fail("server must not start"))
    assert main(["serve", "--db", str(tmp_path / "s.sqlite3")]) == 2
    assert SESSION_TOKEN_REQUIRED_ENV in capsys.readouterr().err


def test_studio_server_honours_the_switch(tmp_path: Path) -> None:
    pytest.importorskip("nexura_studio")
    from nexura_studio.server import create_app

    app = create_app(db_path=tmp_path / "studio.sqlite3", require_session_token=False)
    assert app.state.local_session.require_token is False
    assert create_app(db_path=tmp_path / "s2.sqlite3").state.local_session.require_token
    with pytest.raises(ValueError, match="this machine only"):
        create_app(
            db_path=tmp_path / "s3.sqlite3",
            require_session_token=False,
            allow_unauthenticated_network=True,
        )


@pytest.mark.asyncio
async def test_studio_over_http_with_the_token_off(tmp_path: Path) -> None:
    pytest.importorskip("nexura_studio")
    from nexura_studio.server import create_app

    app = create_app(db_path=tmp_path / "studio.sqlite3", require_session_token=False)
    async with _client(app) as client:
        plain = await client.post("/api/v1/workflows/validate", json={})
        assert plain.status_code != 403, plain.text
        foreign = await client.post(
            "/api/v1/workflows/validate", json={}, headers={"Origin": "http://localhost:3000"}
        )
        assert foreign.status_code == 403
        assert foreign.json()["error"]["code"] == "CROSS_ORIGIN_REQUEST"


def test_studio_cli_flag_and_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    pytest.importorskip("nexura_studio")
    import uvicorn
    from nexura_studio.cli import _run

    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
    db = str(tmp_path / "studio.sqlite3")
    assert _run(["--db", db, "--no-session-token"]) == 0
    assert "token check off" in capsys.readouterr().out
    monkeypatch.setenv(SESSION_TOKEN_REQUIRED_ENV, "off")
    monkeypatch.setenv(INSECURE_ENV_VAR, INSECURE_ENV_VALUE)
    assert _run(["--db", db]) == 2
    assert "this machine only" in capsys.readouterr().err
