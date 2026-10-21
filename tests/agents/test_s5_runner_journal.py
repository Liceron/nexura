"""A6 S5: the SDK Runner on the execution journal. All offline; effects are local counters.

Lease from the first write with a heartbeat, a per-call intent just before each tool call,
``ctx.idempotency_key`` / ``ctx.fencing_token``, ``@tool(effect=..., reconcile=...)``,
``resume(..., outcomes=...)`` and the definition digest. Real process kills are in
``tests/durability/test_durability_agents.py``.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import warnings
from pathlib import Path
from typing import Any

import pytest
from nexura_agents import (
    Agent,
    FakeModel,
    MemoryCheckpointStore,
    ResolvedOutcome,
    RunContext,
    Runner,
    RunRecoveryError,
    SQLiteCheckpointStore,
    ToolCall,
    tool,
)
from nexura_agents.contracts.execution import (
    Applied,
    EffectClass,
    EffectResolution,
    EffectStatus,
    ExecutionId,
    LeaseLostError,
    NotApplied,
    Unknown,
    default_idempotency_key,
)
from nexura_agents.durable import lease_owner_is_dead, new_lease_owner
from nexura_agents.errors import ApprovalDecisionError, SDKValidationError, ToolExecutionError


class Crash(BaseException):
    """Simulated process death (not an Exception): the runner keeps the checkpoint."""


def call(name: str, call_id: str = "c1", **arguments: Any) -> list[ToolCall]:
    return [ToolCall(id=call_id, name=name, arguments=arguments)]


def agent_with(tools: list[Any], responses: list[Any], **kwargs: Any) -> Agent:
    return Agent(
        name="A",
        instructions="Be helpful.",
        model=FakeModel(responses=responses),
        tools=tools,
        **kwargs,
    )


def run_id_of(store: MemoryCheckpointStore) -> str:
    return next(iter(store._data))


async def snapshot(store: Any, run_id: str) -> Any:
    journal = await store.open_execution_store()
    return await journal.get(ExecutionId("default", run_id))


# ---------------------------------------------------------------- tool effect classes


def test_effect_declaration_and_alias() -> None:
    @tool
    def plain() -> str:
        """Plain."""
        return "x"

    @tool(idempotent=True)
    def alias() -> str:
        """Alias."""
        return "x"

    @tool(effect="pure")
    def read() -> str:
        """Read."""
        return "x"

    @tool(effect="reconcilable", reconcile=lambda ctx, intent: Unknown())
    def charge() -> str:
        """Charge."""
        return "x"

    assert plain.effect is EffectClass.NON_IDEMPOTENT and not plain.idempotent
    assert alias.effect is EffectClass.IDEMPOTENT and alias.idempotent
    assert read.effect is EffectClass.PURE and read.idempotent
    assert charge.effect is EffectClass.RECONCILABLE and not charge.idempotent
    assert charge.reconcile is not None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"effect": "sometimes"},
        {"effect": "pure", "idempotent": True},
        {"effect": "reconcilable"},
        {"effect": "idempotent", "reconcile": lambda c, i: Unknown()},
        {"effect": "reconcilable", "reconcile": "not callable"},
        {"effect": "reconcilable", "reconcile": lambda c, i: Unknown(), "cache": True},
    ],
)
def test_invalid_effect_declarations_are_refused(kwargs: dict[str, Any]) -> None:
    with pytest.raises(SDKValidationError):

        @tool(**kwargs)
        def t() -> str:
            """T."""
            return "x"


def test_pure_tools_may_be_cached_and_retried() -> None:
    from nexura_agents.tools import ToolRetry

    @tool(effect="pure", cache=True, retry=ToolRetry(max_attempts=2))
    def read() -> str:
        """Read."""
        return "x"

    assert read.cache and read.retry is not None


# ---------------------------------------------------------------- idempotency key, fencing


async def test_tool_context_gets_a_stable_idempotency_key_and_fencing_token() -> None:
    seen: list[tuple[str | None, int | None]] = []

    @tool(idempotent=True)
    def reserve(ctx: RunContext[Any], item: str) -> str:
        """Reserve."""
        seen.append((ctx.idempotency_key, ctx.fencing_token))
        if len(seen) == 1:
            raise Crash
        return "ok"

    store = MemoryCheckpointStore()
    with pytest.raises(Crash):
        await Runner(checkpoint_store=store).run_async(
            agent_with([reserve], [call("reserve", item="x"), "never"]), "go"
        )
    run_id = run_id_of(store)
    done = await Runner(checkpoint_store=store).recover_async(
        agent_with([reserve], ["done"]), run_id
    )
    assert done.status == "completed"
    (key1, token1), (key2, token2) = seen
    expected = default_idempotency_key(ExecutionId("default", run_id), "turn:1/call:c1")
    assert key1 == key2 == expected
    assert key1 is not None and key1.startswith("nx-") and len(key1) == 35
    assert token1 == 1 and token2 is not None and token2 > token1  # the recoverer's token


async def test_without_a_store_the_key_is_derived_and_there_is_no_token() -> None:
    seen: list[Any] = []

    @tool
    def t(ctx: RunContext[Any]) -> str:
        """T."""
        seen.append((ctx.idempotency_key, ctx.fencing_token))
        return "ok"

    result = await Runner().run_async(agent_with([t], [call("t"), "done"]), "go")
    expected = default_idempotency_key(ExecutionId("default", result.run_id), "turn:1/call:c1")
    assert seen == [(expected, None)]


# ---------------------------------------------------------------- lease from the first write


async def test_the_original_run_holds_a_lease_and_a_recoverer_is_refused() -> None:
    store = MemoryCheckpointStore()
    holder: dict[str, Any] = {}

    @tool
    async def look(ctx: RunContext[Any]) -> str:
        """Look."""
        snap = await snapshot(store, ctx.run_id)
        holder["owner"] = snap.run.lease_owner
        holder["token"] = snap.run.fencing_token
        with pytest.raises(RunRecoveryError, match="already being recovered"):
            await Runner(checkpoint_store=store).recover_async(
                agent_with([look], ["x"]), ctx.run_id
            )
        return "ok"

    result = await Runner(checkpoint_store=store).run_async(
        agent_with([look], [call("look"), "done"]), "go"
    )
    assert result.status == "completed"
    assert holder["owner"] is not None and holder["owner"].startswith("nx1.")
    assert holder["token"] == 1
    final = await snapshot(store, result.run_id)
    assert final.run.status == "completed" and final.run.lease_owner is None
    assert await store.list_incomplete() == []


async def test_heartbeat_renews_the_lease_during_a_long_call() -> None:
    store = MemoryCheckpointStore()
    expiries: list[float] = []

    @tool(effect="pure")
    async def slow(ctx: RunContext[Any]) -> str:
        """Slow."""
        for _ in range(4):
            expiries.append((await snapshot(store, ctx.run_id)).run.lease_expires_at)
            await asyncio.sleep(0.15)
        return "ok"

    runner = Runner(checkpoint_store=store, lease_ttl=0.3)
    result = await runner.run_async(agent_with([slow], [call("slow"), "done"]), "go")
    assert result.status == "completed"  # 0.6 s of work under a 0.3 s lease
    assert expiries == sorted(expiries) and expiries[-1] > expiries[0]


async def test_a_stale_owner_is_fenced_and_dispatches_nothing() -> None:
    from nexura_agents.execution_store import ExecutionLease

    store = MemoryCheckpointStore()
    calls = {"second": 0}

    @tool
    async def first(ctx: RunContext[Any]) -> str:
        """First: while it runs, another worker takes the run over."""
        journal = await store.open_execution_store()
        run = (await snapshot(store, ctx.run_id)).run
        await journal.release(
            ExecutionLease(
                run.execution_id, run.lease_owner, run.fencing_token, run.lease_expires_at
            )
        )
        assert await journal.claim(
            run.execution_id, "other-worker", 60, definition_digest=run.definition_digest
        )
        return "ok"

    @tool
    def second() -> str:
        """Second: must never run in the stale worker."""
        calls["second"] += 1
        return "ok"

    agent = agent_with(
        [first, second],
        [[*call("first"), *call("second", "c2")], "done"],
        parallel_tool_calls=False,
    )
    with pytest.raises(LeaseLostError):
        await Runner(checkpoint_store=store).run_async(agent, "go")
    assert calls["second"] == 0
    snap = await snapshot(store, run_id_of(store))
    assert snap.run.lease_owner == "other-worker" and snap.run.fencing_token == 2


def test_lease_ttl_is_validated() -> None:
    for bad in (0, -1, float("inf"), True, "30"):
        with pytest.raises(SDKValidationError):
            Runner(lease_ttl=bad)  # type: ignore[arg-type]


def test_a_store_without_claim_is_deprecated() -> None:
    class Plain:
        async def save(self, run_id: str, checkpoint_json: str) -> None: ...
        async def load(self, run_id: str) -> str | None: ...
        async def delete(self, run_id: str) -> None: ...
        async def list_incomplete(self, limit: int = 100) -> list[str]: ...

    with pytest.warns(DeprecationWarning, match="without claim|no claim"):
        Runner(checkpoint_store=Plain())
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        Runner(checkpoint_store=MemoryCheckpointStore())


# ---------------------------------------------------------------- per-call intents


def counted(c: dict[str, int], name: str, crash: bool = False, **kw: Any) -> Any:
    @tool(name=name, **kw)
    def fn(x: str = "") -> str:
        """Counted tool."""
        c[name] = c.get(name, 0) + 1
        if crash and c[name] == 1:
            raise Crash
        return f"{name}:{x}"

    return fn


async def test_a_sibling_that_never_started_runs_on_recovery() -> None:
    """K1 per call: only the call that was running is uncertain, not its unstarted sibling."""
    c: dict[str, int] = {}
    store = MemoryCheckpointStore()
    tools = [counted(c, "a", crash=True), counted(c, "b")]
    batch = [*call("a", x="1"), *call("b", "c2", x="2")]
    with pytest.raises(Crash):
        await Runner(checkpoint_store=store).run_async(
            agent_with(tools, [batch, "never"], parallel_tool_calls=False), "go"
        )
    run_id = run_id_of(store)
    snap = await snapshot(store, run_id)
    assert set(snap.steps) == {"turn:1/call:c1"}  # b has no intent: it never started

    model = FakeModel(responses=["finished"])
    agent = Agent(name="A", instructions="Be helpful.", model=model, tools=tools)
    result = await Runner(checkpoint_store=store).recover_async(
        agent, run_id, on_uncertain="tell_model"
    )
    assert result.status == "completed"
    assert c == {"a": 1, "b": 1}
    results = [m.content or "" for m in model.calls[-1]["messages"] if m.role == "tool"]
    assert "unknown" in results[0] and results[1] == "b:2"
    effects = (await snapshot(store, run_id)).effects
    statuses = {e.intent.step_key: e.status for e in effects.values()}
    assert statuses == {"turn:1/call:c1": "indeterminate", "turn:1/call:c2": "confirmed"}


async def test_pure_tools_write_no_intent_and_rerun() -> None:
    c: dict[str, int] = {}
    store = MemoryCheckpointStore()
    with pytest.raises(Crash):
        await Runner(checkpoint_store=store).run_async(
            agent_with([counted(c, "r", crash=True, effect="pure")], [call("r"), "x"]), "go"
        )
    run_id = run_id_of(store)
    assert (await snapshot(store, run_id)).steps == {}
    done = await Runner(checkpoint_store=store).recover_async(
        agent_with([counted(c, "r", effect="pure")], ["done"]), run_id
    )
    assert done.status == "completed" and c["r"] == 2


# ---------------------------------------------------------------- unknown outcome: pause, resolve


async def _paused_charge(store: Any, c: dict[str, int], **tool_kwargs: Any) -> tuple[Any, Any]:
    with pytest.raises(Crash):
        await Runner(checkpoint_store=store).run_async(
            agent_with([counted(c, "charge", crash=True, **tool_kwargs)], [call("charge"), "x"]),
            "go",
        )
    run_id = run_id_of(store)
    model = FakeModel(responses=["done", "done again"])
    agent = Agent(
        name="A",
        instructions="Be helpful.",
        model=model,
        tools=[counted(c, "charge", **tool_kwargs)],
    )
    runner = Runner(checkpoint_store=store)
    paused = await runner.recover_async(agent, run_id)
    return paused, (runner, agent, model)


async def test_unknown_outcome_pauses_with_effect_unknown() -> None:
    c: dict[str, int] = {}
    store = MemoryCheckpointStore()
    paused, _ = await _paused_charge(store, c)
    assert paused.status == "paused"
    assert paused.interruption.reason == "outcome_unknown_after_crash"
    assert paused.interruption.pause_reason == "effect_unknown"
    snap = await snapshot(store, paused.run_id)
    assert snap.run.status == "paused" and snap.run.lease_owner is None
    assert snap.run.state["pause_reason"] == "effect_unknown"
    (effect,) = snap.effects.values()
    assert effect.status == EffectStatus.INDETERMINATE
    assert snap.run.state["pending_effects"] == [effect.effect_id]
    assert c["charge"] == 1


async def test_resolved_outcome_confirmed_is_used_and_not_rerun() -> None:
    c: dict[str, int] = {}
    store = MemoryCheckpointStore()
    paused, (runner, agent, model) = await _paused_charge(store, c)
    final = await runner.resume_async(
        agent, paused.interruption, outcomes={"c1": ResolvedOutcome({"charged": True})}
    )
    assert final.status == "completed" and c["charge"] == 1
    tool_msgs = [m.content for m in model.calls[-1]["messages"] if m.role == "tool"]
    assert tool_msgs == ['{"charged": true}']
    snap = await snapshot(store, paused.run_id)
    (effect,) = snap.effects.values()
    assert effect.status == EffectStatus.RESOLVED
    assert effect.resolution == EffectResolution.CONFIRMED and effect.resolved_by == "sdk:resume"
    assert snap.steps["turn:1/call:c1"].outcome is not None
    assert snap.run.status == "completed"


async def test_resolved_outcome_not_applied_sends_it_again_with_a_new_key() -> None:
    c: dict[str, int] = {}
    keys: list[str] = []
    store = MemoryCheckpointStore()

    @tool
    def charge(ctx: RunContext[Any]) -> str:
        """Charge."""
        keys.append(ctx.idempotency_key)
        c["charge"] = c.get("charge", 0) + 1
        if c["charge"] == 1:
            raise Crash
        return "charged"

    with pytest.raises(Crash):
        await Runner(checkpoint_store=store).run_async(
            agent_with([charge], [call("charge"), "x"]), "go"
        )
    run_id = run_id_of(store)
    runner = Runner(checkpoint_store=store)
    agent = agent_with([charge], ["done"])
    paused = await runner.recover_async(agent, run_id)
    final = await runner.resume_async(
        agent, paused.interruption, outcomes={"c1": ResolvedOutcome.not_applied()}
    )
    assert final.status == "completed" and c["charge"] == 2
    assert keys[0] != keys[1]  # attempt 2 has its own key
    snap = await snapshot(store, run_id)
    assert set(snap.steps) == {"turn:1/call:c1", "turn:1/call:c1#2"}
    resolutions = {
        k: e.resolution or e.status
        for k, e in ((e.intent.step_key, e) for e in snap.effects.values())
    }
    assert resolutions == {"turn:1/call:c1": "not_applied", "turn:1/call:c1#2": "confirmed"}


async def test_not_applied_gated_tool_needs_a_fresh_approval() -> None:
    c: dict[str, int] = {}
    store = MemoryCheckpointStore()

    @tool(requires_approval=True)
    def pay() -> str:
        """Pay (gated)."""
        c["pay"] = c.get("pay", 0) + 1
        if c["pay"] == 1:
            raise Crash
        return "paid"

    runner = Runner(checkpoint_store=store)
    agent = agent_with([pay], [call("pay"), "x"])
    first = await runner.run_async(agent, "go")
    with pytest.raises(Crash):
        await runner.resume_async(agent, first.interruption, approve=True)
    run_id = first.run_id
    agent2 = agent_with([pay], ["done"])
    paused = await runner.recover_async(agent2, run_id)
    assert paused.interruption.pause_reason == "effect_unknown"
    again = await runner.resume_async(
        agent2, paused.interruption, outcomes={"c1": ResolvedOutcome.not_applied()}
    )
    assert again.status == "paused" and c["pay"] == 1  # not sent without a fresh approval
    assert again.interruption.reason == "effect_not_applied"
    assert again.interruption.pause_reason == "approval"
    assert again.interruption.interruption_id != paused.interruption.interruption_id
    final = await runner.resume_async(agent2, again.interruption, approve=True)
    assert final.status == "completed" and c["pay"] == 2


async def test_not_applied_gated_tool_rejected_on_reapproval_tells_the_model() -> None:
    store = MemoryCheckpointStore()
    n = {"pay": 0}

    @tool(requires_approval=True)
    def pay() -> str:
        """Pay (gated)."""
        n["pay"] += 1
        raise Crash

    runner = Runner(checkpoint_store=store)
    first = await runner.run_async(agent_with([pay], [call("pay")]), "go")
    with pytest.raises(Crash):
        await runner.resume_async(agent_with([pay], []), first.interruption, approve=True)
    model = FakeModel(responses=["ok"])
    agent = Agent(name="A", instructions="Be helpful.", model=model, tools=[pay])
    paused = await runner.recover_async(agent, first.run_id)
    again = await runner.resume_async(
        agent, paused.interruption, outcomes={"c1": ResolvedOutcome.not_applied()}
    )
    final = await runner.resume_async(agent, again.interruption, reject={"c1": "too late"})
    assert final.status == "completed" and n["pay"] == 1
    last = [m.content or "" for m in model.calls[-1]["messages"] if m.role == "tool"][-1]
    assert "rejected by a human reviewer: too late" in last


async def test_resolved_outcome_failed_fails_the_run() -> None:
    c: dict[str, int] = {}
    store = MemoryCheckpointStore()
    paused, (runner, agent, _) = await _paused_charge(store, c)
    with pytest.raises(ToolExecutionError, match="resolved as failed: card declined"):
        await runner.resume_async(
            agent, paused.interruption, outcomes={"c1": ResolvedOutcome.failed("card declined")}
        )
    snap = await snapshot(store, paused.run_id)
    assert snap.run.status == "failed"
    (effect,) = snap.effects.values()
    assert effect.resolution == EffectResolution.FAILED
    assert await store.list_incomplete() == []
    assert c["charge"] == 1


async def test_outcomes_are_validated() -> None:
    c: dict[str, int] = {}
    store = MemoryCheckpointStore()
    paused, (runner, agent, _) = await _paused_charge(store, c)
    with pytest.raises(ApprovalDecisionError, match="ResolvedOutcome"):
        await runner.resume_async(agent, paused.interruption, outcomes={"c1": "done"})  # type: ignore[dict-item]
    with pytest.raises(ApprovalDecisionError, match="one decision"):
        await runner.resume_async(
            agent,
            paused.interruption,
            approve=paused.interruption.pending_digests(),
            outcomes={"c1": ResolvedOutcome("x")},
        )
    with pytest.raises(ValueError):
        ResolvedOutcome(resolution="maybe")  # type: ignore[arg-type]

    @tool(requires_approval=True)
    def gated() -> str:
        """Gated."""
        return "x"

    approval = await Runner(checkpoint_store=MemoryCheckpointStore()).run_async(
        agent_with([gated], [call("gated")]), "go"
    )
    with pytest.raises(ApprovalDecisionError, match="waiting for approval"):
        await Runner().resume_async(
            agent_with([gated], []), approval.interruption, outcomes={"c1": ResolvedOutcome("x")}
        )


async def test_effects_resolved_in_the_journal_let_recover_continue() -> None:
    """An operator resolves the effect directly (e.g. through an effects API); recover goes on."""
    c: dict[str, int] = {}
    store = MemoryCheckpointStore()
    paused, (runner, agent, model) = await _paused_charge(store, c)
    journal = await store.open_execution_store()
    eid = ExecutionId("default", paused.run_id)
    lease = await journal.claim(eid, "operator", 30, definition_digest=agent.definition_digest())
    assert lease is not None
    (effect,) = (await journal.get(eid)).effects.values()
    await journal.resolve_effect(
        lease, effect.effect_id, EffectResolution.CONFIRMED, actor="ops@example", result="charged"
    )
    done = await runner.recover_async(agent, paused.run_id)
    assert done.status == "completed" and c["charge"] == 1
    assert [m.content for m in model.calls[-1]["messages"] if m.role == "tool"] == ["charged"]
    # The stale pause cannot be replayed after the run moved on.
    with pytest.raises((ApprovalDecisionError, RunRecoveryError)):
        await runner.resume_async(
            agent, paused.interruption, approve=paused.interruption.pending_digests()
        )


# ---------------------------------------------------------------- reconcilable tools


def reconcilable(c: dict[str, int], probe: Any) -> Any:
    @tool(effect="reconcilable", reconcile=probe)
    def transfer(ctx: RunContext[Any]) -> str:
        """Transfer."""
        c["transfer"] = c.get("transfer", 0) + 1
        c.setdefault("keys", []).append(ctx.idempotency_key)  # type: ignore[attr-defined]
        if c["transfer"] == 1:
            raise Crash
        return "sent"

    return transfer


async def _crashed_transfer(store: Any, c: dict[str, int], probe: Any) -> str:
    with pytest.raises(Crash):
        await Runner(checkpoint_store=store).run_async(
            agent_with([reconcilable(c, probe)], [call("transfer"), "x"]), "go"
        )
    return run_id_of(store)


async def test_reconcile_applied_records_the_result_without_rerunning() -> None:
    c: dict[str, Any] = {}
    seen: list[Any] = []

    async def probe(ctx: Any, intent: Any) -> Any:
        seen.append((ctx.idempotency_key, intent.idempotency_key, str(intent.step_key)))
        return Applied("sent earlier")

    store = MemoryCheckpointStore()
    run_id = await _crashed_transfer(store, c, probe)
    model = FakeModel(responses=["done"])
    agent = Agent(name="A", instructions="Be helpful.", model=model, tools=[reconcilable(c, probe)])
    done = await Runner(checkpoint_store=store).recover_async(agent, run_id)
    assert done.status == "completed" and c["transfer"] == 1
    assert seen[0][0] == seen[0][1] == c["keys"][0] and seen[0][2] == "turn:1/call:c1"
    assert [m.content for m in model.calls[-1]["messages"] if m.role == "tool"] == ["sent earlier"]
    (effect,) = (await snapshot(store, run_id)).effects.values()
    assert effect.status == EffectStatus.CONFIRMED and effect.result == "sent earlier"


async def test_reconcile_not_applied_reruns_with_the_same_key() -> None:
    c: dict[str, Any] = {}
    store = MemoryCheckpointStore()
    run_id = await _crashed_transfer(store, c, lambda ctx, intent: NotApplied())
    done = await Runner(checkpoint_store=store).recover_async(
        agent_with([reconcilable(c, lambda ctx, intent: NotApplied())], ["done"]), run_id
    )
    assert done.status == "completed" and c["transfer"] == 2
    assert c["keys"][0] == c["keys"][1]


@pytest.mark.parametrize("probe", [lambda ctx, intent: Unknown(), lambda ctx, intent: 1 / 0])
async def test_reconcile_unknown_or_error_pauses(probe: Any) -> None:
    c: dict[str, Any] = {}
    store = MemoryCheckpointStore()
    run_id = await _crashed_transfer(store, c, probe)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        paused = await Runner(checkpoint_store=store).recover_async(
            agent_with([reconcilable(c, probe)], ["done"]), run_id
        )
    assert paused.status == "paused" and paused.interruption.pause_reason == "effect_unknown"
    assert c["transfer"] == 1


# ---------------------------------------------------------------- definition digest


def test_definition_digest_is_stable_and_tracks_tools() -> None:
    @tool
    def a(x: str) -> str:
        """A."""
        return x

    @tool(effect="pure")
    def a_pure(x: str) -> str:
        """A."""
        return x

    a_pure.name = "a"
    base = agent_with([a], [])
    assert base.definition_digest() == agent_with([a], ["other model"]).definition_digest()
    assert base.definition_digest().startswith("sha256:")
    assert base.definition_digest() != agent_with([a_pure], []).definition_digest()
    other = Agent(name="A", instructions="Different.", model=FakeModel(responses=[]), tools=[a])
    assert base.definition_digest() != other.definition_digest()


async def test_changed_definition_warns_on_recovery_and_continues() -> None:
    c: dict[str, int] = {}
    store = MemoryCheckpointStore()
    with pytest.raises(Crash):
        await Runner(checkpoint_store=store).run_async(
            agent_with([counted(c, "a", crash=True, effect="pure")], [call("a"), "x"]), "go"
        )
    run_id = run_id_of(store)
    snap = await snapshot(store, run_id)
    assert snap.run.definition_digest.startswith("sha256:")
    changed = [counted(c, "a", effect="pure"), counted(c, "extra")]
    with pytest.warns(RuntimeWarning, match="definition_digest"):
        done = await Runner(checkpoint_store=store).recover_async(
            agent_with(changed, ["done"]), run_id
        )
    assert done.status == "completed"


# ---------------------------------------------------------------- SQLite journal, owners


async def test_sqlite_journal_lives_in_the_checkpoint_file(tmp_path: Path) -> None:
    c: dict[str, int] = {}
    path = tmp_path / "runs.db"
    store = SQLiteCheckpointStore(path)
    with pytest.raises(Crash):
        await Runner(checkpoint_store=store).run_async(
            agent_with([counted(c, "charge", crash=True)], [call("charge"), "x"]), "go"
        )
    (run_id,) = await store.list_incomplete()
    store.close()

    store2 = SQLiteCheckpointStore(path)
    try:
        snap = await snapshot(store2, run_id)
        assert set(snap.steps) == {"turn:1/call:c1"}
        paused = await Runner(checkpoint_store=store2).recover_async(
            agent_with([counted(c, "charge")], ["done"]), run_id
        )
        assert paused.interruption.pause_reason == "effect_unknown"
        assert c["charge"] == 1
    finally:
        store2.close()
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"


def test_lease_owner_liveness_is_conservative() -> None:
    assert not lease_owner_is_dead(new_lease_owner())  # this process
    for other in (None, "", "0b5c1f", "nx1.other-host.1.0.abc", "nx1.x.y.z"):
        assert not lease_owner_is_dead(other)
    if sys.platform == "win32":
        return
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    host = new_lease_owner().split(".")[1]
    owner = f"nx1.{host}.{child.pid}.0.{'f' * 32}"
    child.wait()
    assert lease_owner_is_dead(owner)  # exited (or reaped)
