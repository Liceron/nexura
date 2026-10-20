"""P28 step 5: OSS credentials still work with env and local backends (EDITIONS_PLAN step-5 row).

Organisations and the hosted secrets features moved to the closed edition; the open-source
edition keeps env-var credential references and the local encrypted secrets file (founder
decision 2026-10-18). These tests run with no ``nexura_enterprise`` package installed: a
connector resolves its credential from the local encrypted store (after a service restart) and
from an environment-variable reference, through the open-source ``ScopedCredentialStore``. All
HTTP goes through ``httpx.MockTransport``; nothing touches the network or a model.
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from httpx import ASGITransport
from nexura_agents.contracts.context import CredentialReference, ExecutionContext
from nexura_connectors import GitHubConnector

from nexura_runtime.credentials import ScopedCredentialStore
from nexura_runtime.secrets import (
    EncryptedSecretStore,
    MasterKeyMismatchError,
    generate_master_key,
)
from nexura_runtime.service.api.app import create_api_app

TOKEN = "ghp_local_SENTINEL_0123456789abcdef"
ENV_TOKEN = "ghp_env_SENTINEL_fedcba9876543210"
ENV_VAR = "NEXURA_P28S5_GITHUB_TOKEN"
REPO = {"owner": "acme", "repo": "widgets"}


@pytest.fixture(autouse=True)
def no_enterprise() -> Iterator[None]:
    """The closed edition is not installed, and nothing below imports it."""
    assert importlib.util.find_spec("nexura_enterprise") is None
    yield
    assert not any(m.split(".")[0] == "nexura_enterprise" for m in sys.modules)


class _Recorder:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.transport = httpx.MockTransport(self._serve)

    def _serve(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, json={"full_name": "acme/widgets"})


async def _get_repo(resolver: Any, credential_ref: str) -> tuple[Any, _Recorder]:
    rec = _Recorder()
    ctx = ExecutionContext(run_id="run-1", node_id="node-1", credential_resolver=resolver)
    result = await GitHubConnector(transport=rec.transport).execute(
        ctx, {"operation": "get_repo", "params": REPO}, {"credential_ref": credential_ref}
    )
    return result, rec


def _file_bytes(path: Path) -> bytes:
    data = b""
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(path) + suffix)
        if p.exists():
            data += p.read_bytes()
    return data


def _client(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


async def test_connector_credential_resolves_from_the_local_encrypted_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = generate_master_key()
    monkeypatch.setenv("NEXURA_MASTER_KEY", key)
    monkeypatch.delenv("NEXURA_INSECURE_ALLOW_PLAINTEXT_SECRETS", raising=False)
    db = tmp_path / "svc.sqlite3"
    app = create_api_app(db_path=db, profile="local-owner")
    assert isinstance(app.state.secret_store, EncryptedSecretStore)
    async with _client(app) as c:
        made = await c.post(
            "/api/v1/credentials",
            json={"credential_id": "gh", "credential_type": "github_token", "secret": TOKEN},
        )
        assert made.status_code == 201, made.text
        assert TOKEN not in made.text and made.json()["masked_hint"].endswith(TOKEN[-4:])
        assert (await c.get("/api/v1/secrets")).status_code == 404  # hosted API: closed edition
    assert TOKEN.encode() not in _file_bytes(db)  # encrypted at rest

    # Restart: a new service on the same file and key reads the credential back.
    restarted = create_api_app(db_path=db, profile="local-owner")
    resolver = restarted.state.credential_store.for_workspace("default")
    result, rec = await _get_repo(resolver, "gh")
    assert result.status == "success", result.error_message
    assert rec.requests[-1].headers["authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in str(result.outputs)

    # Workspace isolation and the type check still apply.
    other, rec_other = await _get_repo(
        restarted.state.credential_store.for_workspace("other"), "gh"
    )
    assert other.error_code == "MISSING_CREDENTIAL" and rec_other.requests == []
    restarted.state.credential_store.store("default", "llm", "openai_api_key", "sk-" + "x" * 30)
    wrong, rec_wrong = await _get_repo(resolver, "llm")
    assert wrong.error_code == "MISSING_CREDENTIAL" and rec_wrong.requests == []

    # A different key fails closed instead of silently starting with an empty store.
    monkeypatch.setenv("NEXURA_MASTER_KEY", generate_master_key())
    with pytest.raises(MasterKeyMismatchError):
        create_api_app(db_path=db, profile="local-owner")


async def test_connector_credential_resolves_from_an_env_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ENV_VAR, ENV_TOKEN)
    store = ScopedCredentialStore()  # no backend: env references and in-memory entries only
    workspace = store.for_workspace("default")

    class EnvReferenceResolver:
        """Maps the connector's credential id to an env-var reference, as a flow binding does.

        The connector asks for ``CredentialReference(credential_id=ref, credential_type=...)``;
        this adds ``environment_variable`` and resolves it through the OSS workspace resolver.
        """

        store = workspace.store
        workspace_id = workspace.workspace_id

        def resolve(self, ref: CredentialReference | str) -> str | None:
            assert isinstance(ref, CredentialReference)
            return workspace.resolve(ref.model_copy(update={"environment_variable": ENV_VAR}))

    result, rec = await _get_repo(EnvReferenceResolver(), "gh-env")
    assert result.status == "success", result.error_message
    assert rec.requests[-1].headers["authorization"] == f"Bearer {ENV_TOKEN}"

    # A stored credential of that id wins over the variable; without either it is missing.
    store.store("default", "gh-env", "github_token", TOKEN)
    result, rec = await _get_repo(EnvReferenceResolver(), "gh-env")
    assert rec.requests[-1].headers["authorization"] == f"Bearer {TOKEN}"
    monkeypatch.delenv(ENV_VAR)
    missing, rec_missing = await _get_repo(EnvReferenceResolver(), "absent")
    assert missing.error_code == "MISSING_CREDENTIAL" and rec_missing.requests == []


def test_env_reference_resolves_with_the_local_encrypted_store_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NEXURA_MASTER_KEY", generate_master_key())
    monkeypatch.setenv(ENV_VAR, ENV_TOKEN)
    app = create_api_app(db_path=tmp_path / "svc.sqlite3", profile="local-owner")
    resolver = app.state.credential_store.for_workspace("default")
    ref = CredentialReference(
        credential_id="gh-env", credential_type="github_token", environment_variable=ENV_VAR
    )
    assert resolver.resolve(ref) == ENV_TOKEN
    # A reference into another workspace is refused even when the variable is set.
    foreign = ref.model_copy(update={"workspace_id": "other"})
    assert resolver.resolve(foreign) is None


async def test_local_mode_without_a_key_keeps_credentials_in_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("NEXURA_MASTER_KEY", raising=False)
    db = tmp_path / "svc.sqlite3"
    app = create_api_app(db_path=db, profile="local-owner")
    assert app.state.secret_store is None and app.state.master_key_provider is None
    async with _client(app) as c:
        made = await c.post(
            "/api/v1/credentials",
            json={"credential_id": "gh", "credential_type": "github_token", "secret": TOKEN},
        )
        assert made.status_code == 201 and TOKEN not in made.text
    result, rec = await _get_repo(app.state.credential_store.for_workspace("default"), "gh")
    assert result.status == "success", result.error_message
    assert rec.requests[-1].headers["authorization"] == f"Bearer {TOKEN}"
    assert TOKEN.encode() not in _file_bytes(db)
    # In memory only: a restarted service does not have it.
    restarted = create_api_app(db_path=db, profile="local-owner")
    assert restarted.state.credential_store.list_credentials("default") == []
