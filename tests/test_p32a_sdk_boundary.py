# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""P32a: the SDK replays agent bundles alone; workflow replay comes from the runtime."""

from __future__ import annotations

import subprocess
import sys

import pytest
from nexura_agents import repro
from nexura_agents.repro import ReproRecorder, export_repro_bundle, replay_repro_bundle

from nexura_runtime.workflow import ModuleRegistry, WorkflowEngine, WorkflowNode, WorkflowSpec


async def _workflow_bundle() -> repro.ReproBundle:
    registry = ModuleRegistry()

    @registry.register_handler("step")
    async def step(state: dict) -> dict:
        return {"done": True}

    spec = WorkflowSpec(
        name="one-step",
        entry_node="step",
        nodes={"step": WorkflowNode(id="step", handler="step")},
    )
    recorder = ReproRecorder()
    await recorder.run_workflow(WorkflowEngine(spec, registry), {})
    return export_repro_bundle(recorder)


def test_importing_the_sdk_repro_module_loads_no_runtime() -> None:
    probe = (
        "import sys, nexura_agents.repro as r; assert r._workflow_replay is None; "
        "bad = [m for m in sys.modules if m.startswith('nexura_runtime')]; assert not bad, bad"
    )
    subprocess.run([sys.executable, "-c", probe], check=True)


async def test_runtime_registers_workflow_replay() -> None:
    result = await replay_repro_bundle(await _workflow_bundle())
    assert result.reproduced, result.first_divergence


async def test_workflow_bundle_without_runtime_is_refused_not_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = await _workflow_bundle()
    monkeypatch.setattr(repro, "_workflow_replay", None)
    result = await replay_repro_bundle(bundle)
    assert not result.reproduced
    assert "import nexura_runtime" in (result.first_divergence or "")
