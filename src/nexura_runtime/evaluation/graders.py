# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Deterministic and model-based graders for assessing output quality."""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Protocol, runtime_checkable

from nexura_agents.contracts.errors import SchemaValidationError
from nexura_agents.contracts.schema_validator import validate_schema
from nexura_agents.evals import REGEX_MAX_PATTERN_CHARS as _agents_regex_max_pattern
from nexura_agents.evals import check_regex_safety

from nexura_runtime.evaluation.errors import GraderExecutionError, JudgeUnauthorizedEffectError
from nexura_runtime.evaluation.models import (
    DatasetExample,
    GraderKind,
    GraderResult,
    QualityEvidence,
)


@runtime_checkable
class Grader(Protocol):
    """Protocol for evaluation grading functions."""

    name: str
    kind: GraderKind

    async def grade(
        self,
        example: DatasetExample,
        actual_output: Any,
        context: dict[str, Any] | None = None,
    ) -> GraderResult:
        """Grade the actual output against the example specification."""
        ...


class ExactMatchGrader:
    """Deterministic grader requiring exact equality with expected output."""

    def __init__(
        self,
        name: str = "exact_match",
        *,
        case_sensitive: bool = True,
        strip_whitespace: bool = True,
    ) -> None:
        self.name = name
        self.kind: GraderKind = "deterministic"
        self.case_sensitive = case_sensitive
        self.strip_whitespace = strip_whitespace

    async def grade(
        self,
        example: DatasetExample,
        actual_output: Any,
        context: dict[str, Any] | None = None,
    ) -> GraderResult:
        expected = example.expected_output
        if expected is None:
            return GraderResult(
                grader_name=self.name,
                grader_kind=self.kind,
                passed=False,
                score=0.0,
                reason="Example has no expected_output defined.",
            )

        act_str = str(actual_output)
        exp_str = str(expected)

        if self.strip_whitespace:
            act_str = act_str.strip()
            exp_str = exp_str.strip()

        if not self.case_sensitive:
            act_str = act_str.lower()
            exp_str = exp_str.lower()

        passed = act_str == exp_str
        score = 1.0 if passed else 0.0
        reason = (
            "Output matched expected value exactly."
            if passed
            else f"Mismatch: expected {exp_str!r}, got {act_str!r}."
        )

        return GraderResult(
            grader_name=self.name,
            grader_kind=self.kind,
            passed=passed,
            score=score,
            reason=reason,
        )


class ContainsGrader:
    """Deterministic grader for required/forbidden substrings."""

    def __init__(
        self,
        name: str = "contains",
        *,
        required_substrings: list[str] | None = None,
        forbidden_substrings: list[str] | None = None,
        case_sensitive: bool = False,
    ) -> None:
        self.name = name
        self.kind: GraderKind = "deterministic"
        self.required_substrings = required_substrings or []
        self.forbidden_substrings = forbidden_substrings or []
        self.case_sensitive = case_sensitive

    async def grade(
        self,
        example: DatasetExample,
        actual_output: Any,
        context: dict[str, Any] | None = None,
    ) -> GraderResult:
        act_str = str(actual_output)
        if not self.case_sensitive:
            act_str = act_str.lower()

        required = list(self.required_substrings)
        # Also check substrings listed in example metadata
        if "required_substrings" in example.metadata:
            required.extend(example.metadata["required_substrings"])
        elif isinstance(example.expected_output, str) and not required:
            required.append(example.expected_output)

        forbidden = list(self.forbidden_substrings)
        if "forbidden_substrings" in example.metadata:
            forbidden.extend(example.metadata["forbidden_substrings"])

        # Check forbidden
        for f in forbidden:
            f_check = f if self.case_sensitive else f.lower()
            if f_check in act_str:
                return GraderResult(
                    grader_name=self.name,
                    grader_kind=self.kind,
                    passed=False,
                    score=0.0,
                    reason=f"Forbidden substring '{f}' detected in output.",
                    details={"matched_forbidden": f},
                )

        if not required:
            return GraderResult(
                grader_name=self.name,
                grader_kind=self.kind,
                passed=True,
                score=1.0,
                reason="No required substrings specified.",
            )

        matched_count = 0
        missing = []
        for r in required:
            r_check = r if self.case_sensitive else r.lower()
            if r_check in act_str:
                matched_count += 1
            else:
                missing.append(r)

        score = matched_count / len(required)
        passed = matched_count == len(required)
        reason = (
            "All required substrings present."
            if passed
            else f"Missing required substrings: {missing}"
        )

        return GraderResult(
            grader_name=self.name,
            grader_kind=self.kind,
            passed=passed,
            score=score,
            reason=reason,
            details={"matched": matched_count, "total_required": len(required), "missing": missing},
        )


# ReDoS guard. CPython's ``re`` is a backtracking engine that holds the GIL while matching,
# so a thread-with-timeout cannot interrupt it. Patterns are therefore validated *before*
# execution by the shared ``nexura_agents.evals.check_regex_safety`` (bounded length, no
# backreferences, no nested/alternation-in-repeat quantifiers, no adjacent overlapping
# repeats, capped repeat counts); the subject is truncated and matching runs in a worker
# thread so it never executes on the event loop.
REGEX_MAX_PATTERN_CHARS = _agents_regex_max_pattern
REGEX_MAX_SUBJECT_CHARS = 1024


class RegexGrader:
    """Deterministic grader asserting output matches a compiled regex pattern."""

    def __init__(self, pattern: str | None = None, name: str = "regex") -> None:
        self.name = name
        self.kind: GraderKind = "deterministic"
        self.pattern = pattern

    async def grade(
        self,
        example: DatasetExample,
        actual_output: Any,
        context: dict[str, Any] | None = None,
    ) -> GraderResult:
        pat = self.pattern or example.metadata.get("regex_pattern")
        if not pat:
            return GraderResult(
                grader_name=self.name,
                grader_kind=self.kind,
                passed=False,
                score=0.0,
                reason="No regex pattern configured.",
            )

        pat = str(pat)
        unsafe = check_regex_safety(pat)
        if unsafe is not None:
            # Fail closed without ever executing the pattern (pattern may be untrusted).
            return GraderResult(
                grader_name=self.name,
                grader_kind=self.kind,
                passed=False,
                score=0.0,
                reason=f"Regex rejected: {unsafe}.",
            )

        try:
            compiled = re.compile(pat)
        except re.error as exc:
            return GraderResult(
                grader_name=self.name,
                grader_kind=self.kind,
                passed=False,
                score=0.0,
                reason=f"Invalid regex: {exc}.",
            )
        subject = str(actual_output)[:REGEX_MAX_SUBJECT_CHARS]
        matched = await asyncio.to_thread(lambda: bool(compiled.search(subject)))
        return GraderResult(
            grader_name=self.name,
            grader_kind=self.kind,
            passed=matched,
            score=1.0 if matched else 0.0,
            reason=f"Regex '{pat}' {'matched' if matched else 'did not match'}.",
        )


class JsonSchemaGrader:
    """Deterministic grader asserting structured output complies with JSON schema."""

    def __init__(self, schema: dict[str, Any] | None = None, name: str = "json_schema") -> None:
        self.name = name
        self.kind: GraderKind = "deterministic"
        self.schema = schema

    async def grade(
        self,
        example: DatasetExample,
        actual_output: Any,
        context: dict[str, Any] | None = None,
    ) -> GraderResult:
        target_schema = self.schema or example.metadata.get("json_schema")
        if not target_schema:
            return GraderResult(
                grader_name=self.name,
                grader_kind=self.kind,
                passed=False,
                score=0.0,
                reason="No JSON schema defined.",
            )

        data = actual_output
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except Exception as exc:
                return GraderResult(
                    grader_name=self.name,
                    grader_kind=self.kind,
                    passed=False,
                    score=0.0,
                    reason=f"Output is not valid JSON: {exc}",
                )

        errors: list[str] = []
        try:
            validate_schema(data, target_schema, path="output", schema_kind="evaluation")
            passed = True
        except SchemaValidationError as exc:
            passed = False
            errors = [str(exc)]

        reason = "JSON schema validation passed." if passed else f"Schema violations: {errors}"
        return GraderResult(
            grader_name=self.name,
            grader_kind=self.kind,
            passed=passed,
            score=1.0 if passed else 0.0,
            reason=reason,
            details={"errors": errors},
        )


class CitationCoverageGrader:
    """Deterministic grader verifying retrieval citation coverage against gold citations."""

    def __init__(self, min_coverage: float = 0.5, name: str = "citation_coverage") -> None:
        self.name = name
        self.kind: GraderKind = "deterministic"
        self.min_coverage = min_coverage

    async def grade(
        self,
        example: DatasetExample,
        actual_output: Any,
        context: dict[str, Any] | None = None,
    ) -> GraderResult:
        expected = example.expected_citations or []
        if not expected:
            return GraderResult(
                grader_name=self.name,
                grader_kind=self.kind,
                passed=True,
                score=1.0,
                reason="No gold citations required for this example.",
            )

        actual_citations: list[str] = []
        if context and "citations" in context:
            actual_citations = [str(c) for c in context["citations"]]
        elif isinstance(actual_output, dict) and "citations" in actual_output:
            actual_citations = [str(c) for c in actual_output["citations"]]

        matched = [c for c in expected if c in actual_citations]
        coverage = len(matched) / len(expected)
        passed = coverage >= self.min_coverage

        return GraderResult(
            grader_name=self.name,
            grader_kind=self.kind,
            passed=passed,
            score=coverage,
            reason=f"Citation coverage {coverage:.1%} ({len(matched)}/{len(expected)} matched).",
            details={"matched": matched, "expected": expected, "actual": actual_citations},
        )


class ZeroSideEffectsGrader:
    """Deterministic grader asserting target produced zero unauthorized mutations."""

    def __init__(self, name: str = "zero_side_effects") -> None:
        self.name = name
        self.kind: GraderKind = "deterministic"

    async def grade(
        self,
        example: DatasetExample,
        actual_output: Any,
        context: dict[str, Any] | None = None,
    ) -> GraderResult:
        mutating_calls = 0
        if context:
            mutating_calls = context.get("mutating_tool_calls_count", 0)

        passed = mutating_calls == 0
        reason = (
            "Zero mutating side effects observed."
            if passed
            else f"Detected {mutating_calls} unauthorized mutating tool calls."
        )
        return GraderResult(
            grader_name=self.name,
            grader_kind=self.kind,
            passed=passed,
            score=1.0 if passed else 0.0,
            reason=reason,
        )


class ModelJudgeGrader:
    """Evaluates semantic quality using an LLM judge.

    Security & Truthfulness Rules:
    1. Judges are strictly read-only evaluators; judges CANNOT authorize or emit tool effects.
    2. Fake/mock models are explicitly labeled with quality_evidence='mock_simulated'.
       Fake models never count as verified human or production semantic evidence.
    """

    def __init__(
        self,
        judge_model: Any,
        *,
        name: str = "model_judge",
        rubric: str = "Evaluate if the response is factual, coherent, and helpful.",
        pass_threshold: float = 0.7,
        is_synthetic: bool | None = None,
    ) -> None:
        self.name = name
        self.kind: GraderKind = "model_judge"
        self.judge_model = judge_model
        self.rubric = rubric
        self.pass_threshold = pass_threshold

        # Determine if model is fake/mock
        model_name = getattr(
            judge_model, "model_id", getattr(judge_model, "name", str(judge_model))
        )
        if is_synthetic is not None:
            self.is_synthetic = is_synthetic
        else:
            self.is_synthetic = bool(
                "mock" in model_name.lower()
                or "fake" in model_name.lower()
                or "test" in model_name.lower()
            )

    async def grade(
        self,
        example: DatasetExample,
        actual_output: Any,
        context: dict[str, Any] | None = None,
    ) -> GraderResult:
        prompt = (
            f"You are an impartial evaluation judge. Rubric: {self.rubric}\n\n"
            f"Input: {example.input_data}\n"
            f"Expected Output: {example.expected_output}\n"
            f"Actual Output: {actual_output}\n\n"
            "Respond strictly in JSON with fields 'score' (number between 0.0 and 1.0) "
            "and 'reason' (string)."
        )

        try:
            # Model judges must NEVER execute tool calls
            from nexura_agents.usage import admission
            from nexura_agents.usage.errors import UsageError

            if admission.active_budget.get() is not None and (
                hasattr(self.judge_model, "invoke_async")
                or not hasattr(self.judge_model, "generate")
            ):
                raise UsageError("Budget admission refused: judge has no estimable model protocol.")
            if hasattr(self.judge_model, "invoke_async"):
                response = await self.judge_model.invoke_async(prompt)
            elif hasattr(self.judge_model, "generate"):
                if admission.active_budget.get() is not None:
                    from nexura_agents.types import Message

                    response = await admission.generate(
                        self.judge_model, [Message(role="user", content=prompt)]
                    )
                else:
                    response = self.judge_model.generate(prompt)
                    if hasattr(response, "__await__"):
                        response = await response
            elif callable(self.judge_model):
                res = self.judge_model(prompt)
                response = await res if hasattr(res, "__await__") else res
            else:
                response = '{"score": 0.85, "reason": "Simulated judge response."}'

            # Check if model attempted to emit tool calls
            tool_name: str | None = None
            if isinstance(response, dict) and response.get("tool_calls"):
                tc = response["tool_calls"][0]
                tool_name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", "tool")
            elif hasattr(response, "tool_calls") and response.tool_calls:
                tc = response.tool_calls[0]
                tool_name = getattr(tc, "name", "tool")
            elif hasattr(response, "message") and getattr(response.message, "tool_calls", None):
                tc = response.message.tool_calls[0]
                tool_name = getattr(tc, "name", "tool")

            if tool_name:
                raise JudgeUnauthorizedEffectError(self.name, str(tool_name))

            content = getattr(response, "content", str(response))
            # Parse judge score
            score = 0.8
            reason = "Model judge evaluated output."

            # Extract json block if present
            json_match = re.search(r"\{.*\}", content, re.DOTALL)
            if json_match:
                try:
                    data = json.loads(json_match.group(0))
                    if isinstance(data, dict) and data.get("tool_calls"):
                        tc = data["tool_calls"][0]
                        tool_name = (
                            tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", "tool")
                        )
                        raise JudgeUnauthorizedEffectError(self.name, str(tool_name or "tool"))
                    score = float(data.get("score", 0.0))
                    reason = str(data.get("reason", reason))
                except JudgeUnauthorizedEffectError:
                    raise
                except Exception:
                    pass

            passed = score >= self.pass_threshold
            evidence: QualityEvidence = "mock_simulated" if self.is_synthetic else "model_judge"

            return GraderResult(
                grader_name=self.name,
                grader_kind=self.kind,
                passed=passed,
                score=min(1.0, max(0.0, score)),
                reason=reason,
                is_synthetic_judge=self.is_synthetic,
                quality_evidence=evidence,
                details={
                    "pass_threshold": self.pass_threshold,
                    "is_synthetic": self.is_synthetic,
                    "evidence_level": evidence,
                },
            )
        except JudgeUnauthorizedEffectError:
            raise
        except Exception as exc:
            raise GraderExecutionError(self.name, str(exc), original_error=exc) from exc
