# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Model adapters and protocol for Nexura AgentKit."""

from __future__ import annotations

import importlib
import importlib.util
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexura_agents.models.base import ModelProtocol
    from nexura_agents.models.fake import FakeModel
    from nexura_agents.models.openai import OpenAIChatModel

_EXPORTS: dict[str, str] = {
    "ModelProtocol": "nexura_agents.models.base",
    "FakeModel": "nexura_agents.models.fake",
    "OpenAIChatModel": "nexura_agents.models.openai",
}

__all__ = ["ModelProtocol", "FakeModel", "OpenAIChatModel"]


def __getattr__(name: str) -> Any:
    module_path = _EXPORTS.get(name)
    if module_path is None:
        if not name.startswith("_") and importlib.util.find_spec(f"{__name__}.{name}") is not None:
            return importlib.import_module(f"{__name__}.{name}")
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(module_path), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
