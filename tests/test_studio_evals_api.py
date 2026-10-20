"""Studio evals API: datasets, runs against workflows/agents, comparison and the CI gate.

Everything is offline: workflows use the synthetic handlers, agents use ``FakeModel``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from nexura_agents import Agent, FakeModel
from nexura_agents.evals import EvalRegressionError, EvalReport, assert_no_regressions

from nexura_runtime.evaluation import studio_engine
from nexura_runtime.service.api.app import create_api_app

BASE = "/api/v1/evaluations/studio"
TWO_STEP = {
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
ONE_STEP = {
    "name": "support",
    "entry_node": "lookup",
    "nodes": {"lookup": {"id": "lookup", "handler": "synthetic_order_lookup"}},
    "terminal_nodes": ["lookup"],
}
CASES = [
    {
        "name": "delhi",
        "input": {"order_id": "ORD-DELHI-501"},
        "expected_output": "refund",
        "expected_trajectory": ["synthetic_order_lookup", "synthetic_router"],
        "tags": ["orders"],
    },
    {
        "name": "unknown",
        "input": {"order_id": "ORD-NOPE"},
        "expected_trajectory": ["synthetic_order_lookup", "synthetic_router"],
    },
]


@pytest.fixture
def app(tmp_path: Path) -> Any:
    application = create_api_app(db_path=str(tmp_path / "evals.sqlite3"), profile="local-owner")
    application.state.eval_agents = {
        "answerer": lambda version: Agent(
            name="answerer",
            instructions="x",
            model=FakeModel(handler=lambda m, t: "refund in 30 days" if version == "v2" else "ok"),
        )
    }
    return application


def _client(app: Any, workspace: str = "default") -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
        headers={"X-Workspace-ID": workspace},
    )


async def _workflow(client: AsyncClient, workflow_id: str, spec: dict[str, Any]) -> None:
    res = await client.post("/api/v1/workflows", json={"workflow_id": workflow_id, "spec": spec})
    assert res.status_code in (200, 201), res.text


async def _dataset(client: AsyncClient, dataset_id: str = "ds1", cases: Any = CASES) -> Any:
    return await client.put(
        f"{BASE}/datasets/{dataset_id}", json={"name": "Orders", "cases": cases}
    )


async def _run(client: AsyncClient, target: dict[str, Any], **extra: Any) -> dict[str, Any]:
    res = await client.post(f"{BASE}/runs", json={"dataset_id": "ds1", "target": target, **extra})
    assert res.status_code == 200, res.text
    out: dict[str, Any] = res.json()
    return out


@pytest.mark.asyncio
async def test_dataset_crud_export_import_roundtrip(app: Any) -> None:
    async with _client(app) as c:
        created = await _dataset(c)
        assert created.status_code == 200, created.text
        assert created.json()["revision"] == 1 and created.json()["case_count"] == 2
        listed = (await c.get(f"{BASE}/datasets")).json()
        assert [d["dataset_id"] for d in listed["datasets"]] == ["ds1"]
        exported = (await c.get(f"{BASE}/datasets/ds1/export")).json()
        assert exported["format"] == "nexura.eval-dataset"
        # Import = PUT of the exported document under another id.
        imported = await c.put(f"{BASE}/datasets/ds2", json=exported)
        assert imported.status_code == 200
        assert (await c.get(f"{BASE}/datasets/ds2")).json()["cases"] == exported["cases"]
        # Edit bumps the revision; stale expected_revision conflicts.
        edited = await c.put(
            f"{BASE}/datasets/ds1", json={"cases": CASES[:1], "expected_revision": 1}
        )
        assert edited.json()["revision"] == 2
        stale = await c.put(f"{BASE}/datasets/ds1", json={"cases": CASES, "expected_revision": 1})
        assert stale.status_code == 409 and stale.json()["detail"]["code"] == "REVISION_CONFLICT"
        assert (await c.delete(f"{BASE}/datasets/ds2")).status_code == 200
        assert (await c.get(f"{BASE}/datasets/ds2")).status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cases", "code", "status"),
    [
        ([], "EMPTY_DATASET", 422),
        ([{"name": "a"}], "DATASET_VALIDATION_ERROR", 422),
        (
            [{"name": "a", "input": "x"}, {"name": "a", "input": "y"}],
            "DATASET_VALIDATION_ERROR",
            422,
        ),
        ([{"name": "a", "input": "x", "bogus": 1}], "DATASET_VALIDATION_ERROR", 422),
        (
            [{"name": "a", "input": "x", "expected_trajectory": "tool"}],
            "DATASET_VALIDATION_ERROR",
            422,
        ),
        ([{"name": "a", "input": "x" * 9000}], "DATASET_VALIDATION_ERROR", 422),
        ([{"name": f"c{i}", "input": "x"} for i in range(201)], "DATASET_TOO_LARGE", 413),
    ],
)
async def test_bad_and_oversize_datasets_rejected(
    app: Any, cases: list[Any], code: str, status: int
) -> None:
    async with _client(app) as c:
        res = await _dataset(c, "bad", cases)
        assert res.status_code == status, res.text
        assert res.json()["detail"]["code"] == code
        assert (await c.get(f"{BASE}/datasets/bad")).status_code == 404


@pytest.mark.asyncio
async def test_bad_dataset_id_and_format_and_bytes(app: Any) -> None:
    async with _client(app) as c:
        res = await c.put(f"{BASE}/datasets/bad id", json={"cases": CASES})
        assert res.status_code in (400, 404)
        res = await c.put(f"{BASE}/datasets/.hidden", json={"cases": CASES})
        assert res.json()["detail"]["code"] == "INVALID_DATASET_ID"
        res = await c.put(f"{BASE}/datasets/ds", json={"cases": CASES, "format": "other"})
        assert res.json()["detail"]["code"] == "UNSUPPORTED_FORMAT"
        big = [{"name": f"c{i}", "input": "y" * 7000} for i in range(100)]
        res = await c.put(f"{BASE}/datasets/ds", json={"cases": big})
        assert res.status_code == 413


@pytest.mark.asyncio
async def test_workflow_run_has_per_case_scores_and_reasons(app: Any) -> None:
    async with _client(app) as c:
        await _workflow(c, "wf-good", TWO_STEP)
        await _dataset(c)
        run = await _run(c, {"kind": "workflow", "id": "wf-good", "output_key": "route"})
        assert run["target"]["version"] == "rev_1"
        assert run["summary"]["total"] == 2
        rows = {r["name"]: r for r in run["report"]["results"]}
        delhi = {s["name"]: s for s in rows["delhi"]["scores"]}
        assert delhi["exact_match"]["passed"] and delhi["tool_trajectory"]["passed"]
        assert rows["delhi"]["output_text"] == "refund"
        # A case without expected_output is n/a for exact_match, with an explanation.
        unknown = {s["name"]: s for s in rows["unknown"]["scores"]}
        assert unknown["exact_match"]["reason"].startswith("n/a")
        assert run["scorers"] == [
            {"type": "exact_match"},
            {"type": "tool_trajectory", "mode": "strict"},
        ]
        assert (await c.get(f"{BASE}/runs/{run['run_id']}")).json()["summary"] == run["summary"]
        listed = (await c.get(f"{BASE}/runs", params={"dataset_id": "ds1"})).json()
        assert listed["total"] == 1 and "report" not in listed["runs"][0]


@pytest.mark.asyncio
async def test_compare_flags_regression_and_gate_matches_sdk(app: Any) -> None:
    async with _client(app) as c:
        await _workflow(c, "wf-good", TWO_STEP)
        await _workflow(c, "wf-bad", ONE_STEP)
        await _dataset(c)
        base = await _run(c, {"kind": "workflow", "id": "wf-good", "output_key": "route"})
        cand = await _run(c, {"kind": "workflow", "id": "wf-bad", "output_key": "route"})
        cmp_res = await c.post(
            f"{BASE}/compare",
            json={"baseline_run_id": base["run_id"], "candidate_run_id": cand["run_id"]},
        )
        assert cmp_res.status_code == 200, cmp_res.text
        body = cmp_res.json()
        assert (
            body["gate"]["passed"] is False and body["gate"]["semantics"] == "assert_no_regressions"
        )
        regressed = {(r["case"], r["scorer"]) for r in body["regressions"]}
        assert ("delhi", "tool_trajectory") in regressed
        # Server verdict equals the SDK's own assert_no_regressions on the stored reports.
        sdk_base = EvalReport.from_json(json.dumps(base["report"]))
        sdk_cand = EvalReport.from_json(json.dumps(cand["report"]))
        with pytest.raises(EvalRegressionError):
            assert_no_regressions(sdk_cand, sdk_base)
        # Reverse direction: improvements only, gate passes.
        back = (
            await c.post(
                f"{BASE}/compare",
                json={"baseline_run_id": cand["run_id"], "candidate_run_id": base["run_id"]},
            )
        ).json()
        assert back["gate"]["passed"] and back["improvements"] and not back["regressions"]
        # tolerance above the drop swallows score regressions, matching the SDK.
        loose = (
            await c.post(
                f"{BASE}/compare",
                json={
                    "baseline_run_id": base["run_id"],
                    "candidate_run_id": cand["run_id"],
                    "tolerance": 1.0,
                },
            )
        ).json()
        assert sdk_cand.compare(sdk_base, tolerance=1.0).has_regressions == bool(
            loose["regressions"]
        )
        # min_pass_rate adds a threshold on top of "no regressions".
        same = (
            await c.post(
                f"{BASE}/compare",
                json={
                    "baseline_run_id": base["run_id"],
                    "candidate_run_id": base["run_id"],
                    "min_pass_rate": 1.0,
                },
            )
        ).json()
        assert not same["gate"]["passed"] or base["summary"]["pass_rate"] == 1.0
        bad = await c.post(
            f"{BASE}/compare",
            json={
                "baseline_run_id": base["run_id"],
                "candidate_run_id": base["run_id"],
                "tolerance": 2,
            },
        )
        assert bad.status_code == 422


@pytest.mark.asyncio
async def test_agent_versions_use_sdk_evaluate_with_fake_models(app: Any) -> None:
    cases = [{"name": "q", "input": "refund?", "expected_output": "refund in 30 days"}]
    async with _client(app) as c:
        await _dataset(c, "ds1", cases)
        v1 = await _run(c, {"kind": "agent", "id": "answerer", "version": "v1"})
        v2 = await _run(c, {"kind": "agent", "id": "answerer", "version": "v2"})
        assert v1["summary"]["pass_rate"] == 0.0 and v2["summary"]["pass_rate"] == 1.0
        assert v2["target"]["version"] == "v2"
        gate = (
            await c.post(
                f"{BASE}/compare",
                json={"baseline_run_id": v1["run_id"], "candidate_run_id": v2["run_id"]},
            )
        ).json()
        assert gate["gate"]["passed"] and gate["improvements"]
        local = await _run(c, {"kind": "agent", "id": "local-fake"})
        assert local["target"]["synthetic"] is True
        assert local["report"]["results"][0]["output_text"] == "echo: refund?"


@pytest.mark.asyncio
async def test_secrets_are_redacted_in_stored_inputs_and_outputs(app: Any) -> None:
    secret = "sk-abcdef1234567890abcdef"
    cases = [{"name": "leak", "input": f"my key is {secret}", "expected_output": "x"}]
    async with _client(app) as c:
        saved = await _dataset(c, "ds1", cases)
        assert saved.json()["redactions"] == 1
        assert secret not in json.dumps((await c.get(f"{BASE}/datasets/ds1")).json())
        run = await _run(c, {"kind": "agent", "id": "local-fake"})
        assert secret not in json.dumps(run)
        raw = json.dumps((await c.get(f"{BASE}/runs/{run['run_id']}")).json())
        assert secret not in raw and "[REDACTED]" in raw


@pytest.mark.asyncio
async def test_cross_workspace_access_denied(app: Any) -> None:
    async with _client(app, "ws-a") as a, _client(app, "ws-b") as b:
        await _dataset(a)
        run = await _run(a, {"kind": "agent", "id": "local-fake"})
        assert (await b.get(f"{BASE}/datasets")).json()["datasets"] == []
        assert (await b.get(f"{BASE}/datasets/ds1")).status_code == 404
        assert (await b.get(f"{BASE}/datasets/ds1/export")).status_code == 404
        assert (await b.delete(f"{BASE}/datasets/ds1")).status_code == 404
        assert (await b.get(f"{BASE}/runs/{run['run_id']}")).status_code == 404
        assert (await b.delete(f"{BASE}/runs/{run['run_id']}")).status_code == 404
        res = await b.post(
            f"{BASE}/compare",
            json={"baseline_run_id": run["run_id"], "candidate_run_id": run["run_id"]},
        )
        assert res.status_code == 404
        res = await b.post(
            f"{BASE}/runs",
            json={"dataset_id": "ds1", "target": {"kind": "agent", "id": "local-fake"}},
        )
        assert res.status_code == 404
        # Still intact for the owner.
        assert (await a.get(f"{BASE}/datasets/ds1")).status_code == 200


@pytest.mark.asyncio
async def test_workflow_of_another_workspace_cannot_be_a_target(app: Any) -> None:
    async with _client(app, "ws-a") as a, _client(app, "ws-b") as b:
        await _workflow(a, "wf-a", TWO_STEP)
        await _dataset(b)
        res = await b.post(
            f"{BASE}/runs", json={"dataset_id": "ds1", "target": {"kind": "workflow", "id": "wf-a"}}
        )
        assert res.status_code == 404 and res.json()["detail"]["code"] == "WORKFLOW_NOT_FOUND"
        listed = (await b.get(f"{BASE}/targets")).json()["targets"]
        assert all(t["id"] != "wf-a" for t in listed)


@pytest.mark.asyncio
async def test_run_negative_cases(app: Any) -> None:
    async with _client(app) as c:
        await _dataset(c)
        for target, code in [
            ({"kind": "agent", "id": "nope"}, "AGENT_NOT_FOUND"),
            ({"kind": "workflow", "id": "nope"}, "WORKFLOW_NOT_FOUND"),
            ({"kind": "shell", "id": "ls"}, "INVALID_TARGET"),
            ({"kind": "agent"}, "INVALID_TARGET"),
        ]:
            res = await c.post(f"{BASE}/runs", json={"dataset_id": "ds1", "target": target})
            assert res.json()["detail"]["code"] == code, res.text
        res = await c.post(
            f"{BASE}/runs",
            json={"dataset_id": "missing", "target": {"kind": "agent", "id": "local-fake"}},
        )
        assert res.status_code == 404
        for scorers, code in [
            ([{"type": "eval_code", "code": "1"}], "INVALID_SCORER"),
            ([{"type": "llm_judge", "rubric": "be nice"}], "JUDGE_UNAVAILABLE"),
            ([{"type": "regex", "pattern": "("}], "INVALID_SCORER"),
        ]:
            res = await c.post(
                f"{BASE}/runs",
                json={
                    "dataset_id": "ds1",
                    "target": {"kind": "agent", "id": "local-fake"},
                    "scorers": scorers,
                },
            )
            assert res.status_code == 422 and res.json()["detail"]["code"] == code, res.text
        res = await c.post(
            f"{BASE}/runs",
            json={
                "dataset_id": "ds1",
                "target": {"kind": "agent", "id": "local-fake"},
                "repeats": 99,
            },
        )
        assert res.status_code == 422


@pytest.mark.asyncio
async def test_failed_workflow_case_is_an_error_not_a_crash(app: Any) -> None:
    broken = {
        "name": "t",
        "entry_node": "a",
        "nodes": {"a": {"id": "a", "handler": "synthetic_order_lookup"}},
        "terminal_nodes": ["a"],
    }
    async with _client(app) as c:
        await _workflow(c, "wf", broken)
        # An order_id of a list type makes the handler raise; the run still finishes.
        await _dataset(
            c, "ds1", [{"name": "boom", "input": {"order_id": [1]}, "expected_output": "x"}]
        )
        run = await _run(c, {"kind": "workflow", "id": "wf"})
        assert run["report"]["results"][0]["status"] in ("error", "failed")


def test_scorer_wrapper_uses_the_right_expectation() -> None:
    scorers = studio_engine.build_scorers(
        [{"type": "exact_match"}, {"type": "tool_trajectory"}], None
    )
    assert [s.name for s in scorers] == ["exact_match", "tool_trajectory"]  # type: ignore[attr-defined]
