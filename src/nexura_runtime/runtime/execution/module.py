# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""B00/B01 Module wrapper for Durable Execution and Worker Lifecycle."""

from __future__ import annotations

from typing import Any

from nexura_agents.contracts.base import BaseModule
from nexura_agents.contracts.context import ExecutionContext
from nexura_agents.contracts.manifest import ModuleManifest
from nexura_agents.contracts.result import ModuleResult

from nexura_runtime.runtime.execution.engine import DurableExecutionEngine
from nexura_runtime.storage import get_storage_repository


class DurableExecutionModule(BaseModule):
    """Module interface for durable execution and worker management."""

    manifest = ModuleManifest(
        schema_version=1,
        module_id="nexura.runtime.execution",
        version="1.0.0",
        kind="executor",
        title="Durable Execution and Worker Engine",
        description=(
            "Provides durable run execution, worker leases with fencing tokens, "
            "and crash-safe checkpoint transitions."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["claim", "status", "cancel"],
                    "default": "status",
                },
                "run_id": {"type": "string"},
                "worker_id": {"type": "string", "default": "worker-cli-1"},
            },
            "required": ["run_id"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "status": {"type": "string"},
                "fencing_token": {"type": "integer"},
                "current_node_id": {"type": ["string", "null"]},
                "fuel_remaining": {"type": "integer"},
            },
            "required": ["run_id", "status", "fencing_token"],
            "additionalProperties": False,
        },
        config_schema={
            "type": "object",
            "properties": {
                "storage_uri": {"type": "string", "default": "sqlite://:memory:"},
            },
            "additionalProperties": False,
        },
        effect_policy="idempotent_write",
    )

    async def invoke(
        self,
        context: ExecutionContext,
        inputs: dict[str, Any],
        config: dict[str, Any] | None = None,
    ) -> ModuleResult:
        cfg = config or {}
        storage_uri = cfg.get("storage_uri", "sqlite://:memory:")
        repo = get_storage_repository(storage_uri)
        engine = DurableExecutionEngine(repo)

        run_id = inputs["run_id"]
        action = inputs.get("action", "status")
        worker_id = inputs.get("worker_id", "worker-cli-1")

        if action == "claim":
            state = await engine.claim_run(context.workspace_id, run_id, worker_id)
        elif action == "cancel":
            state = await engine.request_cancellation(context.workspace_id, run_id)
        else:
            state = await engine.get_state(context.workspace_id, run_id)

        await repo.close()
        return ModuleResult(
            outputs={
                "run_id": state.run_id,
                "status": state.status,
                "fencing_token": state.fencing_token,
                "current_node_id": state.current_node_id,
                "fuel_remaining": state.fuel_remaining,
            }
        )
