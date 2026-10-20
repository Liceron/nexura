"""B39 review regressions (Python side), promoted from .autopilot/b39/repros.

canonicalJson must equal Python's json.dumps(sort_keys=True, separators=(",", ":")), and the
Python snippet Studio shows must be accepted by the authenticated shared service. Node >= 22.18
loads webhookSnippet.ts with type stripping. Local in-process TestClient only, no network.
"""

from __future__ import annotations

import io
import json
import subprocess
import urllib.request
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.service.triggers.signatures import serialize_payload

ROOT = Path(__file__).resolve().parents[2]
SNIPPET_TS = ROOT / "studio-web" / "src" / "features" / "triggers" / "webhookSnippet.ts"
ORIGIN = "http://testserver"
SPEC = {
    "name": "B39 review target",
    "entry_node": "a",
    "nodes": {"a": {"id": "a", "handler": "synthetic_order_lookup"}},
    "terminal_nodes": ["a"],
}


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


# --- canonicalJson must equal json.dumps(sort_keys=True, separators=(",", ":")) -------------


def test_canonical_json_escapes_del_like_python() -> None:
    """Python's ensure_ascii escapes U+007F (DEL); escapeNonAscii starts at U+0080."""
    value = {"a": "x\x7fy"}
    assert _ts('m.canonicalJson({"a": "x\\u007fy"})') == serialize_payload(value).decode()


def test_canonical_json_sorts_keys_by_code_point_like_python() -> None:
    """Python sorts str keys by code point; JS `<` compares UTF-16 code units, so an astral
    key (surrogate pair 0xD83D...) sorts before U+FF61 in JS but after it in Python."""
    value = {"｡": 1, "\U0001f600": 2}
    got = _ts('m.canonicalJson({"\\uff61": 1, "\\ud83d\\ude00": 2})')
    assert got == serialize_payload(value).decode()


# --- the Studio snippet must work against the authenticated shared service ----------------


def test_python_snippet_is_accepted_by_local_service_without_api_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The actual generated Python snippet signs and delivers locally without an API key."""
    # tests/conftest.py is not loaded for .autopilot repros: same opt-out it sets.
    monkeypatch.setenv("NEXURA_INSECURE_ALLOW_PLAINTEXT_SECRETS", "I_UNDERSTAND_THIS_IS_UNSAFE")
    key: dict[str, str] = {}
    client = TestClient(create_api_app(db_path=tmp_path / "local.db", listen_host="127.0.0.1"))
    r = client.post("/api/v1/workflows", headers=key, json={"workflow_id": "wf", "spec": SPEC})
    assert r.status_code in (200, 201), r.text
    reg = client.post(
        "/api/v1/triggers/webhooks",
        headers=key,
        json={"endpoint_id": "orders", "workflow_id": "wf"},
    )
    assert reg.status_code == 200, reg.text
    secret = reg.json()["secret"]

    url = _ts(f"m.webhookDeliveryUrl({json.dumps(ORIGIN)}, 'orders')")
    snippet = _ts(f"m.buildPythonSnippet({{ deliveryUrl: {json.dumps(url)} }})")
    responses: list[Any] = []

    class _Resp(io.BytesIO):
        def __init__(self, resp: Any) -> None:
            super().__init__(resp.content)
            self.status = resp.status_code

        def __enter__(self) -> _Resp:
            return self

        def __exit__(self, *exc: object) -> None:
            self.close()

    def fake_urlopen(req: urllib.request.Request) -> _Resp:
        assert not req.has_header("Authorization")
        resp = client.post(
            req.full_url[len(ORIGIN) :], content=req.data, headers=dict(req.header_items())
        )
        responses.append(resp)
        return _Resp(resp)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("NEXURA_WEBHOOK_SECRET", secret)
    monkeypatch.delenv("NEXURA_API_KEY", raising=False)
    exec(compile(snippet, "<b39 snippet>", "exec"), {"__name__": "__b39__"})
    assert len(responses) == 1
    assert responses[0].status_code == 200, responses[0].text
    assert responses[0].json()["accepted"] is True
