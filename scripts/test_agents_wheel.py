"""Test the built ``nexura-agents`` wheel on its own, as an outside user would install it.

    uv run python scripts/test_agents_wheel.py --python 3.11

Builds the wheel, installs it (with the ``otel`` and ``signing`` extras, the OpenTelemetry
SDK used by the tracing tests and mypy for the static typing tests) into a fresh virtual
environment for the given Python, copies the agent test suite to a temporary directory and
runs it there.
Test files that need the platform package (``nexura_runtime``) are skipped, since the
wheel does not contain it.
Exit status is pytest's. Used by CI for Python 3.11, 3.12 and 3.13.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests" / "agents"
OTEL_SDK = "opentelemetry-sdk==1.45.0"  # matches the version locked in uv.lock
MYPY = "mypy==1.20.2"  # matches uv.lock; the typed-result tests run mypy on snippets


def _is_alias(dotted: str) -> bool:
    """Whether ``nexura_runtime<dotted>`` is an alias of an agents module."""
    if not dotted:
        return True  # the bare package name, used for names it re-exports from agents
    sys.path.insert(0, str(ROOT / "src"))
    try:
        from nexura_runtime import _MOVED_TO_AGENTS
    finally:
        sys.path.pop(0)
    first = dotted.lstrip(".").split(".")[0]
    return first in _MOVED_TO_AGENTS or dotted.lstrip(".") in _MOVED_TO_AGENTS


def run(cmd: list[str], **kwargs: object) -> None:
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, **kwargs)  # type: ignore[call-overload]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--python", default="3.12", help="Python version for the venv")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="nexura-agents-wheel-") as tmp:
        work = Path(tmp)
        run(["uv", "build", "--package", "nexura-agents", "--wheel", "-o", str(work / "dist")])
        (wheel,) = (work / "dist").glob("nexura_agents-*.whl")
        venv = work / "venv"
        run(["uv", "venv", "--python", args.python, str(venv)])
        # Windows venvs keep the interpreter in Scripts\, everything else in bin/.
        py = str(venv / "Scripts" / "python.exe" if os.name == "nt" else venv / "bin" / "python")
        run(
            [
                "uv",
                "pip",
                "install",
                "--python",
                py,
                f"{wheel}[otel,signing]",
                OTEL_SDK,
                MYPY,
                "pytest<9",
                "pytest-asyncio<1",
            ]
        )

        suite = work / "tests"
        suite.mkdir()
        skipped = []
        for path in sorted(TESTS.glob("*.py")):
            text = path.read_text(encoding="utf-8")
            platform_modules = set(re.findall(r"nexura_runtime((?:\.\w+)*)", text))
            # Old ``nexura_runtime.<name>`` imports of agent modules are aliases of
            # ``nexura_agents``; rewrite those. Anything else needs the platform, which is
            # not in the wheel, and files that read the repository cannot run outside it.
            if "parents[2]" in text or not all(_is_alias(m) for m in platform_modules):
                skipped.append(path.name)
                continue
            (suite / path.name).write_text(
                text.replace("nexura_runtime", "nexura_agents"), encoding="utf-8"
            )
        (suite / "pytest.ini").write_text("[pytest]\nasyncio_mode = auto\n", encoding="utf-8")
        print(f"Skipping {len(skipped)} platform- or repo-dependent files: {', '.join(skipped)}")
        result = subprocess.run([py, "-m", "pytest", "-q", "-p", "no:cacheprovider"], cwd=suite)
        return result.returncode


if __name__ == "__main__":
    sys.exit(main())
