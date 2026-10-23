# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Studio evals: datasets with expected trajectories, runs against workflows or agents, gates.

Scoring is the SDK engine (:mod:`nexura_agents.evals`): scorers, ``evaluate_async`` for agent
targets, and ``EvalReport.compare`` for regressions. Nothing here re-implements a scorer. The
server never executes uploaded code: a dataset is data, a workflow target is a stored spec
run by the workflow engine, and an agent target is looked up in a host-registered table.
"""

from __future__ import annotations

import copy
import dataclasses
import inspect
import json
import re
import time
import uuid
from collections.abc import Callable, Sequence
from typing import Any

from nexura_agents import Agent, Case, FakeModel
from nexura_agents.evals import (
    CaseResult,
    EvalOutput,
    EvalReport,
    Score,
    Scorer,
    agent_path,
    check_regex_safety,
    contains,
    evaluate_async,
    exact_match,
    max_latency,
    regex_match,
    tool_called,
    tool_trajectory,
)
from nexura_agents.types import Message, RunResult, ToolCall

from nexura_runtime.evaluation.graders import ToolArgumentAccuracyGrader, TrajectoryGrader
from nexura_runtime.evaluation.models import DatasetExample, TrajectoryStep
from nexura_runtime.observability.run_diagnostics import redact_run_content
from nexura_runtime.workflow import ModuleRegistry, WorkflowEngine, WorkflowNode, WorkflowSpec

MAX_CASES = 200
MAX_TEXT_CHARS = 8000
MAX_DATASET_BYTES = 512 * 1024
MAX_REPEATS = 5
MAX_NAME_CHARS = 120
DATASET_FORMAT = "nexura.eval-dataset"
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,79}$")

SCORER_TYPES = (
    "exact_match",
    "contains",
    "regex",
    "tool_trajectory",
    "tool_argument_accuracy",
    "agent_path",
    "tool_called",
    "max_latency",
    "llm_judge",
)
LOCAL_FAKE_AGENT = "local-fake"

AgentFactory = Callable[[str | None], Agent]


class StudioEvalError(Exception):
    """A client-facing error with an HTTP status and a stable code."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        field_errors: list[dict[str, str]] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.field_errors = field_errors or []


# ---------------------------------------------------------------- dataset validation


def _step_ok(item: Any) -> bool:
    if isinstance(item, str):
        return bool(item.strip())
    if isinstance(item, dict):
        return (
            set(item) <= {"name", "args"}
            and isinstance(item.get("name"), str)
            and bool(item["name"].strip())
            and (item.get("args") is None or isinstance(item["args"], dict))
        )
    return False


def _validate_case(raw: Any, index: int, errors: list[dict[str, str]]) -> dict[str, Any] | None:
    where = f"cases[{index}]"
    if not isinstance(raw, dict):
        errors.append({"field": where, "error": "case must be an object"})
        return None
    known = {
        "name",
        "input",
        "expected_output",
        "expected_trajectory",
        "expected_agent_path",
        "tags",
        "metadata",
    }
    before = len(errors)
    for extra in sorted(set(raw) - known):
        errors.append({"field": f"{where}.{extra}", "error": "unknown field"})
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > MAX_NAME_CHARS:
        errors.append({"field": f"{where}.name", "error": f"name must be 1-{MAX_NAME_CHARS} chars"})
    value = raw.get("input")
    if isinstance(value, str):
        if not value.strip():
            errors.append({"field": f"{where}.input", "error": "input must not be empty"})
        elif len(value) > MAX_TEXT_CHARS:
            errors.append({"field": f"{where}.input", "error": f"input over {MAX_TEXT_CHARS}"})
    elif isinstance(value, dict):
        if not value or len(json.dumps(value, default=str)) > MAX_TEXT_CHARS:
            errors.append({"field": f"{where}.input", "error": "input object empty or too large"})
    else:
        errors.append({"field": f"{where}.input", "error": "input must be a string or object"})
    expected = raw.get("expected_output")
    if expected is not None:
        try:
            if len(json.dumps(expected)) > MAX_TEXT_CHARS:
                errors.append({"field": f"{where}.expected_output", "error": "too large"})
        except (TypeError, ValueError):
            errors.append({"field": f"{where}.expected_output", "error": "must be JSON"})
    traj = raw.get("expected_trajectory")
    if traj is not None and (
        not isinstance(traj, list) or len(traj) > 50 or not all(_step_ok(s) for s in traj)
    ):
        errors.append(
            {
                "field": f"{where}.expected_trajectory",
                "error": "must be a list (max 50) of tool names or {name, args} objects",
            }
        )
    path = raw.get("expected_agent_path")
    if path is not None and (
        not isinstance(path, list) or not all(isinstance(n, str) and n for n in path)
    ):
        errors.append({"field": f"{where}.expected_agent_path", "error": "list of names"})
    tags = raw.get("tags", [])
    if (
        not isinstance(tags, list)
        or len(tags) > 20
        or not all(isinstance(t, str) and 0 < len(t) <= 40 for t in tags)
    ):
        errors.append({"field": f"{where}.tags", "error": "up to 20 tags of 1-40 chars"})
    meta = raw.get("metadata", {})
    if not isinstance(meta, dict):
        errors.append({"field": f"{where}.metadata", "error": "metadata must be an object"})
    if len(errors) > before:
        return None
    return {
        "name": str(name).strip(),
        "input": value,
        "expected_output": expected,
        "expected_trajectory": traj,
        "expected_agent_path": path,
        "tags": list(tags),
        "metadata": dict(meta),
    }


def validate_dataset(
    dataset_id: str, body: dict[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, str], int]:
    """Validate and secret-redact a dataset body.

    Returns ``(cases, {"name","description"}, redaction_count)``. Raises ``StudioEvalError``.
    """
    if not SAFE_ID.match(dataset_id):
        raise StudioEvalError(
            400, "INVALID_DATASET_ID", "Dataset id must be 1-80 chars [A-Za-z0-9_.-]."
        )
    cases_raw = body.get("cases")
    if not isinstance(cases_raw, list) or not cases_raw:
        raise StudioEvalError(
            422,
            "EMPTY_DATASET",
            "A dataset needs at least one case.",
            [{"field": "cases", "error": "must be a non-empty list"}],
        )
    if len(cases_raw) > MAX_CASES:
        raise StudioEvalError(
            413, "DATASET_TOO_LARGE", f"A dataset may hold at most {MAX_CASES} cases."
        )
    try:
        size = len(json.dumps(body, default=str))
    except (TypeError, ValueError) as exc:
        raise StudioEvalError(422, "DATASET_NOT_JSON", f"Dataset must be JSON: {exc}") from exc
    if size > MAX_DATASET_BYTES:
        raise StudioEvalError(
            413, "DATASET_TOO_LARGE", f"Dataset exceeds {MAX_DATASET_BYTES} bytes."
        )
    errors: list[dict[str, str]] = []
    cases: list[dict[str, Any]] = []
    for i, raw in enumerate(cases_raw):
        case = _validate_case(raw, i, errors)
        if case is not None:
            cases.append(case)
    names = [c["name"] for c in cases]
    for dup in sorted({n for n in names if names.count(n) > 1}):
        errors.append({"field": "cases", "error": f"duplicate case name {dup!r}"})
    meta: dict[str, str] = {}
    for key, limit in (("name", MAX_NAME_CHARS), ("description", 2000)):
        val = body.get(key, "")
        if val is None:
            val = ""
        if not isinstance(val, str) or len(val) > limit:
            errors.append({"field": key, "error": f"{key} must be a string up to {limit} chars"})
        meta[key] = val if isinstance(val, str) else ""
    if errors:
        raise StudioEvalError(422, "DATASET_VALIDATION_ERROR", "Dataset failed validation.", errors)
    # Redact case by case: redacting the whole list at once truncates it to 100 items.
    redacted = [redact_run_content(case) for case in cases]
    # Two names can differ only in a secret; stored after redaction they would collide.
    names = [c["name"] for c in redacted]
    clashes = sorted({n for n in names if names.count(n) > 1})
    if clashes:
        raise StudioEvalError(
            422,
            "DATASET_VALIDATION_ERROR",
            "Dataset failed validation.",
            [{"field": "cases", "error": f"duplicate case name {n!r}"} for n in clashes],
        )
    redactions = sum(1 for a, b in zip(cases, redacted, strict=True) if a != b)
    for key, value in list(meta.items()):
        clean = redact_run_content(value)
        if clean != value:
            redactions += 1
        meta[key] = clean if isinstance(clean, str) else ""
    return redacted, meta, redactions


def content_hash(cases: Sequence[dict[str, Any]]) -> str:
    from nexura_agents.contracts.canonical import canonical_digest, canonical_json

    return str(canonical_digest(canonical_json(list(cases))))


# ---------------------------------------------------------------- scorers


def _for_field(scorer: Scorer, field: str) -> Scorer:
    """Run ``scorer`` with ``case.expected`` taken from a dataset field.

    A case without that expectation is "n/a": it scores 1.0 and says so, so a dataset can
    mix cases that check the answer with cases that check the trajectory.
    """
    name = str(getattr(scorer, "name", field))

    def wrapped(case: Case, result: EvalOutput) -> Any:
        want = case.metadata.get(field)
        if want is None:
            return Score(name, 1.0, True, f"n/a: case has no {field}")
        return scorer(dataclasses.replace(case, expected=want), result)

    wrapped.__name__ = name
    wrapped.name = name  # type: ignore[attr-defined]
    return wrapped


def build_scorers(specs: Sequence[dict[str, Any]], judge_model: Any | None) -> list[Scorer]:
    out: list[Scorer] = []
    for i, spec in enumerate(specs):
        if not isinstance(spec, dict) or spec.get("type") not in SCORER_TYPES:
            raise StudioEvalError(
                422,
                "INVALID_SCORER",
                f"scorers[{i}].type must be one of {list(SCORER_TYPES)}.",
            )
        kind = spec["type"]
        try:
            if kind == "exact_match":
                out.append(_for_field(exact_match(), "expected_output"))
            elif kind == "contains":
                out.append(_for_field(contains(), "expected_output"))
            elif kind == "regex":
                pattern = str(spec.get("pattern", ""))
                unsafe = check_regex_safety(pattern)
                if unsafe is not None:
                    raise ValueError(f"unsafe pattern: {unsafe}")
                re.compile(pattern)
                out.append(regex_match(pattern))
            elif kind in ("tool_trajectory", "tool_argument_accuracy") and (
                kind == "tool_argument_accuracy"
                or "argument_rules" in spec
                or spec.get("mode") in ("exact", "in_order", "any_order")
            ):
                grader = (
                    ToolArgumentAccuracyGrader(
                        mode=spec.get("mode", "exact"), argument_rules=spec.get("argument_rules")
                    )
                    if kind == "tool_argument_accuracy"
                    else TrajectoryGrader(
                        mode=spec.get("mode", "exact"),
                        check_arguments=spec.get("check_arguments", False),
                        argument_rules=spec.get("argument_rules"),
                    )
                )
                out.append(_trajectory_scorer(grader))
            elif kind == "tool_trajectory":
                out.append(
                    _for_field(
                        tool_trajectory(
                            mode=spec.get("mode", "strict"),
                            check_arguments=spec.get("check_arguments", False),
                        ),
                        "expected_trajectory",
                    )
                )
            elif kind == "agent_path":
                out.append(
                    _for_field(agent_path(mode=spec.get("mode", "strict")), "expected_agent_path")
                )
            elif kind == "tool_called":
                out.append(tool_called(str(spec.get("tool", "")), spec.get("times")))
            elif kind == "max_latency":
                out.append(max_latency(float(spec.get("seconds", 0))))
            else:  # llm_judge
                if judge_model is None:
                    raise StudioEvalError(
                        422,
                        "JUDGE_UNAVAILABLE",
                        "llm_judge needs a judge model registered by the host; none is configured.",
                    )
                from nexura_agents.evals import llm_judge

                out.append(
                    llm_judge(
                        judge_model,
                        str(spec.get("rubric", "")),
                        threshold=float(spec.get("threshold", 0.5)),
                    )
                )
        except StudioEvalError:
            raise
        except Exception as exc:
            raise StudioEvalError(
                422, "INVALID_SCORER", f"scorers[{i}] ({kind}) is invalid: {exc}"
            ) from exc
    names = [str(getattr(s, "name", "")) for s in out]
    if len(set(names)) != len(names):
        raise StudioEvalError(422, "INVALID_SCORER", f"Scorer names must be unique, got {names}.")
    return out


def _trajectory_scorer(grader: TrajectoryGrader) -> Scorer:
    def score(case: Case, result: EvalOutput) -> Score:
        trajectory = case.metadata.get("expected_trajectory")
        example = DatasetExample(
            input_data={},
            expected_trajectory=[
                TrajectoryStep(name=name, args=args or {}) for name, args in trajectory
            ]
            if trajectory is not None
            else None,
        )
        graded = grader.score_trace(
            example,
            {
                "runs": [
                    {
                        "messages": [],
                        "events": [
                            {"type": "tool_started", "data": {"tool_name": name, "arguments": args}}
                            for name, args in result.tool_call_details
                        ],
                    }
                ]
            },
        )
        return Score(grader.name, graded.score, graded.passed, graded.reason)

    score.name = grader.name  # type: ignore[attr-defined]
    return score


def default_scorer_specs(cases: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    if any(c.get("expected_output") is not None for c in cases):
        specs.append({"type": "exact_match"})
    if any(c.get("expected_trajectory") is not None for c in cases):
        specs.append({"type": "tool_trajectory", "mode": "strict"})
    if any(c.get("expected_agent_path") is not None for c in cases):
        specs.append({"type": "agent_path"})
    if not specs:
        specs.append({"type": "max_latency", "seconds": 30})
    return specs


# ---------------------------------------------------------------- targets


def _to_sdk_cases(cases: Sequence[dict[str, Any]]) -> list[Case]:
    out: list[Case] = []
    for c in cases:
        text = c["input"] if isinstance(c["input"], str) else json.dumps(c["input"], default=str)
        meta = {
            "expected_output": c.get("expected_output"),
            "expected_trajectory": [
                (s, None) if isinstance(s, str) else (s["name"], s.get("args"))
                for s in (c.get("expected_trajectory") or [])
            ]
            if c.get("expected_trajectory") is not None
            else None,
            "expected_agent_path": c.get("expected_agent_path"),
            "tags": list(c.get("tags") or []),
            "input": c["input"],
        }
        for field in ("source_run_id", "source_workflow_id", "source_spec_hash", "output_field"):
            if field in c.get("metadata", {}):
                meta[field] = c["metadata"][field]
        out.append(Case(name=c["name"], input=text, expected=None, metadata=meta))
    return out


def local_fake_agent(version: str | None = None) -> Agent:
    """Deterministic offline agent: replies with the user's text. Labelled synthetic."""

    def handler(messages: list[Message], tools: Any) -> str:
        last = next((m.content for m in reversed(messages) if m.role == "user"), "") or ""
        return f"echo: {last}"

    return Agent(
        name=LOCAL_FAKE_AGENT,
        instructions="Local deterministic fake agent (synthetic evidence).",
        model=FakeModel(handler=handler),
    )


def step_label(node: WorkflowNode) -> str:
    """The name a top-level step has in a workflow trajectory.

    Its handler (or module id); composite steps have neither, so they are named by kind and
    node id (``parallel:<id>``, ``subworkflow:<id>``). Saving a run's path and replaying it
    both use this, so a recorded path and its replay always use the same names.
    """
    name = node.handler or node.module_id or ""
    if name:
        return str(name)
    kind = "subworkflow" if node.subgraph is not None else "parallel" if node.parallel else "step"
    return f"{kind}:{node.id}"


async def _run_workflow_case(
    spec: WorkflowSpec,
    registry: ModuleRegistry,
    case: Case,
    workspace_id: str,
    output_key: str | None,
    approval_handlers: frozenset[str],
) -> tuple[CaseResult | None, EvalOutput | None]:
    """Run one case through a stored workflow and express it as an SDK ``RunResult``."""
    from nexura_agents.events import RunEventType

    state = case.metadata.get("input")
    initial = copy.deepcopy(state) if isinstance(state, dict) else {"input": case.input}
    started = time.perf_counter()
    engine = WorkflowEngine(
        spec=spec,
        registry=registry,
        store=None,
        workspace_id=workspace_id,
        approval_required_handlers=approval_handlers,
    )
    handlers: list[str] = []
    result = None
    top_run_id: str | None = None
    async for event in engine.stream(initial):
        if event.type == RunEventType.WORKFLOW_STARTED and top_run_id is None:
            top_run_id = str(event.data.get("workflow_run_id"))
        # Only steps of this run: a sub-workflow's steps carry its own run id, even when the
        # sub-workflow has the same name as its parent.
        if (
            event.type == RunEventType.NODE_STARTED
            and event.data.get("workflow") == spec.name
            and str(event.data.get("workflow_run_id")) == top_run_id
        ):
            node = spec.nodes.get(str(event.data.get("node_id")))
            if node is not None:
                handlers.append(step_label(node))
        if event.type == RunEventType.WORKFLOW_PAUSED:
            result = event.data["result"]
        if event.type == RunEventType.WORKFLOW_COMPLETED:
            result = event.data["result"]
    latency = time.perf_counter() - started
    if result is None or result.status == "paused":
        return (
            CaseResult(
                case.name,
                0,
                "paused",
                latency_seconds=latency,
                metadata=_json(case.metadata),
            ),
            None,
        )
    if result.status != "completed":
        return (
            CaseResult(
                case.name,
                0,
                "error",
                latency_seconds=latency,
                error={"type": "WorkflowFailed", "message": str(result.error or result.status)},
                metadata=_json(case.metadata),
            ),
            None,
        )
    final_state = result.state
    if output_key:
        out_value: Any = final_state.get(output_key)
        text = out_value if isinstance(out_value, str) else json.dumps(out_value, default=str)
    else:
        out_value = final_state
        text = json.dumps(final_state, sort_keys=True, default=str)
    calls = [ToolCall(id=f"n{i}", name=h, arguments={}) for i, h in enumerate(handlers)]
    messages = [
        Message(role="user", content=case.input if isinstance(case.input, str) else ""),
        Message(role="assistant", content=None, tool_calls=calls),
        Message(role="assistant", content=text),
    ]
    run = RunResult(
        run_id=result.workflow_run_id,
        final_output=text,
        messages=messages,
        events=[],
        status="completed",
        output=out_value,
        agent_path=[spec.name],
    )
    return None, EvalOutput(run, latency)


def _json(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


async def evaluate_workflow(
    spec: WorkflowSpec,
    registry: ModuleRegistry,
    cases: Sequence[Case],
    scorers: Sequence[Scorer],
    *,
    workspace_id: str,
    output_key: str | None,
    approval_handlers: frozenset[str],
    repeats: int,
) -> EvalReport:
    """Same report shape as ``evaluate_async``: SDK scorers over each workflow run."""
    results: list[CaseResult] = []
    for case in cases:
        for repeat in range(repeats):
            try:
                early, output = await _run_workflow_case(
                    spec, registry, case, workspace_id, output_key, approval_handlers
                )
            except Exception as exc:
                results.append(
                    CaseResult(
                        case.name,
                        repeat,
                        "error",
                        error={"type": type(exc).__name__, "message": str(exc)},
                        metadata=_json(case.metadata),
                    )
                )
                continue
            if early is not None:
                early.repeat = repeat
                results.append(early)
                continue
            assert output is not None
            scores: list[Score] = []
            for scorer in scorers:
                sname = str(getattr(scorer, "name", "scorer"))
                try:
                    raw = scorer(case, output)
                    if inspect.isawaitable(raw):
                        raw = await raw
                    scores.append(
                        raw if isinstance(raw, Score) else Score(sname, float(raw), bool(raw))
                    )
                except Exception as exc:
                    scores.append(
                        Score(sname, 0.0, False, f"scorer raised {type(exc).__name__}: {exc}")
                    )
            results.append(
                CaseResult(
                    case.name,
                    repeat,
                    "passed" if all(s.passed for s in scores) else "failed",
                    scores=scores,
                    output_text=output.final_output,
                    output=_json(output.output),
                    latency_seconds=output.duration_seconds,
                    agent_path=output.agent_path,
                    metadata=_json(case.metadata),
                )
            )
    return EvalReport(
        target=spec.name,
        results=results,
        scorer_names=[str(getattr(s, "name", "")) for s in scorers],
        repeats=repeats,
    )


async def run_dataset(
    *,
    cases: Sequence[dict[str, Any]],
    scorer_specs: Sequence[dict[str, Any]],
    target: dict[str, Any],
    repeats: int,
    judge_model: Any | None,
    workflow: tuple[WorkflowSpec, ModuleRegistry, str, frozenset[str]] | None = None,
    agent_factory: AgentFactory | None = None,
) -> EvalReport:
    """Score ``cases`` against a workflow or an agent with the SDK evals engine."""
    sdk_cases = _to_sdk_cases(cases)
    scorers = build_scorers(scorer_specs, judge_model)
    if target["kind"] == "workflow":
        assert workflow is not None
        spec, registry, workspace_id, approvals = workflow
        return await evaluate_workflow(
            spec,
            registry,
            sdk_cases,
            scorers,
            workspace_id=workspace_id,
            output_key=target.get("output_key"),
            approval_handlers=approvals,
            repeats=repeats,
        )
    assert agent_factory is not None
    return await evaluate_async(
        agent_factory(target.get("version")), sdk_cases, scorers, repeats=repeats
    )


# ---------------------------------------------------------------- report storage / compare


def redact_report(report: EvalReport) -> dict[str, Any]:
    """JSON report with secrets removed from every output, reason, error and metadata."""
    data = report.to_dict()
    for row in data["results"]:
        for key in ("output_text", "output", "scores", "error", "metadata", "prompt"):
            row[key] = redact_run_content(row[key])
    return data


def report_from_stored(data: dict[str, Any]) -> EvalReport:
    return EvalReport.from_json(json.dumps(data))


def summarize(report: EvalReport) -> dict[str, Any]:
    results = report.results
    counted = [r for r in results if r.status != "paused"]
    values = [s.value for r in counted for s in r.scores]
    return {
        "total": len(results),
        "passed": sum(1 for r in results if r.status == "passed"),
        "failed": sum(1 for r in results if r.status == "failed"),
        "errors": sum(1 for r in results if r.status == "error"),
        "paused": sum(1 for r in results if r.status == "paused"),
        "pass_rate": round(report.pass_rate, 4),
        "mean_score": round(sum(values) / len(values), 4) if values else 0.0,
        "latency_seconds": round(report.total_latency_seconds, 4),
    }


def compare_runs(
    baseline: EvalReport,
    candidate: EvalReport,
    *,
    tolerance: float = 0.0,
    min_pass_rate: float | None = None,
) -> dict[str, Any]:
    """Side-by-side diff and gate. The gate fails exactly when ``assert_no_regressions``
    would raise (``candidate.compare(baseline, tolerance)`` has regressions), and also when
    ``min_pass_rate`` is set and the candidate is below it."""
    if tolerance < 0 or tolerance > 1:
        raise StudioEvalError(422, "INVALID_TOLERANCE", "tolerance must be between 0 and 1.")
    if min_pass_rate is not None and not 0 <= min_pass_rate <= 1:
        raise StudioEvalError(422, "INVALID_THRESHOLD", "min_pass_rate must be between 0 and 1.")
    diff = candidate.compare(baseline, tolerance=tolerance)

    def change(c: Any) -> dict[str, Any]:
        return {
            "case": c.case,
            "scorer": c.scorer,
            "baseline": c.baseline,
            "candidate": c.current,
            "detail": c.detail,
        }

    reasons: list[str] = []
    if diff.has_regressions:
        reasons.append(f"{len(diff.regressions)} regression(s)")
    cand_rate = candidate.pass_rate
    if min_pass_rate is not None and cand_rate < min_pass_rate - 1e-12:
        reasons.append(f"pass rate {cand_rate:.4f} below threshold {min_pass_rate:.4f}")
    return {
        "regressions": [change(c) for c in diff.regressions],
        "improvements": [change(c) for c in diff.improvements],
        "gate": {
            "passed": not reasons,
            "tolerance": tolerance,
            "min_pass_rate": min_pass_rate,
            "reasons": reasons,
            "semantics": "assert_no_regressions",
            "summary": diff.format(),
        },
        "baseline_summary": summarize(baseline),
        "candidate_summary": summarize(candidate),
    }


def new_run_id() -> str:
    return f"er-{uuid.uuid4().hex[:12]}"


__all__ = [
    "LOCAL_FAKE_AGENT",
    "MAX_CASES",
    "StudioEvalError",
    "build_scorers",
    "compare_runs",
    "content_hash",
    "default_scorer_specs",
    "local_fake_agent",
    "new_run_id",
    "redact_report",
    "report_from_stored",
    "run_dataset",
    "summarize",
    "validate_dataset",
]
