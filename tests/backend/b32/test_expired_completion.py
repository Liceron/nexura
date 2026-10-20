"""A handler cannot publish a successful result after losing its execution lease."""

import asyncio
from pathlib import Path
from typing import Any

import pytest

from nexura_runtime.runtime.execution import (
    DurableExecutionEngine,
    DurableExecutionError,
    DurableNodeSpec,
    DurableWorkflowSpec,
    LeaseExpiredError,
)
from nexura_runtime.storage import SQLiteRepository


@pytest.mark.asyncio
async def test_expired_lease_cannot_commit_handler_completion(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "lease.sqlite3")
    engine = DurableExecutionEngine(repository)

    async def slow(state: dict[str, Any]) -> dict[str, Any]:
        await asyncio.sleep(0.06)
        return {"done": True}

    engine.register_handler("slow", slow)
    spec = DurableWorkflowSpec(
        workflow_id="expired",
        entry_node="slow",
        terminal_nodes=["slow"],
        nodes={"slow": DurableNodeSpec(node_id="slow", handler="slow")},
    )
    await engine.init_run("test", "expired", spec)
    await engine.claim_run("test", "expired", "worker", lease_duration=0.03)
    with pytest.raises(LeaseExpiredError):
        await engine.execute_step("test", "expired", "worker", spec)
    stored = await repository.get_run("test", "expired")
    assert stored is not None and stored.status != "completed"
    assert stored.state["pre_dispatch_intent"] is not None
    assert not stored.state["state"].get("done")


@pytest.mark.asyncio
async def test_changed_workflow_handler_rejected_before_dispatch(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "spec.sqlite3")
    engine = DurableExecutionEngine(repository)
    effects: list[str] = []
    engine.register_handler("original", lambda state: {})
    engine.register_handler("changed", lambda state: effects.append("changed"))
    original = DurableWorkflowSpec(
        workflow_id="fixed",
        entry_node="action",
        terminal_nodes=["action"],
        nodes={"action": DurableNodeSpec(node_id="action", handler="original")},
    )
    await engine.init_run("test", "fixed", original)
    await engine.claim_run("test", "fixed", "worker")
    changed = original.model_copy(deep=True)
    changed.nodes["action"].handler = "changed"
    with pytest.raises(DurableExecutionError, match="specification differs"):
        await engine.execute_step("test", "fixed", "worker", changed)
    assert effects == []
