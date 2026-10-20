# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Console entrypoint for launching Nexura Studio dev server.

Usage:
    nexura-studio [--host 127.0.0.1] [--port 8000] [--db /path/to/studio.sqlite3]
"""

from __future__ import annotations

import argparse
import sys
from typing import Any


def _build_app(args: argparse.Namespace, create_app: Any) -> Any:
    return create_app(
        db_path=args.db,
        allowed_hosts={args.host, "127.0.0.1", "localhost", "::1", "[::1]"},
        listen_host=args.host,
        allow_unauthenticated_network=args.insecure_allow_unauthenticated_network,
    )


def main() -> None:
    """CLI entrypoint for nexura-studio."""
    code = _run(None)
    if code:
        sys.exit(code)


def _run(argv: list[str] | None) -> int:
    """Run the shared Studio parser and launcher."""
    try:
        import fastapi  # noqa: F401
        import uvicorn
    except ImportError:
        sys.stderr.write(
            "\n"
            "Error: Nexura Studio requires the optional 'studio' extra dependencies "
            "(fastapi, uvicorn).\n"
            "Please install them using:\n"
            '    pip install "nexura-runtime[studio]"\n\n'
        )
        return 1

    from nexura_studio.server import create_app

    parser = argparse.ArgumentParser(
        prog="nexura-studio",
        description="Launch local Nexura Studio web server for workflows and approvals.",
    )
    parser.add_argument(
        "--host",
        type=str,
        default="127.0.0.1",
        help="Host address to bind (default: 127.0.0.1). Remote binding is discouraged.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port number to listen on (default: 8000).",
    )
    parser.add_argument(
        "--db",
        type=str,
        default="./nexura_studio.sqlite3",
        help=(
            "Path to SQLite database file for workflow and run persistence "
            "(default: ./nexura_studio.sqlite3)."
        ),
    )

    parser.add_argument(
        "--insecure-allow-unauthenticated-network",
        action="store_true",
        help=(
            "UNSAFE: allow a non-loopback --host without NEXURA_STUDIO_TOKEN. "
            "Anyone who can reach the port can run workflows. Local throwaway use only."
        ),
    )

    args = parser.parse_args(argv)

    from nexura_runtime.service.auth.errors import UnauthenticatedNetworkError

    try:
        app = _build_app(args, create_app)
    except UnauthenticatedNetworkError as exc:
        sys.stderr.write(f"\nError: {exc}\n\n")
        return 2
    url = f"http://{args.host}:{args.port}"
    sys.stdout.write(
        "\n"
        "======================================================================\n"
        "  Nexura Studio (Local Developer Environment)\n"
        f"  URL:       {url}\n"
        f"  Database:  {args.db}\n"
        "  Mode:      Synthetic Mock Data (No Paid APIs or Network Calls)\n"
        "  Session:   the page reads its per-session token itself; with NEXURA_STUDIO_TOKEN,\n"
        "             open the URL as .../#token=<your token>\n"
        "======================================================================\n\n"
    )
    sys.stdout.flush()

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    main()
