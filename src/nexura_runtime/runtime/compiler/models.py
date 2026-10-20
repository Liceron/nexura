# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Data models for compiled workflows, ports, and control flow plans."""

from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel, Field


class PortBinding(BaseModel):
    """Binds an output port of a source node to an input port of a target node."""

    source_node: str
    source_port: str
    target_port: str


class CompiledNode(BaseModel):
    """An immutable, version-pinned workflow node."""

    node_id: str
    module_id: str
    module_version: str
    manifest_hash: str
    effect_policy: str = "pure"
    cacheable: bool = False
    config: dict[str, Any] = Field(default_factory=dict)
    bindings: list[PortBinding] = Field(default_factory=list)
    transitions: dict[str, str] = Field(default_factory=dict)
    default_transition: str | None = None
    fuel_cost: int = 1
    timeout_seconds: float | None = None


class ParallelMapSpec(BaseModel):
    """Specification for bounded fanout parallel execution over items."""

    node_id: str
    items_field: str
    item_var: str = "item"
    worker_node_id: str
    max_concurrency: int = 5
    output_field: str = "mapped_results"


class DeterministicJoinSpec(BaseModel):
    """Specification for deterministic joining of concurrent or reordered branches."""

    node_id: str
    incoming_branches: list[str]
    reducer: str = "sorted_merge"


class CompiledWorkflowPlan(BaseModel):
    """An immutable release bundle containing a fully validated workflow graph."""

    workflow_id: str
    version: str = "1.0.0"
    release_digest: str = ""
    entry_node_id: str
    terminal_node_ids: list[str] = Field(default_factory=list)
    nodes: dict[str, CompiledNode]
    parallel_maps: dict[str, ParallelMapSpec] = Field(default_factory=dict)
    joins: dict[str, DeterministicJoinSpec] = Field(default_factory=dict)
    max_cycles: int = 50
    default_fuel_budget: int = 100
    default_time_budget_seconds: float = 60.0
    created_at: float = Field(default_factory=time.time)
