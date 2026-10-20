# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""A2A (Agent2Agent) interoperability: serve Nexura agents and call remote A2A agents.

Implements A2A protocol 1.0 over the JSON-RPC binding (plus the 0.3 method names).
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

_EXPORTS = {
    "A2AServer": "nexura_agents.a2a.server",
    "A2AClient": "nexura_agents.a2a.client",
    "A2ATask": "nexura_agents.a2a.client",
    "A2AEvent": "nexura_agents.a2a.client",
    "remote_agent_tool": "nexura_agents.a2a.client",
    "A2AError": "nexura_agents.a2a.protocol",
    "PROTOCOL_VERSION": "nexura_agents.a2a.protocol",
}

__all__ = [
    "A2AServer",
    "A2AClient",
    "A2ATask",
    "A2AEvent",
    "remote_agent_tool",
    "A2AError",
    "PROTOCOL_VERSION",
]


def __getattr__(name: str) -> Any:
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module 'nexura_agents.a2a' has no attribute {name!r}")
    value = getattr(importlib.import_module(target), name)
    globals()[name] = value
    return value


if TYPE_CHECKING:
    from nexura_agents.a2a.client import A2AClient, A2AEvent, A2ATask, remote_agent_tool
    from nexura_agents.a2a.protocol import PROTOCOL_VERSION, A2AError
    from nexura_agents.a2a.server import A2AServer
