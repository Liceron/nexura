# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
# P38 C4 adversarial review repros. Each test asserts the
# SECURE behaviour; all 11 failed before the review fixes. Offline: in-process apps and one tiny
# Python child; no server, no provider calls.
#
# Setup changes made when moving them, assertions unchanged:
# * Clients that act as a remote peer use an https base URL: since m6 a non-loopback peer over
#   plain HTTP is refused (403 PLAIN_HTTP_REFUSED) before any token is read, which would
#   otherwise mask what these tests check. Plain HTTP is covered by the m6 test below.
# * The /proc/<pid>/environ test cannot pass in any Python process (the initial environment
#   block is fixed at exec); the lead decision for m7 was to correct the docs. It stays as a
#   strict xfail recording the limitation, next to a test of the docs statement.
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from nexura_runtime.service.api import operator_token as ot
from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.service.api.operator_token import (
    OPERATOR_TOKEN_ENV,
    OperatorTokenAuthProvider,
    OperatorTokenError,
    read_token_file,
    validate_operator_token,
)

TOKEN = "Qx7-v2Lr_9mZpA4tKc8NfWj3bYsH6uDe1oRgTiVnXqE"  # test-only
WRONG = "Zz9-a1Bb_2Cc3Dd4Ee5Ff6Gg7Hh8Ii9Jj0KkLlMmNnO"
POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX")


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _app(tmp_path: Path, provider: OperatorTokenAuthProvider) -> Any:
    return create_api_app(db_path=str(tmp_path / "svc.sqlite3"), auth_provider=provider)


# --------------------------------------------------------------------------- MAJOR-1
def test_readyz_is_not_an_unlimited_token_oracle(tmp_path: Path) -> None:
    """/readyz answers differently for the right token, never counts a wrong one, and keeps
    answering a peer that the limiter has blocked: an unlimited, unlogged guessing oracle."""
    provider = OperatorTokenAuthProvider(TOKEN, max_failures=3, failure_window=60.0, clock=_Clock())
    with TestClient(_app(tmp_path, provider), client=("203.0.113.9", 1)) as attacker:
        # 100 wrong guesses on /readyz: none is counted against the peer.
        for i in range(100):
            attacker.get("/readyz", headers={"Authorization": f"Bearer {WRONG[:-3]}{i:03d}"})
        assert provider.limiter.retry_after("203.0.113.9") > 0, (
            "wrong tokens sent to /readyz are not counted by the failed-attempt limiter"
        )


def test_blocked_peer_cannot_confirm_a_token_via_readyz(tmp_path: Path) -> None:
    provider = OperatorTokenAuthProvider(TOKEN, max_failures=3, failure_window=60.0, clock=_Clock())
    with TestClient(
        _app(tmp_path, provider), base_url="https://testserver", client=("203.0.113.10", 1)
    ) as attacker:
        for _ in range(3):
            attacker.get("/api/v1/workflows", headers={"Authorization": f"Bearer {WRONG}"})
        assert attacker.get("/api/v1/workflows").status_code == 429  # blocked
        wrong = attacker.get("/readyz", headers={"Authorization": f"Bearer {WRONG}"}).json()
        right = attacker.get("/readyz", headers={"Authorization": f"Bearer {TOKEN}"}).json()
        assert wrong.keys() == right.keys(), (
            "a blocked peer still learns whether a guess is right from the /readyz body"
        )


# --------------------------------------------------------------------------- MINOR (file)
@POSIX_ONLY
def test_token_file_owned_by_another_user_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the mode is checked, not the owner. A 0600 file planted by another account (e.g. a
    token path in a shared or group-writable directory, service running as root) is accepted,
    and its author becomes the operator."""
    path = tmp_path / "operator.token"
    path.write_text(TOKEN, encoding="utf-8")
    path.chmod(0o600)
    real_uid = os.geteuid()
    monkeypatch.setattr(
        os, "geteuid", lambda: real_uid + 4242
    )  # "the service runs as someone else"
    with pytest.raises(OperatorTokenError):
        read_token_file(path)


@POSIX_ONLY
def test_token_file_is_checked_and_read_through_one_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """stat() and read_text() resolve the path twice. Whatever sits at the path after the
    check is read, even a world-readable file with another token."""
    path = tmp_path / "operator.token"
    path.write_text(TOKEN, encoding="utf-8")
    path.chmod(0o600)
    swap = tmp_path / "swap"
    swap.write_text(WRONG, encoding="utf-8")
    swap.chmod(0o644)

    real_stat = Path.stat

    def racing_stat(self: Path, *a: Any, **kw: Any) -> os.stat_result:
        result = real_stat(self, *a, **kw)
        if self == path and swap.exists():
            os.replace(swap, path)  # the race window between check and use
        return result

    monkeypatch.setattr(Path, "stat", racing_stat)
    try:
        value = read_token_file(path)
    except OperatorTokenError:
        return  # secure: the file that was checked is the file that was read
    assert value != WRONG, "a world-readable replacement was read after the 0600 check"


# --------------------------------------------------------------------------- MINOR (entropy)
@pytest.mark.parametrize(
    "weak",
    [
        "password1234password1234password1234password1234",
        "correcthorsebatterystaplecorrecthorsebatterystaple",
        "abcdefghijklmnopqrstuvwxyzabcdefghijklmnopqrstu",
        "0123456789012345678901234567890123456789012345",
    ],
)
def test_patterned_or_dictionary_tokens_are_refused(weak: str) -> None:
    with pytest.raises(OperatorTokenError):
        validate_operator_token(weak)


# --------------------------------------------------------------------------- MINOR (env)
@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="/proc")
@pytest.mark.xfail(
    strict=True,
    reason="m7: the initial environment block stays in /proc/<pid>/environ for the process "
    "lifetime; documented in docs/remote-single-operator.md, --token-file is preferred",
)
def test_env_token_is_gone_from_proc_environ_after_cli_pop() -> None:
    """serve_command does os.environ.pop(NEXURA_OPERATOR_TOKEN). Children are clean, but the
    initial environment block (/proc/<pid>/environ, `ps e`) still holds the token for the
    process lifetime, so docs' 'does not appear in process listings' overclaims."""
    code = (
        f"import os; os.environ.pop({OPERATOR_TOKEN_ENV!r}, None); "
        "print(b'SECRETMARK' in open('/proc/self/environ','rb').read())"
    )
    env = {**os.environ, OPERATOR_TOKEN_ENV: "SECRETMARK" + TOKEN}
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
    assert out.stdout.strip() == "False", "token still readable in /proc/<pid>/environ"


# --------------------------------------------------------------------------- MINOR (studio noise)
def test_studio_connect_sequence_records_no_failures(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Studio's pingService() calls capabilities (with the bearer) and then
    fetchSessionToken(), which never sends Authorization. In remote mode that second request is
    a counted failure and a WARNING 'operator token rejected ... (missing)' on every connect."""
    provider = OperatorTokenAuthProvider(TOKEN, clock=_Clock())
    with TestClient(
        _app(tmp_path, provider), base_url="https://testserver", client=("198.51.100.77", 1)
    ) as studio:
        assert (
            studio.get(
                "/api/v1/capabilities", headers={"Authorization": f"Bearer {TOKEN}"}
            ).status_code
            == 200
        )
        # clients/typescript/src/client.ts fetchSessionToken(): no Authorization header.
        studio.get("/api/v1/local-session", headers={"Accept": "application/json"})
    assert ot.FailedAttemptLimiter is not None
    assert "198.51.100.77" not in provider.limiter._failures, (
        "a signed-in Studio records a failed attempt on every connect"
    )


# --------------------------------------------------------------------------- MINOR (embedding)
def test_embedded_remote_mode_refuses_plain_http_from_network_peers(tmp_path: Path) -> None:
    """The plain-HTTP guard only looks at the declared listen_host. An embedder that calls
    create_api_app(auth_provider=OperatorTokenAuthProvider(t)) without listen_host and then
    uvicorn.run(app, host="0.0.0.0") serves the bearer over plain HTTP to the network: the
    loopback-client rule that used to answer 403 NON_LOOPBACK_CLIENT is off in this mode and
    nothing checks the scheme at request time."""
    provider = OperatorTokenAuthProvider(TOKEN, clock=_Clock())
    app = _app(tmp_path, provider)  # no listen_host, no allow_plain_http
    with TestClient(app, base_url="http://nexura.example", client=("203.0.113.7", 1)) as remote:
        response = remote.get("/api/v1/workflows", headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code != 200, (
        "a network peer used the operator token over plain HTTP without allow_plain_http"
    )


# =========================================================================== review fixes
# Tests added with the fixes (P38 C4 review: M1, M2, M3, m1, m2, m3, m5, m6, m7, m8).

ROOT = Path(__file__).resolve().parents[2]
HTTPS = "https://testserver"
DELIVER = "/api/v1/triggers/webhooks/{}/deliver"


def _mkdir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _signed(
    secret: str,
    body: dict[str, Any],
    *,
    event_id: str,
    ts: float | None = None,
    legacy: bool = False,
) -> tuple[dict[str, str], bytes]:
    import json
    import time

    from nexura_runtime.service.triggers.signatures import compute_webhook_signature

    stamp = time.time() if ts is None else ts
    sig = compute_webhook_signature(secret, stamp, body, None if legacy else event_id)
    headers = {
        "Content-Type": "application/json",
        "X-Webhook-Signature": sig,
        "X-Webhook-Timestamp": str(int(stamp)),
        "X-Webhook-Event-ID": event_id,
    }
    return headers, json.dumps(body).encode()


def _register(client: TestClient, endpoint_id: str) -> dict[str, Any]:
    r = client.post(
        "/api/v1/triggers/webhooks",
        json={"endpoint_id": endpoint_id, "workflow_id": "wf-order-support"},
        headers=_bearer(TOKEN),
    )
    assert r.status_code == 200, r.text
    body: dict[str, Any] = r.json()
    return body


def test_readyz_gives_no_details_in_remote_mode_even_with_the_token(tmp_path: Path) -> None:
    provider = OperatorTokenAuthProvider(TOKEN, clock=_Clock())
    with TestClient(_app(tmp_path, provider)) as client:
        for headers in ({}, _bearer(TOKEN), _bearer(WRONG)):
            assert set(client.get("/readyz", headers=headers).json()) == {"status"}
        # A probe sends no bearer: not counted. A wrong bearer is a guess: counted.
        client.get("/readyz")
        assert "127.0.0.1" in provider.limiter._failures
    source = (ROOT / "docs/remote-single-operator.md").read_text("utf-8")
    assert "`/readyz` returns only a status" in source


def test_generated_tokens_pass_and_refusals_say_how_to_make_one() -> None:
    import secrets

    for _ in range(200):
        validate_operator_token(secrets.token_urlsafe(32))
        validate_operator_token(secrets.token_hex(32))
    weak = "CorrectHorseBatteryStapleCorrectHorseBatteryStaple1"
    with pytest.raises(OperatorTokenError) as exc:
        validate_operator_token(weak)
    message = str(exc.value)
    assert 'python -c "import secrets;print(secrets.token_urlsafe(32))"' in message
    assert weak not in message


@POSIX_ONLY
def test_token_file_symlink_is_refused_and_bom_is_ignored(tmp_path: Path) -> None:
    real = tmp_path / "real.token"
    real.write_bytes(b"\xef\xbb\xbf" + TOKEN.encode() + b"\r\n")
    real.chmod(0o600)
    assert read_token_file(real) == TOKEN
    link = tmp_path / "link.token"
    link.symlink_to(real)
    with pytest.raises(OperatorTokenError, match="symbolic link"):
        read_token_file(link)


def test_authenticated_session_probe_gets_404_and_nothing_is_counted(tmp_path: Path) -> None:
    provider = OperatorTokenAuthProvider(TOKEN, clock=_Clock())
    with TestClient(_app(tmp_path, provider), base_url=HTTPS, client=("198.51.100.8", 1)) as c:
        r = c.get("/api/v1/local-session", headers=_bearer(TOKEN))
        assert r.status_code == 404
        assert r.json()["detail"]["code"] == "SESSION_TOKEN_NOT_USED"
        assert c.get("/api/v1/local-session").status_code == 401  # still refused
    assert "198.51.100.8" not in provider.limiter._failures


def test_plain_http_rules_at_request_time(tmp_path: Path) -> None:
    provider = OperatorTokenAuthProvider(TOKEN, clock=_Clock())
    app = _app(tmp_path, provider)
    with TestClient(app, base_url="http://nexura.example", client=("203.0.113.7", 1)) as c:
        refused = c.get("/api/v1/workflows", headers=_bearer(TOKEN))
        assert refused.status_code == 403
        assert refused.json()["detail"]["code"] == "PLAIN_HTTP_REFUSED"
        assert c.get("/healthz").status_code == 200  # probes carry no secret
    # Refused before the token is read: nothing counted, nothing learned.
    assert "203.0.113.7" not in provider.limiter._failures
    with TestClient(
        _app(_mkdir(tmp_path / "b"), provider)
    ) as local:  # loopback: a proxy on this host
        assert local.get("/api/v1/workflows", headers=_bearer(TOKEN)).status_code == 200
    with TestClient(
        _app(_mkdir(tmp_path / "c"), provider),
        base_url="https://nexura.example",
        client=("203.0.113.7", 1),
    ) as tls:
        assert tls.get("/api/v1/workflows", headers=_bearer(TOKEN)).status_code == 200
    opted = create_api_app(
        db_path=str(tmp_path / "opt.sqlite3"),
        auth_provider=OperatorTokenAuthProvider(TOKEN),
        listen_host="0.0.0.0",
        allow_plain_http=True,
    )
    with TestClient(opted, base_url="http://nexura.example", client=("203.0.113.7", 1)) as c:
        assert c.get("/api/v1/workflows", headers=_bearer(TOKEN)).status_code == 200


# ------------------------------------------------------------------ M3: delivery tokens


def test_delivery_token_is_issued_once_stored_hashed_and_scoped(tmp_path: Path) -> None:
    import sqlite3

    provider = OperatorTokenAuthProvider(TOKEN, clock=_Clock())
    app = _app(tmp_path, provider)
    with TestClient(app, base_url=HTTPS, client=("198.51.100.20", 1)) as c:
        reg = _register(c, "orders")
        delivery = reg["delivery_token"]
        validate_operator_token(delivery)  # 32 random bytes
        other = _register(c, "refunds")["delivery_token"]
        listed = c.get("/api/v1/triggers/webhooks", headers=_bearer(TOKEN)).text
        assert delivery not in listed and other not in listed

        headers, body = _signed(reg["secret"], {"n": 1}, event_id="evt-1")
        ok = c.post(
            DELIVER.format("orders"), content=body, headers={**headers, **_bearer(delivery)}
        )
        assert ok.status_code == 200, ok.text
        assert ok.json()["accepted"] is True

        # The operator token is not a delivery credential, a delivery token works only on its
        # own endpoint's delivery route and nowhere else.
        h2, b2 = _signed(reg["secret"], {"n": 2}, event_id="evt-2")
        for bearer, path in (
            (TOKEN, DELIVER.format("orders")),
            (other, DELIVER.format("orders")),
            (delivery, DELIVER.format("refunds")),
        ):
            r = c.post(path, content=b2, headers={**h2, **_bearer(bearer)})
            assert r.status_code == 401, (path, r.text)
        for method, path in (
            ("GET", "/api/v1/workflows"),
            ("GET", "/api/v1/triggers/webhooks"),
            ("POST", "/api/v1/triggers/webhooks/orders/test"),
            ("GET", DELIVER.format("orders")),
        ):
            r = c.request(method, path, json={}, headers=_bearer(delivery))
            assert r.status_code in (401, 405), (method, path, r.status_code)
            assert r.status_code != 200
    with sqlite3.connect(str(tmp_path / "svc.sqlite3")) as conn:
        rows = conn.execute("SELECT token_sha256 FROM nexura_webhook_delivery_tokens").fetchall()
    assert len(rows) == 2
    raw = (tmp_path / "svc.sqlite3").read_bytes()
    assert delivery.encode() not in raw and other.encode() not in raw


def test_every_delivery_failure_is_the_same_counted_401(tmp_path: Path) -> None:
    import time

    clock = _Clock()
    provider = OperatorTokenAuthProvider(TOKEN, max_failures=100, clock=clock)
    with TestClient(_app(tmp_path, provider), base_url=HTTPS, client=("198.51.100.30", 1)) as c:
        reg = _register(c, "orders")
        good = _bearer(reg["delivery_token"])
        secret = reg["secret"]
        cases: dict[str, Any] = {}
        h, b = _signed(secret, {"x": 1}, event_id="e1")
        cases["unknown endpoint"] = c.post(DELIVER.format("nope"), content=b, headers={**h, **good})
        cases["no token"] = c.post(DELIVER.format("orders"), content=b, headers=h)
        cases["wrong token"] = c.post(
            DELIVER.format("orders"), content=b, headers={**h, **_bearer(WRONG)}
        )
        bad = {**h, "X-Webhook-Signature": "0" * 64}
        cases["bad signature"] = c.post(
            DELIVER.format("orders"), content=b, headers={**bad, **good}
        )
        old_h, old_b = _signed(secret, {"x": 1}, event_id="e2", ts=time.time() - 3600)
        cases["expired"] = c.post(
            DELIVER.format("orders"), content=old_b, headers={**old_h, **good}
        )
        cases["missing headers"] = c.post(
            DELIVER.format("orders"),
            content=b,
            headers={"Content-Type": "application/json", **good},
        )
        bad_ts = {**h, "X-Webhook-Timestamp": "nan"}
        cases["bad timestamp"] = c.post(
            DELIVER.format("orders"), content=b, headers={**bad_ts, **good}
        )
        bad_id = {**h, "X-Webhook-Event-ID": "a.b"}
        cases["bad event id"] = c.post(
            DELIVER.format("orders"), content=b, headers={**bad_id, **good}
        )
        cases["bad body"] = c.post(DELIVER.format("orders"), content=b"[1]", headers={**h, **good})
        bodies = {name: (r.status_code, r.json()) for name, r in cases.items()}
        first = next(iter(bodies.values()))
        assert first[0] == 401 and first[1]["detail"]["code"] == "UNAUTHENTICATED"
        assert all(v == first for v in bodies.values()), bodies
        assert all(r.headers["www-authenticate"].startswith("Bearer") for r in cases.values())
        assert all("TRIGGER_ERROR" not in r.text and "nope" not in r.text for r in cases.values())
    assert len(provider.limiter._failures["198.51.100.30"]) == len(cases)


def test_legacy_v1_signatures_are_refused_in_remote_mode(tmp_path: Path) -> None:
    provider = OperatorTokenAuthProvider(TOKEN, clock=_Clock())
    app = _app(tmp_path, provider)
    with TestClient(app, base_url=HTTPS, client=("198.51.100.40", 1)) as c:
        # An older endpoint that still allows v1 (only possible through the Python API).
        app.state.trigger_manager.register_webhook(
            endpoint_id="legacy",
            workspace_id="default",
            workflow_id="wf-order-support",
            release_id="current",
            secret="legacy-secret-" + "x" * 40,
            allow_legacy_signature=True,
        )
        assert app.state.delivery_tokens is not None
        delivery = app.state.delivery_tokens.issue("legacy")
        h, b = _signed("legacy-secret-" + "x" * 40, {"v": 1}, event_id="v1-evt", legacy=True)
        r = c.post(DELIVER.format("legacy"), content=b, headers={**h, **_bearer(delivery)})
        assert r.status_code == 401
        h2, b2 = _signed("legacy-secret-" + "x" * 40, {"v": 2}, event_id="v2-evt")
        assert (
            c.post(
                DELIVER.format("legacy"), content=b2, headers={**h2, **_bearer(delivery)}
            ).status_code
            == 200
        )


def test_delivery_token_rotation_and_deletion(tmp_path: Path) -> None:
    provider = OperatorTokenAuthProvider(TOKEN, clock=_Clock())
    app = _app(tmp_path, provider)
    with TestClient(app, base_url=HTTPS, client=("198.51.100.50", 1)) as c:
        reg = _register(c, "orders")
        old = reg["delivery_token"]
        rotated = c.post(
            "/api/v1/triggers/webhooks/orders/delivery-credential", headers=_bearer(TOKEN)
        )
        assert rotated.status_code == 200
        new = rotated.json()["delivery_token"]
        assert new != old
        h, b = _signed(reg["secret"], {"r": 1}, event_id="rot-1")
        assert (
            c.post(DELIVER.format("orders"), content=b, headers={**h, **_bearer(old)}).status_code
            == 401
        )
        assert (
            c.post(DELIVER.format("orders"), content=b, headers={**h, **_bearer(new)}).status_code
            == 200
        )
        # Rotation needs the operator token, not a delivery token.
        assert (
            c.post(
                "/api/v1/triggers/webhooks/orders/delivery-credential", headers=_bearer(new)
            ).status_code
            == 401
        )
        assert (
            c.post(
                "/api/v1/triggers/webhooks/missing/delivery-credential", headers=_bearer(TOKEN)
            ).status_code
            == 404
        )
        assert (
            c.delete("/api/v1/triggers/webhooks/orders", headers=_bearer(TOKEN)).status_code == 200
        )
        assert app.state.delivery_tokens is not None
        assert not app.state.delivery_tokens.has_token("orders")


def test_local_mode_webhooks_are_unchanged(tmp_path: Path) -> None:
    app = create_api_app(db_path=str(tmp_path / "local.sqlite3"))
    with TestClient(app) as c:
        reg = c.post(
            "/api/v1/triggers/webhooks",
            json={"endpoint_id": "orders", "workflow_id": "wf-order-support"},
        ).json()
        assert "delivery_token" not in reg
        rot = c.post("/api/v1/triggers/webhooks/orders/delivery-credential")
        assert rot.status_code == 404
        assert rot.json()["detail"]["code"] == "DELIVERY_TOKENS_NOT_USED"
        h, b = _signed(reg["secret"], {"x": 1}, event_id="loc-1")
        unknown = c.post(DELIVER.format("nope"), content=b, headers=h)
        assert unknown.status_code == 404  # local behaviour kept (no token, loopback only)
        assert c.post(DELIVER.format("orders"), content=b, headers=h).status_code == 200


# ------------------------------------------------------------------ docs (M2, m5, m7, m8, M3)


def _guide() -> str:
    return (ROOT / "docs/remote-single-operator.md").read_text("utf-8")


def _block(text: str, lang: str, contains: str) -> str:
    import re

    blocks = re.findall(rf"```{lang}\n(.*?)```", text, flags=re.DOTALL)
    match = [b for b in blocks if contains in b]
    assert match, (lang, contains)
    return match[0]


def test_proxy_examples_send_the_studio_csp_and_security_headers() -> None:
    from nexura_studio.server import STUDIO_CSP

    guide = _guide()
    caddy = _block(guide, "text", "reverse_proxy")
    nginx = _block(guide, "nginx", "proxy_pass")
    for config in (caddy, nginx):
        assert STUDIO_CSP in config
        assert "connect-src 'self'" in config and "frame-ancestors 'none'" in config
        for header in ("X-Content-Type-Options", "Referrer-Policy", "X-Frame-Options"):
            assert header in config, header
    # nginx: add_header in a location drops the server-level ones; keep them in server.
    tls_server = nginx.split("listen 443", 1)[1]
    before_locations = tls_server.split("location", 1)[0]
    assert "Content-Security-Policy" in before_locations
    assert all("add_header" not in loc for loc in tls_server.split("location")[1:])


def test_nginx_example_and_docs_statements() -> None:
    guide = _guide()
    nginx = _block(guide, "nginx", "proxy_pass")
    assert "proxy_set_header Host $http_host;" in nginx
    assert "proxy_http_version 1.1;" in nginx
    assert 'proxy_set_header Connection "";' in nginx
    assert "proxy_buffering off;" in nginx
    assert "proxy_set_header X-Forwarded-Proto $scheme;" in nginx
    flat = " ".join(guide.split())
    # m8: the token is the service user's authority on the host.
    assert "full authority of the operating-system user that runs the service" in flat
    # m5: the right token always passes; the lockout only blocks wrong or missing tokens.
    assert "The right token always gets through" in flat
    assert "up to a minute" not in flat
    # m7: the environment variable stays readable by the process owner and root.
    assert "/proc/<pid>/environ" in flat and "Prefer the token file" in flat
    # M3: per-endpoint delivery tokens; third-party senders cannot deliver directly.
    for needle in ("delivery token", "/delivery-credential", "Stripe", "GitHub", "v1 signatures"):
        assert needle in flat, needle
    # m1: the server cannot measure entropy.
    assert "cannot measure" in flat
