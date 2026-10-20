"""Registry registration test for the connectors pack."""

from __future__ import annotations

import subprocess
import sys

import pytest

EXPECTED = {
    f"nexura.connectors.{n}"
    for n in [
        "airtable",
        "discord",
        "gdrive",
        "github",
        "gsheets",
        "imap",
        "jira",
        "mysql",
        "notion",
        "postgres",
        "s3",
        "slack",
        "smtp",
        "teams",
        "webhook",
    ]
}


def test_default_registry_lists_connectors() -> None:
    from nexura_runtime.modules import default_registry

    ids = {m["module_id"] for m in default_registry.list_modules()}
    found = {i for i in ids if i.startswith("nexura.connectors.")}
    assert len(EXPECTED) == 15
    assert found == EXPECTED, sorted(found)


def test_cold_imports_have_no_cycle() -> None:
    for stmt in ("import nexura_runtime.modules", "import nexura_runtime"):
        out = subprocess.run([sys.executable, "-c", stmt], capture_output=True, text=True)
        assert out.returncode == 0, out.stderr


# -- audit 11: credential type, operator-only hosts, clamps ---------------------------------
async def test_connector_refuses_credential_of_another_type() -> None:
    from connector_helpers import Recorder, json_response
    from nexura_agents.contracts.context import ExecutionContext
    from nexura_connectors import GitHubConnector

    from nexura_runtime.credentials.store import ScopedCredentialStore

    store = ScopedCredentialStore()
    store.store("w1", "llm", "openai_api_key", "sk-not-for-github-0123456789", provider="openai")
    store.store("w1", "gh", "github_token", "ghx_tokenvalue_0123456789")
    rec = Recorder(lambda r: json_response({"ok": 1}))
    ctx = ExecutionContext(run_id="r", node_id="n", credential_resolver=store.for_workspace("w1"))
    mod = GitHubConnector(transport=rec.transport)
    inputs = {"operation": "get_repo", "params": {"owner": "o", "repo": "r"}}
    bad = await mod.execute(ctx, inputs, {"credential_ref": "llm"})
    assert bad.error_code == "MISSING_CREDENTIAL" and "openai_api_key" in (bad.error_message or "")
    assert rec.requests == []
    ok = await mod.execute(ctx, inputs, {"credential_ref": "gh"})
    assert ok.status == "success"


async def test_flow_config_cannot_choose_base_url_or_open_hosts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from connector_helpers import Recorder, invoke, json_response
    from nexura_connectors import GitHubConnector

    monkeypatch.delenv("NEXURA_CONNECTOR_ALLOWED_HOSTS", raising=False)
    rec = Recorder(lambda r: json_response({"ok": 1}))
    repo = {"owner": "o", "repo": "r"}
    res = await invoke(
        GitHubConnector(transport=rec.transport),
        "get_repo",
        repo,
        cfg={"base_url": "https://evil.example", "allowed_hosts": ["evil.example"]},
    )
    assert res.error_code == "SSRF_BLOCKED" and rec.requests == []
    # operator paths: constructor, env, execution-context settings
    for mod_kw, env in (({"allowed_hosts": ["ghe.example"]}, None), ({}, "a.example, ghe.example")):
        if env:
            monkeypatch.setenv("NEXURA_CONNECTOR_ALLOWED_HOSTS", env)
        res = await invoke(
            GitHubConnector(transport=rec.transport, **mod_kw),
            "get_repo",
            repo,
            cfg={"base_url": "https://ghe.example"},
        )
        assert res.status == "success", res.error_message
    monkeypatch.delenv("NEXURA_CONNECTOR_ALLOWED_HOSTS")
    from connector_helpers import make_context

    ctx = make_context()
    ctx.attributes["connector_allowed_hosts"] = ["ghe.example"]
    res = await GitHubConnector(transport=rec.transport).execute(
        ctx,
        {"operation": "get_repo", "params": repo},
        {"credential_ref": "cred", "base_url": "https://ghe.example"},
    )
    assert res.status == "success", res.error_message


def test_limits_are_clamped_without_schema_validation() -> None:
    from nexura_connectors import GitHubConnector

    http = GitHubConnector().build_http(
        {"timeout_seconds": 10**7, "max_retries": 99, "max_response_bytes": 10**12}, "x"
    )
    assert http.timeout == 120.0 and http.max_retries == 3 and http.max_bytes == 10_000_000
    low = GitHubConnector().build_http({"timeout_seconds": 1e-9, "max_retries": -5}, "x")
    assert low.timeout == 0.1 and low.max_retries == 0
