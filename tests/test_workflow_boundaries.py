"""Independent acceptance cases for SDK deadline and terminal-state promises."""

import asyncio
from typing import Any

import pytest
from nexura_runtime.errors import WorkflowError, WorkflowTimeoutError

from nexura_runtime.workflow import (
    HandlerOutput,
    MemoryWorkflowStore,
    ModuleRegistry,
    WorkflowEngine,
    WorkflowNode,
    WorkflowSpec,
)


async def test_deadline_interrupts_a_single_terminal_handler() -> None:
    completed: list[bool] = []

    async def slow(state: dict[str, Any]) -> HandlerOutput:
        await asyncio.sleep(0.2)
        completed.append(True)
        return HandlerOutput(state_update={"finished": True})

    registry = ModuleRegistry()
    registry.register("slow", slow)
    spec = WorkflowSpec(
        name="single slow node", entry_node="slow", nodes={"slow": WorkflowNode("slow", "slow")}
    )
    engine = WorkflowEngine(spec, registry, store=MemoryWorkflowStore(), timeout=0.02)
    with pytest.raises(WorkflowTimeoutError):
        await engine.run()
    assert completed == []


async def test_existing_terminal_run_id_cannot_repeat_effects() -> None:
    effects: list[bool] = []

    async def effect(state: dict[str, Any]) -> HandlerOutput:
        effects.append(True)
        return HandlerOutput()

    registry = ModuleRegistry()
    registry.register("effect", effect)
    spec = WorkflowSpec(
        name="one effect", entry_node="effect", nodes={"effect": WorkflowNode("effect", "effect")}
    )
    engine = WorkflowEngine(spec, registry, store=MemoryWorkflowStore())
    await engine.run(workflow_run_id="fixed-id")
    with pytest.raises(WorkflowError):
        await engine.run(workflow_run_id="fixed-id")
    assert effects == [True]
