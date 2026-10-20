# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Evaluation runner executing test datasets across modules and agents with cost bounds."""

from __future__ import annotations

import inspect
import json
import re
import time
from decimal import Decimal
from typing import Any

from nexura_agents.contracts.base import BaseModule
from nexura_agents.contracts.context import ExecutionContext
from nexura_agents.usage.manager import BudgetManager, RunBudget
from nexura_agents.usage.models import BudgetConfig, TokenUsage
from nexura_agents.usage.pricing import PricingTable

from nexura_runtime.evaluation.errors import EvaluationBudgetExceededError
from nexura_runtime.evaluation.graders import Grader
from nexura_runtime.evaluation.models import (
    DatasetExample,
    EvaluationDataset,
    EvaluationSummary,
    ExampleEvaluationResult,
    GraderResult,
)
from nexura_runtime.observability.run_diagnostics import _TEXT_RULES

# PII Redaction Patterns
_CARD_REGEX = re.compile(r"\b(?:\d[ -]*?){13,16}\b")
_BEARER_REGEX = re.compile(r"Bearer\s+[A-Za-z0-9\-._~+/]{16,}=*", re.IGNORECASE)
_EMAIL_REGEX = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")


# Shared credential rules, minus the generic "Bearer/Basic/Token <word>" rule, which also
# rewrites ordinary prose ("Verify your Bearer token") that evaluation outputs legitimately
# contain. Long scheme credentials are covered by the stricter pattern below.
_SHARED_RULES = tuple(r for r in _TEXT_RULES if r[1] != r"\1 [REDACTED]")
_AUTH_SCHEME_REGEX = re.compile(r"(?i)\b(Bearer|Basic|Digest|Token)\s+[A-Za-z0-9._~+/=\-]{16,}")


def redact_pii(text: str) -> str:
    """Mask credentials (shared redaction), payment cards, tokens and email addresses."""
    cleaned = _BEARER_REGEX.sub("Bearer [REDACTED_TOKEN]", text)
    cleaned = _AUTH_SCHEME_REGEX.sub(r"\1 [REDACTED]", cleaned)
    for pattern, repl in _SHARED_RULES:
        cleaned = pattern.sub(repl, cleaned)
    cleaned = _CARD_REGEX.sub("[REDACTED_CARD]", cleaned)
    cleaned = _EMAIL_REGEX.sub("[REDACTED_EMAIL]", cleaned)
    return cleaned


class EvaluationRunner:
    """Orchestrates test dataset execution against agents, workflows, or modules.

    Enforces:
    1. Pre-execution budget limits across evaluation batches.
    2. Zero unauthorized judge side effects.
    3. Reproducible seeds and version tracking.
    4. Automated PII sanitization.
    5. Clear labeling of synthetic judge evidence.
    """

    def __init__(
        self,
        graders: list[Grader] | None = None,
        *,
        dataset: EvaluationDataset | None = None,
        budget_manager: BudgetManager | None = None,
        budget_config: BudgetConfig | None = None,
        pricing_table: PricingTable | None = None,
        seed: int | None = 42,
        redact_data: bool = True,
        strict_budget: bool = False,
    ) -> None:
        self.graders = graders or []
        self.dataset = dataset
        self.budget_config = budget_config
        self.pricing_table = pricing_table
        if budget_manager is not None:
            self.budget_manager = budget_manager
        elif pricing_table is not None:
            self.budget_manager = BudgetManager(pricing_table=pricing_table)
        else:
            self.budget_manager = BudgetManager()
        self.seed = seed
        self.redact_data = redact_data
        self.strict_budget = strict_budget

    async def run(
        self,
        target: Any,
        dataset: EvaluationDataset | None = None,
        *,
        run_id: str | None = None,
        workflow_version: str | None = None,
        spec_hash: str | None = None,
        run_budget: RunBudget | None = None,
        context_extra: dict[str, Any] | None = None,
    ) -> EvaluationSummary:
        """Convenience alias for run_evaluation with optional bound dataset."""
        ds = dataset or self.dataset
        if ds is None:
            raise ValueError("No dataset specified for run.")
        return await self.run_evaluation(
            target,
            ds,
            run_id=run_id,
            workflow_version=workflow_version,
            spec_hash=spec_hash,
            run_budget=run_budget,
            context_extra=context_extra,
        )

    async def run_evaluation(
        self,
        target: Any,
        dataset: EvaluationDataset,
        *,
        run_id: str | None = None,
        workflow_version: str | None = None,
        spec_hash: str | None = None,
        run_budget: RunBudget | None = None,
        context_extra: dict[str, Any] | None = None,
    ) -> EvaluationSummary:
        """Run all dataset examples against target and return an aggregated EvaluationSummary."""
        exec_budget = run_budget
        if exec_budget is None and self.budget_config is not None:
            exec_budget = self.budget_manager.create_run_budget(
                workspace_id="evaluation_workspace",
                run_id=run_id or f"eval_{int(time.time())}",
                config=self.budget_config,
            )

        results: list[ExampleEvaluationResult] = []
        passed_count = 0
        failed_count = 0
        total_latency = 0.0
        total_cost = Decimal("0")
        total_tokens = 0
        budget_halted = False
        is_synthetic_judge_used = False
        last_spent_usd = Decimal("0")

        for example in dataset.examples:
            # Check budget limits before dispatching each example
            if exec_budget is not None and exec_budget.is_exhausted():
                budget_halted = True
                if self.strict_budget:
                    status = exec_budget.get_status()
                    raise EvaluationBudgetExceededError(
                        status.effective_cost_usd,
                        self.budget_config.max_cost_usd if self.budget_config else "limit",
                        limit_type="cost",
                    )
                break

            ex_result = await self._evaluate_single_example(
                target,
                example,
                exec_budget=exec_budget,
                prev_spent_usd=last_spent_usd,
                context_extra=context_extra,
            )

            if exec_budget is not None:
                last_spent_usd = exec_budget.get_status().total_spent_usd

            # Check if any grader was a synthetic judge
            for g in ex_result.graders:
                if g.is_synthetic_judge:
                    is_synthetic_judge_used = True

            if ex_result.passed:
                passed_count += 1
            else:
                failed_count += 1

            total_latency += ex_result.latency_ms
            if ex_result.total_tokens:
                total_tokens += ex_result.total_tokens

            results.append(ex_result)

            if exec_budget is not None and exec_budget.is_exhausted():
                budget_halted = True
                break

        total_examples = len(results)
        pass_rate = (passed_count / total_examples) if total_examples > 0 else 0.0
        mean_score = (
            sum(r.overall_score for r in results) / total_examples if total_examples > 0 else 0.0
        )
        total_cost = last_spent_usd

        return EvaluationSummary(
            run_id=run_id or f"eval_{dataset.dataset_id}_{dataset.version}",
            dataset_id=dataset.dataset_id,
            dataset_version=dataset.version,
            dataset_digest=dataset.content_hash,
            dataset_hash=dataset.content_hash,
            workflow_version=workflow_version,
            spec_hash=spec_hash,
            seed=self.seed,
            timestamp=time.time(),
            total_examples=total_examples,
            passed_examples=passed_count,
            failed_examples=failed_count,
            pass_rate=pass_rate,
            mean_score=round(mean_score, 4),
            total_latency_ms=round(total_latency, 2),
            total_cost_usd=total_cost,
            total_tokens=total_tokens,
            results=results,
            is_synthetic_evidence=is_synthetic_judge_used,
            budget_halted=budget_halted,
            metadata={
                "redact_data": self.redact_data,
                "example_pool_total": len(dataset.examples),
            },
        )

    async def _evaluate_single_example(
        self,
        target: Any,
        example: DatasetExample,
        *,
        exec_budget: RunBudget | None = None,
        prev_spent_usd: Decimal = Decimal("0"),
        context_extra: dict[str, Any] | None = None,
    ) -> ExampleEvaluationResult:
        """Execute and grade an individual example."""
        start_t = time.perf_counter()
        actual_output: Any = None
        error_msg: str | None = None
        usage: Any = None
        cost_usd = Decimal("0")
        call_context: dict[str, Any] = {**(context_extra or {})}

        try:
            # 1. Dispatch target
            actual_output, usage, meta = await self._invoke_target(target, example)
            call_context.update(meta)

            # 2. Account budget
            if exec_budget is not None and usage is not None:
                token_usage = (
                    usage
                    if isinstance(usage, TokenUsage)
                    else TokenUsage(
                        prompt_tokens=getattr(usage, "prompt_tokens", None),
                        completion_tokens=getattr(usage, "completion_tokens", None),
                        total_tokens=getattr(usage, "total_tokens", None),
                    )
                )
                # Target usage is not settled through provider admission, so it must count.
                exec_budget.record_usage(
                    token_usage, model_id=call_context.get("model_id"), entry_category="direct"
                )
                status = exec_budget.get_status()
                cost_usd = status.total_spent_usd - prev_spent_usd

        except Exception as exc:
            error_msg = str(exc)
            actual_output = None

        elapsed_ms = (time.perf_counter() - start_t) * 1000.0

        # Redact output if configured
        clean_output = actual_output
        if self.redact_data and isinstance(actual_output, str):
            clean_output = redact_pii(actual_output)
        elif self.redact_data and isinstance(actual_output, dict):
            clean_output = json.loads(redact_pii(json.dumps(actual_output)))

        # 3. Grade output
        grader_results: list[GraderResult] = []
        overall_score = 0.0
        if error_msg:
            # Execution error: all graders fail
            for grader in self.graders:
                grader_results.append(
                    GraderResult(
                        grader_name=grader.name,
                        grader_kind=grader.kind,
                        passed=False,
                        score=0.0,
                        reason=f"Target execution failed: {error_msg}",
                    )
                )
            passed = False
            overall_score = 0.0
        else:
            for grader in self.graders:
                g_res = await grader.grade(example, clean_output, context=call_context)
                grader_results.append(g_res)

            # Failed grader cannot report pass: must satisfy ALL configured graders
            passed = all(g.passed for g in grader_results) if grader_results else True
            scores = [g.score for g in grader_results]
            overall_score = (sum(scores) / len(scores)) if scores else (1.0 if passed else 0.0)

        tok_count = usage.total_tokens if usage else None
        p_tok = (
            usage.prompt_tokens
            if hasattr(usage, "prompt_tokens")
            else getattr(usage, "input_tokens", None)
        )
        c_tok = (
            usage.completion_tokens
            if hasattr(usage, "completion_tokens")
            else getattr(usage, "output_tokens", None)
        )

        return ExampleEvaluationResult(
            example_id=example.example_id,
            passed=passed,
            actual_output=clean_output,
            graders=grader_results,
            overall_score=round(overall_score, 4),
            latency_ms=round(elapsed_ms, 2),
            prompt_tokens=p_tok,
            completion_tokens=c_tok,
            total_tokens=tok_count,
            cost_usd=cost_usd,
            error=error_msg,
            metadata=call_context,
        )

    async def _invoke_target(
        self,
        target: Any,
        example: DatasetExample,
    ) -> tuple[Any, Any, dict[str, Any]]:
        """Invoke agent, callable, or module uniformly."""
        meta: dict[str, Any] = {}
        usage: Any = None
        input_data = example.input_data

        if isinstance(target, BaseModule):
            ctx = ExecutionContext(run_id="eval_run", node_id="eval_node")
            mod_res = await target.invoke(ctx, input_data, {})
            output = mod_res.outputs
            usage = mod_res.usage
            meta["status"] = mod_res.status
            return output, usage, meta

        if hasattr(target, "run_async"):
            # SDK Runner or Agent
            res = await target.run_async(input_data)
            output = getattr(res, "final_output", res)
            usage = getattr(res, "usage", None)
            return output, usage, meta

        if callable(target):
            try:
                if inspect.iscoroutinefunction(target):
                    out = await target(example)
                else:
                    out = target(example)
            except (TypeError, AttributeError):
                if inspect.iscoroutinefunction(target):
                    out = await target(input_data)
                else:
                    out = target(input_data)

            # Check if output is a dict with special fields
            if isinstance(out, dict):
                output = out.get("output", out)
                if "citations" in out:
                    meta["citations"] = out["citations"]
                if "usage" in out:
                    usage = out["usage"]
                if "model_id" in out:
                    meta["model_id"] = out["model_id"]
                if "mutating_tool_calls_count" in out:
                    meta["mutating_tool_calls_count"] = out["mutating_tool_calls_count"]
                return output, usage, meta
            return out, None, meta

        return str(target), None, meta

    @staticmethod
    def compare_runs(
        baseline: EvaluationSummary,
        current: EvaluationSummary,
        *,
        min_pass_rate_drop: float = 0.05,
    ) -> dict[str, Any]:
        """Compare two evaluation summaries to detect regressions and version drift."""
        workflow_version_changed = baseline.workflow_version != current.workflow_version
        spec_hash_changed = (baseline.spec_hash or baseline.workflow_spec_hash) != (
            current.spec_hash or current.workflow_spec_hash
        )
        dataset_changed = (baseline.dataset_digest or baseline.dataset_hash) != (
            current.dataset_digest or current.dataset_hash
        )

        pass_rate_diff = current.pass_rate - baseline.pass_rate
        mean_score_diff = current.mean_score - baseline.mean_score
        regression_detected = pass_rate_diff < -min_pass_rate_drop

        # Map example transitions
        base_map = {r.example_id: r.passed for r in baseline.results}
        regressed_examples = []
        fixed_examples = []

        for r in current.results:
            b_passed = base_map.get(r.example_id)
            if b_passed is True and not r.passed:
                regressed_examples.append(r.example_id)
            elif b_passed is False and r.passed:
                fixed_examples.append(r.example_id)

        has_regressions = bool(
            regressed_examples
            or regression_detected
            or (pass_rate_diff < 0)
            or (mean_score_diff < 0)
        )

        return {
            "regression_detected": regression_detected,
            "has_regressions": has_regressions,
            "pass_rate_baseline": baseline.pass_rate,
            "pass_rate_current": current.pass_rate,
            "pass_rate_delta": round(pass_rate_diff, 4),
            "pass_rate_diff": round(pass_rate_diff, 4),
            "mean_score_diff": round(mean_score_diff, 4),
            "version_changed": workflow_version_changed,
            "workflow_version_changed": workflow_version_changed,
            "spec_hash_changed": spec_hash_changed,
            "dataset_changed": dataset_changed,
            "baseline_version": baseline.workflow_version,
            "current_version": current.workflow_version,
            "baseline_spec_hash": baseline.spec_hash,
            "current_spec_hash": current.spec_hash,
            "regressed_examples": regressed_examples,
            "fixed_examples": fixed_examples,
        }
