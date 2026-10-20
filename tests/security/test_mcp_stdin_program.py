"""MCP stdio: a launch with no script reads its program from stdin; -i opens a REPL there."""

import sys

import pytest
from nexura_agents.mcp.transports import _inline_code_argument


@pytest.mark.parametrize(
    "cmd",
    [
        [sys.executable],
        [sys.executable, "-u"],
        [sys.executable, "-i", "server.py"],
        ["node"],
        ["node", "--no-warnings"],
        ["node", "-i"],
    ],
)
def test_program_from_stdin_or_repl_is_refused(cmd):
    assert _inline_code_argument(cmd) is not None, cmd


@pytest.mark.parametrize(
    "cmd",
    [[sys.executable, "server.py"], [sys.executable, "-u", "-m", "pkg"], ["node", "server.js"]],
)
def test_normal_launches_are_allowed(cmd):
    assert _inline_code_argument(cmd) is None, cmd
