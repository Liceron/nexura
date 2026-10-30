# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Workflow compiler, ports, and control flow package."""

from nexura_runtime.modules.registry import default_registry
from nexura_runtime.runtime.compiler.compiler import (
    MAX_ALLOWED_FANOUT,
    WorkflowCompiler,
)
from nexura_runtime.runtime.compiler.errors import (
    CompilerError,
    FanoutLimitExceededError,
    IncompatiblePortError,
    InvalidCachePolicyError,
    UnresolvedReferenceError,
)
from nexura_runtime.runtime.compiler.executor import CompiledPlanExecutor
from nexura_runtime.runtime.compiler.models import (
    CompiledNode,
    CompiledWorkflowPlan,
    DeterministicJoinSpec,
    ParallelMapSpec,
    PortBinding,
)
from nexura_runtime.runtime.compiler.module import WorkflowCompilerModule

__all__ = [
    "WorkflowCompiler",
    "WorkflowCompilerModule",
    "CompiledPlanExecutor",
    "CompiledWorkflowPlan",
    "CompiledNode",
    "PortBinding",
    "ParallelMapSpec",
    "DeterministicJoinSpec",
    "MAX_ALLOWED_FANOUT",
    "CompilerError",
    "UnresolvedReferenceError",
    "IncompatiblePortError",
    "FanoutLimitExceededError",
    "InvalidCachePolicyError",
]

# Automatically register compiler module in default registry
if not default_registry.has("nexura.runtime.compiler"):
    default_registry.register(WorkflowCompilerModule)
