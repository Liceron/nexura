"""Test-only simulation of a platform without Landlock, including wheel consumers."""

from __future__ import annotations

import os

import pytest
from _c1_platform import NO_LANDLOCK_ENV


@pytest.fixture(autouse=True)
def _without_landlock(monkeypatch: pytest.MonkeyPatch) -> None:
    if os.environ.get(NO_LANDLOCK_ENV) != "1":
        return
    import nexura_agents._filesystem_sandbox as sandbox
    from nexura_agents.errors import SDKValidationError

    def unavailable(root_fd: int) -> None:
        raise SDKValidationError(
            "Disk workspaces require Linux Landlock ABI >= 3; use memory mode."
        )

    monkeypatch.setattr(sandbox, "restrict_worker", unavailable)
