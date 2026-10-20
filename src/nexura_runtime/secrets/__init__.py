# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""The local encrypted secrets file: envelope encryption with a pluggable master key provider.

``EncryptedSecretStore`` backs ``ScopedCredentialStore`` when ``NEXURA_MASTER_KEY`` is set (see
``docs/local-secrets.md``). Organisations, the hosted secrets admin API and master-key rotation
are part of the closed edition (EDITIONS_PLAN step 5).
"""

from __future__ import annotations

from nexura_runtime.credentials.backends import mask_hint
from nexura_runtime.secrets.errors import (
    MasterKeyInvalidError,
    MasterKeyMismatchError,
    MasterKeyMissingError,
    SecretNotFoundError,
    SecretsError,
    SecretsUnavailableError,
)
from nexura_runtime.secrets.keys import (
    INSECURE_PLAINTEXT_ACK,
    INSECURE_PLAINTEXT_ENV,
    MASTER_KEY_ENV,
    MasterKeyProvider,
    StaticMasterKeyProvider,
    generate_master_key,
    master_key_provider_from_env,
)
from nexura_runtime.secrets.store import EncryptedSecretStore, StoredSecretInfo

__all__ = [
    "INSECURE_PLAINTEXT_ACK",
    "INSECURE_PLAINTEXT_ENV",
    "MASTER_KEY_ENV",
    "EncryptedSecretStore",
    "MasterKeyInvalidError",
    "MasterKeyMismatchError",
    "MasterKeyMissingError",
    "MasterKeyProvider",
    "SecretNotFoundError",
    "SecretsError",
    "SecretsUnavailableError",
    "StaticMasterKeyProvider",
    "StoredSecretInfo",
    "generate_master_key",
    "mask_hint",
    "master_key_provider_from_env",
]
