# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Classify-compatible operators and real workflow branch execution."""

import asyncio

import pytest
from nexura_agents.contracts.context import ExecutionContext
from nexura_agents.contracts.errors import SchemaValidationError

from nexura_runtime.modules import default_registry
from nexura_runtime.workflow import ModuleRegistry, WorkflowEngine, WorkflowNode, WorkflowSpec


@pytest.mark.parametrize(
    ("left", "operator", "right", "expected"),
    [
        (1, "eq", 1, True),
        (1, "ne", 1, False),
        (2, "gt", 1, True),
        (2, "gte", 2, True),
        (1, "lt", 2, True),
        (2, "lte", 2, True),
        ("a", "in", ["a", "b"], True),
        (["a"], "contains", "a", True),
        ("abc", "regex", "^a", True),
        (None, "gt", 1, False),
        (1, "contains", "a", False),
        (None, "eq", None, True),
    ],
)
def test_compare_and_if_else(run_component, left, operator, right, expected):
    inputs = {"left": left, "right": right, "operator": operator}
    assert run_component("nexura.logic.compare", **inputs) == {"result": expected}
    assert run_component("nexura.logic.if_else", **inputs) == {
        "result": expected,
        "route": "true" if expected else "false",
    }
    result = asyncio.run(
        default_registry.get("nexura.logic.if_else").invoke(
            ExecutionContext(run_id="r", node_id="n"), inputs
        )
    )
    assert result.branch == result.outputs["route"]


def test_switch_order_and_default(run_component):
    cases = [
        {"field": "x", "operator": "gt", "value": 1, "route": "first"},
        {"field": "x", "operator": "gt", "value": 2, "route": "second"},
    ]
    assert run_component("nexura.logic.switch", data={"x": 3}, cases=cases)["route"] == "first"
    assert run_component("nexura.logic.switch", data={"x": 0}, cases=cases)["route"] == "default"
    assert (
        run_component("nexura.logic.switch", data=0, cases=[], default_route="other")["route"]
        == "other"
    )
    module = default_registry.get("nexura.logic.switch")
    result = asyncio.run(
        module.invoke(
            ExecutionContext(run_id="r", node_id="n"),
            {"data": 1, "cases": [{"operator": "eq", "value": 1, "route": "selected"}]},
        )
    )
    assert result.branch == "selected"


@pytest.mark.parametrize(
    "inputs",
    [
        {"left": 1, "right": 2, "operator": "unknown"},
        {"left": 1, "right": "x", "operator": "gt"},
        {"left": "x", "right": "[", "operator": "regex"},
        {"left": "x", "right": 1, "operator": "regex"},
        {"left": "a" * 30000 + "!", "right": "(a+)+$", "operator": "regex"},
    ],
)
def test_logic_errors(run_component, inputs):
    with pytest.raises(SchemaValidationError):
        run_component("nexura.logic.compare", **inputs)


@pytest.mark.parametrize(("source", "selected"), [(" a,b ", True), (" a ", False)])
async def test_components_in_workflow_with_branch(source, selected):
    def node(name, module_id, inputs, outputs, **kwargs):
        return WorkflowNode(
            id=name,
            handler=module_id,
            module_id=module_id,
            module_version="1.0.0",
            input_mapping=inputs,
            output_mapping=outputs,
            **kwargs,
        )

    spec = WorkflowSpec(
        name="offline-components",
        entry_node="clean",
        nodes={
            "clean": node(
                "clean",
                "nexura.text.clean",
                {"text": "source"},
                {"text": "cleaned"},
                default_transition="split",
            ),
            "split": node(
                "split",
                "nexura.text.split",
                {"text": "cleaned", "separator": "separator"},
                {"items": "chunks"},
                default_transition="branch",
            ),
            "branch": node(
                "branch",
                "nexura.logic.if_else",
                {"left": "chunks", "right": "target", "operator": "operator"},
                {"result": "matched"},
                transitions={"true": "join", "false": "end"},
            ),
            "join": node(
                "join",
                "nexura.text.join",
                {"items": "chunks", "separator": "joiner"},
                {"text": "combined"},
                default_transition="end",
            ),
        },
        terminal_nodes=["end"],
    )
    result = await WorkflowEngine(spec, ModuleRegistry(versioned_registry=default_registry)).run(
        {"source": source, "separator": ",", "target": "b", "operator": "contains", "joiner": "-"}
    )
    assert result.status == "completed"
    assert result.state["matched"] is selected
    if selected:
        assert result.state["combined"] == "a-b"
        assert result.step_count == 4
    else:
        assert "combined" not in result.state
        assert result.step_count == 3
