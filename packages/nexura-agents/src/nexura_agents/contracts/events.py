# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Typed execution events emitted during module execution."""

from __future__ import annotations

import time
import uuid
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class ModuleEventType(StrEnum):
    """Types of events emitted during module execution."""

    INVOKE_STARTED = "invoke_started"
    INVOKE_PROGRESS = "invoke_progress"
    INVOKE_COMPLETED = "invoke_completed"
    INVOKE_FAILED = "invoke_failed"
    STREAM_CHUNK = "stream_chunk"
    ARTIFACT_PRODUCED = "artifact_produced"


class ModuleEvent(BaseModel):
    """Ordered event emitted by a module during invocation."""

    event_id: str = Field(default_factory=lambda: f"evt_{uuid.uuid4().hex[:12]}")
    type: ModuleEventType
    module_id: str
    version: str
    run_id: str
    node_id: str
    attempt_id: str
    timestamp: float = Field(default_factory=time.time)
    payload: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_terminal(self) -> bool:
        """True if this event terminates module invocation."""
        return self.type in (
            ModuleEventType.INVOKE_COMPLETED,
            ModuleEventType.INVOKE_FAILED,
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert event to serialized dictionary."""
        return self.model_dump(mode="json")
