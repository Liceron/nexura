"""Agent-to-agent handoffs: switching the active agent, limits, approvals, tracing, serving."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from nexura_agents import (
    A2AClient,
    A2AServer,
    Agent,
    AgentMCPServer,
    FakeModel,
    GuardrailResult,
    Handoff,
    MCPServer,
    RunContext,
    RunEventType,
    Runner,
    SequentialAgent,
    ToolCall,
    Usage,
    handoff,
    tool,
)
from nexura_agents import tracing as nexura_tracing
from nexura_agents.errors import (
    ApprovalDecisionError,
    MaxHandoffsExceededError,
    SDKValidationError,
    UsageLimitExceededError,
)
from nexura_agents.limits import UsageLimits
from nexura_agents.memory import MemoryRecord
from nexura_agents.session import MemorySessionStore
from nexura_agents.types import Message, RunInterruption
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import BaseModel

USAGE = Usage(prompt_tokens=3, completion_tokens=2, total_tokens=5)


def call(name: str, call_id: str = "c1", **arguments: Any) -> list[ToolCall]:
    return [ToolCall(id=call_id, name=name, arguments=arguments)]


def make(name: str, *replies: Any, instructions: str | None = None, **kwargs: Any) -> Agent:
    model = FakeModel(responses=list(replies), default_usage=USAGE)
    return Agent(name=name, instructions=instructions or f"You are {name}.", model=model, **kwargs)


def fake(agent: Agent) -> FakeModel:
    assert isinstance(agent.model, FakeModel)
    return agent.model


SEARCHES: list[str] = []


class StubStore:
    async def add(self, namespace: str, text: str, metadata: Any = None) -> Any:
        raise NotImplementedError

    async def search(
        self, namespace: str, query: str, limit: int = 5, min_score: Any = None
    ) -> Any:
        SEARCHES.append(query)
        return [MemoryRecord(id="m1", text="likes tea", namespace=namespace, metadata={})]

    async def delete(self, namespace: str, record_id: str) -> bool:
        return False

    async def list(self, namespace: str, limit: int = 100) -> Any:
        return []


def tool_names(model: FakeModel, index: int = 0) -> list[str]:
    return [t["function"]["name"] for t in model.calls[index]["tools"] or []]


def test_basic_triage_to_specialist_uses_target_model_instructions_and_tools() -> None:
    @tool
    def lookup(order: str) -> str:
        """Look up an order."""
        return f"{order}: shipped"

    billing = make(
        "Billing Desk",
        call("lookup", "t1", order="A1"),
        "A1 is shipped.",
        instructions="Billing rules.",
        tools=[lookup],
    )
    triage = make(
        "Triage",
        call("transfer_to_billing_desk", "h1", reason="order question"),
        handoffs=[billing],
    )
    result = Runner().run(triage, "Where is A1?")

    assert result.status == "completed"
    assert result.final_output == "A1 is shipped."
    assert result.last_agent is billing
    assert result.agent_path == ["Triage", "Billing Desk"]
    assert len(fake(triage).calls) == 1
    assert tool_names(fake(triage)) == ["transfer_to_billing_desk"]
    # After the handoff the target's model sees its own instructions and tools.
    first_target = fake(billing).calls[0]
    assert first_target["messages"][0].role == "system"
    assert first_target["messages"][0].content == "Billing rules."
    assert tool_names(fake(billing)) == ["lookup"]
    roles = [m.role for m in first_target["messages"]]
    assert roles == ["system", "user", "assistant", "tool"]
    assert first_target["messages"][-1].content == "Transferred to Billing Desk."
    assert first_target["messages"][-1].tool_call_id == "h1"
    # Single system message in the final transcript, and the starting agent's is restored.
    assert [m.role for m in result.messages].count("system") == 1
    assert result.messages[0].content == "You are Triage."
    assert result.tool_calls_count == 1  # the lookup; a handoff is not a tool run


def test_handoff_tool_schema_takes_optional_reason() -> None:
    specialist = make("Specialist", "x")
    triage = make("Triage", "y", handoffs=[handoff(specialist, description="Hard cases.")])
    Runner().run(triage, "hi")
    schema = fake(triage).calls[0]["tools"][0]["function"]
    assert schema["name"] == "transfer_to_specialist"
    assert schema["description"] == "Hard cases."
    assert list(schema["parameters"]["properties"]) == ["reason"]
    assert "reason" not in schema["parameters"].get("required", [])
    assert Handoff(specialist, tool_name="escalate").tool_name == "escalate"


def test_dynamic_instructions_get_same_deps_after_handoff() -> None:
    seen: list[Any] = []

    def instructions(ctx: RunContext[Any]) -> str:
        seen.append((ctx.agent_name, ctx.deps))
        return f"Serve {ctx.deps}."

    expert = Agent(
        name="Expert",
        instructions=instructions,
        model=FakeModel(responses=["ok"], default_usage=USAGE),
    )
    triage = make("Triage", call("transfer_to_expert"), handoffs=[expert])
    Runner().run(triage, "hi", deps="alice")
    assert seen == [("Expert", "alice")]
    assert fake(expert).calls[0]["messages"][0].content == "Serve alice."


def test_input_filter_shapes_what_the_target_sees() -> None:
    def keep_user_and_last(messages: list[Message]) -> list[Message]:
        # keep the handoff call/result pair, drop the noisy earlier chatter
        return [m for m in messages if m.content != "noise"]

    expert = make("Expert", "done")
    triage = make(
        "Triage",
        call("transfer_to_expert"),
        handoffs=[Handoff(expert, input_filter=keep_user_and_last)],
    )
    Runner().run(
        triage,
        "question",
        history=[Message(role="user", content="noise"), Message(role="assistant", content="noise")],
    )
    contents = [m.content for m in fake(expert).calls[0]["messages"]]
    assert "noise" not in contents and "question" in contents


def test_input_filter_that_breaks_tool_pairing_is_rejected() -> None:
    expert = make("Expert", "done")
    triage = make(
        "Triage",
        call("transfer_to_expert"),
        handoffs=[Handoff(expert, input_filter=lambda ms: [m for m in ms if m.role != "tool"])],
    )
    with pytest.raises(SDKValidationError, match="broke the transcript"):
        Runner().run(triage, "hi")


def test_on_handoff_called_with_reason_sync_and_async() -> None:
    seen: list[tuple[str, str]] = []

    async def on_async(ctx: RunContext[Any], reason: str) -> None:
        seen.append((ctx.agent_name, reason))

    def on_sync(ctx: RunContext[Any], reason: str) -> None:
        seen.append(("sync", reason))

    b = make("B", "b done")
    c = make("C", "c done")
    a = make(
        "A",
        [
            ToolCall(id="1", name="transfer_to_b", arguments={"reason": "needs B"}),
        ],
        handoffs=[Handoff(b, on_handoff=on_async)],
    )
    Runner().run(a, "go")
    assert seen == [("A", "needs B")]
    seen.clear()
    a2 = make("A2", call("transfer_to_c", reason="why"), handoffs=[Handoff(c, on_handoff=on_sync)])
    Runner().run(a2, "go")
    assert seen == [("sync", "why")]


def test_output_type_and_guardrails_of_the_final_agent() -> None:
    class Ticket(BaseModel):
        id: int

    seen_outputs: list[str] = []

    def out_guard(ctx: RunContext[Any], output: Any) -> GuardrailResult:
        seen_outputs.append(type(output).__name__)
        return GuardrailResult.passed()

    in_guard_calls: list[str] = []

    def in_guard(ctx: RunContext[Any], text: str) -> GuardrailResult:
        in_guard_calls.append(ctx.agent_name)
        return GuardrailResult.passed()

    writer = make(
        "Writer",
        '{"id": 7}',
        output_type=Ticket,
        output_guardrails=[out_guard],
        input_guardrails=[in_guard],
    )
    triage = make(
        "Triage", call("transfer_to_writer"), handoffs=[writer], input_guardrails=[in_guard]
    )
    result = Runner().run(triage, "make ticket")
    assert result.output == Ticket(id=7)
    assert seen_outputs == ["Ticket"]
    assert in_guard_calls == ["Triage"]  # input guardrails only for the starting agent


def test_max_handoffs_ping_pong() -> None:
    def ping_model() -> FakeModel:
        return FakeModel(
            handler=lambda m, t: call(
                "transfer_to_pong"
                if t[0]["function"]["name"].endswith("pong")
                else "transfer_to_ping"
            ),
            default_usage=USAGE,
        )

    ping = Agent(name="ping", instructions="", model=ping_model(), max_handoffs=3)
    pong = Agent(name="pong", instructions="", model=ping_model())
    ping.add_handoffs(pong)
    pong.add_handoffs(ping)
    with pytest.raises(MaxHandoffsExceededError) as info:
        Runner().run(ping, "go")
    assert info.value.max_handoffs == 3 and info.value.handoffs_count == 4
    # the Runner setting overrides the agent default
    with pytest.raises(MaxHandoffsExceededError) as info2:
        Runner(max_handoffs=1).run(ping, "go")
    assert info2.value.max_handoffs == 1
    with pytest.raises(SDKValidationError):
        Agent(name="x", instructions="", model=FakeModel(), max_handoffs=-1)


def test_turn_limit_and_usage_limits_span_the_whole_run() -> None:
    b = make("B", "never")
    a = make("A", call("transfer_to_b"), handoffs=[b], max_turns=1)
    # A used its one turn; B's turn would be turn 2 of the run.
    from nexura_agents.errors import MaxTurnsExceededError

    with pytest.raises(MaxTurnsExceededError):
        Runner().run(a, "go")
    b2 = make("B", "ok")
    a2 = make("A", call("transfer_to_b"), handoffs=[b2])
    with pytest.raises(UsageLimitExceededError):
        Runner(usage_limits=UsageLimits(request_limit=1)).run(a2, "go")


def test_usage_is_summed_once_across_agents() -> None:
    b = make("B", "ok")
    a = make("A", call("transfer_to_b"), handoffs=[b])
    result = Runner().run(a, "go")
    assert result.usage.total_tokens == 10
    assert result.turns_count == 2


def test_mixed_turn_runs_tools_then_first_handoff_wins() -> None:
    ran: list[str] = []

    @tool
    def audit(note: str) -> str:
        """Audit."""
        ran.append(note)
        return "audited"

    b = make("B", "from b")
    c = make("C", "from c")
    a = make(
        "A",
        [
            ToolCall(id="t1", name="audit", arguments={"note": "x"}),
            ToolCall(id="h1", name="transfer_to_b", arguments={"reason": "r1"}),
            ToolCall(id="h2", name="transfer_to_c", arguments={"reason": "r2"}),
            ToolCall(id="t2", name="audit", arguments={"note": "y"}),
        ],
        tools=[audit],
        handoffs=[b, c],
    )
    result = Runner().run(a, "go")
    assert result.final_output == "from b"
    assert result.agent_path == ["A", "B"]
    assert ran == ["x", "y"]  # every other tool call still ran, with the calling agent
    results = {m.tool_call_id: m.content for m in result.messages if m.role == "tool"}
    assert results["t1"] == "audited" and results["t2"] == "audited"
    assert results["h1"] == "Transferred to B."
    assert results["h2"].startswith("Ignored: only one handoff per turn")
    assert fake(c).calls == []
    # tool results keep call order so the transcript is valid for every provider
    order = [m.tool_call_id for m in result.messages if m.role == "tool"]
    assert order == ["t1", "h1", "h2", "t2"]


def test_name_collision_validation() -> None:
    other = make("Other", "x")

    @tool
    def transfer_to_other() -> str:
        """Clash."""
        return "x"

    with pytest.raises(SDKValidationError, match="collides"):
        make("A", "x", tools=[transfer_to_other], handoffs=[other])
    with pytest.raises(SDKValidationError, match="Duplicate handoff"):
        make("A", "x", handoffs=[other, Handoff(other)])
    with pytest.raises(SDKValidationError, match="Duplicate handoff"):
        make("A", "x", handoffs=[other, make("other", "y")])
    with pytest.raises(SDKValidationError):
        make("A", "x", handoffs=["nope"])  # type: ignore[list-item]
    # a failed add_handoffs changes nothing
    agent = make("A", "x", handoffs=[other])
    with pytest.raises(SDKValidationError):
        agent.add_handoffs(
            make("Third", "z"), Handoff(make("Fourth", "z"), tool_name="transfer_to_other")
        )
    assert [h.tool_name for h in agent.handoffs] == ["transfer_to_other"]


def test_memory_tool_collision() -> None:
    from nexura_agents import Memory

    other = make("Other", "x")
    with pytest.raises(SDKValidationError, match="collides"):
        make(
            "A",
            "x",
            memory=Memory(StubStore()),
            handoffs=[Handoff(other, tool_name="remember")],
        )


def approval_setup() -> tuple[Agent, Agent, list[str]]:
    done: list[str] = []

    @tool(requires_approval=True)
    def refund(order: str) -> str:
        """Refund an order."""
        done.append(order)
        return f"refunded {order}"

    billing = make(
        "Billing",
        call("refund", "r1", order="A1"),
        "Refund done.",
        tools=[refund],
    )
    triage = make("Triage", call("transfer_to_billing", "h1", reason="refund"), handoffs=[billing])
    return triage, billing, done


async def test_approval_pause_in_target_round_trips_and_resumes_in_new_process() -> None:
    triage, _billing, done = approval_setup()
    paused = await Runner().run_async(triage, "refund A1")
    assert paused.status == "paused"
    assert paused.last_agent is not None and paused.last_agent.name == "Billing"
    assert paused.agent_path == ["Triage", "Billing"]
    interruption = paused.interruption
    assert interruption is not None
    assert interruption.agent_name == "Triage"
    assert interruption.active_agent == "Billing"
    assert interruption.agent_path == ["Triage", "Billing"]
    assert interruption.handoffs_count == 1

    payload = interruption.to_json()
    reviewed = interruption.pending_digests()
    # A "new process": rebuild identical agents; nothing is shared except the JSON.
    triage2, _billing2, done2 = approval_setup()
    restored = RunInterruption.from_json(payload)
    assert restored.active_agent == "Billing" and restored.agent_path == ["Triage", "Billing"]
    # make the rebuilt Billing's model continue after the refund
    fake(triage2.handoffs[0].agent)._responses[:] = ["Refund done."]
    result = await Runner().resume_async(triage2, restored, approve=reviewed)
    assert result.status == "completed"
    assert result.final_output == "Refund done."
    assert result.agent_path == ["Triage", "Billing"]
    assert result.last_agent is triage2.handoffs[0].agent
    assert done2 == ["A1"] and done == []


async def test_old_interruption_payloads_still_load_and_resume() -> None:
    @tool(requires_approval=True)
    def refund(order: str) -> str:
        """Refund."""
        return "r"

    agent = make("Solo", call("refund", "r1", order="A1"), "ok", tools=[refund])
    paused = await Runner().run_async(agent, "go")
    import json

    reviewed = paused.interruption.pending_digests()  # type: ignore[union-attr]
    raw = json.loads(paused.interruption.to_json())  # type: ignore[union-attr]
    for key in ("active_agent", "agent_path", "handoffs_count"):
        raw.pop(key)
    old = RunInterruption.from_json(json.dumps(raw))
    assert old.active_agent is None and old.agent_path == []
    fake(agent)._responses[:] = ["ok"]
    result = await Runner().resume_async(agent, old, approve=reviewed)
    assert result.final_output == "ok" and result.agent_path == ["Solo"]


async def test_resume_with_unknown_active_agent_is_a_clear_error() -> None:
    triage, _b, _d = approval_setup()
    paused = await Runner().run_async(triage, "refund A1")
    assert paused.interruption is not None
    paused.interruption.active_agent = "Ghost"
    paused.interruption.agent_path = ["Triage", "Ghost"]
    with pytest.raises(ApprovalDecisionError, match="Ghost"):
        await Runner().resume_async(triage, paused.interruption, approve=True)
    # a payload with no path falls back to searching the handoff graph by name
    triage2, _b2, done2 = approval_setup()
    paused2 = await Runner().run_async(triage2, "refund A1")
    assert paused2.interruption is not None
    reviewed = paused2.interruption.pending_digests()
    # Simulate a legacy serialized representation, without in-memory provenance.
    restored = RunInterruption.from_json(paused2.interruption.to_json())
    restored.agent_path = []
    fake(triage2.handoffs[0].agent)._responses[:] = ["fine"]
    result = await Runner().resume_async(triage2, restored, approve=reviewed)
    assert result.final_output == "fine" and done2 == ["A1"]


async def test_streaming_event_order_includes_handoff() -> None:
    b = make("B", "bye")
    a = make("A", call("transfer_to_b", "h1", reason="why"), handoffs=[b])
    events = [e async for e in Runner().stream_async(a, "hi")]
    kinds = [e.type for e in events]
    assert kinds == [
        RunEventType.RUN_STARTED,
        RunEventType.AGENT_STARTED,  # added in F40 (2026-10-11)
        RunEventType.MODEL_STARTED,
        RunEventType.MODEL_COMPLETED,
        RunEventType.HANDOFF,
        RunEventType.AGENT_STARTED,
        RunEventType.MODEL_STARTED,
        RunEventType.MODEL_COMPLETED,
        RunEventType.FINAL_OUTPUT,
        RunEventType.RUN_COMPLETED,
    ]
    event = events[4]
    assert event.data["from"] == "A" and event.data["to"] == "B"
    assert events[5].data == {"agent": "B", "path": ["A", "B"]}
    assert event.data["reason"] == "why" and event.data["path"] == ["A", "B"]


async def test_session_transcript_stays_valid_and_starts_from_the_first_agent() -> None:
    store = MemorySessionStore()
    b = make("B", "b says hi", "second answer from B")
    a = make("A", call("transfer_to_b", "h1"), "A again", handoffs=[b])
    runner = Runner(session_store=store)
    first = await runner.run_async(a, "hello", session_id="s")
    assert first.final_output == "b says hi"
    snapshot = await store.get("s")
    assert snapshot is not None
    assert snapshot.messages[0].role == "system" and snapshot.messages[0].content == "You are A."
    calls = {c.id for m in snapshot.messages for c in m.tool_calls or []}
    answered = {m.tool_call_id for m in snapshot.messages if m.role == "tool"}
    assert calls == answered == {"h1"}
    second = await runner.run_async(a, "next", session_id="s")
    assert second.final_output == "A again" and second.agent_path == ["A"]


def test_plain_run_without_handoffs_reports_its_agent() -> None:
    solo = make("Solo", "hi")
    result = Runner().run(solo, "x")
    assert result.last_agent is solo and result.agent_path == ["Solo"]
    assert RunEventType.HANDOFF not in [e.type for e in result.events]


def test_handoff_inside_a_sequential_agent_member() -> None:
    expert = make("Expert", "expert answer")
    front = make("Front", call("transfer_to_expert", "h1"), handoffs=[expert])
    after = make("After", "wrapped")
    team = SequentialAgent("Flow", [front, after])
    result = team.run("go")
    assert result.final_output == "wrapped"
    assert fake(expert).calls and fake(expert).calls[0]["messages"][0].content == "You are Expert."
    after_prompt = [m.content for m in fake(after).calls[0]["messages"] if m.role == "user"]
    assert after_prompt == ["expert answer"]


@pytest.fixture
def exporter() -> Iterator[InMemorySpanExporter]:
    memory = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(memory))
    nexura_tracing.instrument(tracer_provider=provider)
    yield memory
    nexura_tracing.uninstrument()


def test_tracing_records_handoff_span_and_agent_path(exporter: InMemorySpanExporter) -> None:
    b = make("B", "bye")
    a = make("A", call("transfer_to_b", reason="needs b"), handoffs=[b])
    Runner().run(a, "hi")
    spans = exporter.get_finished_spans()
    root = next(s for s in spans if s.name == "invoke_agent A")
    assert tuple(root.attributes["nexura.agent.path"]) == ("A", "B")  # type: ignore[index, arg-type]
    assert root.attributes["nexura.agent.last"] == "B"  # type: ignore[index]
    marks = [s for s in spans if s.name == "handoff A -> B"]
    assert len(marks) == 1
    attrs = marks[0].attributes or {}
    assert attrs["gen_ai.operation.name"] == "handoff"
    assert attrs["nexura.handoff.from"] == "A" and attrs["nexura.handoff.to"] == "B"
    # The reason is model-written content: not an attribute unless capture_content is on.
    assert "nexura.handoff.reason" not in attrs
    assert not marks[0].events
    assert marks[0].parent is not None and marks[0].parent.span_id == root.context.span_id
    chats = [s for s in spans if s.name.startswith("chat")]
    assert len(chats) == 2


async def test_served_via_mcp_server_start_agent_with_handoffs() -> None:
    expert = make("Expert", "expert says 42")
    front = make("Front", call("transfer_to_expert"), handoffs=[expert])
    app = AgentMCPServer("desk", agents=[front]).http_app()
    remote = MCPServer.http("http://testserver/mcp", transport=httpx.ASGITransport(app=app))
    async with remote:
        tools = await remote.tools()
        assert [t.name for t in tools] == ["front"]
        caller = Agent(
            name="Caller",
            instructions="",
            tools=tools,
            model=FakeModel(responses=[call("front", "x", request="question"), "got it"]),
        )
        result = await caller.run_async("ask")
    out = [m.content for m in result.messages if m.role == "tool"]
    assert out == ["expert says 42"]


async def test_served_via_a2a_server_start_agent_with_handoffs() -> None:
    expert = make("Expert", "expert says 42")
    front = make("Front", call("transfer_to_expert"), handoffs=[expert])
    app = A2AServer(front, url="http://a2a.test/").http_app()
    client = A2AClient("http://a2a.test", transport=httpx.ASGITransport(app=app))
    task = await client.send("question")
    assert task.is_completed and task.text == "expert says 42"


def test_memory_is_recalled_once_for_the_starting_agent_only() -> None:
    from nexura_agents import Memory

    SEARCHES.clear()
    expert = make("Expert", "ok", memory=Memory(StubStore()))
    triage = make(
        "Triage", call("transfer_to_expert"), handoffs=[expert], memory=Memory(StubStore())
    )
    Runner().run(triage, "hello")
    assert SEARCHES == ["hello"]
    first = fake(triage).calls[0]["messages"][0].content
    assert "likes tea" in first
    assert "likes tea" not in fake(expert).calls[0]["messages"][0].content


def test_same_named_handoff_targets_are_rejected() -> None:
    w1 = make("worker", "a")
    w2 = make("worker", "b")
    with pytest.raises(SDKValidationError, match="two different handoff targets"):
        make("root", "x", handoffs=[handoff(w1, tool_name="go_a"), handoff(w2, tool_name="go_b")])
    # the same agent object under two tool names is unambiguous and stays allowed
    make("root", "x", handoffs=[handoff(w1, tool_name="go_a"), handoff(w1, tool_name="go_b")])


async def test_starting_system_message_survives_resume_then_handoff() -> None:
    @tool(requires_approval=True)
    def act() -> str:
        """Act."""
        return "ok"

    def build() -> Agent:
        b = Agent(name="B", instructions="B sys", model=FakeModel(responses=["b done"]))
        return Agent(
            name="A",
            instructions="A sys",
            model=FakeModel(
                responses=[[ToolCall(id="c1", name="act", arguments={})], call("transfer_to_b")]
            ),
            tools=[act],
            handoffs=[b],
        )

    store = MemorySessionStore()
    paused = await Runner(session_store=store).run_async(build(), "hi", session_id="s")
    assert paused.status == "paused"
    reviewed = paused.interruption.pending_digests()
    intr = RunInterruption.from_json(paused.interruption.to_json())
    res = await Runner(session_store=store).resume_async(build(), intr, approve=reviewed)
    assert res.messages[0].role == "system" and res.messages[0].content == "A sys"


def test_handoff_reason_is_recorded_only_with_capture_content() -> None:
    memory = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(memory))
    nexura_tracing.instrument(tracer_provider=provider, capture_content=True)
    try:
        b = make("B", "bye")
        a = make("A", call("transfer_to_b", reason="needs b"), handoffs=[b])
        Runner().run(a, "hi")
    finally:
        nexura_tracing.uninstrument()
    mark = next(s for s in memory.get_finished_spans() if s.name == "handoff A -> B")
    assert [e.attributes["nexura.handoff.reason"] for e in mark.events] == ["needs b"]  # type: ignore[index]
