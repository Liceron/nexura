# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Typed exceptions for evaluation datasets and regression runner."""

from __future__ import annotations

from typing import Any

from nexura_agents.errors import NexuraError


class EvaluationError(NexuraError):
    """Base exception for evaluation and regression runner errors."""


class DatasetNotFoundError(EvaluationError):
    """Raised when an evaluation dataset ID or version is unknown."""

    def __init__(self, dataset_id: str, version: str | None = None) -> None:
        v_str = f" (version {version})" if version else ""
        super().__init__(f"Evaluation dataset '{dataset_id}'{v_str} was not found.")
        self.dataset_id = dataset_id
        self.version = version


class DatasetValidationError(EvaluationError):
    """Raised when an evaluation dataset fails schema or example validation."""


class GraderExecutionError(EvaluationError):
    """Raised when a grader or judge fails during evaluation execution."""

    def __init__(
        self, grader_name: str, message: str, original_error: BaseException | None = None
    ) -> None:
        super().__init__(f"Grader '{grader_name}' failed: {message}", original_error=original_error)
        self.grader_name = grader_name


class JudgeUnauthorizedEffectError(EvaluationError):
    """Raised when a model judge attempts to emit tool calls or execute side effects."""

    def __init__(self, judge_name: str, tool_name: str) -> None:
        super().__init__(
            f"Security denial: Model judge '{judge_name}' attempted to invoke tool '{tool_name}'. "
            "Judges are strictly prohibited from authorizing or triggering tool effects."
        )
        self.judge_name = judge_name
        self.tool_name = tool_name


class EvaluationBudgetExceededError(EvaluationError):
    """Raised when an evaluation batch exceeds configured cost, token, or turn budgets."""

    def __init__(self, current_value: Any, max_value: Any, limit_type: str = "cost") -> None:
        super().__init__(
            f"Evaluation batch halted: {limit_type} limit exceeded "
            f"({current_value!r} > {max_value!r})"
        )
        self.limit_type = limit_type
        self.current_value = current_value
        self.max_value = max_value


class RegressionDetectedError(EvaluationError):
    """Raised when evaluation scores regress below baseline thresholds."""

    def __init__(self, metric_name: str, baseline: float, current: float) -> None:
        super().__init__(
            f"Regression detected in metric '{metric_name}': current score {current:.3f} "
            f"fell below baseline {baseline:.3f}"
        )
        self.metric_name = metric_name
        self.baseline = baseline
        self.current = current
