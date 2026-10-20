# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Data models for durable workflow execution and worker leases."""

from __future__ import annotations

import time
from typing import Any, Literal

from pydantic import BaseModel, Field

EffectPolicy = Literal["pure", "read", "idempotent_write", "non_idempotent_write"]


class NodeIntent(BaseModel):
    """Pre-dispatch execution intent recording an impending effect."""

    node_id: str
    effect_policy: str
    dispatched: bool = False
    timestamp: float = Field(default_factory=time.time)


class WorkerLease(BaseModel):
    """Active worker lease with fencing token for concurrency control."""

    worker_id: str
    fencing_token: int
    acquired_at: float
    expires_at: float


class DurableNodeSpec(BaseModel):
    """Specification of an executable node in a durable workflow."""

    node_id: str
    handler: str
    effect_policy: str = "pure"
    timeout_seconds: float | None = None
    fuel_cost: int = 1


class DurableWorkflowSpec(BaseModel):
    """Specification of a durable workflow graph."""

    workflow_id: str
    nodes: dict[str, DurableNodeSpec]
    entry_node: str
    terminal_nodes: list[str] = Field(default_factory=list)
    transitions: dict[str, Any] = Field(default_factory=dict)
    default_fuel_budget: int = 100
    default_time_budget_seconds: float = 60.0


class DurableRunState(BaseModel):
    """Authoritative durable state of a workflow run across worker lifecycles."""

    workspace_id: str
    run_id: str
    workflow_id: str
    status: str = "pending"  # pending, running, paused, completed, failed, cancelled
    revision: int = 1
    fencing_token: int = 0
    worker_id: str | None = None
    lease_expires_at: float = 0.0
    current_node_id: str | None = None
    completed_nodes: list[str] = Field(default_factory=list)
    state: dict[str, Any] = Field(default_factory=dict)
    fuel_remaining: int = 100
    fuel_consumed: int = 0
    time_budget_remaining: float = 60.0
    time_consumed: float = 0.0
    pre_dispatch_intent: NodeIntent | None = None
    error: str | None = None
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
