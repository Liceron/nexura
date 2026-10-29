# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""P40 item 1: a guardrail policy in shadow mode on workflow module nodes.

Shadow ``tool_input`` and ``tool_output`` rules and a shadow allowlist report, never refuse,
pause or withhold. Local fakes only (a module that records charges; no network).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from nexura_agents.contracts import BaseModule, ModuleManifest, ModuleResult
from nexura_agents.errors import WorkflowExecutionError
from nexura_agents.policy import _unlock_for_tests
from nexura_guardrails import GuardrailPolicy, MemoryAuditSink, Rule, When, pii

from nexura_runtime.workflow import (
    ModuleRegistry,
    SQLiteWorkflowStore,
    WorkflowEngine,
    WorkflowNode,
    WorkflowSpec,
)

CARD = "4111 1111 1111 1111"


@pytest.fixture(autouse=True)
def _clean() -> Iterator[None]:
    _unlock_for_tests()
    yield
    _unlock_for_tests()


class Charge(BaseModule):
    """A local fake non-idempotent write: records each charge in a list."""

    def __init__(self, charged: list[Any]) -> None:
        self.charged = charged
        self._manifest = ModuleManifest(
            module_id="fake.charge",
            version="1.0.0",
            kind="integration",
            title="charge",
            description="local fake charge",
            input_schema={
                "type": "object",
                "properties": {"amount": {"type": "integer"}},
                "required": ["amount"],
            },
            effect_policy="non_idempotent_write",
        )

    @property
    def manifest(self) -> ModuleManifest:
        return self._manifest

    async def invoke(
        self, context: Any, inputs: dict[str, Any], config: dict[str, Any]
    ) -> ModuleResult:
        self.charged.append(inputs["amount"])
        return ModuleResult.success({"receipt": "r-1", "card": CARD})


def engine(tmp_path: Path, charged: list[Any], policy: GuardrailPolicy) -> WorkflowEngine:
    registry = ModuleRegistry()
    registry.register("charge", Charge(charged))
    node = WorkflowNode(
        id="charge",
        handler="charge",
        input_mapping={"amount": "amount"},
        output_mapping={"receipt": "receipt", "card": "card"},
    )
    spec = WorkflowSpec(name="shadow", entry_node="charge", nodes={"charge": node})
    store = SQLiteWorkflowStore(tmp_path / "wf.sqlite3")
    return WorkflowEngine(spec, registry, store=store, guardrail_policy=policy)


@pytest.mark.parametrize("mode", ["shadow", "enforce"])
async def test_module_node_rules_report_in_shadow_and_act_in_enforce(
    tmp_path: Path, mode: str
) -> None:
    charged: list[Any] = []
    sink = MemoryAuditSink()
    policy = GuardrailPolicy(
        mode=mode,  # type: ignore[arg-type]
        checks={"pii": pii()},
        rules=[
            Rule(
                "ceiling",
                When(stage="tool_input", tools="charge", args={"$.amount": {"gt": 500}}),
                action="block",
            ),
            Rule(
                "approve-mid",
                When(stage="tool_input", tools="charge", args={"$.amount": {"gt": 100}}),
                action="require_approval",
            ),
            Rule("no-card", When(stage="tool_output"), check="pii", action="block"),
        ],
        audit=sink,
    )
    eng = engine(tmp_path, charged, policy)
    if mode == "enforce":
        with pytest.raises(WorkflowExecutionError, match="guardrail policy rule 'ceiling'"):
            await eng.run({"amount": 900}, workflow_run_id="big")
        paused = await eng.run({"amount": 200}, workflow_run_id="mid")
        assert paused.status == "paused" and charged == []
        return
    big = await eng.run({"amount": 900}, workflow_run_id="big")
    mid = await eng.run({"amount": 200}, workflow_run_id="mid")
    assert big.status == "completed" and mid.status == "completed"
    assert charged == [900, 200]  # not refused, not paused
    assert big.state["card"] == CARD  # not withheld
    seen = [(r.rule_id, r.action, r.mode, r.applied) for r in sink.records]
    assert ("ceiling", "block", "shadow", False) in seen
    assert ("approve-mid", "require_approval", "shadow", False) in seen
    assert ("no-card", "block", "shadow", False) in seen


async def test_a_shadow_allowlist_does_not_refuse_a_plain_handler(tmp_path: Path) -> None:
    ran: list[str] = []

    def plain(state: dict[str, Any]) -> dict[str, Any]:
        ran.append("plain")
        return {}

    registry = ModuleRegistry()
    registry.register("plain", plain, effect="pure")
    spec = WorkflowSpec(
        name="p", entry_node="p", nodes={"p": WorkflowNode(id="p", handler="plain")}
    )
    shadow = GuardrailPolicy(mode="shadow", default_tool_action="block")
    done = await WorkflowEngine(spec, registry, guardrail_policy=shadow).run({})
    assert done.status == "completed" and ran == ["plain"]
    enforced = GuardrailPolicy(default_tool_action="block")
    with pytest.raises(WorkflowExecutionError, match="default_tool_action"):
        await WorkflowEngine(spec, registry, guardrail_policy=enforced).run({})
    assert ran == ["plain"]


async def test_a_shadow_allowlist_reports_an_unlisted_module(tmp_path: Path) -> None:
    charged: list[Any] = []
    sink = MemoryAuditSink()
    policy = GuardrailPolicy(mode="shadow", default_tool_action="block", audit=sink)
    done = await engine(tmp_path, charged, policy).run({"amount": 3})
    assert done.status == "completed" and charged == [3]
    (record,) = sink.records
    assert record.rule_id == "default_tool_action" and record.action == "block"
    assert record.mode == "shadow" and record.applied is False
