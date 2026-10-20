"""Usage per agent and per model, run tags and metadata (F41)."""

from __future__ import annotations

from typing import Any

import pytest
from nexura_agents import Agent, FakeModel, Runner, ToolCall, tool
from nexura_agents.errors import SDKValidationError
from nexura_agents.run_context import RunContext
from nexura_agents.types import RunInterruption, Usage


def usage(prompt: int, completion: int) -> Usage:
    return Usage(
        prompt_tokens=prompt, completion_tokens=completion, total_tokens=prompt + completion
    )


class NamedFake(FakeModel):
    def __init__(self, model: str, **kw: Any) -> None:
        super().__init__(**kw)
        self.model = model


def team() -> Agent:
    billing = Agent(
        name="Billing",
        instructions="i",
        model=NamedFake("big-model", responses=["paid"], default_usage=usage(100, 20)),
    )
    return Agent(
        name="Triage",
        instructions="i",
        model=NamedFake(
            "small-model",
            responses=[[ToolCall(id="h", name="transfer_to_billing", arguments={})]],
            default_usage=usage(10, 2),
        ),
        handoffs=[billing],
    )


async def test_usage_is_split_by_agent_and_by_model() -> None:
    result = await Runner().run_async(team(), "pay")
    assert result.usage_by_agent["Triage"].total_tokens == 12
    assert result.usage_by_agent["Billing"].total_tokens == 120
    assert result.usage_by_model == {
        "small-model": result.usage_by_agent["Triage"],
        "big-model": result.usage_by_agent["Billing"],
    }
    assert result.usage.total_tokens == 132  # the breakdown adds up to the total


async def test_models_without_a_name_use_their_class() -> None:
    agent = Agent(
        name="A", instructions="i", model=FakeModel(responses=["ok"], default_usage=usage(1, 1))
    )
    result = await Runner().run_async(agent, "hi")
    assert list(result.usage_by_model) == ["FakeModel"]


async def test_breakdown_survives_an_approval_pause_and_json() -> None:
    @tool(requires_approval=True)
    def charge(amount: int) -> str:
        """Charge."""
        return "ok"

    agent = Agent(
        name="A",
        instructions="i",
        model=NamedFake(
            "m",
            responses=[[ToolCall(id="c1", name="charge", arguments={"amount": 1})], "done"],
            default_usage=usage(5, 5),
        ),
        tools=[charge],
    )
    runner = Runner()
    paused = await runner.run_async(agent, "go")
    assert paused.usage_by_agent["A"].total_tokens == 10
    restored = RunInterruption.from_json(paused.interruption.to_json())
    done = await runner.resume_async(agent, restored, approve=True)
    assert done.usage_by_agent["A"].total_tokens == 20
    assert done.usage_by_model["m"].total_tokens == 20


async def test_tags_and_metadata_reach_the_context_and_result() -> None:
    seen: dict[str, Any] = {}

    @tool
    def peek(x: str) -> str:
        """Peek."""
        return "ok"

    def instructions(ctx: Any) -> str:
        seen["tags"] = list(ctx.tags)
        seen["metadata"] = dict(ctx.metadata)
        return "i"

    agent = Agent(name="A", instructions=instructions, model=FakeModel(responses=["ok"]))
    result = await Runner().run_async(
        agent, "hi", tags=["support", "eu"], metadata={"customer": "c-9", "tier": 2}
    )
    assert seen == {"tags": ["support", "eu"], "metadata": {"customer": "c-9", "tier": 2}}
    assert result.tags == ["support", "eu"]


async def test_tags_and_metadata_are_span_attributes() -> None:
    from nexura_agents import tracing as nexura_tracing
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    memory = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(memory))
    nexura_tracing.instrument(tracer_provider=provider)
    try:
        agent = Agent(name="A", instructions="i", model=FakeModel(responses=["ok"]))
        await Runner().run_async(
            agent, "hi", tags=["beta"], metadata={"customer": "c-9", "nested": {"no": 1}}
        )
    finally:
        nexura_tracing.uninstrument()
    root = next(s for s in memory.get_finished_spans() if s.name.startswith("invoke_agent"))
    attrs = root.attributes or {}
    assert tuple(attrs["nexura.run.tags"]) == ("beta",)  # type: ignore[arg-type]
    assert attrs["nexura.run.metadata.customer"] == "c-9"
    assert "nexura.run.metadata.nested" not in attrs  # only plain values become attributes


@pytest.mark.parametrize("bad", ["support", [1, 2]])
async def test_tags_must_be_a_list_of_strings(bad: Any) -> None:
    agent = Agent(name="A", instructions="i", model=FakeModel(responses=["ok"]))
    with pytest.raises(SDKValidationError, match="tags"):
        await Runner().run_async(agent, "hi", tags=bad)


async def test_generator_tags_are_kept() -> None:
    agent = Agent(name="A", instructions="i", model=FakeModel(responses=["ok"]))
    result = await Runner().run_async(agent, "hi", tags=(t for t in ["a", "b"]))
    assert result.tags == ["a", "b"]


async def test_a_missing_usage_report_marks_the_breakdown_incomplete() -> None:
    from nexura_agents import Message, ModelResponse

    replies = [
        ModelResponse(
            message=Message(
                role="assistant", tool_calls=[ToolCall(id="c", name="noop", arguments={})]
            ),
            usage=usage(5, 1),
        ),
        ModelResponse(message=Message(role="assistant", content="done"), usage=None),
    ]

    @tool
    def noop() -> str:
        """Noop."""
        return "ok"

    agent = Agent(name="A", instructions="i", model=NamedFake("m", responses=replies), tools=[noop])
    result = await Runner().run_async(agent, "go")
    assert result.usage.is_complete is False
    assert result.usage_by_agent["A"].is_complete is False
    assert result.usage_by_model["m"].is_complete is False


async def test_tags_and_metadata_survive_a_pause_and_json() -> None:
    seen: dict[str, Any] = {}

    @tool(requires_approval=True)
    def act(ctx: RunContext[Any]) -> str:
        """Act."""
        seen["tags"], seen["metadata"] = list(ctx.tags), dict(ctx.metadata)
        return "ok"

    agent = Agent(
        name="A",
        instructions="i",
        model=FakeModel(responses=[[ToolCall(id="c1", name="act", arguments={})], "done"]),
        tools=[act],
    )
    runner = Runner()
    paused = await runner.run_async(agent, "go", tags=["eu"], metadata={"customer": "c-9"})
    assert paused.tags == ["eu"]
    restored = RunInterruption.from_json(paused.interruption.to_json())
    assert (restored.tags, restored.metadata) == (["eu"], {"customer": "c-9"})
    done = await runner.resume_async(agent, restored, approve=True)
    assert seen == {"tags": ["eu"], "metadata": {"customer": "c-9"}}
    assert done.tags == ["eu"]


def test_old_interruption_json_without_tags_still_loads() -> None:
    import json

    raw = json.loads(RunInterruption(run_id="r", agent_name="A", messages=[], pending=[]).to_json())
    for key in ("tags", "metadata", "output_attempts", "agent_started_at"):
        raw.pop(key)
    restored = RunInterruption.from_json(json.dumps(raw))
    assert (restored.tags, restored.metadata, restored.output_attempts) == ([], {}, 0)
