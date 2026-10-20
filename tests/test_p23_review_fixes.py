"""P23 review regressions: a recorded path must equal what the workflow target replays.

Promoted from the adversarial review repros (.autopilot/p23/repros).

Offline only: synthetic handlers of the default registry (plus labelled local fake handlers
registered on the app registry), local-owner profile. Each test asserts CORRECT behaviour.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from nexura_agents.evals import Case

from nexura_runtime.evaluation.studio_engine import _run_workflow_case
from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.studio.builtin_modules import get_default_registry
from nexura_runtime.workflow import WorkflowSpec

API = "/api/v1"
BASE = f"{API}/evaluations/studio"


def _client(tmp_path: Path) -> TestClient:
    return TestClient(create_api_app(db_path=str(tmp_path / "r.sqlite3"), profile="local-owner"))


def _put_workflow(c: TestClient, spec: dict[str, Any], wid: str = "wf") -> None:
    r = c.post(f"{API}/workflows", json={"workflow_id": wid, "spec": spec})
    assert r.status_code in (200, 201), r.text


def _completed_run(c: TestClient, state: dict[str, Any] | None = None, wid: str = "wf") -> str:
    r = c.post(
        f"{API}/runs",
        json={"workflow_id": wid, "initial_state": state or {"order_id": "ORD-DELHI-501"}},
    )
    assert r.status_code == 201 and r.json()["status"] == "completed", r.text
    return str(r.json()["run_id"])


def _save(c: TestClient, run_id: str, output_key: str | None = None, **extra: Any) -> Any:
    body: dict[str, Any] = {"run_id": run_id, "dataset_name": "T", **extra}
    if output_key is not None:
        body["output_key"] = output_key
    return c.post(f"{BASE}/datasets/t/cases/from-run", json=body)


def _eval(c: TestClient, output_key: str | None = None) -> dict[str, Any]:
    target = {"kind": "workflow", "id": "wf", "output_key": output_key}
    r = c.post(f"{BASE}/runs", json={"dataset_id": "t", "target": target})
    assert r.status_code == 200, r.text
    out: dict[str, Any] = r.json()
    return out


def _record_and_replay(c: TestClient, output_key: str | None) -> tuple[Any, dict[str, Any]]:
    res = _save(c, _completed_run(c), output_key, record_path=True)
    assert res.status_code == 200, res.text
    return res.json()["case"], _eval(c, output_key)


# --------------------------------------------------------------------------- composites

PARALLEL = {
    "name": "fanout",
    "entry_node": "lookup",
    "nodes": {
        "lookup": {
            "id": "lookup",
            "handler": "synthetic_order_lookup",
            "default_transition": "both",
        },
        "both": {
            "id": "both",
            "parallel": ["synthetic_fake_agent", "synthetic_trace_agent"],
            "reducers": {"agent_turns": "sum"},
            "default_transition": "agent",
        },
        "agent": {"id": "agent", "handler": "synthetic_fake_agent"},
    },
    "terminal_nodes": ["agent"],
}

SUB = {
    "name": "inner",
    "entry_node": "look",
    "nodes": {"look": {"id": "look", "handler": "synthetic_order_lookup"}},
    "terminal_nodes": ["look"],
}
SUBGRAPH = {
    "name": "outer",
    "entry_node": "child",
    "nodes": {
        "child": {"id": "child", "subgraph": SUB, "default_transition": "route"},
        "route": {"id": "route", "handler": "synthetic_router"},
    },
    "terminal_nodes": ["route"],
}


def test_parallel_node_path_is_recorded_and_replay_passes(tmp_path: Path) -> None:
    """Docs: steps inside a parallel node are not listed; the node itself is one step."""
    c = _client(tmp_path)
    _put_workflow(c, PARALLEL)
    case, run = _record_and_replay(c, "agent_turns")
    assert case["expected_trajectory"] is not None and len(case["expected_trajectory"]) == 3
    assert run["summary"]["passed"] == 1, run["summary"]


def test_subworkflow_node_path_is_recorded_and_replay_passes(tmp_path: Path) -> None:
    """Docs: steps inside a sub-workflow are not listed; the node itself is one step."""
    c = _client(tmp_path)
    _put_workflow(c, SUBGRAPH)
    case, run = _record_and_replay(c, "route")
    assert case["expected_trajectory"] is not None and len(case["expected_trajectory"]) == 2
    assert run["summary"]["passed"] == 1, run["summary"]


async def test_subworkflow_with_parent_name_not_counted_on_replay() -> None:
    """The target keeps NODE_STARTED events whose data.workflow == spec.name. A sub-workflow
    that shares the parent's name (names are free text, nothing forbids it) leaks its inner
    steps into the replayed trajectory, so it can never equal a top-level path."""
    sub = copy.deepcopy(SUB)
    sub["name"] = "outer"  # same name as the parent
    # ...and a node id the parent also has (ids are per-workflow, so this is legal).
    sub["entry_node"] = "route"
    sub["nodes"] = {"route": {"id": "route", "handler": "synthetic_order_lookup"}}
    sub["terminal_nodes"] = ["route"]
    spec_d = copy.deepcopy(SUBGRAPH)
    spec_d["nodes"]["child"]["subgraph"] = sub
    spec = WorkflowSpec.from_dict(spec_d)
    case = Case(name="c", input="{}", expected=None, metadata={"input": {"order_id": "ORD-1"}})
    early, out = await _run_workflow_case(
        spec, get_default_registry(), case, "default", "route", frozenset()
    )
    assert early is None and out is not None
    calls = [tc.name for tc in out.messages[1].tool_calls or []]
    # Two top-level steps: the sub-workflow node and the router.
    assert len(calls) == 2, calls


# ----------------------------------------------------------------- terminal label targets

LABEL_TERMINAL = {
    "name": "support",
    "entry_node": "lookup",
    "nodes": {
        "lookup": {
            "id": "lookup",
            "handler": "synthetic_order_lookup",
            "default_transition": "route",
        },
        "route": {
            "id": "route",
            "handler": "synthetic_router",
            "transitions": {"refund": "done", "standard_support": "done"},
        },
    },
    "terminal_nodes": ["done"],
}


def test_transition_to_bare_terminal_label_is_recordable(tmp_path: Path) -> None:
    """A branch into a terminal *label* (not a node) is a normal completed run; the last
    checkpoint names "done", which is not in spec.nodes, so the save is refused."""
    c = _client(tmp_path)
    _put_workflow(c, LABEL_TERMINAL)
    res = _save(c, _completed_run(c), "route", record_path=True)
    assert res.status_code == 200, res.text
    assert res.json()["case"]["expected_trajectory"] == [
        "synthetic_order_lookup",
        "synthetic_router",
    ]


# ------------------------------------------------------------ handler validation / secrets


def test_run_spec_handler_since_unregistered_is_a_clean_error(tmp_path: Path) -> None:
    """_recorded_path re-validates run["spec"] against today's registry via WorkflowEngine();
    a handler no longer registered must not surface as an unhandled 500."""
    c = TestClient(
        create_api_app(db_path=str(tmp_path / "r.sqlite3"), profile="local-owner"),
        raise_server_exceptions=False,
    )
    registry = c.app.state.registry  # type: ignore[attr-defined]
    registry.register("local_fake_step", lambda s: {"done": True})  # labelled local fake
    _put_workflow(
        c,
        {
            "name": "x",
            "entry_node": "a",
            "nodes": {"a": {"id": "a", "handler": "local_fake_step"}},
            "terminal_nodes": ["a"],
        },
    )
    run_id = _completed_run(c, {"x": 1})
    registry._handlers.pop("local_fake_step")
    res = _save(c, run_id, "done", record_path=True)
    assert res.status_code < 500, res.text


def test_secret_like_handler_name_recorded_path_still_replays(tmp_path: Path) -> None:
    """A recorded handler name is redacted by validate_dataset while the replay reports the
    real name, so the saved test can never pass (expected: refused, or replay passes)."""
    c = _client(tmp_path)
    name = "sk-FAKE0123456789abcdef"  # labelled fake credential-shaped handler name
    c.app.state.registry.register(name, lambda s: {"done": True})  # type: ignore[attr-defined]
    _put_workflow(
        c,
        {
            "name": "x",
            "entry_node": "a",
            "nodes": {"a": {"id": "a", "handler": name}},
            "terminal_nodes": ["a"],
        },
    )
    res = _save(c, _completed_run(c, {"x": 1}), "done", record_path=True)
    if res.status_code != 200:
        return  # an explicit refusal is acceptable
    run = _eval(c, "done")
    assert run["summary"]["passed"] == 1, (res.json()["case"], run["summary"])


# --------------------------------------------------------------------- what should hold


def _chain(n: int) -> dict[str, Any]:
    nodes = {}
    for i in range(n):
        node: dict[str, Any] = {"id": f"n{i}", "handler": "synthetic_order_lookup"}
        if i + 1 < n:
            node["default_transition"] = f"n{i + 1}"
        nodes[f"n{i}"] = node
    return {"name": "chain", "entry_node": "n0", "nodes": nodes, "terminal_nodes": [f"n{n - 1}"]}


def test_holds_fifty_step_run(tmp_path: Path) -> None:
    c = _client(tmp_path)
    _put_workflow(c, _chain(50))
    case, run = _record_and_replay(c, "order_id")
    assert len(case["expected_trajectory"]) == 50
    assert run["summary"]["passed"] == 1, run["summary"]


def test_holds_branch_and_map_and_module_free_retry(tmp_path: Path) -> None:
    spec = {
        "name": "mix",
        "entry_node": "lookup",
        "nodes": {
            "lookup": {
                "id": "lookup",
                "handler": "synthetic_order_lookup",
                "retry": {"max_attempts": 3, "initial_delay": 0},
                "default_transition": "route",
            },
            "route": {
                "id": "route",
                "handler": "synthetic_router",
                "transitions": {"refund": "each", "standard_support": "each"},
            },
            "each": {
                "id": "each",
                "handler": "synthetic_order_lookup",
                "map_over": "ids",
                "map_as": "order_id",
                "reducers": {"order_id": "max", "order_found": "max", "error_note": "max"},
                "default_transition": "end",
            },
            "end": {"id": "end", "handler": "synthetic_router"},
        },
        "terminal_nodes": ["end"],
    }
    c = _client(tmp_path)
    _put_workflow(c, spec)
    res = _save(
        c,
        _completed_run(c, {"order_id": "ORD-DELHI-501", "ids": ["A", "B"]}),
        "route",
        record_path=True,
    )
    assert res.status_code == 200, res.text
    assert len(res.json()["case"]["expected_trajectory"]) == 4
    run = _eval(c, "route")
    assert run["summary"]["passed"] == 1, run["summary"]


def test_holds_loop_revisiting_a_node(tmp_path: Path) -> None:
    c = _client(tmp_path)

    def local_fake_counter(state: dict[str, Any]) -> Any:  # labelled local fake handler
        from nexura_runtime.workflow import HandlerOutput

        n = int(state.get("n", 0)) + 1
        return HandlerOutput(state_update={"n": n}, branch="again" if n < 3 else "stop")

    c.app.state.registry.register("local_fake_counter", local_fake_counter)  # type: ignore[attr-defined]
    _put_workflow(
        c,
        {
            "name": "loop",
            "entry_node": "count",
            "nodes": {
                "count": {
                    "id": "count",
                    "handler": "local_fake_counter",
                    "transitions": {"again": "count", "stop": "fin"},
                },
                "fin": {"id": "fin", "handler": "synthetic_order_lookup"},
            },
            "terminal_nodes": ["fin"],
        },
    )
    res = _save(c, _completed_run(c, {"n": 0}), "n", record_path=True)
    assert res.status_code == 200, res.text
    assert res.json()["case"]["expected_trajectory"] == [
        "local_fake_counter",
        "local_fake_counter",
        "local_fake_counter",
        "synthetic_order_lookup",
    ]
    run = _eval(c, "n")
    assert run["summary"]["passed"] == 1, run["summary"]


def test_holds_mixed_dataset_na_scoring(tmp_path: Path) -> None:
    spec = {
        "name": "support",
        "entry_node": "lookup",
        "nodes": {
            "lookup": {
                "id": "lookup",
                "handler": "synthetic_order_lookup",
                "default_transition": "route",
            },
            "route": {"id": "route", "handler": "synthetic_router"},
        },
        "terminal_nodes": ["route"],
    }
    c = _client(tmp_path)
    _put_workflow(c, spec)
    assert _save(c, _completed_run(c), "route", case_name="a").status_code == 200
    assert _save(c, _completed_run(c), "route", case_name="b", record_path=True).status_code == 200
    run = _eval(c, "route")
    assert run["summary"]["passed"] == 2, run["summary"]


def test_holds_edited_spec_uses_run_spec(tmp_path: Path) -> None:
    """The path comes from the run's own spec, not the edited current revision."""
    spec = _chain(2)
    c = _client(tmp_path)
    _put_workflow(c, spec)
    run_id = _completed_run(c)
    edited = copy.deepcopy(spec)
    edited["nodes"]["n1"]["handler"] = "synthetic_router"
    rev = c.get(f"{API}/workflows/wf").json()["revision"]
    assert (
        c.put(f"{API}/workflows/wf", json={"spec": edited, "expected_revision": rev}).status_code
        == 200
    )
    res = _save(c, run_id, "order_id", record_path=True)
    assert res.status_code == 200, res.text
    assert res.json()["case"]["expected_trajectory"] == ["synthetic_order_lookup"] * 2


@pytest.mark.parametrize("lang", ["en", "hi"])
def test_holds_i18n_key(lang: str) -> None:
    import json

    root = Path(__file__).resolve().parents[1]
    data = json.loads((root / "studio-web" / "src" / "i18n" / f"{lang}.json").read_text())
    assert data["workflowTests"]["recordPath"].strip()
