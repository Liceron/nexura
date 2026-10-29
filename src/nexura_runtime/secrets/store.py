# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Envelope-encrypted secret storage: one data key per secret, wrapped by a master key.

This is the open-source local encrypted secrets file. Master-key rotation, organisations and the
hosted secrets admin API belong to the closed edition (EDITIONS_PLAN step 5).
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from nexura_runtime.credentials.backends import mask_hint
from nexura_runtime.secrets.errors import (
    MasterKeyMismatchError,
    SecretExistsError,
    SecretNotFoundError,
)
from nexura_runtime.secrets.keys import MasterKeyProvider, load_aesgcm


@dataclass(frozen=True)
class StoredSecretInfo:
    """Non-secret description of a stored secret (what APIs may return)."""

    workspace_id: str
    name: str
    credential_type: str
    provider: str
    description: str
    environment_variable: str | None
    status: str
    masked_hint: str
    created_at: float
    updated_at: float
    last_verified_at: float | None
    created_by: str


_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS nexura_secrets (
        workspace_id TEXT NOT NULL, name TEXT NOT NULL,
        ciphertext BLOB NOT NULL, nonce BLOB NOT NULL, wrapped_key BLOB NOT NULL,
        key_id TEXT NOT NULL, credential_type TEXT NOT NULL, provider TEXT NOT NULL,
        description TEXT NOT NULL, environment_variable TEXT, status TEXT NOT NULL,
        masked_hint TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
        last_verified_at REAL, created_by TEXT NOT NULL,
        PRIMARY KEY (workspace_id, name))""",
)
#: How ``put`` treats an existing name: ``upsert`` replaces it (or creates the secret),
#: ``create`` refuses it (``SecretExistsError``), ``replace`` requires it
#: (``SecretNotFoundError``).
PutMode = Literal["upsert", "create", "replace"]
#: A revoked secret stays revoked when its value is replaced: only ``set_status`` changes it.
REVOKED = "revoked"
_COLS = (
    "workspace_id, name, credential_type, provider, description, environment_variable, status, "
    "masked_hint, created_at, updated_at, last_verified_at, created_by"
)


def _aad(workspace_id: str, name: str) -> bytes:
    return f"nexura-secret|{workspace_id}|{name}".encode()


class EncryptedSecretStore:
    """Workspace-scoped secrets encrypted at rest (AES-256-GCM envelope encryption).

    Each secret gets a random data key; the data key is wrapped by the master key provider
    and both ciphertexts are bound to ``workspace_id`` and ``name`` (AAD) so rows cannot be
    swapped between names or workspaces. Plaintext is returned only by :meth:`resolve`.
    Pass ``db_path=None`` for an in-memory store.
    """

    def __init__(self, provider: MasterKeyProvider, db_path: str | Path | None = None) -> None:
        self._provider = provider
        self._lock = threading.RLock()
        self._path = str(db_path) if db_path is not None else None
        self._mem: sqlite3.Connection | None
        if self._path is None:
            self._mem = sqlite3.connect(":memory:", check_same_thread=False)
        else:
            self._mem = None
            if not Path(self._path).exists():
                Path(self._path).parent.mkdir(parents=True, exist_ok=True)
                os.close(os.open(self._path, os.O_WRONLY | os.O_CREAT, 0o600))
        with self._lock, self._conn() as conn:
            for ddl in _SCHEMA:
                conn.execute(ddl)
        load_aesgcm()
        self.verify_master_key()

    def _conn(self) -> sqlite3.Connection:
        if self._mem is not None:
            return self._mem
        assert self._path is not None
        return sqlite3.connect(self._path, check_same_thread=False, timeout=10.0)

    def _run(self, sql: str, args: tuple[object, ...] = ()) -> list[tuple[Any, ...]]:
        with self._lock:
            conn = self._conn()
            try:
                with conn:
                    rows = conn.execute(sql, args).fetchall()
                return [tuple(r) for r in rows]
            finally:
                if self._mem is None:
                    conn.close()

    @property
    def key_id(self) -> str:
        """Identifier of the master key currently in use."""
        return self._provider.key_id

    def verify_master_key(self) -> None:
        """Fail closed unless the provider can unwrap a stored secret (no-op when empty)."""
        rows = self._run("SELECT workspace_id, name, wrapped_key FROM nexura_secrets LIMIT 1")
        if rows:
            ws, name, wrapped = rows[0]
            self._provider.unwrap(bytes(wrapped), _aad(str(ws), str(name)))

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
        mode: PutMode = "upsert",
    ) -> StoredSecretInfo:
        """Encrypt and store (or replace) a secret; replacing keeps created_at/created_by.

        ``mode="create"`` refuses an existing name (``SecretExistsError``) and
        ``mode="replace"`` a missing one (``SecretNotFoundError``). A replace never lowers a
        ``revoked`` status: a revocation is undone only through ``set_status``.
        """
        aesgcm = load_aesgcm()
        data_key = os.urandom(32)
        nonce = os.urandom(12)
        aad = _aad(workspace_id, name)
        ciphertext = aesgcm(data_key).encrypt(nonce, secret.encode("utf-8"), aad)
        now = time.time()
        with self._lock:
            prior = self._run(
                "SELECT created_at, created_by, status FROM nexura_secrets "
                "WHERE workspace_id=? AND name=?",
                (workspace_id, name),
            )
            if prior and mode == "create":
                raise SecretExistsError(workspace_id, name)
            if not prior and mode == "replace":
                raise SecretNotFoundError(workspace_id, name)
            created_at = float(prior[0][0]) if prior else now
            if prior:
                created_by = str(prior[0][1])
                if str(prior[0][2]) == REVOKED:
                    status = REVOKED
            # Wrap and insert under the store lock; re-wrap if the provider was swapped meanwhile
            # (a subclass may replace ``_provider`` under this lock, for example to rotate keys).
            while True:
                provider_in_use = self._provider
                wrapped = provider_in_use.wrap(data_key, aad)
                if provider_in_use is self._provider:
                    break
            try:
                self._run(
                    ("INSERT" if mode == "create" else "INSERT OR REPLACE")
                    + " INTO nexura_secrets (workspace_id, name, ciphertext, nonce, "
                    "wrapped_key, key_id, credential_type, provider, description, "
                    "environment_variable, status, masked_hint, created_at, updated_at, "
                    "last_verified_at, created_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?)",
                    (
                        workspace_id,
                        name,
                        ciphertext,
                        nonce,
                        wrapped,
                        provider_in_use.key_id,
                        credential_type,
                        provider,
                        description,
                        environment_variable,
                        status,
                        mask_hint(secret),
                        created_at,
                        now,
                        created_by,
                    ),
                )
            except sqlite3.IntegrityError:  # another process created it meanwhile
                raise SecretExistsError(workspace_id, name) from None
        return self.get_info(workspace_id, name)

    @staticmethod
    def _info(row: tuple[Any, ...]) -> StoredSecretInfo:
        return StoredSecretInfo(*row)

    def get_info(self, workspace_id: str, name: str) -> StoredSecretInfo:
        """Return masked metadata for one secret."""
        rows = self._run(
            f"SELECT {_COLS} FROM nexura_secrets WHERE workspace_id=? AND name=?",
            (workspace_id, name),
        )
        if not rows:
            raise SecretNotFoundError(workspace_id, name)
        return self._info(rows[0])

    def list_info(self, workspace_id: str) -> list[StoredSecretInfo]:
        """List masked metadata for every secret in a workspace."""
        rows = self._run(
            f"SELECT {_COLS} FROM nexura_secrets WHERE workspace_id=? ORDER BY name",
            (workspace_id,),
        )
        return [self._info(r) for r in rows]

    def list_all_info(self) -> list[StoredSecretInfo]:
        """List masked metadata across all workspaces (used to warm caches on startup)."""
        rows = self._run(f"SELECT {_COLS} FROM nexura_secrets ORDER BY workspace_id, name")
        return [self._info(r) for r in rows]

    def resolve(self, workspace_id: str, name: str) -> str:
        """Decrypt and return the plaintext; callers must never log or return it to clients."""
        aad = _aad(workspace_id, name)
        for attempt in (0, 1):
            with self._lock:
                provider = self._provider
                rows = self._run(
                    "SELECT ciphertext, nonce, wrapped_key FROM nexura_secrets "
                    "WHERE workspace_id=? AND name=?",
                    (workspace_id, name),
                )
                if not rows:
                    raise SecretNotFoundError(workspace_id, name)
                ciphertext, nonce, wrapped = (bytes(x) for x in rows[0])
                try:
                    data_key = provider.unwrap(wrapped, aad)
                    return bytes(load_aesgcm()(data_key).decrypt(nonce, ciphertext, aad)).decode(
                        "utf-8"
                    )
                except Exception as exc:
                    if attempt == 0 and provider is not self._provider:
                        continue  # provider swapped between read and unwrap: re-read once
                    if isinstance(exc, MasterKeyMismatchError):
                        raise
                    raise MasterKeyMismatchError() from exc
        raise MasterKeyMismatchError()  # pragma: no cover

    def set_status(self, workspace_id: str, name: str, status: str) -> StoredSecretInfo:
        """Update the status (for example ``revoked``) of a secret."""
        self.get_info(workspace_id, name)
        self._run(
            "UPDATE nexura_secrets SET status=?, updated_at=? WHERE workspace_id=? AND name=?",
            (status, time.time(), workspace_id, name),
        )
        return self.get_info(workspace_id, name)

    def mark_verified(self, workspace_id: str, name: str, at: float) -> StoredSecretInfo:
        """Record the last verification time of a secret."""
        self.get_info(workspace_id, name)
        self._run(
            "UPDATE nexura_secrets SET last_verified_at=? WHERE workspace_id=? AND name=?",
            (at, workspace_id, name),
        )
        return self.get_info(workspace_id, name)

    def delete(self, workspace_id: str, name: str) -> bool:
        """Delete a secret; returns False when it did not exist."""
        existed = bool(
            self._run(
                "SELECT 1 FROM nexura_secrets WHERE workspace_id=? AND name=?",
                (workspace_id, name),
            )
        )
        self._run(
            "DELETE FROM nexura_secrets WHERE workspace_id=? AND name=?", (workspace_id, name)
        )
        return existed
