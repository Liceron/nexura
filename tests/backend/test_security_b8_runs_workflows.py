"""B8 regression tests: run/workflow redaction, tenant-scoped run listing, workflow ids."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

from nexura_runtime.observability.run_diagnostics import (
    is_secret_key,
    redact_run_content,
    redact_secret_text,
)
from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.workflow import ModuleRegistry, WorkflowNode, WorkflowSpec

sys.path.insert(0, str(Path(__file__).parent))
from test_security_b7_triggers import _local  # noqa: E402

S = "Zq9xLeakValue7731"
API = "/api/v1"
SPEC = {
    "name": "w",
    "entry_node": "n",
    "terminal_nodes": ["n"],
    "nodes": {"n": {"id": "n", "handler": "synthetic_order_lookup", "transitions": {}}},
}


@pytest.mark.parametrize(
    "key",
    [
        "accessToken",
        "clientSecret",
        "refreshToken",
        "client-secret",
        "set-cookie",
        "Set-Cookie",
        "proxy-authorization",
        "passphrase",
        "pwd",
        "private_key",
        "privateKey",
        "X-Api-Key",
        "ACCESSTOKEN",
    ],
)
def test_secret_key_name_variants(key: str) -> None:
    assert is_secret_key(key)
    assert redact_run_content({key: S}) == {key: "[REDACTED]"}


@pytest.mark.parametrize("key", ["max_tokens", "promptTokens", "prompt_tokens", "name", "monkey"])
def test_non_secret_keys_kept(key: str) -> None:
    assert not is_secret_key(key)


def test_text_value_patterns() -> None:
    pem = "-----BEGIN RSA PRIVATE KEY-----\n" + S + "\n-----END RSA PRIVATE KEY-----"
    samples = [
        f'{{"password": "{S} p"}}',
        f"Authorization: Basic {S}",
        f"Bearer {S}",
        "ghp_" + "a" * 36 + S,
        "AKIAIOSFODNN7EXAMPLE",
        "xoxb-123456789012-abcdefghijklmnop",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abc123def456ghi",
        pem,
    ]
    for text in samples:
        out = redact_secret_text("failed: " + text)
        assert S not in out
        for frag in ("AKIAIOSFODNN7", "xoxb-", "eyJhbGci", "ghp_", "BEGIN RSA"):
            assert frag not in out
    assert redact_secret_text("ordinary text, 5 items") == "ordinary text, 5 items"


async def _client(tmp_path: Path, handler: Any) -> tuple[httpx.AsyncClient, str]:
    reg = ModuleRegistry()
    reg.register("h", handler)
    spec = WorkflowSpec(
        name="g",
        entry_node="a",
        nodes={"a": WorkflowNode(id="a", handler="h")},
        terminal_nodes=["a"],
    ).to_dict()
    app = create_api_app(registry=reg, db_path=tmp_path / "s.sqlite3")
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
    r = await c.post(f"{API}/runs", json={"spec": spec, "initial_state": {"password": S, "pwd": S}})
    assert r.status_code in (200, 201), r.text
    assert S not in r.text
    return c, r.json()["run_id"]


@pytest.mark.asyncio
async def test_run_snapshot_and_listing_redacted(tmp_path: Path) -> None:
    async def h(state: dict[str, Any]) -> Any:
        return {"accessToken": S, "set-cookie": S, "note": "Bearer " + S}

    c, rid = await _client(tmp_path, h)
    if True:
        for path in (
            f"{API}/runs/{rid}",
            f"{API}/runs",
            f"{API}/runs/{rid}/events",
            f"{API}/runs/{rid}/history",
            f"{API}/runs/{rid}/subruns",
        ):
            r = await c.get(path, headers={"accept": "application/json"})
            assert r.status_code == 200, path
            assert S not in r.text, path


@pytest.mark.asyncio
async def test_error_text_redacted(tmp_path: Path) -> None:
    async def h(state: dict[str, Any]) -> Any:
        raise RuntimeError(f'401 {{"password": "{S}"}} Authorization: Basic {S} ghp_{"b" * 30}')

    c, rid = await _client(tmp_path, h)
    if True:
        for path in (f"{API}/runs/{rid}", f"{API}/runs/{rid}/events"):
            r = await c.get(path, headers={"accept": "application/json"})
            assert S not in r.text and "ghp_" not in r.text


def test_run_listing_not_hidden_by_other_tenant(tmp_path: Path) -> None:
    c, k, app = _local(tmp_path)
    ra = c.post(f"{API}/runs", json={"workflow_id": "wf"}, headers=k["a"])
    assert ra.status_code == 201, ra.text
    run_a = ra.json()["run_id"]
    for _ in range(25):
        assert (
            c.post(f"{API}/runs", json={"workflow_id": "wf_b"}, headers=k["b"]).status_code == 201
        )
    # Tenant B's newer runs must not push A's run out of A's page.
    listed = c.get(f"{API}/runs?limit=1", headers=k["a"]).json()
    assert [r["run_id"] for r in listed["items"]] == [run_a]
    assert listed["total"] == 1
    listed_b = c.get(f"{API}/runs?limit=10&offset=20", headers=k["b"]).json()
    assert listed_b["total"] == 25 and len(listed_b["items"]) == 5
    assert run_a not in [r["run_id"] for r in listed_b["items"]]
    assert c.get(f"{API}/runs?status=nonexistent", headers=k["a"]).json()["total"] == 0


def test_workflow_id_owned_elsewhere_conflicts_and_read_is_denied(tmp_path: Path) -> None:
    c, k, _ = _local(tmp_path)
    r = c.post(
        f"{API}/workflows", headers=k["a"], json={"name": "w", "workflow_id": "wf-a", "spec": SPEC}
    )
    assert r.status_code == 201 and r.json()["workflow_id"] == "wf-a"
    p = c.post(
        f"{API}/workflows", headers=k["b"], json={"name": "w", "workflow_id": "wf-a", "spec": SPEC}
    )
    # Local owner: reusing another local workspace's id conflicts
    assert p.status_code == 409
    # A's workflow untouched; local owner: B's read by id is denied and names the owner
    foreign = c.get(f"{API}/workflows/wf-a", headers=k["b"])
    assert foreign.status_code == 403
    assert foreign.json()["detail"]["code"] == "CROSS_WORKSPACE_ACCESS_DENIED"
    assert "'wsA'" in foreign.json()["detail"]["message"]
    assert "spec" not in foreign.json()
    own = c.get(f"{API}/workflows/wf-a", headers=k["a"])
    assert own.status_code == 200 and own.json()["revision"] == 1
    # Same-workspace duplicate still conflicts
    d = c.post(
        f"{API}/workflows", headers=k["a"], json={"name": "w", "workflow_id": "wf-a", "spec": SPEC}
    )
    assert d.status_code == 409


def test_local_workflow_secret_never_crosses_workspace_boundary(tmp_path: Path) -> None:
    c, k, _ = _local(tmp_path, (("v", "wsA", "vic", "viewer"),))
    spec = {
        "spec_version": 1,
        "name": "g",
        "entry_node": "a",
        "terminal_nodes": ["a"],
        "nodes": {
            "a": {
                "id": "a",
                "handler": "synthetic_router",
                "config": {"api_key": S, "headers": {"Authorization": f"Bearer {S}"}},
            }
        },
    }
    r = c.post(
        f"{API}/workflows", headers=k["a"], json={"workflow_id": "w1", "name": "w", "spec": spec}
    )
    assert r.status_code == 201
    foreign = c.get(f"{API}/workflows/w1", headers=k["b"])
    assert foreign.status_code == 403
    assert foreign.json()["detail"]["code"] == "CROSS_WORKSPACE_ACCESS_DENIED"
    assert S not in foreign.text
    # A forged viewer header cannot lower the sole local owner's admin role.
    assert S in c.get(f"{API}/workflows/w1", headers=k["v"]).text
    assert S in c.get(f"{API}/workflows/w1", headers=k["a"]).text


@pytest.mark.asyncio
async def test_run_snapshot_scrubs_node_config_secrets(tmp_path: Path) -> None:
    """A run's echoed spec must not carry node-config secrets (B8 verifier finding)."""
    secret = "Zq9xLeakValue7731"
    registry = ModuleRegistry()

    async def handler(state: dict[str, Any]) -> dict[str, Any]:
        return {"x": 1}

    registry.register("h", handler)
    node = WorkflowNode(id="a", handler="h", config={"api_key": secret})
    spec = WorkflowSpec(name="g", entry_node="a", nodes={"a": node}, terminal_nodes=["a"])
    app = create_api_app(registry=registry, db_path=tmp_path / "s.sqlite3")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as client:
        started = await client.post(
            "/api/v1/runs", json={"spec": spec.to_dict(), "initial_state": {}}
        )
        run_id = started.json()["run_id"]
        fetched = await client.get(f"/api/v1/runs/{run_id}")
        assert started.json()["status"] == "completed"
        assert secret not in started.text and secret not in fetched.text
