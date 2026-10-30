# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Data structures and domain models for evaluation datasets, graders, and runs."""

from __future__ import annotations

import time
import uuid
from decimal import Decimal
from typing import Any, Literal

from nexura_agents.contracts.canonical import canonical_digest, canonical_json
from pydantic import BaseModel, ConfigDict, Field, model_validator

GraderKind = Literal["deterministic", "model_judge"]
QualityEvidence = Literal["verified_human", "model_judge", "mock_simulated"]

TrajectoryMode = Literal["exact", "subset", "in_order", "any_order", "strict"]


class ArgumentMatch(BaseModel):
    """Rules for one argument (keyed by dotted path or ``tool.path``)."""

    model_config = ConfigDict(extra="forbid")
    normalisers: list[Literal["strip", "casefold", "whitespace", "numeric"]] = Field(
        default_factory=list
    )
    absolute_tolerance: float = Field(default=0, ge=0, allow_inf_nan=False)
    relative_tolerance: float = Field(default=0, ge=0, allow_inf_nan=False)


class TrajectoryStep(BaseModel):
    """Expected or observed tool invocation; expected args are a required subset."""

    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1)
    args: dict[str, Any] = Field(default_factory=dict)


class UserSimulation(BaseModel):
    """Multi-turn user plan. Persona models are injected by the host, never named here."""

    model_config = ConfigDict(extra="forbid")
    turns: list[str] | None = None
    persona: str | None = None
    max_turns: int = Field(default=10, ge=1, le=100)
    stop_on_agent: list[str] = Field(default_factory=list)
    stop_on_user: list[str] = Field(default_factory=lambda: ["[DONE]"])

    @model_validator(mode="after")
    def one_source(self) -> UserSimulation:
        if (self.turns is None) == (self.persona is None):
            raise ValueError("Set exactly one of turns or persona")
        if any(not text for text in self.stop_on_agent + self.stop_on_user):
            raise ValueError("Stop markers must be non-empty")
        return self


class StoredTrace(BaseModel):
    """Portable snapshot of a recorded run, separate from its evaluation expectations."""

    run_id: str
    workflow_id: str | None = None
    agent_id: str | None = None
    timestamp: float = Field(allow_inf_nan=False)
    output: Any = None
    events: list[dict[str, Any]] = Field(default_factory=list)
    messages: list[dict[str, Any]] = Field(default_factory=list)


class OnlineEvalConfig(BaseModel):
    """Stable hash sampling and inclusive UTC epoch-second filters over stored traces."""

    model_config = ConfigDict(extra="forbid")
    sampling_rate: float = Field(default=1, ge=0, le=1, allow_inf_nan=False)
    seed: int = 42
    workflow_id: str | None = None
    agent_id: str | None = None
    start_time: float | None = Field(default=None, allow_inf_nan=False)
    end_time: float | None = Field(default=None, allow_inf_nan=False)

    @model_validator(mode="after")
    def time_range(self) -> OnlineEvalConfig:
        if (
            self.start_time is not None
            and self.end_time is not None
            and self.start_time > self.end_time
        ):
            raise ValueError("start_time must be <= end_time")
        return self


class DatasetExample(BaseModel):
    """Single test case or gold sample within an evaluation dataset."""

    example_id: str = Field(default_factory=lambda: f"ex_{uuid.uuid4().hex[:10]}")
    input_data: dict[str, Any]
    expected_output: Any | None = None
    expected_citations: list[str] | None = None
    expected_trajectory: list[TrajectoryStep | str] | None = None
    simulation: UserSimulation | None = None
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class EvaluationDataset(BaseModel):
    """Versioned immutable dataset containing test examples."""

    dataset_id: str
    version: str
    description: str = ""
    created_at: float = Field(default_factory=time.time)
    examples: list[DatasetExample] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    content_hash: str = ""

    def model_post_init(self, __context: Any) -> None:
        """Compute canonical content digest if not already set."""
        if not self.content_hash:
            canonical_repr = canonical_json(
                {
                    "dataset_id": self.dataset_id,
                    "version": self.version,
                    "examples": [e.model_dump(mode="json") for e in self.examples],
                }
            )
            self.content_hash = canonical_digest(canonical_repr)


class GraderResult(BaseModel):
    """Outcome produced by an individual deterministic grader or model judge."""

    grader_name: str
    grader_kind: GraderKind
    passed: bool
    score: float = Field(ge=0.0, le=1.0)
    reason: str = ""
    details: dict[str, Any] = Field(default_factory=dict)
    is_synthetic_judge: bool = False
    quality_evidence: QualityEvidence = "verified_human"


class ExampleEvaluationResult(BaseModel):
    """Result of running and grading a single dataset example."""

    example_id: str
    passed: bool
    actual_output: Any
    graders: list[GraderResult] = Field(default_factory=list)
    overall_score: float = 0.0
    latency_ms: float = 0.0
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    cost_usd: Decimal = Decimal("0")
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def grader_results(self) -> list[GraderResult]:
        """Convenience alias for graders list."""
        return self.graders


class EvaluationSummary(BaseModel):
    """Aggregated machine-readable evaluation report across a dataset."""

    run_id: str = Field(default_factory=lambda: f"eval_{uuid.uuid4().hex[:12]}")
    dataset_id: str
    dataset_version: str
    dataset_digest: str = ""
    dataset_hash: str = ""
    workflow_version: str | None = None
    spec_hash: str | None = None
    workflow_spec_hash: str | None = None
    seed: int | None = None
    timestamp: float = Field(default_factory=time.time)
    total_examples: int = 0
    passed_examples: int = 0
    failed_examples: int = 0
    pass_rate: float = 0.0
    mean_score: float = 0.0
    grader_scores: dict[str, float] = Field(default_factory=dict)
    total_latency_ms: float = 0.0
    total_cost_usd: Decimal = Decimal("0")
    total_tokens: int = 0
    results: list[ExampleEvaluationResult] = Field(default_factory=list)
    is_synthetic_evidence: bool = False
    budget_halted: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)

    def model_post_init(self, __context: Any) -> None:
        if not self.dataset_digest and self.dataset_hash:
            self.dataset_digest = self.dataset_hash
        elif not self.dataset_hash and self.dataset_digest:
            self.dataset_hash = self.dataset_digest

        if not self.spec_hash and self.workflow_spec_hash:
            self.spec_hash = self.workflow_spec_hash
        elif not self.workflow_spec_hash and self.spec_hash:
            self.workflow_spec_hash = self.spec_hash

    @property
    def cost_usd(self) -> Decimal:
        """Convenience alias for total_cost_usd."""
        return self.total_cost_usd
