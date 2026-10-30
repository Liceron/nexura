# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""B00/B01 Module wrapper for Workflow Compiler."""

from __future__ import annotations

from typing import Any

from nexura_agents.contracts.base import BaseModule
from nexura_agents.contracts.context import ExecutionContext
from nexura_agents.contracts.manifest import ModuleManifest
from nexura_agents.contracts.result import ModuleResult

from nexura_runtime.runtime.compiler.compiler import WorkflowCompiler


class WorkflowCompilerModule(BaseModule):
    """Module wrapper exposing the workflow compiler via B00/B01 contract."""

    manifest = ModuleManifest(
        schema_version=1,
        module_id="nexura.runtime.compiler",
        version="1.0.0",
        kind="compiler",
        title="Workflow Compiler and Port Validator",
        description=(
            "Validates typed ports, references, and control flow, producing "
            "an immutable release plan with pinned hashes."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "spec": {"type": "object"},
            },
            "required": ["spec"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {
                "workflow_id": {"type": "string"},
                "version": {"type": "string"},
                "release_digest": {"type": "string"},
                "nodes_count": {"type": "integer"},
            },
            "required": ["workflow_id", "version", "release_digest", "nodes_count"],
            "additionalProperties": False,
        },
        effect_policy="pure",
    )

    def __init__(self, registry: Any = None) -> None:
        self._registry = registry

    async def invoke(
        self,
        context: ExecutionContext,
        inputs: dict[str, Any],
        config: dict[str, Any] | None = None,
    ) -> ModuleResult:
        compiler = WorkflowCompiler(registry=self._registry)
        plan = compiler.compile(inputs["spec"])
        return ModuleResult(
            outputs={
                "workflow_id": plan.workflow_id,
                "version": plan.version,
                "release_digest": plan.release_digest,
                "nodes_count": len(plan.nodes),
            }
        )
