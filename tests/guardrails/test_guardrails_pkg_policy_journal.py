# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""P39 G6 (design 8.7): model-based policy verdicts are journaled and replayed.

A process crash is simulated with a ``BaseException`` raised by an audit sink: the policy
writes its audit record after the verdict is committed and before it acts on it, so the
crash falls exactly between the judge's verdict and the tool's intent commit. Recovery must
reuse the journaled verdict (the scripted ``FakeModel`` judge is not called again); a changed
text is a new key and is judged again. Offline: ``MemoryCheckpointStore`` and ``FakeModel``.
"""

from __future__ import annotations

from typing import Any

import pytest
from nexura_agents import Agent, FakeModel, MemoryCheckpointStore, Runner, ToolCall, tool
from nexura_agents.contracts.execution import ExecutionId
from nexura_agents.verdict_journal import GUARD_VERDICTS_KEY, RunVerdicts, step_ref, verdict_key
from nexura_guardrails import CallbackAuditSink, GuardrailPolicy, JudgeCheck, Rule, When

ALLOW = '{"decision": "allow", "categories": [], "score": 0.0, "reason": "ok"}'
BLOCK = '{"decision": "block", "categories": ["fraud"], "score": 0.9, "reason": "fraud"}'


class Crash(BaseException):
    """Simulated process death."""


class CountingJudge(FakeModel):
    """A scripted judge model that counts its calls."""

    def __init__(self, replies: list[str]) -> None:
        super().__init__(responses=replies)
        self.count = 0

    async def generate(self, *args: Any, **kwargs: Any) -> Any:
        self.count += 1
        return await super().generate(*args, **kwargs)


def build(judge: CountingJudge, *, crash_stage: str | None = "tool_input") -> GuardrailPolicy:
    armed = {"on": crash_stage is not None}

    def sink(record: Any) -> None:
        if armed["on"] and record.stage == crash_stage:
            armed["on"] = False
            raise Crash

    return GuardrailPolicy(
        name="journaled",
        checks={"judge": JudgeCheck(judge, "Block fraud.", cache=False)},
        rules=[Rule("judge-sends", When(stage="tool_input"), check="judge", action="block")],
        audit=CallbackAuditSink(sink),
        audit_allows=True,
    )


def sender(log: list[str]) -> Any:
    @tool
    def send(to: str) -> str:
        """Send a payment."""
        log.append(to)
        return f"sent to {to}"

    return send


def agent(responses: list[Any], log: list[str]) -> Agent:
    return Agent(
        name="A", instructions="x", model=FakeModel(responses=responses), tools=[sender(log)]
    )


async def crashed_run(store: MemoryCheckpointStore, policy: GuardrailPolicy, log: list[str]) -> str:
    runner = Runner(checkpoint_store=store, hooks=[policy])
    first = agent([[ToolCall(id="c1", name="send", arguments={"to": "alice"})], "done"], log)
    with pytest.raises(Crash):
        await runner.run_async(first, "pay alice")
    (run_id,) = await store.list_incomplete()
    return run_id


async def test_a_judge_verdict_is_committed_before_it_is_acted_on() -> None:
    store, log = MemoryCheckpointStore(), []
    judge = CountingJudge([ALLOW] * 3)
    run_id = await crashed_run(store, build(judge), log)
    assert judge.count == 1 and log == []
    journal = await (await store.open_execution_store()).get(ExecutionId("default", run_id))
    verdicts = journal.run.state[GUARD_VERDICTS_KEY]
    (record,) = verdicts.values()
    assert record["decision"] == "allow" and record["check"] == "judge"
    assert [e.event_type for e in journal.events if e.event_type == "guard_verdict"] == [
        "guard_verdict"
    ]


async def test_recovery_replays_the_verdict_without_asking_the_judge() -> None:
    store, log = MemoryCheckpointStore(), []
    judge = CountingJudge([ALLOW] * 3)
    policy = build(judge)
    run_id = await crashed_run(store, policy, log)
    runner = Runner(checkpoint_store=store, hooks=[policy])
    result = await runner.recover_async(agent(["done"], log), run_id)
    assert result.status == "completed" and log == ["alice"]
    assert judge.count == 1  # the journaled verdict was used, the judge was not called again


async def test_a_replayed_block_stays_a_block() -> None:
    """A judge that would now allow cannot unblock a call it blocked before the crash."""
    store, log = MemoryCheckpointStore(), []
    judge = CountingJudge([BLOCK, ALLOW, ALLOW])
    policy = build(judge)
    run_id = await crashed_run(store, policy, log)
    result = await Runner(checkpoint_store=store, hooks=[policy]).recover_async(
        agent(["done"], log), run_id
    )
    assert log == [] and judge.count == 1
    (message,) = [m.content for m in result.messages if m.role == "tool"]
    assert message.startswith("Tool call blocked by policy rule 'judge-sends'")


async def test_changed_text_is_judged_again() -> None:
    """A verdict is keyed by a digest of the checked text: other arguments, another key."""
    store, log = MemoryCheckpointStore(), []
    judge = CountingJudge([ALLOW] * 3)
    policy = build(judge)
    run_id = await crashed_run(store, policy, log)
    eid = ExecutionId("default", run_id)
    execution = await store.open_execution_store()
    stored = dict((await execution.get(eid)).run.state[GUARD_VERDICTS_KEY])
    # Re-key the stored verdict as if it had been given for different arguments.
    snapshot = await execution.get(eid)
    lease = await execution.claim(
        eid, "test", 30.0, definition_digest=snapshot.run.definition_digest
    )
    assert lease is not None
    state = dict(snapshot.run.state)
    state[GUARD_VERDICTS_KEY] = {"0" * 64: next(iter(stored.values()))}
    await execution.commit(lease, state=state)
    await execution.release(lease)
    result = await Runner(checkpoint_store=store, hooks=[policy]).recover_async(
        agent(["done"], log), run_id
    )
    assert result.status == "completed" and judge.count == 2  # judged afresh


async def test_without_a_durable_store_nothing_is_journaled_but_the_run_works() -> None:
    log: list[str] = []
    judge = CountingJudge([ALLOW])
    result = await Runner(hooks=[build(judge, crash_stage=None)]).run_async(
        agent([[ToolCall(id="c1", name="send", arguments={"to": "bob"})], "done"], log),
        "pay bob",
    )
    assert result.status == "completed" and log == ["bob"] and judge.count == 1


async def test_deterministic_checks_are_not_journaled() -> None:
    from nexura_guardrails import pii

    store, log = MemoryCheckpointStore(), []
    policy = GuardrailPolicy(
        checks={"pii": pii()},
        rules=[Rule("pii", When(stage="tool_input"), check="pii")],
    )
    runner = Runner(checkpoint_store=store, hooks=[policy])
    result = await runner.run_async(
        agent([[ToolCall(id="c1", name="send", arguments={"to": "x"})], "done"], log), "go"
    )
    journal = await (await store.open_execution_store()).get(ExecutionId("default", result.run_id))
    assert GUARD_VERDICTS_KEY not in journal.run.state


def test_keys_and_step_refs() -> None:
    from nexura_agents import RunContext

    ctx: RunContext[None] = RunContext(deps=None, run_id="r", agent_name="a", turn=3)
    assert step_ref(ctx, "input") == "input" and step_ref(ctx, "output") == "final"
    assert step_ref(ctx, "model_output") == "turn:3/model"
    tool_ctx = ctx.for_tool("c9", "send", 0)
    assert step_ref(tool_ctx, "tool_input") == "turn:3/call:c9"
    base = {"policy_digest": "p", "rule_id": "r", "check": "j", "stage": "tool_input"}
    one = verdict_key(**base, step="turn:3/call:c9", text="a")
    assert one == verdict_key(**base, step="turn:3/call:c9", text="a")
    assert one != verdict_key(**base, step="turn:3/call:c9", text="b")
    assert one != verdict_key(**base, step="turn:4/call:c9", text="a")


async def test_run_verdicts_keep_a_bounded_number() -> None:
    verdicts = RunVerdicts(lambda: None)
    for i in range(300):
        await verdicts.put(f"k{i}", {"decision": "allow"})
    assert len(verdicts.local) == 256 and verdicts.get("k0") is None and verdicts.get("k299")
