"""Acceptance and integration tests for B29 evaluation datasets and regression runner."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from nexura_runtime.contracts.context import ExecutionContext
from nexura_runtime.usage.models import BudgetConfig, ModelPricing, TokenUsage
from nexura_runtime.usage.pricing import PricingTable

from nexura_runtime.evaluation.dataset import (
    DatasetRegistry,
    create_synthetic_support_dataset,
)
from nexura_runtime.evaluation.errors import (
    JudgeUnauthorizedEffectError,
)
from nexura_runtime.evaluation.graders import (
    CitationCoverageGrader,
    ContainsGrader,
    ExactMatchGrader,
    Grader,
    JsonSchemaGrader,
    ModelJudgeGrader,
    ZeroSideEffectsGrader,
)
from nexura_runtime.evaluation.models import (
    DatasetExample,
    EvaluationDataset,
    EvaluationSummary,
)
from nexura_runtime.evaluation.runner import EvaluationRunner
from nexura_runtime.modules.registry import default_registry


class FakeMockJudge:
    """Mock model representing an evaluation judge."""

    def __init__(
        self,
        model_id: str = "mock-judge-v1",
        return_payload: dict[str, Any] | None = None,
    ) -> None:
        self.model_id = model_id
        self.return_payload = return_payload or {
            "score": 0.92,
            "reason": "Clear and aligned with policy guidelines.",
        }

    async def invoke_async(self, prompt: str) -> dict[str, Any]:
        return self.return_payload


class AdversarialJudgeModel:
    """Mock judge attempting unauthorized tool execution."""

    def __init__(self) -> None:
        self.model_id = "adversarial-judge-3"

    async def invoke_async(self, prompt: str) -> dict[str, Any]:
        return {
            "score": 0.85,
            "reason": "Need to wipe logs first",
            "tool_calls": [{"name": "execute_shell", "args": {"cmd": "rm -rf /"}}],
        }


@pytest.mark.asyncio
async def test_fixed_fixtures_reproducible() -> None:
    """Assert fixed fixtures with deterministic graders yield identical scores and hashes."""
    dataset = create_synthetic_support_dataset()

    async def deterministic_target(example: DatasetExample) -> dict[str, Any]:
        # Perfectly produces expected output and citations
        return {
            "output": example.expected_output,
            "citations": example.expected_citations,
            "mutating_tool_calls_count": 0,
        }

    graders: list[Grader] = [
        ContainsGrader(
            required_substrings=None,  # Fall back to example.metadata["required_substrings"]
        ),
        CitationCoverageGrader(min_coverage=1.0),
        ZeroSideEffectsGrader(),
    ]

    runner1 = EvaluationRunner(dataset=dataset, graders=graders, seed=42)
    summary1 = await runner1.run(deterministic_target, workflow_version="1.0.0")

    runner2 = EvaluationRunner(dataset=dataset, graders=graders, seed=42)
    summary2 = await runner2.run(deterministic_target, workflow_version="1.0.0")

    assert summary1.total_examples == 3
    assert summary1.passed_examples == 3
    assert summary1.mean_score == 1.0
    assert summary2.mean_score == 1.0
    assert summary1.dataset_hash == summary2.dataset_hash
    assert summary1.dataset_hash == dataset.content_hash
    assert len(summary1.results) == len(summary2.results)
    for r1, r2 in zip(summary1.results, summary2.results, strict=True):
        assert r1.example_id == r2.example_id
        assert r1.passed is True
        assert r2.passed is True
        assert r1.overall_score == r2.overall_score == 1.0


@pytest.mark.asyncio
async def test_failed_grader_cannot_report_pass() -> None:
    """Assert any failing grader marks the example result as failed."""
    example = DatasetExample(
        example_id="ex_strict_match",
        input_data={"query": "test query"},
        expected_output="EXACT_SECRET_VALUE",
        metadata={"required_substrings": ["SECRET"]},
    )
    dataset = EvaluationDataset(
        dataset_id="strict_test",
        version="1.0.0",
        examples=[example],
    )

    # Output satisfies ContainsGrader ('SECRET') but fails ExactMatchGrader
    async def flawed_target(ex: DatasetExample) -> str:
        return "PARTIAL SECRET VALUE"

    graders: list[Grader] = [
        ContainsGrader(),
        ExactMatchGrader(),
    ]

    runner = EvaluationRunner(dataset=dataset, graders=graders)
    summary = await runner.run(flawed_target)

    assert summary.total_examples == 1
    assert summary.passed_examples == 0
    assert summary.pass_rate == 0.0

    result = summary.results[0]
    assert result.passed is False
    assert len(result.grader_results) == 2

    contains_res = next(g for g in result.grader_results if g.grader_name == "contains")
    exact_res = next(g for g in result.grader_results if g.grader_name == "exact_match")
    assert contains_res.passed is True
    assert exact_res.passed is False


def test_changed_workflow_version_visible() -> None:
    """Assert compare_runs clearly displays version, score, and hash differences."""
    base_summary = EvaluationSummary(
        dataset_id="benchmark_customer_support",
        dataset_version="1.0.0",
        dataset_hash="hash_v1",
        workflow_version="1.0.0",
        workflow_spec_hash="spec_hash_aaa",
        total_examples=3,
        passed_examples=3,
        failed_examples=0,
        pass_rate=1.0,
        mean_score=1.0,
        results=[],
    )

    current_summary = EvaluationSummary(
        dataset_id="benchmark_customer_support",
        dataset_version="1.0.0",
        dataset_hash="hash_v1",
        workflow_version="1.1.0-candidate",
        workflow_spec_hash="spec_hash_bbb",
        total_examples=3,
        passed_examples=2,
        failed_examples=1,
        pass_rate=0.6667,
        mean_score=0.72,
        results=[],
    )

    diff = EvaluationRunner.compare_runs(base_summary, current_summary)

    assert diff["version_changed"] is True
    assert diff["baseline_version"] == "1.0.0"
    assert diff["current_version"] == "1.1.0-candidate"
    assert diff["spec_hash_changed"] is True
    assert diff["pass_rate_diff"] < 0
    assert diff["mean_score_diff"] < 0
    assert diff["has_regressions"] is True


@pytest.mark.asyncio
async def test_cost_bound_holds_across_evaluation_batches() -> None:
    """Assert evaluation batches strictly respect budget limits and halt safely."""
    examples = [
        DatasetExample(
            example_id=f"ex_{i}",
            input_data={"query": f"question {i}"},
            expected_output="valid output",
        )
        for i in range(5)
    ]
    dataset = EvaluationDataset(
        dataset_id="batch_dataset",
        version="1.0.0",
        examples=examples,
    )

    pricing_table = PricingTable(
        version="1.0.0",
        date="2026-10-06",
        models={
            "mock-llm": ModelPricing(
                model_id="mock-llm",
                input_cost_per_million=Decimal("100.00"),  # $0.0001 per token
                output_cost_per_million=Decimal("200.00"),
            )
        },
    )

    # Budget config with $0.0002 limit
    # Each run with 1 input token costs $0.0001
    # After 2 runs, spent = $0.0002 and budget is exhausted
    budget_config = BudgetConfig(
        max_cost_usd=Decimal("0.00020"),
    )

    eval_graders: list[Grader] = [ExactMatchGrader()]
    runner = EvaluationRunner(
        dataset=dataset,
        graders=eval_graders,
        budget_config=budget_config,
        pricing_table=pricing_table,
    )

    async def expensive_target(ex: DatasetExample) -> dict[str, Any]:
        return {
            "output": "valid output",
            "usage": TokenUsage(prompt_tokens=1, completion_tokens=0, total_tokens=1),
            "model_id": "mock-llm",
        }

    summary = await runner.run(expensive_target)

    assert summary.budget_halted is True
    # Should evaluate 2 examples before budget halts
    assert summary.total_examples == 2
    assert summary.passed_examples == 2
    assert summary.cost_usd <= Decimal("0.00025")


@pytest.mark.asyncio
async def test_fake_models_do_not_count_as_semantic_quality_evidence() -> None:
    """Assert fake/mock models are tagged with mock_simulated quality evidence."""
    fake_model = FakeMockJudge(model_id="mock-gpt4-mini")
    grader = ModelJudgeGrader(judge_model=fake_model)

    assert grader.is_synthetic is True

    example = DatasetExample(
        example_id="ex_test",
        input_data={"query": "test query"},
        expected_output="good response",
    )

    result = await grader.grade(example, "good response")

    assert result.passed is True
    assert result.is_synthetic_judge is True
    assert result.quality_evidence == "mock_simulated"
    assert str(result.quality_evidence) != "verified_human"
    assert str(result.quality_evidence) != "model_judge"


@pytest.mark.asyncio
async def test_judge_cannot_authorize_tool_effects() -> None:
    """Assert model judge attempting to emit tool calls raises security denial."""
    adversarial_model = AdversarialJudgeModel()
    grader = ModelJudgeGrader(judge_model=adversarial_model)

    example = DatasetExample(
        example_id="ex_security",
        input_data={"query": "safe query"},
        expected_output="safe response",
    )

    with pytest.raises(JudgeUnauthorizedEffectError) as exc_info:
        await grader.grade(example, "actual response")

    assert "strictly prohibited from authorizing or triggering tool effects" in str(exc_info.value)


@pytest.mark.asyncio
async def test_pii_redaction_in_evaluation_output() -> None:
    """Assert PII like credit cards and Bearer tokens are redacted in evaluation outputs."""
    example = DatasetExample(
        example_id="ex_pii",
        input_data={"query": "My card is 4532 8200 1234 5678 and token is Bearer a1b2c3d4e5f6g7h8"},
        expected_output="masked",
    )
    dataset = EvaluationDataset(
        dataset_id="pii_dataset",
        version="1.0.0",
        examples=[example],
    )

    async def pii_target(ex: DatasetExample) -> str:
        return (
            "Received card 4532-8200-1234-5678 and Bearer super_secret_long_auth_token_value_here"
        )

    runner = EvaluationRunner(
        dataset=dataset, graders=[ContainsGrader(required_substrings=["Received"])]
    )
    summary = await runner.run(pii_target)

    recorded_output = summary.results[0].actual_output
    assert "4532-8200-1234-5678" not in recorded_output
    assert "super_secret_long_auth_token_value_here" not in recorded_output
    assert "[REDACTED_CARD]" in recorded_output
    assert "[REDACTED_TOKEN]" in recorded_output


@pytest.mark.asyncio
async def test_json_schema_grader() -> None:
    """Assert JsonSchemaGrader validates structured outputs."""
    schema = {
        "type": "object",
        "required": ["status", "count"],
        "properties": {
            "status": {"type": "string"},
            "count": {"type": "integer"},
        },
    }
    grader = JsonSchemaGrader(schema=schema)
    example = DatasetExample(
        example_id="schema_ex",
        input_data={},
    )

    # Valid payload
    valid_res = await grader.grade(example, json.dumps({"status": "ok", "count": 42}))
    assert valid_res.passed is True
    assert valid_res.score == 1.0

    # Invalid payload (missing count)
    invalid_res = await grader.grade(example, json.dumps({"status": "ok"}))
    assert invalid_res.passed is False
    assert invalid_res.score == 0.0
    assert "Schema violations" in invalid_res.reason


def test_dataset_registry_persistence(tmp_path: Path) -> None:
    """Assert datasets can be saved and loaded deterministically via registry."""
    registry = DatasetRegistry(storage_dir=tmp_path)
    dataset = create_synthetic_support_dataset()

    file_path = registry.save(dataset)
    assert file_path.exists()

    loaded = registry.load("benchmark_customer_support", "1.0.0")
    assert loaded is not None
    assert loaded.dataset_id == dataset.dataset_id
    assert loaded.version == dataset.version
    assert loaded.content_hash == dataset.content_hash
    assert len(loaded.examples) == len(dataset.examples)


@pytest.mark.asyncio
async def test_evaluation_module_contract() -> None:
    """Assert EvaluationModule works through registry, describe, and execute."""
    module = default_registry.get("nexura.evaluation.runner")
    assert module is not None

    desc = module.manifest
    assert desc.module_id == "nexura.evaluation.runner"
    assert "run_evaluation" in desc.capabilities
    assert "compare_runs" in desc.capabilities

    # Execute run_evaluation via module contract
    ctx = ExecutionContext(run_id="test_run_eval", node_id="eval_node")
    exec_res = await module.execute(
        ctx,
        {
            "operation": "run_evaluation",
            "dataset": {
                "dataset_id": "inline_test",
                "version": "1.0.0",
                "examples": [
                    {
                        "example_id": "ex_contract",
                        "input_data": {"query": "hello"},
                        "expected_output": "hello world",
                    }
                ],
            },
            "graders": [{"kind": "exact_match"}],
        },
        {},
    )

    assert exec_res.status == "success"
    summary_data = exec_res.outputs["summary"]
    assert summary_data["dataset_id"] == "inline_test"
    assert summary_data["total_examples"] == 1
