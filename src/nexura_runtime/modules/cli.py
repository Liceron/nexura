# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Standalone CLI for discovering, inspecting, and running Nexura modules."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from nexura_agents.contracts import ExecutionContext
from nexura_agents.contracts.canonical import canonical_json

from nexura_runtime.modules.registry import VersionedModuleRegistry, default_registry


def _load_json_arg(arg_value: str | None) -> dict[str, Any]:
    """Parse a command line argument that may be a JSON string or a file path."""
    if not arg_value:
        return {}
    trimmed = arg_value.strip()
    if trimmed.startswith("{"):
        parsed = json.loads(trimmed)
        if not isinstance(parsed, dict):
            raise ValueError("Argument must be a JSON object.")
        return parsed

    # Check if it's a file path
    try:
        path = Path(arg_value)
        if path.is_file():
            content = path.read_text(encoding="utf-8")
            parsed = json.loads(content)
            if not isinstance(parsed, dict):
                raise ValueError(f"File '{arg_value}' must contain a JSON object.")
            return parsed
    except OSError:
        pass

    # Otherwise parse as inline JSON string
    parsed = json.loads(arg_value)
    if not isinstance(parsed, dict):
        raise ValueError("Argument must be a JSON object.")
    return parsed


async def _run_command(
    registry: VersionedModuleRegistry,
    args: argparse.Namespace,
) -> int:
    try:
        module = registry.get(args.module_id, args.version)
        inputs = _load_json_arg(args.input)
        config = _load_json_arg(args.config)

        context = ExecutionContext(
            workspace_id=args.workspace or "default",
            principal_id=args.principal or "cli_user",
            run_id=args.run_id or "cli_run",
            node_id=args.node_id or f"node_{args.module_id}",
            attempt_id="att_1",
        )

        result = await module.execute(context, inputs, config)

        output_data = {
            "status": result.status,
            "outputs": result.outputs,
            "error_code": result.error_code,
            "error_message": result.error_message,
            "metadata": result.metadata,
        }
        print(canonical_json(output_data))
        return 0 if result.status == "success" else 1

    except Exception as exc:
        err_data = {
            "status": "failure",
            "error_type": type(exc).__name__,
            "error_message": str(exc),
        }
        if hasattr(exc, "code"):
            err_data["error_code"] = exc.code
        if hasattr(exc, "details"):
            err_data["details"] = exc.details
        print(canonical_json(err_data), file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    """Entry point for python -m nexura_runtime.modules."""
    parser = argparse.ArgumentParser(
        prog="python -m nexura_runtime.modules",
        description="Inspect and run Nexura modules standalone without Studio.",
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    # list
    subparsers.add_parser("list", help="List all registered modules")

    # describe
    desc_parser = subparsers.add_parser("describe", help="Describe a module manifest")
    desc_parser.add_argument("module_id", help="Unique namespaced module ID")
    desc_parser.add_argument("--version", "-v", help="Exact semantic version")

    # run
    run_parser = subparsers.add_parser("run", help="Run a module with inputs and config")
    run_parser.add_argument("module_id", help="Unique namespaced module ID")
    run_parser.add_argument("--version", "-v", help="Exact semantic version")
    run_parser.add_argument("--input", "-i", help="JSON string or path to JSON file for inputs")
    run_parser.add_argument("--config", "-c", help="JSON string or path to JSON file for config")
    run_parser.add_argument("--workspace", help="Workspace ID (default: 'default')")
    run_parser.add_argument("--principal", help="Principal ID (default: 'cli_user')")
    run_parser.add_argument("--run-id", help="Run ID (default: 'cli_run')")
    run_parser.add_argument("--node-id", help="Node ID (default: 'node_<module_id>')")

    args = parser.parse_args(argv)
    registry = default_registry

    if args.subcommand == "list":
        modules = registry.list_modules()
        print(canonical_json(modules))
        return 0

    if args.subcommand == "describe":
        try:
            info = registry.describe(args.module_id, args.version)
            print(canonical_json(info))
            return 0
        except Exception as exc:
            print(f"Error describing module: {exc}", file=sys.stderr)
            return 1

    if args.subcommand == "run":

        def run_command() -> int:
            # Own and close our loop without replacing the caller's policy loop.
            with asyncio.Runner(loop_factory=asyncio.new_event_loop) as runner:
                return runner.run(_run_command(registry, args))

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                return pool.submit(run_command).result()
        return run_command()

    return 0


if __name__ == "__main__":
    sys.exit(main())
