# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Base module abstraction and protocol."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict
from typing import Any

from nexura_agents.contracts.context import ExecutionContext
from nexura_agents.contracts.events import ModuleEventType
from nexura_agents.contracts.manifest import ModuleManifest
from nexura_agents.contracts.result import ModuleResult


class BaseModule(ABC):
    """Abstract base class for all Nexura modules.

    Enforces contract validation before and after invocation, ensuring that
    invalid inputs/configurations cause zero side effects.
    """

    @property
    @abstractmethod
    def manifest(self) -> ModuleManifest:
        """Module manifest defining metadata, schemas, and policies."""
        ...

    @abstractmethod
    async def invoke(
        self,
        context: ExecutionContext,
        inputs: dict[str, Any],
        config: dict[str, Any],
    ) -> ModuleResult:
        """Execute the module's core logic with validated inputs and config."""
        ...

    async def execute(
        self,
        context: ExecutionContext,
        inputs: dict[str, Any],
        config: dict[str, Any],
    ) -> ModuleResult:
        """Standard execution pipeline: validation, lifecycle events, and invocation."""
        context.check_cancelled()

        # Schema validations happen before any invocation or side-effects
        self.manifest.validate_config(config)
        self.manifest.validate_inputs(inputs)

        context.emit_event(
            ModuleEventType.INVOKE_STARTED,
            {"inputs": inputs, "config": config},
            module_id=self.manifest.module_id,
            version=self.manifest.version,
        )

        try:
            result = await self.invoke(context, inputs, config)

            if result.status == "success":
                self.manifest.validate_outputs(result.outputs)
                completed: dict[str, Any] = {
                    "outputs": result.outputs,
                    "status": result.status,
                    "usage": asdict(result.usage) if result.usage is not None else None,
                }
                model_id = (result.metadata or {}).get("model_id")
                if isinstance(model_id, str) and model_id:
                    completed["model_id"] = model_id  # lets traces price the usage
                context.emit_event(
                    ModuleEventType.INVOKE_COMPLETED,
                    completed,
                    module_id=self.manifest.module_id,
                    version=self.manifest.version,
                )
            else:
                context.emit_event(
                    ModuleEventType.INVOKE_FAILED,
                    {
                        "error_code": result.error_code,
                        "error_message": result.error_message,
                        "status": result.status,
                        "usage": asdict(result.usage) if result.usage is not None else None,
                    },
                    module_id=self.manifest.module_id,
                    version=self.manifest.version,
                )
            return result
        except BaseException as exc:
            context.emit_event(
                ModuleEventType.INVOKE_FAILED,
                {"error": str(exc), "type": type(exc).__name__},
                module_id=self.manifest.module_id,
                version=self.manifest.version,
            )
            raise
