# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Data structures and domain models for evaluation datasets, graders, and runs."""

from __future__ import annotations

import time
import uuid
from decimal import Decimal
from typing import Any, Literal

from nexura_agents.contracts.canonical import canonical_digest, canonical_json
from pydantic import BaseModel, Field

GraderKind = Literal["deterministic", "model_judge"]
QualityEvidence = Literal["verified_human", "model_judge", "mock_simulated"]


class DatasetExample(BaseModel):
    """Single test case or gold sample within an evaluation dataset."""

    example_id: str = Field(default_factory=lambda: f"ex_{uuid.uuid4().hex[:10]}")
    input_data: dict[str, Any]
    expected_output: Any | None = None
    expected_citations: list[str] | None = None
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
