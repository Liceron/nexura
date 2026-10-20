# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Scoped Studio diagnostics using the same SDK trace conversion as exports."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from nexura_agents.usage.ledger import UsageLedger

from nexura_runtime.observability.run_diagnostics import describe_run
from nexura_runtime.service.api.runs import _assert_run_ownership
from nexura_runtime.studio.store import StudioStore

router = APIRouter(prefix="/runs", tags=["Run diagnostics"])


@router.get("/{run_id}/diagnostics")
async def run_diagnostics(
    request: Request,
    run_id: str,
    include_content: bool = False,
) -> dict[str, Any]:
    store: StudioStore = request.app.state.store
    run = await store.get_run(run_id)
    if run is None:
        raise HTTPException(404, detail={"code": "RUN_NOT_FOUND", "message": "Run not found"})
    _assert_run_ownership(request, run_id)
    if request.app.state.workspace_repo.get_resource_workspace("run", run_id) is None:
        raise HTTPException(
            403,
            detail={"code": "RUN_OWNERSHIP_UNKNOWN", "message": "Run ownership unavailable"},
        )

    ledger: UsageLedger | None = getattr(request.app.state, "usage_ledger", None)
    if ledger is None and hasattr(store, "db_path") and store.db_path:
        ledger = UsageLedger(db_path=str(store.db_path))

    pricing_table = getattr(request.app.state, "pricing_table", None)
    retention_seconds = getattr(request.app.state, "diagnostic_retention_seconds", None)

    return describe_run(
        run,
        ledger=ledger,
        pricing_table=pricing_table,
        include_content=include_content,
        retention_seconds=retention_seconds,
    )
