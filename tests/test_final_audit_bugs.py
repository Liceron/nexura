"""Local regression coverage for confirmed final-audit bugs B01–B05 and B07."""

import asyncio
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest
from nexura_runtime.errors import WorkflowError, WorkflowExecutionError

from nexura_runtime.cli.main import build_parser
from nexura_runtime.runtime.execution.engine import DurableExecutionEngine
from nexura_runtime.runtime.execution.models import DurableNodeSpec, DurableWorkflowSpec
from nexura_runtime.service.api.client import NexuraClient
from nexura_runtime.storage.repository import SQLiteRepository, get_storage_repository
from nexura_runtime.workflow import (
    MemoryWorkflowStore,
    ModuleRegistry,
    SQLiteWorkflowStore,
    WorkflowEngine,
    WorkflowNode,
    WorkflowSpec,
    approved_request,
    interrupt,
    request_approval,
)


@pytest.fixture(params=["memory", "sqlite"])
async def checkpoint_store(request: Any, tmp_path: Path) -> Any:
    store = (
        MemoryWorkflowStore()
        if request.param == "memory"
        else SQLiteWorkflowStore(tmp_path / "checkpoints.db")
    )
    try:
        yield store
    finally:
        await store.close()


async def test_concurrent_run_id_dispatches_only_once(checkpoint_store: Any) -> None:
    entered = asyncio.Event()
    finish = asyncio.Event()
    calls = []

    async def handler(state: dict[str, Any]) -> dict[str, Any]:
        calls.append(1)
        entered.set()
        await finish.wait()
        return {"done": True}

    registry = ModuleRegistry()
    registry.register("h", handler)
    spec = WorkflowSpec(name="duplicate", entry_node="n", nodes={"n": WorkflowNode("n", "h")})
    first_engine = WorkflowEngine(spec, registry, store=checkpoint_store)
    competing_store = (
        SQLiteWorkflowStore(checkpoint_store.db_path)
        if isinstance(checkpoint_store, SQLiteWorkflowStore)
        else checkpoint_store
    )
    second_engine = WorkflowEngine(spec, registry, store=competing_store)
    first = asyncio.create_task(first_engine.run(workflow_run_id="same"))
    second = None
    try:
        await asyncio.wait_for(entered.wait(), 1)
        second = asyncio.create_task(second_engine.run(workflow_run_id="same"))
        # The second start must be rejected even while the first handler is blocked.
        with pytest.raises(WorkflowError, match="already exists"):
            await asyncio.wait_for(second, 1)
        finish.set()
        result = await asyncio.wait_for(first, 1)
        assert result.status == "completed"
    finally:
        finish.set()
        await asyncio.gather(first, *([second] if second else []), return_exceptions=True)
        if competing_store is not checkpoint_store:
            await competing_store.close()
    assert len(calls) == 1


async def test_conditional_cycle_reexecutes_node(tmp_path: Path) -> None:
    async def loop(state: dict[str, Any]) -> dict[str, Any]:
        count = state.get("count", 0) + 1
        return {"count": count, "loop_branch": "done" if count == 2 else "again"}

    async def end(state: dict[str, Any]) -> dict[str, Any]:
        return {}

    spec = DurableWorkflowSpec(
        workflow_id="cycle",
        entry_node="loop",
        terminal_nodes=["end"],
        nodes={
            "loop": DurableNodeSpec(node_id="loop", handler="loop"),
            "end": DurableNodeSpec(node_id="end", handler="end"),
        },
        transitions={"loop": {"again": "loop", "done": "end"}},
    )
    repo = SQLiteRepository(tmp_path / "cycle.db")
    try:
        engine = DurableExecutionEngine(repo, {"loop": loop, "end": end})
        await engine.init_run("default", "r", spec)
        # A fresh engine must recognize a new visit after the first completion commits.
        first = await engine.run_until_idle("default", "r", "worker", spec, max_steps=1)
        assert first.state["count"] == 1
        result = await DurableExecutionEngine(repo, {"loop": loop, "end": end}).run_until_idle(
            "default", "r", "worker", spec, max_steps=5
        )
        assert (result.status, result.state["count"]) == ("completed", 2)
        assert result.fuel_consumed == 3
        assert result.completed_nodes == ["loop", "loop", "end"]
    finally:
        await repo.close()


async def test_node_grant_survives_input_pause(checkpoint_store: Any) -> None:
    async def handler(state: dict[str, Any]) -> dict[str, Any]:
        if approved_request() is None:
            request_approval({"action": "calculate"})
        return {"answer": interrupt("Which value?"), "second": interrupt("And then?")}

    registry = ModuleRegistry()
    registry.register("h", handler)
    spec = WorkflowSpec(name="both", entry_node="n", nodes={"n": WorkflowNode("n", "h")})
    engine = WorkflowEngine(spec, registry, store=checkpoint_store)
    gated = await engine.run()
    assert gated.pending_approval is not None
    asked = await engine.resume(gated.workflow_run_id, gated.pending_approval.approval_id)
    assert asked.interrupt is not None
    # Restore the grant through the persisted checkpoint, including repeated questions.
    engine = WorkflowEngine(spec, registry, store=checkpoint_store)
    asked_again = await engine.resume_interrupt(
        asked.workflow_run_id, asked.interrupt["interrupt_id"], 42
    )
    assert asked_again.interrupt is not None
    done = await engine.resume_interrupt(
        asked_again.workflow_run_id, asked_again.interrupt["interrupt_id"], 43
    )
    assert done.status == "completed"
    assert done.state == {"answer": 42, "second": 43}


@pytest.mark.parametrize("uri", [":memory:", "sqlite://:memory:"])
async def test_supported_memory_repository_round_trips(uri: str) -> None:
    repo = get_storage_repository(uri)
    other = get_storage_repository(uri)
    try:
        await repo.create_run("default", "r", initial_state={"answer": 42})
        record = await repo.get_run("default", "r")
        assert record is not None and record.state == {"answer": 42}
        assert await other.get_run("default", "r") is None
        updated = await repo.update_run(
            "default", "r", record.revision, "completed", {"answer": 43}, [{"type": "done"}]
        )
        assert updated.state == {"answer": 43}
        assert len(await repo.get_run_events("default", "r")) == 1
    finally:
        await repo.close()
        await other.close()


@pytest.mark.parametrize("later_node", [False, True])
async def test_invalid_branch_is_persisted_as_failed(
    checkpoint_store: Any, later_node: bool
) -> None:
    calls = []

    async def handler(state: dict[str, Any]) -> dict[str, Any]:
        calls.append(1)
        return {"_branch": "missing"}

    async def before(state: dict[str, Any]) -> dict[str, Any]:
        return {"before": True}

    registry = ModuleRegistry()
    registry.register("h", handler)
    registry.register("before", before)
    nodes = {"n": WorkflowNode("n", "h", retry={"max_attempts": 2, "initial_delay": 0})}
    if later_node:
        nodes["before"] = WorkflowNode("before", "before", default_transition="n")
    spec = WorkflowSpec(name="branch", entry_node="before" if later_node else "n", nodes=nodes)
    engine = WorkflowEngine(spec, registry, store=checkpoint_store)
    events = []
    with pytest.raises(WorkflowExecutionError, match="no such transition"):
        async for event in engine.stream(workflow_run_id="bad"):
            events.append((event.type.value, event.data.get("node_id")))
    assert ("node_failed", "n") in events
    assert ("node_completed", "n") not in events
    saved = await checkpoint_store.get("bad")
    assert saved is not None and saved.status == "failed"
    assert saved.current_node_id == "n"
    assert saved.step_count == (2 if later_node else 1)
    with pytest.raises(WorkflowError):
        await engine.run(workflow_run_id="bad")
    assert len(calls) == 1


def test_default_client_port_matches_default_serve_port() -> None:
    serve = build_parser().parse_args(["serve"])
    assert urlparse(NexuraClient().base_url).port == serve.port
