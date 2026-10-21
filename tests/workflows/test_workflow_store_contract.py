"""A6 S8: one workflow-store contract, run against memory, SQLite and Postgres.

The same tests run on ``MemoryWorkflowStore``, ``SQLiteWorkflowStore`` and
``nexura_postgres.PostgresWorkflowStore`` (the Postgres parameter needs
``NEXURA_TEST_POSTGRES_URI``; each test gets its own schema). Covered: create-if-absent,
revision compare-and-swap, last-writer-wins saves, append-only history, carried-over
``forked_from`` / ``workspace_id``, ``claim_approval`` (single winner, expiry, interrupts),
``list_children`` order, ``list_incomplete``, the execution journal next to the
checkpoints, and a WorkflowEngine run, pause, crash and recover on each store.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from nexura_agents.contracts.execution import ExecutionId
from nexura_agents.errors import WorkflowConflictError

from nexura_runtime.workflow import (
    MemoryWorkflowStore,
    ModuleRegistry,
    PendingApproval,
    SQLiteWorkflowStore,
    WorkflowCheckpoint,
    WorkflowEngine,
    WorkflowNode,
    WorkflowSpec,
)


@dataclass
class Backend:
    name: str
    store: Any
    #: Opens a second store on the same data (another process would); None for memory.
    reopen: Callable[[], Any] | None


@pytest.fixture(params=["memory", "sqlite", "postgres"])
def backend(request: pytest.FixtureRequest, tmp_path: Path) -> Any:
    if request.param == "memory":
        yield Backend("memory", MemoryWorkflowStore(), None)
        return
    if request.param == "sqlite":
        path = tmp_path / "wf.sqlite3"
        opened: list[SQLiteWorkflowStore] = []

        def sqlite() -> SQLiteWorkflowStore:
            opened.append(SQLiteWorkflowStore(path))
            return opened[-1]

        yield Backend("sqlite", sqlite(), sqlite)
        for store in opened:
            store.close_sync()
        return
    schema = request.getfixturevalue("postgres_schema")
    yield Backend("postgres", schema.store(), schema.store)


def cp(run_id: str = "r1", **overrides: Any) -> WorkflowCheckpoint:
    values: dict[str, Any] = {
        "workflow_run_id": run_id,
        "spec_version": 1,
        "spec_hash": "h",
        "current_node_id": "a",
        "state": {"n": 0},
        "step_count": 1,
        "status": "running",
        "workspace_id": "ws",
    }
    values.update(overrides)
    return WorkflowCheckpoint(**values)


def paused(run_id: str = "r1", approval: str = "ap1", expires_at: float | None = None) -> Any:
    return cp(
        run_id,
        status="paused",
        current_node_id="b",
        pending_approval=PendingApproval(
            approval_id=approval,
            workflow_run_id=run_id,
            node_id="b",
            input_state={"n": 1},
            workspace_id="ws",
            expires_at=expires_at,
        ),
    )


# ------------------------------------------------------------------ create and get


async def test_create_is_create_if_absent(backend: Backend) -> None:
    store = backend.store
    assert await store.get("r1") is None
    first = cp(step_count=0)
    assert await store.create(first)
    assert first.revision == 1
    assert not await store.create(cp(state={"other": True}, step_count=0))
    got = await store.get("r1")
    assert got is not None
    assert (got.state, got.revision, got.workspace_id) == ({"n": 0}, 1, "ws")


async def test_concurrent_creates_have_one_winner(backend: Backend) -> None:
    results = await asyncio.gather(
        *(backend.store.create(cp(state={"i": i}, step_count=0)) for i in range(6))
    )
    assert sorted(results) == [False] * 5 + [True]
    got = await backend.store.get("r1")
    assert got is not None and got.state == {"i": results.index(True)}


async def test_start_reservation_is_not_history_but_other_creates_are(backend: Backend) -> None:
    store = backend.store
    assert await store.create(cp("reserved", step_count=0))
    assert await store.history("reserved") == []
    forked = cp("forked", step_count=2, forked_from={"run_id": "src", "index": 1})
    assert await store.create(forked)
    history = await store.history("forked")
    assert [h.step_count for h in history] == [2]
    assert history[0].forked_from == {"run_id": "src", "index": 1}


async def test_round_trip_keeps_every_field(backend: Backend) -> None:
    store = backend.store
    original = paused(expires_at=time.time() + 600)
    original.state = {"text": "naïve ☃ \u0000 end", "nested": {"list": [1, 2.5, None, True]}}
    original.budget_state = {"spent": 1.25}
    original.interrupt = None
    original.error = "e"
    original.forked_from = {"run_id": "src", "index": 0}
    original.pending_effect = {"effect_id": "x", "step_key": "b#2"}
    assert await store.create(original)
    got = await store.get("r1")
    assert got is not None
    assert got.state == original.state
    assert got.budget_state == {"spent": 1.25}
    assert got.error == "e" and got.forked_from == {"run_id": "src", "index": 0}
    assert got.pending_effect == {"effect_id": "x", "step_key": "b#2"}
    assert got.pending_approval is not None
    assert got.pending_approval.to_dict() == original.pending_approval.to_dict()  # type: ignore[union-attr]
    assert got.pause_reason == "effect_unknown"
    got.state["text"] = "mutated"
    again = await store.get("r1")
    assert again is not None and again.state["text"] == original.state["text"]


# ------------------------------------------------------------------ revisions


async def test_fenced_save_is_compare_and_swap(backend: Backend) -> None:
    store = backend.store
    first = cp()
    assert await store.create(first)
    first.state = {"n": 1}
    await store.save(first)
    assert first.revision == 2
    stale = cp(state={"n": "stale"}, revision=1)
    with pytest.raises(WorkflowConflictError) as raised:
        await store.save(stale)
    assert (raised.value.expected_revision, raised.value.actual_revision) == (1, 2)
    assert stale.revision == 1
    got = await store.get("r1")
    assert got is not None and (got.state, got.revision) == ({"n": 1}, 2)
    assert [h.state for h in await store.history("r1")] == [{"n": 0}, {"n": 1}]


async def test_unfenced_save_is_last_writer_wins(backend: Backend) -> None:
    store = backend.store
    loose = cp()
    await store.save(loose)  # no row yet: inserted
    assert loose.revision is None
    await store.save(cp(state={"n": 2}))
    got = await store.get("r1")
    assert got is not None and (got.state, got.revision) == ({"n": 2}, 2)
    expected = cp("fresh", revision=5)
    await store.save(expected)  # a fenced save of a missing run inserts after it
    assert expected.revision == 6


async def test_concurrent_fenced_saves_have_one_winner(backend: Backend) -> None:
    store = backend.store
    assert await store.create(cp())

    async def save(i: int) -> str:
        try:
            await store.save(cp(state={"writer": i}, revision=1))
        except WorkflowConflictError:
            return "conflict"
        return "saved"

    outcomes = await asyncio.gather(*(save(i) for i in range(6)))
    assert sorted(outcomes) == ["conflict"] * 5 + ["saved"]
    got = await store.get("r1")
    assert got is not None and got.revision == 2
    assert len(await store.history("r1")) == 2


async def test_history_is_append_only_and_carries_fork_and_workspace(backend: Backend) -> None:
    store = backend.store
    assert await store.create(cp(forked_from={"run_id": "src"}, step_count=1))
    for step in (2, 3):
        later = cp(step_count=step, state={"n": step}, workspace_id=None)
        await store.save(later)
    history = await store.history("r1")
    assert [h.step_count for h in history] == [1, 2, 3]
    assert {h.workspace_id for h in history} == {"ws"}
    assert all(h.forked_from == {"run_id": "src"} for h in history)
    times = [h.updated_at for h in history]
    assert times == sorted(times)
    got = await store.get("r1")
    assert got is not None and got.forked_from == {"run_id": "src"}
    assert await store.history("missing") == []


# ------------------------------------------------------------------ approvals


async def test_claim_approval_is_single_use(backend: Backend) -> None:
    store = backend.store
    assert await store.create(paused())
    assert not await store.claim_approval("r1", "other")
    assert not await store.claim_approval("missing", "ap1")
    assert await store.claim_approval("r1", "ap1")
    got = await store.get("r1")
    assert got is not None and (got.status, got.revision) == ("running", 2)
    assert not await store.claim_approval("r1", "ap1")
    # The claim moved the revision: a writer that read before it is refused.
    with pytest.raises(WorkflowConflictError):
        await store.save(cp(revision=1))


async def test_concurrent_claims_have_one_winner(backend: Backend) -> None:
    store = backend.store
    assert await store.create(paused())
    claims = await asyncio.gather(*(store.claim_approval("r1", "ap1") for _ in range(8)))
    assert sorted(claims) == [False] * 7 + [True]


async def test_expired_approval_is_never_claimed(backend: Backend) -> None:
    store = backend.store
    assert await store.create(paused("old", expires_at=time.time() - 5))
    assert not await store.claim_approval("old", "ap1")
    got = await store.get("old")
    assert got is not None and got.status == "paused"
    assert await store.create(paused("open", expires_at=None))
    assert await store.claim_approval("open", "ap1")
    assert await store.create(paused("later", expires_at=time.time() + 600))
    assert await store.claim_approval("later", "ap1")


async def test_interrupts_are_claimed_by_interrupt_id(backend: Backend) -> None:
    store = backend.store
    waiting = cp(status="paused", interrupt={"interrupt_id": "int-1", "node_id": "a"})
    assert await store.create(waiting)
    assert not await store.claim_approval("r1", "int-2")
    assert await store.claim_approval("r1", "int-1")
    assert not await store.claim_approval("r1", "int-1")


async def test_claim_of_a_running_run_is_refused(backend: Backend) -> None:
    store = backend.store
    assert await store.create(cp())
    assert not await store.claim_approval("r1", "ap1")


# ------------------------------------------------------------------ listing


async def test_list_children_in_natural_order(backend: Backend) -> None:
    store = backend.store
    ids = ["p/n#10", "p/n#2", "p/n#1[10]", "p/n#1[2]", "p/n#1.2", "p/m#1/x#1", "px/n#1", "P/n#1"]
    for run_id in ids:
        await store.save(cp(run_id))
    children = [c.workflow_run_id for c in await store.list_children("p")]
    assert children == ["p/m#1/x#1", "p/n#1[2]", "p/n#1[10]", "p/n#1.2", "p/n#2", "p/n#10"]
    assert all(c.revision for c in await store.list_children("p"))
    assert await store.list_children("p_") == []  # LIKE wildcards are literal


async def test_list_incomplete_lists_running_top_level_runs(backend: Backend) -> None:
    store = backend.store
    await store.save(cp("old"))
    await asyncio.sleep(0.01)
    await store.save(cp("other-ws", workspace_id="ws2"))
    await asyncio.sleep(0.01)
    await store.save(cp("new"))
    await store.save(cp("new/child#1"))
    await store.save(cp("done", status="completed"))
    assert await store.create(paused("waiting"))
    assert await store.list_incomplete() == ["old", "other-ws", "new"]
    assert await store.list_incomplete(workspace_id="ws") == ["old", "new"]
    assert await store.list_incomplete(limit=1) == ["old"]
    assert await store.list_incomplete(workspace_id="none") == []


# ------------------------------------------------------------------ journal and engine


async def test_execution_journal_lives_next_to_the_checkpoints(backend: Backend) -> None:
    journal = await backend.store.open_execution_store()
    assert await backend.store.open_execution_store() is journal
    reg = ModuleRegistry()
    reg.register("a", lambda state: {"x": 1}, effect="pure")
    reg.register("b", lambda state: {"y": 2})
    engine = WorkflowEngine(_spec(), reg, store=backend.store)
    result = await engine.run({}, workflow_run_id="j1")
    assert result.status == "completed"
    snapshot = await journal.get(ExecutionId("default", "j1"))
    assert snapshot.run.status == "completed"
    assert {str(k) for k in snapshot.steps} == {"a#1", "b#2"}
    assert [e.status.value for e in snapshot.effects.values()] == ["confirmed"]


def _spec(gated: bool = False) -> WorkflowSpec:
    return WorkflowSpec(
        name="contract",
        entry_node="a",
        nodes={
            "a": WorkflowNode(id="a", handler="a", default_transition="b"),
            "b": WorkflowNode(id="b", handler="b", requires_approval=gated),
        },
        terminal_nodes=["b"],
    )


class Crash(BaseException):
    """Stands in for the process dying."""


async def test_engine_pause_resume_and_crash_recover_on_a_reopened_store(
    backend: Backend,
) -> None:
    applied: list[str] = []
    crash = {"b"}

    def pay(state: dict[str, Any]) -> dict[str, Any]:
        applied.append("b")
        if "b" in crash:
            crash.clear()
            raise Crash("b")
        return {"paid": True}

    reg = ModuleRegistry()
    reg.register("a", lambda state: {"noted": True}, effect="pure")
    reg.register("b", pay)
    store = backend.store
    paused_run = await WorkflowEngine(_spec(gated=True), reg, store=store).run(
        {}, workflow_run_id="g1"
    )
    assert paused_run.status == "paused"
    assert paused_run.pending_approval is not None
    later = backend.reopen() if backend.reopen is not None else store
    engine = WorkflowEngine(_spec(gated=True), reg, store=later)
    with pytest.raises(Crash):
        await engine.resume("g1", paused_run.pending_approval.approval_id)
    assert applied == ["b"]
    recovered = await WorkflowEngine(_spec(gated=True), reg, store=later).recover("g1")
    assert (recovered.status, recovered.pause_reason) == ("paused", "effect_unknown")
    assert applied == ["b"]  # non_idempotent: not sent again
    done = await WorkflowEngine(_spec(gated=True), reg, store=later).resolve_effect(
        "g1", "confirmed", actor="ops", result={"paid": True}
    )
    assert done.status == "completed" and done.state == {"noted": True, "paid": True}
    assert applied == ["b"]
    statuses = [h.status for h in await later.history("g1")]
    assert statuses[0] == "running" and statuses[-1] == "completed" and "paused" in statuses
