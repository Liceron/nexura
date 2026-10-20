"""P9: the local encrypted secrets file (envelope encryption, master key) of the OSS edition.

These tests were in ``tests/test_orgs_and_secrets.py``. Organisations, invites, the
``/api/v1/secrets`` routes and master-key rotation moved to the closed edition in EDITIONS_PLAN
step 5, with their tests (``enterprise/tests/test_orgs_and_secrets.py``); the store, the master
key rules and the shared-profile key requirement stay here, as does the code they test.
"""

from __future__ import annotations

import builtins
import sqlite3
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from _local_env import TestAuthProvider, local_headers
from httpx import ASGITransport

from nexura_runtime.secrets import (
    EncryptedSecretStore,
    MasterKeyInvalidError,
    MasterKeyMismatchError,
    MasterKeyMissingError,
    SecretNotFoundError,
    SecretsUnavailableError,
    StaticMasterKeyProvider,
    generate_master_key,
    master_key_provider_from_env,
)
from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.service.workspaces import (
    WorkspaceRepository,
)

SENTINEL = "sk-live-SENTINEL-0123456789abcdefXYZ"


def _provider() -> StaticMasterKeyProvider:
    return StaticMasterKeyProvider.from_text(generate_master_key())


def _file_bytes(path: Path) -> bytes:
    data = b""
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(path) + suffix)
        if p.exists():
            data += p.read_bytes()
    return data


def test_secret_roundtrip_is_encrypted_at_rest_and_masked(tmp_path: Path) -> None:
    db = tmp_path / "s.sqlite3"
    store = EncryptedSecretStore(_provider(), db)
    info = store.put("ws", "openai", SENTINEL, provider="openai")
    assert SENTINEL not in repr(info)
    assert info.masked_hint == "\u2022\u2022\u2022\u2022" + SENTINEL[-4:]
    assert SENTINEL not in info.masked_hint
    assert store.resolve("ws", "openai") == SENTINEL
    assert SENTINEL.encode() not in _file_bytes(db)
    assert (db.stat().st_mode & 0o777) == 0o600
    with pytest.raises(SecretNotFoundError):
        store.resolve("other-ws", "openai")


def test_each_secret_has_its_own_data_key(tmp_path: Path) -> None:
    store = EncryptedSecretStore(_provider(), tmp_path / "s.sqlite3")
    store.put("ws", "a", "same-value-same-value")
    store.put("ws", "b", "same-value-same-value")
    rows = (
        sqlite3.connect(tmp_path / "s.sqlite3")
        .execute("SELECT ciphertext, wrapped_key FROM nexura_secrets")
        .fetchall()
    )
    assert rows[0][0] != rows[1][0]
    assert rows[0][1] != rows[1][1]


def test_ciphertext_is_bound_to_workspace_and_name(tmp_path: Path) -> None:
    db = tmp_path / "s.sqlite3"
    store = EncryptedSecretStore(_provider(), db)
    store.put("ws-a", "k", SENTINEL)
    store.put("ws-b", "k", "other-value-other-value")
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE nexura_secrets SET ciphertext=(SELECT ciphertext FROM nexura_secrets "
        "WHERE workspace_id='ws-a'), nonce=(SELECT nonce FROM nexura_secrets WHERE "
        "workspace_id='ws-a'), wrapped_key=(SELECT wrapped_key FROM nexura_secrets WHERE "
        "workspace_id='ws-a') WHERE workspace_id='ws-b'"
    )
    conn.commit()
    with pytest.raises(MasterKeyMismatchError):
        store.resolve("ws-b", "k")


def test_wrong_master_key_fails_closed(tmp_path: Path) -> None:
    db = tmp_path / "s.sqlite3"
    EncryptedSecretStore(_provider(), db).put("ws", "k", SENTINEL)
    with pytest.raises(MasterKeyMismatchError) as exc:
        EncryptedSecretStore(_provider(), db)
    assert SENTINEL not in str(exc.value)


def test_master_key_from_env_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    assert master_key_provider_from_env({}) is None
    with pytest.raises(MasterKeyMissingError):
        master_key_provider_from_env({}, required=True)
    with pytest.raises(MasterKeyInvalidError):
        master_key_provider_from_env({"NEXURA_MASTER_KEY": "not-a-key"})
    good = generate_master_key()
    prov = master_key_provider_from_env({"NEXURA_MASTER_KEY": good})
    assert prov is not None and good not in repr(prov)


def test_missing_cryptography_gives_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    real_import = builtins.__import__

    def fake(name: str, *a: Any, **k: Any) -> Any:
        if name.startswith("cryptography"):
            raise ImportError("blocked")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake)
    with pytest.raises(SecretsUnavailableError, match="cryptography"):
        master_key_provider_from_env({"NEXURA_MASTER_KEY": generate_master_key()})


def test_shared_mode_refuses_to_start_without_master_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("NEXURA_MASTER_KEY", raising=False)
    monkeypatch.delenv("NEXURA_INSECURE_ALLOW_PLAINTEXT_SECRETS", raising=False)
    with pytest.raises(MasterKeyMissingError):
        create_api_app(
            db_path=tmp_path / "s.sqlite3",
            profile="authenticated-shared-service",
            auth_provider=TestAuthProvider(),
        )


def test_shared_mode_explicit_insecure_opt_out_keeps_memory_only_store(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("NEXURA_MASTER_KEY", raising=False)
    monkeypatch.setenv("NEXURA_INSECURE_ALLOW_PLAINTEXT_SECRETS", "yes")
    with pytest.raises(MasterKeyMissingError):  # a vague value is not an opt-out
        create_api_app(
            db_path=tmp_path / "s.sqlite3",
            profile="authenticated-shared-service",
            auth_provider=TestAuthProvider(),
        )
    monkeypatch.setenv("NEXURA_INSECURE_ALLOW_PLAINTEXT_SECRETS", "I_UNDERSTAND_THIS_IS_UNSAFE")
    app = create_api_app(
        db_path=tmp_path / "s.sqlite3",
        profile="authenticated-shared-service",
        auth_provider=TestAuthProvider(),
    )
    assert app.state.secret_store is None


def test_master_key_text_is_strictly_decoded() -> None:
    good = generate_master_key()
    assert StaticMasterKeyProvider.from_text(good).key_id
    assert StaticMasterKeyProvider.from_text(good.rstrip("=")).key_id
    std = good[:10] + "+/" + good[12:]
    bad = [
        good[:10] + "!!@@" + good[10:],
        good[:20] + " " + good[20:],
        good[:20] + "\n" + good[20:],
        good[:-5],
        good + "AAAA",
        "",
        std,
        good[:10] + "é" + good[11:],
    ]
    for text in bad:
        with pytest.raises(MasterKeyInvalidError) as exc:
            StaticMasterKeyProvider.from_text(text)
        assert text not in str(exc.value) or text == ""


async def test_ga_credential_ids_are_workspace_scoped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    monkeypatch.delenv("NEXURA_INSECURE_ALLOW_PLAINTEXT_SECRETS", raising=False)
    monkeypatch.setenv("NEXURA_MASTER_KEY", generate_master_key())
    repo = WorkspaceRepository()
    for who in ("alice", "bob"):
        repo.create_workspace(f"home-{who}", who, who)
    app = create_api_app(
        db_path=tmp_path / "ga.sqlite3",
        workspace_repo=repo,
        profile="local-owner",
    )

    def tok(who: str) -> dict[str, str]:
        t = f"home-{who}"
        return local_headers(t)

    body = {"credential_id": "prod-stripe", "credential_type": "api_key", "secret": "y" * 20}
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        assert (
            await c.post("/api/v1/credentials", json=body, headers=tok("alice"))
        ).status_code == 201
        assert (
            await c.post("/api/v1/credentials", json=body, headers=tok("bob"))
        ).status_code == 201
        for who in ("alice", "bob"):
            g = await c.get("/api/v1/credentials/prod-stripe", headers=tok(who))
            assert g.status_code == 200 and g.json()["workspace_id"] == f"home-{who}"
        dup = await c.post("/api/v1/credentials", json=body, headers=tok("alice"))
        assert dup.status_code == 409  # same-workspace duplicate only


def test_put_replace_keeps_created_at_and_created_by() -> None:
    store = EncryptedSecretStore(StaticMasterKeyProvider.from_text(generate_master_key()))
    a = store.put("w", "k", "v1-value-value", created_by="alice")
    time.sleep(0.02)
    b = store.put("w", "k", "v2-value-value", created_by="bob")
    assert b.created_at == a.created_at and b.created_by == "alice"
    assert b.updated_at > a.updated_at
    assert store.resolve("w", "k") == "v2-value-value"
