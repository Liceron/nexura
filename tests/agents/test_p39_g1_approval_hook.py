"""P39 G1: ``AgentHooks.tool_needs_approval`` adds approval gates, never removes one.

It reuses the A2/S11 approval contract: a hook-gated call pauses the batch like
``requires_approval``, resumes with the same digest checks, and is asked again on durable
recovery when nothing on record approved it. All offline; crashes are simulated with a
``BaseException``, as in the S5/S11 tests.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest
from nexura_agents import (
    Agent,
    AgentHooks,
    FakeModel,
    MemoryCheckpointStore,
    ResolvedOutcome,
    RunContext,
    RunInterruption,
    Runner,
    RunResult,
    Tool,
    ToolCall,
    tool,
)
from nexura_agents.contracts.execution import ExecutionId, NotApplied
from nexura_agents.errors import SDKValidationError


class Crash(BaseException):
    """Simulated process death: the runner keeps the checkpoint."""


def call(name: str, call_id: str = "c1", **arguments: Any) -> list[ToolCall]:
    return [ToolCall(id=call_id, name=name, arguments=arguments)]


def held(result: RunResult[Any]) -> RunInterruption:
    """The interruption of a paused result."""
    assert result.interruption is not None
    return result.interruption


def agent_with(tools: list[Any], responses: list[Any]) -> Agent:
    return Agent(
        name="A", instructions="Be helpful.", model=FakeModel(responses=responses), tools=tools
    )


class GateNames(AgentHooks):
    """Requires approval for the named tools (a deterministic, name-based rule)."""

    def __init__(self, *names: str, answer: bool | None = True) -> None:
        self.names = set(names)
        self.answer = answer
        self.asked: list[tuple[str, dict[str, Any], Any]] = []

    def tool_needs_approval(
        self, ctx: RunContext[Any], tool: Tool, arguments: Mapping[str, Any]
    ) -> bool | None:
        self.asked.append((tool.name, dict(arguments), ctx.tool))
        return self.answer if tool.name in self.names else None


class NeverGate(AgentHooks):
    """Tries to remove every gate."""

    def __init__(self) -> None:
        self.asked = 0

    def tool_needs_approval(self, ctx: Any, tool: Any, arguments: Any) -> bool:
        self.asked += 1
        return False


# ---------------------------------------------------------------- adds a gate


async def test_a_hook_gate_pauses_an_ungated_tool_and_resume_runs_it() -> None:
    sent: list[int] = []

    @tool
    def transfer(amount: int) -> str:
        """Transfer money (no requires_approval on the tool)."""
        sent.append(amount)
        return "sent"

    hooks = GateNames("transfer")
    runner = Runner(hooks=hooks)
    agent = agent_with([transfer], [call("transfer", amount=5), "done"])
    paused = await runner.run_async(agent, "pay")
    assert paused.status == "paused" and sent == []
    assert [c.name for c in held(paused).pending] == ["transfer"]
    name, arguments, info = hooks.asked[0]
    assert (name, arguments) == ("transfer", {"amount": 5})
    assert info is not None and info.name == "transfer"  # ctx.tool is set for the hook
    done = await runner.resume_async(agent, held(paused), approve=True)
    assert done.status == "completed" and sent == [5]


async def test_reject_after_a_hook_gate_tells_the_model() -> None:
    @tool
    def transfer(amount: int) -> str:
        """Transfer money."""
        raise AssertionError("must not run")

    runner = Runner(hooks=GateNames("transfer"))
    agent = agent_with([transfer], [call("transfer", amount=5), "ok"])
    paused = await runner.run_async(agent, "pay")
    done = await runner.resume_async(agent, held(paused), reject={"c1": "no"})
    tool_msg = next(m for m in done.messages if m.role == "tool")
    assert tool_msg.content == "Tool call rejected by a human reviewer: no"


async def test_the_whole_batch_waits_and_ungated_calls_run_on_resume() -> None:
    ran: list[str] = []

    @tool
    def read() -> str:
        """Read."""
        ran.append("read")
        return "r"

    @tool
    def write() -> str:
        """Write."""
        ran.append("write")
        return "w"

    batch = [ToolCall("r1", "read", {}), ToolCall("w1", "write", {})]
    runner = Runner(hooks=GateNames("write"))
    agent = agent_with([read, write], [batch, "done"])
    paused = await runner.run_async(agent, "go")
    assert [c.id for c in held(paused).pending] == ["w1"] and ran == []
    await runner.resume_async(agent, held(paused), approve=True)
    assert ran == ["read", "write"]


# ---------------------------------------------------------------- cannot remove a gate


async def test_a_hook_returning_false_cannot_remove_requires_approval() -> None:
    @tool(requires_approval=True)
    def wire(amount: int) -> str:
        """Wire money."""
        raise AssertionError("must not run before approval")

    hooks = NeverGate()
    paused = await Runner(hooks=hooks).run_async(
        agent_with([wire], [call("wire", amount=1), "x"]), "go"
    )
    assert paused.status == "paused"
    assert hooks.asked == 0  # the tool's own gate is decided first and wins


async def test_a_hook_cannot_remove_a_predicate_gate_either() -> None:
    @tool(requires_approval=lambda ctx, args: args["amount"] > 100)
    def wire(amount: int) -> str:
        """Wire money."""
        return "sent"

    runner = Runner(hooks=[NeverGate(), NeverGate()])
    big = await runner.run_async(agent_with([wire], [call("wire", amount=500), "x"]), "go")
    small = await runner.run_async(agent_with([wire], [call("wire", amount=5), "ok"]), "go")
    assert big.status == "paused" and small.status == "completed"


async def test_composed_hooks_or_their_votes_in_list_order() -> None:
    @tool
    def act() -> str:
        """Act."""
        return "acted"

    first, second, third = NeverGate(), GateNames("act"), GateNames("act")
    paused = await Runner(hooks=[first, second, third]).run_async(
        agent_with([act], [call("act"), "x"]), "go"
    )
    assert paused.status == "paused"
    assert first.asked == 1 and len(second.asked) == 1
    assert third.asked == []  # any True decides; later members are not asked


async def test_the_hook_gets_a_copy_of_the_arguments() -> None:
    received: list[dict[str, Any]] = []

    class Mutate(AgentHooks):
        def tool_needs_approval(self, ctx: Any, tool: Any, arguments: Any) -> None:
            arguments["amount"] = 999_999
            arguments["nested"]["x"] = "changed"

    @tool
    def pay(amount: int, nested: dict[str, Any]) -> str:
        """Pay."""
        received.append({"amount": amount, "nested": nested})
        return "ok"

    agent = agent_with([pay], [call("pay", amount=5, nested={"x": "y"}), "done"])
    await Runner(hooks=[Mutate(), Mutate()]).run_async(agent, "go")
    assert received == [{"amount": 5, "nested": {"x": "y"}}]


@pytest.mark.parametrize("bad", ["yes", 1, [True]])
async def test_a_non_boolean_answer_is_refused(bad: Any) -> None:
    class Bad(AgentHooks):
        def tool_needs_approval(self, ctx: Any, tool: Any, arguments: Any) -> Any:
            return bad

    @tool
    def act() -> str:
        """Act."""
        return "acted"

    with pytest.raises(SDKValidationError, match="must return True, False or None"):
        await Runner(hooks=Bad()).run_async(agent_with([act], [call("act"), "x"]), "go")


async def test_an_async_approval_hook_is_refused() -> None:
    class Async(AgentHooks):
        async def tool_needs_approval(self, ctx: Any, tool: Any, arguments: Any) -> Any:  # type: ignore[override]
            return True

    @tool
    def act() -> str:
        """Act."""
        return "acted"

    with pytest.raises(SDKValidationError, match="must be a regular"):
        await Runner(hooks=Async()).run_async(agent_with([act], [call("act"), "x"]), "go")


async def test_hooks_without_the_method_and_the_default_add_no_gate() -> None:
    class Legacy:  # an old duck-typed hooks object
        async def on_run_start(self, ctx: Any) -> None:
            return None

        async def before_model(self, ctx: Any, messages: Any) -> None:
            return None

        async def after_model(self, ctx: Any, response: Any) -> None:
            return None

        async def before_tool(self, ctx: Any, call: Any) -> None:
            return None

        async def after_tool(self, ctx: Any, call: Any, result: Any) -> None:
            return None

        async def on_run_end(self, ctx: Any, output: Any) -> None:
            return None

    @tool
    def act() -> str:
        """Act."""
        return "acted"

    options: list[Any] = [Legacy(), AgentHooks(), [Legacy(), AgentHooks()]]
    for hooks in options:
        result = await Runner(hooks=hooks).run_async(agent_with([act], [call("act"), "ok"]), "go")
        assert result.status == "completed"


# ---------------------------------------------------------------- resume and recovery


async def test_a_hook_gated_call_not_applied_after_a_crash_needs_a_fresh_approval() -> None:
    """Site ``_apply_outcomes``: as for a ``requires_approval`` tool (S5)."""
    count: dict[str, int] = {}
    store = MemoryCheckpointStore()

    @tool
    def pay() -> str:
        """Pay (gated by the hook only)."""
        count["pay"] = count.get("pay", 0) + 1
        if count["pay"] == 1:
            raise Crash
        return "paid"

    runner = Runner(checkpoint_store=store, hooks=GateNames("pay"))
    agent = agent_with([pay], [call("pay"), "x"])
    first = await runner.run_async(agent, "go")
    assert first.status == "paused"
    with pytest.raises(Crash):
        await runner.resume_async(agent, held(first), approve=True)
    agent2 = agent_with([pay], ["done"])
    paused = await runner.recover_async(agent2, first.run_id)
    assert held(paused).pause_reason == "effect_unknown"
    again = await runner.resume_async(
        agent2, held(paused), outcomes={"c1": ResolvedOutcome.not_applied()}
    )
    assert again.status == "paused" and count["pay"] == 1  # not sent without a fresh approval
    assert held(again).reason == "effect_not_applied"
    final = await runner.resume_async(agent2, held(again), approve=True)
    assert final.status == "completed" and count["pay"] == 2


async def test_an_undecided_hook_gated_call_in_a_recovered_batch_is_asked_again() -> None:
    """Site ``_apply_claim`` (S11 review B1, fail closed) with a hook gate."""
    count: dict[str, int] = {}
    store = MemoryCheckpointStore()

    @tool
    def pay() -> str:
        """Pay."""
        count["pay"] = count.get("pay", 0) + 1
        return "paid"

    class CrashBeforeTool(GateNames):
        async def before_tool(self, ctx: Any, call: Any) -> None:
            raise Crash("after the batch checkpoint, before the call")

    runner = Runner(checkpoint_store=store, hooks=GateNames("pay"))
    paused = await runner.run_async(agent_with([pay], [call("pay"), "x"]), "go")
    runner.hooks = CrashBeforeTool("pay")
    with pytest.raises(Crash):
        await runner.resume_async(agent_with([pay], ["x"]), held(paused), approve=True)
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
    result = await Runner(checkpoint_store=store, hooks=GateNames("pay")).recover_async(
        agent_with([pay], ["done"]), paused.run_id
    )
    assert result.status == "paused" and count == {}
    assert held(result).reason == "effect_not_applied"  # a fresh approval is needed


async def test_the_same_recovery_without_the_hook_gate_sends_the_call() -> None:
    """Control for the test above: the gate, not something else, asked again.

    P39 G2 (MIN7): a hook gate is now recorded in the run's journal (``hook_gates``), so
    a recovery runner without the hook still waits for a person (tested in
    ``test_p39_g2_policy.py``). This control therefore also clears that record, as in a
    journal written before G2; with no gate anywhere the call is sent.
    """
    count: dict[str, int] = {}
    store = MemoryCheckpointStore()

    @tool
    def pay() -> str:
        """Pay."""
        count["pay"] = count.get("pay", 0) + 1
        return "paid"

    class CrashBeforeTool(GateNames):
        async def before_tool(self, ctx: Any, call: Any) -> None:
            raise Crash("after the batch checkpoint, before the call")

    runner = Runner(checkpoint_store=store, hooks=GateNames("pay"))
    paused = await runner.run_async(agent_with([pay], [call("pay"), "x"]), "go")
    runner.hooks = CrashBeforeTool("pay")
    with pytest.raises(Crash):
        await runner.resume_async(agent_with([pay], ["x"]), held(paused), approve=True)
    journal = await store.open_execution_store()
    eid = ExecutionId("default", paused.run_id)
    lease = await journal.claim(
        eid, "tester", 5, definition_digest=(await journal.get(eid)).run.definition_digest
    )
    assert lease is not None
    state = dict((await journal.get(eid)).run.state)
    state["approval_claim"] = None
    assert state.get("hook_gates") == ["turn:1/call:c1"]  # G2: the gate was recorded
    state.pop("hook_gates")  # no gate on record (a pre-G2 journal)
    await journal.commit(lease, state=state, status="running")
    await journal.release(lease)
    result = await Runner(checkpoint_store=store).recover_async(
        agent_with([pay], ["done"]), paused.run_id
    )
    assert result.status == "completed" and count == {"pay": 1}


async def test_reconcile_not_applied_on_a_hook_gated_tool_waits_for_approval() -> None:
    """Site ``_settle_in_doubt``: a reconcilable call that did not happen is sent again only
    after a fresh approval when a hook gates it (its first approval was used)."""
    count: dict[str, int] = {}
    store = MemoryCheckpointStore()

    def reconcile(ctx: Any, intent: Any) -> Any:
        return NotApplied()

    @tool(effect="reconcilable", reconcile=reconcile)
    def charge() -> str:
        """Charge."""
        count["charge"] = count.get("charge", 0) + 1
        if count["charge"] == 1:
            raise Crash
        return "charged"

    runner = Runner(checkpoint_store=store, hooks=GateNames("charge"))
    agent = agent_with([charge], [call("charge"), "x"])
    first = await runner.run_async(agent, "go")
    with pytest.raises(Crash):
        await runner.resume_async(agent, held(first), approve=True)
    agent2 = agent_with([charge], ["done"])
    again = await runner.recover_async(agent2, first.run_id)
    assert again.status == "paused" and count["charge"] == 1
    assert held(again).reason == "effect_not_applied"
    final = await runner.resume_async(agent2, held(again), approve=True)
    assert final.status == "completed" and count["charge"] == 2
