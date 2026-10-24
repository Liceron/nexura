# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""P39 G4 review: extra SIGKILL points for the guarded streaming scenario (SQLite, local).

Moved from the independent review's repros. The
assertions are unchanged; ``overlap_chars`` went from 24 to 32, the new minimum. The last
row covers M1: text written before a tool call is journaled only after it was checked.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from durability_child import STREAM_REPLY, STREAM_SECRET
from durability_harness import Harness

pytestmark = pytest.mark.durability

CHECKED = STREAM_REPLY.replace(STREAM_SECRET, "[card]")


def delivered(harness: Harness, name: str) -> list[dict[str, Any]]:
    path = Path(harness.workdir) / f"{name}.events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def texts(events: list[dict[str, Any]]) -> list[str]:
    return [e["data"]["text"] for e in events if e["type"] == "text_delta"]


def no_secret(events: list[dict[str, Any]]) -> None:
    dumped = json.dumps(events)
    assert STREAM_SECRET not in dumped and "4111" not in dumped, dumped


def count_checks() -> int:
    """How many guardrail calls a clean chunked run makes (window checks + final)."""
    from nexura_agents import Agent, FakeModel, GuardrailResult, Runner
    from nexura_agents.stream_guard import StreamGuard

    n = {"n": 0}

    def scrub(ctx: Any, output: Any) -> GuardrailResult:
        n["n"] += 1
        t = str(output)
        if STREAM_SECRET in t:
            return GuardrailResult.rewritten(t.replace(STREAM_SECRET, "[card]"))
        return GuardrailResult.passed()

    agent = Agent(
        name="Streamer",
        instructions="Answer.",
        model=FakeModel([STREAM_REPLY], stream=True, chunk_size=4),
        output_guardrails=[scrub],
        stream_guard=StreamGuard(mode="chunked", chunk_chars=20, overlap_chars=32),
    )

    async def go() -> None:
        async for _ in Runner().stream_async(agent, "card?", stream_text=True):
            pass

    asyncio.run(go())
    return n["n"]


TOTAL = count_checks()


@pytest.mark.parametrize("point", ["final_check", "after_model_next", "after_completed"])
def test_chunked_kill_points(harness: Harness, point: str) -> None:
    marker = f"in_guardrail_{TOTAL}" if point == "final_check" else point
    run = harness.spawn(
        "stream", "run", mode="run", stream_guard="chunked", block={marker: "forever"}
    )
    run_id = str(run.wait_marker(marker)["run_id"])
    run.kill()
    before = delivered(harness, "run")
    no_secret(before)
    shown = "".join(texts(before))
    assert CHECKED.startswith(shown)
    if point == "after_completed":
        assert shown == CHECKED  # the rest is shown before the completed checkpoint

    result = harness.spawn(
        "stream", "recover", mode="recover", run_id=run_id, stream_guard="chunked", claim_wait=10
    ).result()
    assert result["status"] == "completed", result
    after = delivered(harness, "recover")
    no_secret(after)
    finals = [e["data"]["output"] for e in after if e["type"] == "final_output"]
    assert finals == [CHECKED]
    if point == "after_completed":
        assert texts(after) == []
    else:
        assert "".join(texts(after)) == CHECKED


def test_hold_kill_after_model_next(harness: Harness) -> None:
    run = harness.spawn(
        "stream", "run", mode="run", stream_guard="hold", block={"after_model_next": "forever"}
    )
    run_id = str(run.wait_marker("after_model_next")["run_id"])
    run.kill()
    no_secret(delivered(harness, "run"))
    result = harness.spawn(
        "stream", "recover", mode="recover", run_id=run_id, stream_guard="hold", claim_wait=10
    ).result()
    assert result["final_output"] == CHECKED
    after = delivered(harness, "recover")
    no_secret(after)
    assert texts(after) == []


def test_recover_chunked_run_as_hold(harness: Harness) -> None:
    """A run started chunked, killed mid-window, recovered by a process whose agent holds."""
    run = harness.spawn(
        "stream", "run", mode="run", stream_guard="chunked", block={"in_guardrail_2": "forever"}
    )
    run_id = str(run.wait_marker("in_guardrail_2")["run_id"])
    run.kill()
    no_secret(delivered(harness, "run"))
    result = harness.spawn(
        "stream", "recover", mode="recover", run_id=run_id, stream_guard="hold", claim_wait=10
    ).result()
    assert result["final_output"] == CHECKED
    after = delivered(harness, "recover")
    no_secret(after)
    assert texts(after) == []


@pytest.mark.parametrize("mode", ["hold", "chunked"])
def test_pre_tool_text_is_checked_before_it_is_journaled(harness: Harness, mode: str) -> None:
    """M1: killed inside the tool, after the tools_pending checkpoint: the journaled
    transcript holds the checked text, never the card; recovery completes once."""
    from nexura_agents import SQLiteCheckpointStore

    run = harness.spawn(
        "stream", "run", mode="run", stream_guard=mode, pre_tool=True, block={"in_tool": "forever"}
    )
    run.wait_marker("in_tool")
    run.kill()
    no_secret(delivered(harness, "run"))

    async def checkpoints() -> list[tuple[str, str]]:
        store = SQLiteCheckpointStore(str(harness.db_path))
        found = []
        for rid in await store.list_incomplete():
            raw = await store.load(rid)
            if raw:
                found.append((rid, raw))
        return found

    stored = asyncio.run(checkpoints())
    assert len(stored) == 1, "the run left one checkpoint"
    run_id, raw = stored[0]
    assert STREAM_SECRET not in raw and "4111" not in raw
    assert "Checking card [card] now." in raw

    result = harness.spawn(
        "stream", "recover", mode="recover", run_id=run_id, stream_guard=mode, claim_wait=10
    ).result()
    assert result["final_output"] == CHECKED
    no_secret(delivered(harness, "recover"))
