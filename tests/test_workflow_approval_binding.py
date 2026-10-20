"""Approvals bind to the inputs the approver saw; mapped writes show items and are capped."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "connectors"))
from connector_helpers import FakeResolver, Recorder, json_response  # noqa: E402
from nexura_connectors import SlackConnector  # noqa: E402

from nexura_runtime import workflow as wf  # noqa: E402
from nexura_runtime.workflow import (  # noqa: E402
    ModuleRegistry,
    SQLiteWorkflowStore,
    WorkflowEngine,
    WorkflowExecutionError,
    WorkflowNode,
    WorkflowSpec,
    WorkflowValidationError,
)

POST = {"channel": "C1", "text": "approved text"}


def _rec() -> Recorder:
    return Recorder(
        lambda r: json_response({"ok": True, "channel": "C1", "ts": "1", "channels": []})
    )


def _posts(rec: Recorder) -> list[httpx.Request]:
    return [r for r in rec.requests if r.method == "POST"]


def _engine(rec: Recorder, tmp_path: Path, node: WorkflowNode, **kw: Any) -> WorkflowEngine:
    reg = ModuleRegistry()
    reg.register("slack", SlackConnector(transport=rec.transport))
    return WorkflowEngine(
        WorkflowSpec(name="b", entry_node="send", nodes={"send": node}),
        reg,
        store=SQLiteWorkflowStore(tmp_path / "b.db"),
        credential_resolver=FakeResolver({"cred": "tok_SuperSecretValue_12345678"}),
        **kw,
    )


def _node(**kw: Any) -> WorkflowNode:
    return WorkflowNode(id="send", handler="slack", config={"credential_ref": "cred"}, **kw)


async def test_changed_params_regate_and_fresh_approval_sends_new_inputs(tmp_path: Path) -> None:
    rec = _rec()
    eng = _engine(rec, tmp_path, _node())
    p = await eng.run({"operation": "post_message", "params": POST})
    res = await eng.resume(
        p.workflow_run_id,
        p.pending_approval.approval_id,
        state_update={"params": {"channel": "C9", "text": "EDIT"}},
    )
    assert res.status == "paused" and _posts(rec) == []
    assert res.pending_approval.approval_id != p.pending_approval.approval_id
    assert res.pending_approval.input_state["params"]["text"] == "EDIT"
    assert res.pending_approval.payload_digest != p.pending_approval.payload_digest
    done = await eng.resume(res.workflow_run_id, res.pending_approval.approval_id)
    assert done.status == "completed" and len(_posts(rec)) == 1
    assert b"EDIT" in _posts(rec)[0].content


async def test_read_approval_cannot_become_write(tmp_path: Path) -> None:
    rec = _rec()
    eng = _engine(rec, tmp_path, _node(requires_approval=True))
    p = await eng.run({"operation": "list_channels", "params": {}})
    res = await eng.resume(
        p.workflow_run_id,
        p.pending_approval.approval_id,
        state_update={"operation": "post_message", "params": POST},
    )
    assert res.status == "paused" and _posts(rec) == []
    assert res.pending_approval.input_state["operation"] == "post_message"


async def test_unchanged_inputs_with_unrelated_update_still_runs(tmp_path: Path) -> None:
    rec = _rec()
    node = _node(input_mapping={"operation": "operation", "params": "params"})
    eng = _engine(rec, tmp_path, node)
    p = await eng.run({"operation": "post_message", "params": POST})
    res = await eng.resume(
        p.workflow_run_id, p.pending_approval.approval_id, state_update={"note": "ok by Alice"}
    )
    assert res.status == "completed" and len(_posts(rec)) == 1


async def test_function_node_edit_after_pause_needs_fresh_approval(tmp_path: Path) -> None:
    # B43: was test_function_node_edit_before_approve_still_documented, which pinned the old
    # behaviour (the function ran the edited state on the old approval). A function handler
    # reads the whole state, so an edit now re-gates it and the edit never runs unapproved.
    seen: list[dict[str, Any]] = []
    reg = ModuleRegistry()

    def h(state: dict[str, Any]) -> dict[str, Any]:
        seen.append(dict(state))
        return {"done": True}

    reg.register("h", h)
    eng = WorkflowEngine(
        WorkflowSpec(
            name="f",
            entry_node="n",
            nodes={"n": WorkflowNode(id="n", handler="h", requires_approval=True)},
        ),
        reg,
        store=SQLiteWorkflowStore(tmp_path / "f.db"),
    )
    p = await eng.run({"to": "a"})
    res = await eng.resume(
        p.workflow_run_id, p.pending_approval.approval_id, state_update={"to": "b"}
    )
    assert res.status == "paused" and seen == []
    assert res.pending_approval.approval_id != p.pending_approval.approval_id
    assert res.pending_approval.input_state["to"] == "b"
    done = await eng.resume(res.workflow_run_id, res.pending_approval.approval_id)
    assert done.status == "completed" and [s["to"] for s in seen] == ["b"]


async def test_map_approval_shows_items_and_count(tmp_path: Path) -> None:
    rec = _rec()
    node = _node(
        map_over="items",
        map_as="item",
        input_mapping={"operation": "op", "params": "item"},
        reducers={"data": "append", "operation": "append", "write": "append"},
    )
    eng = _engine(rec, tmp_path, node)
    items = [{"channel": "C1", "text": f"m{i}"} for i in range(3)]
    p = await eng.run({"op": "post_message", "items": items})
    d = p.pending_approval.details
    assert d["item_count"] == 3 and d["max_writes_per_approval"] == 50
    assert [i["params"]["text"] for i in d["items"]] == ["m0", "m1", "m2"]
    assert d["items_digest"]
    again = type(p.pending_approval).from_dict(p.pending_approval.to_dict())
    assert again.details == d
    # Changing the items after seeing the request re-gates.
    res = await eng.resume(
        p.workflow_run_id,
        p.pending_approval.approval_id,
        state_update={"items": items + [{"channel": "C2", "text": "extra"}]},
    )
    assert res.status == "paused" and _posts(rec) == []
    assert res.pending_approval.details["item_count"] == 4


async def test_map_write_cap_refuses_oversized_list(tmp_path: Path) -> None:
    rec = _rec()
    node = _node(
        map_over="items", map_as="item", input_mapping={"operation": "op", "params": "item"}
    )
    eng = _engine(rec, tmp_path, node, max_map_writes_per_approval=2)
    with pytest.raises((WorkflowValidationError, WorkflowExecutionError), match="at most 2"):
        await eng.run({"op": "post_message", "items": [POST, POST, POST]})
    assert _posts(rec) == []


async def test_approval_flag_reset_after_node(tmp_path: Path) -> None:
    rec = _rec()
    eng = _engine(rec, tmp_path, _node())
    p = await eng.run({"operation": "post_message", "params": POST})
    await eng.resume(p.workflow_run_id, p.pending_approval.approval_id)
    assert wf._NODE_APPROVED.get() is False
    p2 = await eng.run({"operation": "post_message", "params": POST})
    assert p2.status == "paused" and len(_posts(rec)) == 1
