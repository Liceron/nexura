"""P38 C6 review round 2: real concurrent processes migrating one SQLite run (no network).

Half of the processes are v2 migrators (hook +100), half are stale v1 workers (recover, or
deciding the v1 approval). All start together from a file barrier with randomized jitter.
After every round: the hook commits at most once, the v1 handler never sees migrated state,
the v2 handler never sees unmigrated state, the action runs at most once and the final
checkpoint carries the definition that produced it. Handlers are local fakes.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from nexura_runtime.workflow import (
    ModuleRegistry,
    SQLiteWorkflowStore,
    WorkflowEngine,
    WorkflowNode,
    WorkflowSpec,
)

WORKER = r"""
import asyncio, json, os, random, sys, time
from pathlib import Path
from nexura_runtime.workflow import (
    SQLiteWorkflowStore, ModuleRegistry, WorkflowEngine, WorkflowNode, WorkflowSpec,
)
root, role, seed, mode = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3]), sys.argv[4]
random.seed(seed)

def log(name, value):
    with open(root / name, "a") as fh:
        fh.write(json.dumps(value) + "\n")

def spec(version, handler):
    return WorkflowSpec(name="mp", version=version, entry_node="act", nodes={
        "act": WorkflowNode(id="act", handler=handler, requires_approval=(mode == "approval"))})

async def main():
    store = SQLiteWorkflowStore(root / "state.db")
    reg = ModuleRegistry()
    reg.register("old_h", lambda s: log("old_h", s) or {"by": "old"}, effect="pure")
    reg.register("new_h", lambda s: log("new_h", s) or {"by": "new"}, effect="pure")
    while not (root / "go").exists():
        await asyncio.sleep(0.002)
    await asyncio.sleep(random.random() * 0.05)
    try:
        if role == "v2":
            v2 = spec(2, "new_h")
            def hook(a, b, s):
                return log("hook", s) or {**s, "value": s["value"] + 100}
            v2.register_migration(1, 2, hook)
            engine = WorkflowEngine(v2, reg, store=store)
            if mode == "approval":
                cp = await engine.migrate("r")
                log("result", {"role": role, "ok": cp.status})
            else:
                res = await engine.recover("r")
                log("result", {"role": role, "ok": res.status})
        else:
            engine = WorkflowEngine(spec(1, "old_h"), reg, store=store)
            if mode == "approval":
                approval = json.loads((root / "approval").read_text())
                res = await engine.resume("r", approval)
            else:
                res = await engine.recover("r")
            log("result", {"role": role, "ok": res.status})
    except Exception as exc:
        log("result", {"role": role, "err": type(exc).__name__})
    finally:
        await store.close()

asyncio.run(main())
"""


def _setup(root: Path, mode: str) -> None:
    class Crash(BaseException):
        pass

    async def run() -> None:
        store = SQLiteWorkflowStore(root / "state.db")
        reg = ModuleRegistry()

        def crash(state: dict[str, Any]) -> dict[str, Any]:
            raise Crash

        reg.register("old_h", crash, effect="pure")
        spec = WorkflowSpec(
            name="mp",
            version=1,
            entry_node="act",
            nodes={
                "act": WorkflowNode(
                    id="act", handler="old_h", requires_approval=(mode == "approval")
                )
            },
        )
        try:
            if mode == "approval":
                paused = await WorkflowEngine(spec, reg, store=store).run(
                    {"value": 1}, workflow_run_id="r"
                )
                assert paused.pending_approval is not None
                (root / "approval").write_text(json.dumps(paused.pending_approval.approval_id))
            else:
                with pytest.raises(Crash):
                    await WorkflowEngine(spec, reg, store=store).run(
                        {"value": 1}, workflow_run_id="r"
                    )
        finally:
            await store.close()

    asyncio.run(run())


def _lines(path: Path) -> list[Any]:
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


@pytest.mark.parametrize("mode", ["recover", "approval"])
@pytest.mark.parametrize("round_", range(6))
def test_b1_concurrent_processes(tmp_path: Path, mode: str, round_: int) -> None:
    _setup(tmp_path, mode)
    roles = ["v2", "v1", "v2", "v1", "v2", "v1"]
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", WORKER, str(tmp_path), role, str(round_ * 10 + i), mode],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for i, role in enumerate(roles)
    ]
    time.sleep(1.5)  # let every interpreter import and reach the barrier
    (tmp_path / "go").touch()
    for p in procs:
        out, err = p.communicate(timeout=60)
        assert p.returncode == 0, err[-2000:]
    hooks = _lines(tmp_path / "hook")
    old = _lines(tmp_path / "old_h")
    new = _lines(tmp_path / "new_h")
    results = _lines(tmp_path / "result")
    assert len(results) == len(roles), results  # every process reported an outcome
    assert len(hooks) <= 1, hooks
    assert all(s["value"] == 1 for s in old), "v1 handler ran on migrated state"
    assert all(s["value"] == 101 for s in new), "v2 handler ran on unmigrated state"
    assert len(old) + len(new) <= 1, "the action ran more than once"

    async def final() -> None:
        store = SQLiteWorkflowStore(tmp_path / "state.db")
        try:
            cp = await store.get("r")
            assert cp is not None
            v1 = WorkflowSpec(
                name="mp",
                version=1,
                entry_node="act",
                nodes={
                    "act": WorkflowNode(
                        id="act", handler="old_h", requires_approval=(mode == "approval")
                    )
                },
            )
            if old:
                assert cp.spec_hash == v1.canonical_hash()
            if hooks:
                assert cp.workflow_version == 2 and cp.state["value"] == 101
        finally:
            await store.close()

    asyncio.run(final())
