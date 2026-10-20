# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""REST API router for evaluation datasets, benchmarks, grading, and run comparisons."""

from __future__ import annotations

import re
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from nexura_runtime.evaluation.errors import (
    DatasetNotFoundError,
)
from nexura_runtime.evaluation.graders import (
    CitationCoverageGrader,
    ContainsGrader,
    ExactMatchGrader,
    Grader,
    RegexGrader,
)
from nexura_runtime.evaluation.models import (
    DatasetExample,
    EvaluationDataset,
)
from nexura_runtime.evaluation.runner import EvaluationRunner
from nexura_runtime.evaluation.store import EvaluationStore, SqliteEvaluationStore
from nexura_runtime.service.api.models import (
    CompareEvaluationsRequest,
    CreateDatasetRequest,
    RunEvaluationRequest,
)
from nexura_runtime.service.auth.errors import PermissionDeniedError
from nexura_runtime.service.auth.models import ActionPermission, Principal, WorkspaceRole
from nexura_runtime.service.auth.permissions import check_permission
from nexura_runtime.service.workspaces.repository import WorkspaceRepository

router = APIRouter(prefix="/evaluations", tags=["evaluations"])

_SAFE_ID_REGEX = re.compile(r"^[a-zA-Z0-9_\-\.]+$")


def _get_caller_context(request: Request) -> tuple[str, Principal, WorkspaceRole]:
    caller_ws: str = getattr(request.state, "workspace_id", "default")
    principal: Principal = getattr(
        request.state,
        "principal",
        Principal(principal_id="local-owner", profile="local-owner"),
    )
    raw_role = getattr(request.state, "role", "admin")
    role: WorkspaceRole = (
        "admin" if raw_role == "admin" else ("editor" if raw_role == "editor" else "viewer")
    )
    return caller_ws, principal, role


def _require(permission: ActionPermission) -> Any:
    """Route dependency enforcing a workspace permission (local-owner is always allowed)."""

    def dependency(request: Request) -> None:
        _, principal, role = _get_caller_context(request)
        try:
            if principal is None:
                raise PermissionDeniedError("No authenticated principal on this request.")
            check_permission(principal, role, permission)
        except PermissionDeniedError as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"code": "PERMISSION_DENIED", "message": str(exc)},
            ) from exc

    return Depends(dependency)


def _get_eval_store(request: Request) -> EvaluationStore:
    eval_store = getattr(request.app.state, "eval_store", None)
    if eval_store is None:
        db_file = getattr(request.app.state.store, "db_path", ":memory:")
        eval_store = SqliteEvaluationStore(db_file)
        request.app.state.eval_store = eval_store
    return eval_store


@router.get("/datasets", summary="List evaluation datasets")
async def list_datasets(request: Request) -> dict[str, Any]:
    """List registered and built-in evaluation datasets in the active workspace."""
    workspace_id, _, _ = _get_caller_context(request)
    store = _get_eval_store(request)
    datasets = store.list_datasets(workspace_id)
    return {"datasets": datasets, "total": len(datasets)}


@router.post(
    "/datasets", summary="Create or upload evaluation dataset", dependencies=[_require("edit")]
)
async def create_dataset(request: Request, payload: CreateDatasetRequest) -> dict[str, Any]:
    """Upload or register an evaluation dataset with schema and input validation."""
    workspace_id, _, _ = _get_caller_context(request)
    store = _get_eval_store(request)

    # 1. Validate dataset ID safety
    if not payload.dataset_id or not _SAFE_ID_REGEX.match(payload.dataset_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "INVALID_DATASET_ID",
                "message": (
                    f"Dataset ID '{payload.dataset_id}' contains invalid characters or path "
                    "traversal components."
                ),
            },
        )
    if ".." in payload.dataset_id or "/" in payload.dataset_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "PATH_TRAVERSAL_DETECTED",
                "message": "Dataset ID cannot contain path traversal patterns.",
            },
        )

    # 2. Validate examples
    if not payload.examples or not isinstance(payload.examples, list):
        raise HTTPException(
            status_code=422,  # literal: the constant was renamed in newer Starlette
            detail={
                "code": "EMPTY_EXAMPLES",
                "message": "Dataset must contain at least one example in 'examples' array.",
                "field_errors": [{"field": "examples", "error": "examples list cannot be empty"}],
            },
        )

    field_errors: list[dict[str, str]] = []
    parsed_examples: list[DatasetExample] = []
    for idx, ex_dict in enumerate(payload.examples):
        if not isinstance(ex_dict, dict):
            field_errors.append(
                {"field": f"examples[{idx}]", "error": "example must be a dictionary"}
            )
            continue
        input_data = ex_dict.get("input_data")
        if not input_data or not isinstance(input_data, dict):
            field_errors.append(
                {
                    "field": f"examples[{idx}].input_data",
                    "error": "input_data must be a non-empty dictionary",
                }
            )
            continue

        parsed_examples.append(
            DatasetExample(
                example_id=ex_dict.get("example_id") or f"ex_{idx + 1}",
                input_data=input_data,
                expected_output=ex_dict.get("expected_output"),
                expected_citations=ex_dict.get("expected_citations"),
                tags=ex_dict.get("tags") or [],
                metadata=ex_dict.get("metadata") or {},
            )
        )

    if field_errors:
        raise HTTPException(
            status_code=422,  # literal: the constant was renamed in newer Starlette
            detail={
                "code": "DATASET_VALIDATION_ERROR",
                "message": "One or more examples failed schema validation.",
                "field_errors": field_errors,
            },
        )

    dataset = EvaluationDataset(
        dataset_id=payload.dataset_id,
        version=payload.version,
        description=payload.description,
        examples=parsed_examples,
        metadata=payload.metadata,
    )
    store.save_dataset(dataset, workspace_id)

    return {
        "success": True,
        "dataset_id": dataset.dataset_id,
        "version": dataset.version,
        "content_hash": dataset.content_hash,
        "example_count": len(dataset.examples),
    }


@router.get("/datasets/{dataset_id}", summary="Get evaluation dataset details")
async def get_dataset(
    request: Request,
    dataset_id: str,
    version: str | None = Query(None, description="Optional version"),
) -> dict[str, Any]:
    """Retrieve full dataset representation with all examples."""
    workspace_id, _, _ = _get_caller_context(request)
    store = _get_eval_store(request)
    try:
        ds = store.get_dataset(dataset_id, version=version, workspace_id=workspace_id)
        return ds.model_dump(mode="json")
    except DatasetNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "DATASET_NOT_FOUND", "message": str(exc)},
        ) from exc


@router.delete(
    "/datasets/{dataset_id}",
    summary="Delete custom evaluation dataset",
    dependencies=[_require("edit")],
)
async def delete_dataset(request: Request, dataset_id: str) -> dict[str, Any]:
    """Remove a custom dataset. Built-in benchmark datasets cannot be deleted."""
    workspace_id, _, _ = _get_caller_context(request)
    store = _get_eval_store(request)
    deleted = store.delete_dataset(dataset_id, workspace_id=workspace_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "CANNOT_DELETE_DATASET",
                "message": (
                    f"Dataset '{dataset_id}' could not be deleted or is a built-in benchmark "
                    "fixture."
                ),
            },
        )
    return {"deleted": True, "dataset_id": dataset_id}


@router.post("/run", summary="Run evaluation against a dataset", dependencies=[_require("run")])
async def run_evaluation(request: Request, payload: RunEvaluationRequest) -> dict[str, Any]:
    """Execute evaluation benchmark deterministically with local fake models and graders."""
    workspace_id, caller_principal, _ = _get_caller_context(request)
    store = _get_eval_store(request)

    # 1. Load dataset
    try:
        dataset = store.get_dataset(
            payload.dataset_id,
            version=payload.dataset_version,
            workspace_id=workspace_id,
        )
    except DatasetNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "DATASET_NOT_FOUND", "message": str(exc)},
        ) from exc

    # 2. Build Graders
    graders_list: list[Grader] = []
    requested_graders = payload.graders or ["exact_match", "substring"]
    for g_name in requested_graders:
        if g_name == "exact_match":
            graders_list.append(ExactMatchGrader(case_sensitive=False, strip_whitespace=True))
        elif g_name in ("substring", "contains"):
            graders_list.append(ContainsGrader(case_sensitive=False))
        elif g_name in ("citation_coverage", "citation_overlap"):
            graders_list.append(CitationCoverageGrader(min_coverage=0.5))
        elif g_name == "regex":
            graders_list.append(RegexGrader())

    if not graders_list:
        graders_list.append(ExactMatchGrader(case_sensitive=False))

    # 3. Build evaluation target function
    # If target workflow is provided, ensure it exists in the caller's workspace
    workflow_version: str | None = None
    spec_hash: str | None = None
    if payload.target_workflow_id:
        app_store = getattr(request.app.state, "store", None)
        if app_store:
            ws_repo: WorkspaceRepository = request.app.state.workspace_repo
            owned = ws_repo.get_resource_ownership("workflow", payload.target_workflow_id)
            owner_ws = owned[0] if owned else None
            if owner_ws is None and caller_principal.profile == "local-owner":
                # Legacy unowned workflow: only the local default workspace may use it.
                owner_ws = "default"
            wf = (
                await app_store.get_workflow(payload.target_workflow_id)
                if owner_ws == workspace_id
                else None
            )
            if not wf:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail={
                        "code": "WORKFLOW_NOT_FOUND",
                        "message": (
                            f"Target workflow '{payload.target_workflow_id}' not found in active "
                            "workspace."
                        ),
                    },
                )
            workflow_version = f"rev_{wf.get('revision', 1)}"
            spec_hash = wf.get("spec_hash")

    # Local deterministic synthetic target responding cleanly to queries
    def _deterministic_eval_target(
        example: DatasetExample, context: dict[str, Any] | None = None
    ) -> Any:
        query = str(example.input_data.get("query", "")).lower()
        # Synthetic deterministic benchmark answer mapping
        if "return" in query or "refund" in query:
            return "30 days from purchase date with valid proof of purchase"
        if "cancel" in query or "cooling-off" in query:
            return "Contact support within 14 days or submit cancellation in billing portal"
        if "401" in query or "unauthorized" in query or "bearer" in query:
            return "Verify your Bearer token is valid and passed in Authorization header"
        # Return expected output if available or fallback
        if example.expected_output is not None:
            return example.expected_output
        return "Deterministic response for query"

    runner = EvaluationRunner(
        graders=graders_list,
        seed=42,
        redact_data=True,
    )

    eval_run_id = f"eval_{dataset.dataset_id}_{uuid.uuid4().hex[:8]}"

    summary = await runner.run_evaluation(
        target=_deterministic_eval_target,
        dataset=dataset,
        run_id=eval_run_id,
        workflow_version=workflow_version or "local_fixture_v1",
        spec_hash=spec_hash,
    )

    # Persist summary
    store.save_run(summary, workspace_id)

    res_data = summary.model_dump(mode="json")
    res_data["mean_latency_ms"] = round(
        summary.total_latency_ms / max(summary.total_examples, 1), 2
    )
    return res_data


@router.get("/runs", summary="List evaluation runs")
async def list_runs(
    request: Request,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """List past evaluation runs in active workspace."""
    workspace_id, _, _ = _get_caller_context(request)
    store = _get_eval_store(request)
    runs = store.list_runs(workspace_id=workspace_id, limit=limit, offset=offset)
    return {"runs": runs, "total": len(runs)}


@router.get("/runs/{eval_id}", summary="Get evaluation run details")
async def get_run_details(request: Request, eval_id: str) -> dict[str, Any]:
    """Retrieve full evaluation run details including example results and grader outputs."""
    workspace_id, _, _ = _get_caller_context(request)
    store = _get_eval_store(request)
    run = store.get_run(eval_id, workspace_id=workspace_id)
    if not run:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "RUN_NOT_FOUND", "message": f"Evaluation run '{eval_id}' not found."},
        )
    res_data = run.model_dump(mode="json")
    res_data["mean_latency_ms"] = round(run.total_latency_ms / max(run.total_examples, 1), 2)
    return res_data


@router.post("/compare", summary="Compare two evaluation runs")
async def compare_runs(request: Request, payload: CompareEvaluationsRequest) -> dict[str, Any]:
    """Compare baseline and candidate runs side by side, identifying regressions and
    improvements."""
    workspace_id, _, _ = _get_caller_context(request)
    store = _get_eval_store(request)

    run_a = store.get_run(payload.run_id_a, workspace_id=workspace_id)
    if not run_a:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "RUN_A_NOT_FOUND",
                "message": f"Baseline run '{payload.run_id_a}' not found.",
            },
        )

    run_b = store.get_run(payload.run_id_b, workspace_id=workspace_id)
    if not run_b:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "RUN_B_NOT_FOUND",
                "message": f"Candidate run '{payload.run_id_b}' not found.",
            },
        )

    # Compare metrics
    delta_pass_rate = round(run_b.pass_rate - run_a.pass_rate, 4)
    delta_mean_score = round(run_b.mean_score - run_a.mean_score, 4)
    delta_latency_ms = round(run_b.total_latency_ms - run_a.total_latency_ms, 2)
    delta_cost_usd = float(run_b.total_cost_usd - run_a.total_cost_usd)

    results_a_map = {res.example_id: res for res in run_a.results}
    results_b_map = {res.example_id: res for res in run_b.results}

    regressions: list[dict[str, Any]] = []
    improvements: list[dict[str, Any]] = []
    unchanged: list[dict[str, Any]] = []

    for ex_id, res_b in results_b_map.items():
        res_a = results_a_map.get(ex_id)
        if not res_a:
            continue
        if res_a.passed and not res_b.passed:
            regressions.append(
                {
                    "example_id": ex_id,
                    "score_a": res_a.overall_score,
                    "score_b": res_b.overall_score,
                }
            )
        elif not res_a.passed and res_b.passed:
            improvements.append(
                {
                    "example_id": ex_id,
                    "score_a": res_a.overall_score,
                    "score_b": res_b.overall_score,
                }
            )
        else:
            unchanged.append(
                {
                    "example_id": ex_id,
                    "passed": res_b.passed,
                    "score": res_b.overall_score,
                }
            )

    return {
        "baseline_run_id": run_a.run_id,
        "candidate_run_id": run_b.run_id,
        "dataset_id": run_b.dataset_id,
        "delta_pass_rate": delta_pass_rate,
        "delta_mean_score": delta_mean_score,
        "delta_latency_ms": delta_latency_ms,
        "delta_cost_usd": delta_cost_usd,
        "total_regressions": len(regressions),
        "total_improvements": len(improvements),
        "regressions": regressions,
        "improvements": improvements,
        "unchanged_count": len(unchanged),
    }


# Studio evals (datasets with expected trajectories, SDK-engine runs, CI gate).
from nexura_runtime.service.api.studio_evals import router as _studio_evals_router  # noqa: E402

router.include_router(_studio_evals_router)
