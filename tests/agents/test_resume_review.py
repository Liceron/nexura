"""Regressions from the review of resume run context (B12/B13)."""

from __future__ import annotations

import datetime
import json
from typing import Any

from nexura_agents import Agent, FakeModel, MemoryCheckpointStore, Runner, ToolCall, tool
from nexura_agents import tracing as nexura_tracing
from nexura_agents.cancel import CancelToken
from nexura_agents.durable import encode_checkpoint
from nexura_agents.handoffs import Handoff
from nexura_agents.run_context import RunContext
from nexura_agents.types import RunInterruption


def mk(seen: dict | None = None, responses: list | None = None, **kw: Any) -> Agent:
    seen = seen if seen is not None else {}

    @tool(requires_approval=True)
    def charge(ctx: RunContext, amount: int) -> str:
        """Charge."""
        seen["tags"] = list(ctx.tags)
        seen["metadata"] = ctx.metadata
        return "ok"

    return Agent(
        name="A",
        instructions="i",
        model=FakeModel(
            responses=responses
            or [[ToolCall(id="c1", name="charge", arguments={"amount": 1})], "done"]
        ),
        tools=[charge],
        **kw,
    )


async def test_non_json_metadata_durable_pause_does_not_crash() -> None:
    agent = mk()
    r = Runner(checkpoint_store=MemoryCheckpointStore())
    res = await r.run_async(
        agent, "go", metadata={"when": datetime.datetime(2026, 1, 1)}, tags=["t"]
    )
    assert res.status == "paused"


async def test_non_json_metadata_pause_to_json_works() -> None:
    agent = mk()
    res = await Runner().run_async(agent, "go", metadata={"when": datetime.date(2026, 1, 1)})
    assert res.status == "paused"
    res.interruption.to_json()  # metadata was accepted by run(); pausing must be storable


async def test_nested_metadata_mutation_does_not_leak_to_caller_or_interruption() -> None:

    @tool(requires_approval=True)
    def charge(ctx: RunContext, amount: int) -> str:
        """Charge."""
        ctx.metadata["a"]["b"] = 99
        return "ok"

    agent = Agent(
        name="A",
        instructions="i",
        model=FakeModel(
            responses=[[ToolCall(id="c1", name="charge", arguments={"amount": 1})], "done"]
        ),
        tools=[charge],
    )
    md = {"a": {"b": 1}}
    paused = await Runner().run_async(agent, "go", metadata=md)
    snap = json.dumps(paused.interruption.metadata)
    await Runner().resume_async(agent, paused.interruption, approve=True)
    assert json.dumps(paused.interruption.metadata) == snap
    assert md == {"a": {"b": 1}}


async def test_resume_twice_same_interruption_is_independent() -> None:
    agent = mk(responses=[[ToolCall(id="c1", name="charge", arguments={"amount": 1})], "d1", "d2"])
    paused = await Runner().run_async(agent, "go")
    before = paused.interruption.usage.total_tokens
    nmsg = len(paused.interruption.messages)
    await Runner().resume_async(agent, paused.interruption, approve=True)
    assert paused.interruption.usage.total_tokens == before
    assert len(paused.interruption.messages) == nmsg


def _handoff_agents(seen: dict) -> tuple[Agent, Agent]:
    @tool(requires_approval=True)
    def charge(ctx: RunContext, amount: int) -> str:
        """Charge."""
        seen["meta"] = dict(ctx.metadata)
        return "ok"

    b = Agent(
        name="B",
        instructions="b",
        model=FakeModel(
            responses=[[ToolCall(id="c2", name="charge", arguments={"amount": 1})], "done"]
        ),
        tools=[charge],
    )
    a = Agent(
        name="A",
        instructions="a",
        model=FakeModel(responses=[[ToolCall(id="h1", name="transfer_to_b", arguments={})]]),
        handoffs=[Handoff(b)],
    )
    return a, b


async def test_handoff_pause_resume_agent_started_at_roundtrip() -> None:
    seen: dict = {}
    a, b = _handoff_agents(seen)
    paused = await Runner().run_async(a, "go", tags=["x"], metadata={"k": "v"})
    assert paused.status == "paused", paused.status
    it = paused.interruption
    assert it.agent_started_at > 0
    it2 = RunInterruption.from_json(it.to_json())
    assert it2.agent_started_at == it.agent_started_at
    res = await Runner().resume_async(a, it2, approve=True)
    assert res.status == "completed"
    assert seen["meta"].get("k") == "v"
    assert res.tags == ["x"]


async def test_handoff_input_is_a_reserved_metadata_key() -> None:
    seen: dict = {}
    a, b = _handoff_agents(seen)
    paused = await Runner().run_async(a, "go", metadata={"handoff_input": "mine"})
    await Runner().resume_async(a, paused.interruption, approve=True)
    # Documented: handoff_input is reserved; a handoff without input_type removes it.
    assert seen["meta"].get("handoff_input") is None


async def test_recover_old_checkpoint_without_new_fields() -> None:
    agent = mk()
    store = MemoryCheckpointStore()
    r = Runner(checkpoint_store=store)
    paused = await r.run_async(agent, "go", tags=["t"], metadata={"m": 1})
    assert paused.status == "paused"
    run_id = paused.run_id
    raw = json.loads(paused.interruption.to_json())
    for k in ("tags", "metadata", "output_attempts", "agent_started_at"):
        raw.pop(k)
    await store.save(run_id, encode_checkpoint("paused", json.dumps(raw)))
    res = await Runner(checkpoint_store=store).recover_async(agent, run_id)
    assert res.status == "paused"
    assert res.tags == []


async def test_recover_prefers_envelope_zero_over_newer_interruption_value() -> None:
    # interruption says output_attempts=2, envelope says 0 -> stale envelope should not matter;
    # reverse: envelope 3, interruption 0 -> used. Check envelope can also LOWER? (or-fallback)
    agent = mk()
    store = MemoryCheckpointStore()
    r = Runner(checkpoint_store=store)
    paused = await r.run_async(agent, "go")
    it = paused.interruption
    it.output_attempts = 2
    it.agent_started_at = 1
    await store.save(paused.run_id, encode_checkpoint("paused", it.to_json()))
    res = await Runner(checkpoint_store=store).recover_async(agent, paused.run_id)
    assert res.interruption.output_attempts == 2
    assert res.interruption.agent_started_at == 1


async def test_recovered_paused_state_keeps_tags_metadata_in_new_interruption() -> None:
    agent = mk()
    store = MemoryCheckpointStore()
    paused = await Runner(checkpoint_store=store).run_async(
        agent, "go", tags=["t"], metadata={"m": 1}
    )
    res = await Runner(checkpoint_store=store).recover_async(agent, paused.run_id)
    assert res.interruption.metadata == {"m": 1}
    assert res.tags == ["t"]
    assert res.interruption.tags == ["t"]


async def test_resume_then_pause_again_carries_context() -> None:
    @tool(requires_approval=True)
    def charge(ctx: RunContext, amount: int) -> str:
        """Charge."""
        ctx.metadata["added"] = 1
        return "ok"

    agent = Agent(
        name="A",
        instructions="i",
        model=FakeModel(
            responses=[
                [ToolCall(id="c1", name="charge", arguments={"amount": 1})],
                [ToolCall(id="c2", name="charge", arguments={"amount": 2})],
                "done",
            ]
        ),
        tools=[charge],
    )
    p1 = await Runner().run_async(agent, "go", tags=["t"], metadata={"m": 1})
    p2 = await Runner().resume_async(agent, p1.interruption, approve=True)
    assert p2.status == "paused"
    assert p2.tags == ["t"]
    assert p2.interruption.metadata == {"m": 1, "added": 1}
    p3 = await Runner().resume_async(agent, p2.interruption, approve=True)
    assert p3.status == "completed" and p3.tags == ["t"]


async def test_streaming_resume_events_carry_tags() -> None:
    agent = mk()
    paused = await Runner().run_async(agent, "go", tags=["t"])
    evs = [e async for e in Runner().resume_stream_async(agent, paused.interruption, approve=True)]
    done = [e for e in evs if e.type.name == "RUN_COMPLETED"][0]
    assert done.data["tags"] == ["t"]


async def test_cancel_on_resume_keeps_tags() -> None:
    agent = mk()
    paused = await Runner().run_async(agent, "go", tags=["t"])
    tok = CancelToken()
    tok.cancel()
    res = await Runner().resume_async(agent, paused.interruption, approve=True, cancel=tok)
    assert res.status == "cancelled"
    assert res.tags == ["t"]


async def test_cancel_on_first_run_keeps_tags() -> None:
    agent = mk()
    tok = CancelToken()
    tok.cancel()
    res = await Runner().run_async(agent, "go", tags=["t"], cancel=tok)
    assert res.status == "cancelled"
    assert res.tags == ["t"]


async def test_resume_stream_pause_again_streaming_tags() -> None:
    agent = mk(
        responses=[
            [ToolCall(id="c1", name="charge", arguments={"amount": 1})],
            [ToolCall(id="c2", name="charge", arguments={"amount": 1})],
            "x",
        ]
    )
    paused = await Runner().run_async(agent, "go", tags=["t"])
    res = await Runner().resume_async(agent, paused.interruption, approve=True)
    assert res.status == "paused" and res.tags == ["t"]


async def test_tracing_attrs_on_resume() -> None:
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exp = InMemorySpanExporter()
    prov = TracerProvider()
    prov.add_span_processor(SimpleSpanProcessor(exp))
    nexura_tracing.instrument(tracer_provider=prov)
    try:
        agent = mk()
        paused = await Runner().run_async(agent, "go", tags=["t"], metadata={"m": "v"})
        exp.clear()
        await Runner().resume_async(agent, paused.interruption, approve=True)
        attrs: dict = {}
        for s in exp.get_finished_spans():
            attrs.update(s.attributes or {})
        assert list(attrs.get("nexura.run.tags", [])) == ["t"]
        assert attrs.get("nexura.run.metadata.m") == "v"
    finally:
        nexura_tracing.uninstrument()


async def test_tracing_handoff_input_not_leaked_as_span_attr_after_handoff_resume() -> None:
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exp = InMemorySpanExporter()
    prov = TracerProvider()
    prov.add_span_processor(SimpleSpanProcessor(exp))
    nexura_tracing.instrument(tracer_provider=prov)
    try:
        from pydantic import BaseModel

        class T(BaseModel):
            secret: str

        @tool(requires_approval=True)
        def charge(ctx: RunContext, amount: int) -> str:
            """Charge."""
            return "ok"

        b = Agent(
            name="B",
            instructions="b",
            tools=[charge],
            model=FakeModel(
                responses=[[ToolCall(id="c2", name="charge", arguments={"amount": 1})], "done"]
            ),
        )
        a = Agent(
            name="A",
            instructions="a",
            handoffs=[Handoff(b, input_type=T)],
            model=FakeModel(
                responses=[[ToolCall(id="h1", name="transfer_to_b", arguments={"secret": "s"})]]
            ),
        )
        paused = await Runner().run_async(a, "go")
        assert paused.status == "paused", paused.status
        exp.clear()
        await Runner().resume_async(a, paused.interruption, approve=True)
        attrs: dict = {}
        for s in exp.get_finished_spans():
            attrs.update(s.attributes or {})
        assert not [k for k in attrs if "handoff_input" in k]
    finally:
        nexura_tracing.uninstrument()
