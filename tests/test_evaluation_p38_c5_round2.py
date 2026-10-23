"""C5 review regressions: offline authentication, precision and captured evidence."""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request

from nexura_runtime.evaluation import (
    DatasetExample,
    EvaluationRunner,
    ExactMatchGrader,
    StoredTrace,
    ToolArgumentAccuracyGrader,
    recorded_tool_calls,
)
from nexura_runtime.evaluation.runner import redact_value
from nexura_runtime.evaluation.store import SqliteEvaluationStore
from nexura_runtime.evaluation.studio_engine import local_fake_agent
from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.service.api.evaluations import _get_caller_context
from nexura_runtime.service.auth.models import Principal, WorkspaceRole
from nexura_runtime.service.extensions import Identity

BASE = "/api/v1/evaluations"


class OfflineIdentity:
    name = "c5-round2-offline"
    is_authenticating = True

    def __init__(self, resources: list[str] | None = None) -> None:
        self.resources = resources
        self.workspace = "a"
        self.role: WorkspaceRole = "editor"

    def authenticate(self, request: object, target_workspace_id: str | None) -> Identity:
        return Identity(
            Principal(principal_id="caller", profile="authenticated-shared-service",
                      resources=self.resources, scopes=["run", "edit"]),
            self.workspace, self.role,
        )

    def is_operator(self, request: object) -> bool:
        return False


def test_missing_identity_fails_closed() -> None:
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as denied:
        _get_caller_context(Request({"type": "http"}))
    assert denied.value.status_code == 403


@pytest.mark.parametrize("resources", [[], ["workflow:allowed"]])
async def test_scoped_principal_cannot_import_grade_simulate_or_read_reports(
    tmp_path: Path, resources: list[str],
) -> None:
    identity = OfflineIdentity()
    app = create_api_app(db_path=str(tmp_path / "scopes.sqlite"),
                         profile="authenticated-shared-service", auth_provider=identity)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1") as c:
            assert (await c.post(f"{BASE}/traces", json={
                "run_id": "imported", "timestamp": 1, "output": "private imported evidence",
            })).status_code == 200
            report = await c.post(f"{BASE}/online", json={
                "example": {"input_data": {}, "expected_output": "private imported evidence"},
                "graders": [{"type": "exact_match"}],
            })
            assert report.status_code == 200, report.text
            run_id = report.json()["run_id"]
            identity.resources = resources
            for method, path, body in [
                ("POST", "/traces", {"run_id": "blocked", "timestamp": 1}),
                ("POST", "/online", {"example": {"input_data": {}}}),
                ("POST", "/simulate", {"dataset_id": "any"}),
                ("GET", "/runs", None),
                ("GET", f"/runs/{run_id}", None),
                ("POST", "/compare", {"run_id_a": run_id, "run_id_b": run_id}),
            ]:
                response = await c.request(method, BASE + path, json=body)
                assert response.status_code == 403, response.text
                assert "private imported evidence" not in response.text
    finally:
        app.state.eval_store.close()
        await app.state.store.close()


async def test_workspace_isolation_and_reauthorization(tmp_path: Path) -> None:
    identity = OfflineIdentity()
    app = create_api_app(db_path=str(tmp_path / "workspaces.sqlite"),
                         profile="authenticated-shared-service", auth_provider=identity)
    repo = app.state.workspace_repo
    called: list[str] = []
    app.state.eval_agents = {"foreign": lambda _: called.append("foreign")}
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1") as c:
            assert (await c.post(f"{BASE}/datasets", json={
                "dataset_id": "conversation", "version": "1", "examples": [
                    {"input_data": {"prompt": "hi"}, "simulation": {"turns": ["hi"]}},
                ],
            })).status_code == 200
            # A global host registration cannot dispatch in a shared workspace.
            assert (await c.post(f"{BASE}/simulate", json={
                "dataset_id": "conversation", "agent_id": "foreign",
            })).status_code == 404
            assert not called
            app.state.eval_agents_by_workspace = {
                "a": {"allowed": lambda _: local_fake_agent(None)},
            }
            response = await c.post(f"{BASE}/simulate", json={
                "dataset_id": "conversation", "agent_id": "allowed",
            })
            assert response.status_code == 200, response.text
            assert response.json()["total_examples"] == 1
            assert (await c.post(f"{BASE}/traces", json={
                "run_id": "source", "timestamp": 1, "output": "workspace a secret",
            })).status_code == 200
            report = await c.post(f"{BASE}/online", json={
                "example": {"input_data": {}, "expected_output": "workspace a secret"},
                "graders": [{"type": "exact_match"}],
            })
            assert report.status_code == 200, report.text
            run_id = report.json()["run_id"]
            identity.workspace = "b"
            assert (await c.get(f"{BASE}/runs/{run_id}")).status_code == 404
            assert (await c.post(f"{BASE}/simulate", json={
                "dataset_id": "conversation", "agent_id": "foreign",
            })).status_code == 404
            assert (await c.post(f"{BASE}/datasets", json={
                "dataset_id": "conversation", "version": "1", "examples": [
                    {"input_data": {"prompt": "hi"}, "simulation": {"turns": ["hi"]}},
                ],
            })).status_code == 200
            assert (await c.post(f"{BASE}/simulate", json={
                "dataset_id": "conversation", "agent_id": "allowed",
            })).status_code == 404
            online = await c.post(f"{BASE}/online", json={"example": {"input_data": {}}})
            assert online.status_code == 200, online.text
            assert online.json()["total_examples"] == 0
            identity.workspace = "a"
            # Ownership added/changed after import is rechecked on replay and reports.
            repo.record_resource_ownership("run", "source", "b", "other")
            requests: list[tuple[str, str, dict[str, Any] | None]] = [
                ("POST", "/online", {"example": {"input_data": {}}}),
                ("GET", f"/runs/{run_id}", None),
                ("GET", "/runs", None),
                ("POST", "/compare", {"run_id_a": run_id, "run_id_b": run_id}),
            ]
            for method, path, body in requests:
                response = await c.request(method, BASE + path, json=body)
                assert response.status_code == 403, response.text
                assert "workspace a secret" not in response.text
            payloads: list[dict[str, Any]] = [
                {"run_id": "source"},
                {"run_id": "new", "workflow_id": "foreign-workflow"},
                {"run_id": "new", "events": [{"run_id": "source"}]},
            ]
            for payload in payloads:
                repo.record_resource_ownership("workflow", "foreign-workflow", "b", "other")
                response = await c.post(f"{BASE}/traces", json={"timestamp": 1, **payload})
                assert response.status_code == 403, response.text
            assert not called
    finally:
        app.state.eval_store.close()
        await app.state.store.close()


@pytest.mark.parametrize("expected,actual,rules,passed", [
    (2**53, 2**53 + 1, {}, False),
    (10**400, 10**400 + 1, {}, False),
    (10**400, 10**400, {}, True),
    (10**400, 10**400 + 2, {"absolute_tolerance": 1}, False),
    (10**400, 10**400 + 1, {"absolute_tolerance": 1}, True),
    ("9007199254740992", "9007199254740993", {"normalisers": ["numeric"]}, False),
    ("1.0000000000000000000001", "1.0000000000000000000002",
     {"normalisers": ["numeric"]}, False),
    (True, 1, {}, False),
    (float("inf"), float("inf"), {}, False),
])
def test_numeric_precision(
    expected: Any, actual: Any, rules: dict[str, Any], passed: bool,
) -> None:
    example = DatasetExample.model_validate({
        "input_data": {}, "expected_trajectory": [{"name": "x", "args": {"id": expected}}],
    })
    grader = ToolArgumentAccuracyGrader(argument_rules={"id": rules})
    result = grader.score_trace(example, {"messages": [{"tool_calls": [
        {"id": "c", "name": "x", "arguments": {"id": actual}},
    ]}]})
    assert result.passed is passed


def test_capture_reconciliation_and_ambiguity() -> None:
    def call(name: str) -> dict[str, Any]:
        return {"id": name, "name": name, "arguments": {}}

    events = [{"type": "tool_started", "data": call(name)} for name in ["a", "b"]]
    trace = {"messages": [{"tool_calls": [call("b"), call("c")]}], "events": events}
    assert [s.name for s in recorded_tool_calls(trace)] == ["a", "b", "c"]
    for names in [["b", "a"], ["c"]]:
        trace["messages"] = [{"tool_calls": [call(n) for n in names]}]
        with pytest.raises(ValueError, match="ordering"):
            recorded_tool_calls(trace)


async def test_import_precedence_filters_and_numeric_persistence(tmp_path: Path) -> None:
    from nexura_runtime.workflow import WorkflowNode, WorkflowSpec

    app = create_api_app(db_path=str(tmp_path / "capture.sqlite"))
    spec = WorkflowSpec(name="capture", entry_node="x",
                        nodes={"x": WorkflowNode(id="x", handler="synthetic_order_lookup")},
                        terminal_nodes=["x"])
    card = 4111111111111111
    try:
        await app.state.store.record_run_start("recorded", "wf", spec, {})
        await app.state.store.update_run("recorded", "completed", None, {"studio": True}, 1)
        app.state.workspace_repo.record_resource_ownership("run", "recorded", "default", "owner")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1") as c:
            response = await c.post(f"{BASE}/traces", json={
                "run_id": "recorded", "workflow_id": "wf", "agent_id": "agent-a",
                "timestamp": 1, "output": [{"card": card}],
                "messages": [{"tool_calls": [{
                    "id": "c", "name": "charge", "arguments": {"card": card},
                }]}],
            })
            assert response.status_code == 200, response.text
            stored = app.state.eval_store.list_traces("default")[0]
            assert stored.output == [{"card": "[REDACTED_CARD]"}]
            assert str(card) not in stored.model_dump_json()
            for config, count in [
                ({"workflow_id": "wf", "agent_id": "agent-a", "start_time": 1, "end_time": 1}, 1),
                ({"workflow_id": "wrong"}, 0),
                ({"start_time": 2}, 0),
                ({"end_time": 0}, 0),
            ]:
                response = await c.post(f"{BASE}/online", json={
                    "example": {"input_data": {}, "expected_trajectory": ["charge"]},
                    "config": config,
                })
                assert response.status_code == 200, response.text
                assert response.json()["total_examples"] == count
                if count:
                    result = response.json()["results"][0]
                    assert result["metadata"]["trace"]["timestamp"] == 1
                    assert result["actual_output"] == [{"card": "[REDACTED_CARD]"}]
                    assert result["passed"]
    finally:
        app.state.eval_store.close()
        await app.state.store.close()


async def test_numeric_redaction_in_output_metadata_and_grader_persistence(tmp_path: Path) -> None:
    card = 4111111111111111
    example = DatasetExample(input_data={}, expected_output={"card": card})
    summary = await EvaluationRunner([ExactMatchGrader()]).evaluate_traces([
        StoredTrace(run_id="numeric", timestamp=1, output={"card": card},
                    messages=[{"tool_calls": [{"arguments": {"card": card}}]}]),
    ], example)
    store = SqliteEvaluationStore(str(tmp_path / "redaction.sqlite"))
    try:
        store.save_run(summary, "a")
        stored = store.get_run(summary.run_id, "a")
        assert stored is not None
        assert str(card) not in stored.model_dump_json()
        assert stored.results[0].metadata["trace"]["timestamp"] == 1
        assert stored.results[0].actual_output == {"card": "[REDACTED_CARD]"}
        assert redact_value([{"card": card}, True, 4]) == [
            {"card": "[REDACTED_CARD]"}, True, 4,
        ]
        assert redact_value({"api_key": "credential"}) == {"api_key": "[REDACTED]"}
    finally:
        store.close()


def test_promoted_review_assertions_are_unchanged() -> None:
    root = Path(__file__).resolve().parents[1]
    path = root / "tests/test_evaluation_p38_c5_review.py"
    assertions = "\n".join(ast.dump(n) for n in ast.walk(ast.parse(path.read_text()))
                           if isinstance(n, ast.Assert))
    # SHA256 of the original review repros' assertion ASTs, before promotion.
    assert hashlib.sha256(assertions.encode()).hexdigest() == (
        "bddc949b590b9bd78c6c2262e49d638e29f02f5d65d1e29e0c028ef098ccc520"
    )
