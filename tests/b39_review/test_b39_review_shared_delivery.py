"""Local Studio signing snippets: no API key, mandatory HMAC and replay/body guards."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.service.api.triggers import MAX_WEBHOOK_BODY_BYTES
from nexura_runtime.service.triggers.signatures import compute_webhook_signature

ROOT = Path(__file__).resolve().parents[2]
SNIPPET_TS = ROOT / "studio-web" / "src" / "features" / "triggers" / "webhookSnippet.ts"
ORIGIN = "http://testserver"
DELIVER = "/api/v1/triggers/webhooks/orders/deliver"
SPEC = {
    "name": "B39 shared delivery target",
    "entry_node": "a",
    "nodes": {"a": {"id": "a", "handler": "synthetic_order_lookup"}},
    "terminal_nodes": ["a"],
}

needs_tools = pytest.mark.skipif(
    not (shutil.which("node") and shutil.which("openssl") and shutil.which("sh")),
    reason="needs node, openssl and sh",
)


def _ts(call: str) -> Any:
    script = (
        f"import * as m from {json.dumps(SNIPPET_TS.as_uri())};\n"
        f"process.stdout.write(JSON.stringify({call}));\n"
    )
    proc = subprocess.run(
        ["node", "--no-warnings", "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return json.loads(proc.stdout)


@pytest.fixture()
def local(tmp_path: Path) -> tuple[TestClient, str]:
    client = TestClient(create_api_app(db_path=tmp_path / "local.db", listen_host="127.0.0.1"))
    r = client.post("/api/v1/workflows", json={"workflow_id": "wf", "spec": SPEC})
    assert r.status_code == 201, r.text
    reg = client.post(
        "/api/v1/triggers/webhooks", json={"endpoint_id": "orders", "workflow_id": "wf"}
    )
    assert reg.status_code == 200, reg.text
    return client, reg.json()["secret"]


def _run_curl(snippet: str, env_extra: dict[str, str], tmp_path: Path) -> dict[str, Any]:
    record = tmp_path / "curl-args"
    fake = (
        "curl() {\n"
        f"  : > {record}\n"
        f'  for a in "$@"; do printf \'%s\\0\' "$a" >> {record}; done\n'
        "}\n"
    )
    env = {k: v for k, v in os.environ.items() if k != "NEXURA_API_KEY"}
    env.update(env_extra)
    subprocess.run(["sh", "-c", fake + snippet], env=env, check=True, timeout=60)
    argv = [a.decode() for a in record.read_bytes().split(b"\0")[:-1]]
    headers: dict[str, str] = {}
    data: bytes | None = None
    url = ""
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "-H":
            name, _, value = argv[i + 1].partition(":")
            headers[name.strip()] = value.strip()
            i += 2
        elif a == "-d":
            data = argv[i + 1].encode()
            i += 2
        elif a == "-X":
            i += 2
        elif a == "-s":
            i += 1
        else:
            url = a
            i += 1
    assert data is not None and url.startswith(ORIGIN), argv
    return {"path": url[len(ORIGIN) :], "headers": headers, "data": data}


@needs_tools
def test_curl_snippet_without_bearer_is_accepted_locally(
    local: tuple[TestClient, str], tmp_path: Path
) -> None:
    client, secret = local
    url = _ts(f"m.webhookDeliveryUrl({json.dumps(ORIGIN)}, 'orders')")
    snippet = _ts(f"m.buildCurlSnippet({{ deliveryUrl: {json.dumps(url)} }})")
    assert secret not in snippet
    env = {"NEXURA_WEBHOOK_SECRET": secret}
    req = _run_curl(snippet, env, tmp_path)
    assert "Authorization" not in req["headers"]
    r = client.post(req["path"], content=req["data"], headers=req["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["accepted"] is True


@needs_tools
def test_curl_snippet_omits_bearer_when_variable_is_unset(
    local: tuple[TestClient, str], tmp_path: Path
) -> None:
    client, secret = local
    url = _ts(f"m.webhookDeliveryUrl({json.dumps(ORIGIN)}, 'orders')")
    snippet = _ts(f"m.buildCurlSnippet({{ deliveryUrl: {json.dumps(url)} }})")
    req = _run_curl(snippet, {"NEXURA_WEBHOOK_SECRET": secret}, tmp_path)
    assert "Authorization" not in req["headers"]  # unset: no empty bearer header
    r = client.post(req["path"], content=req["data"], headers=req["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["accepted"] is True


def _signed(secret: str, body: dict[str, Any], event_id: str, ts: float) -> dict[str, str]:
    return {
        "content-type": "application/json",
        "X-Webhook-Timestamp": str(int(ts)),
        "X-Webhook-Event-ID": event_id,
        "X-Webhook-Signature": compute_webhook_signature(secret, int(ts), body, event_id),
    }


def test_local_owner_cannot_bypass_signature_window_or_body_checks(
    local: tuple[TestClient, str],
) -> None:
    """The local owner still needs HMAC, a fresh timestamp and a bounded body."""
    client, secret = local
    auth: dict[str, str] = {}
    body = {"event": "ping"}
    content = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    now = time.time()

    unsigned = client.post(
        DELIVER, content=content, headers={**auth, "content-type": "application/json"}
    )
    assert unsigned.status_code == 401
    assert unsigned.json()["detail"]["code"] == "MISSING_SIGNATURE_HEADERS"

    forged = client.post(
        DELIVER, content=content, headers={**auth, **_signed("not-the-secret", body, "e1", now)}
    )
    assert forged.status_code == 401
    assert forged.json()["detail"]["code"] == "INVALID_SIGNATURE"

    stale = client.post(
        DELIVER, content=content, headers={**auth, **_signed(secret, body, "e2", now - 3600)}
    )
    assert stale.status_code == 400
    assert stale.json()["detail"]["code"] == "REPLAY_EXPIRED"

    big = {"pad": "x" * (MAX_WEBHOOK_BODY_BYTES + 1)}
    big_content = json.dumps(big, sort_keys=True, separators=(",", ":")).encode()
    oversize = client.post(
        DELIVER, content=big_content, headers={**auth, **_signed(secret, big, "e3", now)}
    )
    assert oversize.status_code == 413

    ok = client.post(DELIVER, content=content, headers={**auth, **_signed(secret, body, "e4", now)})
    assert ok.status_code == 200, ok.text
    jobs = client.get("/api/v1/triggers/jobs", headers=auth).json()["jobs"]
    assert len([j for j in jobs if j["trigger_type"] == "webhook"]) == 1


def test_signed_delivery_without_key_is_accepted_locally(
    local: tuple[TestClient, str],
) -> None:
    """A signed local delivery requires no login, token or API key."""
    client, secret = local
    body = {"event": "ping"}
    content = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    r = client.post(DELIVER, content=content, headers=_signed(secret, body, "e1", time.time()))
    assert r.status_code == 200, r.text
    assert r.json()["accepted"] is True
    assert client.get("/api/v1/triggers/jobs").status_code == 200
