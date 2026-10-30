# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Completed summary checkpoint reused by a fresh process after a real SIGKILL."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_p38_c1_todos import spawn

pytestmark = pytest.mark.durability


def test_summary_is_checkpointed_before_main_model_and_replayed_after_sigkill(
    tmp_path: Path,
) -> None:
    first = spawn(tmp_path, "first", mode="run", summary=True, block={"in_main_model": "forever"})
    try:
        run_id = first.wait_marker("in_main_model")["run_id"]
    finally:
        first.kill()
    recovered = spawn(tmp_path, "recovered", mode="recover", summary=True, run_id=run_id)
    try:
        result = recovered.result()
    finally:
        recovered.kill()
    assert result["status"] == "completed" and result["output"] == "done"
    assert result["total_tokens"] == 10
    assert len((tmp_path / "summary-calls.jsonl").read_text().splitlines()) == 1
    (request,) = [
        json.loads(line) for line in (tmp_path / "main-requests.jsonl").read_text().splitlines()
    ]
    assert sum((message.get("content") or "").count("SAVED SUMMARY") for message in request) == 1
