# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""``nexura-runtime deploy``: write deployment files for a project. Nothing is built or run."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from nexura_runtime.project.config import ProjectError, find_project
from nexura_runtime.project.scaffold import (
    render_text,
    template_files,
    template_values,
    write_files,
)

SUPPORTED_TARGETS = {
    "docker": "Dockerfile, .dockerignore and DEPLOY.md for a single-user container "
    "(Studio on a loopback-published port, token required).",
}

UNSUPPORTED_NOTE = (
    "Kubernetes/Helm, serverless and hosted targets are not part of the open-source edition, "
    "which is single user on one machine. For a team or a public endpoint use the hosted or "
    "enterprise edition."
)


class DeployError(ValueError):
    """Deployment files cannot be written as asked."""


@dataclass
class DeployResult:
    """The files ``write_deploy_files`` wrote, and those it replaced (``force=True``)."""

    target: str
    written: list[Path] = field(default_factory=list)
    replaced: list[Path] = field(default_factory=list)


def supported_targets_text() -> str:
    lines = ["Supported deploy targets:"]
    lines += [f"  {name:<8} {summary}" for name, summary in SUPPORTED_TARGETS.items()]
    lines.append(UNSUPPORTED_NOTE)
    return "\n".join(lines)


def write_deploy_files(
    project_dir: Path, target: str, *, port: int = 8000, force: bool = False
) -> DeployResult:
    """Write the target's files into ``project_dir``.

    Existing files are never overwritten silently: without ``force`` the call is refused and
    nothing is written; with ``force`` the listed files are replaced.
    """
    if target not in SUPPORTED_TARGETS:
        raise DeployError(f"unsupported target '{target}'.\n{supported_targets_text()}")
    if not 1 <= port <= 65535:
        raise DeployError("--port must be between 1 and 65535")
    try:
        project = find_project(project_dir)
    except ProjectError as exc:
        raise DeployError(str(exc)) from exc
    if project is None:
        raise DeployError(
            f"{project_dir} is not a Nexura project (no [tool.nexura] table in pyproject.toml); "
            "create one with 'nexura-runtime init'"
        )
    package = project.name.replace("-", "_").lower()  # docker files name no package dir
    values = template_values(
        project.template or "", package, project_name=_image_name(project.name), port=str(port)
    )
    rendered = {
        rel: render_text(node.read_text(encoding="utf-8"), values)
        for rel, node in template_files([target], package).items()
    }
    existing = [rel for rel in rendered if os.path.lexists(project_dir / rel)]
    if existing and not force:
        raise DeployError(
            f"refusing to overwrite {', '.join(existing)}; pass --force to replace them"
        )
    replaced = [project_dir / rel for rel in existing]
    for path in replaced:  # only reached with force; the caller prints each replaced file
        path.unlink()
    written, _ = write_files(project_dir, rendered, skip_existing=False)
    return DeployResult(target=target, written=written, replaced=replaced)


def _image_name(name: str) -> str:
    cleaned = "".join(c if c.isalnum() or c in "-_." else "-" for c in name.lower()).strip("-._")
    return cleaned or "nexura-app"
