"""P38 C5: offline trajectory, simulation, replay, and persisted API contracts."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest
from _service_state import service_evaluation_store
from httpx import ASGITransport, AsyncClient
from nexura_agents import Agent, FakeModel, ToolCall, tool
from nexura_agents.events import RunEvent, RunEventType
from nexura_agents.types import Usage
from pydantic import ValidationError

from nexura_runtime.evaluation import (
    ArgumentMatch,
    DatasetExample,
    DatasetRegistry,
    EvaluationDataset,
    EvaluationRunner,
    ExactMatchGrader,
    OnlineEvalConfig,
    StoredTrace,
    ToolArgumentAccuracyGrader,
    TrajectoryGrader,
    TrajectoryStep,
    UserSimulation,
    recorded_tool_calls,
    simulate_user,
)
from nexura_runtime.evaluation.store import SqliteEvaluationStore
from nexura_runtime.service.api.app import create_api_app

BASE = "/api/v1/evaluations"


def trace(*names: str, args: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "run_id": "r",
        "events": [
            {
                "type": "tool_started",
                "run_id": "r",
                "data": {
                    "tool_call_id": str(i),
                    "tool_name": name,
                    "arguments": args[i] if args else {},
                },
            }
            for i, name in enumerate(names)
        ],
    }


@pytest.mark.parametrize(
    ("mode", "actual", "passed", "score"),
    [
        ("exact", ["a", "b"], True, 1),
        ("strict", ["a", "b"], True, 1),
        ("exact", ["b", "a"], False, 0),
        ("exact", ["a"], False, 0.5),
        ("exact", ["a", "b", "c"], False, 2 / 3),
        ("subset", ["x", "a", "x", "b"], True, 1),
        ("in_order", ["a", "x", "b"], True, 1),
        ("subset", ["b", "a"], False, 0.5),
        ("any_order", ["b", "a"], True, 1),
        ("any_order", ["a", "a"], False, 0.5),
        ("any_order", ["b", "a", "c"], False, 2 / 3),
    ],
)
async def test_eval_trajectory_modes(
    mode: Any, actual: list[str], passed: bool, score: float
) -> None:
    result = await TrajectoryGrader(mode=mode).grade(
        DatasetExample(input_data={}, expected_trajectory=["a", "b"]), trace(*actual)
    )
    assert result.passed is passed
    assert result.score == pytest.approx(score)


@pytest.mark.parametrize("mode", ["exact", "subset", "any_order"])
async def test_eval_empty_and_missing_expectation(mode: Any) -> None:
    grader = TrajectoryGrader(mode=mode)
    assert (
        await grader.grade(DatasetExample(input_data={}, expected_trajectory=[]), trace())
    ).passed
    assert not (await grader.grade(DatasetExample(input_data={}), trace())).passed
    extra = await grader.grade(DatasetExample(input_data={}, expected_trajectory=[]), trace("x"))
    assert extra.passed is (mode == "subset")


async def test_eval_argument_rules_partial_missing_and_tolerance() -> None:
    example = DatasetExample(
        input_data={},
        expected_trajectory=[
            TrajectoryStep(
                name="lookup", args={"q": "  New  YORK ", "price": 100, "nested": {"ok": True}}
            )
        ],
    )
    grader = ToolArgumentAccuracyGrader(
        argument_rules={
            "lookup.q": ArgumentMatch(normalisers=["strip", "casefold", "whitespace"]),
            "price": {"normalisers": ["numeric"], "relative_tolerance": 0.01},
        }
    )
    observed = trace(
        "lookup", args=[{"q": "new york", "price": "100.5", "nested": {"ok": True}, "extra": 1}]
    )
    assert (await grader.grade(example, observed)).passed
    observed["events"][0]["data"]["arguments"]["nested"]["ok"] = 1
    partial = await grader.grade(example, observed)
    assert not partial.passed and partial.score == pytest.approx(2 / 3)
    assert partial.details["matches"][0]["arguments"]["nested.ok"] is False
    missing = await grader.grade(example, trace("lookup", args=[{"q": "new york"}]))
    assert missing.score == pytest.approx(1 / 3)
    absolute = ToolArgumentAccuracyGrader(argument_rules={"price": {"absolute_tolerance": 0.6}})
    assert (
        await absolute.grade(
            DatasetExample(
                input_data={}, expected_trajectory=[TrajectoryStep(name="x", args={"price": 100})]
            ),
            trace("x", args=[{"price": 100.5}]),
        )
    ).passed


async def test_eval_any_order_duplicate_assignment_and_subsequence() -> None:
    example = DatasetExample(
        input_data={},
        expected_trajectory=[
            TrajectoryStep(name="x", args={"a": 1}),
            TrajectoryStep(name="x", args={"a": 1, "b": 2}),
        ],
    )
    observed = trace("x", "x", args=[{"a": 1, "b": 2}, {"a": 1, "b": 3}])
    assert (await ToolArgumentAccuracyGrader(mode="any_order").grade(example, observed)).passed
    in_order = await ToolArgumentAccuracyGrader(mode="subset").grade(example, observed)
    assert not in_order.passed
    assert in_order.score == 0.75


async def test_eval_reads_real_run_events_messages_and_deduplicates() -> None:
    @tool
    def lookup(q: str) -> str:
        """Local fixture lookup."""
        return q

    agent = Agent(
        name="local fixture",
        instructions="test",
        tools=[lookup],
        model=FakeModel([[ToolCall(id="call1", name="lookup", arguments={"q": "hello"})], "ok"]),
    )
    run = await agent.run_async("hello")
    assert [c.name for c in recorded_tool_calls(run)] == ["lookup"]
    event = RunEvent(
        type=RunEventType.TOOL_STARTED,
        run_id="r",
        data={"tool_call_id": "c", "tool_name": "lookup", "arguments": {"q": "hello"}},
    )
    assert recorded_tool_calls([event])[0].args == {"q": "hello"}
    assert recorded_tool_calls([dataclasses.asdict(event)])[0].name == "lookup"
    example = DatasetExample(input_data={}, expected_output="ok", expected_trajectory=["lookup"])
    summary = await EvaluationRunner([TrajectoryGrader()]).run(
        lambda _: run, EvaluationDataset(dataset_id="d", version="1", examples=[example])
    )
    assert summary.pass_rate == 1 and summary.grader_scores == {"tool_trajectory": 1}
    assert summary.results[0].metadata["trace"]["events"]


async def test_eval_invalid_trace_and_configuration() -> None:
    with pytest.raises(ValueError, match="mode"):
        TrajectoryGrader(mode="broken")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        ArgumentMatch(normalisers=["execute"])  # type: ignore[list-item]
    with pytest.raises(ValidationError):
        ArgumentMatch(absolute_tolerance=-1)
    result = await TrajectoryGrader().grade(
        DatasetExample(input_data={}, expected_trajectory=["x"]),
        trace("x", args=["invalid"]),  # type: ignore[list-item]
    )
    assert not result.passed and result.score == 0


def fixture_agent(responses: list[str] | None = None) -> Agent[Any]:
    return Agent(
        name="fixture",
        instructions="offline",
        model=FakeModel(
            responses or ["first", "second", "third"],
            default_usage=Usage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        ),
    )


@pytest.mark.parametrize(
    ("plan", "stop_reason", "count"),
    [
        (UserSimulation(turns=["one", "two"]), "script_exhausted", 2),
        (UserSimulation(turns=["one", "two"], max_turns=1), "max_turns", 1),
        (UserSimulation(turns=[]), "script_exhausted", 0),
        (UserSimulation(turns=["[DONE]", "one"]), "user_stop", 0),
        (UserSimulation(turns=["", "one"]), "empty_user", 0),
        (UserSimulation(turns=["one", "two"], stop_on_agent=["first"]), "agent_stop", 1),
    ],
)
async def test_eval_simulation_script_stops(
    plan: UserSimulation, stop_reason: str, count: int
) -> None:
    result = await simulate_user(fixture_agent(), plan)
    assert result["stop_reason"] == stop_reason
    assert result["turns"] == count and len(result["transcript"]) == 2 * count


async def test_eval_simulation_persona_determinism_history_and_runner() -> None:
    user = FakeModel(["hello", "more", "[DONE]"])
    agent_model = FakeModel(["first", "second"])
    agent = Agent(name="fixture", instructions="offline", model=agent_model)
    plan = UserSimulation(persona="A curious user")
    result = await simulate_user(agent, plan, model=user)
    again = await simulate_user(fixture_agent(), plan, model=FakeModel(["hello", "more", "[DONE]"]))
    assert result["transcript"] == again["transcript"]
    assert result["turns"] == 2 and result["stop_reason"] == "user_stop"
    assert len([m for m in agent_model.calls[1]["messages"] if m.role == "user"]) == 2
    assert user.calls[1]["messages"][-1].content == "first"
    assert user.calls[1]["messages"][-1].role == "user"
    assert user.calls[0]["tools"] is None
    summary = await EvaluationRunner(
        [ExactMatchGrader()], user_model=FakeModel(["hello", "[DONE]"])
    ).run(
        fixture_agent(),
        EvaluationDataset(
            dataset_id="s",
            version="1",
            examples=[DatasetExample(input_data={}, simulation=plan, expected_output="first")],
        ),
    )
    assert summary.pass_rate == 1
    assert summary.results[0].metadata["stop_reason"] == "user_stop"


async def test_eval_simulation_callback_missing_model_and_noncompleted() -> None:
    assert (
        await simulate_user(
            fixture_agent(),
            UserSimulation(turns=["one", "two"]),
            stop=lambda messages: len(messages) == 2,
        )
    )["stop_reason"] == "callback"
    with pytest.raises(ValueError, match="injected"):
        await simulate_user(fixture_agent(), UserSimulation(persona="user"))
    with pytest.raises(ValidationError):
        UserSimulation(turns=["x"], persona="x")
    with pytest.raises(ValidationError):
        UserSimulation(turns=["x"], max_turns=0)

    @tool(requires_approval=True)
    def pending() -> str:
        """Never executed by this evaluation."""
        raise AssertionError("must not run")

    agent = Agent(
        name="fixture",
        instructions="offline",
        tools=[pending],
        model=FakeModel([[ToolCall(id="p", name="pending", arguments={})]]),
    )
    summary = await EvaluationRunner([TrajectoryGrader()]).run(
        agent,
        EvaluationDataset(
            dataset_id="s",
            version="1",
            examples=[
                DatasetExample(
                    input_data={},
                    simulation=UserSimulation(turns=["one"]),
                    expected_trajectory=["pending"],
                )
            ],
        ),
    )
    assert summary.pass_rate == 0
    assert summary.results[0].metadata["stop_reason"] == "agent_paused"


async def test_eval_simulation_multi_turn_tool_calls_once() -> None:
    @tool
    def echo(text: str) -> str:
        """Offline echo."""
        return text

    agent = Agent(
        name="fixture",
        instructions="offline",
        tools=[echo],
        model=FakeModel(
            [
                [ToolCall(id="same", name="echo", arguments={"text": "1"})],
                "first",
                [ToolCall(id="same", name="echo", arguments={"text": "2"})],
                "second",
            ]
        ),
    )
    result = await simulate_user(agent, UserSimulation(turns=["one", "two"]))
    assert [s.args for s in recorded_tool_calls(result["trace"])] == [{"text": "1"}, {"text": "2"}]


async def test_eval_online_filters_sampling_and_no_execution(tmp_path: Path) -> None:
    store = SqliteEvaluationStore(str(tmp_path / "eval.sqlite"))
    for i in range(20):
        store.save_trace(
            StoredTrace(
                run_id=f"run{i}",
                timestamp=i,
                workflow_id="wf",
                agent_id="a",
                output="ok",
                events=trace("x")["events"],
            )
        )
    store.save_trace(StoredTrace(run_id="other", timestamp=10, workflow_id="other", agent_id="b"))
    store.save_trace(StoredTrace(run_id="private", timestamp=10), "private")
    runner = EvaluationRunner([TrajectoryGrader()])
    expected = DatasetExample(
        input_data={}, expected_trajectory=["x"], simulation=UserSimulation(turns=["never"])
    )
    config = OnlineEvalConfig(workflow_id="wf", agent_id="a", start_time=5, end_time=15)
    all_runs = await runner.evaluate_traces(store.list_traces(), expected, config=config)
    assert all_runs.total_examples == 11 and all_runs.pass_rate == 1
    none = await runner.evaluate_traces(
        store.list_traces(), expected, config=OnlineEvalConfig(sampling_rate=0)
    )
    assert none.total_examples == 0 and none.pass_rate == 0
    sample = config.model_copy(update={"sampling_rate": 0.5})
    a = await runner.evaluate_traces(store.list_traces(), expected, config=sample)
    b = await runner.evaluate_traces(reversed(store.list_traces()), expected, config=sample)
    assert 0 < a.total_examples < 11
    assert a.metadata["source_run_ids"] == b.metadata["source_run_ids"]
    assert (
        await runner.evaluate_traces(
            store.list_traces(), expected, config=OnlineEvalConfig(agent_id="absent")
        )
    ).total_examples == 0
    assert all_runs.total_tokens == 0
    store.save_run(all_runs)
    store.close()
    reopened = SqliteEvaluationStore(str(tmp_path / "eval.sqlite"))
    assert reopened.get_run(all_runs.run_id) == all_runs
    assert len(reopened.list_traces()) == 21
    assert reopened.get_run(all_runs.run_id, "private") is None
    assert len(reopened.list_traces("private")) == 1
    reopened.close()
    with pytest.raises(ValidationError):
        OnlineEvalConfig(start_time=2, end_time=1)
    with pytest.raises(ValidationError):
        OnlineEvalConfig(sampling_rate=1.1)


async def test_eval_dataset_roundtrip_and_redaction(tmp_path: Path) -> None:
    dataset = EvaluationDataset(
        dataset_id="d",
        version="1",
        examples=[
            DatasetExample(
                input_data={"x": 1},
                simulation=UserSimulation(turns=["hello"]),
                expected_trajectory=[TrajectoryStep(name="x", args={"email": "hello@example.com"})],
            )
        ],
    )
    registry = DatasetRegistry(tmp_path)
    reloaded = registry.load_from_json(registry.save(dataset))
    assert reloaded.examples == dataset.examples and reloaded.content_hash == dataset.content_hash
    summary = await EvaluationRunner([ToolArgumentAccuracyGrader()]).evaluate_traces(
        [
            StoredTrace(
                run_id="secret",
                timestamp=1,
                events=trace("x", args=[{"email": "hello@example.com"}])["events"],
            )
        ],
        dataset.examples[0],
    )
    assert summary.pass_rate == 1
    assert "hello@example.com" not in summary.model_dump_json()


async def test_eval_api_roundtrip_simulation_online_and_persistence(tmp_path: Path) -> None:
    application = create_api_app(db_path=str(tmp_path / "api.sqlite"), profile="local-owner")
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        capabilities = (await client.get(f"{BASE}/graders")).json()
        assert "tool_argument_accuracy" in capabilities["types"]
        payload = {
            "run_id": "imported",
            "timestamp": 10,
            "workflow_id": "wf",
            "agent_id": "a",
            "output": "ok",
            "events": trace("x", args=[{"q": " HI "}])["events"],
        }
        assert (await client.post(f"{BASE}/traces", json=payload)).status_code == 200
        request = {
            "example": {
                "input_data": {},
                "expected_trajectory": [{"name": "x", "args": {"q": "hi"}}],
            },
            "config": {"workflow_id": "wf", "agent_id": "a", "start_time": 10, "end_time": 10},
            "graders": [
                {
                    "type": "tool_argument_accuracy",
                    "mode": "any_order",
                    "argument_rules": {"q": {"normalisers": ["strip", "casefold"]}},
                }
            ],
        }
        response = await client.post(f"{BASE}/online", json=request)
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["pass_rate"] == 1 and result["total_examples"] == 1
        assert (await client.get(f"{BASE}/runs/{result['run_id']}")).json() == result
        assert result["run_id"] in [
            r["run_id"] for r in (await client.get(f"{BASE}/runs")).json()["runs"]
        ]
        assert (
            await client.post(f"{BASE}/online", json={**request, "config": {"sampling_rate": -1}})
        ).status_code == 422
        assert (
            await client.post(f"{BASE}/online", json={**request, "graders": [{"type": "bad"}]})
        ).status_code == 422
        created = await client.post(
            f"{BASE}/datasets",
            json={
                "dataset_id": "simulation",
                "examples": [
                    {
                        "input_data": {"q": "hello"},
                        "expected_output": "echo: two",
                        "simulation": {"turns": ["one", "two"]},
                        "expected_trajectory": [],
                    }
                ],
            },
        )
        assert created.status_code == 200, created.text
        stored = (await client.get(f"{BASE}/datasets/simulation")).json()
        assert stored["examples"][0]["simulation"]["turns"] == ["one", "two"]
        simulated = await client.post(f"{BASE}/simulate", json={"dataset_id": "simulation"})
        assert simulated.status_code == 200, simulated.text
        assert simulated.json()["pass_rate"] == 1
        metadata = simulated.json()["results"][0]["metadata"]
        assert metadata["stop_reason"] == "script_exhausted" and metadata["turns"] == 2
        assert len(metadata["transcript"]) == 4
        invalid = await client.post(
            f"{BASE}/datasets",
            json={
                "dataset_id": "bad",
                "examples": [
                    {"input_data": {"x": 1}, "simulation": {"turns": ["one"], "persona": "bad"}}
                ],
            },
        )
        assert invalid.status_code == 422
        application.state.eval_store.close()
        # The service's evaluation store as another process opens it (Postgres with a URL).
        reopened = service_evaluation_store(tmp_path / "api.sqlite")
        assert reopened.get_run(result["run_id"]) is not None
        assert len(reopened.list_traces()) == 1
        reopened.close()


async def test_eval_studio_argument_scorer_roundtrip(tmp_path: Path) -> None:
    application = create_api_app(db_path=str(tmp_path / "studio.sqlite"), profile="local-owner")

    @tool
    def lookup(q: str) -> str:
        """Offline tool."""
        return q

    application.state.eval_agents = {
        "fixture": lambda _: Agent(
            name="fixture",
            instructions="offline",
            tools=[lookup],
            model=FakeModel([[ToolCall(id="1", name="lookup", arguments={"q": " Hi "})], "ok"]),
        )
    }
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        response = await client.put(
            f"{BASE}/studio/datasets/args",
            json={
                "cases": [
                    {
                        "name": "one",
                        "input": "hello",
                        "expected_trajectory": [{"name": "lookup", "args": {"q": "hi"}}],
                    }
                ]
            },
        )
        assert response.status_code == 200, response.text
        response = await client.post(
            f"{BASE}/studio/runs",
            json={
                "dataset_id": "args",
                "target": {"kind": "agent", "id": "fixture"},
                "scorers": [
                    {
                        "type": "tool_argument_accuracy",
                        "mode": "any_order",
                        "argument_rules": {"q": {"normalisers": ["strip", "casefold"]}},
                    }
                ],
            },
        )
        assert response.status_code == 200, response.text
        assert '"tool_argument_accuracy"' in json.dumps(response.json())
        assert response.json()["summary"]["pass_rate"] == 1


async def test_eval_online_api_workspace_runs_and_imports(tmp_path: Path) -> None:
    from nexura_runtime.workflow import WorkflowNode, WorkflowSpec

    application = create_api_app(db_path=str(tmp_path / "ownership.sqlite"), profile="local-owner")
    spec = WorkflowSpec(
        name="fixture",
        entry_node="x",
        nodes={"x": WorkflowNode(id="x", handler="synthetic_order_lookup")},
        terminal_nodes=["x"],
    )
    store = application.state.store
    repo = application.state.workspace_repo
    # Real stored workflow runs, including legacy default-workspace and in-flight rows.
    for run_id, workspace, completed in [
        ("legacy", None, True),
        ("own", "a", True),
        ("foreign", "b", True),
        ("busy", "a", False),
    ]:
        await store.record_run_start(run_id, "wf", spec, {})
        await store.record_run_event(
            run_id, "tool_started", data={"tool_call_id": "c", "tool_name": "x", "arguments": {}}
        )
        if completed:
            await store.update_run(run_id, "completed", None, {"answer": "ok"}, 1)
        if workspace:
            repo.record_resource_ownership("run", run_id, workspace, "owner")
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        request = {"example": {"input_data": {}, "expected_trajectory": ["x"]}}
        for workspace, expected in [("default", ["legacy"]), ("a", ["own"]), ("b", ["foreign"])]:
            response = await client.post(
                f"{BASE}/online", json=request, headers={"X-Workspace-ID": workspace}
            )
            assert response.status_code == 200, response.text
            assert response.json()["metadata"]["source_run_ids"] == expected
            assert response.json()["pass_rate"] == 1
        payload = {"run_id": "import", "timestamp": 1, "events": trace("x")["events"]}
        assert (
            await client.post(f"{BASE}/traces", json=payload, headers={"X-Workspace-ID": "a"})
        ).status_code == 200
        a = (
            await client.post(f"{BASE}/online", json=request, headers={"X-Workspace-ID": "a"})
        ).json()
        b = (
            await client.post(f"{BASE}/online", json=request, headers={"X-Workspace-ID": "b"})
        ).json()
        assert a["metadata"]["source_run_ids"] == ["import", "own"]
        assert b["metadata"]["source_run_ids"] == ["foreign"]
        assert (
            await client.get(f"{BASE}/runs/{a['run_id']}", headers={"X-Workspace-ID": "b"})
        ).status_code == 404


async def test_eval_persona_api_and_user_tool_refusal(tmp_path: Path) -> None:
    application = create_api_app(db_path=str(tmp_path / "persona.sqlite"), profile="local-owner")
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        response = await client.post(
            f"{BASE}/datasets",
            json={
                "dataset_id": "persona",
                "examples": [
                    {
                        "input_data": {"q": "help"},
                        "expected_output": "echo: hello",
                        "simulation": {"persona": "user"},
                    }
                ],
            },
        )
        assert response.status_code == 200
        unavailable = await client.post(f"{BASE}/simulate", json={"dataset_id": "persona"})
        assert unavailable.status_code == 422
        assert unavailable.json()["detail"]["code"] == "USER_MODEL_UNAVAILABLE"
        application.state.eval_user_model = FakeModel(["hello", "[DONE]"])
        response = await client.post(f"{BASE}/simulate", json={"dataset_id": "persona"})
        assert response.status_code == 200 and response.json()["pass_rate"] == 1
        assert response.json()["results"][0]["metadata"]["stop_reason"] == "user_stop"
        bad = await client.post(
            f"{BASE}/simulate", json={"dataset_id": "persona", "agent_id": "missing"}
        )
        assert bad.status_code == 404
    with pytest.raises(ValueError, match="not tool calls"):
        await simulate_user(
            fixture_agent(),
            UserSimulation(persona="user"),
            model=FakeModel([[ToolCall(id="x", name="danger", arguments={})]]),
        )


async def test_eval_online_per_trace_expectations_and_aggregate() -> None:
    traces = [
        StoredTrace(run_id="good", timestamp=1, output="yes", events=trace("a")["events"]),
        StoredTrace(run_id="bad", timestamp=2, output="no", events=trace("b")["events"]),
    ]
    example = DatasetExample(input_data={}, expected_trajectory=["a"])
    runner = EvaluationRunner([TrajectoryGrader()])
    result = await runner.evaluate_traces(traces, example)
    assert result.pass_rate == 0.5 and result.grader_scores["tool_trajectory"] == 0.5
    per_trace = await runner.evaluate_traces(
        traces,
        example,
        expectation_for=lambda t: DatasetExample(
            input_data={}, expected_trajectory=["a" if t.run_id == "good" else "b"]
        ),
    )
    assert per_trace.pass_rate == 1
