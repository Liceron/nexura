# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""A local fake model fails on its second tool turn, then replays offline."""

import asyncio
import tempfile
from pathlib import Path

from nexura_agents import Agent, FakeModel, Runner, Tool, ToolCall
from nexura_agents.repro import (
    ReproBundle,
    ReproRecorder,
    export_repro_bundle,
    replay_repro_bundle,
)


async def main() -> None:
    async def lookup(item: int) -> str:
        if item == 2:
            raise ValueError("local fixture failure")
        return "first lookup succeeded"

    agent = Agent(
        "failure-demo",
        "Use the local lookup fixture.",
        FakeModel(
            [[ToolCall("one", "lookup", {"item": 1})], [ToolCall("two", "lookup", {"item": 2})]]
        ),
        tools=[Tool("lookup", lookup, max_retries=0)],
    )
    recorder = ReproRecorder()
    try:
        await recorder.run_agent(Runner(), agent, "Look up two items")
    except Exception:
        assert recorder.data["expected_outcome"]["status"] == "failed"
    else:
        raise AssertionError("The local fixture must fail")
    with tempfile.TemporaryDirectory(prefix="nexura-repro-") as directory:
        path = Path(directory) / "bundle.json"
        export_repro_bundle(recorder).save(path)
        result = await replay_repro_bundle(ReproBundle.load(path))
    print(f"reproduced: {result.reproduced}")
    assert result.reproduced, result.first_divergence


if __name__ == "__main__":
    asyncio.run(main())
