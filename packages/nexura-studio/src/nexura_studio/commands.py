# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Adapter for the Nexura Cloud client's ``nexura.commands`` group."""


def main(argv: list[str]) -> int:
    from nexura_studio.cli import _run

    try:
        return _run(argv)
    except SystemExit as exc:
        return int(exc.code or 0)


setattr(main, "help", "Launch the local Nexura Studio web server.")  # noqa: B010
