"""Regression tests for review findings on composite steps (validate path)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from nexura_runtime.service.api.validation import validate_workflow_graph
from nexura_runtime.workflow import ModuleRegistry


def _registry() -> ModuleRegistry:
    reg = ModuleRegistry()

    def _h(state: Any = None) -> dict[str, Any]:
        return {}

    _h.manifest = SimpleNamespace(  # type: ignore[attr-defined]
        module_id="writer",
        effect_policy="non_idempotent_write",
        min_sdk_version="0.0.1",
        input_schema={},
        output_schema={},
        named_ports=[],
        validate_config=lambda c: None,
    )
    reg.register("writer", _h)
    return reg


def _spec(node: dict[str, Any]) -> dict[str, Any]:
    return {"name": "wf", "entry_node": "a", "nodes": {"a": {"id": "a", **node}}}


def test_effect_policy_counts_parallel_branches() -> None:
    plain = validate_workflow_graph(_spec({"handler": "writer"}), _registry())
    par = validate_workflow_graph(_spec({"handler": "", "parallel": ["writer"]}), _registry())
    assert plain.valid and par.valid, (plain.errors, par.errors)
    assert plain.estimated_effect_policy == "non_idempotent_write"
    assert par.estimated_effect_policy == "non_idempotent_write", par.estimated_effect_policy


def test_effect_policy_counts_subworkflow_steps() -> None:
    sub = {"name": "c", "entry_node": "w", "nodes": {"w": {"id": "w", "handler": "writer"}}}
    res = validate_workflow_graph(_spec({"handler": "", "subgraph": sub}), _registry())
    assert res.valid, res.errors
    assert res.estimated_effect_policy == "non_idempotent_write", res.estimated_effect_policy


def test_garbage_retry_is_reported_not_raised() -> None:
    node = {"handler": "", "parallel": ["writer"], "retry": {"max_attempts": "lots"}}
    res = validate_workflow_graph(_spec(node), _registry())  # must not raise
    assert res.valid is False


def test_fractional_max_concurrency_rejected() -> None:
    node = {"handler": "writer", "map_over": "items", "max_concurrency": 5.5}
    res = validate_workflow_graph(_spec(node), _registry())
    assert res.valid is False, (
        "5.5 is accepted (silently truncated to 5), but the inspector flags it"
    )


def test_parallel_with_map_over_rejected() -> None:
    node = {"handler": "", "parallel": ["writer"], "map_over": "items"}
    assert validate_workflow_graph(_spec(node), _registry()).valid is False


def test_bool_max_concurrency_rejected() -> None:
    node = {"handler": "writer", "map_over": "items", "max_concurrency": True}
    assert validate_workflow_graph(_spec(node), _registry()).valid is False


def test_garbage_retry_reports_error_code() -> None:
    node = {"handler": "", "parallel": ["writer"], "retry": {"max_attempts": "lots"}}
    res = validate_workflow_graph(_spec(node), _registry())
    assert any(e.code == "INVALID_COMPOSITE" for e in res.errors), res.errors


def test_integral_max_concurrency_still_accepted() -> None:
    node = {"handler": "writer", "map_over": "items", "max_concurrency": 5.0}
    assert validate_workflow_graph(_spec(node), _registry()).valid is True


def test_engine_refuses_fractional_concurrency_before_running() -> None:
    from nexura_runtime.workflow import WorkflowEngine, WorkflowSpec

    reg = ModuleRegistry()
    reg.register("h", lambda s: {"x": 1})
    spec = WorkflowSpec.from_dict(
        {
            "name": "w",
            "entry_node": "n",
            "terminal_nodes": ["n"],
            "nodes": {
                "n": {
                    "id": "n",
                    "handler": "h",
                    "map_over": "items",
                    "map_as": "it",
                    "max_concurrency": 5.5,
                }
            },
        }
    )
    with pytest.raises(Exception, match="max_concurrency"):
        WorkflowEngine(spec, reg)
