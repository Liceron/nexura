"""Run each agents example in a fresh interpreter and check a key output line."""

import subprocess
import sys
from pathlib import Path

import pytest

EXAMPLES = Path(__file__).resolve().parents[2] / "examples" / "agents"

CASES = [
    ("support_triage.py", "REFUND APPROVED AND EXECUTED: ('A1', 40)"),
    ("research_team.py", "RESEARCH TEAM DONE"),
    ("a2a_interop.py", "concierge says: We have 12 widgets available."),
    ("tools_mcp_openapi.py", "MCP AND OPENAPI OK"),
    ("memory_per_user.py", "MEMORY ISOLATION OK"),
    ("structured_and_guardrails.py", "tripwire: redact_output_pii output"),
    ("prompts_and_evals.py", "winner: candidate"),
]


@pytest.mark.parametrize(("name", "expected"), CASES)
def test_example_runs(name: str, expected: str, tmp_path: Path) -> None:
    proc = subprocess.run(
        [sys.executable, str(EXAMPLES / name)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert expected in proc.stdout


def test_readme_lists_every_example() -> None:
    readme = (EXAMPLES / "README.md").read_text()
    for name, _ in CASES:
        assert name in readme
