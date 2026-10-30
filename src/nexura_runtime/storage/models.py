# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Models for unified storage runs and events."""

from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel, Field


class RunRecord(BaseModel):
    """Represents a durable workflow or agent execution run in storage."""

    workspace_id: str
    run_id: str
    status: str
    revision: int
    input_data: dict[str, Any] = Field(default_factory=dict)
    state: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)


class RunEventRecord(BaseModel):
    """An ordered, sequenced event associated with a run."""

    workspace_id: str
    run_id: str
    seq: int
    event_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    timestamp: float = Field(default_factory=time.time)
