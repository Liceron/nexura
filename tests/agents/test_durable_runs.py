"""Durable agent runs: checkpoints, crash recovery and uncertain tool calls. All offline."""

from __future__ import annotations

import os
import sqlite3
import stat
from pathlib import Path
from typing import Any

import pytest
from nexura_agents import Agent as _Agent
from nexura_agents import (
    FakeModel,
    MemoryCheckpointStore,
    Runner,
    RunRecoveryError,
    SQLiteCheckpointStore,
    ToolCall,
    UsageLimits,
    tool,
)
from nexura_agents.durable import encode_checkpoint
from nexura_agents.errors import (
    CheckpointLeaseLostError,
    ProviderError,
    SDKValidationError,
    UsageLimitExceededError,
)
from nexura_agents.handoffs import Handoff
from nexura_agents.hooks import AgentHooks
from nexura_agents.types import Usage


def Agent(**kwargs: Any) -> _Agent:  # noqa: N802
    kwargs.setdefault("instructions", "Be helpful.")
    return _Agent(**kwargs)


class Crash(BaseException):
    """Simulated process death: not an Exception, so the runner leaves the checkpoint."""


def call(name: str, call_id: str = "c1", **arguments: Any) -> list[ToolCall]:
    return [ToolCall(id=call_id, name=name, arguments=arguments)]


def scripted(script: list[Any], crash_on: int | None = None) -> FakeModel:
    """A model replaying ``script``; raises ``Crash`` on the (0-based) call ``crash_on``."""
    seen = {"n": 0}

    def handler(messages: Any, tools: Any) -> Any:
        n = seen["n"]
        seen["n"] += 1
        if crash_on is not None and n == crash_on:
            raise Crash
        return script[n if crash_on is None else n]

    return FakeModel(handler=handler)


def counters() -> dict[str, int]:
    return {"a": 0, "b": 0}


def make_tools(c: dict[str, int], crash_in: str | None = None, idempotent: bool = False) -> list:
    @tool(idempotent=idempotent)
    def a(x: str) -> str:
        """Tool a."""
        c["a"] += 1
        if crash_in == "a":
            raise Crash
        return f"a:{x}"

    @tool
    def b(x: str) -> str:
        """Tool b."""
        c["b"] += 1
        return f"b:{x}"

    return [a, b]


def run_id_of(store: MemoryCheckpointStore) -> str:
    return next(iter(store._data))


# ---------------------------------------------------------------- model_next


async def test_crash_before_model_recovers_without_rerunning_tools() -> None:
    c = counters()
    store = MemoryCheckpointStore()
    script = [call("a", x="1"), "done"]
    # Call 0 returns the tool call; call 1 (the follow-up model call) crashes.
    seen = {"n": 0}

    def handler(messages: Any, tools: Any) -> Any:
        n = seen["n"]
        seen["n"] += 1
        if n == 1:
            raise Crash
        return script[n]

    agent = Agent(name="A", model=FakeModel(handler=handler), tools=make_tools(c))
    with pytest.raises(Crash):
        await Runner(checkpoint_store=store).run_async(agent, "go")
    assert c["a"] == 1
    run_id = run_id_of(store)
    assert "model_next" in (await store.load(run_id) or "")

    agent2 = Agent(name="A", model=FakeModel(responses=["done"]), tools=make_tools(c))
    result = await Runner(checkpoint_store=store).recover_async(agent2, run_id)
    assert result.status == "completed"
    assert result.final_output == "done"
    assert c["a"] == 1  # finished tool was not re-run
    assert await store.list_incomplete() == []
    assert any(m.role == "tool" and m.content == "a:1" for m in result.messages)


async def test_crash_in_first_model_call_recovers() -> None:
    store = MemoryCheckpointStore()
    agent = Agent(name="A", model=scripted(["x"], crash_on=0))
    with pytest.raises(Crash):
        await Runner(checkpoint_store=store).run_async(agent, "hi")
    agent2 = Agent(name="A", model=FakeModel(responses=["recovered"]))
    result = await Runner(checkpoint_store=store).recover_async(agent2, run_id_of(store))
    assert result.final_output == "recovered"
    assert result.messages[-2].content == "hi"


# ---------------------------------------------------------------- mid-tools


async def _crash_mid_tool(
    store: Any, c: dict[str, int], idempotent: bool, extra_calls: bool = False
) -> str:
    calls = call("a", x="1")
    if extra_calls:
        calls = [*calls, *call("b", "c2", x="2")]
    agent = Agent(
        name="A",
        model=FakeModel(responses=[calls, "done"]),
        tools=make_tools(c, crash_in="a", idempotent=idempotent),
        parallel_tool_calls=False,
    )
    with pytest.raises(Crash):
        await Runner(checkpoint_store=store).run_async(agent, "go")
    return run_id_of(store) if isinstance(store, MemoryCheckpointStore) else ""


async def test_idempotent_tool_is_rerun_on_recover() -> None:
    c, store = counters(), MemoryCheckpointStore()
    run_id = await _crash_mid_tool(store, c, idempotent=True)
    agent = Agent(
        name="A", model=FakeModel(responses=["done"]), tools=make_tools(c, idempotent=True)
    )
    result = await Runner(checkpoint_store=store).recover_async(agent, run_id)
    assert result.status == "completed"
    assert c["a"] == 2


async def test_non_idempotent_tool_pauses_then_approve_reruns() -> None:
    c, store = counters(), MemoryCheckpointStore()
    run_id = await _crash_mid_tool(store, c, idempotent=False)
    agent = Agent(name="A", model=FakeModel(responses=["done"]), tools=make_tools(c))
    runner = Runner(checkpoint_store=store)
    paused = await runner.recover_async(agent, run_id)
    assert paused.status == "paused"
    assert paused.interruption is not None
    assert paused.interruption.reason == "outcome_unknown_after_crash"
    assert [p.name for p in paused.interruption.pending] == ["a"]
    assert c["a"] == 1
    # The pause is persisted: a fresh recover returns it again.
    again = await Runner(checkpoint_store=store).recover_async(agent, run_id)
    assert again.status == "paused"

    interruption = paused.interruption
    final = await runner.resume_async(agent, interruption, approve=interruption.pending_digests())
    assert final.status == "completed"
    assert c["a"] == 2
    assert await store.list_incomplete() == []


async def test_non_idempotent_reject_tells_model_not_retried() -> None:
    c, store = counters(), MemoryCheckpointStore()
    run_id = await _crash_mid_tool(store, c, idempotent=False)
    model = FakeModel(responses=["ok"])
    agent = Agent(name="A", model=model, tools=make_tools(c))
    runner = Runner(checkpoint_store=store)
    paused = await runner.recover_async(agent, run_id)
    assert paused.interruption is not None
    final = await runner.resume_async(agent, paused.interruption, reject={"c1": "no"})
    assert final.status == "completed"
    assert c["a"] == 1
    tool_msgs = [m for m in model.calls[-1]["messages"] if m.role == "tool"]
    assert "unknown" in (tool_msgs[-1].content or "")
    assert "not retried" in (tool_msgs[-1].content or "")


async def test_pause_interruption_survives_json() -> None:
    c, store = counters(), MemoryCheckpointStore()
    run_id = await _crash_mid_tool(store, c, idempotent=False, extra_calls=True)
    agent = Agent(name="A", model=FakeModel(responses=["done"]), tools=make_tools(c))
    runner = Runner(checkpoint_store=store)
    paused = await runner.recover_async(agent, run_id)
    assert paused.interruption is not None
    from nexura_agents import RunInterruption

    restored = RunInterruption.from_json(paused.interruption.to_json())
    assert restored.reason == "outcome_unknown_after_crash"
    final = await runner.resume_async(agent, restored, approve=True)
    assert final.status == "completed"
    assert c["a"] == 2
    assert c["b"] == 1  # the unstarted call ran once, on resume


async def test_on_uncertain_fail() -> None:
    c, store = counters(), MemoryCheckpointStore()
    run_id = await _crash_mid_tool(store, c, idempotent=False)
    agent = Agent(name="A", model=FakeModel(responses=["done"]), tools=make_tools(c))
    with pytest.raises(RunRecoveryError, match="unknown"):
        await Runner(checkpoint_store=store).recover_async(agent, run_id, on_uncertain="fail")
    assert c["a"] == 1


class _NoJournalStore(MemoryCheckpointStore):
    """A checkpoint store without an execution journal (like a custom store): nothing
    records which calls of a batch started, so every unanswered call is uncertain."""

    open_execution_store = None  # type: ignore[assignment]


async def test_on_uncertain_tell_model() -> None:
    # A6 S5: with a journal the never-started sibling "b" is known not to have run and runs
    # on recovery (tests/agents/test_s5_runner_journal.py). Without one, it stays uncertain.
    c, store = counters(), _NoJournalStore()
    run_id = await _crash_mid_tool(store, c, idempotent=False, extra_calls=True)
    model = FakeModel(responses=["finished"])
    agent = Agent(name="A", model=model, tools=make_tools(c))
    result = await Runner(checkpoint_store=store).recover_async(
        agent, run_id, on_uncertain="tell_model"
    )
    assert result.status == "completed"
    assert c["a"] == 1
    assert c["b"] == 0  # also non-idempotent, so also uncertain
    results = [m.content for m in model.calls[-1]["messages"] if m.role == "tool"]
    assert len(results) == 2
    assert all("unknown" in (r or "") for r in results)


async def test_invalid_on_uncertain_rejected() -> None:
    agent = Agent(name="A", model=FakeModel(responses=["x"]))
    with pytest.raises(SDKValidationError):
        await Runner(checkpoint_store=MemoryCheckpointStore()).recover_async(
            agent, "r", on_uncertain="maybe"
        )


# ---------------------------------------------------------------- new process / sqlite


async def test_recovery_in_new_process_with_sqlite(tmp_path: Path) -> None:
    path = tmp_path / "ckpt" / "runs.db"
    c = counters()
    store = SQLiteCheckpointStore(path)
    agent = Agent(
        name="A",
        model=FakeModel(responses=[call("a", x="1"), "never"]),
        tools=make_tools(c, crash_in="a", idempotent=True),
    )
    with pytest.raises(Crash):
        await Runner(checkpoint_store=store).run_async(agent, "go")
    ids = await store.list_incomplete()
    assert len(ids) == 1
    store.close()

    # "New process": fresh store, runner, agent, tools and model.
    c2 = counters()
    store2 = SQLiteCheckpointStore(path)
    agent2 = Agent(
        name="A", model=FakeModel(responses=["all done"]), tools=make_tools(c2, idempotent=True)
    )
    result = await Runner(checkpoint_store=store2).recover_async(agent2, ids[0])
    assert result.final_output == "all done"
    assert c2["a"] == 1
    assert await store2.list_incomplete() == []
    store2.close()


async def test_sqlite_file_is_owner_only(tmp_path: Path) -> None:
    old = os.umask(0)
    try:
        store = SQLiteCheckpointStore(tmp_path / "c.db")
        await store.save("r1", "{}")
    finally:
        os.umask(old)
    assert stat.S_IMODE(os.stat(tmp_path / "c.db").st_mode) == 0o600
    store.close()


async def test_sqlite_store_roundtrip_and_schema(tmp_path: Path) -> None:
    store = SQLiteCheckpointStore(tmp_path / "c.db")
    assert await store.load("x") is None
    await store.save("x", "one")
    await store.save("x", "two")  # atomic upsert
    await store.save("y", "three")
    assert await store.load("x") == "two"
    assert sorted(await store.list_incomplete()) == ["x", "y"]
    assert len(await store.list_incomplete(limit=1)) == 1
    await store.delete("x")
    await store.delete("x")  # idempotent
    assert await store.list_incomplete() == ["y"]
    store.close()
    conn = sqlite3.connect(tmp_path / "c.db")
    conn.execute("UPDATE nexura_meta SET v = '99'")
    conn.commit()
    conn.close()
    from nexura_agents.errors import StorageError

    with pytest.raises(StorageError):
        SQLiteCheckpointStore(tmp_path / "c.db")


# ---------------------------------------------------------------- limits, handoffs, approvals


async def test_usage_and_limits_carry_over() -> None:
    store = MemoryCheckpointStore()
    usage = Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15)
    seen = {"n": 0}

    def handler(messages: Any, tools: Any) -> Any:
        seen["n"] += 1
        if seen["n"] == 2:
            raise Crash
        return call("a", x="1")

    c = counters()
    agent = Agent(
        name="A", model=FakeModel(handler=handler, default_usage=usage), tools=make_tools(c)
    )
    runner = Runner(checkpoint_store=store, usage_limits=UsageLimits(request_limit=2))
    with pytest.raises(Crash):
        await runner.run_async(agent, "go")
    agent2 = Agent(
        name="A", model=FakeModel(responses=["x"], default_usage=usage), tools=make_tools(c)
    )
    # One request was already made: the second is allowed, a third would not be.
    done = await Runner(
        checkpoint_store=store, usage_limits=UsageLimits(request_limit=2)
    ).recover_async(agent2, run_id_of(store))
    assert done.usage.total_tokens == 30
    assert done.status == "completed"

    store3 = MemoryCheckpointStore()
    seen["n"] = 0
    agent3 = Agent(
        name="A", model=FakeModel(handler=handler, default_usage=usage), tools=make_tools(c)
    )
    with pytest.raises(Crash):
        await Runner(checkpoint_store=store3, usage_limits=UsageLimits(request_limit=1)).run_async(
            agent3, "go"
        )
    agent4 = Agent(
        name="A", model=FakeModel(responses=["x"], default_usage=usage), tools=make_tools(c)
    )
    with pytest.raises(UsageLimitExceededError):
        await Runner(
            checkpoint_store=store3, usage_limits=UsageLimits(request_limit=1)
        ).recover_async(agent4, run_id_of(store3))


async def test_handoff_active_agent_restored() -> None:
    store = MemoryCheckpointStore()

    def build(model_b: FakeModel) -> Agent:
        specialist = Agent(name="Specialist", model=model_b, instructions="You are the specialist.")
        return Agent(
            name="Triage",
            model=FakeModel(responses=[call("transfer_to_specialist")]),
            handoffs=[Handoff(specialist)],
        )

    seen = {"n": 0}

    def crashing(messages: Any, tools: Any) -> Any:
        seen["n"] += 1
        raise Crash

    agent = build(FakeModel(handler=crashing))
    with pytest.raises(Crash):
        await Runner(checkpoint_store=store).run_async(agent, "help")
    # The handoff model also crashed on the specialist's first call.
    specialist_model = FakeModel(responses=["specialist here"])
    agent2 = build(specialist_model)
    result = await Runner(checkpoint_store=store).recover_async(agent2, run_id_of(store))
    assert result.agent_path == ["Triage", "Specialist"]
    assert result.final_output == "specialist here"
    sent = specialist_model.calls[0]["messages"]
    assert sent[0].role == "system" and "specialist" in (sent[0].content or "")


async def test_crash_after_handoff_tool_before_apply_restores_handoff() -> None:
    store = MemoryCheckpointStore()

    class CrashAfterTool(AgentHooks):
        async def after_tool(self, ctx: Any, call: Any, result: str) -> None:
            raise Crash

    def build(responses: list[Any]) -> Agent:
        specialist = Agent(name="Specialist", model=FakeModel(responses=responses))
        return Agent(
            name="Triage",
            model=FakeModel(responses=[[*call("transfer_to_specialist"), *call("b", "c2", x="2")]]),
            handoffs=[Handoff(specialist)],
            tools=make_tools(counters()),
            parallel_tool_calls=False,
        )

    with pytest.raises(Crash):
        await Runner(checkpoint_store=store, hooks=CrashAfterTool()).run_async(
            build(["never"]), "help"
        )
    # b is non-idempotent and uncertain; tell the model and continue on the specialist.
    result = await Runner(checkpoint_store=store).recover_async(
        build(["from specialist"]), run_id_of(store), on_uncertain="tell_model"
    )
    assert result.agent_path == ["Triage", "Specialist"]
    assert result.final_output == "from specialist"


async def test_approval_pause_and_resume_with_checkpointing() -> None:
    store = MemoryCheckpointStore()
    c = counters()

    @tool(requires_approval=True)
    def a(x: str) -> str:
        """Risky."""
        c["a"] += 1
        return "ok"

    agent = Agent(name="A", model=FakeModel(responses=[call("a", x="1"), "done"]), tools=[a])
    runner = Runner(checkpoint_store=store)
    paused = await runner.run_async(agent, "go")
    assert paused.status == "paused"
    assert await store.list_incomplete() == [paused.run_id]
    assert '"phase": "paused"' in (await store.load(paused.run_id) or "")
    # Recovering a paused run hands the pending approval back.
    again = await runner.recover_async(agent, paused.run_id)
    assert again.status == "paused" and again.interruption is not None
    final = await runner.resume_async(
        agent, again.interruption, approve=again.interruption.pending_digests()
    )
    assert final.status == "completed"
    assert c["a"] == 1
    assert await store.list_incomplete() == []


async def test_stream_checkpoints_and_recovers() -> None:
    store = MemoryCheckpointStore()
    c = counters()
    agent = Agent(
        name="A",
        model=FakeModel(responses=[call("a", x="1"), "never"]),
        tools=make_tools(c, crash_in="a", idempotent=True),
    )
    runner = Runner(checkpoint_store=store)
    with pytest.raises(Crash):
        async for _ in runner.stream_async(agent, "go"):
            pass
    agent2 = Agent(
        name="A", model=FakeModel(responses=["fin"]), tools=make_tools(c, idempotent=True)
    )
    types = [e.type.value async for e in runner.recover_stream_async(agent2, run_id_of(store))]
    assert types[-1] == "run_completed"


# ---------------------------------------------------------------- lifecycle and errors


async def test_completed_and_failed_runs_leave_no_checkpoint() -> None:
    store = MemoryCheckpointStore()
    ok = Agent(name="A", model=FakeModel(responses=["fine"]))
    await Runner(checkpoint_store=store).run_async(ok, "hi")
    assert await store.list_incomplete() == []

    @tool
    def boom() -> str:
        """Fails."""
        raise ValueError("nope")

    bad = Agent(name="A", model=FakeModel(responses=[call("boom"), "x"]), tools=[boom])
    with pytest.raises(Exception, match="boom|nope"):
        await Runner(checkpoint_store=store).run_async(bad, "hi")
    assert await store.list_incomplete() == []


async def test_unknown_run_id() -> None:
    agent = Agent(name="A", model=FakeModel(responses=["x"]))
    with pytest.raises(RunRecoveryError, match="No checkpoint"):
        await Runner(checkpoint_store=MemoryCheckpointStore()).recover_async(agent, "missing")


async def test_recover_without_store_is_an_error() -> None:
    agent = Agent(name="A", model=FakeModel(responses=["x"]))
    with pytest.raises(SDKValidationError):
        await Runner().recover_async(agent, "r")


async def _crashed_store() -> tuple[MemoryCheckpointStore, str]:
    store = MemoryCheckpointStore()
    agent = Agent(name="A", model=scripted(["x"], crash_on=0))
    with pytest.raises(Crash):
        await Runner(checkpoint_store=store).run_async(agent, "hi")
    return store, run_id_of(store)


async def test_corrupt_checkpoint_raises() -> None:
    store, run_id = await _crashed_store()
    agent = Agent(name="A", model=FakeModel(responses=["x"]))
    for bad in ("not json", "{}", '{"schema_version": 1, "phase": "weird", "interruption": "{}"}'):
        await store.save(run_id, bad)
        with pytest.raises(RunRecoveryError, match="corrupt"):
            await Runner(checkpoint_store=store).recover_async(agent, run_id)


async def test_mismatched_checkpoint_raises() -> None:
    store, run_id = await _crashed_store()
    other = Agent(name="Other", model=FakeModel(responses=["x"]))
    with pytest.raises(RunRecoveryError, match="belongs to agent"):
        await Runner(checkpoint_store=store).recover_async(other, run_id)
    # Stored under the wrong run id.
    raw = await store.load(run_id) or ""
    await store.save("other-id", raw)
    agent = Agent(name="A", model=FakeModel(responses=["x"]))
    with pytest.raises(RunRecoveryError, match="belongs to run"):
        await Runner(checkpoint_store=store).recover_async(agent, "other-id")


async def test_unreachable_active_agent_raises() -> None:
    store = MemoryCheckpointStore()
    run_id = "r-1"
    from nexura_agents.types import Message, RunInterruption

    interruption = RunInterruption(
        run_id=run_id,
        agent_name="A",
        messages=[Message(role="user", content="hi")],
        pending=[],
        active_agent="Ghost",
        agent_path=["A", "Ghost"],
    )
    await store.save(run_id, encode_checkpoint("model_next", interruption.to_json()))
    agent = Agent(name="A", model=FakeModel(responses=["x"]))
    with pytest.raises(RunRecoveryError, match="not reachable"):
        await Runner(checkpoint_store=store).recover_async(agent, run_id)


# ---------------------------------------------------------------- zero overhead


async def test_no_store_calls_when_off() -> None:
    class Spy(MemoryCheckpointStore):
        calls = 0

        async def save(self, run_id: str, checkpoint_json: str) -> None:
            Spy.calls += 1

    c = counters()
    agent = Agent(
        name="A", model=FakeModel(responses=[call("a", x="1"), "done"]), tools=make_tools(c)
    )
    runner = Runner()  # no checkpoint_store
    assert runner.checkpoint_store is None
    result = await runner.run_async(agent, "go")
    assert result.status == "completed"
    assert Spy.calls == 0
    # The off path returns the plain loop generator, not the durable wrapper.
    assert runner._loop.__func__ is Runner._loop  # type: ignore[attr-defined]


def test_sync_recover(tmp_path: Path) -> None:
    store = SQLiteCheckpointStore(tmp_path / "s.db")
    agent = Agent(name="A", model=scripted(["x"], crash_on=0))
    runner = Runner(checkpoint_store=store)
    with pytest.raises(Crash):
        runner.run(agent, "hi")
    import asyncio

    run_id = asyncio.run(store.list_incomplete())[0]
    done = runner.recover(Agent(name="A", model=FakeModel(responses=["ok"])), run_id)
    assert done.final_output == "ok"
    store.close()


# ---------------------------------------------------------------- review fixes


async def test_concurrent_recoverers_only_one_continues() -> None:
    import asyncio

    n = {"t": 0}

    @tool
    def charge(x: str) -> str:
        """Charge."""
        n["t"] += 1
        return "ok"

    store = MemoryCheckpointStore()
    seen = {"i": 0}

    def handler(messages: Any, tools: Any) -> Any:
        seen["i"] += 1
        if seen["i"] == 1:
            return call("charge", x="1")
        raise Crash

    first = Agent(name="A", model=FakeModel(handler=handler), tools=[charge])
    with pytest.raises(Crash):
        await Runner(checkpoint_store=store).run_async(first, "go")
    run_id = run_id_of(store)

    class Slow(MemoryCheckpointStore):
        async def load(self, run_id: str) -> str | None:
            raw = await super().load(run_id)
            await asyncio.sleep(0.01)
            return raw

    slow = Slow()
    slow._data = dict(store._data)

    def mk() -> Agent:
        return Agent(name="A", model=FakeModel(responses=["done"]), tools=[charge])

    results = await asyncio.gather(
        Runner(checkpoint_store=slow).recover_async(mk(), run_id),
        Runner(checkpoint_store=slow).recover_async(mk(), run_id),
        return_exceptions=True,
    )
    errors = [r for r in results if isinstance(r, RunRecoveryError)]
    done = [r for r in results if not isinstance(r, BaseException)]
    assert len(errors) == 1 and len(done) == 1
    assert done[0].status == "completed"


async def test_sqlite_claim_is_exclusive_and_releasable(tmp_path: Path) -> None:
    store = SQLiteCheckpointStore(tmp_path / "c.db")
    try:
        assert await store.claim("r", "o1", 60)
        assert not await store.claim("r", "o2", 60)
        assert await store.claim("r", "o1", 60)
        await store.release("r", "o1")
        assert await store.claim("r", "o2", 60)
        assert await store.claim("x", "o1", 0.0)
        assert await store.claim("x", "o2", 60)  # expired lease can be taken over
    finally:
        store.close()


async def test_crash_after_session_save_recovers_without_second_save() -> None:
    from nexura_agents.session import MemorySessionStore

    class H(AgentHooks):
        armed = True

        async def on_run_end(self, ctx: Any, output: Any) -> None:
            if H.armed:
                H.armed = False
                raise Crash

    store, sessions = MemoryCheckpointStore(), MemorySessionStore()
    model = FakeModel(responses=["final"])
    with pytest.raises(Crash):
        await Runner(checkpoint_store=store, session_store=sessions, hooks=H()).run_async(
            Agent(name="A", model=model), "go", session_id="s"
        )
    calls_before = len(model.calls) if hasattr(model, "calls") else None
    again = FakeModel(responses=["different"])
    result = await Runner(checkpoint_store=store, session_store=sessions).recover_async(
        Agent(name="A", model=again), run_id_of(store)
    )
    assert result.status == "completed"
    assert result.final_output == "final"
    assert await store.list_incomplete() == []
    snap = await sessions.get("s")
    assert snap is not None and snap.revision == 1
    del calls_before


async def test_checkpoint_save_failure_keeps_last_good_checkpoint() -> None:
    class Flaky(MemoryCheckpointStore):
        fail = False

        async def save(self, run_id: str, checkpoint_json: str) -> None:
            if self.fail:
                raise OSError("disk busy")
            await super().save(run_id, checkpoint_json)

    store = Flaky()

    @tool
    def t(x: str) -> str:
        """T."""
        store.fail = True
        return "ok"

    agent = Agent(name="A", model=FakeModel(responses=[call("t", x="1"), "never"]), tools=[t])
    with pytest.raises(OSError):
        await Runner(checkpoint_store=store).run_async(agent, "go")
    assert await store.list_incomplete()


async def test_failing_delete_on_completion_does_not_fail_run() -> None:
    class BadDelete(MemoryCheckpointStore):
        async def delete(self, run_id: str) -> None:
            raise OSError("db locked")

    agent = Agent(name="A", model=FakeModel(responses=["final"]))
    result = await Runner(checkpoint_store=BadDelete()).run_async(agent, "go")
    assert result.status == "completed"
    assert result.final_output == "final"


async def test_on_handoff_callback_not_repeated_after_crash() -> None:
    n = {"h": 0}

    def cb(ctx: Any, reason: Any) -> None:
        n["h"] += 1
        if n["h"] == 1:
            raise Crash  # process dies while the callback runs

    def build(responses: list[Any], first: list[Any]) -> Agent:
        target = Agent(name="T", model=FakeModel(responses=responses))
        h = Handoff(target, on_handoff=cb)
        return Agent(
            name="S",
            model=FakeModel(responses=first),
            handoffs=[h],
        )

    store = MemoryCheckpointStore()
    src = build(["t"], [call("transfer_to_t")])
    with pytest.raises(Crash):
        await Runner(checkpoint_store=store).run_async(src, "go")
    assert n["h"] == 1
    result = await Runner(checkpoint_store=store).recover_async(
        build(["t"], ["x"]), run_id_of(store)
    )
    assert result.final_output == "t"
    assert n["h"] == 1


# ---------------------------------------------------------------- fenced saves (F13b)


def _stores(tmp_path: Path) -> list[Any]:
    return [MemoryCheckpointStore(), SQLiteCheckpointStore(tmp_path / "fence.db")]


async def test_save_is_fenced_by_the_recovery_lease(tmp_path: Path) -> None:
    for store in _stores(tmp_path):
        await store.save("r", "v0")  # no lease: anyone may write
        assert await store.claim("r", "o1", 60)
        for intruder in (None, "o2"):
            with pytest.raises(CheckpointLeaseLostError):
                await store.save("r", "stale", owner=intruder)
        await store.save("r", "v1", owner="o1")
        assert await store.load("r") == "v1"
        with pytest.raises(CheckpointLeaseLostError):  # a lease that is gone fences its owner
            await store.save("nobody-claimed", "x", owner="o1")
        assert await store.load("nobody-claimed") is None


async def test_stalled_recoverer_cannot_overwrite_after_takeover(tmp_path: Path) -> None:
    for store in _stores(tmp_path):
        await store.save("r", "v0")
        assert await store.claim("r", "slow", 0.0)  # lease already expired: it stalled
        assert await store.claim("r", "fresh", 60)
        await store.save("r", "fresh-progress", owner="fresh")
        with pytest.raises(CheckpointLeaseLostError):
            await store.save("r", "stale", owner="slow")
        assert await store.load("r") == "fresh-progress"
        await store.release("r", "fresh")
        await store.save("r", "after-release")  # a writer without a lease is free again
        assert await store.claim("x", "gone", 0.0)
        await store.save("x", "ok")  # an expired lease nobody renewed does not block
        if isinstance(store, SQLiteCheckpointStore):
            store.close()


async def test_save_by_lease_holder_renews_the_lease(tmp_path: Path) -> None:
    import asyncio

    for store in _stores(tmp_path):
        assert await store.claim("r", "o1", 0.6)
        await asyncio.sleep(0.4)
        await store.save("r", "progress", owner="o1")
        await asyncio.sleep(0.4)  # past the original expiry, within the renewed one
        assert not await store.claim("r", "o2", 60)
        if isinstance(store, SQLiteCheckpointStore):
            store.close()


async def test_recoverer_that_lost_its_lease_stops_and_keeps_checkpoint() -> None:
    import time

    store = MemoryCheckpointStore()
    agent = Agent(name="A", model=scripted(["x"], crash_on=0))
    with pytest.raises(Crash):
        await Runner(checkpoint_store=store).run_async(agent, "go")
    run_id = run_id_of(store)
    before = await store.load(run_id)

    def handler(messages: Any, tools: Any) -> Any:
        # While this recoverer waits on the model, another caller takes the run over.
        store._claims[run_id] = ("other", time.time() + 60, 60.0)
        return "late answer"

    late = Agent(name="A", model=FakeModel(handler=handler))
    with pytest.raises(CheckpointLeaseLostError) as info:
        await Runner(checkpoint_store=store).recover_async(late, run_id)
    assert isinstance(info.value, RunRecoveryError)
    assert await store.load(run_id) == before  # neither overwritten nor deleted
    assert store._claims[run_id][0] == "other"


async def test_original_run_is_fenced_once_a_recoverer_holds_the_lease() -> None:
    import time

    n = {"t": 0}

    @tool
    def lookup(x: str) -> str:
        """Lookup."""
        n["t"] += 1
        return "found"

    store = MemoryCheckpointStore()
    seen = {"i": 0}

    def handler(messages: Any, tools: Any) -> Any:
        seen["i"] += 1
        if seen["i"] == 1:
            return call("lookup", x="1")
        # The original process was slow, not dead: a recoverer has claimed the run meanwhile.
        store._claims[run_id_of(store)] = ("recoverer", time.time() + 60, 60.0)
        return "slow answer"

    agent = Agent(name="A", model=FakeModel(handler=handler), tools=[lookup])
    with pytest.raises(CheckpointLeaseLostError):
        await Runner(checkpoint_store=store).run_async(agent, "go")
    assert run_id_of(store)  # the recoverer's checkpoint is still there


async def test_custom_store_without_fencing_still_recovers() -> None:
    class Plain:
        def __init__(self) -> None:
            self.data: dict[str, str] = {}

        async def save(self, run_id: str, checkpoint_json: str) -> None:
            self.data[run_id] = checkpoint_json

        async def load(self, run_id: str) -> str | None:
            return self.data.get(run_id)

        async def delete(self, run_id: str) -> None:
            self.data.pop(run_id, None)

        async def list_incomplete(self, limit: int = 100) -> list[str]:
            return list(self.data)[:limit]

    store = Plain()
    agent = Agent(name="A", model=scripted(["x"], crash_on=0))
    with pytest.raises(Crash):
        await Runner(checkpoint_store=store).run_async(agent, "go")
    run_id = next(iter(store.data))
    done = await Runner(checkpoint_store=store).recover_async(
        Agent(name="A", model=FakeModel(responses=["done"])), run_id
    )
    assert done.status == "completed" and store.data == {}


async def test_stale_run_failure_does_not_delete_the_recoverers_checkpoint() -> None:
    import time

    @tool
    def lookup(x: str) -> str:
        """Lookup."""
        return "found"

    store = MemoryCheckpointStore()
    seen = {"i": 0}

    def handler(messages: Any, tools: Any) -> Any:
        seen["i"] += 1
        if seen["i"] == 1:
            return call("lookup", x="1")
        store._claims[run_id_of(store)] = ("recoverer", time.time() + 60, 60.0)
        raise RuntimeError("the slow original fails after a recoverer took over")

    agent = Agent(name="A", model=FakeModel(handler=handler), tools=[lookup])
    with pytest.raises(ProviderError):
        await Runner(checkpoint_store=store).run_async(agent, "go")
    assert store._data  # the cleanup delete was fenced
    assert next(iter(store._claims.values()))[0] == "recoverer"


async def test_fenced_delete_and_operator_purge(tmp_path: Path) -> None:
    for store in _stores(tmp_path):
        await store.save("r", "v0")
        assert await store.claim("r", "o1", 60)
        with pytest.raises(CheckpointLeaseLostError):
            await store.delete("r", None, fenced=True)
        with pytest.raises(CheckpointLeaseLostError):
            await store.delete("r", "o2", fenced=True)
        assert await store.load("r") == "v0"
        await store.delete("r", "o1", fenced=True)
        assert await store.load("r") is None
        await store.save("p", "v0")
        assert await store.claim("p", "o1", 60)
        await store.delete("p")  # an unfenced delete is an operator purge
        assert await store.load("p") is None
        if isinstance(store, SQLiteCheckpointStore):
            store.close()


def test_concurrent_open_migrates_old_claims_table_once(tmp_path: Path) -> None:
    import threading

    errors: list[str] = []
    for i in range(20):
        path = tmp_path / f"old{i}.db"
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE nexura_meta (k TEXT PRIMARY KEY, v TEXT)")
        conn.execute("INSERT INTO nexura_meta VALUES ('checkpoint_schema', '1')")
        conn.execute(
            "CREATE TABLE nexura_run_claims ("
            "run_id TEXT PRIMARY KEY, owner TEXT NOT NULL, expires_at REAL NOT NULL)"
        )
        conn.commit()
        conn.close()
        barrier = threading.Barrier(4)

        def open_store(p: Path = path, b: threading.Barrier = barrier) -> None:
            b.wait()
            try:
                SQLiteCheckpointStore(p).close()
            except Exception as exc:
                errors.append(repr(exc))

        threads = [threading.Thread(target=open_store) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert not errors, errors[:2]
