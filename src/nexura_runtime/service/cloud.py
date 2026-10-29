# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Detect a managed deployment, where development-only switches must never take effect."""

from __future__ import annotations

import os

#: Cloud Run sets ``K_SERVICE`` in every service container and ``CLOUD_RUN_JOB`` in every job.
CLOUD_RUN_ENV_VARS = ("K_SERVICE", "CLOUD_RUN_JOB")


def on_cloud_run() -> str | None:
    """The name of the Cloud Run variable that is set, or ``None`` outside Cloud Run."""
    return next((name for name in CLOUD_RUN_ENV_VARS if os.environ.get(name)), None)


#: Cloud Run sends SIGTERM and stops the container 10 seconds later. Each shutdown phase that
#: waits for work (the trigger worker, a tick's jobs, key revocations) gets at most this long
#: there, so the phases together end before that; jobs cut off are reclaimed by their lease.
CLOUD_RUN_SHUTDOWN_PHASE_SECONDS = 2.5


def shutdown_grace(default: float) -> float:
    """``default`` outside Cloud Run; on Cloud Run at most ``CLOUD_RUN_SHUTDOWN_PHASE_SECONDS``."""
    if on_cloud_run() is None:
        return default
    return min(default, CLOUD_RUN_SHUTDOWN_PHASE_SECONDS)
