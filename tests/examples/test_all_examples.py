"""Run every offline example in a fresh interpreter from a temporary directory.

``examples/agents`` has its own test with expected output lines (test_agent_examples.py);
``examples/live`` makes billed provider calls on purpose and is never run here. Each run gets
no provider credentials and an unreachable proxy for outbound HTTP, so an example that tries a
real provider fails instead of spending money.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "examples"
SKIP_DIRS = {"agents", "live"}
TIMEOUT_SECONDS = 180

_CREDENTIAL_PREFIXES = ("OPENAI", "ANTHROPIC", "GEMINI", "GOOGLE", "VERTEX", "AZURE", "LITELLM")


def _offline_examples() -> list[Path]:
    found = []
    for path in sorted(EXAMPLES.rglob("*.py")):
        rel = path.relative_to(EXAMPLES)
        if rel.parts[0] in SKIP_DIRS or "__pycache__" in rel.parts:
            continue
        found.append(path)
    return found


def _offline_env() -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(_CREDENTIAL_PREFIXES) and not k.endswith(("_API_KEY", "_TOKEN"))
    }
    # Local mock servers stay reachable; anything else goes to a closed port.
    dead = "http://127.0.0.1:9"
    env.update(
        HTTP_PROXY=dead,
        HTTPS_PROXY=dead,
        http_proxy=dead,
        https_proxy=dead,
        NO_PROXY="localhost,127.0.0.1,::1",
        no_proxy="localhost,127.0.0.1,::1",
    )
    return env


EXAMPLE_FILES = _offline_examples()


def test_examples_are_found() -> None:
    dirs = {p.relative_to(EXAMPLES).parts[0] for p in EXAMPLE_FILES}
    assert {"backend", "oss"} <= dirs
    assert not any(p.relative_to(EXAMPLES).parts[0] == "live" for p in EXAMPLE_FILES)


@pytest.mark.parametrize(
    "path", EXAMPLE_FILES, ids=[str(p.relative_to(EXAMPLES)) for p in EXAMPLE_FILES]
)
def test_example_runs_offline(path: Path, tmp_path: Path) -> None:
    proc = subprocess.run(
        [sys.executable, str(path)],
        cwd=tmp_path,
        env=_offline_env(),
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
    )
    assert proc.returncode == 0, (proc.stdout[-2000:], proc.stderr[-4000:])
