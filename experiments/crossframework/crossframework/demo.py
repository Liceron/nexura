"""Offline harness with a FAKE automatic reviewer; not human authorization evidence."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from .client import connect, run_agent


async def run_demo(state_dir: Path) -> list[dict[str, Any]]:
    async def operator_command(approval_id: str, *decision_args: str) -> dict[str, Any]:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "crossframework.review",
            "--state-dir",
            str(state_dir),
            "--approval-id",
            approval_id,
            *decision_args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), 15)
        except TimeoutError:
            process.kill()
            await process.wait()
            raise
        if process.returncode:
            raise RuntimeError(stderr.decode())
        result = json.loads(stdout)
        if not isinstance(result, dict):
            raise ValueError("Expected an operator response object")
        return result

    async def fake_review(pending: dict[str, Any]) -> None:
        # The fake operator inspects authoritative stored state, rather than treating
        # model-carried arguments or digests as its source of approval authority.
        stored = await operator_command(pending["approval_id"])
        await operator_command(
            stored["approval_id"],
            "--expected-digest",
            stored["payload_digest"],
            "--decision",
            "approved",
        )

    async with connect(state_dir) as session:
        return await run_agent(session, fake_review)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path)
    args = parser.parse_args()
    if args.state_dir is None:
        with tempfile.TemporaryDirectory(prefix="nexura-crossframework-") as directory:
            transcript = asyncio.run(run_demo(Path(directory)))
    else:
        transcript = asyncio.run(run_demo(args.state_dir.resolve()))
    print(json.dumps({"model": "scripted fake", "reviewer": "fake", "calls": transcript}, indent=2))


if __name__ == "__main__":
    main()
