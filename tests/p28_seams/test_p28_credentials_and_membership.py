"""P28 step 2: the ``CredentialBackend`` and ``MembershipStore`` seams."""

from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path

import pytest
from nexura_agents.contracts import CredentialReference

from nexura_runtime.credentials import ScopedCredentialStore
from nexura_runtime.credentials.backends import (
    CredentialBackend,
    EnvironmentCredentialSource,
    encrypted_credential_backend,
    mask_hint,
)
from nexura_runtime.credentials.errors import CredentialAccessDeniedError, CredentialNotFoundError
from nexura_runtime.secrets import (
    EncryptedSecretStore,
    StaticMasterKeyProvider,
    generate_master_key,
)
from nexura_runtime.secrets import store as secrets_store
from nexura_runtime.service.extensions import Identity
from nexura_runtime.service.workspaces.membership import MembershipStore

# ---------------------------------------------------------------------------------------
# CredentialBackend
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Record:
    workspace_id: str
    name: str
    credential_type: str
    provider: str
    description: str
    environment_variable: str | None
    status: str
    masked_hint: str
    created_at: float
    last_verified_at: float | None


class DictBackend:
    """A minimal CredentialBackend that records every call."""

    def __init__(self) -> None:
        self.secrets: dict[tuple[str, str], str] = {}
        self.records: dict[tuple[str, str], _Record] = {}
        self.calls: list[str] = []

    def list_all_info(self) -> list[_Record]:
        self.calls.append("list_all_info")
        return list(self.records.values())

    def put(
        self,
        workspace_id: str,
        name: str,
        secret: str,
        *,
        credential_type: str = "api_key",
        provider: str = "custom",
        description: str = "",
        environment_variable: str | None = None,
        status: str = "active",
        created_by: str = "",
    ) -> _Record:
        self.calls.append("put")
        self.secrets[(workspace_id, name)] = secret
        record = _Record(
            workspace_id,
            name,
            credential_type,
            provider,
            description,
            environment_variable,
            status,
            mask_hint(secret),
            time.time(),
            None,
        )
        self.records[(workspace_id, name)] = record
        return record

    def resolve(self, workspace_id: str, name: str) -> str:
        self.calls.append("resolve")
        return self.secrets[(workspace_id, name)]

    def set_status(self, workspace_id: str, name: str, status: str) -> _Record:
        self.calls.append("set_status")
        record = replace(self.records[(workspace_id, name)], status=status)
        self.records[(workspace_id, name)] = record
        return record

    def mark_verified(self, workspace_id: str, name: str, at: float) -> _Record:
        self.calls.append("mark_verified")
        record = replace(self.records[(workspace_id, name)], last_verified_at=at)
        self.records[(workspace_id, name)] = record
        return record

    def delete(self, workspace_id: str, name: str) -> bool:
        self.calls.append("delete")
        self.records.pop((workspace_id, name), None)
        return self.secrets.pop((workspace_id, name), None) is not None


SECRET = "sk-test-0123456789abcdefWXYZ"


def test_encrypted_store_is_a_credential_backend(tmp_path: Path) -> None:
    provider = StaticMasterKeyProvider.from_text(generate_master_key())
    backend = encrypted_credential_backend(provider, tmp_path / "secrets.sqlite3")
    assert isinstance(backend, EncryptedSecretStore)
    assert isinstance(backend, CredentialBackend)

    store = ScopedCredentialStore(backend)
    meta = store.store("ws", "openai", "api_key", SECRET)
    assert meta.masked_hint == mask_hint(SECRET)
    assert store.resolve("ws", "openai").reveal() == SECRET
    assert SECRET.encode() not in (tmp_path / "secrets.sqlite3").read_bytes()
    # A new store over the same backend sees the credential (metadata loaded at start).
    again = ScopedCredentialStore(
        encrypted_credential_backend(provider, tmp_path / "secrets.sqlite3")
    )
    assert again.resolve("ws", "openai").reveal() == SECRET


def test_scoped_store_uses_any_backend_and_keeps_isolation() -> None:
    backend = DictBackend()
    assert isinstance(backend, CredentialBackend)
    store = ScopedCredentialStore(backend)
    store.store("ws-a", "token", "api_key", SECRET)
    assert store.resolve("ws-a", "token").reveal() == SECRET
    with pytest.raises(CredentialNotFoundError):
        store.resolve("ws-b", "token")
    with pytest.raises(CredentialAccessDeniedError):
        store.resolve_ref(
            "ws-b",
            CredentialReference(
                credential_id="token", credential_type="api_key", workspace_id="ws-a"
            ),
        )
    store.mark_verified("ws-a", "token", 123.0)
    store.revoke("ws-a", "token")
    with pytest.raises(CredentialAccessDeniedError):
        store.resolve("ws-a", "token")
    assert store.delete("ws-a", "token") is True
    assert backend.calls == [
        "list_all_info",
        "put",
        "resolve",
        "mark_verified",
        "set_status",
        "delete",
    ]

    # Only metadata is kept in memory; the value lives in the backend.
    backend.put("ws-a", "kept", SECRET)
    reloaded = ScopedCredentialStore(backend)
    assert [m.credential_id for m in reloaded.list_credentials("ws-a")] == ["kept"]
    assert SECRET not in repr(reloaded._entries)


def test_environment_source_is_the_reference_fallback() -> None:
    store = ScopedCredentialStore(
        environment=EnvironmentCredentialSource({"P28_TOKEN": "from-env", "P28_EMPTY": ""})
    )
    ref = CredentialReference(
        credential_id="tok", credential_type="api_key", environment_variable="P28_TOKEN"
    )
    assert store.resolve_ref("default", ref).reveal() == "from-env"
    empty = CredentialReference(
        credential_id="tok", credential_type="api_key", environment_variable="P28_EMPTY"
    )
    with pytest.raises(CredentialNotFoundError):
        store.resolve_ref("default", empty)


def test_default_environment_source_reads_os_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("P28_OS_TOKEN", "os-value")
    ref = CredentialReference(
        credential_id="tok", credential_type="api_key", environment_variable="P28_OS_TOKEN"
    )
    assert ScopedCredentialStore().resolve_ref("default", ref).reveal() == "os-value"


def test_mask_hint_is_shared() -> None:
    assert secrets_store.mask_hint is mask_hint
    assert mask_hint("short") == "••••••••"
    assert mask_hint(SECRET) == "••••" + SECRET[-4:]


def test_credentials_package_no_longer_imports_the_secrets_store() -> None:
    code = (
        "import sys, nexura_runtime.credentials.store\n"
        "assert 'nexura_runtime.secrets' not in sys.modules, sorted(sys.modules)\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr[-2000:]


# ---------------------------------------------------------------------------------------
# MembershipStore
# ---------------------------------------------------------------------------------------


def test_workspace_repository_is_the_read_only_local_membership_store() -> None:
    from nexura_runtime.service.workspaces.membership import MembershipStore
    from nexura_runtime.service.workspaces.repository import WorkspaceRepository

    repo = WorkspaceRepository()
    assert isinstance(repo, MembershipStore)
    for wid in ("ws-1", "ws-2"):
        repo.create_workspace(wid, wid, "local-owner")
        member = repo.get_member(wid, "local-owner")
        assert member is not None and member.role == "admin"
        assert repo.get_member(wid, "forged-other-user") is None
        assert not repo.is_removed_member(wid, "local-owner")
        assert wid in {w.workspace_id for w in repo.list_workspaces_for_principal("local-owner")}
        assert repo.list_workspaces_for_principal("forged-other-user") == []
    for method in ("add_member", "remove_member", "update_member_role"):
        assert not hasattr(repo, method)


class MembershipReader:
    """Test-only consumer of the OSS read-side protocol, no member writes."""

    def __init__(self, membership: MembershipStore | None = None) -> None:
        from nexura_runtime.service.workspaces.repository import WorkspaceRepository

        self.membership = membership if membership is not None else WorkspaceRepository()

    def role(self, workspace: str, principal: str) -> str | None:
        member = self.membership.get_member(workspace, principal)
        return member.role if member else None


def test_test_provider_reads_membership_through_the_protocol() -> None:
    from _local_env import TestAuthProvider
    from starlette.requests import Request

    from nexura_runtime.service.workspaces.repository import WorkspaceRepository

    class ReadingProvider(TestAuthProvider):
        def __init__(self, membership: MembershipStore) -> None:
            self.reader = MembershipReader(membership)

        def authenticate(self, request: Request, target_workspace_id: str | None) -> Identity:
            identity = super().authenticate(request, target_workspace_id)
            return Identity(
                identity.principal,
                identity.workspace_id,
                self.reader.role(identity.workspace_id, identity.principal.principal_id),
            )

    repo = WorkspaceRepository()
    repo.create_workspace("ws-1", "Test local project", "local-owner")
    request = Request({"type": "http", "headers": [(b"x-test-user", b"local-owner")]})
    identity = ReadingProvider(repo).authenticate(request, "ws-1")
    assert (identity.principal.principal_id, identity.workspace_id, identity.role) == (
        "local-owner",
        "ws-1",
        "admin",
    )
    assert MembershipReader(repo).role("ws-1", "forged") is None
