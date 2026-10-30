# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Project scaffolding and the local workflow: ``nexura-runtime init``, ``dev`` and ``deploy``.

See docs/project-templates.md.
"""

from __future__ import annotations

from nexura_runtime.project.config import (
    ProjectConfig,
    ProjectError,
    find_project,
    project_registry,
    project_workflows,
)
from nexura_runtime.project.deploy import (
    SUPPORTED_TARGETS,
    DeployError,
    DeployResult,
    write_deploy_files,
)
from nexura_runtime.project.scaffold import (
    DEFAULT_TEMPLATE,
    TEMPLATES,
    ProjectTemplate,
    ScaffoldError,
    ScaffoldResult,
    scaffold_project,
)

__all__ = [
    "DEFAULT_TEMPLATE",
    "SUPPORTED_TARGETS",
    "TEMPLATES",
    "DeployError",
    "DeployResult",
    "ProjectConfig",
    "ProjectError",
    "ProjectTemplate",
    "ScaffoldError",
    "ScaffoldResult",
    "find_project",
    "project_registry",
    "project_workflows",
    "scaffold_project",
    "write_deploy_files",
]
