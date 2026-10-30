# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""P38 C1 todo state before and after result checkpoint under real SIGKILL."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from durability_harness import Child
from nexura_agents.harness import TODO_STATE_KEY
from p38_c1_child import PLAN

pytestmark = pytest.mark.durability


def spawn(workdir: Path, name: str, **params: Any) -> Child:
    """Use the existing process/marker lifecycle with a separate, socket-free scenario."""
    config = {
        "name": name,
        "workdir": str(workdir),
        "db": str(workdir / "todo.sqlite3"),
        "backend": {"name": "sqlite"},
        "effect_url": "",
        "block": {},
        **params,
    }
    path = workdir / f"{name}.config.json"
    path.write_text(json.dumps(config))
    log_path = workdir / f"{name}.log"
    with log_path.open("wb") as log:
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).with_name("p38_c1_child.py")), str(path)],
            cwd=workdir,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    return Child(name, process, workdir, log_path, workdir / f"{name}.result.json")


@pytest.mark.parametrize("point", ["after_todo_mutation", "after_tool_result"])
def test_todo_survives_sigkill_and_durable_recovery(tmp_path: Path, point: str) -> None:
    first = spawn(tmp_path, "first", mode="run", block={point: "forever"})
    try:
        run_id = first.wait_marker(point)["run_id"]
    finally:
        first.kill()
    recovered = spawn(tmp_path, "recovered", mode="recover", run_id=run_id)
    try:
        result = recovered.result()
    finally:
        recovered.kill()
    assert result["status"] == "completed" and result["output"] == "done"
    assert result["state"][TODO_STATE_KEY] == PLAN
    # Before checkpoint: the idempotent replacement is replayed; after checkpoint: it isn't.
    repeated = [e for e in result["events"] if e["data"].get("name") == "nexura.todos_updated"]
    assert len(repeated) == (1 if point == "after_todo_mutation" else 0)
