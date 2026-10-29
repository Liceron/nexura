# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Artifact and recipe checks for the final packaging audit (no Docker or network)."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import subprocess
import tarfile
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def workspace() -> dict[str, Path]:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    paths = [ROOT, *(ROOT / p for p in project["tool"]["uv"]["workspace"]["members"])]
    return {tomllib.loads((p / "pyproject.toml").read_text())["project"]["name"]: p for p in paths}


def check_recipe(text: str) -> None:
    packages = workspace()
    lines = text.replace("\\\n", " ").splitlines()
    commands = [shlex.split(line[4:]) for line in lines if line.startswith("RUN uv ")]
    builds = {c[c.index("--package") + 1] for c in commands if c[:2] == ["uv", "build"]}
    install = next(c for c in commands if c[:3] == ["uv", "pip", "install"])
    installed = {}
    for token in install:
        if ".whl" not in token:
            continue
        match = re.fullmatch(r"/build/dist/(.+)-([^-]+)-py3-none-any\.whl(\[.*\])?", token)
        assert match, token
        name, version, extras = match.groups()
        name = name.replace("_", "-")
        assert name in packages, f"non-workspace wheel: {token}"
        metadata = tomllib.loads((packages[name] / "pyproject.toml").read_text())["project"]
        assert version == metadata["version"], f"stale Docker wheel: {token}"
        assert set((extras or "[]")[1:-1].split(",")) - {""} <= set(
            metadata.get("optional-dependencies", {})
        )
        installed[name] = extras
    assert builds == set(installed)
    assert {"nexura-runtime", "nexura-studio", "nexura-agents"} <= set(installed)
    assert installed["nexura-runtime"] == "[studio]"
    assert builds <= packages.keys()
    assert "COPY packages/ ./packages/" in lines
    assert "COPY src/ ./src/" in lines
    for required in ("pyproject.toml", "uv.lock", "README.md", "LICENSE", "THIRD_PARTY.md"):
        assert any(
            required in shlex.split(line)[1:-1]
            for line in lines
            if line.startswith("COPY ") and "--from=" not in line
        )
    exports = next(c for c in commands if c[:2] == ["uv", "export"])
    assert {exports[i + 1] for i, arg in enumerate(exports) if arg == "--package"} == builds
    assert {"--no-emit-workspace", "--frozen", "--no-dev", "--extra"} <= set(exports)
    assert exports[exports.index("--extra") + 1] == "studio"
    assert "COPY --from=builder /opt/venv /opt/venv" in lines


def test_docker_installs_workspace_packages() -> None:
    recipes = list((ROOT / "deployment").rglob("*Dockerfile*"))
    assert recipes
    for recipe in recipes:
        check_recipe(recipe.read_text())


def test_docker_check_rejects_missing_split_package() -> None:
    recipe = (ROOT / "deployment/Dockerfile").read_text()
    broken = recipe.replace("/build/dist/nexura_studio-0.4.1-py3-none-any.whl", "")
    with pytest.raises(AssertionError):
        check_recipe(broken)


def assert_public_members(members: list[str]) -> None:
    for member in members:
        parts = Path(member).parts[1:]  # Ignore the distribution-version archive prefix.
        assert not any(
            "blueprint" in p or p in {"internal", "product", ".autopilot"} for p in parts
        ), member
        if parts:
            assert parts[0] in {
                "src",
                "tests",
                "docs",
                "README.md",
                "LICENSE",
                "THIRD_PARTY.md",
                "pyproject.toml",
                "PKG-INFO",
                ".gitignore",  # Hatch's standard VCS metadata.
            }, member


def test_all_published_sdists_exclude_internal_material(tmp_path: Path) -> None:
    subprocess.run(
        ["uv", "build", "--all-packages", "--sdist", "--out-dir", str(tmp_path)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    sdists = sorted(tmp_path.glob("*.tar.gz"))
    assert len(sdists) == len(workspace()) == 9
    for sdist in sdists:
        with tarfile.open(sdist) as archive:
            members = archive.getnames()
        assert_public_members(members)
        for required in ("README.md", "LICENSE", "pyproject.toml", "PKG-INFO"):
            assert any(n.endswith("/" + required) for n in members), (sdist, required)


@pytest.mark.parametrize(
    "path",
    [
        "backend-blueprint/catalog.json",
        "internal/plan.md",
        "product/brief.md",
        ".autopilot/log.txt",
        "src/nexura_runtime/internal/plan.md",
    ],
)
def test_sdist_scan_rejects_internal_material(path: str) -> None:
    with pytest.raises(AssertionError):
        assert_public_members([f"distribution-0.4.0/{path}"])


def test_sbom_matches_locked_release_dependencies() -> None:
    sbom = json.loads((ROOT / "packaging/sbom.json").read_text())
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    locked = {(p["name"], p["version"]) for p in lock["package"]}
    # Independent oracle: uv's own resolution/export of the published profiles.
    output = subprocess.run(
        [
            "uv",
            "export",
            "--frozen",
            "--all-packages",
            "--all-extras",
            "--no-dev",
            "--no-emit-workspace",
            "--no-hashes",
            "--no-header",
            "--no-annotate",
        ],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
    ).stdout
    expected = set(re.findall(r"^([a-z0-9-]+)==([^;\s]+)", output, re.MULTILINE))
    expected |= {(p["name"], p["version"]) for p in lock["package"] if p["name"] in workspace()}
    actual = {(p["name"], p["version"]) for p in sbom["components"]}
    assert actual == expected
    assert actual <= locked
    assert len(actual) == len(sbom["components"])
    properties = {p["name"]: p["value"] for p in sbom["metadata"]["properties"]}
    assert (
        properties["nexura:lock-sha256"]
        == hashlib.sha256((ROOT / "uv.lock").read_bytes()).hexdigest()
    )
    refs = {c["bom-ref"] for c in sbom["components"]}
    assert {edge["ref"] for edge in sbom["dependencies"]} == refs
    assert all(set(edge["dependsOn"]) <= refs for edge in sbom["dependencies"])
    subprocess.run(
        ["python", "scripts/generate_sbom.py", "--check"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        timeout=30,
    )
