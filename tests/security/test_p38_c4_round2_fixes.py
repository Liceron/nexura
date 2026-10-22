# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
# P38 C4 re-review (round 2) repros, moved from .autopilot/p38/c4_repros/round2. Each asserts
# the secure / documented behaviour; all three failed before the R2 fixes. Offline: in-process
# apps and one short child process. Only ROOT (new location) and the header changed, plus
# tests added for the fixes at the end.
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.service.api.operator_token import OperatorTokenAuthProvider

ROOT = Path(__file__).resolve().parents[2]
TOKEN = "Qx7-v2Lr_9mZpA4tKc8NfWj3bYsH6uDe1oRgTiVnXqE"  # test-only
HTTPS = "https://nexura.example"


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


# --------------------------------------------------------------------------- R2-1 (minor)
def test_block_does_not_limit_guessing_so_docs_must_not_say_it_does(tmp_path: Path) -> None:
    """Since the m5 fix, a blocked peer's guesses are still evaluated: wrong -> 429, right ->
    200, and blocked attempts are not counted. The block therefore never slows guessing; it
    only changes the status code. The guide still says it does."""
    provider = OperatorTokenAuthProvider(TOKEN, max_failures=3, clock=_Clock())
    app = create_api_app(db_path=str(tmp_path / "svc.sqlite3"), auth_provider=provider)
    with TestClient(app, base_url=HTTPS, client=("203.0.113.9", 1)) as attacker:
        for i in range(3):
            attacker.get("/api/v1/workflows", headers=_bearer(f"{TOKEN[:-3]}{i:03d}"))
        codes = {
            attacker.get("/api/v1/workflows", headers=_bearer(f"{TOKEN[:-3]}x{i:02d}")).status_code
            for i in range(200)  # 200 more guesses while blocked: all evaluated, none counted
        }
        assert codes == {429}
        assert attacker.get("/api/v1/workflows", headers=_bearer(TOKEN)).status_code == 200
    guide = (ROOT / "docs/remote-single-operator.md").read_text("utf-8")
    assert "slows guessing" not in guide and "Guessing is limited per address" not in guide, (
        "docs claim the per-address block limits guessing; a blocked peer can still test "
        "unlimited guesses (429 = wrong, 200 = right)"
    )


# --------------------------------------------------------------------------- R2-2 (minor)
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX FIFO")
def test_token_file_that_is_a_fifo_is_refused_without_hanging(tmp_path: Path) -> None:
    """os.open(O_RDONLY) on a FIFO blocks until a writer appears, before fstat can refuse it.
    `serve --token-file <fifo>` hangs at startup instead of failing. Fix: add O_NONBLOCK."""
    fifo = tmp_path / "operator.token"
    os.mkfifo(fifo, 0o600)
    code = (
        "from nexura_runtime.service.api.operator_token import read_token_file, "
        "OperatorTokenError\n"
        f"try:\n    read_token_file({str(fifo)!r})\nexcept OperatorTokenError:\n"
        "    print('refused')\n"
    )
    try:
        out = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=10
        )
    except subprocess.TimeoutExpired:
        pytest.fail("read_token_file blocked on a FIFO instead of refusing it")
    assert out.stdout.strip() == "refused"


# --------------------------------------------------------------------------- R2-3 (minor)
def test_one_provider_shared_by_two_apps_keeps_each_apps_delivery_tokens(tmp_path: Path) -> None:
    """The delivery-token store hangs off the provider, not the app. A second create_api_app with
    the same provider repoints it to the second database, and shutting either app down sets it
    to None for both: the other app then silently registers webhooks WITHOUT a delivery token
    (and refuses every delivery). Fails closed, but the registration answer is wrong."""
    provider = OperatorTokenAuthProvider(TOKEN)
    app1 = create_api_app(db_path=str(tmp_path / "one.sqlite3"), auth_provider=provider)
    app2 = create_api_app(db_path=str(tmp_path / "two.sqlite3"), auth_provider=provider)
    with TestClient(app1, base_url=HTTPS) as c1:
        with TestClient(app2, base_url=HTTPS):
            pass  # app2 starts and stops (e.g. a test, a reload, a second mount)
        reg = c1.post(
            "/api/v1/triggers/webhooks",
            json={"endpoint_id": "ep-shared", "workflow_id": "wf-order-support"},
            headers=_bearer(TOKEN),
        )
    assert reg.status_code in (200, 201), reg.text
    assert "delivery_token" in reg.json(), (
        "remote-mode registration returned no delivery token after another app sharing the "
        "provider shut down"
    )


# --------------------------------------------------------------------------- tests of the fixes


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX FIFO")
def test_fifo_refusal_names_the_reason(tmp_path: Path) -> None:
    from nexura_runtime.service.api.operator_token import OperatorTokenError, read_token_file

    fifo = tmp_path / "pipe.token"
    os.mkfifo(fifo, 0o600)
    with pytest.raises(OperatorTokenError, match="not a regular file"):
        read_token_file(fifo)


def test_two_live_apps_sharing_a_provider_keep_separate_stores(tmp_path: Path) -> None:
    import sqlite3

    provider = OperatorTokenAuthProvider(TOKEN)
    app1 = create_api_app(db_path=str(tmp_path / "one.sqlite3"), auth_provider=provider)
    app2 = create_api_app(db_path=str(tmp_path / "two.sqlite3"), auth_provider=provider)
    assert not hasattr(provider, "delivery_tokens")
    assert app1.state.delivery_tokens is not app2.state.delivery_tokens
    with TestClient(app1, base_url=HTTPS) as c1, TestClient(app2, base_url=HTTPS) as c2:
        for client, endpoint in ((c1, "ep-one"), (c2, "ep-two")):
            r = client.post(
                "/api/v1/triggers/webhooks",
                json={"endpoint_id": endpoint, "workflow_id": "wf-order-support"},
                headers=_bearer(TOKEN),
            )
            assert "delivery_token" in r.json(), r.text
        token_one = app1.state.delivery_tokens.issue("ep-one")
        # app2's store does not know app1's endpoint, so app1's token is refused there.
        refused = c2.post(
            "/api/v1/triggers/webhooks/ep-one/deliver", json={}, headers=_bearer(token_one)
        )
        assert refused.status_code == 401
    for name, endpoint in (("one", "ep-one"), ("two", "ep-two")):
        with sqlite3.connect(str(tmp_path / f"{name}.sqlite3")) as conn:
            ids = {
                r[0] for r in conn.execute("SELECT endpoint_id FROM nexura_webhook_delivery_tokens")
            }
        assert ids == {endpoint}


def test_extensions_cannot_replace_the_delivery_token_store() -> None:
    from nexura_runtime.service.extensions import PROTECTED_STATE

    assert "delivery_tokens" in PROTECTED_STATE


def test_local_mode_app_has_no_delivery_token_store(tmp_path: Path) -> None:
    app = create_api_app(db_path=str(tmp_path / "local.sqlite3"))
    assert app.state.delivery_tokens is None


def test_security_model_states_the_guessing_limit_honestly() -> None:
    text = " ".join((ROOT / "SECURITY_MODEL.md").read_text("utf-8").split())
    assert "does not limit guessing" in text
    assert "secrets.token_urlsafe(32)" in text
    assert "cannot measure the strength of a human-made one" in text
    guide = " ".join((ROOT / "docs/remote-single-operator.md").read_text("utf-8").split())
    assert "Resistance to guessing comes entirely from the token's randomness" in guide
    assert "only marks the address and limits log noise and load" in guide
