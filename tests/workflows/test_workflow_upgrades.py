"""Workflow upgrades: per-node retries, interrupt() for human input, history/replay/fork."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from nexura_runtime.errors import (
    ApprovalAlreadyClaimedError,
    WorkflowError,
    WorkflowExecutionError,
    WorkflowTimeoutError,
    WorkflowValidationError,
)

from nexura_runtime.workflow import (
    MemoryWorkflowStore,
    ModuleRegistry,
    NodeRetryPolicy,
    SQLiteWorkflowStore,
    WorkflowEngine,
    WorkflowNode,
    WorkflowSpec,
    interrupt,
)


@pytest.fixture(params=["memory", "sqlite"])
async def store(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[Any]:
    s: Any = (
        MemoryWorkflowStore()
        if request.param == "memory"
        else SQLiteWorkflowStore(tmp_path / "wf.sqlite3")
    )
    yield s
    await s.close()


def linear(*nodes: WorkflowNode) -> WorkflowSpec:
    chained = []
    for i, node in enumerate(nodes):
        nxt = nodes[i + 1].id if i + 1 < len(nodes) else None
        chained.append(
            WorkflowNode(
                id=node.id,
                handler=node.handler,
                default_transition=nxt,
                requires_approval=node.requires_approval,
                retry=node.retry,
            )
        )
    return WorkflowSpec(
        name="t",
        entry_node=chained[0].id,
        nodes={n.id: n for n in chained},
        terminal_nodes=[chained[-1].id],
    )


# --------------------------------------------------------------------------- retries


async def test_flaky_node_retries_with_backoff_then_succeeds(store: Any) -> None:
    calls: list[int] = []
    registry = ModuleRegistry()

    @registry.register_handler("flaky")
    def flaky(state: dict[str, Any]) -> dict[str, Any]:
        calls.append(1)
        if len(calls) < 3:
            raise ConnectionError("upstream reset")
        return {"fetched": True}

    spec = linear(
        WorkflowNode(id="fetch", handler="flaky", retry={"max_attempts": 3, "initial_delay": 0.01})
    )
    result = await WorkflowEngine(spec, registry, store=store).run({})
    assert result.status == "completed" and result.state == {"fetched": True}
    assert len(calls) == 3
    assert result.step_count == 1  # one step, however many attempts


async def test_retry_on_filters_and_exhaustion_fails_cleanly(store: Any) -> None:
    calls: list[str] = []
    registry = ModuleRegistry()

    @registry.register_handler("bad_input")
    def bad_input(state: dict[str, Any]) -> dict[str, Any]:
        calls.append("x")
        raise ValueError("malformed order")

    spec = linear(
        WorkflowNode(
            id="n",
            handler="bad_input",
            retry={
                "max_attempts": 5,
                "initial_delay": 0,
                "retry_on": ["ConnectionError", "TimeoutError"],
            },
        )
    )
    engine = WorkflowEngine(spec, registry, store=store)
    with pytest.raises(WorkflowExecutionError, match="malformed order"):
        await engine.run({}, workflow_run_id="r1")
    assert calls == ["x"]  # ValueError is not in retry_on
    cp = await store.get("r1")
    assert cp is not None and cp.status == "failed"


async def test_retry_respects_the_workflow_deadline() -> None:
    registry = ModuleRegistry()

    @registry.register_handler("always_fails")
    def always_fails(state: dict[str, Any]) -> dict[str, Any]:
        raise ConnectionError("down")

    spec = linear(
        WorkflowNode(id="n", handler="always_fails", retry={"max_attempts": 20, "initial_delay": 5})
    )
    with pytest.raises(WorkflowExecutionError):
        # A 5 s backoff cannot fit in a 1 s workflow, so it fails now instead of sleeping.
        await WorkflowEngine(spec, registry, timeout=1.0).run({})


def test_invalid_retry_policies_are_rejected() -> None:
    for bad in (
        {"max_attempts": 0},
        {"max_attempts": 50},
        {"backoff": 0.5},
        {"initial_delay": -1},
        {"tries": 3},
    ):
        with pytest.raises(WorkflowValidationError):
            NodeRetryPolicy.from_dict(bad, "n")
    assert NodeRetryPolicy(initial_delay=1, backoff=2, max_delay=5).delay(4) == 5


def test_non_idempotent_modules_cannot_be_retried() -> None:
    from nexura_agents.contracts import BaseModule, ExecutionContext, ModuleManifest, ModuleResult

    class SendPayment(BaseModule):
        manifest = ModuleManifest(
            module_id="pay.send",
            version="1.0.0",
            kind="tool",
            title="Send",
            description="Moves money.",
            effect_policy="non_idempotent_write",
        )

        async def invoke(self, ctx: ExecutionContext, inputs: Any, cfg: Any) -> ModuleResult:
            return ModuleResult(status="success")

    registry = ModuleRegistry()
    registry.register("pay", SendPayment())
    spec = linear(WorkflowNode(id="pay", handler="pay", retry={"max_attempts": 3}))
    with pytest.raises(WorkflowValidationError, match="non-idempotent"):
        WorkflowEngine(spec, registry)


def test_retry_is_part_of_the_spec_hash_only_when_set() -> None:
    plain = linear(WorkflowNode(id="n", handler="h"))
    retried = linear(WorkflowNode(id="n", handler="h", retry={"max_attempts": 2}))
    assert "retry" not in plain.to_dict()["nodes"]["n"]  # old specs keep their hash
    assert plain.canonical_hash() != retried.canonical_hash()
    assert WorkflowSpec.from_dict(retried.to_dict()).nodes["n"].retry == {"max_attempts": 2}


# --------------------------------------------------------------------------- interrupts


def review_registry(log: list[str]) -> ModuleRegistry:
    registry = ModuleRegistry()

    @registry.register_handler("draft")
    def draft(state: dict[str, Any]) -> dict[str, Any]:
        return {"draft": f"Refund {state['order']} in full"}

    @registry.register_handler("review")
    def review(state: dict[str, Any]) -> dict[str, Any]:
        log.append("review-started")
        edited = interrupt({"question": "Edit or approve the reply", "draft": state["draft"]})
        amount = interrupt({"question": "Refund amount?"})
        log.append("review-finished")
        return {"final": edited, "amount": amount}

    @registry.register_handler("send")
    def send(state: dict[str, Any]) -> dict[str, Any]:
        log.append(f"sent:{state['final']}:{state['amount']}")
        return {"sent": True}

    return registry


async def test_interrupt_pauses_mid_node_and_resumes_with_answers(store: Any) -> None:
    log: list[str] = []
    spec = linear(
        WorkflowNode(id="draft", handler="draft"),
        WorkflowNode(id="review", handler="review"),
        WorkflowNode(id="send", handler="send"),
    )
    engine = WorkflowEngine(spec, review_registry(log), store=store)

    first = await engine.run({"order": "A1"})
    assert first.status == "paused" and first.interrupt is not None
    assert first.interrupt["node_id"] == "review"
    assert first.interrupt["value"]["draft"] == "Refund A1 in full"
    assert "final" not in first.state and first.step_count == 1

    # A new engine instance, as after a restart, answers the first question...
    engine2 = WorkflowEngine(spec, review_registry(log), store=store)
    second = await engine2.resume_interrupt(
        first.workflow_run_id, first.interrupt["interrupt_id"], "Refund A1: ₹450, sorry!"
    )
    assert second.status == "paused" and second.interrupt is not None
    assert second.interrupt["value"] == {"question": "Refund amount?"}

    done = await engine2.resume_interrupt(
        first.workflow_run_id, second.interrupt["interrupt_id"], 450
    )
    assert done.status == "completed"
    assert done.state["final"] == "Refund A1: ₹450, sorry!" and done.state["amount"] == 450
    assert log.count("review-finished") == 1 and log[-1] == "sent:Refund A1: ₹450, sorry!:450"

    with pytest.raises(WorkflowError):
        await engine2.resume_interrupt(first.workflow_run_id, second.interrupt["interrupt_id"], 1)


async def test_interrupt_answers_are_claimed_once(store: Any) -> None:
    spec = linear(
        WorkflowNode(id="draft", handler="draft"),
        WorkflowNode(id="review", handler="review"),
        WorkflowNode(id="send", handler="send"),
    )
    engine = WorkflowEngine(spec, review_registry([]), store=store)
    first = await engine.run({"order": "A1"})
    assert first.interrupt is not None
    claimed = await store.claim_approval(first.workflow_run_id, first.interrupt["interrupt_id"])
    assert claimed  # someone else answered first
    with pytest.raises((ApprovalAlreadyClaimedError, WorkflowError)):
        await engine.resume_interrupt(first.workflow_run_id, first.interrupt["interrupt_id"], "x")


async def test_interrupt_is_not_swallowed_or_retried() -> None:
    calls: list[int] = []
    registry = ModuleRegistry()

    @registry.register_handler("careless")
    def careless(state: dict[str, Any]) -> dict[str, Any]:
        calls.append(1)
        try:
            interrupt("need a human")
        except Exception:  # broad handlers must not swallow the pause
            return {"swallowed": True}
        return {}

    spec = linear(WorkflowNode(id="n", handler="careless", retry={"max_attempts": 3}))
    result = await WorkflowEngine(spec, registry, store=MemoryWorkflowStore()).run({})
    assert result.status == "paused" and calls == [1]


def test_interrupt_outside_a_workflow_fails_clearly() -> None:
    with pytest.raises(WorkflowError, match="inside a running workflow node"):
        interrupt("hello")


# --------------------------------------------------------------------------- history / fork


def pipeline_registry(log: list[str]) -> ModuleRegistry:
    registry = ModuleRegistry()

    @registry.register_handler("classify")
    def classify(state: dict[str, Any]) -> dict[str, Any]:
        log.append("classify")
        return {"category": "refund" if state["amount"] > 100 else "faq"}

    @registry.register_handler("respond")
    def respond(state: dict[str, Any]) -> dict[str, Any]:
        log.append(f"respond:{state['category']}")
        return {"reply": f"{state['category']} reply for {state['amount']}"}

    return registry


async def test_history_records_every_step(store: Any) -> None:
    spec = linear(
        WorkflowNode(id="classify", handler="classify"),
        WorkflowNode(id="respond", handler="respond"),
    )
    result = await WorkflowEngine(spec, pipeline_registry([]), store=store).run(
        {"amount": 500}, workflow_run_id="run-1"
    )
    history = await WorkflowEngine(spec, pipeline_registry([]), store=store).history("run-1")
    assert [(c.current_node_id, c.step_count, c.status) for c in history] == [
        ("respond", 1, "running"),
        (None, 2, "completed"),
    ]
    assert history[0].state == {"amount": 500, "category": "refund"}
    assert history[-1].state == result.state


async def test_fork_replays_from_a_checkpoint_with_edited_state(store: Any) -> None:
    log: list[str] = []
    spec = linear(
        WorkflowNode(id="classify", handler="classify"),
        WorkflowNode(id="respond", handler="respond"),
    )
    engine = WorkflowEngine(spec, pipeline_registry(log), store=store)
    await engine.run({"amount": 500}, workflow_run_id="run-1")
    log.clear()

    # "What if it had been classified as FAQ?" — fork after classify, edit, continue.
    fork = await engine.fork(
        "run-1", 0, state_update={"category": "faq"}, new_run_id="run-1-what-if"
    )
    assert fork.status == "completed" and fork.state["reply"] == "faq reply for 500"
    assert log == ["respond:faq"]  # classify was not re-executed

    source = await store.get("run-1")
    assert source.state["category"] == "refund"  # original untouched
    forked = await store.get("run-1-what-if")
    assert forked.forked_from == {
        "workflow_run_id": "run-1",
        "checkpoint_index": 0,
        "step_count": 1,
        "state_changed": True,
    }
    fork_history = await engine.history("run-1-what-if")
    assert all(c.forked_from["workflow_run_id"] == "run-1" for c in fork_history)

    with pytest.raises(WorkflowError, match="nothing left"):
        await engine.fork("run-1", -1)
    with pytest.raises(WorkflowError, match="out of range"):
        await engine.fork("run-1", 9)
    with pytest.raises(WorkflowError, match="already exists"):
        await engine.fork("run-1", 0, new_run_id="run-1-what-if")


async def test_fork_from_a_failed_run_retries_after_a_fix(store: Any) -> None:
    attempts: list[int] = []
    registry = ModuleRegistry()

    @registry.register_handler("prep")
    def prep(state: dict[str, Any]) -> dict[str, Any]:
        return {"prepared": True}

    @registry.register_handler("charge")
    def charge(state: dict[str, Any]) -> dict[str, Any]:
        attempts.append(1)
        if state.get("card") != "valid":
            raise RuntimeError("card declined")
        return {"charged": True}

    spec = linear(
        WorkflowNode(id="prep", handler="prep"), WorkflowNode(id="charge", handler="charge")
    )
    engine = WorkflowEngine(spec, registry, store=store)
    with pytest.raises(WorkflowExecutionError):
        await engine.run({"card": "expired"}, workflow_run_id="pay-1")
    history = await engine.history("pay-1")
    assert history[-1].status == "failed" and history[-1].current_node_id == "charge"

    fixed = await engine.fork("pay-1", -1, state_update={"card": "valid"})
    assert fixed.status == "completed" and fixed.state["charged"] is True


async def test_fork_through_an_approval_gate_asks_again(store: Any) -> None:
    registry = ModuleRegistry()
    registry.register("prep", lambda s: {"ready": True})
    registry.register("pay", lambda s: {"paid": True})
    spec = linear(
        WorkflowNode(id="prep", handler="prep"),
        WorkflowNode(id="pay", handler="pay", requires_approval=True),
    )
    engine = WorkflowEngine(spec, registry, store=store)
    first = await engine.run({}, workflow_run_id="g-1")
    assert first.status == "paused"
    done = await engine.resume("g-1", first.pending_approval.approval_id)
    assert done.status == "completed"

    fork = await engine.fork("g-1", 0)  # before the gate
    assert fork.status == "paused" and fork.pending_approval is not None  # no free pass


async def test_fork_rejects_a_changed_spec(store: Any) -> None:
    spec = linear(
        WorkflowNode(id="classify", handler="classify"),
        WorkflowNode(id="respond", handler="respond"),
    )
    await WorkflowEngine(spec, pipeline_registry([]), store=store).run(
        {"amount": 5}, workflow_run_id="r"
    )
    changed = linear(
        WorkflowNode(id="classify", handler="classify", retry={"max_attempts": 2}),
        WorkflowNode(id="respond", handler="respond"),
    )
    from nexura_runtime.errors import WorkflowSpecMismatchError

    with pytest.raises(WorkflowSpecMismatchError):
        await WorkflowEngine(changed, pipeline_registry([]), store=store).fork("r", 0)


def test_workflow_timeout_error_is_still_raised() -> None:
    assert issubclass(WorkflowTimeoutError, Exception)


async def test_crash_before_the_terminal_node_resumes_and_runs_it(store: Any) -> None:
    log: list[str] = []
    registry = pipeline_registry(log)
    spec = linear(
        WorkflowNode(id="classify", handler="classify"),
        WorkflowNode(id="respond", handler="respond"),
    )
    await WorkflowEngine(spec, registry, store=store).run({"amount": 5}, workflow_run_id="c")
    after_classify = (await WorkflowEngine(spec, registry, store=store).history("c"))[0]
    # The checkpoint written before the terminal node must not claim the run is finished,
    # or a restart would skip that node.
    assert after_classify.status == "running" and after_classify.current_node_id == "respond"


# --------------------------------------------------------------------------- B1 regressions


async def test_retry_after_an_answered_interrupt_reuses_the_answer() -> None:
    calls: list[str] = []
    registry = ModuleRegistry()

    @registry.register_handler("confirm_then_call")
    def confirm_then_call(state: dict[str, Any]) -> dict[str, Any]:
        answer = interrupt("Proceed?")
        calls.append(answer)
        if len(calls) < 2:
            raise ConnectionError("flaky upstream")
        return {"answer": answer}

    spec = linear(
        WorkflowNode(
            id="n", handler="confirm_then_call", retry={"max_attempts": 3, "initial_delay": 0}
        )
    )
    engine = WorkflowEngine(spec, registry, store=MemoryWorkflowStore())
    paused = await engine.run({})
    assert paused.interrupt is not None
    done = await engine.resume_interrupt(
        paused.workflow_run_id, paused.interrupt["interrupt_id"], "yes"
    )
    # The retry must not ask the person the same question again.
    assert done.status == "completed" and done.state == {"answer": "yes"}
    assert calls == ["yes", "yes"]


async def test_retry_attempts_share_the_workflow_deadline() -> None:
    calls: list[int] = []
    registry = ModuleRegistry()

    @registry.register_handler("fail_then_hang")
    async def fail_then_hang(state: dict[str, Any]) -> dict[str, Any]:
        calls.append(1)
        if len(calls) == 1:
            raise ConnectionError("reset")
        await asyncio.sleep(5)
        return {}

    spec = linear(
        WorkflowNode(
            id="n", handler="fail_then_hang", retry={"max_attempts": 2, "initial_delay": 0.6}
        )
    )
    started = time.monotonic()
    with pytest.raises(WorkflowTimeoutError):
        await WorkflowEngine(spec, registry, timeout=1.0).run({})
    assert time.monotonic() - started < 1.3  # not 0.6 s backoff + a fresh 1 s


async def test_another_workspace_cannot_read_or_fork_a_run(store: Any) -> None:
    from nexura_runtime.service.auth.errors import CrossWorkspaceAccessError

    spec = linear(
        WorkflowNode(id="classify", handler="classify"),
        WorkflowNode(id="respond", handler="respond"),
    )
    owner = WorkflowEngine(spec, pipeline_registry([]), store=store, workspace_id="tenant-a")
    await owner.run({"amount": 900}, workflow_run_id="private")
    other = WorkflowEngine(spec, pipeline_registry([]), store=store, workspace_id="tenant-b")
    with pytest.raises(CrossWorkspaceAccessError):
        await other.history("private")
    with pytest.raises(CrossWorkspaceAccessError):
        await other.fork("private", 0, new_run_id="stolen")
    assert await store.get("stolen") is None
    forked = await owner.fork("private", 0, new_run_id="mine")
    assert (await store.get("mine")).workspace_id == "tenant-a" and forked.status == "completed"


async def test_concurrent_forks_cannot_share_a_run_id(store: Any) -> None:
    log: list[str] = []
    spec = linear(
        WorkflowNode(id="classify", handler="classify"),
        WorkflowNode(id="respond", handler="respond"),
    )
    engine = WorkflowEngine(spec, pipeline_registry(log), store=store)
    await engine.run({"amount": 5}, workflow_run_id="src")
    log.clear()
    results = await asyncio.gather(
        engine.fork("src", 0, new_run_id="dup"),
        engine.fork("src", 0, new_run_id="dup"),
        return_exceptions=True,
    )
    assert sum(not isinstance(r, BaseException) for r in results) == 1
    assert any(isinstance(r, WorkflowError) and "already exists" in str(r) for r in results)
    assert log == ["respond:faq"]  # the losing fork ran nothing


async def test_runs_saved_before_history_existed_are_backfilled(tmp_path: Path) -> None:
    import sqlite3

    db = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE nexura_workflow_checkpoints (workflow_run_id TEXT PRIMARY KEY, "
        "spec_version INTEGER NOT NULL, spec_hash TEXT NOT NULL, current_node_id TEXT, "
        "state_json TEXT NOT NULL, step_count INTEGER NOT NULL, status TEXT NOT NULL, "
        "approval_id TEXT, approval_json TEXT, error TEXT, updated_at REAL NOT NULL, "
        "budget_json TEXT)"
    )
    conn.execute(
        "INSERT INTO nexura_workflow_checkpoints VALUES "
        "('old', 1, 'h', 'respond', '{\"amount\": 5}', 1, 'running', NULL, NULL, NULL, 1.0, NULL)"
    )
    conn.commit()
    conn.close()
    store = SQLiteWorkflowStore(db)
    history = await store.history("old")
    assert [(c.current_node_id, c.state) for c in history] == [("respond", {"amount": 5})]
    await store.close()
    reopened = SQLiteWorkflowStore(db)
    assert len(await reopened.history("old")) == 1  # backfilled once, not on every open
    await reopened.close()
