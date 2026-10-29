# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Evaluation runner executing test datasets across modules and agents with cost bounds."""

from __future__ import annotations

import dataclasses
import hashlib
import inspect
import json
import re
import time
import uuid
from collections.abc import Callable, Iterable
from decimal import Decimal
from typing import Any

from nexura_agents.agent import Agent
from nexura_agents.contracts.base import BaseModule
from nexura_agents.contracts.context import ExecutionContext
from nexura_agents.models.base import ModelProtocol
from nexura_agents.types import Message, Usage
from nexura_agents.usage.manager import BudgetManager, RunBudget, budget_call
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
    OnlineEvalConfig,
    StoredTrace,
    UserSimulation,
)
from nexura_runtime.observability.run_diagnostics import _TEXT_RULES, is_secret_key

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


def redact_value(value: Any) -> Any:
    """Redact content scalars without rewriting JSON syntax or object keys."""
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if is_secret_key(str(key)) else redact_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_value(item) for item in value]
    if isinstance(value, str):
        if value.lstrip().startswith(("{", "[")):
            try:
                return json.dumps(redact_value(json.loads(value)))
            except (ValueError, TypeError):
                pass
        return redact_pii(value)
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        text = str(value)
        cleaned = redact_pii(text)
        return cleaned if cleaned != text else value
    return value


def redact_trace(trace: dict[str, Any]) -> dict[str, Any]:
    """Keep recorded identities/timestamps intact while redacting captured content."""
    cleaned: dict[str, Any] = redact_value(trace)
    for field in ("run_id", "workflow_id", "agent_id", "timestamp"):
        if field in trace:
            cleaned[field] = trace[field]
    if isinstance(trace.get("runs"), list):
        cleaned["runs"] = [
            redact_trace(run) if isinstance(run, dict) else redact_value(run)
            for run in trace["runs"]
        ]
    if isinstance(trace.get("events"), list):
        events = []
        for event in trace["events"]:
            if not isinstance(event, dict):
                events.append(redact_value(event))
                continue
            item = redact_trace(event)
            data = event.get("data")
            if isinstance(data, dict):
                for field in ("tool_call_id", "tool_name"):
                    if field in data:
                        item["data"][field] = data[field]
            events.append(item)
        cleaned["events"] = events
    if isinstance(trace.get("messages"), list):
        messages = []
        for message in trace["messages"]:
            if not isinstance(message, dict):
                messages.append(redact_value(message))
                continue
            item = redact_value(message)
            if isinstance(message.get("tool_calls"), list):
                for call, redacted in zip(message["tool_calls"], item["tool_calls"], strict=True):
                    if not isinstance(call, dict):
                        continue
                    for field in ("id", "name"):
                        if field in call:
                            redacted[field] = call[field]
                    if isinstance(call.get("function"), dict) and "name" in call["function"]:
                        redacted["function"]["name"] = call["function"]["name"]
            messages.append(item)
        cleaned["messages"] = messages
    return cleaned


def _trace_json(run: Any) -> dict[str, Any]:
    if isinstance(run, dict):
        return run
    return {
        "run_id": getattr(run, "run_id", ""),
        "messages": [m.to_dict() for m in getattr(run, "messages", [])],
        "events": [
            {"type": str(e.type), "run_id": e.run_id, "timestamp": e.timestamp, "data": e.data}
            for e in getattr(run, "events", [])
            if str(e.type) not in ("run_completed", "run_started")
        ],
    }


async def simulate_user(
    agent: Any,
    plan: UserSimulation,
    *,
    model: ModelProtocol | None = None,
    stop: Callable[[list[Message]], bool] | None = None,
) -> dict[str, Any]:
    """Run isolated chat turns with history, bounded by plan and optional stop callback.

    Script exhaustion, markers, empty model output, callback, non-completed agent
    status and max_turns all stop the conversation. A persona model receives the
    conversation with roles reversed and no tools. Usage includes both participants.
    """
    if plan.persona is not None and model is None:
        raise ValueError("Persona simulation requires an injected user model")
    transcript: list[Message] = []
    history: list[Message] = []
    runs: list[dict[str, Any]] = []
    usage = Usage()
    output, reason = "", "max_turns"
    for turn in range(plan.max_turns):
        if plan.turns is not None:
            if turn >= len(plan.turns):
                reason = "script_exhausted"
                break
            text = plan.turns[turn]
        else:
            assert model is not None
            messages = [Message(role="system", content=plan.persona)] + [
                Message(role="assistant" if m.role == "user" else "user", content=m.content)
                for m in transcript
            ]
            if len(messages) == 1:
                messages.append(Message(role="user", content="Begin the conversation."))
            response = await model.generate(messages, tools=None)
            usage = usage.add(response.usage)
            if response.message.tool_calls:
                raise ValueError("Simulated user must return text, not tool calls")
            text = response.message.content or ""
        if not text.strip():
            reason = "empty_user"
            break
        if any(marker in text for marker in plan.stop_on_user):
            reason = "user_stop"
            break
        transcript.append(Message(role="user", content=text))
        result = await agent.run_async(text, history=history)
        history = list(result.messages)
        output = result.final_output
        usage = usage.add(result.usage)
        # Keep only this turn's messages: SDK history contains all preceding turns.
        trace = _trace_json(result)
        trace["messages"] = trace["messages"][len(runs[-1]["history"]) if runs else 0 :]
        runs.append({"trace": trace, "history": history})
        transcript.append(Message(role="assistant", content=output))
        if result.status != "completed":
            reason = f"agent_{result.status}"
            break
        if any(marker in output for marker in plan.stop_on_agent):
            reason = "agent_stop"
            break
        if stop is not None and stop(list(transcript)):
            reason = "callback"
            break
        if plan.turns is not None and turn + 1 == len(plan.turns):
            reason = "script_exhausted"
            break
    return {
        "output": output,
        "trace": {"runs": [r["trace"] for r in runs]},
        "transcript": [m.to_dict() for m in transcript],
        "turns": len(runs),
        "stop_reason": reason,
        "usage": dataclasses.asdict(usage),
    }


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
        user_model: ModelProtocol | None = None,
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
        self.user_model = user_model

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
            if exec_budget is not None and await budget_call(exec_budget, exec_budget.is_exhausted):
                budget_halted = True
                if self.strict_budget:
                    status = await budget_call(exec_budget, exec_budget.get_status)
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
                last_spent_usd = (
                    await budget_call(exec_budget, exec_budget.get_status)
                ).total_spent_usd

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

            if exec_budget is not None and await budget_call(exec_budget, exec_budget.is_exhausted):
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
            grader_scores={
                name: sum(g.score for r in results for g in r.graders if g.grader_name == name)
                / sum(g.grader_name == name for r in results for g in r.graders)
                for name in {g.grader_name for r in results for g in r.graders}
            },
            total_latency_ms=round(total_latency, 2),
            total_cost_usd=total_cost,
            total_tokens=total_tokens,
            results=results,
            is_synthetic_evidence=is_synthetic_judge_used
            or any(e.simulation for e in dataset.examples),
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
            if str(call_context.get("stop_reason", "")) in (
                "agent_paused",
                "agent_failed",
                "agent_cancelled",
            ):
                error_msg = f"Simulation stopped: {call_context['stop_reason']}"

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
                await budget_call(
                    exec_budget,
                    exec_budget.record_usage,
                    token_usage,
                    model_id=call_context.get("model_id"),
                    entry_category="direct",
                )
                status = await budget_call(exec_budget, exec_budget.get_status)
                cost_usd = status.total_spent_usd - prev_spent_usd

        except Exception as exc:
            error_msg = str(exc)
            actual_output = None

        elapsed_ms = (time.perf_counter() - start_t) * 1000.0

        # Redact output if configured
        clean_output = actual_output
        if self.redact_data:
            clean_output = redact_value(actual_output)

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
                g_res = await grader.grade(example, actual_output, context=call_context)
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

        # Grade original evidence, then redact every persisted output/detail/trace.
        if self.redact_data:
            error_msg = redact_pii(error_msg) if error_msg else error_msg
            original_trace = call_context.get("trace")
            call_context = redact_value(call_context)
            if isinstance(original_trace, dict):
                call_context["trace"] = redact_trace(original_trace)
            grader_results = [
                g.model_copy(
                    update={
                        "reason": redact_value(g.reason),
                        "details": redact_value(g.details),
                    }
                )
                for g in grader_results
            ]
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

        if example.simulation is not None:
            result = await simulate_user(target, example.simulation, model=self.user_model)
            return result["output"], Usage(**result.pop("usage")), result

        if isinstance(target, BaseModule):
            ctx = ExecutionContext(run_id="eval_run", node_id="eval_node")
            mod_res = await target.invoke(ctx, input_data, {})
            output = mod_res.outputs
            usage = mod_res.usage
            meta["status"] = mod_res.status
            return output, usage, meta

        if hasattr(target, "run_async"):
            # SDK Runner or Agent
            prompt = (
                input_data.get("prompt", input_data.get("query", json.dumps(input_data)))
                if isinstance(target, Agent)
                else input_data
            )
            res = await target.run_async(prompt)
            output = getattr(res, "final_output", res)
            usage = getattr(res, "usage", None)
            meta["trace"] = _trace_json(res)
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
                for field in ("transcript", "turns", "stop_reason"):
                    if field in out:
                        meta[field] = out[field]
                if "trace" in out:
                    meta["trace"] = _trace_json(out["trace"])
                elif "events" in out or "messages" in out:
                    meta["trace"] = _trace_json(out)
                return output, usage, meta
            if hasattr(out, "final_output"):
                return out.final_output, getattr(out, "usage", None), {"trace": _trace_json(out)}
            return out, None, meta

        return str(target), None, meta

    async def evaluate_traces(
        self,
        traces: Iterable[StoredTrace],
        example: DatasetExample,
        *,
        config: OnlineEvalConfig | None = None,
        expectation_for: Callable[[StoredTrace], DatasetExample] | None = None,
    ) -> EvaluationSummary:
        """Grade recorded runs without executing an agent; persist via EvaluationStore.save_run.

        ``expectation_for`` supplies case-specific expectations; otherwise ``example``
        is a shared policy. Sampling uses seed/run-id SHA256, independent of input order.
        """
        config = config or OnlineEvalConfig()
        selected: dict[str, StoredTrace] = {}
        for trace in traces:
            if config.workflow_id is not None and trace.workflow_id != config.workflow_id:
                continue
            if config.agent_id is not None and trace.agent_id != config.agent_id:
                continue
            if config.start_time is not None and trace.timestamp < config.start_time:
                continue
            if config.end_time is not None and trace.timestamp > config.end_time:
                continue
            digest = hashlib.sha256(f"{config.seed}:{trace.run_id}".encode()).digest()
            sample = int.from_bytes(digest, "big") / (1 << 256)
            if sample < config.sampling_rate:
                selected[trace.run_id] = trace
        cases = []
        for run_id, trace in sorted(selected.items()):
            expectation = expectation_for(trace) if expectation_for else example
            # Online grading must never dispatch simulation or re-execute a target.
            cases.append(expectation.model_copy(update={"example_id": run_id, "simulation": None}))
        dataset = EvaluationDataset(dataset_id="online_traces", version="1", examples=cases)

        def recorded(case: DatasetExample) -> dict[str, Any]:
            trace = selected[case.example_id]
            return {"output": trace.output, "trace": trace.model_dump(mode="json")}

        summary = await self.run(recorded, dataset, run_id=f"eval_online_{uuid.uuid4().hex}")
        summary.metadata.update(
            {
                "source": "stored_traces",
                "online": config.model_dump(),
                "source_run_ids": sorted(selected),
            }
        )
        summary.seed = config.seed
        return summary

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
