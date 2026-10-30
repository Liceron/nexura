# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Installed command plugins share the existing CLI parsers and exit codes."""

from __future__ import annotations

import re
import subprocess
import sys
from importlib.metadata import EntryPoint, entry_points
from pathlib import Path

import pytest

COMMANDS = tuple(entry_points(group="nexura.commands"))
RUNTIME_NAMES = {
    "serve",
    "worker",
    "workflow",
    "repro",
    "redteam",
    "evals",
    "sandbox",
    # P37 B5: `init` and `deploy` are client built-ins, so they live under `project`.
    "dev",
    "project",
    "logout",
    "whoami",
    "push",
}
BUILTINS = {
    "doctor",
    "login",
    "init",
    "run",
    "status",
    "traces",
    "deploy",
    "logs",
    "rollback",
    "stop",
    "secret",
    "help",
    "version",
}


def test_installed_command_names_and_owners() -> None:
    assert {ep.name for ep in COMMANDS} == RUNTIME_NAMES | {"studio"}
    assert len(COMMANDS) == 13
    for ep in COMMANDS:
        assert re.fullmatch(r"[a-z][a-z0-9-]{0,31}", ep.name)
        assert ep.name not in BUILTINS
        assert ep.dist is not None
        assert ep.dist.name == ("nexura-studio" if ep.name == "studio" else "nexura-runtime")


@pytest.mark.parametrize("ep", COMMANDS, ids=lambda ep: ep.name)
def test_command_help(ep: EntryPoint, capsys: pytest.CaptureFixture[str]) -> None:
    target = ep.load()
    assert callable(target)
    assert isinstance(target.help, str)
    assert target.help.strip()
    assert len(target.help.splitlines()) == 1
    assert target(["--help"]) == 0
    assert "usage:" in capsys.readouterr().out


@pytest.mark.parametrize("ep", COMMANDS, ids=lambda ep: ep.name)
def test_bad_option_returns_code(ep: EntryPoint, capsys: pytest.CaptureFixture[str]) -> None:
    assert ep.load()(["--not-a-nexura-option"]) != 0
    assert "error:" in capsys.readouterr().err


@pytest.mark.parametrize("name", ["serve", "studio", "dev", "project"])
def test_help_matches_console_script(name: str, capsys: pytest.CaptureFixture[str]) -> None:
    ep = next(ep for ep in COMMANDS if ep.name == name)
    assert ep.load()(["--help"]) == 0
    plugin_output = capsys.readouterr()
    executable = Path(sys.executable).parent / (
        "nexura-studio" if name == "studio" else "nexura-runtime"
    )
    argv = [str(executable), *([] if name == "studio" else [name]), "--help"]
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=60, check=False)
    assert proc.returncode == 0
    assert proc.stdout == plugin_output.out
    assert proc.stderr == plugin_output.err
