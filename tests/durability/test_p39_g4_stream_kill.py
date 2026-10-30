# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""P39 G4: a crash while a guarded reply is held or checked in windows (SIGKILL rows).

The durability harness runs each process against one SQLite file; the scenario records every
event its consumer received. Nothing unchecked may reach a consumer before or after the
crash, the recovered stream shows the checked reply once, and the stored output is the
checked text. Local FakeModel only.
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


def stored_checkpoint(harness: Harness, run_id: str) -> str | None:
    from nexura_agents import SQLiteCheckpointStore

    async def load() -> str | None:
        raw: str | None = await SQLiteCheckpointStore(str(harness.db_path)).load(run_id)
        return raw

    return asyncio.run(load())


def assert_no_secret(events: list[dict[str, Any]]) -> None:
    dumped = json.dumps(events)
    assert STREAM_SECRET not in dumped and "4111" not in dumped


def recover(harness: Harness, run_id: str, name: str, mode: str) -> dict[str, Any]:
    return harness.spawn(
        "stream", name, mode="recover", run_id=run_id, stream_guard=mode, claim_wait=10
    ).result()


def test_kill_while_the_held_reply_is_checked_leaks_nothing_and_recovers_once(
    harness: Harness,
) -> None:
    run = harness.spawn(
        "stream", "run", mode="run", stream_guard="hold", block={"in_guardrail_1": "forever"}
    )
    run_id = str(run.wait_marker("in_guardrail_1")["run_id"])
    run.kill()
    before = delivered(harness, "run")
    assert texts(before) == []  # nothing of the held reply reached the consumer
    assert_no_secret(before)
    assert not any(e["type"] == "final_output" for e in before)

    result = recover(harness, run_id, "recover", "hold")
    assert result["status"] == "completed", result
    assert result["final_output"] == CHECKED
    after = delivered(harness, "recover")
    assert_no_secret(after)
    assert texts(after) == []
    assert [e["data"]["output"] for e in after if e["type"] == "final_output"] == [CHECKED]
    assert stored_checkpoint(harness, run_id) is None  # finished and cleaned up


def test_kill_mid_window_check_shows_only_checked_windows_and_recovers_without_duplicates(
    harness: Harness,
) -> None:
    run = harness.spawn(
        "stream", "run", mode="run", stream_guard="chunked", block={"in_guardrail_3": "forever"}
    )
    run_id = str(run.wait_marker("in_guardrail_3")["run_id"])
    run.kill()
    before = delivered(harness, "run")
    assert_no_secret(before)
    shown_before = "".join(texts(before))
    assert shown_before and CHECKED.startswith(shown_before)

    result = recover(harness, run_id, "recover", "chunked")
    assert result["status"] == "completed", result
    after = delivered(harness, "recover")
    assert_no_secret(after)
    # The recovered stream is a new stream: it shows the checked reply exactly once.
    assert "".join(texts(after)) == CHECKED
    assert [e["data"]["output"] for e in after if e["type"] == "final_output"] == [CHECKED]


def test_kill_after_the_completed_checkpoint_replays_only_the_checked_output(
    harness: Harness,
) -> None:
    run = harness.spawn(
        "stream", "run", mode="run", stream_guard="hold", block={"after_completed": "forever"}
    )
    run_id = str(run.wait_marker("after_completed")["run_id"])
    run.kill()
    raw = stored_checkpoint(harness, run_id)
    assert raw is not None and STREAM_SECRET not in raw  # the journaled output is checked
    assert "[card]" in raw
    before = delivered(harness, "run")
    assert_no_secret(before)
    assert [e["data"]["output"] for e in before if e["type"] == "final_output"] == [CHECKED]

    result = recover(harness, run_id, "recover", "hold")
    assert result["final_output"] == CHECKED
    after = delivered(harness, "recover")
    assert texts(after) == []  # no model call, no text again
    assert [e["data"]["output"] for e in after if e["type"] == "final_output"] == [CHECKED]
