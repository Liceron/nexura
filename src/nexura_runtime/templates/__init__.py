# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Starter workflow templates: a tested, offline-runnable catalog."""

from __future__ import annotations

from typing import TYPE_CHECKING

from nexura_runtime.templates.catalog import (
    Template,
    TemplateError,
    get_template,
    list_templates,
)
from nexura_runtime.templates.handlers import register_template_handlers, template_registry
from nexura_runtime.templates.runner import FixtureOutcome, run_fixture

if TYPE_CHECKING:
    from fastapi import APIRouter, FastAPI

__all__ = [
    "FixtureOutcome",
    "Template",
    "TemplateError",
    "get_template",
    "list_templates",
    "mount_templates",
    "register_template_handlers",
    "run_fixture",
    "template_registry",
]


def mount_templates(app: FastAPI, api_v1: APIRouter) -> None:
    """Register the template handlers on the app's registry and mount ``/templates``."""
    from nexura_runtime.service.api.templates import router

    register_template_handlers(app.state.registry)
    api_v1.include_router(router)
