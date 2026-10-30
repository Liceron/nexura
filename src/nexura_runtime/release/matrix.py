# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Packaged OSS backend catalog; historical verification is not release approval."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from importlib.resources import files
from pathlib import Path
from typing import Literal

VerificationLevel = Literal["verified", "mock_verified", "pending_integration", "planned"]
DependencyProfile = Literal["core", "studio", "knowledge", "auth", "dev"]


@dataclass(frozen=True)
class ModuleEntry:
    """A backend work package and its recorded verification evidence."""

    code: str
    name: str
    description: str
    status: VerificationLevel
    profile: DependencyProfile
    test_path: str
    report_path: str
    dependencies: list[str]
    capabilities: list[str]
    isolation_guarantees: list[str]
    verification_note: str = ""


MODULE_REGISTRY: list[ModuleEntry] = [
    ModuleEntry(**entry)
    for entry in json.loads(files(__package__).joinpath("capabilities.json").read_text())
]


def get_matrix() -> list[dict[str, object]]:
    """Return independent records; a count is not proof of operational readiness."""
    return [asdict(entry) for entry in MODULE_REGISTRY]


def get_module(code: str) -> ModuleEntry | None:
    return next((entry for entry in MODULE_REGISTRY if entry.code == code), None)


def validate_matrix(source_root: str | Path) -> list[str]:
    """Check the public catalog's identity, dependencies and source evidence paths."""
    root = Path(source_root)
    expected = {f"B{i:02d}" for i in range(33)}
    problems: list[str] = []
    seen: set[str] = set()
    for entry in MODULE_REGISTRY:
        if entry.code in seen:
            problems.append(f"Duplicate ID: {entry.code}")
        seen.add(entry.code)
        if entry.code not in expected:
            problems.append(f"Unknown ID: {entry.code}")
        for dependency in entry.dependencies:
            if dependency not in expected or dependency == entry.code:
                problems.append(f"Invalid dependency: {entry.code}: {dependency}")
        for path in (entry.test_path, entry.report_path):
            if not (root / path).exists():
                problems.append(f"Missing evidence: {entry.code}: {path}")
    for code in sorted(expected - seen):
        problems.append(f"Missing ID: {code}")
    return problems
