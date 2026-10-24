"""R3-2 regression: local fake models and an in-memory OTel exporter only."""

from __future__ import annotations

import copy
import hmac
import re
from typing import Any

import pytest
from nexura_agents import (
    Agent,
    FakeModel,
    GuardrailResult,
    Harness,
    MemoryCheckpointStore,
    Message,
    ResolvedOutcome,
    RunContext,
    RunInterruption,
    Runner,
    ToolCall,
    tool,
    tracing,
)
from nexura_agents._agent_journal import call_step_key
from nexura_agents.contracts.execution import (
    EffectRecord,
    ExecutionId,
    StepIntent,
    StepKey,
    default_idempotency_key,
    derive_effect_id,
)
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


def call(identifier: str, name: str, **arguments: Any) -> Message:
    return Message("assistant", tool_calls=[ToolCall(identifier, name, arguments)])


# Copied from the independent review's repro; assertion unchanged.
async def test_model_tool_call_id_is_not_captured_when_content_capture_is_off() -> None:
    private = "PRIVATE-MODEL-CALL-ID"
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracing.instrument(tracer_provider=provider, capture_content=False)
    try:
        agent = Agent(
            "A",
            "local fake",
            FakeModel([call(private, "write_todos", items=[])]),
            harness=Harness(todo=True),
            tool_use_behavior="stop_on_first_tool",
            output_guardrails=[lambda ctx, value: GuardrailResult.rewritten("[checked]")],
        )
        await Runner(checkpoint_store=MemoryCheckpointStore()).run_async(agent, "go")
        attributes = [
            (
                dict(s.attributes or {}),
                [(e.name, dict(e.attributes or {})) for e in s.events],
            )
            for s in exporter.get_finished_spans()
        ]
        assert private not in str(attributes), attributes
    finally:
        tracing.uninstrument()


@pytest.mark.parametrize("capture", [False, True])
@pytest.mark.parametrize("private", ["", "PRIVATE-MODEL-CALL-ID", "PRIVATE-%23#2/call:秘密"])
async def test_tool_and_effect_ids_correlate_without_changing_journal(
    capture: bool, private: str
) -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracing.instrument(tracer_provider=provider, capture_content=capture)
    exported_ids = []

    def scrub(ctx: Any, value: str) -> GuardrailResult:
        return GuardrailResult.rewritten("[checked]")

    @tool(output_guardrails=[scrub])
    def write() -> str:
        """A local fake side effect."""
        return "done"

    try:
        # Reuse the same model id in two turns and then in a second independent run.
        for _ in range(2):
            exporter.clear()
            store = MemoryCheckpointStore()
            agent = Agent(
                "A",
                "local fake",
                FakeModel([call(private, "write"), call(private, "write"), "done"]),
                tools=[write],
            )
            result = await Runner(checkpoint_store=store).run_async(agent, "go")
            expected = (
                private
                if capture
                else "call-"
                + hmac.digest(result.run_id.encode(), private.encode(), "sha256").hex()[:16]
            )
            exported_ids.append(expected)
            spans = exporter.get_finished_spans()
            tools = [span for span in spans if span.name == "execute_tool write"]
            assert len(tools) == 2
            assert [dict(span.attributes or {})[tracing.TOOL_CALL_ID] for span in tools] == [
                expected,
                expected,
            ]
            decisions = [
                dict(event.attributes or {})
                for span in tools
                for event in span.events
                if event.name == tracing.EVENT_GUARDRAIL_DECISION
            ]
            assert len(decisions) == 2
            assert [event[tracing.TOOL_CALL_ID] for event in decisions] == [expected, expected]
            effects = [
                dict(event.attributes or {})
                for span in spans
                for event in span.events
                if event.name.startswith("nexura.effect.")
            ]
            assert len(effects) == 4  # Intent and outcome for each dispatch.
            assert [event["nexura.effect.step_key"] for event in effects] == [
                str(call_step_key(turn, expected)) for turn in (1, 1, 2, 2)
            ]
            if not capture:
                assert re.fullmatch(r"call-[0-9a-f]{16}", expected)
                if private:
                    assert private not in str(
                        [(dict(span.attributes or {}), span.events) for span in spans]
                    )

            journal = await store.open_execution_store()
            eid = ExecutionId("default", result.run_id)
            snapshot = await journal.get(eid)
            keys = [call_step_key(turn, private) for turn in (1, 2)]
            assert set(snapshot.steps) == set(keys)
            assert len(snapshot.effects) == 2
            for key in keys:
                intent = snapshot.steps[key].intent
                assert intent is not None
                assert intent.step_key == key
                assert intent.payload_digest == RunInterruption.action_digest(
                    ToolCall(private, "write", {})
                )
                assert intent.idempotency_key == default_idempotency_key(eid, key)
            for effect in snapshot.effects.values():
                assert effect.effect_id == derive_effect_id(eid, effect.intent.step_key)
                assert {event["nexura.effect.id"] for event in effects} == set(snapshot.effects)
            assert (await journal.get(eid)) == snapshot
        assert (exported_ids[0] == exported_ids[1]) is capture
    finally:
        tracing.uninstrument()


@pytest.mark.parametrize("capture", [False, True])
def test_effect_export_preserves_retry_suffix_and_input_records(capture: bool) -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracing.instrument(tracer_provider=provider, capture_content=capture)
    private = "PRIVATE-%23#2/call:秘密"
    run_id = "local-run"
    pseudonym = "call-" + hmac.digest(run_id.encode(), private.encode(), "sha256").hex()[:16]
    keys = [call_step_key(7, private, 3), StepKey("local-workflow#2.3[0]")]
    records = [
        EffectRecord(ExecutionId("default", run_id), StepIntent(key, "digest", "idempotency"))
        for key in keys
    ]
    before = copy.deepcopy(records)
    try:
        tracing.effects_committed(records, run_id=run_id)
        attributes = [dict(span.attributes or {}) for span in exporter.get_finished_spans()]
        assert [attrs["nexura.effect.step_key"] for attrs in attributes] == [
            str(keys[0]) if capture else f"turn:7/call:{pseudonym}#3",
            str(keys[1]),
        ]
        assert [attrs["nexura.effect.id"] for attrs in attributes] == [
            effect.effect_id for effect in before
        ]
        assert records == before
    finally:
        tracing.uninstrument()


@pytest.mark.parametrize("private", ["", "PRIVATE-APPROVAL-CALL-ID"])
async def test_approval_span_and_resumed_tool_use_same_pseudonym(private: str) -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracing.instrument(tracer_provider=provider, capture_content=False)

    @tool(requires_approval=True)
    def write() -> str:
        """A local fake side effect requiring approval."""
        return "done"

    try:
        runner = Runner(checkpoint_store=MemoryCheckpointStore())
        agent = Agent("A", "local fake", FakeModel([call(private, "write"), "done"]), tools=[write])
        paused = await runner.run_async(agent, "go")
        assert paused.interruption is not None
        result = await runner.resume_async(
            agent, paused.interruption, approve=paused.interruption.pending_digests()
        )
        assert result.status == "completed"
        expected = (
            "call-" + hmac.digest(result.run_id.encode(), private.encode(), "sha256").hex()[:16]
        )
        tools = [s for s in exporter.get_finished_spans() if s.name == "execute_tool write"]
        assert len(tools) == 2
        assert [dict(s.attributes or {})[tracing.TOOL_CALL_ID] for s in tools] == [
            expected,
            expected,
        ]
        effects = [
            dict(e.attributes or {})["nexura.effect.step_key"]
            for s in exporter.get_finished_spans()
            for e in s.events
            if e.name.startswith("nexura.effect.")
        ]
        assert effects == [f"turn:1/call:{expected}", f"turn:1/call:{expected}"]
    finally:
        tracing.uninstrument()


async def test_empty_call_id_correlates_after_crash_recovery_and_resolution() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracing.instrument(tracer_provider=provider, capture_content=False)
    dispatched = []

    class Crash(BaseException):
        """Local simulated process death after dispatch."""

    @tool
    def write(ctx: RunContext[Any]) -> str:
        """A local fake non-idempotent side effect."""
        dispatched.append(ctx.run_id)
        raise Crash

    store = MemoryCheckpointStore()
    try:
        with pytest.raises(Crash):
            await Runner(checkpoint_store=store).run_async(
                Agent("A", "local fake", FakeModel([call("", "write")]), tools=[write]), "go"
            )
        (run_id,) = dispatched
        journal = await store.open_execution_store()
        eid = ExecutionId("default", run_id)
        before = await journal.get(eid)
        (original_effect,) = before.effects.values()

        runner = Runner(checkpoint_store=store)
        agent = Agent("A", "local fake", FakeModel(["done"]), tools=[write])
        paused = await runner.recover_async(agent, run_id)
        assert paused.status == "paused"
        assert paused.interruption is not None
        done = await runner.resume_async(
            agent, paused.interruption, outcomes={"": ResolvedOutcome("done")}
        )
        assert done.status == "completed"
        assert dispatched == [run_id]

        expected = "call-" + hmac.digest(run_id.encode(), b"", "sha256").hex()[:16]
        tools = [s for s in exporter.get_finished_spans() if s.name == "execute_tool write"]
        assert tools
        assert {dict(s.attributes or {})[tracing.TOOL_CALL_ID] for s in tools} == {expected}
        events = [
            (e.name, dict(e.attributes or {}))
            for s in exporter.get_finished_spans()
            for e in s.events
            if e.name.startswith("nexura.effect.")
        ]
        events.extend(
            (s.name, dict(s.attributes or {}))
            for s in exporter.get_finished_spans()
            if s.name.startswith("nexura.effect.")
        )
        assert {name for name, _ in events} == {
            "nexura.effect.intent",
            "nexura.effect.unknown",
        }
        assert {attrs["nexura.effect.step_key"] for _, attrs in events} == {
            f"turn:1/call:{expected}"
        }
        assert {attrs["nexura.effect.id"] for _, attrs in events} == {original_effect.effect_id}
        after = await journal.get(eid)
        assert set(after.steps) == {"turn:1/call:-"}
        assert after.steps["turn:1/call:-"].intent == before.steps["turn:1/call:-"].intent
        assert after.effects[original_effect.effect_id].status == "resolved"
        assert "_trace_call_ids" not in after.run.state
    finally:
        tracing.uninstrument()
