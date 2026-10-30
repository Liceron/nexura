# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Typed errors for the encrypted secrets store."""

from __future__ import annotations


class SecretsError(Exception):
    """Base class for secrets-store failures; messages never contain secret material."""

    def __init__(self, message: str, code: str = "SECRETS_ERROR") -> None:
        super().__init__(message)
        self.code = code


class SecretsUnavailableError(SecretsError):
    """Raised when ``cryptography`` (a core dependency) cannot be imported: a broken install."""

    def __init__(self) -> None:
        super().__init__(
            "The encrypted secrets store needs the 'cryptography' package, a dependency of "
            "nexura-runtime that could not be imported; reinstall nexura-runtime.",
            code="SECRETS_UNAVAILABLE",
        )


class MasterKeyMissingError(SecretsError):
    """Raised when a master key is required but NEXURA_MASTER_KEY is not set."""

    def __init__(self) -> None:
        super().__init__(
            "NEXURA_MASTER_KEY is not set. The shared service refuses to start without a master "
            "key. Generate one with nexura_runtime.secrets.generate_master_key().",
            code="MASTER_KEY_MISSING",
        )


class MasterKeyInvalidError(SecretsError):
    """Raised when the configured master key is not 32 bytes of url-safe base64."""

    def __init__(self) -> None:
        super().__init__(
            "NEXURA_MASTER_KEY must be 32 random bytes encoded as url-safe base64.",
            code="MASTER_KEY_INVALID",
        )


class MasterKeyMismatchError(SecretsError):
    """Raised (fail closed) when stored secrets cannot be unwrapped with the supplied key."""

    def __init__(self) -> None:
        super().__init__(
            "The master key does not match the one that protects the stored secrets.",
            code="MASTER_KEY_MISMATCH",
        )


class SecretNotFoundError(SecretsError):
    """Raised when a named secret does not exist in the workspace."""

    def __init__(self, workspace_id: str, name: str) -> None:
        super().__init__(
            f"Secret '{name}' not found in workspace '{workspace_id}'.", code="SECRET_NOT_FOUND"
        )


class SecretExistsError(SecretsError):
    """Raised by a create-only write (``put(..., mode="create")``) when the name is taken."""

    def __init__(self, workspace_id: str, name: str) -> None:
        super().__init__(
            f"Secret '{name}' already exists in workspace '{workspace_id}'.", code="SECRET_EXISTS"
        )
