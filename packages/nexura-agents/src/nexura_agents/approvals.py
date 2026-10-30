# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""SDK approval envelopes and the optional atomic approval-store contract."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import time
from typing import Protocol

from nexura_agents.contracts.canonical import canonical_json
from nexura_agents.errors import ApprovalDecisionError


class ApprovalEnvelope(Protocol):
    """Common canonical contract for agent and whole-team approval envelopes."""

    interruption_id: str
    expires_at: float
    signature: str | None

    @property
    def _approval_snapshot(self) -> str | None: ...

    @_approval_snapshot.setter
    def _approval_snapshot(self, value: str) -> None: ...

    def to_json(self) -> str:
        """Serialize every field used to resume, excluding private trust metadata."""
        ...


class ApprovalStore(Protocol):
    """Trusted, immutable envelopes and permanent consumption tombstones.

    Built-in MemoryCheckpointStore and SQLiteCheckpointStore implement this
    protocol. Custom stores must compare payload, expiry and consumption atomically
    using their own clock. Consumption must survive checkpoint deletion.
    """

    async def save_approval(self, approval_id: str, payload: str, expires_at: float) -> None:
        """Register once; refuse replacing an existing id with another payload."""
        ...

    async def load_approval(self, approval_id: str) -> str | None:
        """Load the trusted envelope, including after consumption."""
        ...

    async def consume_approval(self, approval_id: str, payload: str) -> bool:
        """Atomically consume an unexpired matching envelope once; False otherwise."""
        ...


def payload(interruption: ApprovalEnvelope) -> str:
    """Canonical full envelope (identity, exact actions, expiry, transcript and state)."""
    try:
        data = json.loads(interruption.to_json())
        data.pop("signature", None)
        canonical = canonical_json(data)
        canonical.encode("utf-8")  # Reject unencodable surrogates with a typed approval error.
        return canonical
    except (ValueError, TypeError, UnicodeError) as exc:
        raise ApprovalDecisionError(
            "Interruption cannot be canonicalized; refusing resume."
        ) from exc


def signature(interruption: ApprovalEnvelope, secret: bytes) -> str:
    return hmac.new(secret, payload(interruption).encode("utf-8"), hashlib.sha256).hexdigest()


def check_expiry(interruption: ApprovalEnvelope) -> None:
    expiry = interruption.expires_at
    if (
        isinstance(expiry, bool)
        or not isinstance(expiry, (float, int))
        or not math.isfinite(expiry)
        or expiry <= time.time()
    ):
        raise ApprovalDecisionError(
            "Interruption expired; obtain a fresh approval before resuming."
        )
