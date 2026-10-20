# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Models for tracking and reconciling external side-effects."""

from __future__ import annotations

import time
from typing import Any, Literal

from pydantic import BaseModel, Field

EffectStatus = Literal["pending", "confirmed", "indeterminate", "reconciled"]


class EffectRecord(BaseModel):
    """Durable record of an external side-effect mutation."""

    workspace_id: str
    effect_id: str
    run_id: str
    action_name: str
    idempotency_key: str
    payload_digest: str
    status: EffectStatus = "pending"
    dispatched_at: float = Field(default_factory=time.time)
    response_payload: dict[str, Any] | None = None
    reconciliation_notes: str | None = None
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
