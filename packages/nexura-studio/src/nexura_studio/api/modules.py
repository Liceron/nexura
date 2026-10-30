# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""API endpoints for inspecting available registered workflow modules."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from nexura_runtime.studio.builtin_modules import get_default_module_metadata
from nexura_runtime.workflow import ModuleRegistry

router = APIRouter(prefix="/modules", tags=["modules"])


@router.get("", response_model=dict[str, list[dict[str, Any]]])
async def list_modules(request: Request) -> dict[str, list[dict[str, Any]]]:
    """List all trusted Python modules registered in this Studio instance."""
    registry: ModuleRegistry = request.app.state.registry
    metadata_lookup = {m["name"]: m for m in get_default_module_metadata()}

    modules: list[dict[str, Any]] = []
    # Inspect registered handlers
    for name, fn in registry._handlers.items():
        meta = metadata_lookup.get(name)
        if meta:
            modules.append(dict(meta))
        else:
            doc = fn.__doc__ or "Custom developer-registered Python handler."
            modules.append(
                {
                    "name": name,
                    "title": name.replace("_", " ").title(),
                    "description": doc.strip().split("\n")[0],
                    "disclaimer": "Custom Trusted Python Handler (Registered at create_app)",
                    "is_synthetic": False,
                    "parameters": [],
                    "outputs": [],
                    "branches": [],
                }
            )

    return {"modules": modules}
