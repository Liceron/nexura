# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""``CredentialBackend``: where ``ScopedCredentialStore`` keeps secrets at rest, as a protocol.

``ScopedCredentialStore`` always enforces workspace isolation, revocation and redaction. The
backend only stores and returns values:

* With no backend, secrets live in process memory, as before.
* ``EnvironmentCredentialSource`` is the environment-variable fallback used for credential
  references that name an ``environment_variable``.
* ``nexura_runtime.secrets.EncryptedSecretStore`` (envelope encryption at rest) satisfies
  ``CredentialBackend`` structurally; ``encrypted_credential_backend`` builds one.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from nexura_runtime.secrets.keys import MasterKeyProvider


def mask_hint(secret: str) -> str:
    """Return a non-reversible display hint: bullets plus the last 4 chars of long secrets."""
    if len(secret) >= 16:
        return "••••" + secret[-4:]
    return "••••••••"


class CredentialRecord(Protocol):
    """Non-secret description of a stored credential, as returned by a backend."""

    @property
    def workspace_id(self) -> str: ...
    @property
    def name(self) -> str: ...
    @property
    def credential_type(self) -> str: ...
    @property
    def provider(self) -> str: ...
    @property
    def description(self) -> str: ...
    @property
    def environment_variable(self) -> str | None: ...
    @property
    def status(self) -> str: ...
    @property
    def masked_hint(self) -> str: ...
    @property
    def created_at(self) -> float: ...
    @property
    def last_verified_at(self) -> float | None: ...


@runtime_checkable
class CredentialBackend(Protocol):
    """Persistent secret storage behind ``ScopedCredentialStore``."""

    def list_all_info(self) -> Sequence[CredentialRecord]:
        """Every stored credential of every workspace (no secret values)."""
        ...

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
    ) -> CredentialRecord:
        """Create or replace a credential and return its description."""
        ...

    def resolve(self, workspace_id: str, name: str) -> str:
        """Return the plaintext secret."""
        ...

    def set_status(self, workspace_id: str, name: str, status: str) -> CredentialRecord:
        """Change the status (for example ``revoked``)."""
        ...

    def mark_verified(self, workspace_id: str, name: str, at: float) -> CredentialRecord:
        """Record when the credential was last verified."""
        ...

    def delete(self, workspace_id: str, name: str) -> bool:
        """Delete the credential; True when it existed."""
        ...


class EnvironmentCredentialSource:
    """Reads secrets from environment variables (``os.environ`` unless a mapping is given)."""

    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        self._environ = environ

    def get(self, variable: str) -> str | None:
        """The non-empty value of ``variable``, else ``None``."""
        environ = os.environ if self._environ is None else self._environ
        value = environ.get(variable)
        return value or None


def encrypted_credential_backend(
    provider: MasterKeyProvider, db_path: str | Path | None = None
) -> CredentialBackend:
    """Build the encrypted at-rest backend (``EncryptedSecretStore``)."""
    from nexura_runtime.secrets.store import EncryptedSecretStore

    return EncryptedSecretStore(provider, db_path)


__all__ = [
    "CredentialBackend",
    "CredentialRecord",
    "EnvironmentCredentialSource",
    "encrypted_credential_backend",
    "mask_hint",
]
