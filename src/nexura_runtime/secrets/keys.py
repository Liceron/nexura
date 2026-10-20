# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Master key providers: the KMS-style interface that wraps per-secret data keys."""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re
from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from nexura_runtime.secrets.errors import (
    MasterKeyInvalidError,
    MasterKeyMismatchError,
    MasterKeyMissingError,
    SecretsUnavailableError,
)

_KEY_TEXT_RE = re.compile(r"[A-Za-z0-9_-]{43}={0,1}")
MASTER_KEY_ENV = "NEXURA_MASTER_KEY"
INSECURE_PLAINTEXT_ENV = "NEXURA_INSECURE_ALLOW_PLAINTEXT_SECRETS"
INSECURE_PLAINTEXT_ACK = "I_UNDERSTAND_THIS_IS_UNSAFE"


def load_aesgcm() -> Any:
    """Import ``cryptography``'s AESGCM lazily; raise SecretsUnavailableError if absent."""
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:  # pragma: no cover - exercised via monkeypatch in tests
        raise SecretsUnavailableError() from exc
    return AESGCM


def generate_master_key() -> str:
    """Return a fresh random 32-byte master key as url-safe base64 text."""
    return base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")


@runtime_checkable
class MasterKeyProvider(Protocol):
    """Wraps and unwraps data keys; implement this to back the store with a real KMS."""

    @property
    def key_id(self) -> str:
        """Stable non-secret identifier of the master key (never the key itself)."""
        ...

    def wrap(self, data_key: bytes, aad: bytes) -> bytes:
        """Encrypt ``data_key`` bound to ``aad``; the result embeds any nonce it needs."""
        ...

    def unwrap(self, wrapped: bytes, aad: bytes) -> bytes:
        """Decrypt a wrapped data key; raise MasterKeyMismatchError when it cannot."""
        ...


class StaticMasterKeyProvider:
    """Local master key provider holding a 32-byte AES-256-GCM key in memory."""

    def __init__(self, key: bytes) -> None:
        if len(key) != 32:
            raise MasterKeyInvalidError()
        self._key = key
        self._key_id = hashlib.sha256(b"nexura-master-key-id|" + key).hexdigest()[:16]

    @classmethod
    def from_text(cls, text: str) -> StaticMasterKeyProvider:
        """Build a provider from strict url-safe base64 text (no ``+``/``/``, no whitespace)."""
        if not isinstance(text, str) or not _KEY_TEXT_RE.fullmatch(text.strip()):
            raise MasterKeyInvalidError()
        stripped = text.strip()
        try:
            raw = base64.urlsafe_b64decode(stripped + "=" * (-len(stripped) % 4))
        except (binascii.Error, ValueError):
            raise MasterKeyInvalidError() from None
        return cls(raw)

    @property
    def key_id(self) -> str:
        """Non-secret fingerprint of the key."""
        return self._key_id

    def wrap(self, data_key: bytes, aad: bytes) -> bytes:
        """AES-GCM encrypt the data key; output is nonce + ciphertext."""
        nonce = os.urandom(12)
        return nonce + bytes(load_aesgcm()(self._key).encrypt(nonce, data_key, aad))

    def unwrap(self, wrapped: bytes, aad: bytes) -> bytes:
        """AES-GCM decrypt the data key; any failure is reported as a key mismatch."""
        try:
            return bytes(load_aesgcm()(self._key).decrypt(wrapped[:12], wrapped[12:], aad))
        except SecretsUnavailableError:
            raise
        except Exception as exc:
            raise MasterKeyMismatchError() from exc

    def __repr__(self) -> str:
        return f"StaticMasterKeyProvider(key_id={self._key_id!r})"


def master_key_provider_from_env(
    environ: Mapping[str, str] | None = None, *, required: bool = False
) -> MasterKeyProvider | None:
    """Return the NEXURA_MASTER_KEY provider, None if unset, or raise when ``required``.

    The only way to run ``required`` without a key is the loudly named
    ``NEXURA_INSECURE_ALLOW_PLAINTEXT_SECRETS=I_UNDERSTAND_THIS_IS_UNSAFE`` opt-out, which
    leaves credentials in the in-memory store (never encrypted, never persisted).
    """
    env = os.environ if environ is None else environ
    raw = env.get(MASTER_KEY_ENV, "").strip()
    if not raw:
        if required and env.get(INSECURE_PLAINTEXT_ENV) != INSECURE_PLAINTEXT_ACK:
            raise MasterKeyMissingError()
        return None
    load_aesgcm()  # fail early with a clear message if cryptography is not importable
    return StaticMasterKeyProvider.from_text(raw)
