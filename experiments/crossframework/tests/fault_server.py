"""Explicit test-only fault injection in a real stdio server process."""

import argparse
import asyncio
from pathlib import Path
from typing import Any

from crossframework import server
from crossframework.stdio import serve


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--approval-ttl", type=float, default=900)
    parser.add_argument("--fault", choices=["half-effect", "completion-audit"], required=True)
    args = parser.parse_args()
    original = server.append_record

    def append(path: Path, record: dict[str, Any]) -> None:
        if args.fault == "completion-audit" and record.get("event") == "completed":
            raise OSError("simulated ENOSPC after refund")
        original(path, record)
        if args.fault == "half-effect" and path.name == "fake-refunds.jsonl":
            raise OSError("simulated interruption after the effect, before its outcome was saved")

    server.append_record = append
    asyncio.run(serve(server.build_server(args.state_dir, approval_ttl=args.approval_ttl)))


if __name__ == "__main__":
    main()
