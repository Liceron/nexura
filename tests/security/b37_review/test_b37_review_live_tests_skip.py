"""B37 review regression (promoted from .autopilot/b37/repros).

B37 review repro (pre-existing, not a B37 regression): live gVisor tests fail without gVisor.

``tests/backend/b30/test_sandbox.py`` has two live tests
(``test_live_gvisor_execution_under_genuine_isolation`` and
``test_sandbox_execution_module_contract_with_gvisor``) with no skip guard. On a host with no
``.agy/review-infrastructure.json`` and no ``runsc`` (GitHub CI runs ``pytest --deselect
tests/backend/b22``, which includes b30), they fail instead of skipping. SECURITY_MODEL.md says
"real gVisor is not exercised in CI here".

The repro copies the b30 conftest and test file into a temporary tree (so the conftest's
project-root infra file is absent), removes ``runsc`` from ``PATH`` and runs only those two
tests in a child pytest. Nothing reaches gVisor: the worker fails closed before spawning.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]  # tests/security/b37_review -> project root
B30 = ROOT / "tests" / "backend" / "b30"


def test_live_gvisor_tests_skip_when_no_gvisor_is_configured(tmp_path: Path) -> None:
    dest = tmp_path / "tests" / "backend" / "b30"
    dest.mkdir(parents=True)
    shutil.copy(B30 / "conftest.py", dest / "conftest.py")
    shutil.copy(B30 / "test_sandbox.py", dest / "test_sandbox.py")
    (tmp_path / "pytest.ini").write_text("[pytest]\nasyncio_mode = auto\n", encoding="utf-8")

    env = {k: v for k, v in os.environ.items() if not k.startswith("NEXURA_")}
    env["PATH"] = os.pathsep.join(
        p for p in env.get("PATH", "").split(os.pathsep) if p and not (Path(p) / "runsc").exists()
    )
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-c",
            str(tmp_path / "pytest.ini"),
            "--rootdir",
            str(tmp_path),
            str(dest / "test_sandbox.py"),
            "-k",
            "live_gvisor_execution or module_contract_with_gvisor",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    out = proc.stdout + proc.stderr
    assert "2 skipped" in out and "failed" not in out, out[-2000:]
