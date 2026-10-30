# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Scoped Studio diagnostics using the same SDK trace conversion as exports."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from nexura_agents.usage.ledger import UsageLedger

from nexura_runtime.observability.run_diagnostics import describe_run
from nexura_runtime.service.api.runs import _assert_run_ownership
from nexura_runtime.service.storage_backend import app_usage_ledger
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
    stores = getattr(request.app.state, "service_stores", None)
    if ledger is None and (
        getattr(store, "db_path", None) or getattr(stores, "database_url", None)
    ):
        # One app-level ledger (shared with budgets) replaces the per-request reload. On Postgres
        # (MVP WP5) its ``get_entries`` reads only this run's entries, without the budget lock.
        ledger = app_usage_ledger(request.app.state)
    pricing_table = getattr(request.app.state, "pricing_table", None)
    retention_seconds = getattr(request.app.state, "diagnostic_retention_seconds", None)

    # The ledger read may wait on the database (Postgres); keep the event loop free.
    return await asyncio.to_thread(
        describe_run,
        run,
        ledger=ledger,
        pricing_table=pricing_table,
        include_content=include_content,
        retention_seconds=retention_seconds,
    )
