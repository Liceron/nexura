"""Executable keyless example for O07 — Consistent SDK, API and Studio behavior.

Demonstrates:
1. The built-in order-support workflow run through three entry points: the Python
   SDK engine, the local service API (the same API Studio uses) and an exported
   standalone bundle in a separate process.
2. Each entry point pauses at the same approval gate and, once approved, ends in
   the same authoritative state.
3. Unknown workflow versions fail with an actionable error instead of running.
4. A foreign workspace cannot read a workflow it does not own.

Uses only built-in synthetic handlers and FakeModel. The service API part needs
the ``studio`` extra (FastAPI). No network calls or credentials.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from nexura_runtime.errors import UnsupportedWorkflowVersionError

from nexura_runtime.service.api.export import WorkflowExporter
from nexura_runtime.studio.builtin_modules import (
    get_default_registry,
    get_default_workflow_spec,
)
from nexura_runtime.workflow import MemoryWorkflowStore, WorkflowEngine, WorkflowSpec

INITIAL_STATE = {"order_id": "ORD-DELHI-501"}
# Keys whose values must match on every entry point (timestamps are excluded).
COMPARED_KEYS = (
    "order_id",
    "amount",
    "category",
    "route",
    "classification",
    "ticket_id",
    "ticket_status",
    "agent_response",
)


def comparable(status: str, steps: int, state: dict[str, Any]) -> dict[str, Any]:
    return {"status": status, "steps": steps, **{k: state.get(k) for k in COMPARED_KEYS}}


async def run_with_sdk() -> dict[str, Any]:
    engine = WorkflowEngine(
        get_default_workflow_spec(), get_default_registry(), store=MemoryWorkflowStore()
    )
    paused = await engine.run(dict(INITIAL_STATE))
    assert paused.status == "paused" and paused.pending_approval is not None
    assert paused.pending_approval.node_id == "approval_gate"
    done = await engine.resume(
        paused.workflow_run_id, paused.pending_approval.approval_id, decision="approve"
    )
    return comparable(done.status, done.step_count, done.state)


def run_with_service_api(tmp: Path) -> dict[str, Any]:
    from starlette.testclient import TestClient

    from nexura_runtime.service.api.app import create_api_app

    app = create_api_app(db_path=tmp / "service.sqlite3", profile="local-owner")
    # The local service serves loopback peers only and needs its per-session token on every
    # state-changing request (app.state.session_token; Studio reads it from its own origin).
    headers = {"X-Workspace-ID": "default", "X-Nexura-Session": app.state.session_token}
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as client:
        created = client.post(
            "/api/v1/workflows",
            json={"name": "O07 Order Support", "spec": get_default_workflow_spec().to_dict()},
            headers=headers,
        )
        assert created.status_code == 201, created.text
        workflow_id = created.json()["workflow_id"]

        started = client.post(
            "/api/v1/runs",
            json={"workflow_id": workflow_id, "initial_state": INITIAL_STATE},
            headers=headers,
        )
        assert started.status_code == 201, started.text
        run = started.json()
        assert run["status"] == "pending_approval"
        assert run["pending_approval"]["node_id"] == "approval_gate"

        decided = client.post(
            f"/api/v1/runs/{run['run_id']}/decide",
            json={"approval_id": run["pending_approval"]["approval_id"], "decision": "approve"},
            headers=headers,
        )
        assert decided.status_code == 200, decided.text
        final = decided.json()

        # Ownership: another workspace cannot read this workflow.
        foreign = client.get(
            f"/api/v1/workflows/{workflow_id}", headers={"X-Workspace-ID": "other-team"}
        )
        assert foreign.status_code in (403, 404), foreign.status_code
        print(f"Foreign workspace read of {workflow_id}: HTTP {foreign.status_code} (denied)")

    status = "completed" if final["status"] == "completed" else final["status"]
    return comparable(status, final["step_count"], final["current_state"])


def run_exported_bundle(tmp: Path) -> dict[str, Any]:
    bundle_dir = tmp / "bundle"
    WorkflowExporter().export_bundle(get_default_workflow_spec(), target_dir=bundle_dir)
    proc = subprocess.run(
        [sys.executable, str(bundle_dir / "run.py"), json.dumps(INITIAL_STATE), "--approve"],
        capture_output=True,
        text=True,
        cwd=bundle_dir,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    # The runner prints the final state as the last JSON object in its output.
    state_text = proc.stdout[proc.stdout.index("{") :]
    state = json.loads(state_text[: state_text.rindex("}") + 1])
    steps_line = next(line for line in proc.stdout.splitlines() if line.startswith("Steps:"))
    status_line = [line for line in proc.stdout.splitlines() if "Status:" in line][-1]
    status = status_line.split("Status:")[1].strip()
    return comparable(status, int(steps_line.split(":")[1]), state)


def show_unknown_version_rejection() -> None:
    data = get_default_workflow_spec().to_dict()
    data["spec_version"] = 99
    try:
        WorkflowSpec.from_dict(data)
    except UnsupportedWorkflowVersionError as exc:
        print(f"Unknown version rejected: {exc}")
    else:
        raise AssertionError("spec_version 99 must not load")


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="nexura-o07-") as tmp_name:
        tmp = Path(tmp_name)
        results = {
            "sdk": await run_with_sdk(),
            "service_api": run_with_service_api(tmp),
            "exported_bundle": run_exported_bundle(tmp),
        }

    for surface, result in results.items():
        print(
            f"{surface:16} status={result['status']} steps={result['steps']} "
            f"ticket={result['ticket_id']}"
        )
    reference = results["sdk"]
    for surface, result in results.items():
        assert result == reference, f"{surface} diverged from sdk:\n{result}\n{reference}"
    print("All entry points reached the same authoritative state.")

    show_unknown_version_rejection()


if __name__ == "__main__":
    asyncio.run(main())
