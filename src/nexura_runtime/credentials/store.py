# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Scoped credential resolution and secret redaction."""

from __future__ import annotations

from nexura_agents.contracts import CredentialReference

from nexura_runtime.credentials.backends import (
    CredentialBackend,
    CredentialRecord,
    EnvironmentCredentialSource,
    mask_hint,
)
from nexura_runtime.credentials.errors import (
    CredentialAccessDeniedError,
    CredentialNotFoundError,
)
from nexura_runtime.credentials.models import CredentialMetadata, SecretValue


def redact_secrets(text: str, secrets: list[str]) -> str:
    """Replace all known secret occurrences in text with [REDACTED]."""
    result = text
    for secret in secrets:
        if secret and len(secret) >= 4:
            result = result.replace(secret, "[REDACTED]")
    return result


class ScopedCredentialStore:
    """In-memory workspace-scoped credential store enforcing strict isolation.

    Guarantees:
    - Never exposes secret values in metadata or string representations.
    - Prevents cross-workspace credential access.
    - Resolves environment variable fallbacks if permitted.

    ``secret_store`` is an optional ``CredentialBackend`` (for example the encrypted
    ``EncryptedSecretStore``); without one, secrets are held in memory. ``environment`` is the
    source of environment-variable fallbacks (``os.environ`` by default).
    """

    def __init__(
        self,
        secret_store: CredentialBackend | None = None,
        *,
        environment: EnvironmentCredentialSource | None = None,
    ) -> None:
        # Key: (workspace_id, credential_id) -> (CredentialMetadata, SecretValue)
        self._entries: dict[tuple[str, str], tuple[CredentialMetadata, SecretValue]] = {}
        # Optional backend: when set, plaintext lives only in the backend (encrypted at rest for
        # EncryptedSecretStore) and the in-memory entries hold metadata with an empty placeholder.
        self._backend = secret_store
        self._environment = environment or EnvironmentCredentialSource()
        if secret_store is not None:
            for info in secret_store.list_all_info():
                self._entries[(info.workspace_id, info.name)] = (
                    self._meta_from_info(info),
                    SecretValue(""),
                )

    @staticmethod
    def _meta_from_info(info: CredentialRecord) -> CredentialMetadata:
        return CredentialMetadata(
            credential_id=info.name,
            workspace_id=info.workspace_id,
            credential_type=info.credential_type,
            description=info.description,
            created_at=info.created_at,
            environment_variable=info.environment_variable,
            provider=info.provider,
            status=info.status,
            last_verified_at=info.last_verified_at,
            masked_hint=info.masked_hint,
        )

    def _reveal(self, workspace_id: str, credential_id: str, held: SecretValue) -> SecretValue:
        if self._backend is None:
            return held
        return SecretValue(self._backend.resolve(workspace_id, credential_id))

    def store(
        self,
        workspace_id: str,
        credential_id: str,
        credential_type: str,
        secret: str,
        *,
        description: str = "",
        environment_variable: str | None = None,
        provider: str = "custom",
        status: str = "active",
        created_by: str = "",
    ) -> CredentialMetadata:
        """Store a secret under an isolated workspace namespace."""
        if self._backend is not None:
            info = self._backend.put(
                workspace_id,
                credential_id,
                secret,
                credential_type=credential_type,
                provider=provider,
                description=description,
                environment_variable=environment_variable,
                status=status,
                created_by=created_by,
            )
            meta = self._meta_from_info(info)
            self._entries[(workspace_id, credential_id)] = (meta, SecretValue(""))
            return meta
        meta = CredentialMetadata(
            credential_id=credential_id,
            workspace_id=workspace_id,
            credential_type=credential_type,
            description=description,
            environment_variable=environment_variable,
            provider=provider,
            status=status,
            masked_hint=mask_hint(secret),
        )
        self._entries[(workspace_id, credential_id)] = (meta, SecretValue(secret))
        return meta

    def get_metadata(self, workspace_id: str, credential_id: str) -> CredentialMetadata:
        """Retrieve metadata for a credential without revealing secret."""
        key = (workspace_id, credential_id)
        if key not in self._entries:
            raise CredentialNotFoundError(credential_id, workspace_id)
        return self._entries[key][0]

    def _redaction_values(self, workspace_id: str) -> list[str]:
        """Known workspace values for review redaction, including revoked credentials.

        This does not authorise outbound use. A storage/decryption failure propagates so
        a caller cannot accidentally return an unsanitised approval view.
        """
        values = []
        for (ws, name), (meta, held) in self._entries.items():
            if ws != workspace_id:
                continue
            values.append(self._reveal(ws, name, held).reveal())
            if meta.environment_variable:
                values.append(self._environment.get(meta.environment_variable) or "")
        return [value for value in values if value]

    def resolve(self, workspace_id: str, credential_id: str) -> SecretValue:
        """Resolve a secret for the specified workspace.

        Raises:
            CredentialNotFoundError: If credential does not exist in workspace.
            CredentialAccessDeniedError: If credential has been revoked.
        """
        key = (workspace_id, credential_id)
        if key not in self._entries:
            raise CredentialNotFoundError(credential_id, workspace_id)
        meta, secret = self._entries[key]
        if meta.status == "revoked":
            raise CredentialAccessDeniedError(f"Credential '{credential_id}' has been revoked.")
        return self._reveal(workspace_id, credential_id, secret)

    def resolve_ref(
        self,
        context_workspace_id: str,
        ref: CredentialReference,
    ) -> SecretValue:
        """Resolve a CredentialReference within the context workspace boundary.

        Raises:
            CredentialAccessDeniedError: On cross-workspace violation or revoked credential.
            CredentialNotFoundError: If secret is absent.
        """
        # If reference explicitly specifies a foreign workspace, reject
        target_ws = getattr(ref, "workspace_id", None) or context_workspace_id
        if target_ws != context_workspace_id:
            raise CredentialAccessDeniedError(
                f"Cross-workspace credential access denied: context '{context_workspace_id}' "
                f"cannot access credential in '{target_ws}'.",
                details={
                    "context_workspace": context_workspace_id,
                    "target_workspace": target_ws,
                    "credential_id": ref.credential_id,
                },
            )

        # 1. Check scoped store
        key = (context_workspace_id, ref.credential_id)
        if key in self._entries:
            meta, secret = self._entries[key]
            if meta.status == "revoked":
                raise CredentialAccessDeniedError(
                    f"Credential '{ref.credential_id}' has been revoked."
                )
            return self._reveal(context_workspace_id, ref.credential_id, secret)

        # 2. Check environment fallback if declared on ref
        if ref.environment_variable:
            env_val = self._environment.get(ref.environment_variable)
            if env_val:
                return SecretValue(env_val)

        raise CredentialNotFoundError(ref.credential_id, context_workspace_id)

    def revoke(self, workspace_id: str, credential_id: str) -> CredentialMetadata:
        """Mark a credential as revoked."""
        key = (workspace_id, credential_id)
        if key not in self._entries:
            raise CredentialNotFoundError(credential_id, workspace_id)
        meta, _ = self._entries[key]
        if self._backend is not None:
            self._backend.set_status(workspace_id, credential_id, "revoked")
        meta.status = "revoked"
        return meta

    def mark_verified(
        self, workspace_id: str, credential_id: str, timestamp: float | None = None
    ) -> CredentialMetadata:
        """Update last_verified_at timestamp."""
        import time

        key = (workspace_id, credential_id)
        if key not in self._entries:
            raise CredentialNotFoundError(credential_id, workspace_id)
        meta, _ = self._entries[key]
        meta.last_verified_at = timestamp or time.time()
        if self._backend is not None:
            self._backend.mark_verified(workspace_id, credential_id, meta.last_verified_at)
        return meta

    def delete(self, workspace_id: str, credential_id: str) -> bool:
        """Remove a credential from storage."""
        key = (workspace_id, credential_id)
        if key in self._entries:
            del self._entries[key]
            if self._backend is not None:
                self._backend.delete(workspace_id, credential_id)
            return True
        return False

    def list_credentials(self, workspace_id: str) -> list[CredentialMetadata]:
        """List metadata for all credentials belonging to a workspace."""
        return [meta for (ws, _), (meta, _) in self._entries.items() if ws == workspace_id]

    def for_workspace(self, workspace_id: str) -> WorkspaceCredentialResolver:
        """Create a workspace-bound adapter implementing the CredentialResolver protocol."""
        return WorkspaceCredentialResolver(self, workspace_id)


class WorkspaceCredentialResolver:
    """Workspace-bound adapter implementing the CredentialResolver protocol."""

    def __init__(self, store: ScopedCredentialStore, workspace_id: str) -> None:
        self.store = store
        self.workspace_id = workspace_id

    def resolve(self, ref: CredentialReference | str) -> str | None:
        """Resolve a secret token or key by reference."""
        try:
            if isinstance(ref, str):
                return self.store.resolve(self.workspace_id, ref).reveal()
            return self.store.resolve_ref(self.workspace_id, ref).reveal()
        except Exception:
            return None
