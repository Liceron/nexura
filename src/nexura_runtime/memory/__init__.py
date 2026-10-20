# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Sessions and long-term memory package."""

from nexura_runtime.memory.errors import (
    DuplicateSystemPromptError,
    FactNotFoundError,
    MemoryError,
    SessionAccessDeniedError,
    SessionNotFoundError,
    SessionRevisionConflictError,
)
from nexura_runtime.memory.models import (
    ContextTruncationPolicy,
    ConversationSession,
    LongTermFact,
)
from nexura_runtime.memory.module import MemoryModule
from nexura_runtime.memory.store import MemoryStore, SessionTransaction
from nexura_runtime.modules.registry import default_registry

__all__ = [
    "MemoryStore",
    "SessionTransaction",
    "ConversationSession",
    "LongTermFact",
    "ContextTruncationPolicy",
    "MemoryModule",
    "MemoryError",
    "SessionNotFoundError",
    "SessionAccessDeniedError",
    "FactNotFoundError",
    "DuplicateSystemPromptError",
    "SessionRevisionConflictError",
]

# Auto-register memory module in default registry
if not default_registry.has("nexura.runtime.memory"):
    default_registry.register(MemoryModule)
