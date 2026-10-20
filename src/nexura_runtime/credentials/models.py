# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""SecretValue wrapper and CredentialMetadata models."""

from __future__ import annotations

import hmac
import time

from pydantic import BaseModel, Field


class SecretValue:
    """Opaque secret container that masks raw tokens from string/repr representations."""

    def __init__(self, secret: str) -> None:
        self._secret = secret

    def __repr__(self) -> str:
        return "***REDACTED***"

    def __str__(self) -> str:
        return "***REDACTED***"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, SecretValue):
            return hmac.compare_digest(self._secret, other._secret)
        if isinstance(other, str):
            return hmac.compare_digest(self._secret, other)
        return False

    def reveal(self) -> str:
        """Explicitly reveal raw secret string for outbound request headers/auth."""
        return self._secret


class CredentialMetadata(BaseModel):
    """Metadata describing a credential without exposing its secret value."""

    credential_id: str
    workspace_id: str
    credential_type: str
    description: str = ""
    created_at: float = Field(default_factory=time.time)
    environment_variable: str | None = None
    provider: str = "custom"
    status: str = "active"  # "active" | "revoked" | "expired"
    last_verified_at: float | None = None
    masked_hint: str = ""
