"""Shared C1 capability marks; also copied by the standalone wheel test runner."""

from __future__ import annotations

import os
import platform
import tempfile
from functools import lru_cache
from pathlib import Path

import pytest

NO_LANDLOCK_ENV = "NEXURA_TEST_NO_LANDLOCK"
LANDLOCK_REASON = "disk workspaces need Linux Landlock ABI >= 3"
LINUX_LANDLOCK_API = platform.system() == "Linux" and platform.machine() in ("x86_64", "aarch64")


@lru_cache(maxsize=1)
def landlock_available() -> bool:
    """Probe confinement on a disposable worker, never the calling test thread."""
    if not LINUX_LANDLOCK_API or os.environ.get(NO_LANDLOCK_ENV) == "1":
        return False
    from nexura_agents import ScopedFilesystem
    from nexura_agents.errors import SDKValidationError

    try:
        with (
            tempfile.TemporaryDirectory(prefix="nexura-landlock-probe-") as root,
            ScopedFilesystem(Path(root).resolve()),
        ):
            pass
    except (OSError, SDKValidationError):
        return False
    return True


requires_landlock = pytest.mark.skipif(not landlock_available(), reason=LANDLOCK_REASON)
requires_linux_landlock_api = pytest.mark.skipif(
    not LINUX_LANDLOCK_API, reason="Landlock syscall tests require Linux x86-64/AArch64"
)
