"""Regression tests for audit 10 fixes (B19: security + API)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from httpx import ASGITransport

from nexura_runtime.service.api.app import create_api_app

VIEWER = {"x-principal-id": "vic", "x-principal-role": "viewer"}

CASES = [
    ("PUT", "/api/v1/evaluations/studio/datasets/d1", {"name": "n", "cases": []}),
    ("DELETE", "/api/v1/evaluations/studio/datasets/d1", None),
    (
        "POST",
        "/api/v1/evaluations/studio/runs",
        {"dataset_id": "d1", "target": {"kind": "workflow", "id": "w"}},
    ),
    ("DELETE", "/api/v1/evaluations/studio/runs/r1", None),
    ("POST", "/api/v1/builder/draft", {"description": "an agent that answers"}),
    ("POST", "/api/v1/templates/support-desk-refund-approval/instantiate", {}),
    ("POST", "/api/v1/workflows/import/python", {"source": "x = 1"}),
    ("POST", "/api/v1/workflows/w/export", {}),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path,body", CASES, ids=[f"{m} {p}" for m, p, _ in CASES])
async def test_new_endpoints_ignore_forged_identity_and_role(
    tmp_path: Any, method: str, path: str, body: Any
) -> None:
    app = create_api_app(db_path=str(tmp_path / "t.db"), profile="local-owner")
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://localhost"
    ) as c:
        normal = await c.request(method, path, json=body)
    other_app = create_api_app(db_path=str(tmp_path / "viewer.db"), profile="local-owner")
    async with httpx.AsyncClient(
        transport=ASGITransport(app=other_app), base_url="http://localhost"
    ) as c:
        r = await c.request(method, path, headers=VIEWER, json=body)
    assert r.status_code == normal.status_code, (r.status_code, r.text, normal.text)
    if path.endswith("/instantiate"):
        assert r.status_code == 201
        assert r.json()["workflow_id"]
    else:
        assert r.status_code in (400, 404, 422), r.text
        assert r.json()["detail"]["code"] == normal.json()["detail"]["code"]


SECRET_PAYLOAD = {
    "apiKey": "sk-abcdef1234567890abcdef",
    "note": "k sk-ant-api03-abcdefghijklmnopqrstuvwx Bearer abcdefghijklmnopqrstuv0123 "
    "Basic dXNlcjpwYXNzd29yZA== ghp_abcdefghijklmnopqrstuvwxyz0123456789",
}
LEAKS = (
    "sk-abcdef1234567890abcdef",
    "sk-ant-api03-abcdefghijklmnopqrstuvwx",
    "abcdefghijklmnopqrstuv0123",
    "dXNlcjpwYXNzd29yZA==",
    "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
)


def test_sdk_trace_redact_matches_run_redaction() -> None:
    from nexura_agents.tracing import redact

    from nexura_runtime.observability.run_diagnostics import redact_run_content

    a = json.dumps(redact(SECRET_PAYLOAD))
    b = json.dumps(redact_run_content(SECRET_PAYLOAD))
    assert [s for s in LEAKS if s in a] == [s for s in LEAKS if s in b]


def test_eval_runner_pii_redaction_covers_provider_keys() -> None:
    from nexura_runtime.evaluation.runner import redact_pii

    out = redact_pii(json.dumps(SECRET_PAYLOAD))
    assert not [s for s in LEAKS if s in out], [s for s in LEAKS if s in out]


@pytest.mark.parametrize(
    "token",
    [
        "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        "gho_abcdefghijklmnopqrstuvwxyz0123456789",
        "ghu_abcdefghijklmnopqrstuvwxyz0123456789",
        "ghs_abcdefghijklmnopqrstuvwxyz0123456789",
        "ghr_abcdefghijklmnopqrstuvwxyz0123456789",
        "github_pat_11ABCDEFG0abcdefghijklmnopqrstuvwxyz",
    ],
)
def test_trace_redact_github_tokens(token: str) -> None:
    from nexura_agents.tracing import redact

    assert token not in json.dumps(redact({"note": f"use {token} now"}))


def test_trace_redact_keeps_ordinary_text() -> None:
    from nexura_agents.tracing import redact

    text = "ghost_story and github_page and ghp_short are fine"
    assert redact({"note": text})["note"] == text
