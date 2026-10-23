"""P38 C6 review round 2, SDK variants (converted from the reviewer's repros).

Stale-definition races on the approval, interrupt and lease-expiry paths (B1), changed
child subgraphs (M2) and ``not_applied`` retries after a migration (M3). Every handler,
payment and action is a local fake on an in-process store; nothing leaves the process.
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal

import pytest
from nexura_agents.contracts.execution import LeaseLostError
from nexura_agents.errors import WorkflowSpecMismatchError

from nexura_runtime._workflow_journal import WorkflowJournal
from nexura_runtime.workflow import (
    MemoryWorkflowStore,
    ModuleRegistry,
    WorkflowEngine,
    WorkflowMigrationError,
    WorkflowNode,
    WorkflowSpec,
    interrupt,
    workflow_run_identities,
)


def note(items: list[Any], value: Any) -> Literal[False]:
    """Record ``value`` (a local fake's call) and return False, for ``note(...) or {...}``."""
    items.append(value)
    return False


class Crash(BaseException):
    """A stopped worker."""


def pay_spec(
    version: int, *, gated: bool = True, name: str = "pay", cfg: Any = None
) -> WorkflowSpec:
    return WorkflowSpec(
        name=name,
        version=version,
        entry_node="pay",
        nodes={
            "pay": WorkflowNode(id="pay", handler="pay", requires_approval=gated, config=cfg or {})
        },
    )


# ------------------------------------------------------------------ B1 variants
async def test_b1_migration_commits_before_old_engine_claims_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Variant of B1 on the approval path: the v2 migration commits AFTER the old engine's
    preflight checks but BEFORE its claim_approval (the shipped test injects after claim)."""
    store = MemoryWorkflowStore()
    sends: list[int] = []
    hooks: list[int] = []
    reg = ModuleRegistry()
    reg.register("pay", lambda s: note(sends, s["amount"]) or {}, effect="pure")
    old = WorkflowEngine(pay_spec(1), reg, store=store)
    paused = await old.run({"amount": 5}, workflow_run_id="r")
    assert paused.pending_approval is not None
    approval_id = paused.pending_approval.approval_id
    v2 = pay_spec(2)
    v2.register_migration(1, 2, lambda a, b, s: note(hooks, 1) or {**s, "amount": 500})
    new = WorkflowEngine(v2, reg, store=store)
    real_claim = store.claim_approval
    fired = False

    async def claim(run_id: str, pending_id: str) -> bool:
        nonlocal fired
        if not fired:
            fired = True
            await new.migrate("r")
        return await real_claim(run_id, pending_id)

    monkeypatch.setattr(store, "claim_approval", claim)
    with pytest.raises(WorkflowSpecMismatchError):
        await old.resume("r", approval_id)
    monkeypatch.setattr(store, "claim_approval", real_claim)
    assert sends == [] and hooks == [1]
    cp = await store.get("r")
    assert cp is not None and cp.spec_hash == v2.canonical_hash()
    # The stale claim used up the person's decision; the v2 engine asks again (fail closed).
    fresh = await new.recover("r")
    assert fresh.status == "paused" and fresh.pending_approval is not None
    assert fresh.pending_approval.approval_id != approval_id
    assert fresh.pending_approval.input_state["amount"] == 500
    assert (await new.resume("r", fresh.pending_approval.approval_id)).status == "completed"
    assert sends == [500] and hooks == [1]


async def test_b1_migration_commits_before_old_engine_claims_interrupt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryWorkflowStore()
    seen: list[Any] = []
    hooks: list[int] = []

    def ask(state: dict[str, Any]) -> dict[str, Any]:
        answer = interrupt("local question")
        seen.append((state["amount"], answer))
        return {"answer": answer}

    reg = ModuleRegistry()
    reg.register("pay", ask, effect="pure")
    old = WorkflowEngine(pay_spec(1, gated=False), reg, store=store)
    paused = await old.run({"amount": 5}, workflow_run_id="r")
    assert paused.interrupt is not None
    v2 = pay_spec(2, gated=False)
    v2.register_migration(1, 2, lambda a, b, s: note(hooks, 1) or {**s, "amount": 500})
    new = WorkflowEngine(v2, reg, store=store)
    real_claim = store.claim_approval
    fired = False

    async def claim(run_id: str, pending_id: str) -> bool:
        nonlocal fired
        if not fired:
            fired = True
            await new.migrate("r")
        return await real_claim(run_id, pending_id)

    monkeypatch.setattr(store, "claim_approval", claim)
    with pytest.raises(WorkflowSpecMismatchError):
        await old.resume_interrupt("r", paused.interrupt["interrupt_id"], "local answer")
    monkeypatch.setattr(store, "claim_approval", real_claim)
    assert seen == [] and hooks == [1]


async def test_b1_stale_lease_holder_after_expiry_cannot_overwrite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A v1 worker that already HOLDS the lease stalls (no heartbeat) past its TTL; v2
    migrates under a new lease; the v1 worker then wakes up. It must not commit, overwrite
    the migrated checkpoint or dispatch the next v1 node, and the hook must not run again."""
    store = MemoryWorkflowStore()
    gate = asyncio.Event()
    entered = asyncio.Event()
    hooks: list[int] = []
    old_second: list[Any] = []
    new_second: list[Any] = []

    async def first(state: dict[str, Any]) -> dict[str, Any]:
        entered.set()
        await gate.wait()
        return {"first": True}

    reg = ModuleRegistry()
    reg.register("first", first, effect="pure")
    reg.register("old_second", lambda s: note(old_second, dict(s)) or {}, effect="pure")
    reg.register("new_second", lambda s: note(new_second, dict(s)) or {}, effect="pure")

    def two(version: int, second: str) -> WorkflowSpec:
        return WorkflowSpec(
            name="two",
            version=version,
            entry_node="a",
            nodes={
                "a": WorkflowNode(id="a", handler="first", default_transition="b"),
                "b": WorkflowNode(id="b", handler=second),
            },
        )

    monkeypatch.setattr(WorkflowJournal, "start_heartbeat", lambda self: None)
    old = WorkflowEngine(two(1, "old_second"), reg, store=store, lease_ttl=0.3)
    task = asyncio.create_task(old.run({"value": 1}, workflow_run_id="r"))
    await entered.wait()
    await asyncio.sleep(0.6)  # the stalled holder's lease expires
    v2 = two(2, "new_second")
    v2.register_migration(1, 2, lambda a, b, s: note(hooks, 1) or {**s, "value": s["value"] + 100})
    new = WorkflowEngine(v2, reg, store=store, lease_ttl=0.3)
    migrated = await new.migrate("r")
    assert migrated.state["value"] == 101 and hooks == [1]
    gate.set()
    outcome: Any
    try:
        outcome = await task
    except BaseException as exc:  # noqa: BLE001
        outcome = exc
    # The stale holder lost its lease: it raises instead of committing anything.
    assert isinstance(outcome, LeaseLostError), outcome
    assert old_second == [], "old definition dispatched on migrated state"
    cp = await store.get("r")
    assert cp is not None
    assert cp.spec_hash == v2.canonical_hash()
    result = await new.recover("r")
    assert hooks == [1]
    assert result.status == "completed" and new_second and new_second[-1]["value"] == 101


# ------------------------------------------------------------------ M2 variants
def child_spec(limit: int, version: int | None = None) -> WorkflowSpec:
    return WorkflowSpec(
        name="child",
        version=version,
        entry_node="pay",
        nodes={
            "pay": WorkflowNode(
                id="pay", handler="pay", requires_approval=True, config={"limit": limit}
            )
        },
    )


def parent_spec(version: int, child: WorkflowSpec, *, nested: bool = False) -> WorkflowSpec:
    inner = child
    if nested:
        inner = WorkflowSpec(
            name="middle",
            entry_node="inner",
            nodes={"inner": WorkflowNode(id="inner", handler="", subgraph=child)},
        )
    return WorkflowSpec(
        name="parent",
        version=version,
        entry_node="sub",
        nodes={"sub": WorkflowNode(id="sub", handler="", subgraph=inner)},
    )


@pytest.mark.parametrize("nested", [False, True])
async def test_m2_incompatible_parent_migration_refused_and_v1_still_decides(
    nested: bool,
) -> None:
    store = MemoryWorkflowStore()
    sends: list[int] = []
    reg = ModuleRegistry()
    reg.register("pay", lambda s: note(sends, s["amount"]) or {}, effect="pure")
    v1 = parent_spec(1, child_spec(10), nested=nested)
    paused = await WorkflowEngine(v1, reg, store=store).run({"amount": 5}, workflow_run_id="r")
    assert paused.pending_approval is not None
    v2 = parent_spec(2, child_spec(10_000), nested=nested)
    v2.register_migration(1, 2, lambda a, b, s: s)
    with pytest.raises(WorkflowMigrationError, match="child"):
        await WorkflowEngine(v2, reg, store=store).migrate("r")
    with pytest.raises(WorkflowMigrationError):
        await WorkflowEngine(v2, reg, store=store).resume("r", paused.pending_approval.approval_id)
    cp = await store.get("r")
    assert cp is not None and cp.status == "paused" and cp.workflow_version == 1
    done = await WorkflowEngine(v1, reg, store=store).resume(
        "r", paused.pending_approval.approval_id
    )
    assert done.status == "completed" and sends == [5]


async def test_m2_child_with_registered_path_regates_changed_amount() -> None:
    """The new subgraph registers its own path that changes the child's amount; the old
    child approval (amount 5) must not authorize the migrated amount."""
    store = MemoryWorkflowStore()
    sends: list[int] = []
    reg = ModuleRegistry()
    reg.register("pay", lambda s: note(sends, s["amount"]) or {}, effect="pure")
    v1 = parent_spec(1, child_spec(10, version=1))
    paused = await WorkflowEngine(v1, reg, store=store).run({"amount": 5}, workflow_run_id="r")
    assert paused.pending_approval is not None
    old_id = paused.pending_approval.approval_id
    new_child = child_spec(10_000, version=2)
    new_child.register_migration(1, 2, lambda a, b, s: {**s, "amount": 500})
    v2 = parent_spec(2, new_child)
    v2.register_migration(1, 2, lambda a, b, s: s)
    engine = WorkflowEngine(v2, reg, store=store)
    await engine.migrate("r")
    result = await engine.resume("r", old_id)
    assert sends == [], "migrated child amount sent on an approval of the old amount"
    assert result.status == "paused" and result.pending_approval is not None
    assert result.pending_approval.approval_id != old_id
    final = await engine.resume("r", result.pending_approval.approval_id)
    assert final.status == "completed" and sends == [500]


# ------------------------------------------------------------------ M3 variants
async def _unknown_after_approval(
    store: MemoryWorkflowStore, reg: ModuleRegistry, crash: dict[str, bool], v1: WorkflowSpec
) -> WorkflowEngine:
    e1 = WorkflowEngine(v1, reg, store=store)
    paused = await e1.run({"amount": 5}, workflow_run_id="r")
    assert paused.pending_approval is not None
    with pytest.raises(Crash):
        await e1.resume("r", paused.pending_approval.approval_id)
    unknown = await e1.recover("r")
    assert unknown.pause_reason == "effect_unknown"
    return e1


def pay_registry(sent: list[int], crashes: int = 1) -> tuple[ModuleRegistry, dict[str, Any]]:
    crash = {"left": crashes}

    def pay(state: dict[str, Any]) -> dict[str, Any]:
        if crash["left"] > 0:
            crash["left"] -= 1
            raise Crash
        sent.append(state["amount"])
        return {"paid": state["amount"]}

    reg = ModuleRegistry()
    reg.register("pay", pay, effect="non_idempotent")
    return reg, crash


@pytest.mark.parametrize(
    "change",
    ["amount_and_gate_dropped", "gate_dropped_only", "amount_only_gate_kept", "config_only"],
)
async def test_m3_not_applied_after_migration_needs_fresh_approval(change: str) -> None:
    store = MemoryWorkflowStore()
    sent: list[int] = []
    reg, crash = pay_registry(sent)
    await _unknown_after_approval(store, reg, crash, pay_spec(1))
    gated = change in ("amount_only_gate_kept", "config_only")
    v2 = pay_spec(2, gated=gated, cfg={"x": 1} if change == "config_only" else None)
    scale = 100 if change.startswith("amount") else 1
    v2.register_migration(1, 2, lambda a, b, s: {**s, "amount": s["amount"] * scale})
    e2 = WorkflowEngine(v2, reg, store=store)
    await e2.migrate("r")
    cp = await store.get("r")
    assert cp is not None and cp.pending_effect is not None
    res = await e2.resolve_effect("r", "not_applied", actor="local-reviewer")
    assert sent == []
    assert res.status == "paused" and res.pending_approval is not None
    done = await e2.resume("r", res.pending_approval.approval_id)
    assert done.status == "completed" and sent == [5 * scale]


async def test_m3_two_step_migration_chain_while_unknown() -> None:
    """v1->v2 keeps the payload; v2->v3 (a later deploy) changes it and drops the gate."""
    store = MemoryWorkflowStore()
    sent: list[int] = []
    reg, crash = pay_registry(sent)
    await _unknown_after_approval(store, reg, crash, pay_spec(1))
    v2 = pay_spec(2)
    v2.register_migration(1, 2, lambda a, b, s: s)
    await WorkflowEngine(v2, reg, store=store).migrate("r")
    v3 = pay_spec(3, gated=False)
    v3.register_migration(1, 2, lambda a, b, s: s)
    v3.register_migration(2, 3, lambda a, b, s: {**s, "amount": 500})
    e3 = WorkflowEngine(v3, reg, store=store)
    await e3.migrate("r")
    res = await e3.resolve_effect("r", "not_applied", actor="local-reviewer")
    assert sent == [] and res.status == "paused" and res.pending_approval is not None


async def test_m3_second_not_applied_after_regate_asks_again() -> None:
    """After the migration re-gate is approved (500) and that attempt is ALSO interrupted
    and resolved not_applied, the lingering migration_regates entry must not let the claimed
    fresh approval dispatch again without asking (suspected; it asks again)."""
    store = MemoryWorkflowStore()
    sent: list[int] = []
    reg, crash = pay_registry(sent)
    await _unknown_after_approval(store, reg, crash, pay_spec(1))
    v2 = pay_spec(2, gated=True)
    v2.register_migration(1, 2, lambda a, b, s: {**s, "amount": 500})
    e2 = WorkflowEngine(v2, reg, store=store)
    await e2.migrate("r")
    res = await e2.resolve_effect("r", "not_applied", actor="local-reviewer")
    assert res.pending_approval is not None
    crash["left"] = 1  # the approved 500 attempt is interrupted mid-dispatch too
    with pytest.raises(Crash):
        await e2.resume("r", res.pending_approval.approval_id)
    unknown = await e2.recover("r")
    assert unknown.pause_reason == "effect_unknown" and sent == []
    again = await e2.resolve_effect("r", "not_applied", actor="local-reviewer")
    assert again.status == "paused" and again.pending_approval is not None and sent == []
    final = await e2.resume("r", again.pending_approval.approval_id)
    assert final.status == "completed" and sent == [500]


async def test_baseline_gated_not_applied_twice_asks_each_time() -> None:
    """Control: without a migration the same sequence asks again."""
    store = MemoryWorkflowStore()
    sent: list[int] = []
    reg, crash = pay_registry(sent)
    e1 = await _unknown_after_approval(store, reg, crash, pay_spec(1))
    res = await e1.resolve_effect("r", "not_applied", actor="local-reviewer")
    assert res.status == "paused" and res.pending_approval is not None
    crash["left"] = 1
    with pytest.raises(Crash):
        await e1.resume("r", res.pending_approval.approval_id)
    await e1.recover("r")
    again = await e1.resolve_effect("r", "not_applied", actor="local-reviewer")
    assert again.status == "paused" and again.pending_approval is not None and sent == []


# ------------------------------------------------------------------ Postgres variants
# Sync tests: the ``postgres_workflow_store`` fixture creates its schema with asyncio.run,
# so the async work runs inside each test's own asyncio.run. They need
# NEXURA_TEST_POSTGRES_URI (a disposable database) and skip without it.
def pg_spec(version: int, *, gated: bool = True) -> WorkflowSpec:
    return WorkflowSpec(
        name="pg",
        version=version,
        entry_node="pay",
        nodes={"pay": WorkflowNode(id="pay", handler="pay", requires_approval=gated)},
    )


def test_postgres_batched_identities(postgres_workflow_store: Any) -> None:
    async def run() -> None:
        reg = ModuleRegistry()
        reg.register("pay", lambda s: {}, effect="pure")
        await WorkflowEngine(pg_spec(1), reg, store=postgres_workflow_store).run(
            {}, workflow_run_id="r"
        )
        await WorkflowEngine(pg_spec(1), reg, store=postgres_workflow_store).run(
            {}, workflow_run_id="untouched"
        )
        target = pg_spec(2)
        target.register_migration(1, 2, lambda a, b, s: s)
        await WorkflowEngine(target, reg, store=postgres_workflow_store).migrate("r")
        got = await workflow_run_identities(postgres_workflow_store, ["r", "untouched", "missing"])
        assert got == {
            "r": {
                "definition_digest": target.canonical_hash(),
                "workflow_version": 2,
                "started_workflow_version": 1,
            },
            "untouched": {
                "definition_digest": pg_spec(1).canonical_hash(),
                "workflow_version": 1,
                "started_workflow_version": 1,
            },
        }

    asyncio.run(run())


def test_postgres_m3_not_applied_after_migration_regates(postgres_workflow_store: Any) -> None:
    async def run() -> None:
        sent: list[int] = []
        crash = {"left": 1}

        def pay(state: dict[str, Any]) -> dict[str, Any]:
            if crash["left"]:
                crash["left"] -= 1
                raise Crash
            sent.append(state["amount"])
            return {}

        reg = ModuleRegistry()
        reg.register("pay", pay, effect="non_idempotent")
        e1 = WorkflowEngine(pg_spec(1), reg, store=postgres_workflow_store)
        paused = await e1.run({"amount": 5}, workflow_run_id="r")
        assert paused.pending_approval is not None
        with pytest.raises(Crash):
            await e1.resume("r", paused.pending_approval.approval_id)
        assert (await e1.recover("r")).pause_reason == "effect_unknown"
        v2 = pg_spec(2, gated=False)
        v2.register_migration(1, 2, lambda a, b, s: {**s, "amount": 500})
        e2 = WorkflowEngine(v2, reg, store=postgres_workflow_store)
        await e2.migrate("r")
        res = await e2.resolve_effect("r", "not_applied", actor="local-reviewer")
        assert sent == [] and res.status == "paused" and res.pending_approval is not None
        assert res.pending_approval.regate_reason == "definition_migrated"
        done = await e2.resume("r", res.pending_approval.approval_id)
        assert done.status == "completed" and sent == [500]

    asyncio.run(run())


def test_postgres_stale_v1_claim_after_migration(postgres_workflow_store: Any) -> None:
    async def run() -> None:
        sends: list[int] = []
        hooks: list[int] = []
        reg = ModuleRegistry()
        reg.register("pay", lambda s: note(sends, s["amount"]) or {}, effect="pure")
        old = WorkflowEngine(pg_spec(1), reg, store=postgres_workflow_store)
        paused = await old.run({"amount": 5}, workflow_run_id="r")
        assert paused.pending_approval is not None
        v2 = pg_spec(2)
        v2.register_migration(1, 2, lambda a, b, s: note(hooks, 1) or {**s, "amount": 500})
        new = WorkflowEngine(v2, reg, store=postgres_workflow_store)
        real_claim = postgres_workflow_store.claim_approval
        fired = False

        async def claim(run_id: str, pending_id: str) -> bool:
            nonlocal fired
            if not fired:
                fired = True
                await new.migrate("r")
            return bool(await real_claim(run_id, pending_id))

        postgres_workflow_store.claim_approval = claim
        with pytest.raises(WorkflowSpecMismatchError):
            await old.resume("r", paused.pending_approval.approval_id)
        postgres_workflow_store.claim_approval = real_claim
        assert sends == [] and hooks == [1]
        fresh = await new.recover("r")
        assert fresh.pending_approval is not None
        assert fresh.pending_approval.input_state["amount"] == 500
        await new.resume("r", fresh.pending_approval.approval_id)
        assert sends == [500] and hooks == [1]

    asyncio.run(run())
