# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Diagnostics, environment hygiene audit, and performance benchmarks for release gate."""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _own_tests_dir() -> Path | None:
    """The ``tests`` directory of the source checkout this package runs from, else None.

    Pytest puts the checkout's own test directories (such as ``tests/integrations/langflow``)
    on ``sys.path``; those are this project's files, whatever the checkout directory is
    called. An installed wheel has no ``src/nexura_runtime`` beside it, so nothing is exempt.
    """
    root = Path(__file__).resolve().parents[3]
    if (root / "pyproject.toml").is_file() and (root / "src" / "nexura_runtime").is_dir():
        return root / "tests"
    return None


def _inside(path: str, root: Path | None) -> bool:
    if root is None or not path:
        return False
    try:
        return Path(path).resolve().is_relative_to(root)
    except (OSError, ValueError):
        return False


@dataclass(frozen=True)
class BenchmarkMetrics:
    """Performance measurements on declared interpreter and host."""

    cold_import_ms: float
    resident_memory_mb: float
    interpreter: str
    platform: str


@dataclass(frozen=True)
class HygieneAuditResult:
    """Security and environment purity audit results."""

    passed: bool
    foreign_paths_found: list[str]
    unauthorized_modules_found: list[str]
    details: dict[str, Any]


class ReleaseDiagnostics:
    """Verifies performance thresholds, dependency isolation, and packaging hygiene."""

    COLD_IMPORT_TARGET_MS = 500.0  # Engineering target < 500ms
    MEMORY_TARGET_MB = 128.0  # Target < 128MB RSS

    @staticmethod
    def measure_cold_import() -> BenchmarkMetrics:
        """Measure cold import latency and RSS in an isolated child process."""
        probe_code = (
            "import time, resource, sys\n"
            "t0 = time.perf_counter()\n"
            "import nexura_runtime\n"
            "dur = (time.perf_counter() - t0) * 1000.0\n"
            "if sys.platform == 'linux':\n"
            "    with open('/proc/self/status') as status:\n"
            "        line = next(line for line in status if line.startswith('VmRSS:'))\n"
            "        rss = float(line.split()[1]) / 1024.0\n"
            "else:\n"
            "    divisor = 1024.0 * 1024.0 if sys.platform == 'darwin' else 1024.0\n"
            "    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / divisor\n"
            "print(f'{dur:.2f},{rss:.2f}')\n"
        )

        res = subprocess.run(
            [sys.executable, "-s", "-c", probe_code],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if res.returncode != 0:
            raise RuntimeError(f"Cold import benchmark failed:\n{res.stderr}")

        output = res.stdout.strip()
        parts = output.split(",")
        dur_ms = float(parts[0])
        rss_mb = float(parts[1])

        return BenchmarkMetrics(
            cold_import_ms=dur_ms,
            resident_memory_mb=rss_mb,
            interpreter=sys.version.split()[0],
            platform=sys.platform,
        )

    @staticmethod
    def audit_environment() -> HygieneAuditResult:
        """Verify absolute isolation from parent repository and unauthorized legacy modules."""
        foreign_paths: list[str] = []
        unauthorized_modules: list[str] = []

        # 1. Audit sys.path for Nexura V2 parent repo leaks
        forbidden_substrings = ["Nexura V2", "nexura-sdk", "langflow", "lfx"]
        own_tests = _own_tests_dir()
        for p in sys.path:
            if _inside(p, own_tests):
                continue  # this checkout's own test directories
            for forbidden in forbidden_substrings:
                if forbidden.lower() in p.lower() and "nexura-standalone-sdk" not in p:
                    foreign_paths.append(p)

        # 2. Audit sys.modules for the Langflow / lfx packages themselves. Matched on the
        # top-level package, so our own reader of the Langflow file format
        # (``nexura_runtime.integrations.langflow``) is not mistaken for a dependency.
        for mod_name in sys.modules:
            top = mod_name.split(".", 1)[0].lower()
            if top in ("langflow", "lfx") or top.startswith(("langflow_", "lfx_")):
                unauthorized_modules.append(mod_name)

        passed = len(foreign_paths) == 0 and len(unauthorized_modules) == 0
        return HygieneAuditResult(
            passed=passed,
            foreign_paths_found=foreign_paths,
            unauthorized_modules_found=unauthorized_modules,
            details={
                "sys_path_count": len(sys.path),
                "loaded_module_count": len(sys.modules),
            },
        )
