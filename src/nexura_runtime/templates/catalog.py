# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""The versioned template catalog: JSON files in ``data/``, validated when loaded."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from nexura_runtime.templates.handlers import template_registry
from nexura_runtime.workflow import WorkflowSpec

DATA_DIR = Path(__file__).parent / "data"
TEMPLATE_FORMAT_VERSION = 1
DIFFICULTIES = ("beginner", "intermediate", "advanced")


class TemplateError(ValueError):
    """A template file is malformed or does not validate against the engine."""


@dataclass(frozen=True)
class Template:
    """One starter workflow: metadata, the spec, a sample input and a test fixture."""

    id: str
    title: str
    description: str
    category: str
    difficulty: str
    tags: tuple[str, ...]
    required_modules: tuple[str, ...]
    required_credentials: tuple[str, ...]
    spec: dict[str, Any]
    sample_input: dict[str, Any]
    fixture: dict[str, Any]
    template_version: int = TEMPLATE_FORMAT_VERSION
    suggested_trigger: dict[str, Any] | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        """Catalog-card view: metadata only."""
        return {
            "id": self.id,
            "template_version": self.template_version,
            "title": self.title,
            "description": self.description,
            "category": self.category,
            "difficulty": self.difficulty,
            "tags": list(self.tags),
            "required_modules": list(self.required_modules),
            "required_credentials": list(self.required_credentials),
            "node_count": len(self.spec["nodes"]),
            "has_approval": any(n.get("requires_approval") for n in self.spec["nodes"].values()),
            "has_trigger": self.suggested_trigger is not None,
        }

    def detail(self) -> dict[str, Any]:
        """Full view: summary plus the spec, a Mermaid diagram and the sample input."""
        spec = WorkflowSpec.from_dict(copy.deepcopy(self.spec))
        view = self.summary()
        view.update(
            spec=copy.deepcopy(self.spec),
            mermaid=spec.to_mermaid(),
            sample_input=copy.deepcopy(self.sample_input),
            suggested_trigger=copy.deepcopy(self.suggested_trigger),
        )
        return view

    def new_spec(self, name: str | None = None) -> dict[str, Any]:
        """A fresh copy of the spec, optionally renamed, ready to create as a workflow."""
        spec = copy.deepcopy(self.spec)
        if name:
            spec["name"] = name
        return spec


def _load_file(path: Path) -> Template:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TemplateError(f"{path.name}: unreadable template ({exc})") from exc
    try:
        template = Template(
            id=str(raw["id"]),
            title=str(raw["title"]),
            description=str(raw["description"]),
            category=str(raw["category"]),
            difficulty=str(raw["difficulty"]),
            tags=tuple(raw["tags"]),
            required_modules=tuple(raw["required_modules"]),
            required_credentials=tuple(raw["required_credentials"]),
            spec=raw["spec"],
            sample_input=raw["sample_input"],
            fixture=raw["fixture"],
            template_version=int(raw["template_version"]),
            suggested_trigger=raw.get("suggested_trigger"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise TemplateError(f"{path.name}: missing or invalid field ({exc!r})") from exc
    if template.id != path.stem:
        raise TemplateError(f"{path.name}: id '{template.id}' must match the file name")
    if template.template_version != TEMPLATE_FORMAT_VERSION:
        raise TemplateError(f"{path.name}: unsupported template_version")
    if template.difficulty not in DIFFICULTIES:
        raise TemplateError(f"{path.name}: difficulty must be one of {DIFFICULTIES}")
    if not template.fixture.get("runs"):
        raise TemplateError(f"{path.name}: a template needs at least one fixture run")
    used = sorted({n.get("handler") or n.get("module_id") for n in template.spec["nodes"].values()})
    if sorted(template.required_modules) != used:
        raise TemplateError(f"{path.name}: required_modules {used} must list the spec handlers")
    try:
        WorkflowSpec.from_dict(copy.deepcopy(template.spec)).validate(template_registry())
    except Exception as exc:
        raise TemplateError(f"{path.name}: spec does not validate: {exc}") from exc
    return template


@lru_cache(maxsize=1)
def _catalog() -> dict[str, Template]:
    found = [_load_file(p) for p in sorted(DATA_DIR.glob("*.json"))]
    return {t.id: t for t in found}


def list_templates(category: str | None = None, tag: str | None = None) -> list[Template]:
    """Templates, optionally narrowed to one category and/or tag (exact, case-insensitive)."""
    items = list(_catalog().values())
    if category:
        items = [t for t in items if t.category.lower() == category.lower()]
    if tag:
        items = [t for t in items if tag.lower() in (x.lower() for x in t.tags)]
    return items


def get_template(template_id: str) -> Template | None:
    """One template by id, or ``None``."""
    return _catalog().get(template_id)
