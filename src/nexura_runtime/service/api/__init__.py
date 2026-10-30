# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Nexura Service API and Client public interfaces."""

from __future__ import annotations

from typing import Any

from nexura_runtime.service.api.client import (
    NexuraApiError,
    NexuraClient,
    RunsClient,
    WorkflowsClient,
    WorkspacesClient,
)
from nexura_runtime.service.api.export import ExportBundle, WorkflowExporter

try:
    from nexura_runtime.service.api.app import create_api_app
    from nexura_runtime.service.api.middleware import RequestIdMiddleware
except (ImportError, TypeError):
    create_api_app: Any = None  # type: ignore[no-redef]
    RequestIdMiddleware: Any = None  # type: ignore[no-redef]
from nexura_runtime.service.api.models import (
    CancelRunRequest,
    CreateWorkflowRequest,
    DecisionRequest,
    ExportBundleResponse,
    ExportWorkflowRequest,
    PaginatedResponse,
    PaginationParams,
    RunEventModel,
    RunResponse,
    StartRunRequest,
    UpdateWorkflowRequest,
)

__all__ = [
    "create_api_app",
    "NexuraClient",
    "NexuraApiError",
    "WorkflowsClient",
    "RunsClient",
    "WorkspacesClient",
    "WorkflowExporter",
    "ExportBundle",
    "RequestIdMiddleware",
    "PaginationParams",
    "PaginatedResponse",
    "CreateWorkflowRequest",
    "UpdateWorkflowRequest",
    "StartRunRequest",
    "DecisionRequest",
    "CancelRunRequest",
    "ExportWorkflowRequest",
    "ExportBundleResponse",
    "RunResponse",
    "RunEventModel",
]
