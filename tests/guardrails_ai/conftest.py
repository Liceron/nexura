# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Guardrails AI tests use local toy validators and deny network access."""

from __future__ import annotations

import os
import socket
from typing import Any

import pytest

# Set before importing Guardrails (including test-module collection). OpenTelemetry is left
# alone: disabling it here would switch tracing off for every other test in the process;
# Guardrails metrics are off below and the socket guard proves nothing is sent.
os.environ.setdefault("GUARDRAILS_PROCESS_COUNT", "1")

from guardrails.settings import settings  # noqa: E402

settings.rc.enable_metrics = False
settings.rc.use_remote_inferencing = False
settings.use_server = False


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.rc, "enable_metrics", False)
    monkeypatch.setattr(settings.rc, "use_remote_inferencing", False)
    monkeypatch.setattr(settings, "use_server", False)

    def denied(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("network access forbidden in Guardrails AI tests")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
