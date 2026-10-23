# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Evaluation datasets, regression runner, and grading framework for Nexura AgentKit."""

from __future__ import annotations

from nexura_runtime.evaluation.dataset import (
    DatasetRegistry,
    create_synthetic_support_dataset,
)
from nexura_runtime.evaluation.errors import (
    DatasetNotFoundError,
    DatasetValidationError,
    EvaluationBudgetExceededError,
    EvaluationError,
    GraderExecutionError,
    JudgeUnauthorizedEffectError,
    RegressionDetectedError,
)
from nexura_runtime.evaluation.graders import (
    CitationCoverageGrader,
    ContainsGrader,
    ExactMatchGrader,
    Grader,
    JsonSchemaGrader,
    ModelJudgeGrader,
    RegexGrader,
    ToolArgumentAccuracyGrader,
    TrajectoryGrader,
    ZeroSideEffectsGrader,
    recorded_tool_calls,
)
from nexura_runtime.evaluation.models import (
    ArgumentMatch,
    DatasetExample,
    EvaluationDataset,
    EvaluationSummary,
    ExampleEvaluationResult,
    GraderKind,
    GraderResult,
    OnlineEvalConfig,
    QualityEvidence,
    StoredTrace,
    TrajectoryStep,
    UserSimulation,
)
from nexura_runtime.evaluation.module import EvaluationModule
from nexura_runtime.evaluation.runner import EvaluationRunner, redact_pii, simulate_user

__all__ = [
    # Errors
    "EvaluationError",
    "DatasetNotFoundError",
    "DatasetValidationError",
    "GraderExecutionError",
    "JudgeUnauthorizedEffectError",
    "EvaluationBudgetExceededError",
    "RegressionDetectedError",
    # Models
    "DatasetExample",
    "EvaluationDataset",
    "GraderKind",
    "QualityEvidence",
    "GraderResult",
    "ExampleEvaluationResult",
    "EvaluationSummary",
    # Graders
    "Grader",
    "ExactMatchGrader",
    "ContainsGrader",
    "RegexGrader",
    "JsonSchemaGrader",
    "CitationCoverageGrader",
    "ZeroSideEffectsGrader",
    "ModelJudgeGrader",
    "TrajectoryGrader",
    "ToolArgumentAccuracyGrader",
    "recorded_tool_calls",
    "ArgumentMatch",
    "TrajectoryStep",
    "UserSimulation",
    "StoredTrace",
    "OnlineEvalConfig",
    "simulate_user",
    # Dataset & Runner
    "DatasetRegistry",
    "create_synthetic_support_dataset",
    "EvaluationRunner",
    "redact_pii",
    # Module
    "EvaluationModule",
]

from nexura_runtime.modules.registry import default_registry

_default_eval_module = EvaluationModule()
if not default_registry.has(_default_eval_module.manifest.module_id):
    default_registry.register(_default_eval_module)
