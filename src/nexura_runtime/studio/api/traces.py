# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Run trace endpoint: a span tree of workflow node steps and agent-internal spans."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from nexura_runtime.studio.store import StudioStore
from nexura_runtime.studio.traces import build_trace

router = APIRouter(prefix="/runs", tags=["Run traces"])


@router.get("/{run_id}/trace")
async def run_trace(
    request: Request,
    run_id: str,
    include_content: bool = False,
) -> dict[str, Any]:
    """Span tree for a run. Inputs and outputs are redacted and only sent on request."""
    store: StudioStore = request.app.state.store
    run = await store.get_run(run_id)
    if run is None:
        raise HTTPException(404, detail={"code": "RUN_NOT_FOUND", "message": "Run not found"})
    if hasattr(request.app.state, "workspace_repo"):
        from nexura_runtime.service.api.runs import _assert_run_ownership

        _assert_run_ownership(request, run_id)
        if request.app.state.workspace_repo.get_resource_workspace("run", run_id) is None:
            raise HTTPException(
                403,
                detail={"code": "RUN_OWNERSHIP_UNKNOWN", "message": "Run ownership unavailable"},
            )
    ledger = getattr(request.app.state, "usage_ledger", None)
    pricing_table = getattr(request.app.state, "pricing_table", None)
    retention_seconds = getattr(request.app.state, "diagnostic_retention_seconds", None)
    return build_trace(
        run,
        ledger=ledger,
        pricing_table=pricing_table,
        include_content=include_content,
        retention_seconds=retention_seconds,
    )
