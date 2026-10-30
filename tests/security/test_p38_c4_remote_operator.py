# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""P38 C4: remote single-operator mode (one operator token on every request).

Offline only: in-process apps, the CLI with ``uvicorn.run`` replaced, and one real ``serve``
subprocess on 127.0.0.1 called over HTTP with httpx. No provider or paid calls.
"""

from __future__ import annotations

import hashlib
import inspect
import logging
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from nexura_runtime.service.api import operator_token as ot
from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.service.api.bind_guard import INSECURE_ENV_VAR
from nexura_runtime.service.api.operator_token import (
    OPERATOR_TOKEN_ENV,
    OPERATOR_TOKEN_FILE_ENV,
    FailedAttemptLimiter,
    OperatorTokenAuthProvider,
    OperatorTokenError,
    PlainHttpNetworkError,
    generate_operator_token,
    load_operator_token,
    read_token_file,
    validate_operator_token,
)
from nexura_runtime.service.auth.errors import UnauthenticatedNetworkError

ROOT = Path(__file__).resolve().parents[2]
TOKEN = "Qx7-v2Lr_9mZpA4tKc8NfWj3bYsH6uDe1oRgTiVnXqE"  # 43 chars, test-only, not a secret
WRONG = "Zz9-a1Bb_2Cc3Dd4Ee5Ff6Gg7Hh8Ii9Jj0KkLlMmNnO"
POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits; Windows ACLs")


def _bearer(token: str = TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (OPERATOR_TOKEN_ENV, OPERATOR_TOKEN_FILE_ENV, INSECURE_ENV_VAR):
        monkeypatch.delenv(name, raising=False)


def _app(tmp_path: Path, provider: OperatorTokenAuthProvider | None = None, **kw: Any) -> Any:
    return create_api_app(
        db_path=str(tmp_path / "svc.sqlite3"),
        auth_provider=provider or OperatorTokenAuthProvider(TOKEN),
        **kw,
    )


# ---------------------------------------------------------------- token sources and validation


def test_generated_token_is_valid_and_weak_tokens_are_refused() -> None:
    assert validate_operator_token(generate_operator_token())
    for weak in ("short", "a" * 64, "ab" * 40, TOKEN[:42], TOKEN[:20] + " " + TOKEN[20:]):
        with pytest.raises(OperatorTokenError) as exc:
            validate_operator_token(weak)
        assert weak not in str(exc.value) or len(weak) < 6


def test_token_from_environment_or_file_exactly_one(tmp_path: Path) -> None:
    assert load_operator_token(environ={OPERATOR_TOKEN_ENV: TOKEN}) == TOKEN
    path = tmp_path / "token"
    path.write_text(TOKEN + "\n", encoding="utf-8")
    path.chmod(0o600)
    assert load_operator_token(token_file=path, environ={}) == TOKEN
    assert load_operator_token(environ={OPERATOR_TOKEN_FILE_ENV: str(path)}) == TOKEN
    with pytest.raises(OperatorTokenError, match="one way only") as both:
        load_operator_token(token_file=path, environ={OPERATOR_TOKEN_ENV: WRONG})
    assert WRONG not in str(both.value) and TOKEN not in str(both.value)
    with pytest.raises(OperatorTokenError, match="needs an operator token"):
        load_operator_token(environ={})
    with pytest.raises(OperatorTokenError, match="needs an operator token"):
        load_operator_token(environ={OPERATOR_TOKEN_ENV: "   "})


@POSIX_ONLY
def test_token_file_readable_by_others_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "token"
    path.write_text(TOKEN, encoding="utf-8")
    for mode in (0o644, 0o640, 0o604, 0o660):
        path.chmod(mode)
        with pytest.raises(OperatorTokenError, match="chmod 600") as exc:
            read_token_file(path)
        assert TOKEN not in str(exc.value)
    path.chmod(0o400)
    assert read_token_file(path) == TOKEN
    with pytest.raises(OperatorTokenError, match="not a regular file"):
        read_token_file(tmp_path)
    with pytest.raises(OperatorTokenError, match="does not exist"):
        read_token_file(tmp_path / "missing")


# ------------------------------------------------------------------ hashing, constant time


def test_provider_keeps_only_the_hash_and_compares_digests_in_constant_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OperatorTokenAuthProvider(TOKEN)
    digest = hashlib.sha256(TOKEN.encode()).digest()
    assert provider._digest == digest
    values = [v for v in vars(provider).values() if isinstance(v, (str, bytes))]
    assert TOKEN not in values and TOKEN.encode() not in values
    assert TOKEN not in repr(provider) and digest.hex() not in repr(provider)

    # Code inspection: the comparison is hmac.compare_digest over SHA-256 digests.
    source = inspect.getsource(OperatorTokenAuthProvider.token_matches)
    assert "hmac.compare_digest(self._hash(supplied), self._digest)" in source
    assert "hashlib.sha256" in inspect.getsource(OperatorTokenAuthProvider._hash)

    seen: list[tuple[bytes, bytes]] = []
    real = ot.hmac.compare_digest

    def spy(a: bytes, b: bytes) -> bool:
        seen.append((a, b))
        return real(a, b)

    monkeypatch.setattr(ot.hmac, "compare_digest", spy)
    assert provider.token_matches(TOKEN) is True
    assert provider.token_matches(WRONG) is False
    assert provider.token_matches(None) is False and provider.token_matches("") is False
    assert provider.token_matches("x" * 5000) is False  # refused before hashing
    assert seen == [
        (digest, digest),
        (hashlib.sha256(WRONG.encode()).digest(), digest),
    ]


# ------------------------------------------------------------------------------ API behaviour


def test_missing_wrong_and_right_tokens(tmp_path: Path) -> None:
    with TestClient(_app(tmp_path)) as client:
        missing = client.get("/api/v1/workflows")
        wrong = client.get("/api/v1/workflows", headers=_bearer(WRONG))
        basic = client.get("/api/v1/workflows", headers={"Authorization": f"Basic {TOKEN}"})
        empty = client.get("/api/v1/workflows", headers={"Authorization": "Bearer "})
        for refused in (missing, wrong, basic, empty):
            assert refused.status_code == 401
            assert refused.headers["www-authenticate"].startswith("Bearer")
        # No hint whether a token was sent, close, or configured at all.
        assert missing.json() == wrong.json() == basic.json() == empty.json()
        assert missing.json()["detail"]["code"] == "UNAUTHENTICATED"
        assert TOKEN not in missing.text and WRONG not in wrong.text

        ok = client.get("/api/v1/workflows", headers=_bearer())
        assert ok.status_code == 200
        assert any(w["workflow_id"] == "wf-order-support" for w in ok.json()["items"])
        # State-changing request: the bearer is enough, the local session token is not used.
        started = client.post(
            "/api/v1/runs",
            json={
                "workflow_id": "wf-order-support",
                "initial_state": {"order_id": "ORD-DELHI-501"},
            },
            headers={**_bearer(), "X-Nexura-Session": "not-the-session-token"},
        )
        assert started.status_code in (200, 201), started.text
        assert started.json()["status"] == "pending_approval"
        refused_post = client.post(
            "/api/v1/runs", json={"workflow_id": "wf-order-support"}, headers=_bearer(WRONG)
        )
        assert refused_post.status_code == 401
        # The local-session endpoint is not used in remote mode.
        assert client.get("/api/v1/local-session", headers=_bearer()).status_code == 404


def test_health_probes_are_open_and_everything_else_is_protected(tmp_path: Path) -> None:
    with TestClient(_app(tmp_path)) as client:
        assert client.get("/healthz").status_code == 200
        ready = client.get("/readyz")
        assert ready.status_code in (200, 503) and set(ready.json()) == {"status"}
        # Review M1: no probe details on the public probe in remote mode, even with the token.
        detailed = client.get("/readyz", headers=_bearer())
        assert set(detailed.json()) == {"status"}
        for path in ("/docs", "/openapi.json", "/redoc", "/api/v1/capabilities", "/nope"):
            assert client.get(path).status_code == 401, path
        assert client.get("/openapi.json", headers=_bearer()).status_code == 200
        caps = client.get("/api/v1/capabilities", headers=_bearer()).json()
        assert caps["active_profile"] == "local-owner"
        assert caps["supported_profiles"] == ["local-owner"]
        # Webhook deliveries are API routes too: no token, no delivery.
        assert client.post("/api/v1/triggers/webhooks/x/deliver", json={}).status_code == 401


def test_remote_peers_and_same_origin_studio(tmp_path: Path) -> None:
    app = _app(tmp_path)
    # Review m6: a network peer must arrive over HTTPS (a TLS proxy sets the scheme).
    with TestClient(app, base_url="https://testserver", client=("203.0.113.7", 40000)) as client:
        assert client.get("/api/v1/workflows").status_code == 401
        assert client.get("/api/v1/workflows", headers=_bearer()).status_code == 200
        host = {"Host": "nexura.example.com"}
        same = client.post(
            "/api/v1/workflows/validate",
            json={},
            headers={**_bearer(), **host, "Origin": "https://nexura.example.com"},
        )
        assert same.status_code != 403, same.text
        foreign = client.post(
            "/api/v1/workflows/validate",
            json={},
            headers={**_bearer(), **host, "Origin": "https://evil.example"},
        )
        assert foreign.status_code == 403
        assert foreign.json()["detail"]["code"] == "CROSS_ORIGIN_FORBIDDEN"
        # The service sets no cookies and sends no CORS headers.
        ok = client.get("/api/v1/workflows", headers={**_bearer(), "Origin": "https://evil.x"})
        assert "set-cookie" not in ok.headers
        assert "access-control-allow-origin" not in ok.headers


def test_failed_attempts_are_rate_limited_per_peer(tmp_path: Path) -> None:
    clock = _Clock()
    provider = OperatorTokenAuthProvider(TOKEN, max_failures=3, failure_window=60.0, clock=clock)
    app = _app(tmp_path, provider)
    https = "https://testserver"
    with (
        TestClient(app, base_url=https, client=("198.51.100.1", 1)) as attacker,
        TestClient(app, base_url=https, client=("198.51.100.2", 1)) as operator,
    ):
        codes = [attacker.get("/api/v1/workflows", headers=_bearer(WRONG)).status_code]
        codes += [attacker.get("/api/v1/workflows").status_code for _ in range(2)]
        assert codes == [401, 401, 401]
        for headers in (_bearer(WRONG), {}):
            blocked = attacker.get("/api/v1/workflows", headers=headers)
            assert blocked.status_code == 429
            assert blocked.json()["detail"]["code"] == "TOO_MANY_AUTH_FAILURES"
            assert 1 <= int(blocked.headers["retry-after"]) <= 60
        # Another peer is not affected.
        assert operator.get("/api/v1/workflows", headers=_bearer()).status_code == 200
        # Review m5: the right token always gets through, even from a blocked address (and it
        # clears that address's failures); the block only stops guessing.
        assert attacker.get("/api/v1/workflows", headers=_bearer()).status_code == 200
        assert attacker.get("/api/v1/workflows", headers=_bearer(WRONG)).status_code == 401
        for _ in range(2):
            attacker.get("/api/v1/workflows", headers=_bearer(WRONG))
        assert attacker.get("/api/v1/workflows").status_code == 429
        clock.now += 61
        assert attacker.get("/api/v1/workflows", headers=_bearer(WRONG)).status_code == 401


def test_limiter_forgets_old_failures_and_caps_tracked_peers() -> None:
    clock = _Clock()
    limiter = FailedAttemptLimiter(2, 10.0, clock=clock, max_peers=2)
    assert limiter.record_failure("a") is False
    assert limiter.record_failure("a") is True
    assert limiter.retry_after("a") == pytest.approx(10.0)
    clock.now += 5
    assert limiter.retry_after("a") == pytest.approx(5.0)
    limiter.record_failure("b")
    limiter.record_failure("c")  # "a" is dropped: at most two peers are tracked
    assert limiter.retry_after("a") == 0.0
    limiter.reset("b")
    assert limiter.retry_after("b") == 0.0


def test_token_never_appears_in_logs(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    with TestClient(_app(tmp_path)) as client:
        client.get("/api/v1/workflows", headers=_bearer(WRONG))
        client.get("/api/v1/workflows")
        client.get("/api/v1/workflows", headers=_bearer())
        client.get("/readyz", headers=_bearer())
    text = caplog.text
    assert "operator token rejected" in text
    assert TOKEN not in text and WRONG not in text
    assert hashlib.sha256(TOKEN.encode()).hexdigest() not in text


def test_existing_redaction_covers_authorization_headers() -> None:
    from nexura_agents.tracing import redact

    from nexura_runtime.integrations.channels.redaction import redact_text
    from nexura_runtime.observability.run_diagnostics import redact_run_content

    headers = {"headers": {"Authorization": f"Bearer {TOKEN}", "authorization": TOKEN}}
    assert TOKEN not in repr(redact(headers))
    assert TOKEN not in repr(redact_run_content(headers))
    assert TOKEN not in redact_text(f"Authorization: Bearer {TOKEN}")


# ----------------------------------------------------------------------------- bind and wiring


def test_remote_bind_rules(tmp_path: Path) -> None:
    # Without the token provider the existing refusal stands.
    with pytest.raises(UnauthenticatedNetworkError, match="refuses to listen"):
        create_api_app(db_path=str(tmp_path / "a.sqlite3"), listen_host="0.0.0.0")
    # With it, a non-loopback bind is plain HTTP on the network: refused unless explicit.
    with pytest.raises(PlainHttpNetworkError, match="plain HTTP"):
        _app(tmp_path, listen_host="0.0.0.0")
    with pytest.raises(PlainHttpNetworkError):
        _app(tmp_path, bound_host="192.168.1.20")
    assert _app(tmp_path, listen_host="127.0.0.1")
    assert _app(tmp_path, listen_host="0.0.0.0", allow_plain_http=True)
    with pytest.raises(ValueError, match="allow_unauthenticated_network"):
        _app(tmp_path, allow_unauthenticated_network=True)
    with pytest.raises(ValueError, match="allow_plain_http"):
        create_api_app(db_path=str(tmp_path / "b.sqlite3"), allow_plain_http=True)
    # The operator token is single-owner only; the shared profile stays enterprise.
    with pytest.raises(ValueError, match="enterprise edition"):
        _app(tmp_path, profile="authenticated-shared-service")


def test_loopback_default_is_unchanged(tmp_path: Path) -> None:
    app = create_api_app(db_path=str(tmp_path / "svc.sqlite3"))
    with TestClient(app) as client:
        assert client.get("/api/v1/workflows").status_code == 200
        refused = client.post(
            "/api/v1/runs",
            json={"workflow_id": "wf-order-support"},
            headers={"X-Nexura-Session": "wrong"},
        )
        assert refused.json()["detail"]["code"] == "SESSION_TOKEN_INVALID"
    with TestClient(app, client=("203.0.113.7", 1)) as remote:
        assert remote.get("/api/v1/workflows").json()["detail"]["code"] == "NON_LOOPBACK_CLIENT"


def _serve(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, argv: list[str]
) -> tuple[int, list[dict[str, Any]]]:
    import uvicorn

    from nexura_runtime.cli.main import main

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: calls.append({"app": app, **kw}))
    return main(["serve", "--db", str(tmp_path / "s.db"), *argv]), calls


def test_cli_remote_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, calls = _serve(monkeypatch, tmp_path, ["--host", "0.0.0.0"])
    assert (code, calls) == (2, [])
    assert "--auth token" in capsys.readouterr().err

    code, calls = _serve(monkeypatch, tmp_path, ["--host", "0.0.0.0", "--auth", "token"])
    assert (code, calls) == (2, [])
    assert "needs an operator token" in capsys.readouterr().err

    monkeypatch.setenv(OPERATOR_TOKEN_ENV, TOKEN)
    code, calls = _serve(monkeypatch, tmp_path, ["--host", "0.0.0.0", "--auth", "token"])
    assert (code, calls) == (2, [])
    assert "plain HTTP" in capsys.readouterr().err

    monkeypatch.setenv(OPERATOR_TOKEN_ENV, TOKEN)
    code, calls = _serve(
        monkeypatch, tmp_path, ["--host", "0.0.0.0", "--auth", "token", "--insecure-plain-http"]
    )
    assert code == 0 and calls[0]["host"] == "0.0.0.0"
    assert isinstance(calls[0]["app"].state.auth_provider, OperatorTokenAuthProvider)
    out = capsys.readouterr()
    assert "WARNING" in out.err and "plain HTTP" in out.err
    assert TOKEN not in out.out + out.err
    # Only the hash is kept: the variable is removed so child processes do not inherit it.
    assert OPERATOR_TOKEN_ENV not in os.environ

    monkeypatch.setenv(OPERATOR_TOKEN_ENV, TOKEN)
    code, calls = _serve(monkeypatch, tmp_path, ["--auth", "token"])
    assert code == 0 and calls[0]["host"] == "127.0.0.1"

    for argv in (
        ["--token-file", str(tmp_path / "t")],
        ["--insecure-plain-http"],
        ["--auth", "token", "--insecure-allow-unauthenticated-network"],
    ):
        monkeypatch.setenv(OPERATOR_TOKEN_ENV, TOKEN)
        code, calls = _serve(monkeypatch, tmp_path, argv)
        assert (code, calls) == (2, []), argv
    capsys.readouterr()


@POSIX_ONLY
def test_cli_token_file_permissions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "operator.token"
    path.write_text(TOKEN + "\n", encoding="utf-8")
    path.chmod(0o644)
    code, calls = _serve(monkeypatch, tmp_path, ["--auth", "token", "--token-file", str(path)])
    assert (code, calls) == (2, [])
    err = capsys.readouterr().err
    assert "chmod 600" in err and TOKEN not in err
    path.chmod(0o600)
    code, calls = _serve(monkeypatch, tmp_path, ["--auth", "token", "--token-file", str(path)])
    assert code == 0 and len(calls) == 1


# ------------------------------------------------------------------------ real server over HTTP


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_real_server_in_remote_mode_over_http(tmp_path: Path) -> None:
    port = _free_port()
    token_file = tmp_path / "operator.token"
    token_file.write_text(TOKEN + "\n", encoding="utf-8")
    if sys.platform != "win32":
        token_file.chmod(0o600)
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in (OPERATOR_TOKEN_ENV, OPERATOR_TOKEN_FILE_ENV, INSECURE_ENV_VAR)
    }
    log = tmp_path / "serve.log"
    with log.open("wb") as sink:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "nexura_runtime.cli.main",
                "serve",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--db",
                str(tmp_path / "svc.sqlite3"),
                "--auth",
                "token",
                "--token-file",
                str(token_file),
            ],
            cwd=tmp_path,
            env=env,
            stdout=sink,
            stderr=subprocess.STDOUT,
        )
        try:
            base = f"http://127.0.0.1:{port}"
            deadline = time.monotonic() + 60
            while True:
                try:
                    if httpx.get(f"{base}/healthz", timeout=2).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                assert proc.poll() is None, log.read_text(errors="replace")
                assert time.monotonic() < deadline, "server did not start"
                time.sleep(0.2)
            with httpx.Client(base_url=base, timeout=10) as client:
                assert client.get("/readyz").json().keys() == {"status"}
                assert client.get("/api/v1/workflows").status_code == 401
                assert client.get("/api/v1/workflows", headers=_bearer(WRONG)).status_code == 401
                assert client.get("/openapi.json").status_code == 401
                ok = client.get("/api/v1/workflows", headers=_bearer())
                assert ok.status_code == 200
                run = client.post(
                    "/api/v1/runs",
                    json={
                        "workflow_id": "wf-order-support",
                        "initial_state": {"order_id": "ORD-DELHI-501"},
                    },
                    headers=_bearer(),
                )
                assert run.status_code in (200, 201), run.text
                assert run.json()["status"] == "pending_approval"
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
    output = log.read_text(errors="replace")
    assert "Remote single-operator mode" in output
    assert "operator token rejected" in output or "401" in output
    assert TOKEN not in output and WRONG not in output


def test_operator_token_module_is_oss_only() -> None:
    source = (ROOT / "src/nexura_runtime/service/api/operator_token.py").read_text("utf-8")
    assert "nexura_enterprise" not in source and "enterprise/" not in source
    assert source.startswith("# SPDX-License-Identifier: Apache-2.0\n")
