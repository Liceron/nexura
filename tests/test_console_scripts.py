# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Console scripts (B42): the CLI is ``nexura-runtime``; the bare ``nexura`` command belongs
to the separately published Nexura Cloud client package, so this project must not install it."""

from __future__ import annotations

import importlib.metadata
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _installed_console_scripts(distribution: str = "nexura-runtime") -> dict[str, str]:
    dist = importlib.metadata.distribution(distribution)
    return {ep.name: ep.value for ep in dist.entry_points if ep.group == "console_scripts"}


def test_installed_console_scripts_do_not_include_bare_nexura() -> None:
    scripts = _installed_console_scripts()
    assert "nexura" not in scripts, scripts
    assert scripts.get("nexura-runtime") == "nexura_runtime.cli:main"
    # The Studio launcher moved to the nexura-studio package (P28 step 10 stage 4b).
    assert "nexura-studio" not in scripts, scripts
    studio = _installed_console_scripts("nexura-studio")
    assert studio == {"nexura-studio": "nexura_studio.cli:main"}


def test_pyproject_declares_the_same_scripts() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["scripts"] == {"nexura-runtime": "nexura_runtime.cli:main"}
    studio = tomllib.loads(
        (ROOT / "packages" / "nexura-studio" / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]
    assert studio["scripts"] == {"nexura-studio": "nexura_studio.cli:main"}


def test_no_other_installed_distribution_of_this_project_claims_nexura() -> None:
    for name in (
        "nexura-runtime",
        "nexura-agents",
        "nexura-connectors",
        "nexura-guardrails",
        "nexura-postgres",
        "nexura-qdrant",
        "nexura-sandbox",
        "nexura-studio",
        "nexura-vertex",
    ):
        try:
            dist = importlib.metadata.distribution(name)
        except importlib.metadata.PackageNotFoundError:
            continue
        names = {ep.name for ep in dist.entry_points if ep.group == "console_scripts"}
        assert "nexura" not in names, (name, names)


def _runtime_script() -> str:
    bin_dir = Path(sys.executable).parent
    candidate = bin_dir / "nexura-runtime"
    if candidate.exists():
        return str(candidate)
    found = shutil.which("nexura-runtime")
    assert found, "nexura-runtime is not installed; run uv sync"
    return found


def test_nexura_runtime_help_works() -> None:
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    proc = subprocess.run(
        [_runtime_script(), "--help"],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("usage: nexura-runtime "), proc.stdout
    for sub in ("workflow", "serve", "worker", "redteam", "evals"):
        assert sub in proc.stdout


def test_cli_prog_name_and_error_messages_use_nexura_runtime() -> None:
    from nexura_runtime.cli.main import build_parser

    parser = build_parser()
    assert parser.prog == "nexura-runtime"
    assert parser.format_usage().startswith("usage: nexura-runtime ")
    source = (ROOT / "src" / "nexura_runtime" / "cli" / "main.py").read_text(encoding="utf-8")
    assert "'nexura serve'" not in source and "'nexura worker'" not in source
