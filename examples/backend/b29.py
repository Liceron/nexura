"""B29 Standalone Example: Evaluation Datasets and Regression Runner.

Demonstrates:
1. Versioned evaluation dataset execution across 3 synthetic support cases.
2. Deterministic graders (Contains, Citations, ZeroSideEffects) and mock LLM judges.
3. Explicit quality evidence tagging ('mock_simulated' vs 'verified_human').
4. Cost budget enforcement and halting across batches.
5. Machine-readable export of structured evaluation summaries.
6. Module contract invocation via B01 registry.
"""

from __future__ import annotations

import asyncio
import tempfile
from decimal import Decimal
from pathlib import Path
from typing import Any

from nexura_runtime.contracts.context import ExecutionContext
from nexura_runtime.usage.models import BudgetConfig, ModelPricing, TokenUsage
from nexura_runtime.usage.pricing import PricingTable

from nexura_runtime.evaluation.dataset import create_synthetic_support_dataset
from nexura_runtime.evaluation.graders import (
    CitationCoverageGrader,
    ContainsGrader,
    Grader,
    ModelJudgeGrader,
    ZeroSideEffectsGrader,
)
from nexura_runtime.evaluation.models import DatasetExample
from nexura_runtime.evaluation.runner import EvaluationRunner
from nexura_runtime.modules.registry import default_registry


class SimulatedModelJudge:
    """Mock LLM judge for evaluation grading."""

    def __init__(self, model_id: str = "mock-judge-v1") -> None:
        self.model_id = model_id

    async def invoke_async(self, prompt: str) -> dict[str, Any]:
        return {
            "score": 0.95,
            "reason": "Accurate, helpful, and adheres strictly to corporate policy boundaries.",
        }


async def simulated_support_agent(example: DatasetExample) -> dict[str, Any]:
    """Target agent answering synthetic support queries."""
    query = str(example.input_data.get("query", "")).lower()

    if "refund" in query:
        return {
            "output": (
                "Our refund policy allows returns within 30 days of purchase "
                "with valid proof of purchase."
            ),
            "citations": ["doc_refund_policy_v2#chunk_1"],
            "mutating_tool_calls_count": 0,
            "usage": TokenUsage(prompt_tokens=42, completion_tokens=18, total_tokens=60),
            "model_id": "mock-support-agent",
        }
    elif "cancel" in query:
        return {
            "output": (
                "You can cancel your enterprise plan within 14 days by submitting "
                "a request in the billing portal."
            ),
            "citations": ["doc_terms_of_service#chunk_4"],
            "mutating_tool_calls_count": 0,
            "usage": TokenUsage(prompt_tokens=38, completion_tokens=22, total_tokens=60),
            "model_id": "mock-support-agent",
        }
    else:
        return {
            "output": (
                "For HTTP 401 Unauthorized errors, verify that your Bearer token is valid "
                "and passed in the Authorization header."
            ),
            "citations": ["doc_api_reference#chunk_2"],
            "mutating_tool_calls_count": 0,
            "usage": TokenUsage(prompt_tokens=50, completion_tokens=20, total_tokens=70),
            "model_id": "mock-support-agent",
        }


async def main() -> None:
    print("=" * 70)
    print("Nexura AgentKit - B29 Evaluation Datasets & Regression Runner")
    print("=" * 70)

    # 1. Load Synthetic Benchmark Dataset
    dataset = create_synthetic_support_dataset()
    print(f"\n[1] Loaded Dataset: '{dataset.dataset_id}' v{dataset.version}")
    print(f"    Description: {dataset.description}")
    print(f"    Examples: {len(dataset.examples)}")
    print(f"    Content Digest: {dataset.content_hash[:16]}...")

    # 2. Configure Graders
    graders: list[Grader] = [
        ContainsGrader(name="policy_check"),
        CitationCoverageGrader(min_coverage=1.0, name="citations"),
        ZeroSideEffectsGrader(name="safety_check"),
        ModelJudgeGrader(
            judge_model=SimulatedModelJudge(),
            name="simulated_semantic_judge",
        ),
    ]

    # Pricing table for cost accounting
    pricing_table = PricingTable(
        version="2026.10",
        date="2026-10-06",
        models={
            "mock-support-agent": ModelPricing(
                model_id="mock-support-agent",
                input_cost_per_million=Decimal("2.50"),
                output_cost_per_million=Decimal("10.00"),
            )
        },
    )

    budget_config = BudgetConfig(
        max_cost_usd=Decimal("0.05"),
    )

    runner = EvaluationRunner(
        graders=graders,
        dataset=dataset,
        budget_config=budget_config,
        pricing_table=pricing_table,
        seed=42,
    )

    # 3. Execute Benchmark Run
    print("\n[2] Executing Evaluation Run...")
    summary = await runner.run(simulated_support_agent, workflow_version="1.0.0")

    print(f"    Total Evaluated: {summary.total_examples}")
    print(f"    Passed: {summary.passed_examples} / {summary.total_examples}")
    print(f"    Pass Rate: {summary.pass_rate * 100:.1f}%")
    print(f"    Mean Quality Score: {summary.mean_score:.3f}")
    print(f"    Total Cost: ${summary.total_cost_usd:.6f}")
    print(f"    Total Tokens: {summary.total_tokens}")
    print(f"    Synthetic Judge Evidence: {summary.is_synthetic_evidence}")

    for res in summary.results:
        print(f"\n    Example: {res.example_id}")
        print(f"      Status: {'PASSED' if res.passed else 'FAILED'} (Score: {res.overall_score})")
        print(f"      Latency: {res.latency_ms:.2f}ms | Cost: ${res.cost_usd:.6f}")
        for g in res.graders:
            print(
                f"      - Grader '{g.grader_name}': "
                f"passed={g.passed}, score={g.score:.2f}, evidence='{g.quality_evidence}'"
            )

    # 4. Export Machine-Readable Scored Results
    output_path = Path(tempfile.gettempdir()) / "evaluation_benchmark_results.json"
    output_path.write_text(summary.model_dump_json(indent=2), encoding="utf-8")
    print(f"\n[3] Exported Scored Results -> {output_path.resolve()}")

    # 5. Invoke through B01 Module Contract
    print("\n[4] Invoking via B01 Module Contract ('nexura.evaluation.runner')...")
    module = default_registry.get("nexura.evaluation.runner")
    assert module is not None
    ctx = ExecutionContext(run_id="example_b29_eval", node_id="eval_node")

    mod_res = await module.execute(
        ctx,
        {
            "operation": "run_evaluation",
            "dataset_id": "benchmark_customer_support",
            "dataset_version": "1.0.0",
        },
        {},
    )
    print(f"    Module Status: {mod_res.status}")
    print(f"    Module Output Summary: {mod_res.outputs.get('summary', {}).get('run_id')}")

    print("\n" + "=" * 70)
    print("B29 Evaluation and Regression Runner executed successfully!")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
