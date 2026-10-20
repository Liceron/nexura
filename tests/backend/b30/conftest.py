"""B30 live-gVisor test helper: an explicit operator ``SandboxConfig`` for this host.

Production code never reads ``.agy/review-infrastructure.json`` (B37): whoever controls the
current directory must not choose the runsc binary, the rootfs or a ``sudo`` prefix. The live
tests here are the operator: this helper reads the review-infrastructure file from the project
root (not the current directory) and passes everything explicitly in a ``SandboxConfig``.

Without the file, the config carries no paths, so the worker falls back to
``NEXURA_RUNSC_PATH`` / ``NEXURA_OCI_ROOTFS_PATH`` / ``PATH`` exactly as production does.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from nexura_runtime.integrations.sandbox.models import SandboxConfig

ROOT = Path(__file__).resolve().parents[3]
INFRA_FILE = ROOT / ".agy" / "review-infrastructure.json"


def load_review_infrastructure() -> dict[str, Any]:
    """The review-infrastructure record for this checkout, or ``{}`` when absent."""
    if not INFRA_FILE.is_file():
        return {}
    data = json.loads(INFRA_FILE.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def operator_gvisor_config(**overrides: Any) -> SandboxConfig:
    """Build the operator ``SandboxConfig`` the live gVisor tests run with.

    ``runsc_command_prefix`` (``["sudo", "-n"]`` on the review host) is passed explicitly in
    ``extra``.

    ``allow_without_cgroups=True`` is set deliberately. With cgroups enforced, runsc applies
    the OCI ``pids`` limit (``max_processes``, 1 by default and in these tests) to the whole
    sandbox cgroup, which also holds runsc's own sentry/gofer threads. Checked on the review
    host (cgroup v2, runsc as root via ``sudo -n``) on 2026-10-18: with ``max_processes`` 1
    or 16 the sandbox cannot start ("fork/exec gvisor-sentry-prewarmer: resource temporarily
    unavailable"); with 64 it starts. These tests keep ``max_processes=1`` so the in-sandbox
    ``RLIMIT_NPROC`` blocks ``fork``, so they need the opt-in. Their results therefore report
    ``limits_advisory=True``: the memory/pids cgroup limits are not enforced by runsc here.
    """
    data = load_review_infrastructure()
    sandbox_info = data.get("sandbox") or {}
    extra: dict[str, Any] = {"allow_without_cgroups": True}
    fields: dict[str, Any] = {"backend": "gvisor_oci", "extra": extra}
    runsc = sandbox_info.get("runsc_path") or data.get("runsc_path")
    rootfs = sandbox_info.get("rootfs_path") or data.get("rootfs_path")
    prefix = sandbox_info.get("runsc_command_prefix") or data.get("runsc_command_prefix")
    if runsc:
        fields["runsc_path"] = runsc
    if rootfs:
        fields["rootfs_path"] = rootfs
    if prefix:
        extra["runsc_command_prefix"] = list(prefix)
    fields.update(overrides)
    return SandboxConfig(**fields)


@pytest.fixture
def review_infrastructure() -> dict[str, Any]:
    """The review-infrastructure record (project root), or ``{}`` when absent."""
    return load_review_infrastructure()


@pytest.fixture
def make_gvisor_operator_config() -> Any:
    """Factory for operator configs with overrides (see ``operator_gvisor_config``)."""
    return operator_gvisor_config


def live_gvisor_available() -> bool:
    """True when this host has the gVisor test infrastructure (runsc and a rootfs).

    The test profile runs runsc through a command prefix and without cgroups, which the
    worker allows only with ``NEXURA_REAL_GVISOR=1`` (the release gate sets it when runsc is
    present). Without it these tests skip rather than fail closed.
    """
    if os.environ.get("NEXURA_REAL_GVISOR") != "1":
        return False
    config = operator_gvisor_config()
    runsc = config.runsc_path or shutil.which("runsc")
    return bool(runsc and config.rootfs_path and Path(config.rootfs_path).is_dir())


@pytest.fixture
def gvisor_operator_config() -> SandboxConfig:
    """Operator config for the live gVisor tests; skips them on hosts without gVisor (CI)."""
    if not live_gvisor_available():
        pytest.skip("no gVisor test profile here (runsc + rootfs and NEXURA_REAL_GVISOR=1)")
    return operator_gvisor_config(timeout_seconds=15.0)
