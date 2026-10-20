"""B43: every approval-gated node kind re-gates when its resolved inputs change after pausing.

Connector writes already re-gated (tests/test_workflow_approval_binding.py); these cover the
other gated kinds: plain, mapped and parallel function nodes, a generic module node with
``requires_approval=True`` and a sub-workflow node. An edit that does not change what the
node reads still runs on the original approval.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from nexura_agents.contracts.base import BaseModule
from nexura_agents.contracts.context import ExecutionContext
from nexura_agents.contracts.manifest import ModuleManifest
from nexura_agents.contracts.result import ModuleResult

from nexura_runtime.workflow import (
    ModuleRegistry,
    SQLiteWorkflowStore,
    WorkflowEngine,
    WorkflowNode,
    WorkflowSpec,
)


def _engine(tmp_path: Path, registry: ModuleRegistry, node: WorkflowNode) -> WorkflowEngine:
    return WorkflowEngine(
        WorkflowSpec(name="b43", entry_node=node.id, nodes={node.id: node}),
        registry,
        store=SQLiteWorkflowStore(tmp_path / "b43.db"),
    )


async def _edit_regates(engine: WorkflowEngine, start: dict[str, Any], edit: dict[str, Any]):
    paused = await engine.run(start)
    assert paused.status == "paused"
    again = await engine.resume(
        paused.workflow_run_id, paused.pending_approval.approval_id, state_update=edit
    )
    assert again.status == "paused", "edited inputs must not run on the old approval"
    assert again.pending_approval.approval_id != paused.pending_approval.approval_id
    assert again.pending_approval.payload_digest != paused.pending_approval.payload_digest
    return again


async def test_plain_function_node_unchanged_edit_still_runs(tmp_path: Path) -> None:
    seen: list[str] = []
    reg = ModuleRegistry()
    reg.register("send", lambda state: seen.append(state["to"]) or {"sent": True})
    eng = _engine(tmp_path, reg, WorkflowNode(id="n", handler="send", requires_approval=True))
    paused = await eng.run({"to": "a"})
    done = await eng.resume(
        paused.workflow_run_id, paused.pending_approval.approval_id, state_update={"to": "a"}
    )
    assert done.status == "completed" and seen == ["a"]


async def test_plain_function_node_any_state_edit_regates(tmp_path: Path) -> None:
    # A function handler receives the whole state, so even a key it may not use re-gates.
    seen: list[dict[str, Any]] = []
    reg = ModuleRegistry()
    reg.register("send", lambda state: seen.append(dict(state)) or {"sent": True})
    eng = _engine(tmp_path, reg, WorkflowNode(id="n", handler="send", requires_approval=True))
    again = await _edit_regates(eng, {"to": "a"}, {"note": "added"})
    assert seen == []
    assert again.pending_approval.input_state["note"] == "added"
    done = await eng.resume(again.workflow_run_id, again.pending_approval.approval_id)
    assert done.status == "completed" and [s["note"] for s in seen] == ["added"]


async def test_mapped_function_node_item_edit_regates(tmp_path: Path) -> None:
    sent: list[str] = []
    reg = ModuleRegistry()
    reg.register("send", lambda state: sent.append(state["item"]) or {})
    node = WorkflowNode(id="n", handler="send", map_over="items", requires_approval=True)
    eng = _engine(tmp_path, reg, node)
    again = await _edit_regates(eng, {"items": ["a", "b"]}, {"items": ["a", "b", "evil"]})
    assert sent == []
    done = await eng.resume(again.workflow_run_id, again.pending_approval.approval_id)
    assert done.status == "completed" and sorted(sent) == ["a", "b", "evil"]


async def test_parallel_function_node_edit_regates(tmp_path: Path) -> None:
    ran: list[str] = []
    reg = ModuleRegistry()
    reg.register("one", lambda state: ran.append(f"one:{state['x']}") or {"one": True})
    reg.register("two", lambda state: ran.append(f"two:{state['x']}") or {"two": True})
    node = WorkflowNode(id="n", handler="", parallel=["one", "two"], requires_approval=True)
    eng = _engine(tmp_path, reg, node)
    again = await _edit_regates(eng, {"x": 1}, {"x": 2})
    assert ran == []
    done = await eng.resume(again.workflow_run_id, again.pending_approval.approval_id)
    assert done.status == "completed" and sorted(ran) == ["one:2", "two:2"]


class _Recorder(BaseModule):
    """A generic module with no declared writes: gated only by ``requires_approval``."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._manifest = ModuleManifest(
            module_id="test.b43.recorder",
            version="1.0.0",
            kind="integration",
            title="Recorder",
            description="Records its inputs.",
            input_schema={"type": "object", "properties": {"amount": {"type": "integer"}}},
            effect_policy="read",
        )

    @property
    def manifest(self) -> ModuleManifest:
        return self._manifest

    async def invoke(
        self, context: ExecutionContext, inputs: dict[str, Any], config: dict[str, Any]
    ) -> ModuleResult:
        self.calls.append(dict(inputs))
        return ModuleResult.success({"ok": True})


async def test_module_node_requires_approval_binds_resolved_inputs(tmp_path: Path) -> None:
    mod = _Recorder()
    reg = ModuleRegistry()
    reg.register("rec", mod)
    node = WorkflowNode(
        id="n", handler="rec", input_mapping={"amount": "amount"}, requires_approval=True
    )
    eng = _engine(tmp_path, reg, node)
    # A key the module does not read leaves the approval standing...
    paused = await eng.run({"amount": 10})
    done = await eng.resume(
        paused.workflow_run_id, paused.pending_approval.approval_id, state_update={"memo": "x"}
    )
    assert done.status == "completed" and mod.calls == [{"amount": 10}]
    # ...while a change to a mapped input needs a fresh approval.
    mod.calls.clear()
    again = await _edit_regates(eng, {"amount": 10}, {"amount": 9999})
    assert mod.calls == []
    done = await eng.resume(again.workflow_run_id, again.pending_approval.approval_id)
    assert done.status == "completed" and mod.calls == [{"amount": 9999}]


async def test_subgraph_node_binds_the_inputs_it_passes(tmp_path: Path) -> None:
    ran: list[Any] = []
    reg = ModuleRegistry()
    reg.register("inner", lambda state: ran.append(state.get("amount")) or {"done": True})
    child = WorkflowSpec(
        name="child", entry_node="i", nodes={"i": WorkflowNode(id="i", handler="inner")}
    )
    node = WorkflowNode(
        id="n",
        handler="",
        subgraph=child,
        input_mapping={"amount": "amount"},
        requires_approval=True,
    )
    eng = _engine(tmp_path, reg, node)
    paused = await eng.run({"amount": 5})
    done = await eng.resume(
        paused.workflow_run_id, paused.pending_approval.approval_id, state_update={"memo": "x"}
    )
    assert done.status == "completed" and ran == [5]

    ran.clear()
    again = await _edit_regates(eng, {"amount": 5}, {"amount": 500})
    assert ran == []
    done = await eng.resume(again.workflow_run_id, again.pending_approval.approval_id)
    assert done.status == "completed" and ran == [500]
