# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Offline workflow replay for :mod:`nexura_agents.repro` bundles.

The SDK records and replays agent runs on its own; workflow bundles need the
workflow engine, so the runtime registers this replayer when it is imported.
"""

from __future__ import annotations

import copy
from typing import Any

from nexura_runtime.workflow import (
    MemoryWorkflowStore,
    ModuleRegistry,
    WorkflowEngine,
    WorkflowSpec,
)


async def replay_workflow(
    data: dict[str, Any],
    consume: Any,
    budget: Any,
    gates: list[dict[str, Any]],
    divergence: type[BaseException],
) -> Any:
    """Replay a recorded workflow through inert node fixtures.

    ``consume`` returns the recorded output for a node (or raises its recorded
    error); ``gates`` is the recorded approval-gate list, consumed in place so the
    caller can detect leftovers; ``divergence`` is raised when the run departs from
    the recording.
    """
    _Divergence = divergence

    # Each recorded node is an opaque fixture boundary. Keep graph routing,
    # but remove module/composite dispatch so no module can be instantiated.
    replay_spec = copy.deepcopy(data["spec"])
    required = set(data.get("approval_required_handlers", []))
    for node in replay_spec["nodes"].values():
        node["requires_approval"] = (
            node.get("requires_approval", False)
            or node.get("handler") in required
            or node.get("module_id") in required
        )
        node["handler"] = node["id"]
        for key in ("module_id", "module_version", "subgraph", "parallel", "map_over"):
            node.pop(key, None)
    workflow_spec = WorkflowSpec.from_dict(replay_spec)
    registry = ModuleRegistry()
    for node in workflow_spec.nodes.values():
        registry.register_handler(node.handler)(lambda state: {})
    executor: Any = WorkflowEngine(
        workflow_spec,
        registry,
        store=MemoryWorkflowStore(),
        max_steps=data.get("max_steps", 50),
        budget=budget,
    )

    def gate(node: Any, state: Any) -> bool:
        index = next((i for i, g in enumerate(gates) if g["node"] == node.id), None)
        if index is None:
            raise _Divergence(f"Approval gate diverged at {node.id}")
        observed = gates.pop(index)
        return bool(observed["needed"])

    async def step(node: Any, handler: Any, state: Any, *args: Any) -> Any:
        return consume(node.id, state)

    def map_write_cap(node: Any, state: Any) -> None:
        # Composite execution stays opaque; reproduce its pre-approval
        # gate check at the same boundary as the original engine.
        if data["spec"]["nodes"][node.id].get("map_over"):
            gate(node, state)

    executor._run_step = step
    executor._write_operation_gated = gate
    executor._check_map_write_cap = map_write_cap
    result = await executor.run(data["inputs"])
    for decision in data["approvals"]:
        result = await executor.resume(
            result.workflow_run_id, result.pending_approval.approval_id, **decision
        )
    return result
