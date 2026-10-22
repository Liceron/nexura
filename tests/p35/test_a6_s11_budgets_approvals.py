"""A6 S11: budgets and approvals across a takeover (design §2.7). All offline.

Crashes are simulated in-process with a ``BaseException`` that nothing catches (as in the
S5/S6 tests); the real-process kill rows with ledger assertions are in
``tests/durability``. Model calls are reserved against a ``RunBudget`` with token limits
only, so the unpriced ``fake-model`` is admitted and reserved at the configured
conservative cost.
"""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from nexura_agents import (
    Agent,
    AgentHooks,
    ApproveWith,
    FakeModel,
    MemoryCheckpointStore,
    RunContext,
    Runner,
    SQLiteCheckpointStore,
    ToolCall,
    tool,
)
from nexura_agents.contracts.execution import ExecutionId, StepIntent, StepOutcome
from nexura_agents.errors import ApprovalDecisionError, WorkflowError
from nexura_agents.execution_store import ExecutionStep
from nexura_agents.types import Usage
from nexura_agents.usage import (
    BudgetConfig,
    BudgetManager,
    RunBudget,
    UsageError,
    UsageLedger,
    admission,
)
from nexura_agents.usage.linkage import (
    current_dispatch_path,
    dispatch_scope,
    frames_of,
    scoped_stream,
)
from nexura_agents.usage.recovery import (
    ledgers_of,
    restore_run_budget,
    settle_run_reservations,
)

from nexura_runtime.workflow import (
    MemoryWorkflowStore,
    ModuleRegistry,
    WorkflowEngine,
    WorkflowNode,
    WorkflowSpec,
)


class Crash(BaseException):
    """Stands in for the process dying: nothing catches it."""


def manager(tmp_path: Path | None = None) -> BudgetManager:
    ledger = UsageLedger(str(tmp_path / "usage.db") if tmp_path is not None else None)
    return BudgetManager(ledger=ledger)


def run_budget(mgr: BudgetManager, scope: str = "scope-1", ws: str = "ws") -> RunBudget:
    return mgr.create_run_budget(
        ws, scope, config=BudgetConfig(max_tokens=1_000_000), default_model="fake-model"
    )


def open_rows(mgr: BudgetManager) -> list[Any]:
    return [r for r in mgr.ledger._reservations.values() if r.status in ("active", "expired")]


def settlement(mgr: BudgetManager, reservation_id: str) -> Any:
    return next(
        e
        for e in mgr.ledger.get_entries()
        if e.reservation_id == reservation_id and e.entry_type in ("reconcile", "release")
    )


# ------------------------------------------------------------------- linkage + ledger


def test_reservations_record_the_dispatch_path_outermost_first() -> None:
    mgr = manager()
    budget = run_budget(mgr)
    outside = budget.reserve(10)
    assert "execution" not in outside.metadata
    with dispatch_scope("w", "wf-1", "a#1", tracked=True):
        with dispatch_scope("w", "agent-1", "turn:1/model"):
            assert [f.run_id for f in current_dispatch_path()] == ["wf-1", "agent-1"]
            inner = budget.reserve(10)
        # a run appears once: a new step of wf-1 replaces its entry
        with dispatch_scope("w", "wf-1", "b#2", tracked=True):
            assert [f.step_key for f in current_dispatch_path()] == ["b#2"]
    assert current_dispatch_path() == ()
    frames = frames_of(inner.metadata)
    assert [(f.run_id, f.step_key, f.tracked) for f in frames] == [
        ("wf-1", "a#1", True),
        ("agent-1", "turn:1/model", False),
    ]
    assert inner.metadata["step_key"] == "turn:1/model"
    assert inner.metadata["execution_run_id"] == "agent-1"
    # The reservation's own ledger entry carries the same link.
    entry = next(e for e in mgr.ledger.get_entries() if e.reservation_id == inner.reservation_id)
    assert entry.metadata["step_key"] == "turn:1/model"


async def test_scoped_stream_does_not_leak_the_scope_to_the_consumer() -> None:
    seen: list[tuple[str, ...]] = []

    async def source() -> Any:
        for i in range(2):
            seen.append(tuple(f.step_key for f in current_dispatch_path()))
            yield i

    items = []
    async for item in scoped_stream(source(), "w", "r", "turn:3/model"):
        assert current_dispatch_path() == ()
        items.append(item)
    assert items == [0, 1] and seen == [("turn:3/model",), ("turn:3/model",)]


def test_open_reservations_matches_the_execution_not_the_budget_scope() -> None:
    mgr = manager()
    budget = run_budget(mgr, scope="run-x", ws="ws")
    with dispatch_scope("ws", "run-x", "a#1", tracked=True):
        linked = budget.reserve(10)
    with dispatch_scope("other-ws", "run-x", "a#1", tracked=True):
        budget.reserve(10)  # the same run id in another execution workspace
    unlinked = budget.reserve(10)
    settled = budget.reserve(10)
    budget.release(settled.reservation_id)
    ids = [r.reservation_id for r in mgr.ledger.open_reservations("ws", "run-x")]
    assert ids == [linked.reservation_id]
    with_unlinked = mgr.ledger.open_reservations("ws", "run-x", include_unlinked=True)
    assert {r.reservation_id for r in with_unlinked} == {
        linked.reservation_id,
        unlinked.reservation_id,
    }


def _intent(key: str) -> StepIntent:
    return StepIntent(key, "digest", "nx-key")


@pytest.mark.parametrize(
    ("tracked", "step", "action", "reason"),
    [
        (False, None, "settled_unknown", "taken at admission"),
        (True, ExecutionStep(intent=_intent("b#2")), "settled_unknown", "without an outcome"),
        (
            True,
            ExecutionStep(
                intent=_intent("b#2"), outcome=StepOutcome("b#2", "completed", {"ok": 1})
            ),
            "settled_unknown",
            "settlement was lost",
        ),
        (True, None, "released", "never dispatched"),
    ],
)
def test_takeover_settles_dispatched_steps_and_releases_undispatched_ones(
    tracked: bool, step: Any, action: str, reason: str
) -> None:
    mgr = manager()
    budget = run_budget(mgr, scope="wf-1", ws="ws")
    with dispatch_scope("ws", "wf-1", "b#2", tracked=tracked):
        res = budget.reserve(500)
    steps = {"b#2": step} if step is not None else {}
    done = settle_run_reservations(mgr.ledger, workspace_id="ws", run_id="wf-1", steps=steps)
    assert [(d.reservation_id, d.action, d.step_key) for d in done] == [
        (res.reservation_id, action, "b#2")
    ]
    assert reason in done[0].reason
    assert open_rows(mgr) == []
    entry = settlement(mgr, res.reservation_id)
    assert entry.metadata["settled_by"] == "execution_takeover"
    if action == "released":
        assert entry.entry_type == "release" and entry.amount_usd == Decimal("0")
        assert budget.get_status().total_reserved_tokens == 0
        assert budget.get_status().total_tokens == 0
    else:
        # unknown usage at the reservation's estimate (cost and tokens)
        assert entry.entry_type == "reconcile" and entry.usage_state == "unknown"
        assert entry.amount_usd == res.estimated_cost_usd
        assert entry.tokens == res.estimated_tokens == 500
        status = budget.get_status()
        assert status.unknown_usage_count == 1 and status.total_tokens == 500


def test_without_a_journal_and_unlinked_reservations_are_settled_not_released() -> None:
    mgr = manager()
    budget = run_budget(mgr, scope="wf-1", ws="ws")
    with dispatch_scope("ws", "wf-1", "b#2", tracked=True):
        tracked = budget.reserve(10)
    unlinked = budget.reserve(10)
    done = settle_run_reservations(
        mgr.ledger, workspace_id="ws", run_id="wf-1", steps=None, include_unlinked=True
    )
    assert {(d.reservation_id, d.action) for d in done} == {
        (tracked.reservation_id, "settled_unknown"),
        (unlinked.reservation_id, "settled_unknown"),
    }
    assert open_rows(mgr) == []


def test_a_stale_owner_settling_later_gets_the_first_settlement_back() -> None:
    mgr = manager()
    budget = run_budget(mgr, scope="wf-1", ws="ws")
    with dispatch_scope("ws", "wf-1", "b#2", tracked=True):
        res = budget.reserve(100)
    settle_run_reservations(
        mgr.ledger, workspace_id="ws", run_id="wf-1", steps={"b#2": ExecutionStep(_intent("b#2"))}
    )
    # The frozen owner wakes and reports real usage: no double charge, first one stands.
    budget.settle_provider_call(
        res.reservation_id, usage=Usage(prompt_tokens=10, completion_tokens=10, total_tokens=20)
    )
    settlements = [e for e in mgr.ledger.get_entries() if e.reservation_id == res.reservation_id]
    assert [e.entry_type for e in settlements] == ["reservation", "reconcile"]
    # Settling again is a no-op as well.
    assert settle_run_reservations(mgr.ledger, workspace_id="ws", run_id="wf-1", steps={}) == []


def test_ledgers_of_looks_through_combined_budgets_and_dedupes_by_file(tmp_path: Path) -> None:
    first = BudgetManager(ledger=UsageLedger(str(tmp_path / "u.db")))
    second = BudgetManager(ledger=UsageLedger(str(tmp_path / "u.db")))
    a, b = run_budget(first, "a"), run_budget(second, "b")
    combined = admission.combine_budgets(a, b, object())
    assert ledgers_of(combined, None, a) == [first.ledger]


def test_restore_run_budget_returns_the_persisted_policy(tmp_path: Path) -> None:
    original = BudgetManager(ledger=UsageLedger(str(tmp_path / "u.db")))
    run_budget(original, "wf-1")
    later = BudgetManager(ledger=UsageLedger(str(tmp_path / "u.db")))
    # A handle made directly with another config: the ledger's policy wins.
    handle = RunBudget(later, workspace_id="ws", run_id="wf-1", config=BudgetConfig(max_tokens=5))
    restored = restore_run_budget(handle)
    assert restored is not handle and restored.config.max_tokens == 1_000_000
    unknown = RunBudget(later, workspace_id="ws", run_id="nope", config=BudgetConfig())
    assert restore_run_budget(unknown) is unknown
    assert restore_run_budget("not a budget") == "not a budget"


# ------------------------------------------------------------------- SDK Runner


def helper_tool(crash: dict[str, bool], leak: bool = False) -> Any:
    @tool
    async def helper(ctx: RunContext[Any], x: int) -> str:
        """Call a helper model (not idempotent)."""
        budget = admission.active_budget.get()
        reservation = budget.admit_provider_call("fake-model", estimated_tokens=300)
        if crash.pop("now", False):
            raise Crash("in flight")
        if not leak:
            budget.settle_provider_call(reservation, usage=Usage(1, 1, 2))
        return "ok"

    return helper


def billing_agent(tools: list[Any], responses: list[Any]) -> Agent:
    return Agent(name="A", instructions="i", model=FakeModel(responses=responses), tools=tools)


async def test_runner_recover_settles_the_in_flight_tool_reservation_before_any_step() -> None:
    mgr, store = manager(), MemoryCheckpointStore()
    budget = run_budget(mgr)
    crash = {"now": True}
    agent = billing_agent(
        [helper_tool(crash)], [[ToolCall(id="c1", name="helper", arguments={"x": 1})], "done"]
    )
    with pytest.raises(Crash):
        await Runner(checkpoint_store=store, budget=budget).run_async(agent, "go")
    run_id = next(iter(store._data))
    (left,) = open_rows(mgr)
    assert left.metadata["step_key"] == "turn:1/call:c1"
    assert frames_of(left.metadata)[0].tracked is True

    again = billing_agent([helper_tool({})], ["done"])
    result = await Runner(checkpoint_store=store, budget=budget).recover_async(again, run_id)
    assert result.status == "paused"  # non_idempotent call in doubt: effect_unknown
    assert result.interruption.pause_reason == "effect_unknown"
    assert open_rows(mgr) == []
    entry = settlement(mgr, left.reservation_id)
    assert (entry.usage_state, entry.metadata["settled_by"]) == ("unknown", "execution_takeover")
    assert entry.amount_usd == left.estimated_cost_usd


@pytest.mark.parametrize("durable", [False, True])
async def test_a_finished_runner_run_leaves_no_reservation_active(durable: bool) -> None:
    mgr = manager()
    budget = run_budget(mgr)
    agent = billing_agent(
        [helper_tool({}, leak=True)],
        [[ToolCall(id="c1", name="helper", arguments={"x": 1})], "done"],
    )
    runner = Runner(checkpoint_store=MemoryCheckpointStore() if durable else None, budget=budget)
    result = await runner.run_async(agent, "go")
    assert result.status == "completed"
    assert open_rows(mgr) == []
    leaked = next(
        e for e in mgr.ledger.get_entries() if e.metadata.get("settled_by") == "execution_terminal"
    )
    assert leaked.usage_state == "unknown" and leaked.metadata["step_key"] == "turn:1/call:c1"


async def test_a_failed_runner_run_leaves_no_reservation_active() -> None:
    mgr = manager()
    budget = run_budget(mgr)

    @tool
    async def broken(x: int) -> str:
        """Reserve, then fail."""
        admission.active_budget.get().admit_provider_call("fake-model", estimated_tokens=10)
        raise RuntimeError("boom")

    agent = billing_agent(
        [broken], [[ToolCall(id="c1", name="broken", arguments={"x": 1})], "done"]
    )
    with pytest.raises(Exception, match="boom"):
        await Runner(checkpoint_store=MemoryCheckpointStore(), budget=budget).run_async(agent, "go")
    assert open_rows(mgr) == []


def gated_pay(counter: dict[str, int]) -> Any:
    @tool(requires_approval=True)
    def pay(amount: int) -> str:
        """Pay (needs approval)."""
        counter["n"] = counter.get("n", 0) + 1
        counter["last"] = amount
        return "paid"

    return pay


def pay_agent(counter: dict[str, int], first: bool = True) -> Agent:
    responses: list[Any] = ["done"]
    if first:
        responses.insert(0, [ToolCall(id="c1", name="pay", arguments={"amount": 1})])
    return billing_agent([gated_pay(counter)], responses)


async def test_a_run_started_with_a_budget_is_not_continued_without_one(tmp_path: Path) -> None:
    store = SQLiteCheckpointStore(str(tmp_path / "cp.db"))
    mgr = manager(tmp_path)
    counter: dict[str, int] = {}
    paused = await Runner(checkpoint_store=store, budget=run_budget(mgr)).run_async(
        pay_agent(counter), "go"
    )
    assert paused.status == "paused"
    with pytest.raises(UsageError, match="budget scope 'scope-1'"):
        await Runner(checkpoint_store=store).resume_async(
            pay_agent(counter, first=False), paused.interruption, approve=True
        )
    with pytest.raises(UsageError):
        await Runner(checkpoint_store=store).recover_async(
            pay_agent(counter, first=False), paused.run_id
        )
    assert counter == {}
    # The refusal consumed nothing: with the restored budget the same approval works.
    restored = BudgetManager(ledger=UsageLedger(str(tmp_path / "usage.db"))).get_run_budget(
        "scope-1"
    )
    assert restored is not None and restored.config.max_tokens == 1_000_000
    done = await Runner(checkpoint_store=store, budget=restored).resume_async(
        pay_agent(counter, first=False), paused.interruption, approve=True
    )
    assert done.status == "completed" and counter["n"] == 1
    store.close()


async def test_another_budget_scope_warns() -> None:
    store, mgr = MemoryCheckpointStore(), manager()
    counter: dict[str, int] = {}
    paused = await Runner(checkpoint_store=store, budget=run_budget(mgr)).run_async(
        pay_agent(counter), "go"
    )
    with pytest.warns(RuntimeWarning, match="continues under"):
        done = await Runner(checkpoint_store=store, budget=run_budget(mgr, "other")).resume_async(
            pay_agent(counter, first=False), paused.interruption, approve=True
        )
    assert done.status == "completed"


# ------------------------------------------------------------------- SDK approvals


async def journal_state(store: Any, run_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    snapshot = await (await store.open_execution_store()).get(ExecutionId("default", run_id))
    return dict(snapshot.run.state), dict(snapshot.steps)


async def test_the_intent_carries_the_approval_consumed_in_the_same_commit() -> None:
    store, counter = MemoryCheckpointStore(), {}
    runner = Runner(checkpoint_store=store)
    paused = await runner.run_async(pay_agent(counter), "go")
    approval = paused.interruption.interruption_id
    done = await runner.resume_async(
        pay_agent(counter, first=False), paused.interruption, approve=True
    )
    assert done.status == "completed" and counter["n"] == 1
    events = await (await store.open_execution_store()).get(ExecutionId("default", done.run_id))
    state, steps = dict(events.run.state), events.steps
    assert steps["turn:1/call:c1"].intent.approval_id == approval
    assert state["consumed_approvals"] == [{"approval_id": approval, "step_key": "turn:1/call:c1"}]
    assert state["approval_claim"]["approval_id"] == approval
    dispatched = [e for e in events.events if e.event_type == "tool_dispatched"]
    assert dispatched[0].payload["approval_id"] == approval
    claimed = [e for e in events.events if e.event_type == "approval_claimed"]
    assert claimed and claimed[0].payload["approval_id"] == approval


def crash_after_consume(runner: Runner) -> None:
    original = runner._consume_approval

    async def consume_then_die(interruption: Any, envelope: str) -> None:
        await original(interruption, envelope)
        raise Crash("claimed and consumed, nothing dispatched")

    runner._consume_approval = consume_then_die  # type: ignore[method-assign]


@pytest.mark.parametrize("window", ["after_consume", "before_consume"])
async def test_claimed_unused_approval_is_dispatched_once_by_recover(window: str) -> None:
    store, counter = MemoryCheckpointStore(), {}
    runner = Runner(checkpoint_store=store)
    paused = await runner.run_async(pay_agent(counter), "go")
    approval = paused.interruption.interruption_id
    if window == "after_consume":
        crash_after_consume(runner)
    else:  # the claim is in the journal, the approval store was never reached

        async def die(*args: Any) -> bool:
            raise Crash("claimed, not consumed")

        store.consume_approval = die  # type: ignore[method-assign]
    with pytest.raises(Crash):
        await runner.resume_async(
            pay_agent(counter, first=False), paused.interruption, approve=True
        )
    if window == "before_consume":
        del store.consume_approval
    state, steps = await journal_state(store, paused.run_id)
    assert state["approval_claim"]["approval_id"] == approval and steps == {}

    # A second resume of the claimed approval is refused: recover() continues it.
    with pytest.raises(ApprovalDecisionError, match="recover"):
        await Runner(checkpoint_store=store).resume_async(
            pay_agent(counter, first=False), paused.interruption, approve=True
        )
    done = await Runner(checkpoint_store=store).recover_async(
        pay_agent(counter, first=False), paused.run_id
    )
    assert done.status == "completed" and counter["n"] == 1
    state, steps = await journal_state(store, paused.run_id)
    assert steps["turn:1/call:c1"].intent.approval_id == approval
    # The approval is spent: it cannot be used again.
    with pytest.raises(ApprovalDecisionError):
        await Runner(checkpoint_store=store).resume_async(
            pay_agent(counter, first=False), paused.interruption, approve=True
        )
    assert counter["n"] == 1


async def test_claimed_unused_approval_keeps_the_reviewers_edit() -> None:
    store, counter = MemoryCheckpointStore(), {}
    runner = Runner(checkpoint_store=store)
    paused = await runner.run_async(pay_agent(counter), "go")
    digest = paused.interruption.pending_digests()["c1"]
    crash_after_consume(runner)
    with pytest.raises(Crash):
        await runner.resume_async(
            pay_agent(counter, first=False),
            paused.interruption,
            approve={"c1": ApproveWith(arguments={"amount": 7}, digest=digest)},
        )
    done = await Runner(checkpoint_store=store).recover_async(
        pay_agent(counter, first=False), paused.run_id
    )
    assert done.status == "completed" and counter == {"n": 1, "last": 7}


async def test_claimed_unused_approval_that_expired_is_asked_for_again() -> None:
    store, counter = MemoryCheckpointStore(), {}
    runner = Runner(checkpoint_store=store, approval_ttl=0.3)
    paused = await runner.run_async(pay_agent(counter), "go")
    crash_after_consume(runner)
    with pytest.raises(Crash):
        await runner.resume_async(
            pay_agent(counter, first=False), paused.interruption, approve=True
        )
    await asyncio.sleep(0.4)
    recovering = Runner(checkpoint_store=store, approval_ttl=30)
    again = await recovering.recover_async(pay_agent(counter, first=False), paused.run_id)
    assert again.status == "paused" and counter == {}
    fresh = again.interruption
    assert fresh.interruption_id != paused.interruption.interruption_id
    assert [c.id for c in fresh.pending] == ["c1"]
    done = await recovering.resume_async(pay_agent(counter, first=False), fresh, approve=True)
    assert done.status == "completed" and counter["n"] == 1
    state, steps = await journal_state(store, paused.run_id)
    assert steps["turn:1/call:c1"].intent.approval_id == fresh.interruption_id


async def test_an_approved_call_not_dispatched_before_a_crash_asks_again_once_expired() -> None:
    """The run already checkpointed the batch (tools_pending) under the claim, but the call
    never dispatched: before expiry it runs under the claim; after, a person is asked."""

    for expired in (False, True):
        store, counter = MemoryCheckpointStore(), {}
        runner = Runner(checkpoint_store=store, approval_ttl=0.3 if expired else 30)
        paused = await runner.run_async(pay_agent(counter), "go")

        class Hooks(AgentHooks):
            async def before_tool(self, ctx: Any, call: Any) -> None:
                raise Crash("after the batch checkpoint, before the call")

        runner.hooks = Hooks()
        with pytest.raises(Crash):
            await runner.resume_async(
                pay_agent(counter, first=False), paused.interruption, approve=True
            )
        if expired:
            await asyncio.sleep(0.4)
        result = await Runner(checkpoint_store=store, approval_ttl=30).recover_async(
            pay_agent(counter, first=False), paused.run_id
        )
        if expired:
            assert result.status == "paused" and counter == {}
            assert result.interruption.interruption_id != paused.interruption.interruption_id
        else:
            assert result.status == "completed" and counter["n"] == 1
            _, steps = await journal_state(store, paused.run_id)
            approval = paused.interruption.interruption_id
            assert steps["turn:1/call:c1"].intent.approval_id == approval


async def test_a_resume_after_its_owner_died_in_the_pause_window_is_not_refused() -> None:
    """K7 window: the paused checkpoint is saved, the journal pause commit is not; the
    owner process is gone. Resume releases its lease on its behalf (same host)."""
    from nexura_agents._agent_journal import AgentJournal

    store, counter = MemoryCheckpointStore(), {}
    original = AgentJournal.pause

    async def die(self: AgentJournal, *args: Any, **kwargs: Any) -> None:
        raise Crash("between the checkpoint and the journal pause")

    AgentJournal.pause = die  # type: ignore[method-assign]
    try:
        with pytest.raises(Crash):
            await Runner(checkpoint_store=store).run_async(pay_agent(counter), "go")
    finally:
        AgentJournal.pause = original  # type: ignore[method-assign]
    run_id = next(iter(store._data))
    journal = await store.open_execution_store()
    snapshot = await journal.get(ExecutionId("default", run_id))
    assert snapshot.run.status == "running"  # the journal pause was never committed
    # The in-process "crash" released the lease on the way out (generator finally); make the
    # store look like a dead process still holds it, as after SIGKILL.
    from nexura_agents.durable import new_lease_owner

    dead = new_lease_owner().split(".")
    dead[2] = "999999999"  # a pid that does not exist on this host
    dead_owner = ".".join(dead)
    lease = await journal.claim(
        ExecutionId("default", run_id),
        dead_owner,
        60,
        definition_digest=snapshot.run.definition_digest,
    )
    assert lease is not None
    assert await store.claim(run_id, dead_owner, 60)

    from nexura_agents.durable import decode_checkpoint

    _, interruption = decode_checkpoint(await store.load(run_id), run_id)
    done = await Runner(checkpoint_store=store).resume_async(
        pay_agent(counter, first=False), interruption, approve=True
    )
    assert done.status == "completed" and counter["n"] == 1


# ------------------------------------------------------------------- WorkflowEngine


def wf_spec(gated: bool = False) -> WorkflowSpec:
    return WorkflowSpec(
        name="s11",
        entry_node="a",
        nodes={
            "a": WorkflowNode(id="a", handler="note", default_transition="b"),
            "b": WorkflowNode(id="b", handler="pay", requires_approval=gated),
        },
        terminal_nodes=["b"],
    )


def wf_registry(holder: dict[str, Any], crash_in: set[str], leak: set[str]) -> ModuleRegistry:
    reg = ModuleRegistry()

    def handler(name: str) -> Any:
        def run(state: dict[str, Any]) -> dict[str, Any]:
            budget = holder["budget"]
            reservation = budget.admit_provider_call("fake-model", estimated_tokens=200)
            if name in crash_in:
                crash_in.discard(name)
                raise Crash(name)
            if name not in leak:
                budget.settle_provider_call(reservation, usage=Usage(1, 1, 2))
            holder.setdefault("ran", []).append(name)
            return {name: True}

        return run

    reg.register("note", handler("note"), effect="idempotent")
    reg.register("pay", handler("pay"))  # non_idempotent
    return reg


async def test_workflow_recover_settles_the_crashed_node_and_restores_the_policy(
    tmp_path: Path,
) -> None:
    store = MemoryWorkflowStore()
    first = BudgetManager(ledger=UsageLedger(str(tmp_path / "u.db")))
    holder = {"budget": run_budget(first, "wf-1")}
    engine = WorkflowEngine(
        wf_spec(), wf_registry(holder, {"pay"}, set()), store=store, budget=holder["budget"]
    )
    with pytest.raises(Crash):
        await engine.run({}, workflow_run_id="wf-1")
    (left,) = open_rows(first)
    assert left.metadata["step_key"] == "b#2" and frames_of(left.metadata)[0].tracked

    # Another process: a handle made with another config is replaced by the ledger's policy.
    later = BudgetManager(ledger=UsageLedger(str(tmp_path / "u.db")))
    handle = RunBudget(later, workspace_id="ws", run_id="wf-1", config=BudgetConfig(max_tokens=9))
    holder["budget"] = handle
    again = WorkflowEngine(wf_spec(), wf_registry(holder, set(), set()), store=store, budget=handle)
    result = await again.recover("wf-1")
    assert result.status == "paused" and result.pause_reason == "effect_unknown"
    assert again.budget is not handle and again.budget.config.max_tokens == 1_000_000
    assert open_rows(later) == []
    entry = settlement(later, left.reservation_id)
    assert (entry.usage_state, entry.metadata["settled_by"]) == ("unknown", "execution_takeover")


async def test_workflow_terminal_status_leaves_no_reservation_active() -> None:
    for store in (MemoryWorkflowStore(), None):
        mgr = manager()
        holder = {"budget": run_budget(mgr, "wf-2")}
        engine = WorkflowEngine(
            wf_spec(), wf_registry(holder, set(), {"pay"}), store=store, budget=holder["budget"]
        )
        result = await engine.run({}, workflow_run_id="wf-2")
        assert result.status == "completed"
        assert open_rows(mgr) == [], store
        assert any(
            e.metadata.get("settled_by") == "execution_terminal" for e in mgr.ledger.get_entries()
        )


async def test_workflow_run_with_a_budget_is_not_recovered_without_one() -> None:
    store, mgr = MemoryWorkflowStore(), manager()
    holder = {"budget": run_budget(mgr, "wf-3")}
    engine = WorkflowEngine(
        wf_spec(), wf_registry(holder, {"pay"}, set()), store=store, budget=holder["budget"]
    )
    with pytest.raises(Crash):
        await engine.run({}, workflow_run_id="wf-3")
    bare = WorkflowEngine(wf_spec(), wf_registry(holder, set(), set()), store=store)
    with pytest.raises(WorkflowError, match="budget scope 'wf-3'"):
        await bare.recover("wf-3")
    assert len(open_rows(mgr)) == 1  # nothing ran, nothing was settled by the refusal


async def test_workflow_gated_resume_restores_and_settles() -> None:
    store, mgr = MemoryWorkflowStore(), manager()
    holder = {"budget": run_budget(mgr, "wf-4")}
    engine = WorkflowEngine(
        wf_spec(gated=True),
        wf_registry(holder, set(), set()),
        store=store,
        budget=holder["budget"],
    )
    paused = await engine.run({}, workflow_run_id="wf-4")
    assert paused.status == "paused"
    done = await engine.resume("wf-4", paused.pending_approval.approval_id)
    assert done.status == "completed" and holder["ran"] == ["note", "pay"]
    assert open_rows(mgr) == []


# ------------------------------------------------------------------- legacy engine


async def test_durable_execution_engine_claim_settles_what_a_stopped_worker_left(
    tmp_path: Path,
) -> None:
    from nexura_runtime.runtime.execution import (
        DurableExecutionEngine,
        DurableNodeSpec,
        DurableWorkflowSpec,
    )
    from nexura_runtime.storage.repository import SQLiteRepository

    mgr = manager()
    budget = run_budget(mgr, "legacy-1")
    repository = SQLiteRepository(str(tmp_path / "legacy.db"))
    spec = DurableWorkflowSpec(
        workflow_id="legacy",
        entry_node="t",
        terminal_nodes=["t"],
        nodes={
            "t": DurableNodeSpec(
                node_id="t", handler="transfer", effect_policy="non_idempotent_write"
            )
        },
    )
    crash = {"now": True}

    async def transfer(state: dict[str, Any]) -> dict[str, Any]:
        budget.admit_provider_call("fake-model", estimated_tokens=50)
        if crash.pop("now", False):
            raise Crash("worker died mid-call")
        return {"done": True}

    engine = DurableExecutionEngine(repository, budget=budget)
    engine.register_handler("transfer", transfer)
    try:
        await engine.init_run("ws", "legacy-1", spec)
        await engine.claim_run("ws", "legacy-1", "w1", lease_duration=0.2)
        with pytest.raises(Crash):
            await engine.execute_step("ws", "legacy-1", "w1", spec)
        (left,) = open_rows(mgr)
        assert left.metadata["step_key"].startswith("t#") and frames_of(left.metadata)[0].tracked
        await asyncio.sleep(0.3)
        await engine.claim_run("ws", "legacy-1", "w2", lease_duration=5)
        assert open_rows(mgr) == []
        assert settlement(mgr, left.reservation_id).usage_state == "unknown"
    finally:
        await repository.close()


# ------------------------------------------------------------------- streaming


async def test_streamed_model_calls_are_linked_and_the_scope_does_not_leak() -> None:
    mgr = manager()
    budget = run_budget(mgr)
    agent = Agent(name="S", instructions="i", model=FakeModel(responses=["hello"]))
    run_id = None
    async for event in Runner(budget=budget).stream_async(agent, "go"):
        assert current_dispatch_path() == ()  # the consumer never sees the scope
        run_id = event.run_id
    (res,) = mgr.ledger._reservations.values()
    assert res.status == "committed"
    assert [(f.run_id, f.step_key) for f in frames_of(res.metadata)] == [(run_id, "turn:1/model")]


# ------------------------------------------------------------------- trigger continuation


async def test_a_reclaimed_trigger_job_settles_its_run_reservations(tmp_path: Path) -> None:
    """The worker that continues a job's run (A6 S10) restores the run's budget from the
    service ledger and settles what the dead worker's crashed node left open."""
    import time as _time

    from nexura_runtime.service.api.app import create_api_app
    from nexura_runtime.service.api.runs import start_workflow_run
    from nexura_runtime.service.triggers.models import DurableTriggerJob
    from nexura_runtime.service.triggers.worker import TriggerWorker

    crash = {"pay": True}
    calls = {"pay": 0}
    holder: dict[str, Any] = {}
    reg = ModuleRegistry()

    async def note(state: dict[str, Any]) -> dict[str, Any]:
        return {"noted": True}

    async def pay(state: dict[str, Any]) -> dict[str, Any]:
        calls["pay"] += 1
        budget = holder["app"].state.budget_manager.get_run_budget(holder["run_id"])
        budget.admit_provider_call("fake-model", estimated_tokens=400)
        if crash.pop("pay", False):
            raise Crash("worker died mid-node")
        return {"paid": True}

    reg.register("note", note)
    reg.register("pay", pay)
    app = create_api_app(registry=reg, db_path=str(tmp_path / "svc.sqlite3"), profile="local-owner")
    holder["app"] = app
    state = app.state
    workflow = await state.store.create_workflow(wf_spec())
    state.trigger_store.enqueue_trigger_job(
        DurableTriggerJob(
            job_id="j1",
            workspace_id="default",
            trigger_type="schedule",
            trigger_source_id="s1",
            workflow_id=workflow["workflow_id"],
            release_id="current",
        )
    )
    assert state.trigger_store.claim_pending_job(
        "j1", "dead", lease_seconds=1, now=_time.time() - 100
    )

    def record(run_id: str) -> None:
        holder["run_id"] = run_id
        assert state.trigger_store.set_job_run("j1", "dead", run_id)

    with pytest.raises(Crash):
        await start_workflow_run(
            state,
            spec=wf_spec(),
            workflow_id=workflow["workflow_id"],
            workspace_id="default",
            principal_id="trigger:schedule:s1",
            initial_state={},
            on_run_id=record,
            budget_config={"max_tokens": 50_000},
            trigger={"job_id": "j1", "attempt": 1},
        )
    run_id = holder["run_id"]
    ledger = UsageLedger(str(tmp_path / "svc.sqlite3"))
    (left,) = ledger.open_reservations("default", run_id)
    assert left.metadata["step_key"] == "b#2"

    # A new worker process: a fresh budget manager on the same service database.
    state.budget_manager = None
    outcomes = await TriggerWorker(state, worker_id="w2").run_once()
    assert [(o.status, o.run_id) for o in outcomes] == [("completed", run_id)]
    assert calls["pay"] == 1  # non_idempotent: paused effect_unknown, not repeated
    ledger = UsageLedger(str(tmp_path / "svc.sqlite3"))
    assert ledger.open_reservations("default", run_id, include_unlinked=True) == []
    entry = next(
        e
        for e in ledger.get_entries()
        if e.reservation_id == left.reservation_id and e.entry_type == "reconcile"
    )
    assert (entry.usage_state, entry.metadata["settled_by"]) == ("unknown", "execution_takeover")
    restored = state.budget_manager.get_run_budget(run_id)
    assert restored is not None and restored.config.max_tokens == 50_000
    await state.store.close()


# ------------------------------------------------------------------- review fixes


async def test_a_paused_run_gives_back_its_checkpoint_claim_while_the_stream_is_open() -> None:
    """Review M6: the consumer keeps the stream open after RUN_PAUSED (the run generator has
    not finished, so its ``finally`` has not released anything). Another Runner can resume
    at once because the pause commit gave back the checkpoint claim; without that release
    the claim of this live owner holds the run for the claim's TTL."""
    store, counter = MemoryCheckpointStore(), {}
    stream = Runner(checkpoint_store=store, lease_ttl=30).stream_async(pay_agent(counter), "go")
    interruption = None
    async for event in stream:
        if event.type.value == "run_paused":
            interruption = event.data["interruption"]
            break  # keep the generator suspended: nothing after the pause has run
    assert interruption is not None
    try:
        done = await Runner(checkpoint_store=store).resume_async(
            pay_agent(counter, first=False), interruption, approve=True
        )
        assert done.status == "completed" and counter["n"] == 1
    finally:
        await stream.aclose()


async def test_workflow_recover_under_another_scope_uses_the_recorded_policy(
    tmp_path: Path,
) -> None:
    """Review M3: the recorded scope's persisted policy is restored (with a warning); a
    recorded scope the engine's ledger does not know is refused before anything runs."""
    store = MemoryWorkflowStore()
    mgr = BudgetManager(ledger=UsageLedger(str(tmp_path / "u.db")))
    tight = mgr.create_run_budget(
        "ws", "tenant-A", config=BudgetConfig(max_tokens=5_000), default_model="fake-model"
    )
    holder: dict[str, Any] = {"budget": tight}
    engine = WorkflowEngine(
        wf_spec(), wf_registry(holder, {"pay"}, set()), store=store, budget=tight
    )
    with pytest.raises(Crash):
        await engine.run({}, workflow_run_id="wf-m3")
    other_ledger = BudgetManager(ledger=UsageLedger(str(tmp_path / "other.db")))
    stranger = run_budget(other_ledger, "tenant-C")
    refused = WorkflowEngine(
        wf_spec(), wf_registry(holder, set(), set()), store=store, budget=stranger
    )
    with pytest.raises(WorkflowError, match="no policy for the recorded scope"):
        await refused.recover("wf-m3")
    loose = mgr.create_run_budget("ws", "tenant-B", config=BudgetConfig(max_tokens=10**7))
    again = WorkflowEngine(wf_spec(), wf_registry(holder, set(), set()), store=store, budget=loose)
    with pytest.warns(RuntimeWarning, match="continues under that scope's persisted policy"):
        result = await again.recover("wf-m3")
    assert result.status == "paused"  # pay is non_idempotent and was in flight
    assert again.budget.run_id == "tenant-A" and again.budget.config.max_tokens == 5_000


async def test_runner_terminal_sweep_runs_before_the_terminal_commit() -> None:
    """Review m1: a crash in the terminal commit leaves no reservation active."""
    mgr = manager()
    budget = run_budget(mgr)
    agent = billing_agent(
        [helper_tool({}, leak=True)],
        [[ToolCall(id="c1", name="helper", arguments={"x": 1})], "done"],
    )
    runner = Runner(checkpoint_store=MemoryCheckpointStore(), budget=budget)

    async def die(*args: Any, **kwargs: Any) -> None:
        raise Crash("in the terminal commit")

    runner._delete_checkpoint = die  # type: ignore[method-assign]
    with pytest.raises(Crash):
        await runner.run_async(agent, "go")
    assert open_rows(mgr) == []


async def test_workflow_terminal_sweep_runs_before_the_terminal_commit() -> None:
    from nexura_runtime._workflow_journal import WorkflowJournal

    mgr = manager()
    holder = {"budget": run_budget(mgr, "wf-m1")}
    engine = WorkflowEngine(
        wf_spec(),
        wf_registry(holder, set(), {"pay"}),
        store=MemoryWorkflowStore(),
        budget=holder["budget"],
    )
    original = WorkflowJournal.record_outcome

    async def die(self: Any, *args: Any, terminal: bool, **kwargs: Any) -> None:
        if terminal:
            raise Crash("in the terminal journal commit")
        await original(self, *args, terminal=terminal, **kwargs)

    WorkflowJournal.record_outcome = die  # type: ignore[method-assign]
    try:
        with pytest.raises(Crash):
            await engine.run({}, workflow_run_id="wf-m1")
    finally:
        WorkflowJournal.record_outcome = original  # type: ignore[method-assign]
    assert open_rows(mgr) == []


def test_an_approval_claim_that_is_not_exact_json_is_not_recorded() -> None:
    """Review m3: tuples, int keys and NaN would change (or be refused) in the journal."""
    from nexura_agents.durable import ResolvedOutcome
    from nexura_agents.runner import _approval_claim, _Decisions
    from nexura_agents.types import RunInterruption

    interruption = RunInterruption(
        run_id="r", agent_name="A", messages=[], pending=[], turn=1, expires_at=time.time() + 60
    )
    for value in ((1, 2), {1: "a"}, float("nan")):
        outcomes = {"c1": ResolvedOutcome(result=value)}
        assert _approval_claim(interruption, _Decisions(), {}, outcomes, {}, "sdk") is None
    exact = _approval_claim(
        interruption, _Decisions(), {}, {"c1": ResolvedOutcome(result=[1, 2])}, {}, "sdk"
    )
    assert exact is not None and exact["turn"] == 1


async def test_an_undecided_gated_call_in_a_recovered_batch_is_asked_again() -> None:
    """Review B1, fail closed: a call to a gated tool with no decision on record and no
    intent is never sent by recovery (here the journal has no claim at all)."""
    store, counter = MemoryCheckpointStore(), {}
    runner = Runner(checkpoint_store=store)
    paused = await runner.run_async(pay_agent(counter), "go")

    class Hooks(AgentHooks):
        async def before_tool(self, ctx: Any, call: Any) -> None:
            raise Crash("after the batch checkpoint, before the call")

    runner.hooks = Hooks()
    with pytest.raises(Crash):
        await runner.resume_async(
            pay_agent(counter, first=False), paused.interruption, approve=True
        )
    journal = await store.open_execution_store()
    eid = ExecutionId("default", paused.run_id)
    lease = await journal.claim(
        eid, "tester", 5, definition_digest=(await journal.get(eid)).run.definition_digest
    )
    assert lease is not None
    state = dict((await journal.get(eid)).run.state)
    state["approval_claim"] = None  # as for a batch checkpointed without a recorded claim
    await journal.commit(lease, state=state, status="running")
    await journal.release(lease)
    result = await Runner(checkpoint_store=store).recover_async(
        pay_agent(counter, first=False), paused.run_id
    )
    assert result.status == "paused" and counter == {}
    assert result.interruption.reason == "effect_not_applied"  # a fresh approval is needed


# ------------------------------------------------------------------- round-2 review fixes


def test_a_terminal_sweep_ends_only_reservations_of_its_own_lease_or_older() -> None:
    """R2-M1: reservations carry the dispatcher's lease token; a terminal sweep with
    ``max_fencing_token`` ends only those no newer than it (and leaves unlinked ones to a
    takeover); a takeover (no limit) ends them all, once."""
    mgr = manager()
    budget = run_budget(mgr, scope="wf-9", ws="ws")
    with dispatch_scope("ws", "wf-9", "a#1", tracked=False, fencing_token=1):
        old = budget.reserve(10)
    with dispatch_scope("ws", "wf-9", "a#1", tracked=False, fencing_token=2):
        newer = budget.reserve(10)
    unlinked = budget.reserve(10)
    assert frames_of(newer.metadata)[0].fencing_token == 2
    done = settle_run_reservations(
        mgr.ledger,
        workspace_id="ws",
        run_id="wf-9",
        steps={},
        include_unlinked=True,
        context="terminal",
        max_fencing_token=1,
    )
    assert [d.reservation_id for d in done] == [old.reservation_id]
    later = settle_run_reservations(
        mgr.ledger, workspace_id="ws", run_id="wf-9", steps={}, include_unlinked=True
    )
    assert {d.reservation_id for d in later} == {newer.reservation_id, unlinked.reservation_id}
    assert open_rows(mgr) == []
    for res in (old, newer, unlinked):
        ends = [
            e
            for e in mgr.ledger.get_entries()
            if e.reservation_id == res.reservation_id and e.entry_type in ("reconcile", "release")
        ]
        assert len(ends) == 1


async def test_a_stale_workflow_owner_does_not_end_the_new_owners_reservation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R2-M1, workflow variant: the first owner's step outlives its lease (no heartbeat), a
    second engine recovers the run and re-runs the idempotent node, which reserves and
    waits; the stale owner then reaches its terminal sweep and must leave that live
    reservation alone (its lease renewal fails first)."""
    from nexura_runtime._workflow_journal import WorkflowJournal

    async def no_heartbeat(self: Any) -> None:
        return None

    monkeypatch.setattr(WorkflowJournal, "_beat", no_heartbeat)
    store, mgr = MemoryWorkflowStore(), manager()
    budget = run_budget(mgr, "wf-stale")
    second_in_flight, release_second = asyncio.Event(), asyncio.Event()
    calls = {"n": 0}
    held: dict[str, str] = {}
    reg = ModuleRegistry()

    async def call(state: dict[str, Any]) -> dict[str, Any]:
        calls["n"] += 1
        me = calls["n"]
        held[f"o{me}"] = budget.admit_provider_call("fake-model", estimated_tokens=50)
        if me == 1:
            await asyncio.sleep(0.6)  # outlives its 0.3 s lease: the run is taken over
        else:
            second_in_flight.set()
            await release_second.wait()
        budget.settle_provider_call(held[f"o{me}"], usage=Usage(4000, 1000, 5000))
        return {"done": me}

    reg.register("call", call, effect="idempotent")
    spec = WorkflowSpec(
        name="stale",
        entry_node="c",
        nodes={"c": WorkflowNode(id="c", handler="call")},
        terminal_nodes=["c"],
    )
    first = WorkflowEngine(spec, reg, store=store, budget=budget, lease_ttl=0.3)
    stale = asyncio.create_task(first.run({}, workflow_run_id="wf-stale"))
    await asyncio.sleep(0.4)
    second = WorkflowEngine(spec, reg, store=store, budget=budget, lease_ttl=30)
    recovering = asyncio.create_task(second.recover("wf-stale"))
    await second_in_flight.wait()
    outcome = await asyncio.gather(stale, return_exceptions=True)
    assert isinstance(outcome[0], BaseException)  # the stale owner's commit is refused
    assert mgr.ledger._reservations[held["o2"]].status == "active"
    release_second.set()
    done = await recovering
    assert done.status == "completed"
    second_entry = settlement(mgr, held["o2"])
    assert second_entry.usage_state != "unknown" and second_entry.tokens == 5000
    assert open_rows(mgr) == []


async def test_a_crash_between_the_terminal_sweep_and_commit_settles_each_once() -> None:
    """R2-M1: after a crash in the terminal commit, recovery's takeover finds every
    reservation already ended by the sweep: one settlement per reservation."""
    mgr = manager()
    budget = run_budget(mgr)
    store = MemoryCheckpointStore()
    agent = billing_agent(
        [helper_tool({}, leak=True)],
        [[ToolCall(id="c1", name="helper", arguments={"x": 1})], "done"],
    )
    runner = Runner(checkpoint_store=store, budget=budget)
    original = runner._delete_checkpoint

    async def die(*args: Any, **kwargs: Any) -> None:
        raise Crash("in the terminal commit")

    runner._delete_checkpoint = die  # type: ignore[method-assign]
    with pytest.raises(Crash):
        await runner.run_async(agent, "go")
    run_id = next(iter(store._data))
    assert open_rows(mgr) == []
    runner._delete_checkpoint = original  # type: ignore[method-assign]
    again = billing_agent([helper_tool({}, leak=True)], ["done"])
    result = await Runner(checkpoint_store=store, budget=budget).recover_async(again, run_id)
    assert result.status == "completed"
    assert open_rows(mgr) == []
    for res in mgr.ledger._reservations.values():
        ends = [
            e
            for e in mgr.ledger.get_entries()
            if e.reservation_id == res.reservation_id and e.entry_type in ("reconcile", "release")
        ]
        assert len(ends) == 1, (res.reservation_id, [e.metadata.get("settled_by") for e in ends])


# ------------------------------------------------------------------- round-3 review fixes


async def test_a_transient_lease_error_at_the_terminal_sweep_leaves_settlement_to_recovery() -> (
    None
):
    """Round 3, minor 2: the sweep's lease renewal fails with a transient store error and
    the terminal commit fails too. The run's result is still returned (with a warning), the
    checkpoint stays, and recovery settles each reservation exactly once."""
    from nexura_agents._agent_journal import AgentJournal

    mgr = manager()
    budget = run_budget(mgr)
    store = MemoryCheckpointStore()
    agent = billing_agent(
        [helper_tool({}, leak=True)],
        [[ToolCall(id="c1", name="helper", arguments={"x": 1})], "done"],
    )
    original_renew, original_finish = AgentJournal.renew, AgentJournal.finish
    calls = {"renew": 0}

    async def flaky_renew(self: AgentJournal) -> None:
        calls["renew"] += 1
        raise OSError("database is locked (transient)")

    async def flaky_finish(self: AgentJournal, status: str) -> None:
        raise OSError("database is locked (transient)")

    AgentJournal.renew = flaky_renew  # type: ignore[method-assign]
    AgentJournal.finish = flaky_finish  # type: ignore[method-assign]
    try:
        with pytest.warns(RuntimeWarning, match="left for recovery"):
            result = await Runner(checkpoint_store=store, budget=budget).run_async(agent, "go")
    finally:
        AgentJournal.renew = original_renew  # type: ignore[method-assign]
        AgentJournal.finish = original_finish  # type: ignore[method-assign]
    assert result.status == "completed" and calls["renew"] >= 1
    leaked = open_rows(mgr)
    assert len(leaked) == 1  # left for recovery, not settled by an unconfirmed owner
    run_id = next(iter(store._data))
    again = billing_agent([helper_tool({}, leak=True)], ["done"])
    replay = await Runner(checkpoint_store=store, budget=budget).recover_async(again, run_id)
    assert replay.status == "completed"
    assert open_rows(mgr) == []
    for res in mgr.ledger._reservations.values():
        ends = [
            e
            for e in mgr.ledger.get_entries()
            if e.reservation_id == res.reservation_id and e.entry_type in ("reconcile", "release")
        ]
        assert len(ends) == 1


async def test_dual_enforcement_counts_each_scope_once_and_the_workspace_per_scope(
    tmp_path: Path,
) -> None:
    """Round 3, minor 3, documented: enforcing the recorded scope alongside a combined
    budget charges each run scope once for the recovered call; the shared workspace counts
    that call once per scope of the combination (conservative over-count, fail closed)."""
    mgr = BudgetManager(ledger=UsageLedger(str(tmp_path / "u.db")))
    mgr.set_workspace_budget("ws", BudgetConfig(max_tokens=10_000_000))
    scopes = {
        name: mgr.create_run_budget(
            "ws", name, config=BudgetConfig(max_tokens=1_000_000), default_model="fake-model"
        )
        for name in "ABC"
    }
    crash = {"now": True}
    holder: dict[str, Any] = {}

    async def call(state: dict[str, Any]) -> dict[str, Any]:
        budget = holder["engine"].budget
        res = await admission.admit(budget, "fake-model", 100, "generate")
        if crash.pop("now", False):
            raise Crash("died")
        await admission.settle(budget, res, "fake-model", Usage(10, 10, 20))
        return {"ok": True}

    reg = ModuleRegistry()
    reg.register("call", call, effect="idempotent")
    spec = WorkflowSpec(
        name="dual",
        entry_node="a",
        nodes={"a": WorkflowNode(id="a", handler="call")},
        terminal_nodes=["a"],
    )
    store = MemoryWorkflowStore()
    holder["engine"] = WorkflowEngine(spec, reg, store=store, budget=scopes["A"])
    with pytest.raises(Crash):
        await holder["engine"].run({}, workflow_run_id="wf-dual")
    before = {s: scopes[s].get_status().total_tokens for s in "ABC"}
    ws_before = mgr.get_workspace_status("ws").total_tokens
    holder["engine"] = WorkflowEngine(
        spec, reg, store=store, budget=admission.combine_budgets(scopes["B"], scopes["C"])
    )
    with pytest.warns(RuntimeWarning, match="enforced alongside"):
        result = await holder["engine"].recover("wf-dual")
    assert result.status == "completed"
    delta = {s: scopes[s].get_status().total_tokens - before[s] for s in "ABC"}
    assert delta == {"A": 200, "B": 100, "C": 100}  # A: takeover settlement + the new call
    # The workspace: the takeover's 100 plus the one new call once per scope (A, B, C).
    assert mgr.get_workspace_status("ws").total_tokens - ws_before == 400


# ------------------------------------------------------------------- round-4 review fix


def _settlements(mgr: BudgetManager, reservation_id: str) -> list[Any]:
    return [
        e
        for e in mgr.ledger.get_entries()
        if e.reservation_id == reservation_id and e.entry_type in ("reconcile", "release")
    ]


async def test_runner_sweeps_after_the_terminal_commit_when_the_lease_check_keeps_failing() -> None:
    """F4-m1: both lease renewals fail transiently, the fenced terminal commit succeeds:
    the same token-filtered sweep runs after it, so nothing stays active, once each."""
    from nexura_agents._agent_journal import AgentJournal

    mgr = manager()
    budget = run_budget(mgr)
    agent = billing_agent(
        [helper_tool({}, leak=True)],
        [[ToolCall(id="c1", name="helper", arguments={"x": 1})], "done"],
    )
    original = AgentJournal.renew
    calls = {"n": 0}

    async def flaky(self: AgentJournal) -> None:
        calls["n"] += 1
        raise OSError("database is locked (transient)")

    AgentJournal.renew = flaky  # type: ignore[method-assign]
    try:
        with pytest.warns(RuntimeWarning, match="settled after the terminal commit"):
            result = await Runner(
                checkpoint_store=MemoryCheckpointStore(), budget=budget
            ).run_async(agent, "go")
    finally:
        AgentJournal.renew = original  # type: ignore[method-assign]
    assert result.status == "completed" and calls["n"] == 2  # retried once
    assert open_rows(mgr) == []
    for res in mgr.ledger._reservations.values():
        ends = _settlements(mgr, res.reservation_id)
        assert len(ends) == 1
    leaked = [
        e for e in mgr.ledger.get_entries() if e.metadata.get("settled_by") == "execution_terminal"
    ]
    assert leaked and leaked[0].metadata["step_key"] == "turn:1/call:c1"


async def test_workflow_sweeps_after_the_terminal_commit_when_the_lease_check_keeps_failing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nexura_runtime._workflow_journal import WorkflowJournal

    async def flaky(self: Any) -> None:
        raise OSError("database is locked (transient)")

    mgr = manager()
    holder = {"budget": run_budget(mgr, "wf-f4")}
    engine = WorkflowEngine(
        wf_spec(),
        wf_registry(holder, set(), {"pay"}),
        store=MemoryWorkflowStore(),
        budget=holder["budget"],
    )
    monkeypatch.setattr(WorkflowJournal, "renew", flaky)
    with pytest.warns(RuntimeWarning, match="settled after the terminal commit"):
        result = await engine.run({}, workflow_run_id="wf-f4")
    assert result.status == "completed"
    assert open_rows(mgr) == []
    for res in mgr.ledger._reservations.values():
        assert len(_settlements(mgr, res.reservation_id)) == 1


async def test_legacy_engine_sweeps_after_the_terminal_commit_when_the_lease_check_keeps_failing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nexura_runtime.runtime.execution import (
        DurableExecutionEngine,
        DurableNodeSpec,
        DurableWorkflowSpec,
    )
    from nexura_runtime.storage.repository import SQLiteRepository

    mgr = manager()
    budget = run_budget(mgr, "legacy-f4")
    repository = SQLiteRepository(str(tmp_path / "legacy.db"))
    spec = DurableWorkflowSpec(
        workflow_id="legacy",
        entry_node="t",
        terminal_nodes=["t"],
        nodes={
            "t": DurableNodeSpec(node_id="t", handler="transfer", effect_policy="idempotent_write")
        },
    )

    async def transfer(state: dict[str, Any]) -> dict[str, Any]:
        budget.admit_provider_call("fake-model", estimated_tokens=50)  # leaks
        return {"done": True}

    engine = DurableExecutionEngine(repository, budget=budget)
    engine.register_handler("transfer", transfer)
    try:
        await engine.init_run("ws", "legacy-f4", spec)
        await engine.claim_run("ws", "legacy-f4", "w1", lease_duration=30)
        store = await engine.get_execution_store()

        async def flaky(*args: Any, **kwargs: Any) -> Any:
            raise OSError("database is locked (transient)")

        monkeypatch.setattr(store, "renew", flaky)
        with pytest.warns(RuntimeWarning, match="settled after the terminal commit"):
            state = await engine.execute_step("ws", "legacy-f4", "w1", spec)
        assert state.status == "completed"
        assert open_rows(mgr) == []
        for res in mgr.ledger._reservations.values():
            assert len(_settlements(mgr, res.reservation_id)) == 1
    finally:
        await repository.close()
