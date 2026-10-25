# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""P39 G3: ``nexura-guardrails`` is its own workspace package that depends on nexura-agents only."""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PKG = ROOT / "packages" / "nexura-guardrails"
SRC = PKG / "src" / "nexura_guardrails"


def _project(path: Path) -> dict[str, object]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))["project"]
    assert isinstance(data, dict)
    return data


def test_metadata_matches_the_other_agent_only_packages() -> None:
    project = _project(PKG / "pyproject.toml")
    agents = _project(ROOT / "packages/nexura-agents/pyproject.toml")
    assert project["name"] == "nexura-guardrails"
    assert project["version"] == agents["version"]
    assert project["requires-python"] == ">=3.11,<3.14" == agents["requires-python"]
    assert project["license"] == "MIT"
    assert project["dependencies"] == [f"nexura-agents=={agents['version']}"]
    extras = project["optional-dependencies"]
    assert isinstance(extras, dict)
    assert extras["judge"] == []
    assert extras["yaml"] == ["pyyaml>=6"]
    # P39 G5 (review minor 9): the local safety-model extra declares httpx, with exactly the
    # range nexura-agents already requires, so it adds no new package to an install.
    agents_httpx = [d for d in agents["dependencies"] if str(d).startswith("httpx")]  # type: ignore[attr-defined]
    assert extras["ollama"] == agents_httpx and len(agents_httpx) == 1
    assert set(extras) == {"judge", "ollama", "yaml"}
    for name in ("LICENSE", "README.md", "src/nexura_guardrails/py.typed"):
        assert (PKG / name).is_file(), name


def test_registered_in_the_workspace() -> None:
    root = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert "packages/nexura-guardrails" in root["tool"]["uv"]["workspace"]["members"]
    assert root["tool"]["uv"]["sources"]["nexura-guardrails"] == {"workspace": True}


def test_imports_only_the_agent_sdk_and_the_stdlib() -> None:
    allowed_roots = {"nexura_agents", "nexura_guardrails"} | set(sys.stdlib_module_names)
    # P39 G5: httpx is allowed only in the HTTP layer of the safety-model adapters, and only
    # because nexura-agents itself depends on it (so it adds no dependency to this package).
    agents_deps = _project(ROOT / "packages/nexura-agents/pyproject.toml")["dependencies"]
    assert isinstance(agents_deps, list)
    assert any(str(d).startswith("httpx") for d in agents_deps)
    httpx_allowed = {SRC / "classifiers" / "_base.py"}
    # P39 G6: PyYAML (the [yaml] extra) only in the policy loader, imported inside the YAML
    # function so JSON policies and every other module work without it.
    yaml_allowed = {SRC / "policy" / "loader.py"}
    offenders = []
    for path in SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert text.startswith("# SPDX-License-Identifier: MIT\n# Copyright (c) 2026 Liceron\n")
        roots = allowed_roots | ({"httpx"} if path in httpx_allowed else set())
        roots |= {"yaml"} if path in yaml_allowed else set()
        for node in ast.walk(ast.parse(text)):
            names: list[str] = []
            if isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [node.module or ""]
            elif isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            offenders += [f"{path.name}: {n}" for n in names if n.split(".")[0] not in roots]
    assert offenders == []
    loader = (SRC / "policy" / "loader.py").read_text(encoding="utf-8")
    top_level = [
        node
        for node in ast.parse(loader).body
        if isinstance(node, (ast.Import, ast.ImportFrom))
        and any((getattr(node, "module", None) or a.name).startswith("yaml") for a in node.names)
    ]
    assert top_level == []  # yaml is imported lazily, inside policy_from_yaml


def test_import_is_lazy_and_every_export_resolves() -> None:
    probe = (
        "import json, sys, nexura_guardrails as g\n"
        "loaded = sorted(m for m in sys.modules if m.startswith('nexura_guardrails.'))\n"
        "missing = [n for n in g.__all__ if getattr(g, n, None) is None]\n"
        "print(json.dumps({'loaded': loaded, 'missing': missing, 'version': g.__version__}))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, timeout=120, check=True
    )
    data = json.loads(out.stdout)
    assert data["loaded"] == [] and data["missing"] == [] and data["version"] == "0.4.0"
