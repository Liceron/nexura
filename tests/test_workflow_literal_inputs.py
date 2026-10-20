"""``{"$literal": value}`` input mapping: run-time value, approval gating, code round-trip."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent / "connectors"))
from connector_helpers import FakeResolver, Recorder, json_response  # noqa: E402
from nexura_connectors import SlackConnector  # noqa: E402

from nexura_runtime.codegen import python_to_workflow, workflow_to_python  # noqa: E402
from nexura_runtime.workflow import (  # noqa: E402
    ModuleRegistry,
    SQLiteWorkflowStore,
    WorkflowEngine,
    WorkflowNode,
    WorkflowSpec,
    _map_inputs,
)


def _spec(mapping: dict[str, Any]) -> WorkflowSpec:
    node = WorkflowNode(
        id="send", handler="slack", config={"credential_ref": "cred"}, input_mapping=mapping
    )
    return WorkflowSpec(name="lit", entry_node="send", nodes={"send": node})


def _engine(tmp_path: Path, spec: WorkflowSpec) -> tuple[WorkflowEngine, Recorder]:
    rec = Recorder(
        lambda r: json_response({"ok": True, "channel": "C1", "ts": "1", "channels": []})
    )
    reg = ModuleRegistry()
    reg.register("slack", SlackConnector(transport=rec.transport))
    eng = WorkflowEngine(
        spec,
        reg,
        store=SQLiteWorkflowStore(tmp_path / "l.db"),
        credential_resolver=FakeResolver({"cred": "tok_SuperSecretValue_12345678"}),
    )
    return eng, rec


def _posts(rec: Recorder) -> list[httpx.Request]:
    return [r for r in rec.requests if r.method == "POST"]


def test_map_inputs_literal_and_state_key() -> None:
    state = {"post_message": "from-state", "k": 1}
    out = _map_inputs({"a": {"$literal": "post_message"}, "b": "k", "c": 5}, state)
    assert out == {"a": "post_message", "b": 1, "c": 5}
    # a dict with other keys is not the literal form
    assert _map_inputs({"a": {"$literal": 1, "x": 2}}, {}) == {"a": {"$literal": 1, "x": 2}}


async def test_literal_write_pauses_and_literal_read_does_not(tmp_path: Path) -> None:
    eng, rec = _engine(
        tmp_path, _spec({"operation": {"$literal": "post_message"}, "params": "params"})
    )
    paused = await eng.run({"params": {"channel": "C1", "text": "hi"}})
    assert paused.status == "paused" and _posts(rec) == []
    done = await eng.resume(paused.workflow_run_id, paused.pending_approval.approval_id)
    assert done.status == "completed" and len(_posts(rec)) == 1

    eng2, rec2 = _engine(tmp_path / "..", _spec({"operation": {"$literal": "list_channels"}}))
    res = await eng2.run({})
    assert res.status == "completed", res.error
    assert _posts(rec2) == [] and rec2.requests


def test_literal_round_trips_through_codegen() -> None:
    spec = _spec({"operation": {"$literal": "post_message"}, "params": "params"})
    back = python_to_workflow(workflow_to_python(spec))
    assert back.nodes["send"].input_mapping == spec.nodes["send"].input_mapping
