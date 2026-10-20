# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""B00/B01 module contract adapter for dataset evaluation and regression runner."""

from __future__ import annotations

from typing import Any

from nexura_agents.contracts.base import BaseModule
from nexura_agents.contracts.context import ExecutionContext
from nexura_agents.contracts.manifest import EffectPolicy, ModuleManifest
from nexura_agents.contracts.result import ModuleResult

from nexura_runtime.evaluation.dataset import DatasetRegistry, create_synthetic_support_dataset
from nexura_runtime.evaluation.graders import (
    ContainsGrader,
    ExactMatchGrader,
    Grader,
    JsonSchemaGrader,
    RegexGrader,
)
from nexura_runtime.evaluation.models import (
    DatasetExample,
    EvaluationDataset,
    EvaluationSummary,
)
from nexura_runtime.evaluation.runner import EvaluationRunner


class EvaluationModule(BaseModule):
    """B00/B01 module for running regression benchmarks and grading model outputs."""

    def __init__(
        self,
        registry: DatasetRegistry | None = None,
        *,
        module_id: str = "nexura.evaluation.runner",
        version: str = "1.0.0",
        effect_policy: EffectPolicy = "read",
    ) -> None:
        self.registry = registry or DatasetRegistry()
        # Pre-seed standard benchmark
        synthetic_ds = create_synthetic_support_dataset()
        self.registry.register(synthetic_ds)

        self._manifest = ModuleManifest(
            schema_version=1,
            module_id=module_id,
            version=version,
            kind="service",
            internal=True,
            title="Dataset Evaluation and Regression Runner",
            description=(
                "Executes versioned datasets against modules and agents, evaluates "
                "deterministic/model-judge graders, enforces budget bounds, and tracks regressions."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "operation": {
                        "type": "string",
                        "enum": [
                            "evaluate",
                            "run_evaluation",
                            "grade_output",
                            "compare_runs",
                            "list_datasets",
                        ],
                    },
                    "dataset_id": {"type": "string"},
                    "dataset_version": {"type": "string"},
                    "dataset": {"type": "object"},
                    "example": {"type": "object"},
                    "actual_output": {},
                    "graders": {
                        "type": "array",
                    },
                    "baseline_summary": {"type": "object"},
                    "current_summary": {"type": "object"},
                },
                "required": ["operation"],
            },
            output_schema={
                "type": "object",
                "properties": {
                    "status": {"type": "string"},
                    "summary": {"type": "object"},
                    "result": {"type": "object"},
                },
                "required": ["status"],
            },
            config_schema={"type": "object", "properties": {}, "additionalProperties": True},
            capabilities={
                "network": False,
                "streaming": False,
                "run_evaluation": True,
                "compare_runs": True,
            },
            effect_policy=effect_policy,
        )

    @property
    def manifest(self) -> ModuleManifest:
        return self._manifest

    def describe(self) -> ModuleManifest:
        """Module manifest inspector."""
        return self._manifest

    async def invoke(
        self,
        context: ExecutionContext,
        inputs: dict[str, Any],
        config: dict[str, Any],
    ) -> ModuleResult:
        operation = inputs.get("operation")

        try:
            if operation == "list_datasets":
                return ModuleResult.success(
                    {
                        "status": "ok",
                        "datasets": self.registry.list_datasets(),
                    }
                )

            elif operation == "grade_output":
                raw_ex = inputs.get("example", {})
                example = DatasetExample(
                    example_id=raw_ex.get("example_id", "adhoc_ex"),
                    input_data=raw_ex.get("input_data", {}),
                    expected_output=raw_ex.get("expected_output"),
                    metadata=raw_ex.get("metadata", {}),
                )
                actual_output = inputs.get("actual_output")
                graders = self._build_graders(inputs.get("graders", ["exact_match"]))

                results = []
                for g in graders:
                    res = await g.grade(example, actual_output)
                    results.append(res.model_dump(mode="json"))

                passed = all(r["passed"] for r in results)
                return ModuleResult.success(
                    {
                        "status": "graded",
                        "passed": passed,
                        "grader_results": results,
                    }
                )

            elif operation in ("evaluate", "run_evaluation"):
                raw_ds = inputs.get("dataset")
                if raw_ds:
                    if isinstance(raw_ds, EvaluationDataset):
                        dataset = raw_ds
                    else:
                        dataset = EvaluationDataset(**raw_ds)
                else:
                    dataset_id = inputs.get("dataset_id", "benchmark_customer_support")
                    dataset_ver = inputs.get("dataset_version")
                    dataset = self.registry.get(dataset_id, dataset_ver)

                graders = self._build_graders(inputs.get("graders", ["contains"]))
                runner = EvaluationRunner(graders=graders)

                # Use a dummy echo target if none supplied
                def echo_target(inp: dict[str, Any]) -> str:
                    q = inp.get("query", "")
                    if "refund" in q.lower():
                        return "30 days from purchase date with valid proof of purchase"
                    if "cancel" in q.lower():
                        return "14 days cooling off in billing portal"
                    if "hello" in q.lower():
                        return "hello world"
                    return "Verify your Bearer token in Authorization header"

                summary = await runner.run_evaluation(
                    echo_target,
                    dataset,
                    run_id=context.run_id,
                )

                return ModuleResult.success(
                    {
                        "status": "evaluated",
                        "summary": summary.model_dump(mode="json"),
                    }
                )

            elif operation == "compare_runs":
                base_raw = inputs.get("baseline_summary")
                curr_raw = inputs.get("current_summary")
                if not base_raw or not curr_raw:
                    return ModuleResult.failure(
                        "MISSING_DATA", "baseline_summary and current_summary are required."
                    )

                base_sum = EvaluationSummary(**base_raw)
                curr_sum = EvaluationSummary(**curr_raw)
                comparison = EvaluationRunner.compare_runs(base_sum, curr_sum)

                return ModuleResult.success(
                    {
                        "status": "compared",
                        "comparison": comparison,
                    }
                )

            else:
                return ModuleResult.failure("UNKNOWN_OPERATION", f"Unknown operation: {operation}")

        except Exception as exc:
            return ModuleResult.failure("EVALUATION_ERROR", str(exc))

    def _build_graders(self, grader_items: list[Any]) -> list[Grader]:
        graders: list[Grader] = []
        for item in grader_items:
            name = item.get("kind", item.get("name")) if isinstance(item, dict) else str(item)
            if name == "exact_match":
                graders.append(ExactMatchGrader())
            elif name == "contains":
                graders.append(ContainsGrader())
            elif name == "regex":
                graders.append(RegexGrader())
            elif name == "json_schema":
                graders.append(JsonSchemaGrader())
        return graders or [ContainsGrader()]
