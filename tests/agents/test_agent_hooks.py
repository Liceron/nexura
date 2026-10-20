"""Per-agent lifecycle hooks and the AGENT_STARTED event (F40)."""

from __future__ import annotations

from typing import Any

import pytest
from nexura_agents import Agent, FakeModel, Runner, ToolCall
from nexura_agents.events import RunEventType
from nexura_agents.hooks import AgentHooks


class Recorder(AgentHooks):
    def __init__(self) -> None:
        self.seen: list[tuple[Any, ...]] = []

    async def on_run_start(self, ctx: Any) -> None:
        self.seen.append(("run_start",))

    async def on_agent_start(self, ctx: Any, agent: Agent) -> None:
        self.seen.append(("agent_start", agent.name))

    async def on_agent_end(self, ctx: Any, agent: Agent, output: Any) -> None:
        self.seen.append(("agent_end", agent.name, output))

    async def on_handoff(self, ctx: Any, source: Agent, target: Agent) -> None:
        self.seen.append(("handoff", source.name, target.name))

    async def on_run_end(self, ctx: Any, output: Any) -> None:
        self.seen.append(("run_end", output))


def team() -> Agent:
    billing = Agent(name="Billing", instructions="i", model=FakeModel(responses=["paid"]))
    return Agent(
        name="Triage",
        instructions="i",
        model=FakeModel(responses=[[ToolCall(id="h", name="transfer_to_billing", arguments={})]]),
        handoffs=[billing],
    )


async def test_hooks_fire_in_order_across_a_handoff() -> None:
    hooks = Recorder()
    result = await Runner(hooks=hooks).run_async(team(), "pay")
    assert result.output == "paid"
    assert hooks.seen == [
        ("run_start",),
        ("agent_start", "Triage"),
        ("agent_end", "Triage", None),
        ("handoff", "Triage", "Billing"),
        ("agent_start", "Billing"),
        ("agent_end", "Billing", "paid"),
        ("run_end", "paid"),
    ]


async def test_agent_started_events() -> None:
    events = [e async for e in Runner().stream_async(team(), "pay")]
    started = [e.data for e in events if e.type == RunEventType.AGENT_STARTED]
    assert started == [
        {"agent": "Triage", "path": ["Triage"]},
        {"agent": "Billing", "path": ["Triage", "Billing"]},
    ]


async def test_hooks_objects_without_the_new_methods_still_work() -> None:
    class OldStyle:
        """A duck-typed hooks object written before F40."""

        def __init__(self) -> None:
            self.calls = 0

        async def on_run_start(self, ctx: Any) -> None:
            self.calls += 1

        async def before_model(self, ctx: Any, messages: Any) -> None:
            return None

        async def after_model(self, ctx: Any, response: Any) -> None:
            return None

        async def before_tool(self, ctx: Any, call: Any) -> None:
            return None

        async def after_tool(self, ctx: Any, call: Any, result: Any) -> None:
            return None

        async def on_run_end(self, ctx: Any, output: Any) -> None:
            self.calls += 1

    hooks = OldStyle()
    result = await Runner(hooks=hooks).run_async(team(), "pay")  # type: ignore[arg-type]
    assert result.output == "paid" and hooks.calls == 2


async def test_a_failing_handoff_hook_fails_the_run() -> None:
    class Broken(AgentHooks):
        async def on_handoff(self, ctx: Any, source: Agent, target: Agent) -> None:
            raise RuntimeError("audit log down")

    with pytest.raises(RuntimeError, match="audit log down"):
        await Runner(hooks=Broken()).run_async(team(), "pay")
