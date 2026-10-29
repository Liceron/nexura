# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Envelope-encrypted secrets on Postgres (MVP Postgres WP5, store #11).

``PostgresSecretStore`` has the API and errors of ``nexura_runtime.secrets.EncryptedSecretStore``
(it is a subclass) and the **same envelope format**: one random AES-256-GCM data key per secret,
wrapped by the master key provider, both bound to ``workspace_id`` and ``name`` by the same
associated data. The ciphertext, nonce and wrapped key are stored byte for byte as the SQLite store
stores them (``BYTEA`` here, ``BLOB`` there), so a row copied between the backends decrypts with
the same master key, and a different key fails closed with ``MasterKeyMismatchError``, at open
time as on SQLite.

The plaintext never reaches the database, a log or an error message: only the ciphertext and the
masked hint are sent. Every write is one statement, atomic across instances: an upsert is
``INSERT … ON CONFLICT DO UPDATE`` that keeps ``created_at`` and ``created_by``; a create
(``mode="create"``) is ``ON CONFLICT DO NOTHING``, so of two instances creating one name exactly
one succeeds and the other gets ``SecretExistsError``; a replace (``mode="replace"``) is an
``UPDATE`` of an existing row. No write lowers a ``revoked`` status, so a replace racing a revoke
on another instance never un-revokes the secret. Listings are in byte order (``COLLATE "C"``), as
SQLite orders them.

Differences from SQLite: Postgres ``TEXT`` cannot hold NUL, so a workspace, name or other text
field with a NUL is refused on write (``SecretsError``, code ``SECRET_INVALID``) and reads as
missing; a secret value with a NUL is refused too (its masked hint would hold it). Master-key
rotation (closed edition, ``RotatingSecretStore``) is SQLite-only for now.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any

from nexura_postgres.migrations import MIGRATION_TIMEOUT, Migration, migrate_all, register
from nexura_runtime.credentials.backends import mask_hint
from nexura_runtime.secrets.errors import (
    MasterKeyMismatchError,
    SecretExistsError,
    SecretNotFoundError,
    SecretsError,
)
from nexura_runtime.secrets.keys import MasterKeyProvider, load_aesgcm
from nexura_runtime.secrets.store import (
    _COLS,
    EncryptedSecretStore,
    PutMode,
    StoredSecretInfo,
    _aad,
)

COMPONENT = "secrets"
MIGRATIONS: tuple[Migration, ...] = (
    (
        1,
        "encrypted secrets",
        (
            """CREATE TABLE nexura_secrets (
                workspace_id TEXT NOT NULL,
                name TEXT NOT NULL,
                ciphertext BYTEA NOT NULL,
                nonce BYTEA NOT NULL,
                wrapped_key BYTEA NOT NULL,
                key_id TEXT NOT NULL,
                credential_type TEXT NOT NULL,
                provider TEXT NOT NULL,
                description TEXT NOT NULL,
                environment_variable TEXT,
                status TEXT NOT NULL,
                masked_hint TEXT NOT NULL,
                created_at DOUBLE PRECISION NOT NULL,
                updated_at DOUBLE PRECISION NOT NULL,
                last_verified_at DOUBLE PRECISION,
                created_by TEXT NOT NULL,
                PRIMARY KEY (workspace_id, name))""",
        ),
    ),
)
register(COMPONENT, MIGRATIONS)

_INSERT = (
    "INSERT INTO nexura_secrets (workspace_id, name, ciphertext, nonce, wrapped_key, key_id, "
    "credential_type, provider, description, environment_variable, status, masked_hint, "
    "created_at, updated_at, last_verified_at, created_by) "
    "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$13,NULL,$14) "
)
#: A replace never lowers ``revoked``: of a replace (read ``active``, then write) racing a revoke
#: on another instance, the revocation always survives (review WP5 M1).
_KEEP_REVOKED = "CASE WHEN nexura_secrets.status = 'revoked' THEN 'revoked' ELSE {new} END"
_SET = (
    "ciphertext = {x}ciphertext, nonce = {x}nonce, wrapped_key = {x}wrapped_key, "
    "key_id = {x}key_id, credential_type = {x}credential_type, provider = {x}provider, "
    "description = {x}description, environment_variable = {x}environment_variable, "
    "status = {status}, masked_hint = {x}masked_hint, updated_at = {x}updated_at, "
    "last_verified_at = NULL "
)
#: ``mode="upsert"``: one atomic statement that keeps ``created_at`` and ``created_by``.
_UPSERT = (
    _INSERT
    + "ON CONFLICT (workspace_id, name) DO UPDATE SET "
    + _SET.format(x="EXCLUDED.", status=_KEEP_REVOKED.format(new="EXCLUDED.status"))
    + f"RETURNING {_COLS}"
)
#: ``mode="create"``: no row comes back when the name exists (another instance's create won).
_CREATE = _INSERT + f"ON CONFLICT (workspace_id, name) DO NOTHING RETURNING {_COLS}"
#: ``mode="replace"``: no row comes back when the secret does not exist (or was just deleted).
#: Parameters are those of ``_INSERT`` without ``created_by`` ($1–$13).
_REPLACE = (
    "UPDATE nexura_secrets SET ciphertext = $3, nonce = $4, wrapped_key = $5, key_id = $6, "
    "credential_type = $7, provider = $8, description = $9, environment_variable = $10, "
    f"status = {_KEEP_REVOKED.format(new='$11')}, masked_hint = $12, updated_at = $13, "
    "last_verified_at = NULL "
    f"WHERE workspace_id = $1 AND name = $2 RETURNING {_COLS}"
)


def _storable(*values: str | None) -> bool:
    """Postgres ``TEXT`` cannot hold NUL; such a value can never have been stored."""
    return not any(value is not None and "\x00" in value for value in values)


def _invalid(what: str) -> SecretsError:
    # The message names the field, never its value.
    return SecretsError(f"{what} cannot contain NUL characters.", code="SECRET_INVALID")


class PostgresSecretStore(EncryptedSecretStore):
    """Workspace-scoped secrets encrypted at rest in the shared Postgres ``Database``.

    ``database``: a ``nexura_postgres.Database`` (the service's shared pool); the store never
    closes it. Opening the store applies its migrations (component ``secrets``) and then checks
    the master key against a stored secret, as the SQLite store does.
    """

    #: Several service instances share these rows, so ``ScopedCredentialStore`` reads its
    #: metadata through on every lookup instead of trusting what it loaded at startup.
    shared = True

    def __init__(self, provider: MasterKeyProvider, database: Any, *, migrate: bool = True) -> None:
        # EncryptedSecretStore.__init__ is not called: it opens a SQLite file.
        self._provider = provider
        self._lock = threading.RLock()
        self._path = None
        self._mem = None
        self.database = database
        if migrate:
            components = {COMPONENT: MIGRATIONS}

            async def apply(conn: Any) -> None:
                await migrate_all(conn, components)

            database.run(apply, timeout=MIGRATION_TIMEOUT + 30)
        load_aesgcm()
        self.verify_master_key()

    def _conn(self) -> Any:
        raise TypeError("PostgresSecretStore has no SQLite connection")

    def _run(self, sql: str, args: tuple[object, ...] = ()) -> list[tuple[Any, ...]]:
        raise TypeError("PostgresSecretStore does not run SQLite statements")

    def verify_master_key(self) -> None:
        """Fail closed unless the provider can unwrap a stored secret (no-op when empty)."""

        async def run(conn: Any) -> Any:
            return await conn.fetchrow(
                "SELECT workspace_id, name, wrapped_key FROM nexura_secrets LIMIT 1"
            )

        row = self.database.run(run)
        if row is not None:
            self._provider.unwrap(bytes(row[2]), _aad(str(row[0]), str(row[1])))

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

        One statement per mode, atomic across instances: ``upsert`` (``INSERT … ON CONFLICT DO
        UPDATE``), ``create`` (``ON CONFLICT DO NOTHING``; ``SecretExistsError`` when another
        write got there first) and ``replace`` (``UPDATE``; ``SecretNotFoundError`` when the
        secret is gone). A replace never lowers a ``revoked`` status.
        """
        if mode not in ("upsert", "create", "replace"):
            raise ValueError("mode must be 'upsert', 'create' or 'replace'")
        if not _storable(workspace_id, name):
            raise _invalid("Workspace and secret names")
        if not _storable(
            credential_type, provider, description, environment_variable, status, created_by
        ):
            raise _invalid("Secret metadata")
        if not _storable(secret):
            raise _invalid("Secret values stored in Postgres")
        aesgcm = load_aesgcm()
        data_key = os.urandom(32)
        nonce = os.urandom(12)
        aad = _aad(workspace_id, name)
        ciphertext = aesgcm(data_key).encrypt(nonce, secret.encode("utf-8"), aad)
        hint = mask_hint(secret)
        with self._lock:
            # Re-wrap if the provider was swapped meanwhile (as the SQLite store does).
            while True:
                provider_in_use = self._provider
                wrapped = provider_in_use.wrap(data_key, aad)
                if provider_in_use is self._provider:
                    break
            args: tuple[Any, ...] = (
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
                hint,
                time.time(),
            )
            if mode == "replace":
                sql = _REPLACE
            else:
                sql = _CREATE if mode == "create" else _UPSERT
                args = (*args, created_by)

            async def run(conn: Any) -> Any:
                return await conn.fetchrow(sql, *args)

            row = self.database.run(run)
        if row is None:
            if mode == "create":
                raise SecretExistsError(workspace_id, name)
            raise SecretNotFoundError(workspace_id, name)
        return self._info(tuple(row))

    def _fetch_info(self, sql: str, *args: Any) -> list[StoredSecretInfo]:
        async def run(conn: Any) -> Any:
            return await conn.fetch(sql, *args)

        # One statement: no transaction (MVP WP11a).
        return [self._info(tuple(row)) for row in self.database.run(run, autocommit=True)]

    def get_info(self, workspace_id: str, name: str) -> StoredSecretInfo:
        """Return masked metadata for one secret."""
        rows = (
            self._fetch_info(
                f"SELECT {_COLS} FROM nexura_secrets WHERE workspace_id = $1 AND name = $2",
                workspace_id,
                name,
            )
            if _storable(workspace_id, name)
            else []
        )
        if not rows:
            raise SecretNotFoundError(workspace_id, name)
        return rows[0]

    def list_info(self, workspace_id: str) -> list[StoredSecretInfo]:
        """List masked metadata for every secret in a workspace (by name, byte order)."""
        if not _storable(workspace_id):
            return []
        return self._fetch_info(
            f'SELECT {_COLS} FROM nexura_secrets WHERE workspace_id = $1 ORDER BY name COLLATE "C"',
            workspace_id,
        )

    def list_all_info(self) -> list[StoredSecretInfo]:
        """List masked metadata across all workspaces (used to warm caches on startup)."""
        return self._fetch_info(
            f'SELECT {_COLS} FROM nexura_secrets ORDER BY workspace_id COLLATE "C", '
            'name COLLATE "C"'
        )

    def resolve(self, workspace_id: str, name: str) -> str:
        """Decrypt and return the plaintext; callers must never log or return it to clients."""
        if not _storable(workspace_id, name):
            raise SecretNotFoundError(workspace_id, name)
        aad = _aad(workspace_id, name)

        async def read(conn: Any) -> Any:
            return await conn.fetchrow(
                "SELECT ciphertext, nonce, wrapped_key FROM nexura_secrets "
                "WHERE workspace_id = $1 AND name = $2",
                workspace_id,
                name,
            )

        for attempt in (0, 1):
            with self._lock:
                provider = self._provider
                row = self.database.run(read)
                if row is None:
                    raise SecretNotFoundError(workspace_id, name)
                ciphertext, nonce, wrapped = (bytes(x) for x in row)
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

    def _update(
        self, workspace_id: str, name: str, assignment: str, *values: Any
    ) -> StoredSecretInfo:
        """One ``UPDATE … RETURNING``: ``assignment`` uses ``$3`` onwards for ``values``."""
        rows = (
            self._fetch_info(
                f"UPDATE nexura_secrets SET {assignment} WHERE workspace_id = $1 AND name = $2 "
                f"RETURNING {_COLS}",
                workspace_id,
                name,
                *values,
            )
            if _storable(workspace_id, name)
            else []
        )
        if not rows:
            raise SecretNotFoundError(workspace_id, name)
        return rows[0]

    def set_status(self, workspace_id: str, name: str, status: str) -> StoredSecretInfo:
        """Update the status (for example ``revoked``) of a secret."""
        if not _storable(status):
            raise _invalid("Secret status")
        return self._update(workspace_id, name, "status = $3, updated_at = $4", status, time.time())

    def mark_verified(self, workspace_id: str, name: str, at: float) -> StoredSecretInfo:
        """Record the last verification time of a secret."""
        return self._update(workspace_id, name, "last_verified_at = $3", at)

    def delete(self, workspace_id: str, name: str) -> bool:
        """Delete a secret; returns False when it did not exist."""
        if not _storable(workspace_id, name):
            return False

        async def run(conn: Any) -> Any:
            return await conn.fetchval(
                "DELETE FROM nexura_secrets WHERE workspace_id = $1 AND name = $2 RETURNING 1",
                workspace_id,
                name,
            )

        return self.database.run(run) is not None

    def close(self) -> None:
        """Nothing to release: the shared ``Database`` belongs to the app."""


__all__ = ["COMPONENT", "MIGRATIONS", "PostgresSecretStore"]
