# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Guardrails AI telemetry is off unless the user chose it (run in fresh interpreters)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

PROBE = """
import socket
attempts = []
def spy(self, address):
    attempts.append(address)
    raise OSError("blocked")
socket.socket.connect = spy
import nexura_guardrails_ai  # noqa: F401
from guardrails import Guard
from guardrails.settings import settings
from guardrails.utils.hub_telemetry_utils import HubTelemetry
from nexura_guardrails.prefilters import pii
from nexura_guardrails_ai import as_validator
Guard().use(as_validator(pii(), name="telemetry_probe")).validate("hello")
print(settings.rc.enable_metrics, HubTelemetry()._enabled, len(attempts))
"""


def _probe(home: Path, **env: str) -> str:
    environment = {
        k: v for k, v in os.environ.items() if k not in ("NEXURA_GUARDRAILS_AI_TELEMETRY", "HOME")
    }
    environment.update(HOME=str(home), **env)
    proc = subprocess.run(
        [sys.executable, "-c", PROBE],
        capture_output=True,
        text=True,
        timeout=120,
        env=environment,
        check=True,
    )
    return proc.stdout.strip().splitlines()[-1]


def test_unset_preference_means_off_and_nothing_is_sent(tmp_path: Path) -> None:
    assert _probe(tmp_path) == "False False 0"


@pytest.mark.parametrize(
    ("rc", "env", "expected"),
    [
        ("", {"NEXURA_GUARDRAILS_AI_TELEMETRY": "on"}, "True True"),
        ("enable_metrics=true\n", {}, "True True"),
        ("enable_metrics=false\n", {}, "False False"),
        ("no_metrics=false\n", {}, "True True"),
    ],
    ids=["opt-in-env", "rc-enable", "rc-disable", "legacy-no-metrics"],
)
def test_explicit_choices_are_respected(
    tmp_path: Path, rc: str, env: dict[str, str], expected: str
) -> None:
    if rc:
        (tmp_path / ".guardrailsrc").write_text(rc, encoding="utf-8")
    assert _probe(tmp_path, **env).startswith(expected)
