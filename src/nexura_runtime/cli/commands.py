# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Lazy adapters for the Nexura Cloud client's ``nexura.commands`` group."""

from collections.abc import Callable


def _command(name: str, help_text: str) -> Callable[[list[str]], int]:
    def main(argv: list[str]) -> int:
        from nexura_runtime.cli.main import main as runtime_main

        try:
            return runtime_main([name, *argv])
        except SystemExit as exc:
            return int(exc.code or 0)

    setattr(main, "help", help_text)  # noqa: B010 - callable plugin metadata
    return main


serve = _command("serve", "Launch the local REST service.")
worker = _command("worker", "Run scheduled and webhook trigger jobs.")
workflow = _command("workflow", "Manage and run workflows.")
repro = _command("repro", "Export or replay a portable run recording.")
redteam = _command("redteam", "Run the red-team attack pack against an agent.")
evals = _command("evals", "Manage Studio eval datasets.")
sandbox = _command("sandbox", "Inspect sandbox backends.")
