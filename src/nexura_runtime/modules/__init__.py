# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Versioned module registry and CLI package for Nexura AgentKit."""

import nexura_agents.mcp  # noqa: F401
import nexura_agents.usage  # noqa: F401

import nexura_runtime.evaluation  # noqa: F401
import nexura_runtime.integrations.http  # noqa: F401
import nexura_runtime.integrations.sandbox  # noqa: F401
import nexura_runtime.integrations.sql  # noqa: F401
import nexura_runtime.knowledge.chunks  # noqa: F401
import nexura_runtime.knowledge.embeddings  # noqa: F401
import nexura_runtime.knowledge.ingest  # noqa: F401
import nexura_runtime.knowledge.retrieval  # noqa: F401
import nexura_runtime.knowledge.vector  # noqa: F401
import nexura_runtime.modules.data  # noqa: F401
import nexura_runtime.modules.dataops  # noqa: F401
import nexura_runtime.modules.logic  # noqa: F401
import nexura_runtime.modules.text  # noqa: F401
import nexura_runtime.modules.textops  # noqa: F401
import nexura_runtime.modules.utils  # noqa: F401
import nexura_runtime.runtime.agents  # noqa: F401
import nexura_runtime.service  # noqa: F401
from nexura_runtime.modules.cli import main
from nexura_runtime.modules.optional_packages import load_connectors
from nexura_runtime.modules.registry import (
    IncompatibleSDKVersionError,
    LegacyModuleAdapter,
    ModuleCollisionError,
    ModuleNotFoundError,
    RegisteredModule,
    VersionedModuleRegistry,
    default_registry,
)

# The connectors live in the separate nexura-connectors package; without it their module ids
# report "pip install nexura-connectors" (modules/optional_packages.py).
load_connectors(default_registry)

__all__ = [
    "VersionedModuleRegistry",
    "RegisteredModule",
    "ModuleCollisionError",
    "IncompatibleSDKVersionError",
    "ModuleNotFoundError",
    "LegacyModuleAdapter",
    "default_registry",
    "main",
]
