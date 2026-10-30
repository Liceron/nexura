# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Data models for conversation sessions, long-term memory facts, and context policies."""

from __future__ import annotations

import time
from typing import Any, Literal

from nexura_agents.types import Message
from pydantic import BaseModel, ConfigDict, Field


class LongTermFact(BaseModel):
    """An immutable or mutable long-term memory fact with provenance and soft deletion."""

    fact_id: str
    workspace_id: str
    principal_id: str
    fact_key: str
    value: Any
    category: str = "general"
    source_session_id: str | None = None
    source_message_index: int | None = None
    confidence: float = 1.0
    is_deleted: bool = False
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)


class ConversationSession(BaseModel):
    """A durable conversation session scoped by workspace and owner principal."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    session_id: str
    workspace_id: str
    principal_id: str
    revision: int = 1
    messages: list[Message] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)


class ContextTruncationPolicy(BaseModel):
    """Specification for bounding conversation context before model invocation."""

    strategy: Literal["preserve_system_and_recent", "sliding_window"] = "preserve_system_and_recent"
    max_messages: int = 20
    preserve_system_prompt: bool = True
