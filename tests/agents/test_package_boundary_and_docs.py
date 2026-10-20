"""The standalone ``nexura-agents`` package: boundary, public API, model strings, README."""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PKG = ROOT / "packages" / "nexura-agents"
SRC = PKG / "src" / "nexura_agents"
README = (PKG / "README.md").read_text(encoding="utf-8")
# Blocks fenced as ```python notest need live credentials and are not executed here.
BLOCKS = re.findall(r"```python\n(.*?)```", README, flags=re.DOTALL)


def test_agents_package_never_imports_the_platform() -> None:
    offenders = []
    for path in SRC.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = (
                [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else []
            )
            offenders += [f"{path.relative_to(SRC)}: {n}" for n in names if "nexura_runtime" in n]
    assert not offenders, offenders


def test_core_import_is_light_and_loads_no_platform_code() -> None:
    probe = (
        "import sys, nexura_agents; from nexura_agents import Agent, Runner, tool; "
        "bad = [m for m in sys.modules if m.startswith(('nexura_runtime', 'fastapi', "
        "'google.auth', 'sqlalchemy'))]; assert not bad, bad"
    )
    subprocess.run([sys.executable, "-c", probe], check=True)


def test_every_public_name_resolves() -> None:
    import nexura_agents

    for name in nexura_agents.__all__:
        assert getattr(nexura_agents, name) is not None, name


def test_platform_paths_are_the_same_objects() -> None:
    # A provider subpackage alias (Vertex AI, used here before, is now nexura-vertex).
    import nexura_agents.providers.gemini as new_gemini
    import nexura_agents.runner as new_runner
    import nexura_runtime.providers.gemini as old_gemini
    import nexura_runtime.runner as old_runner
    from nexura_agents import Agent

    from nexura_runtime import Agent as OldAgent

    assert old_runner is new_runner and old_gemini is new_gemini and OldAgent is Agent


def test_model_strings_build_providers_and_reject_unknown() -> None:
    from nexura_agents import Agent, FakeModel, OpenAIProvider, infer_model
    from nexura_agents.errors import SDKValidationError

    ollama = infer_model("ollama:llama3.1")
    assert isinstance(ollama, OpenAIProvider)
    assert ollama.base_url == "http://localhost:11434/v1"
    assert isinstance(Agent("T", "", "test").model, FakeModel)
    for bad in ("gpt-4o", "nope:model", "openai:"):
        with pytest.raises(SDKValidationError, match="provider:model"):
            infer_model(bad)


def test_api_keys_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    from nexura_agents import GeminiProvider, OpenAIProvider

    monkeypatch.setenv("OPENAI_API_KEY", "sk-env-openai")
    monkeypatch.setenv("GEMINI_API_KEY", "env-gemini")
    assert OpenAIProvider("gpt-4.1-mini")._build_headers()["Authorization"] == (
        "Bearer sk-env-openai"
    )
    assert GeminiProvider()._build_headers()["x-goog-api-key"] == "env-gemini"
    assert "env-gemini" not in repr(GeminiProvider())


def test_readme_has_runnable_examples() -> None:
    assert len(BLOCKS) >= 3
    assert "```python notest" in README  # the real-model example is shown first


@pytest.mark.parametrize("index", range(len(BLOCKS)))
def test_readme_example_runs_offline(index: int, tmp_path: Path) -> None:
    script = tmp_path / f"example_{index}.py"
    script.write_text(BLOCKS[index], encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(script)], cwd=tmp_path, capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, proc.stderr


def test_refund_example_prints_documented_output(tmp_path: Path) -> None:
    block = next(b for b in BLOCKS if "RunInterruption.from_json" in b)
    script = tmp_path / "refund.py"
    script.write_text(block, encoding="utf-8")
    out = subprocess.run(
        [sys.executable, str(script)], capture_output=True, text=True, check=True
    ).stdout.splitlines()
    assert out == ["order_id='A1' refunded=250.0", "['A1:250.0']"]
