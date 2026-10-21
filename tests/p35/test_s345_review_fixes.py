"""A6 S2-S5 review fixes (SDK Runner side): the reviewer's repros, assertions unchanged.

Copied from ``.autopilot/p35/review/test_s345_review.py`` (REVIEW_S345.md). M3a, M3b and
L5 concern the S3 engine and are fixed and tested elsewhere. All effects are local fakes.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import inspect
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from nexura_agents import (
    Agent,
    FakeModel,
    MemoryCheckpointStore,
    ResolvedOutcome,
    Runner,
    RunRecoveryError,
    SQLiteCheckpointStore,
    ToolCall,
    durable,
    tool,
)
from nexura_agents._agent_journal import AgentJournal, call_step_key
from nexura_agents.contracts.execution import (
    EffectClass,
    EffectResolution,
    EffectStatus,
    ExecutionId,
    LeaseLostError,
    StepIntent,
    StepKey,
    default_idempotency_key,
)
from nexura_agents.execution_store import MemoryExecutionStore

ROOT = Path(__file__).resolve().parents[2]


class Crash(BaseException):
    """Simulated process death (not an Exception): the runner keeps the last checkpoint."""


def agent(tools: list[Any], responses: list[Any]) -> Agent:
    return Agent(
        name="review",
        instructions="Local fake effects only.",
        tools=tools,
        model=FakeModel(responses=responses),
    )


def calls(name: str, call_id: str = "c1") -> list[ToolCall]:
    return [ToolCall(id=call_id, name=name, arguments={})]


def crashing_debit(effects: list[str]) -> Any:
    """A plain (non_idempotent) tool whose first dispatch 'applies' and then dies."""

    @tool
    async def charge() -> str:
        """Apply a local fake debit; the first call loses its response (crash)."""
        effects.append("debit")
        if len(effects) == 1:
            raise Crash()
        return "paid"

    return charge


# =============================================================== HIGH


async def test_h1_upgraded_sqlite_checkpoint_keeps_an_inflight_call_in_doubt(tmp_path):
    """H1: a pre-S5 tools_pending checkpoint has no journal row. Recovery after the upgrade
    journals it as if nothing was dispatched (K1) and re-sends the non_idempotent call.
    Correct: a checkpoint journaled at recovery keeps batch-level uncertainty (pause)."""
    path = tmp_path / "upgrade.sqlite3"

    class PreS5SQLiteStore(SQLiteCheckpointStore):
        open_execution_store = None  # what 0.4 wrote: checkpoints and claims, no journal

    effects: list[str] = []
    old = PreS5SQLiteStore(path)
    with pytest.raises(Crash):
        await Runner(checkpoint_store=old).run_async(
            agent([crashing_debit(effects)], [calls("charge")]), "go"
        )
    (run_id,) = await old.list_incomplete()
    old.close()

    upgraded = SQLiteCheckpointStore(path)  # same file, opened by the S5 release
    try:
        result = await Runner(checkpoint_store=upgraded).recover_async(
            agent([crashing_debit(effects)], ["done"]), run_id
        )
    finally:
        upgraded.close()
    assert effects == ["debit"], f"non_idempotent call dispatched again after upgrade: {effects}"
    assert result.status == "paused"


async def test_h2_sqlite_workspace_cannot_recover_another_workspaces_run(tmp_path):
    """H2: SQLiteCheckpointStore scopes only the journal by workspace_id; checkpoints and
    claims are shared. Workspace B lists and recovers A's crashed run, finds no journal row
    under (B, run), journals it fresh and re-sends A's in-flight non_idempotent call."""
    path = tmp_path / "shared.sqlite3"
    effects: list[str] = []
    a = SQLiteCheckpointStore(path, workspace_id="A")
    b = SQLiteCheckpointStore(path, workspace_id="B")
    try:
        with pytest.raises(Crash):
            await Runner(checkpoint_store=a).run_async(
                agent([crashing_debit(effects)], [calls("charge")]), "go"
            )
        (run_id,) = await a.list_incomplete()
        leaked = run_id in await b.list_incomplete()
        with contextlib.suppress(RunRecoveryError):
            await Runner(checkpoint_store=b).recover_async(
                agent([crashing_debit(effects)], ["done"]), run_id
            )
    finally:
        a.close()
        b.close()
    assert effects == ["debit"], f"workspace B re-dispatched A's in-flight call: {effects}"
    assert not leaked, "workspace B lists workspace A's checkpoint"


async def test_h3_recovery_uses_the_persisted_effect_class_not_the_current_tool():
    """H3: the digest change is only a warning, and _settle_in_doubt reads the CURRENT
    tool's class. Re-declaring a crashed non_idempotent tool as idempotent re-sends the
    dispatched call. Correct: the persisted StepIntent.effect_class decides recovery."""
    store = MemoryCheckpointStore()
    effects: list[str] = []

    async def fn() -> str:
        """Apply a local fake debit."""
        effects.append("debit")
        if len(effects) == 1:
            raise Crash()
        return "paid"

    with pytest.raises(Crash):
        await Runner(checkpoint_store=store).run_async(
            agent([tool(name="charge", effect="non_idempotent")(fn)], [calls("charge")]), "go"
        )
    (run_id,) = await store.list_incomplete()
    changed = tool(name="charge", effect="idempotent")(fn)
    with pytest.warns(RuntimeWarning, match="(?i)definition"):
        result = await Runner(checkpoint_store=store).recover_async(
            agent([changed], ["done"]), run_id
        )
    assert effects == ["debit"], f"persisted non_idempotent intent repeated: {effects}"
    assert result.status == "paused"


# =============================================================== MEDIUM


async def test_m1_reused_intent_is_fenced_before_redispatch():
    """M1: record_intent returns early when the step already has an intent (the recovery
    re-run of an idempotent/reconcilable call), so no store write and no lease check
    happens before dispatch. A recoverer that stalled past its lease dispatches after
    another worker took over. Correct: assert_lease() against the store first."""
    clock = [100.0]
    store = MemoryExecutionStore(clock=lambda: clock[0])
    checkpoints = MemoryCheckpointStore(execution_store=store)
    journal = await AgentJournal.open(checkpoints, "run", 10)
    await journal.begin("old-owner", "sha256:definition", "review")
    key = StepKey.agent_tool(1, "c1")
    intent = StepIntent(
        key, "payload", default_idempotency_key(journal.eid, key), EffectClass.IDEMPOTENT
    )
    await journal.record_intent(intent, "charge")
    clock[0] = 111.0  # the frozen owner's lease expired by the store clock
    winner = await store.claim(journal.eid, "new-owner", 10, definition_digest="sha256:definition")
    assert winner is not None
    try:
        with pytest.raises(LeaseLostError):
            await journal.record_intent(intent, "charge")  # the runner dispatches right after
    finally:
        await journal.close()


def test_m2_owner_hidden_by_proc_hidepid_is_not_declared_dead(monkeypatch):
    """M2: with /proc mounted hidepid=2 (or a sandboxed /proc), /proc/<pid> of a live
    process of another user is invisible: _proc_stat raises FileNotFoundError and
    lease_owner_is_dead() returns True, so takeover() releases a LIVE owner's lease and
    checkpoint claim at once. Correct: invisible is not dead (confirm with kill(pid, 0):
    EPERM = alive)."""
    if not Path("/proc/self/stat").exists():
        pytest.skip("Linux /proc only")
    pid = os.getppid()  # a live process
    owner = f"nx1.{durable._host_identity()}.{pid}.{durable._start_ticks(pid)}.{'a' * 32}"
    assert not durable.lease_owner_is_dead(owner)  # visible: correctly alive
    real = durable._proc_stat

    def hidden(target: int) -> list[str]:
        if target == pid:
            raise FileNotFoundError(f"/proc/{target}/stat")  # what hidepid=2 returns
        return real(target)

    monkeypatch.setattr(durable, "_proc_stat", hidden)
    assert os.kill(pid, 0) is None  # the process is alive
    assert not durable.lease_owner_is_dead(owner), "live owner hidden by /proc declared dead"


async def test_l1_checkpointed_result_crash_does_not_leave_a_pending_effect():
    """L1: crash after the tool result is checkpointed, before record_outcome. Recovery
    sees an answered call and never closes the intent: a completed run keeps a pending
    effect (not resolvable via REST). Correct: recovery confirms it from the checkpoint."""

    class CrashOutcomeStore(MemoryExecutionStore):
        crash = True

        async def commit(self, *args: Any, **kwargs: Any) -> Any:
            confirming = any(e.status == EffectStatus.CONFIRMED for e in kwargs.get("effects", ()))
            if self.crash and confirming:
                self.crash = False
                raise Crash()
            return await super().commit(*args, **kwargs)

    journal = CrashOutcomeStore()
    checkpoints = MemoryCheckpointStore(execution_store=journal)
    effects: list[str] = []

    @tool
    async def charge() -> str:
        """Apply a local fake debit."""
        effects.append("debit")
        return "paid"

    with pytest.raises(Crash):
        await Runner(checkpoint_store=checkpoints).run_async(
            agent([charge], [calls("charge")]), "go"
        )
    (run_id,) = await checkpoints.list_incomplete()
    done = await Runner(checkpoint_store=checkpoints).recover_async(
        agent([charge], ["done"]), run_id
    )
    snapshot = await journal.get(ExecutionId("default", run_id))
    assert done.status == "completed" and effects == ["debit"]
    (effect,) = snapshot.effects.values()
    assert effect.status == EffectStatus.CONFIRMED, "completed run retained a pending effect"


async def test_l2_completion_cleanup_crash_leaves_no_orphan_running_row():
    """L2: _delete_checkpoint deletes the checkpoint first and commits the journal's
    terminal status second. A crash in between leaves a journal row 'running' with no
    checkpoint: list_recoverable() lists it forever, recover raises 'No checkpoint found'.
    Correct: commit finish('completed') first (a terminal journal + completed checkpoint is
    already replayed by takeover), then delete."""

    class CrashAfterDelete(MemoryCheckpointStore):
        crash = True

        async def delete(self, run_id: str, owner: str | None = None, *, fenced: bool = False):
            await super().delete(run_id, owner, fenced=fenced)
            if self.crash:
                self.crash = False
                raise Crash()

    checkpoints = CrashAfterDelete()
    with pytest.raises(Crash):
        await Runner(checkpoint_store=checkpoints).run_async(agent([], ["final answer"]), "go")
    journal = await checkpoints.open_execution_store()
    (eid,) = list(journal._runs)
    snapshot = await journal.get(eid)
    has_checkpoint = await checkpoints.load(eid.run_id) is not None
    assert snapshot.run.status == "completed" or has_checkpoint, (
        "journal says running, checkpoint gone: an unrecoverable orphan"
    )


async def test_l3_crash_between_paused_checkpoint_and_journal_pause_heals_on_recover():
    """L3: the paused checkpoint is saved before journal.pause(). A crash in between leaves
    the journal 'running' with a pending effect; every later recover returns the stored
    pause without writing it, so the journal never says paused/indeterminate and the
    run stays in list_recoverable(). Correct: recover re-commits the pause."""

    class CrashPause(MemoryExecutionStore):
        crash = True

        async def commit(self, *args: Any, **kwargs: Any) -> Any:
            if self.crash and kwargs.get("status") == "paused":
                self.crash = False
                raise Crash()
            return await super().commit(*args, **kwargs)

    journal = CrashPause()
    checkpoints = MemoryCheckpointStore(execution_store=journal)
    effects: list[str] = []
    with pytest.raises(Crash):
        await Runner(checkpoint_store=checkpoints).run_async(
            agent([crashing_debit(effects)], [calls("charge")]), "go"
        )
    (run_id,) = await checkpoints.list_incomplete()
    with pytest.raises(Crash):  # dies inside journal.pause, after the paused checkpoint
        await Runner(checkpoint_store=checkpoints).recover_async(
            agent([crashing_debit(effects)], ["done"]), run_id
        )
    again = await Runner(checkpoint_store=checkpoints).recover_async(
        agent([crashing_debit(effects)], ["done"]), run_id
    )
    assert again.status == "paused" and effects == ["debit"]
    snapshot = await journal.get(ExecutionId("default", run_id))
    (effect,) = snapshot.effects.values()
    assert snapshot.run.status == "paused", "journal still 'running' for a paused run"
    assert effect.status == EffectStatus.INDETERMINATE


def test_l4_attempt_suffix_cannot_collide_with_an_opaque_call_id():
    """L4: the attempt suffix '#k' is appended to an opaque provider call id, so attempt 2
    of 'c1' and attempt 1 of a call whose id is 'c1#2' share a StepKey and idempotency key
    (and latest('c1') picks up the other call's step). Correct: unambiguous encoding."""
    assert call_step_key(1, "c1", 2) != call_step_key(1, "c1#2", 1)


async def test_l6_approving_an_unknown_effect_records_its_resolution():
    """L6: resume(approve=...) on an effect_unknown pause re-sends the call as attempt 2
    but never resolves attempt 1: the journal keeps an indeterminate effect on a completed
    run. Correct: approve journals not_applied (actor recorded) before re-sending."""
    store = MemoryCheckpointStore()
    effects: list[str] = []
    with pytest.raises(Crash):
        await Runner(checkpoint_store=store).run_async(
            agent([crashing_debit(effects)], [calls("charge")]), "go"
        )
    (run_id,) = await store.list_incomplete()
    runner = Runner(checkpoint_store=store)
    resumed_agent = agent([crashing_debit(effects)], ["done"])
    paused = await runner.recover_async(resumed_agent, run_id)
    assert paused.interruption.pause_reason == "effect_unknown"
    final = await runner.resume_async(
        resumed_agent, paused.interruption, approve=paused.interruption.pending_digests()
    )
    assert final.status == "completed" and effects == ["debit", "debit"]
    journal = await store.open_execution_store()
    snapshot = await journal.get(ExecutionId("default", run_id))
    first = next(e for e in snapshot.effects.values() if e.intent.step_key == "turn:1/call:c1")
    assert first.status == EffectStatus.RESOLVED, f"attempt 1 left {first.status}"


def test_l7_sdk_resolution_can_name_the_person_who_decided():
    """L7: SDK resolutions are always journaled with actor='sdk:resume'
    (runner.py:2288-2293); resume_async/ResolvedOutcome cannot carry who decided, unlike
    REST (principal) and engine.resolve_effect(actor=). Correct: an actor parameter."""
    fields = {f.name for f in dataclasses.fields(ResolvedOutcome)}
    params = set(inspect.signature(Runner.resume_async).parameters)
    assert "actor" in params or "resolved_by" in fields or "actor" in fields


def test_l8_identity_without_boot_id_does_not_match_another_host(monkeypatch):
    """L8: without /proc (macOS, BSD) the host identity is the hostname alone, and the
    liveness check is os.kill(pid, 0) on THIS host. A lease held by a live process on
    another machine with the same hostname (cloned CI images, 'localhost') is declared
    dead and released. Correct: no boot id / namespace -> never dead (wait for expiry)."""
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()  # this pid is free here; on the other host it is the live owner

    class NoProc:
        def __init__(self, path: Any) -> None:
            self.path = str(path)

        def read_text(self) -> str:
            raise OSError("no /proc on this platform")

        def exists(self) -> bool:
            return False

    real_stat = os.stat

    def stat(path: Any, *args: Any, **kwargs: Any) -> Any:
        if str(path).startswith("/proc"):
            raise OSError("no /proc on this platform")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(durable, "Path", NoProc)
    monkeypatch.setattr(durable.os, "stat", stat)
    monkeypatch.setattr(durable.socket, "gethostname", lambda: "build-mac.local")
    durable._host_identity.cache_clear()
    try:
        other_host_owner = f"nx1.{durable._host_identity()}.{child.pid}.0.{'b' * 32}"
        dead = durable.lease_owner_is_dead(other_host_owner)
    finally:
        durable._host_identity.cache_clear()
    assert not dead, "an owner on another host with the same hostname was declared dead"


async def test_l9_lost_lease_cancels_the_running_local_task():
    """L9: docs/durable-execution.md says 'Failed renewal cancels the local task'. The
    heartbeat (_agent_journal.py:254-262) only records the loss and returns; the tool,
    and any pure tools / model work until the next checkpoint, keep running."""
    offset = [0.0]
    journal = MemoryExecutionStore(clock=lambda: time.time() + offset[0])
    checkpoints = MemoryCheckpointStore(execution_store=journal)
    started = asyncio.Event()
    finished: list[bool] = []

    async def slow() -> str:
        """A local fake long call."""
        started.set()
        await asyncio.sleep(1.5)
        finished.append(True)
        return "ok"

    runner = Runner(checkpoint_store=checkpoints, lease_ttl=0.6)
    task = asyncio.create_task(
        runner.run_async(
            agent([tool(name="slow", effect="idempotent")(slow)], [calls("slow"), "done"]), "go"
        )
    )
    await asyncio.wait_for(started.wait(), 5)
    run_id = next(iter(checkpoints._data))
    eid = ExecutionId("default", run_id)
    offset[0] += 1000.0  # store clock: the lease has expired
    digest = (await journal.get(eid)).run.definition_digest
    assert await journal.claim(eid, "other-worker", 60, definition_digest=digest) is not None
    with contextlib.suppress(BaseException):
        await asyncio.wait_for(task, 10)
    assert finished == [], "the tool kept running after the run lease was lost"


def test_l10_docs_name_the_clock_the_code_uses():
    """L10: docs say 'Postgres uses now()' for lease expiry; PostgresExecutionStore uses
    clock_timestamp() (correct: now() is transaction start and could revive a lease after a
    lock wait). The doc should say clock_timestamp()."""
    text = (ROOT / "docs" / "durable-execution.md").read_text()
    assert "Postgres uses `now()`" not in text


# =============================================================== additional checks of the fixes


def test_sqlite_schema_1_file_is_migrated_to_workspace_scoped_tables(tmp_path):
    """H2: a schema-1 file keeps its checkpoints, claims and approvals under 'default'."""
    import sqlite3

    path = tmp_path / "v1.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE nexura_meta (k TEXT PRIMARY KEY, v TEXT)")
    conn.execute("INSERT INTO nexura_meta VALUES ('checkpoint_schema', '1')")
    conn.execute(
        "CREATE TABLE nexura_run_checkpoints (run_id TEXT PRIMARY KEY, data TEXT NOT NULL, "
        "updated_at REAL NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE nexura_run_claims (run_id TEXT PRIMARY KEY, owner TEXT NOT NULL, "
        "expires_at REAL NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE nexura_sdk_approvals (approval_id TEXT PRIMARY KEY, payload TEXT NOT NULL, "
        "expires_at REAL NOT NULL, consumed_at REAL)"
    )
    conn.execute("INSERT INTO nexura_run_checkpoints VALUES ('r1', 'old', 1.0)")
    conn.execute("INSERT INTO nexura_run_claims VALUES ('r1', 'o1', ?)", (time.time() + 60,))
    conn.execute(
        "INSERT INTO nexura_sdk_approvals VALUES ('a1', 'p', ?, NULL)", (time.time() + 60,)
    )
    conn.commit()
    conn.close()

    default = SQLiteCheckpointStore(path)
    other = SQLiteCheckpointStore(path, workspace_id="other")
    try:
        assert asyncio.run(default.load("r1")) == "old"
        assert asyncio.run(default.list_incomplete()) == ["r1"]
        assert not asyncio.run(default.claim("r1", "o2", 60))  # the migrated claim holds
        assert asyncio.run(default.load_approval("a1")) == "p"
        assert asyncio.run(other.load("r1")) is None
        assert asyncio.run(other.list_incomplete()) == []
        assert asyncio.run(other.claim("r1", "o2", 60))  # its own claim row
        assert asyncio.run(other.load_approval("a1")) is None
    finally:
        default.close()
        other.close()
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT v FROM nexura_meta").fetchone() == ("2",)
    conn.close()


def test_step_keys_escape_ids_and_parse_both_forms():
    """L4: the escaped encoding round-trips; keys written before escaping still parse."""
    from nexura_agents._agent_journal import parse_call_step_key

    for call_id in ("c1", "c1#2", "50%", "a%23b", "#"):
        for attempt in (1, 3):
            key = call_step_key(4, call_id, attempt)
            assert parse_call_step_key(key) == (4, call_id, attempt)
    assert call_step_key(1, "c1") == "turn:1/call:c1"  # plain ids keep their key
    assert parse_call_step_key("turn:1/call:c1#2") == (1, "c1", 2)  # pre-escaping form
    with pytest.raises(ValueError):
        parse_call_step_key("turn:1/model")


async def test_resolution_and_approval_record_the_named_actor():
    """L6/L7: resume(actor=...) is recorded on outcome resolutions and on approvals."""
    store = MemoryCheckpointStore()
    effects: list[str] = []
    with pytest.raises(Crash):
        await Runner(checkpoint_store=store).run_async(
            agent([crashing_debit(effects)], [calls("charge")]), "go"
        )
    (run_id,) = await store.list_incomplete()
    runner = Runner(checkpoint_store=store)
    resumed_agent = agent([crashing_debit(effects)], ["done"])
    paused = await runner.recover_async(resumed_agent, run_id)
    with pytest.raises(Exception, match="actor"):
        await runner.resume_async(
            resumed_agent, paused.interruption, outcomes={"c1": ResolvedOutcome("x")}, actor=" "
        )
    final = await runner.resume_async(
        resumed_agent,
        paused.interruption,
        outcomes={"c1": ResolvedOutcome("paid")},
        actor="ops@example.test",
    )
    assert final.status == "completed" and effects == ["debit"]
    journal = await store.open_execution_store()
    (effect,) = (await journal.get(ExecutionId("default", run_id))).effects.values()
    assert effect.resolution == EffectResolution.CONFIRMED
    assert effect.resolved_by == "ops@example.test"


def test_hidden_pid_that_is_really_gone_is_dead(monkeypatch):
    """M2: when /proc hides the pid, kill(pid, 0) decides; ESRCH (gone) is still dead."""
    if not Path("/proc/self/stat").exists():
        pytest.skip("Linux /proc only")
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    owner = f"nx1.{durable._host_identity()}.{child.pid}.0.{'c' * 32}"

    def hidden(target: int) -> list[str]:
        raise FileNotFoundError(f"/proc/{target}/stat")

    monkeypatch.setattr(durable, "_proc_stat", hidden)
    assert durable.lease_owner_is_dead(owner)
