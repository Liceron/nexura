# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""REST API router for evaluation datasets, benchmarks, grading, and run comparisons."""

from __future__ import annotations

import asyncio
import re
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from nexura_runtime.evaluation._service_access import registered_agents
from nexura_runtime.evaluation.errors import (
    DatasetNotFoundError,
)
from nexura_runtime.evaluation.graders import (
    CitationCoverageGrader,
    ContainsGrader,
    ExactMatchGrader,
    Grader,
    RegexGrader,
    ToolArgumentAccuracyGrader,
    TrajectoryGrader,
)
from nexura_runtime.evaluation.models import (
    ArgumentMatch,
    DatasetExample,
    EvaluationDataset,
    EvaluationSummary,
    OnlineEvalConfig,
    StoredTrace,
    TrajectoryMode,
)
from nexura_runtime.evaluation.runner import EvaluationRunner, redact_trace, simulate_user
from nexura_runtime.evaluation.store import EvaluationStore
from nexura_runtime.service.api.models import (
    CompareEvaluationsRequest,
    CreateDatasetRequest,
    RunEvaluationRequest,
)
from nexura_runtime.service.auth.errors import PermissionDeniedError
from nexura_runtime.service.auth.models import ActionPermission, Principal, WorkspaceRole
from nexura_runtime.service.auth.permissions import check_permission
from nexura_runtime.service.storage_backend import store_call
from nexura_runtime.service.workspaces.repository import WorkspaceRepository

_SAFE_ID_REGEX = re.compile(r"^[a-zA-Z0-9_\-\.]+$")


def _get_caller_context(request: Request) -> tuple[str, Principal, WorkspaceRole]:
    caller_ws = getattr(request.state, "workspace_id", None)
    principal = getattr(request.state, "principal", None)
    if not isinstance(caller_ws, str) or not caller_ws or not isinstance(principal, Principal):
        raise HTTPException(403, detail={"code": "PERMISSION_DENIED"})
    raw_role = getattr(request.state, "role", "viewer")
    role: WorkspaceRole = (
        "admin" if raw_role == "admin" else ("editor" if raw_role == "editor" else "viewer")
    )
    return caller_ws, principal, role


def _evaluation_access(request: Request) -> None:
    """Reports/imports cannot yet enforce per-workflow/starter resource scopes."""
    _, principal, _ = _get_caller_context(request)
    if principal.resources is not None:
        raise HTTPException(403, detail={"code": "RESOURCE_SCOPE_DENIED"})


router = APIRouter(
    prefix="/evaluations", tags=["evaluations"], dependencies=[Depends(_evaluation_access)]
)


async def _assert_trace_access(request: Request, trace: dict[str, Any]) -> None:
    """Check known run/workflow references, including nested conversation captures.

    New standalone imports need no Studio row. Existing unowned rows are legacy
    resources accessible only to the default local owner. Never adopt foreign IDs.
    """
    workspace_id, principal, _ = _get_caller_context(request)
    repo: WorkspaceRepository = request.app.state.workspace_repo
    app_store = request.app.state.store
    legacy = principal.profile == "local-owner" and workspace_id == "default"
    for kind in ("run", "workflow"):
        resource_id = trace.get(f"{kind}_id")
        if not resource_id:
            continue
        ownership = repo.get_resource_ownership(kind, str(resource_id))
        owner = ownership[0] if ownership else None
        if owner is not None:
            allowed = owner == workspace_id
        else:
            getter = app_store.get_run if kind == "run" else app_store.get_workflow
            allowed = legacy or await getter(str(resource_id)) is None
        if not allowed:
            raise HTTPException(403, detail={"code": "CROSS_WORKSPACE_ACCESS_DENIED"})
    for child in [*(trace.get("runs") or []), *(trace.get("events") or [])]:
        if isinstance(child, dict):
            await _assert_trace_access(request, child)


async def _assert_summary_access(request: Request, summary: EvaluationSummary) -> None:
    for result in summary.results:
        trace = result.metadata.get("trace")
        if isinstance(trace, dict):
            await _assert_trace_access(request, trace)
    for run_id in summary.metadata.get("source_run_ids", []):
        await _assert_trace_access(request, {"run_id": run_id})


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
    return request.app.state.eval_store  # type: ignore[no-any-return]


class TraceGraderSpec(BaseModel):
    """JSON configuration shared by replay and simulation endpoints."""

    model_config = ConfigDict(extra="forbid")
    type: str = "tool_trajectory"
    mode: TrajectoryMode = "exact"
    check_arguments: bool = False
    argument_rules: dict[str, ArgumentMatch] = Field(default_factory=dict)


class TraceEvaluationRequest(BaseModel):
    """Grade existing traces using shared expectations, without dispatching an agent."""

    model_config = ConfigDict(extra="forbid")
    example: DatasetExample
    graders: list[TraceGraderSpec] = Field(default_factory=lambda: [TraceGraderSpec()])
    config: OnlineEvalConfig = Field(default_factory=OnlineEvalConfig)


class SimulationEvaluationRequest(BaseModel):
    """Evaluate a versioned dataset using a host-registered agent and user model."""

    model_config = ConfigDict(extra="forbid")
    dataset_id: str
    dataset_version: str | None = None
    agent_id: str = "local-fake"
    graders: list[TraceGraderSpec] = Field(
        default_factory=lambda: [TraceGraderSpec(type="exact_match")]
    )


def _summary_payload(summary: EvaluationSummary) -> dict[str, Any]:
    data = summary.model_dump(mode="json")
    data["mean_latency_ms"] = round(summary.total_latency_ms / max(summary.total_examples, 1), 2)
    return data


def _trace_graders(specs: list[TraceGraderSpec]) -> list[Grader]:
    if not specs:
        raise HTTPException(422, detail={"code": "INVALID_GRADER", "message": "Provide graders"})
    graders: list[Grader] = []
    for spec in specs:
        if spec.type == "tool_argument_accuracy":
            graders.append(
                ToolArgumentAccuracyGrader(mode=spec.mode, argument_rules=dict(spec.argument_rules))
            )
        elif spec.type == "tool_trajectory":
            graders.append(
                TrajectoryGrader(
                    mode=spec.mode,
                    check_arguments=spec.check_arguments,
                    argument_rules=dict(spec.argument_rules),
                )
            )
        elif spec.type == "exact_match":
            graders.append(ExactMatchGrader())
        elif spec.type in ("contains", "substring"):
            graders.append(ContainsGrader())
        else:
            raise HTTPException(422, detail={"code": "INVALID_GRADER", "message": spec.type})
    if len({g.name for g in graders}) != len(graders):
        raise HTTPException(422, detail={"code": "INVALID_GRADER", "message": "Duplicate graders"})
    return graders


@router.get("/graders", summary="Evaluation grader capabilities")
async def grader_capabilities() -> dict[str, Any]:
    return {
        "types": [
            "exact_match",
            "contains",
            "regex",
            "citation_coverage",
            "tool_trajectory",
            "tool_argument_accuracy",
        ],
        "trajectory_modes": ["exact", "subset", "in_order", "any_order"],
        "argument_rule_schema": ArgumentMatch.model_json_schema(),
        "result_schema": EvaluationSummary.model_json_schema(),
        "simulation_schema": SimulationEvaluationRequest.model_json_schema(),
        "online_config_schema": OnlineEvalConfig.model_json_schema(),
    }


@router.post("/traces", dependencies=[_require("edit")], summary="Import recorded trace")
async def import_trace(request: Request, payload: StoredTrace) -> dict[str, Any]:
    workspace_id, _, _ = _get_caller_context(request)
    await _assert_trace_access(request, payload.model_dump())
    # Only captured content is redacted; typed identity/time fields stay intact.
    try:
        cleaned = StoredTrace.model_validate(redact_trace(payload.model_dump()))
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, detail={"code": "INVALID_TRACE"}) from exc
    # Off the event loop: a large trace is uploaded to the blob store (WP2b).
    await asyncio.to_thread(_get_eval_store(request).save_trace, cleaned, workspace_id)
    return {"run_id": payload.run_id, "source": "imported_trace"}


@router.post("/online", dependencies=[_require("run")], summary="Grade stored traces offline")
async def evaluate_online(request: Request, payload: TraceEvaluationRequest) -> dict[str, Any]:
    workspace_id, principal, _ = _get_caller_context(request)
    eval_store = _get_eval_store(request)
    graders = _trace_graders(payload.graders)
    # Off the event loop: offloaded traces are read from the blob store (WP2b).
    stored_traces = await asyncio.to_thread(eval_store.list_traces, workspace_id)
    traces = {t.run_id: t for t in stored_traces}
    app_store = getattr(request.app.state, "store", None)
    repo: WorkspaceRepository = request.app.state.workspace_repo
    if app_store is not None:
        # Match existing run ownership semantics without writing/adopting run records.
        owned = repo.list_workspace_resource_ids("run", workspace_id)
        legacy = principal.profile == "local-owner" and workspace_id == "default"
        for row in await app_store.list_runs(limit=None, run_ids=None if legacy else owned):
            run_id = row["run_id"]
            owner = repo.get_resource_workspace("run", run_id)
            if owner != workspace_id and not (legacy and owner is None):
                continue
            # Do not evaluate in-flight snapshots.
            if row.get("status") not in ("completed", "failed", "cancelled"):
                continue
            run = await app_store.get_run(run_id)
            if run:
                # Imports are explicit snapshots: preserve their capture, identity
                # and timestamp as one coherent record, evaluated only once.
                if run_id in traces:
                    continue
                traces[run_id] = StoredTrace(
                    run_id=run_id,
                    workflow_id=run.get("workflow_id"),
                    agent_id=run.get("agent_id"),
                    timestamp=run["created_at"],
                    output=run.get("output", run.get("current_state")),
                    events=run.get("events", []),
                    messages=run.get("messages", []),
                )
    for trace in traces.values():
        await _assert_trace_access(request, trace.model_dump())
    summary = await EvaluationRunner(graders).evaluate_traces(
        traces.values(), payload.example, config=payload.config
    )
    await store_call(eval_store, eval_store.save_run, summary, workspace_id)
    return _summary_payload(summary)


@router.post(
    "/simulate", dependencies=[_require("run")], summary="Evaluate simulated conversations"
)
async def evaluate_simulation(
    request: Request, payload: SimulationEvaluationRequest
) -> dict[str, Any]:
    from nexura_runtime.evaluation.studio_engine import local_fake_agent

    workspace_id, principal, _ = _get_caller_context(request)
    store = _get_eval_store(request)
    try:
        dataset = await store_call(
            store, store.get_dataset, payload.dataset_id, payload.dataset_version, workspace_id
        )
    except DatasetNotFoundError as exc:
        raise HTTPException(404, detail={"code": "DATASET_NOT_FOUND", "message": str(exc)}) from exc
    if any(e.simulation is None for e in dataset.examples):
        raise HTTPException(422, detail={"code": "SIMULATION_REQUIRED"})
    legacy = principal.profile == "local-owner" and workspace_id == "default"
    user_model = getattr(request.app.state, "eval_user_models", {}).get(workspace_id)
    if user_model is None and legacy:
        user_model = getattr(request.app.state, "eval_user_model", None)
    if (
        any(e.simulation and e.simulation.persona is not None for e in dataset.examples)
        and user_model is None
    ):
        raise HTTPException(422, detail={"code": "USER_MODEL_UNAVAILABLE"})
    registered = registered_agents(
        request.app.state, workspace_id, local_owner=principal.profile == "local-owner"
    )
    factory = registered.get(payload.agent_id)
    if payload.agent_id == "local-fake":
        factory = local_fake_agent
    if factory is None:
        raise HTTPException(404, detail={"code": "AGENT_NOT_FOUND"})
    runner = EvaluationRunner(_trace_graders(payload.graders), user_model=user_model)

    originals = {e.example_id: e for e in dataset.examples}

    async def dispatch(example: DatasetExample) -> dict[str, Any]:
        from nexura_agents.types import Usage

        plan = originals[example.example_id].simulation
        assert plan is not None
        result = await simulate_user(factory(None), plan, model=user_model)
        result["usage"] = Usage(**result["usage"])
        return result

    # The callable owns simulation dispatch; the outer runner only grades its result.
    outer = dataset.model_copy(
        update={"examples": [e.model_copy(update={"simulation": None}) for e in dataset.examples]}
    )
    summary = await runner.run(dispatch, outer, run_id=f"eval_sim_{uuid.uuid4().hex}")
    summary.dataset_digest = summary.dataset_hash = dataset.content_hash
    summary.metadata.update({"source": "user_simulation", "agent_id": payload.agent_id})
    summary.is_synthetic_evidence = True
    await store_call(store, store.save_run, summary, workspace_id)
    return _summary_payload(summary)


@router.get("/datasets", summary="List evaluation datasets")
async def list_datasets(request: Request) -> dict[str, Any]:
    """List registered and built-in evaluation datasets in the active workspace."""
    workspace_id, _, _ = _get_caller_context(request)
    store = _get_eval_store(request)
    datasets = await store_call(store, store.list_datasets, workspace_id)
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

        try:
            parsed = DatasetExample(
                example_id=ex_dict.get("example_id") or f"ex_{idx + 1}",
                input_data=input_data,
                expected_output=ex_dict.get("expected_output"),
                expected_citations=ex_dict.get("expected_citations"),
                expected_trajectory=ex_dict.get("expected_trajectory"),
                simulation=ex_dict.get("simulation"),
                tags=ex_dict.get("tags") or [],
                metadata=ex_dict.get("metadata") or {},
            )
            parsed_examples.append(parsed)
        except ValidationError as exc:
            field_errors.append({"field": f"examples[{idx}]", "error": str(exc)})

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
    await store_call(store, store.save_dataset, dataset, workspace_id)

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
        ds = await store_call(
            store, store.get_dataset, dataset_id, version=version, workspace_id=workspace_id
        )
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
    deleted = await store_call(store, store.delete_dataset, dataset_id, workspace_id=workspace_id)
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
        dataset = await store_call(
            store,
            store.get_dataset,
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
        elif g_name == "tool_trajectory":
            graders_list.append(TrajectoryGrader())
        elif g_name == "tool_argument_accuracy":
            graders_list.append(ToolArgumentAccuracyGrader())

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
    await store_call(store, store.save_run, summary, workspace_id)

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
    runs = await store_call(
        store, store.list_runs, workspace_id=workspace_id, limit=limit, offset=offset
    )
    for row in runs:
        summary = await store_call(store, store.get_run, row["run_id"], workspace_id=workspace_id)
        if summary is not None:
            await _assert_summary_access(request, summary)
    return {"runs": runs, "total": len(runs)}


@router.get("/runs/{eval_id}", summary="Get evaluation run details")
async def get_run_details(request: Request, eval_id: str) -> dict[str, Any]:
    """Retrieve full evaluation run details including example results and grader outputs."""
    workspace_id, _, _ = _get_caller_context(request)
    store = _get_eval_store(request)
    run = await store_call(store, store.get_run, eval_id, workspace_id=workspace_id)
    if not run:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "RUN_NOT_FOUND", "message": f"Evaluation run '{eval_id}' not found."},
        )
    await _assert_summary_access(request, run)
    res_data = run.model_dump(mode="json")
    res_data["mean_latency_ms"] = round(run.total_latency_ms / max(run.total_examples, 1), 2)
    return res_data


@router.delete(
    "/runs/{eval_id}", summary="Delete an evaluation run", dependencies=[_require("edit")]
)
async def delete_eval_run(request: Request, eval_id: str) -> dict[str, Any]:
    """Delete one benchmark evaluation run (summary and per-example results) of the caller's
    workspace. Needs the ``edit`` permission; 404 ``RUN_NOT_FOUND`` otherwise."""
    workspace_id, _, _ = _get_caller_context(request)
    store = _get_eval_store(request)
    run = await store_call(store, store.get_run, eval_id, workspace_id=workspace_id)
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "RUN_NOT_FOUND", "message": f"Evaluation run '{eval_id}' not found."},
        )
    await _assert_summary_access(request, run)
    if not await store_call(store, store.delete_run, eval_id, workspace_id=workspace_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "RUN_NOT_FOUND", "message": f"Evaluation run '{eval_id}' not found."},
        )
    return {"deleted": True, "run_id": eval_id}


@router.post("/compare", summary="Compare two evaluation runs")
async def compare_runs(request: Request, payload: CompareEvaluationsRequest) -> dict[str, Any]:
    """Compare baseline and candidate runs side by side, identifying regressions and
    improvements."""
    workspace_id, _, _ = _get_caller_context(request)
    store = _get_eval_store(request)

    run_a = await store_call(store, store.get_run, payload.run_id_a, workspace_id=workspace_id)
    if not run_a:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "RUN_A_NOT_FOUND",
                "message": f"Baseline run '{payload.run_id_a}' not found.",
            },
        )

    run_b = await store_call(store, store.get_run, payload.run_id_b, workspace_id=workspace_id)
    if not run_b:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "RUN_B_NOT_FOUND",
                "message": f"Candidate run '{payload.run_id_b}' not found.",
            },
        )

    await _assert_summary_access(request, run_a)
    await _assert_summary_access(request, run_b)

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
