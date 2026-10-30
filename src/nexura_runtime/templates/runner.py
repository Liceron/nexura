# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Run a template's test fixture offline on the real workflow engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from nexura_runtime.templates.catalog import Template
from nexura_runtime.templates.handlers import template_registry
from nexura_runtime.workflow import MemoryWorkflowStore, WorkflowEngine, WorkflowSpec


@dataclass
class FixtureOutcome:
    """The result of one fixture run: failures is empty when every expectation held."""

    name: str
    status: str
    state: dict[str, Any]
    path: list[str]
    failures: list[str] = field(default_factory=list)


def _dig(state: dict[str, Any], dotted: str) -> Any:
    value: Any = state
    for part in dotted.split("."):
        value = value[int(part)] if isinstance(value, list) else value[part]
    return value


async def run_fixture(template: Template) -> list[FixtureOutcome]:
    """Execute every fixture run: start, decide any approval, check the expectations."""
    outcomes: list[FixtureOutcome] = []
    for case in template.fixture["runs"]:
        store = MemoryWorkflowStore()
        engine = WorkflowEngine(
            WorkflowSpec.from_dict(template.new_spec()), template_registry(), store=store
        )
        result = await engine.run(case["input"], workflow_run_id=f"fx-{template.id}")
        gate = case.get("approval")
        if gate is not None:
            pending = result.pending_approval
            if result.status != "paused" or pending is None:
                outcomes.append(
                    FixtureOutcome(case["name"], result.status, result.state, [], ["no approval"])
                )
                continue
            result = await engine.resume(
                result.workflow_run_id,
                pending.approval_id,
                decision=gate["decision"],
                actor="fixture",
                actor_role="admin",
            )
        history = await engine.history(result.workflow_run_id)
        path: list[str] = [template.spec["entry_node"]]
        for cp in history:
            if cp.current_node_id and (not path or path[-1] != cp.current_node_id):
                path.append(cp.current_node_id)
        outcome = FixtureOutcome(case["name"], result.status, result.state, path)
        _check(case, outcome)
        outcomes.append(outcome)
    return outcomes


def _check(case: dict[str, Any], outcome: FixtureOutcome) -> None:
    fails = outcome.failures
    if outcome.status != case["expect_status"]:
        fails.append(f"status {outcome.status!r} != {case['expect_status']!r}")
    for key, want in case.get("expect_state", {}).items():
        try:
            got = _dig(outcome.state, key)
        except (KeyError, IndexError, ValueError, TypeError):
            fails.append(f"state[{key}] missing")
            continue
        if got != want:
            fails.append(f"state[{key}] = {got!r}, expected {want!r}")
    for key, needle in case.get("expect_contains", {}).items():
        if needle not in str(outcome.state.get(key, "")):
            fails.append(f"state[{key}] does not contain {needle!r}")
    for key in case.get("expect_absent", []):
        if key in outcome.state:
            fails.append(f"state[{key}] should be absent")
    for node in case.get("expect_path", []):
        if node not in outcome.path:
            fails.append(f"node {node!r} not visited (path {outcome.path})")
