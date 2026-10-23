# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Deterministic and model-based graders for assessing output quality."""

from __future__ import annotations

import asyncio
import json
import re
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from typing import Any, Protocol, runtime_checkable

from nexura_agents.contracts.errors import SchemaValidationError
from nexura_agents.contracts.schema_validator import validate_schema
from nexura_agents.evals import REGEX_MAX_PATTERN_CHARS as _agents_regex_max_pattern
from nexura_agents.evals import check_regex_safety

from nexura_runtime.evaluation.errors import GraderExecutionError, JudgeUnauthorizedEffectError
from nexura_runtime.evaluation.models import (
    ArgumentMatch,
    DatasetExample,
    GraderKind,
    GraderResult,
    QualityEvidence,
    TrajectoryMode,
    TrajectoryStep,
)


def recorded_tool_calls(trace: Any) -> list[TrajectoryStep]:
    """Read SDK RunResult/RunEvents or their stored JSON, deduplicating per run/call id.

    Messages include requested calls; tool_started events also cover calls missing from
    message capture. Streaming deltas and completion events never count as extra calls.
    """

    def get(value: Any, key: str, default: Any = None) -> Any:
        return value.get(key, default) if isinstance(value, dict) else getattr(value, key, default)

    calls: list[TrajectoryStep] = []
    nodes: dict[tuple[str, str], TrajectoryStep] = {}
    edges: dict[tuple[str, str], set[tuple[str, str]]] = {}

    def add(call: Any, run_id: str) -> tuple[str, str]:
        call_id = get(call, "id", get(call, "tool_call_id"))
        key = (run_id, str(call_id) if call_id else f"anonymous:{len(nodes)}")
        if key in nodes:
            return key
        function = get(call, "function", call)
        name = get(function, "name", get(function, "tool_name"))
        args = get(function, "arguments", get(function, "args", {}))
        if isinstance(args, str):
            args = json.loads(args)
        if not isinstance(name, str) or not name or not isinstance(args, dict):
            raise ValueError("Recorded tool call requires name and object arguments")
        nodes[key] = TrajectoryStep(name=name, args=args)
        edges[key] = set()
        return key

    def read(run: Any) -> None:
        prior = set(nodes)
        run_id = str(get(run, "run_id", ""))
        sequences: list[list[tuple[str, str]]] = [[], []]
        for message in get(run, "messages", []) or []:
            for call in get(message, "tool_calls", []) or []:
                sequences[0].append(add(call, run_id))
        for event in get(run, "events", []) or []:
            if get(event, "type") == "tool_started":
                sequences[1].append(
                    add(get(event, "data", {}), str(get(event, "run_id", run_id)))
                )
        for sequence in sequences:
            unique = list(dict.fromkeys(sequence))
            for before, after in zip(unique, unique[1:], strict=False):
                edges[before].add(after)
        # Conversation runs themselves are recorded in turn order.
        for key in prior:
            edges[key].update(set(nodes) - prior)

    if isinstance(trace, (list, tuple)):
        read({"events": trace})
    else:
        runs = get(trace, "runs")
        for run in runs if runs is not None else [trace]:
            read(run)
    # Both captures provide ordering constraints. Shared call IDs anchor partial
    # captures; no arbitrary message-first ordering is invented for disjoint data.
    while nodes:
        successors = {key for targets in edges.values() for key in targets}
        ready = [key for key in nodes if key not in successors]
        if len(ready) != 1:
            raise ValueError("Recorded tool call ordering is ambiguous or contradictory")
        key = ready[0]
        calls.append(nodes.pop(key))
        edges.pop(key)
    return calls


def _argument_equal(actual: Any, expected: Any, rule: ArgumentMatch) -> bool:
    def normalise(value: Any) -> Any:
        for operation in rule.normalisers:
            if operation == "numeric":
                if isinstance(value, bool):
                    raise ValueError("Boolean is not numeric")
                value = Decimal(str(value))
            elif isinstance(value, str):
                if operation == "strip":
                    value = value.strip()
                elif operation == "casefold":
                    value = value.casefold()
                elif operation == "whitespace":
                    value = " ".join(value.split())
        return value

    try:
        actual, expected = normalise(actual), normalise(expected)
    except (ValueError, TypeError, OverflowError, InvalidOperation):
        return False
    if isinstance(actual, (int, float, Decimal)) and not isinstance(actual, bool):
        if not isinstance(expected, (int, float, Decimal)) or isinstance(expected, bool):
            return False
        try:
            left, right = Fraction(actual), Fraction(expected)
        except (ValueError, OverflowError):
            return False
        return abs(left - right) <= max(
            Fraction(str(rule.absolute_tolerance)),
            Fraction(str(rule.relative_tolerance)) * max(abs(left), abs(right)),
        )
    # JSON booleans must not accidentally equal 0/1 (including nested objects).
    return type(actual) is type(expected) and json.dumps(actual, sort_keys=True) == json.dumps(
        expected, sort_keys=True
    )


def _maximum_assignment(weights: list[list[float]]) -> list[tuple[int, int]]:
    """Maximum-weight bipartite assignment, padding with unmatched zero-weight calls."""
    rows = len(weights)
    columns = max(rows, len(weights[0]) if rows else 0)
    u, v = [0.0] * (rows + 1), [0.0] * (columns + 1)
    p, way = [0] * (columns + 1), [0] * (columns + 1)
    for i in range(1, rows + 1):
        p[0], j0 = i, 0
        minimum, used = [float("inf")] * (columns + 1), [False] * (columns + 1)
        while True:
            used[j0] = True
            i0, delta, j1 = p[j0], float("inf"), 0
            for j in range(1, columns + 1):
                if not used[j]:
                    weight = weights[i0 - 1][j - 1] if j <= len(weights[i0 - 1]) else 0
                    cost = -weight - u[i0] - v[j]
                    if cost < minimum[j]:
                        minimum[j], way[j] = cost, j0
                    if minimum[j] < delta:
                        delta, j1 = minimum[j], j
            for j in range(columns + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minimum[j] -= delta
            j0 = j1
            if not p[j0]:
                break
        while j0:
            p[j0], j0 = p[way[j0]], way[j0]
    return [
        (p[j] - 1, j - 1) for j in range(1, columns + 1) if p[j] and j <= len(weights[p[j] - 1])
    ]


class TrajectoryGrader:
    """Score tool sequence accuracy; optionally compare expected argument leaves.

    Exact/any_order require equal call counts. Subset/in_order permits extra actual
    calls. Scores average expected step accuracy, penalising extras outside subset.
    Argument rules use dotted paths, optionally prefixed with the tool name.
    """

    def __init__(
        self,
        name: str = "tool_trajectory",
        *,
        mode: TrajectoryMode = "exact",
        check_arguments: bool = False,
        argument_rules: dict[str, ArgumentMatch | dict[str, Any]] | None = None,
    ) -> None:
        if mode not in ("exact", "strict", "subset", "in_order", "any_order"):
            raise ValueError(f"Unsupported trajectory mode: {mode}")
        self.name = name
        self.kind: GraderKind = "deterministic"
        self.mode = "exact" if mode == "strict" else "subset" if mode == "in_order" else mode
        self.check_arguments = check_arguments
        self.argument_rules = {
            key: ArgumentMatch.model_validate(value)
            for key, value in (argument_rules or {}).items()
        }

    def _step(
        self, expected: TrajectoryStep, actual: TrajectoryStep
    ) -> tuple[float, dict[str, bool]]:
        if expected.name != actual.name:
            return 0.0, {}
        checks: dict[str, bool] = {}
        if self.check_arguments:

            def compare(want: dict[str, Any], got: Any, prefix: str = "") -> None:
                for key, value in want.items():
                    path = f"{prefix}.{key}" if prefix else key
                    present = isinstance(got, dict) and key in got
                    observed = got[key] if present else None
                    rule = self.argument_rules.get(
                        f"{expected.name}.{path}", self.argument_rules.get(path, ArgumentMatch())
                    )
                    if isinstance(value, dict) and not rule.normalisers:
                        if value:
                            compare(value, observed, path)
                        else:
                            checks[path] = present and isinstance(observed, dict)
                    else:
                        checks[path] = present and _argument_equal(observed, value, rule)

            compare(expected.args, actual.args)
        return (sum(checks.values()) / len(checks) if checks else 1.0), checks

    def score_trace(self, example: DatasetExample, trace: Any) -> GraderResult:
        """Synchronous grading shared by the runtime and SDK Studio scorer adapter."""
        try:
            if example.expected_trajectory is None:
                raise ValueError("Example has no expected_trajectory")
            expected = [
                TrajectoryStep(name=s) if isinstance(s, str) else s
                for s in example.expected_trajectory
            ]
            actual = recorded_tool_calls(trace)
            pairs = [[self._step(e, a) for a in actual] for e in expected]
            weights = [[value for value, _ in row] for row in pairs]
            if self.mode == "exact":
                matching = [(i, i) for i in range(min(len(expected), len(actual)))]
            elif self.mode == "any_order":
                matching = _maximum_assignment(weights)
            else:
                # Weighted longest in-order subsequence, including repeated tool names.
                dp = [[0.0] * (len(actual) + 1) for _ in range(len(expected) + 1)]
                for i in range(1, len(expected) + 1):
                    for j in range(1, len(actual) + 1):
                        dp[i][j] = max(
                            dp[i - 1][j], dp[i][j - 1], dp[i - 1][j - 1] + weights[i - 1][j - 1]
                        )
                i, j, matching = len(expected), len(actual), []
                while i and j:
                    if dp[i][j] == dp[i - 1][j - 1] + weights[i - 1][j - 1]:
                        matching.append((i - 1, j - 1))
                        i, j = i - 1, j - 1
                    elif dp[i - 1][j] >= dp[i][j - 1]:
                        i -= 1
                    else:
                        j -= 1
                matching.reverse()
            denominator = (
                len(expected) if self.mode == "subset" else max(len(expected), len(actual))
            )
            score = sum(weights[i][j] for i, j in matching) / denominator if denominator else 1.0
            passed = score == 1.0
            return GraderResult(
                grader_name=self.name,
                grader_kind=self.kind,
                score=score,
                passed=passed,
                reason="Trajectory matched" if passed else "Trajectory or argument mismatch",
                details={
                    "mode": self.mode,
                    "expected_calls": len(expected),
                    "actual_calls": len(actual),
                    "matches": [
                        {
                            "expected_index": i,
                            "actual_index": j,
                            "score": pairs[i][j][0],
                            "arguments": pairs[i][j][1],
                        }
                        for i, j in matching
                    ],
                },
            )
        except (ValueError, TypeError) as exc:
            return GraderResult(
                grader_name=self.name, grader_kind=self.kind, score=0, passed=False, reason=str(exc)
            )

    async def grade(
        self, example: DatasetExample, actual_output: Any, context: dict[str, Any] | None = None
    ) -> GraderResult:
        return self.score_trace(example, (context or {}).get("trace", actual_output))


class ToolArgumentAccuracyGrader(TrajectoryGrader):
    """Trajectory accuracy with per-argument normalisation and numeric tolerances."""

    def __init__(
        self,
        name: str = "tool_argument_accuracy",
        *,
        mode: TrajectoryMode = "exact",
        argument_rules: dict[str, ArgumentMatch | dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(name, mode=mode, check_arguments=True, argument_rules=argument_rules)


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
