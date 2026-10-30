# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Detect a Nexura project and load what it asks ``nexura-runtime dev`` to register.

A project declares a ``[tool.nexura]`` table in its ``pyproject.toml``::

    [tool.nexura]
    template = "workflow"
    handlers = ["my_app.studio:registry"]     # same format as --handlers
    workflows = ["my_app.studio:workflows"]   # dict of workflow id -> WorkflowSpec

Both lists name trusted code of the project itself, chosen by the developer who runs the
command; workflow JSON never chooses what is imported.
"""

from __future__ import annotations

import importlib
import re
import sys
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nexura_agents.errors import WorkflowValidationError

from nexura_runtime.studio.builtin_modules import get_default_registry
from nexura_runtime.workflow import ModuleRegistry, WorkflowSpec

WORKFLOW_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,79}")


class ProjectError(ValueError):
    """The project's ``[tool.nexura]`` table or the code it names cannot be used."""


@dataclass(frozen=True)
class ProjectConfig:
    """A project's ``[tool.nexura]`` settings: its root, name, template and code sources."""

    root: Path
    name: str
    template: str | None
    handlers: tuple[str, ...]
    workflows: tuple[str, ...]


def _targets(table: Mapping[str, Any], key: str) -> tuple[str, ...]:
    value = table.get(key, [])
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise ProjectError(f"[tool.nexura] {key} must be a list of 'package.module:attribute'")
    for target in value:
        module, sep, attr = target.partition(":")
        if not sep or not module or not attr:
            raise ProjectError(f"[tool.nexura] {key}: '{target}' is not 'package.module:attribute'")
    return tuple(value)


def find_project(root: Path) -> ProjectConfig | None:
    """The project in ``root``, or None when it has no ``[tool.nexura]`` table."""
    pyproject = root / "pyproject.toml"
    if not pyproject.is_file():
        return None
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ProjectError(f"cannot read {pyproject}: {exc}") from exc
    table = data.get("tool", {}).get("nexura")
    if table is None:
        return None
    if not isinstance(table, dict):
        raise ProjectError("[tool.nexura] must be a table")
    template = table.get("template")
    name = data.get("project", {}).get("name") or root.resolve().name
    return ProjectConfig(
        root=root.resolve(),
        name=str(name),
        template=str(template) if template is not None else None,
        handlers=_targets(table, "handlers"),
        workflows=_targets(table, "workflows"),
    )


def _on_path(root: Path) -> None:
    entry = str(root)
    if entry not in sys.path:
        sys.path.insert(0, entry)


def project_registry(config: ProjectConfig | None) -> ModuleRegistry:
    """Studio's built-in handlers plus the project's handler sources."""
    registry = get_default_registry()
    if config is None:
        return registry
    _on_path(config.root)
    for target in config.handlers:
        try:
            registry.include_from(target)
        except WorkflowValidationError as exc:
            raise ProjectError(str(exc)) from exc
    return registry


def project_workflows(config: ProjectConfig | None) -> dict[str, WorkflowSpec]:
    """Workflow id -> spec for every ``workflows`` source of the project."""
    found: dict[str, WorkflowSpec] = {}
    if config is None:
        return found
    _on_path(config.root)
    for target in config.workflows:
        module_name, _, attr = target.partition(":")
        try:
            value = getattr(importlib.import_module(module_name), attr)
        except (ImportError, AttributeError) as exc:
            raise ProjectError(f"cannot load workflows from '{target}': {exc}") from exc
        if callable(value) and not isinstance(value, Mapping):
            value = value()
        if not isinstance(value, Mapping):
            raise ProjectError(f"'{target}' must be a dict of workflow id -> WorkflowSpec")
        for workflow_id, spec in value.items():
            if not isinstance(workflow_id, str) or not WORKFLOW_ID.fullmatch(workflow_id):
                raise ProjectError(
                    f"'{target}': workflow id {workflow_id!r} must be 1-80 lowercase letters, "
                    "digits, '-' or '_'"
                )
            if not isinstance(spec, WorkflowSpec):
                raise ProjectError(f"'{target}': '{workflow_id}' is not a WorkflowSpec")
            if workflow_id in found:
                raise ProjectError(f"workflow id '{workflow_id}' is declared twice")
            found[workflow_id] = spec
    return found
