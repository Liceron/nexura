# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Nexura Studio: the local workflow and approval web page and its launcher.

Distributed as ``nexura-studio`` (it depends on ``nexura-runtime`` with FastAPI and uvicorn).
The ``nexura-studio`` command starts it; ``create_app`` builds the FastAPI application. The
run store, built-in demo modules, traces and security middleware it uses are part of
``nexura-runtime`` (``nexura_runtime.studio``), which the REST service shares.
"""

from __future__ import annotations

from typing import Any

try:
    import fastapi  # noqa: F401
    import uvicorn  # noqa: F401

    _STUDIO_AVAILABLE = True
except ImportError:
    _STUDIO_AVAILABLE = False


def create_app(*args: Any, **kwargs: Any) -> Any:
    """Create configured FastAPI app, checking for optional studio extra dependencies."""
    if not _STUDIO_AVAILABLE:
        raise ImportError(
            "Nexura Studio requires the 'studio' extra dependencies (fastapi, uvicorn).\n"
            "Please install them with: pip install 'nexura-runtime[studio]'"
        )
    from nexura_studio.server import create_app as _create_app

    return _create_app(*args, **kwargs)


__all__ = ["create_app"]
