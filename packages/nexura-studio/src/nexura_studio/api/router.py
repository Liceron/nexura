# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Main API v1 router combining modules, workflows, and runs endpoints."""

from __future__ import annotations

from fastapi import APIRouter

from nexura_runtime.studio.api.traces import router as traces_router
from nexura_studio.api.modules import router as modules_router
from nexura_studio.api.runs import router as runs_router
from nexura_studio.api.workflows import router as workflows_router

api_v1_router = APIRouter(prefix="/api/v1")
api_v1_router.include_router(modules_router)
api_v1_router.include_router(workflows_router)
api_v1_router.include_router(runs_router)
api_v1_router.include_router(traces_router)
