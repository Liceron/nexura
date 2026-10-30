# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Scoped credential resolution and secret redaction."""

from __future__ import annotations

import threading
from typing import Any, Literal

from nexura_agents.contracts import CredentialReference

from nexura_runtime.credentials.backends import (
    CredentialBackend,
    CredentialRecord,
    EnvironmentCredentialSource,
    mask_hint,
)
from nexura_runtime.credentials.errors import (
    CredentialAccessDeniedError,
    CredentialAlreadyExistsError,
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

    A backend whose ``shared`` attribute is true (``nexura_postgres.PostgresSecretStore``, MVP
    WP5) is shared by several service instances, so the metadata held here is re-read from it
    on every lookup: a credential created, revoked or deleted through another instance is seen
    at once. Such a backend also provides ``get_info`` and ``list_info``.
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
        self._shared = bool(getattr(secret_store, "shared", False))
        # Guards ``_entries``: a shared backend's read-through replaces entries while other
        # threads (resolvers in worker threads) read them.
        self._lock = threading.RLock()
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

    def _current(
        self, workspace_id: str, credential_id: str
    ) -> tuple[CredentialMetadata, SecretValue] | None:
        """The credential's entry: re-read from a shared backend (another instance may have
        changed it), else the one held here. ``None`` when there is none."""
        key = (workspace_id, credential_id)
        if not self._shared:
            with self._lock:
                return self._entries.get(key)
        from nexura_runtime.secrets.errors import SecretNotFoundError

        backend: Any = self._backend
        try:
            info = backend.get_info(workspace_id, credential_id)
        except SecretNotFoundError:
            with self._lock:
                self._entries.pop(key, None)
            return None
        entry = (self._meta_from_info(info), SecretValue(""))
        with self._lock:
            self._entries[key] = entry
        return entry

    def _workspace_entries(
        self, workspace_id: str
    ) -> list[tuple[str, tuple[CredentialMetadata, SecretValue]]]:
        """``(name, entry)`` for every credential of a workspace, re-read from a shared backend.
        The map is changed in one step under the lock, so a concurrent lookup never sees the
        workspace half refreshed."""
        if self._shared:
            backend: Any = self._backend
            fresh = [
                (info.name, (self._meta_from_info(info), SecretValue("")))
                for info in backend.list_info(workspace_id)
            ]
            with self._lock:
                for key in [key for key in self._entries if key[0] == workspace_id]:
                    del self._entries[key]
                for name, entry in fresh:
                    self._entries[(workspace_id, name)] = entry
            return fresh
        with self._lock:
            return [
                (name, entry) for (ws, name), entry in self._entries.items() if ws == workspace_id
            ]

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
        mode: Literal["upsert", "create", "replace"] = "upsert",
    ) -> CredentialMetadata:
        """Store a secret under an isolated workspace namespace.

        ``mode="create"`` refuses an existing credential (``CredentialAlreadyExistsError``) and
        ``mode="replace"`` a missing one (``CredentialNotFoundError``), atomically in the
        backend, so two instances creating one name cannot both succeed. A replace never
        lowers a ``revoked`` status.
        """
        key = (workspace_id, credential_id)
        if self._backend is not None:
            from nexura_runtime.secrets.errors import SecretExistsError, SecretNotFoundError

            extra: dict[str, Any] = {} if mode == "upsert" else {"mode": mode}
            try:
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
                    **extra,
                )
            except SecretExistsError:
                self._current(workspace_id, credential_id)
                raise CredentialAlreadyExistsError(credential_id, workspace_id) from None
            except SecretNotFoundError:
                with self._lock:
                    self._entries.pop(key, None)
                raise CredentialNotFoundError(credential_id, workspace_id) from None
            meta = self._meta_from_info(info)
            with self._lock:
                self._entries[key] = (meta, SecretValue(""))
            return meta
        with self._lock:
            prior = self._entries.get(key)
            if prior is not None and mode == "create":
                raise CredentialAlreadyExistsError(credential_id, workspace_id)
            if prior is None and mode == "replace":
                raise CredentialNotFoundError(credential_id, workspace_id)
            if prior is not None and prior[0].status == "revoked":
                status = "revoked"  # as the backends: only ``revoke`` changes it
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
            self._entries[key] = (meta, SecretValue(secret))
        return meta

    def get_metadata(self, workspace_id: str, credential_id: str) -> CredentialMetadata:
        """Retrieve metadata for a credential without revealing secret."""
        entry = self._current(workspace_id, credential_id)
        if entry is None:
            raise CredentialNotFoundError(credential_id, workspace_id)
        return entry[0]

    def _redaction_values(self, workspace_id: str) -> list[str]:
        """Known workspace values for review redaction, including revoked credentials.

        This does not authorise outbound use. A storage/decryption failure propagates so
        a caller cannot accidentally return an unsanitised approval view.
        """
        values = []
        for name, (meta, held) in self._workspace_entries(workspace_id):
            values.append(self._reveal(workspace_id, name, held).reveal())
            if meta.environment_variable:
                values.append(self._environment.get(meta.environment_variable) or "")
        return [value for value in values if value]

    def resolve(self, workspace_id: str, credential_id: str) -> SecretValue:
        """Resolve a secret for the specified workspace.

        Raises:
            CredentialNotFoundError: If credential does not exist in workspace.
            CredentialAccessDeniedError: If credential has been revoked.
        """
        entry = self._current(workspace_id, credential_id)
        if entry is None:
            raise CredentialNotFoundError(credential_id, workspace_id)
        meta, secret = entry
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
        entry = self._current(context_workspace_id, ref.credential_id)
        if entry is not None:
            meta, secret = entry
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
        entry = self._current(workspace_id, credential_id)
        if entry is None:
            raise CredentialNotFoundError(credential_id, workspace_id)
        meta, _ = entry
        if self._backend is not None:
            self._backend.set_status(workspace_id, credential_id, "revoked")
        meta.status = "revoked"
        return meta

    def mark_verified(
        self, workspace_id: str, credential_id: str, timestamp: float | None = None
    ) -> CredentialMetadata:
        """Update last_verified_at timestamp."""
        import time

        entry = self._current(workspace_id, credential_id)
        if entry is None:
            raise CredentialNotFoundError(credential_id, workspace_id)
        meta, _ = entry
        meta.last_verified_at = timestamp or time.time()
        if self._backend is not None:
            self._backend.mark_verified(workspace_id, credential_id, meta.last_verified_at)
        return meta

    def delete(self, workspace_id: str, credential_id: str) -> bool:
        """Remove a credential from storage."""
        if self._current(workspace_id, credential_id) is None:
            return False
        with self._lock:
            self._entries.pop((workspace_id, credential_id), None)
        if self._backend is not None:
            self._backend.delete(workspace_id, credential_id)
        return True

    def list_credentials(self, workspace_id: str) -> list[CredentialMetadata]:
        """List metadata for all credentials belonging to a workspace."""
        return [meta for _, (meta, _) in self._workspace_entries(workspace_id)]

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
