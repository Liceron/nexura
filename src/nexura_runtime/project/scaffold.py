# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Project templates for ``nexura-runtime init``.

Template files live in ``files/<template>/`` (plus ``files/common/``) with a ``.tmpl`` suffix,
so the linters and test collectors of this repository never read them as code. File names
starting with ``dot-`` become dotfiles, ``env.example`` becomes ``.env.example`` and a
``__package__`` directory becomes the project's import package. ``{{key}}`` placeholders are
replaced with plain string substitution; nothing in a template is evaluated.
"""

from __future__ import annotations

import keyword
import os
import re
import sys
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path

from nexura_runtime import __version__

TEMPLATE_SUFFIX = ".tmpl"
PACKAGE_DIR = "__package__"


class ScaffoldError(ValueError):
    """A project cannot be written as asked (bad name, non-empty directory, unknown template)."""


@dataclass(frozen=True)
class ProjectTemplate:
    """A template ``nexura-runtime init --template NAME`` can write, with its one-line summary."""

    name: str
    summary: str


TEMPLATES: dict[str, ProjectTemplate] = {
    t.name: t
    for t in (
        ProjectTemplate(
            "agent",
            "Tool-using agent with an approval-gated write tool and a token and cost budget.",
        ),
        ProjectTemplate(
            "workflow",
            "Approval-gated workflow with durable SQLite checkpoints, resumable after a restart.",
        ),
        ProjectTemplate(
            "rag",
            "Knowledge ingestion and answers with citations (test embedder; real one optional).",
        ),
    )
}
DEFAULT_TEMPLATE = "agent"

# Names a generated package must not take: it would shadow Nexura, a test layout or stdlib.
_RESERVED = {
    "nexura",
    "nexura_agents",
    "nexura_runtime",
    "nexura_studio",
    "tests",
    "test",
    "docs",
    "src",
}


@dataclass
class ScaffoldResult:
    """What ``scaffold_project`` wrote, and the existing files it kept (``force=True``)."""

    root: Path
    template: str
    package: str
    written: list[Path] = field(default_factory=list)
    skipped: list[Path] = field(default_factory=list)


def files_root() -> Traversable:
    return resources.files("nexura_runtime.project") / "files"


def _walk(node: Traversable, prefix: str = "") -> Iterator[tuple[str, Traversable]]:
    for child in sorted(node.iterdir(), key=lambda c: c.name):
        if child.name == "__pycache__":
            continue
        rel = f"{prefix}{child.name}"
        if child.is_dir():
            yield from _walk(child, rel + "/")
        elif child.name.endswith(TEMPLATE_SUFFIX):
            yield rel, child


def _target_name(part: str, package: str) -> str:
    if part == PACKAGE_DIR:
        return package
    if part.endswith(TEMPLATE_SUFFIX):
        part = part[: -len(TEMPLATE_SUFFIX)]
    if part.startswith("dot-"):
        return "." + part[4:]
    if part == "env.example":
        return ".env.example"
    return part


def render_text(text: str, values: Mapping[str, str]) -> str:
    """Replace ``{{key}}`` placeholders; an unknown placeholder is a template bug."""

    def substitute(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            raise ScaffoldError(f"template placeholder '{{{{{key}}}}}' has no value")
        return values[key]

    return re.sub(r"\{\{([a-z_]+)\}\}", substitute, text)


def template_files(names: list[str], package: str) -> dict[str, Traversable]:
    """Relative output path -> template file; later groups override earlier ones."""
    root = files_root()
    chosen: dict[str, Traversable] = {}
    for group in names:
        source = root / group
        if not source.is_dir():
            raise ScaffoldError(f"template files for '{group}' are missing from the package")
        for rel, node in _walk(source):
            out = "/".join(_target_name(part, package) for part in rel.split("/"))
            chosen[out] = node
    return chosen


def package_name_for(directory: Path, explicit: str | None = None) -> str:
    """The import package for a project directory (``my-agent`` -> ``my_agent``)."""
    raw = explicit if explicit is not None else directory.name
    name = raw if explicit is not None else re.sub(r"[^0-9a-zA-Z_]+", "_", raw).strip("_").lower()
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,63}", name or ""):
        raise ScaffoldError(
            f"'{raw}' does not give a valid Python package name; pass --name with lowercase "
            "letters, digits and underscores, starting with a letter"
        )
    if keyword.iskeyword(name) or name in _RESERVED or name in sys.stdlib_module_names:
        raise ScaffoldError(
            f"package name '{name}' would shadow Python, Nexura or a test folder; pass --name"
        )
    return name


def _project_name(package: str) -> str:
    return package.replace("_", "-")


def _title(package: str) -> str:
    return " ".join(part.capitalize() for part in package.split("_") if part)


def template_values(template: str, package: str, **extra: str) -> dict[str, str]:
    values = {
        "package": package,
        "project_name": _project_name(package),
        "slug": _project_name(package),
        "title": _title(package),
        "template": template,
        "description": TEMPLATES[template].summary if template in TEMPLATES else "",
        "nexura_version": __version__,
    }
    values.update(extra)
    return values


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def write_files(
    root: Path, files: Mapping[str, str], *, skip_existing: bool
) -> tuple[list[Path], list[Path]]:
    """Create each file exclusively; an existing file is never replaced.

    With ``skip_existing`` an existing file is reported as skipped; otherwise it is an error
    (checked for every file before anything is written).
    """
    existing = [root / rel for rel in files if os.path.lexists(root / rel)]
    if existing and not skip_existing:
        listed = ", ".join(str(p.relative_to(root)) for p in existing)
        raise ScaffoldError(f"refusing to overwrite existing files: {listed}")
    outside = [rel for rel in files if not _inside((root / rel).parent, root)]
    if outside:
        raise ScaffoldError(
            f"refusing to write through a link that leaves the project: {', '.join(outside)}"
        )
    written: list[Path] = []
    skipped: list[Path] = []
    for rel, text in files.items():
        target = root / rel
        if os.path.lexists(target):
            skipped.append(target)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            # "x": create only. Fails on an existing file or link, so nothing is overwritten.
            with open(target, "x", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
        except FileExistsError:
            skipped.append(target)
            continue
        written.append(target)
    return written, skipped


def scaffold_project(
    directory: Path,
    template: str = DEFAULT_TEMPLATE,
    *,
    package: str | None = None,
    force: bool = False,
    env_example: bool = True,
) -> ScaffoldResult:
    """Write a runnable project from ``template`` into ``directory``.

    ``directory`` must be missing or empty unless ``force``; even with ``force`` an existing
    file is kept and listed in ``ScaffoldResult.skipped``, never overwritten.
    """
    if template not in TEMPLATES:
        raise ScaffoldError(
            f"unknown template '{template}'; available: {', '.join(sorted(TEMPLATES))}"
        )
    if directory.exists() and not directory.is_dir():
        raise ScaffoldError(f"{directory} exists and is not a directory")
    if directory.is_dir() and any(directory.iterdir()) and not force:
        raise ScaffoldError(
            f"{directory} is not empty; choose a new directory, or pass --force to add the "
            "template's files next to what is there (existing files are never overwritten)"
        )
    pkg = package_name_for(directory.resolve(), package)
    values = template_values(template, pkg)
    rendered: dict[str, str] = {}
    for rel, node in template_files(["common", template], pkg).items():
        if rel == ".env.example" and not env_example:
            continue
        rendered[rel] = render_text(node.read_text(encoding="utf-8"), values)
    directory.mkdir(parents=True, exist_ok=True)
    written, skipped = write_files(directory, rendered, skip_existing=True)
    return ScaffoldResult(
        root=directory, template=template, package=pkg, written=written, skipped=skipped
    )
