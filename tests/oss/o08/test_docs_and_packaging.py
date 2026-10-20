"""O08: documented commands, examples and support boundaries must match reality.

These tests execute the README's code and commands instead of checking its wording.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
README = (ROOT / "README.md").read_text(encoding="utf-8")
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

# Blocks fenced as ```python notest need live credentials and are not executed here.
PYTHON_BLOCKS = re.findall(r"```python\n(.*?)```", README, flags=re.DOTALL)


def test_readme_has_quickstart_and_tutorial_blocks() -> None:
    assert len(PYTHON_BLOCKS) >= 2


@pytest.mark.parametrize("index", range(len(PYTHON_BLOCKS)))
def test_readme_python_block_runs_offline(index: int, tmp_path: Path) -> None:
    script = tmp_path / f"readme_block_{index}.py"
    script.write_text(PYTHON_BLOCKS[index], encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(script)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, f"README block {index} failed:\n{proc.stderr}"


def test_tutorial_block_pauses_then_completes(tmp_path: Path) -> None:
    tutorial = next(b for b in PYTHON_BLOCKS if "requires_approval=True" in b)
    script = tmp_path / "tutorial.py"
    script.write_text(tutorial, encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(script)], cwd=tmp_path, capture_output=True, text=True, check=True
    )
    assert proc.stdout.splitlines() == ["paused", "completed REF-9901"]


def test_every_referenced_repository_path_exists() -> None:
    referenced = set(
        re.findall(r"`((?:examples|src|clients|studio-web|experiments)/[^`\s]*)`", README)
    )
    referenced |= set(re.findall(r"\]\(((?!https?:)[^)#]+)\)", README))
    assert referenced
    missing = sorted(p for p in referenced if not (ROOT / p.rstrip("/")).exists())
    assert not missing, f"README references missing paths: {missing}"


def test_documented_extras_match_pyproject() -> None:
    declared = set(PYPROJECT["project"]["optional-dependencies"])
    table = README.split("Optional extras:")[1].split("\n\n", 2)[1]
    documented = set(re.findall(r"^\| `([a-z][a-z0-9-]*)` \|", table, flags=re.MULTILINE))
    assert documented == declared


def test_documented_python_support_matches_metadata() -> None:
    assert PYPROJECT["project"]["requires-python"] == ">=3.12,<3.13"
    assert "Python | 3.12 only (`>=3.12,<3.13`)" in README
    assert sys.version_info[:2] == (3, 12)


@pytest.mark.parametrize(
    "argv",
    [
        ["serve", "--help"],
        ["workflow", "export", "--help"],
        ["workflow", "run", "--help"],
    ],
)
def test_documented_cli_commands_exist(argv: list[str]) -> None:
    from nexura_runtime.cli.main import main

    with pytest.raises(SystemExit) as exc:
        main(argv)
    assert exc.value.code == 0


def test_serve_defaults_match_studio_web_proxy() -> None:
    from nexura_runtime.cli.main import build_parser

    args = build_parser().parse_args(["serve"])
    assert (args.host, args.port) == ("127.0.0.1", 8765)
    vite_config = (ROOT / "studio-web" / "vite.config.ts").read_text(encoding="utf-8")
    assert "http://127.0.0.1:8765" in vite_config


def test_core_import_does_not_load_optional_extras() -> None:
    probe = (
        "import sys, nexura_runtime; "
        "loaded = [m for m in ('fastapi', 'uvicorn', 'pypdf', 'asyncpg', 'qdrant_client', "
        "'authlib') if m in sys.modules]; "
        "assert not loaded, loaded"
    )
    subprocess.run([sys.executable, "-c", probe], check=True)


def test_license_and_security_contact_match_project_metadata() -> None:
    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert license_text.startswith("MIT License")
    author = PYPROJECT["project"]["authors"][0]
    assert f"Copyright (c) 2026 {author['name']}" in license_text
    assert PYPROJECT["project"]["license"] == "MIT"
    security = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
    assert author["email"] in security
    ts_package = (ROOT / "clients" / "typescript" / "package.json").read_text(encoding="utf-8")
    assert author["name"] in ts_package and '"license": "MIT"' in ts_package


def test_ci_never_publishes_or_needs_paid_keys() -> None:
    workflows = list((ROOT / ".github" / "workflows").glob("*.yml"))
    assert workflows
    for wf in workflows:
        text = wf.read_text(encoding="utf-8")
        for forbidden in ("npm publish", "twine upload", "uv publish", "secrets.", "API_KEY"):
            assert forbidden not in text, f"{wf.name} contains {forbidden!r}"


def test_built_wheel_contains_docs_and_excludes_local_state(tmp_path: Path) -> None:
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(tmp_path)],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    wheel = next(tmp_path.glob("nexura_runtime-*.whl"))
    with zipfile.ZipFile(wheel) as zf:
        names = zf.namelist()
        metadata = zf.read(next(n for n in names if n.endswith("METADATA"))).decode()
    assert "License-Expression: MIT" in metadata
    assert any(n.endswith("licenses/LICENSE") for n in names)
    assert "Requires-Python: <3.13,>=3.12" in metadata
    assert "nexura_runtime/py.typed" in names
    leaked = [n for n in names if n.endswith((".sqlite3", ".db", ".env", ".pem"))]
    assert not leaked, leaked
