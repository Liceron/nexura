# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""At-rest encryption for webhook signing secrets.

The service had no credential-encryption mechanism to reuse for trigger secrets, so this module
provides a small one. Key material comes from, in order:

1. ``NEXURA_TRIGGER_SECRET_KEY`` (any string; recommended for shared deployments, keeps the key
   off the database host's disk), else
2. a random key file ``<db_path>.trigger.key`` created with mode 0600 next to the database
   (so the key is not inside the SQLite file, its WAL or a plain copy of them), else
3. a random per-process key for in-memory databases.

Ciphertext is ``enc:v1:<token>`` using Fernet (``cryptography``) when installed, otherwise an
HMAC-SHA256 counter-mode stream with an encrypt-then-MAC tag (``enc:v2:``). Values without an
``enc:`` prefix are treated as legacy plaintext and re-encrypted on store startup.
"""

from __future__ import annotations

import base64
import contextlib
import hmac
import os
import secrets
from hashlib import sha256
from pathlib import Path

ENV_KEY = "NEXURA_TRIGGER_SECRET_KEY"
_PREFIX = "enc:"


def _load_master_key(db_path: str) -> bytes:
    db_path = str(db_path)
    env = os.environ.get(ENV_KEY)
    if env:
        return sha256(env.encode("utf-8")).digest()
    if db_path in ("", ":memory:") or db_path.startswith("file:"):
        return secrets.token_bytes(32)
    key_path = Path(f"{db_path}.trigger.key")
    try:
        return base64.urlsafe_b64decode(key_path.read_bytes().strip())
    except FileNotFoundError:
        pass
    key = secrets.token_bytes(32)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        fd = os.open(key_path, flags, 0o600)
    except FileExistsError:  # concurrent creator won the race
        return base64.urlsafe_b64decode(key_path.read_bytes().strip())
    with os.fdopen(fd, "wb") as fh:
        fh.write(base64.urlsafe_b64encode(key))
    return key


def _stream(key: bytes, nonce: bytes, length: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < length:
        out += hmac.new(key, nonce + counter.to_bytes(8, "big"), sha256).digest()
        counter += 1
    return bytes(out[:length])


class SecretBox:
    """Encrypt/decrypt short secrets with a key bound to one database."""

    def __init__(self, db_path: str) -> None:
        master = _load_master_key(db_path)
        self._enc_key = hmac.new(master, b"nexura-trigger-enc", sha256).digest()
        self._mac_key = hmac.new(master, b"nexura-trigger-mac", sha256).digest()
        self._fernet = None
        with contextlib.suppress(ImportError):
            from cryptography.fernet import Fernet

            self._fernet = Fernet(base64.urlsafe_b64encode(self._enc_key))

    @staticmethod
    def is_encrypted(value: str) -> bool:
        return value.startswith(_PREFIX)

    def encrypt(self, plaintext: str) -> str:
        if self._fernet is not None:
            return "enc:v1:" + self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")
        data = plaintext.encode("utf-8")
        nonce = secrets.token_bytes(16)
        ct = bytes(
            a ^ b for a, b in zip(data, _stream(self._enc_key, nonce, len(data)), strict=True)
        )
        tag = hmac.new(self._mac_key, nonce + ct, sha256).digest()
        return "enc:v2:" + base64.urlsafe_b64encode(nonce + ct + tag).decode("ascii")

    def decrypt(self, value: str) -> str:
        if not value.startswith(_PREFIX):
            return value  # legacy plaintext row
        scheme, _, token = value[len(_PREFIX) :].partition(":")
        if scheme == "v1":
            if self._fernet is None:
                raise ValueError("Webhook secret requires the 'cryptography' package to decrypt.")
            return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
        if scheme == "v2":
            raw = base64.urlsafe_b64decode(token.encode("ascii"))
            nonce, ct, tag = raw[:16], raw[16:-32], raw[-32:]
            expect = hmac.new(self._mac_key, nonce + ct, sha256).digest()
            if not hmac.compare_digest(tag, expect):
                raise ValueError("Webhook secret ciphertext failed authentication.")
            return bytes(
                a ^ b for a, b in zip(ct, _stream(self._enc_key, nonce, len(ct)), strict=True)
            ).decode("utf-8")
        raise ValueError(f"Unknown webhook secret encoding '{scheme}'.")
