"""Tools that return images and files (F42)."""

from __future__ import annotations

import base64
from typing import Any

import pytest
from nexura_agents import Agent, FakeModel, Runner, ToolCall, ToolOutput, keep_last_turns, tool
from nexura_agents.attachments import Attachment
from nexura_agents.errors import SDKValidationError
from nexura_agents.providers.types import ProviderCapabilities

PNG = Attachment.from_bytes(base64.b64decode("iVBORw0KGgo="), "image/png", name="chart.png")


def call(name: str, call_id: str = "c1", **arguments: Any) -> list[ToolCall]:
    return [ToolCall(id=call_id, name=name, arguments=arguments)]


async def run(tools: list[Any], responses: list[Any], **kw: Any) -> tuple[Any, FakeModel]:
    model = FakeModel(responses=responses)
    agent = Agent(name="A", instructions="i", model=model, tools=tools, **kw)
    return await Runner().run_async(agent, "go"), model


async def test_tool_output_text_and_files_reach_the_model() -> None:
    @tool
    def plot(metric: str) -> ToolOutput:
        """Plot a metric."""
        return ToolOutput(f"{metric} rose 12%.", [PNG])

    result, model = await run([plot], [call("plot", metric="sales"), "Sales rose."])
    assert result.output == "Sales rose."
    sent = model.calls[1]["messages"]
    assert sent[-2].role == "tool" and sent[-2].content == "sales rose 12%."
    assert sent[-1].role == "user" and sent[-1].attachments == [PNG]
    assert "chart.png (image/png)" in (sent[-1].content or "")


async def test_returning_an_attachment_or_a_list() -> None:
    @tool
    def snapshot(x: str) -> Attachment:
        """Snapshot."""
        return PNG

    @tool
    def report(x: str) -> list[Any]:
        """Report."""
        return ["summary text", PNG]

    _, model = await run([snapshot], [call("snapshot", x="a"), "ok"])
    tool_text = model.calls[1]["messages"][-2].content
    assert "Returned 1 file(s): chart.png" in tool_text
    _, model = await run([report], [call("report", x="a"), "ok"])
    assert model.calls[1]["messages"][-2].content == "summary text"
    assert model.calls[1]["messages"][-1].attachments == [PNG]


async def test_files_from_a_parallel_batch_follow_all_tool_results() -> None:
    @tool
    def plot(metric: str) -> ToolOutput:
        """Plot."""
        return ToolOutput(metric, [PNG])

    batch = [
        ToolCall(id="c1", name="plot", arguments={"metric": "a"}),
        ToolCall(id="c2", name="plot", arguments={"metric": "b"}),
    ]
    _, model = await run([plot], [batch, "ok"], parallel_tool_calls=True)
    roles = [m.role for m in model.calls[1]["messages"]][-4:]
    assert roles == ["assistant", "tool", "tool", "user"]  # tool results stay adjacent
    assert len(model.calls[1]["messages"][-1].attachments) == 2


async def test_models_that_cannot_view_files_get_a_description() -> None:
    class TextOnly(FakeModel):
        capabilities = ProviderCapabilities(can_multimodal=False)

    @tool
    def plot(metric: str) -> ToolOutput:
        """Plot."""
        return ToolOutput("done", [PNG])

    model = TextOnly(responses=[call("plot", metric="a"), "ok"])
    agent = Agent(name="A", instructions="i", model=model, tools=[plot])
    await Runner().run_async(agent, "go")
    last = model.calls[1]["messages"][-1]
    assert last.attachments is None
    assert "cannot view files" in (last.content or "")


async def test_files_message_stays_in_the_current_turn_when_trimming() -> None:
    @tool
    def plot(metric: str) -> ToolOutput:
        """Plot."""
        return ToolOutput("done", [PNG])

    _, model = await run(
        [plot], [call("plot", metric="a"), "ok"], history_processors=[keep_last_turns(1)]
    )
    contents = [m.content for m in model.calls[1]["messages"]]
    assert "go" in contents  # the user's question was kept


async def test_cached_calls_show_their_files_again() -> None:
    @tool(idempotent=True, cache=True)
    def plot(metric: str) -> ToolOutput:
        """Plot."""
        return ToolOutput("done", [PNG])

    responses = [call("plot", metric="a"), call("plot", "c2", metric="a"), "ok"]
    _, model = await run([plot], responses)
    assert model.calls[2]["messages"][-1].attachments == [PNG]


def test_tool_output_validates() -> None:
    with pytest.raises(SDKValidationError):
        ToolOutput(3)  # type: ignore[arg-type]
    with pytest.raises(SDKValidationError):
        ToolOutput("x", ["not a file"])


# ---------------------------------------------------------------- review fix (privacy)


@pytest.mark.parametrize("outcome", ["rejected", "rewritten"])
async def test_files_of_a_guarded_result_are_withheld(outcome: str) -> None:
    from nexura_agents import GuardrailResult

    def guard(ctx: Any, text: Any) -> GuardrailResult:
        if outcome == "rejected":
            return GuardrailResult.rejected("contains customer data")
        return GuardrailResult.rewritten("[redacted]")

    @tool(output_guardrails=[guard])
    def export(q: str) -> ToolOutput:
        """Export."""
        return ToolOutput("rows", [PNG])

    _, model = await run([export], [call("export", q="x"), "ok"])
    sent = model.calls[1]["messages"]
    assert sent[-1].role == "tool"  # no follow-up message carrying the file
    assert not any(m.attachments for m in sent)


async def test_files_pass_when_the_guardrail_passes() -> None:
    from nexura_agents import GuardrailResult

    @tool(output_guardrails=[lambda ctx, text: GuardrailResult.passed()])
    def export(q: str) -> ToolOutput:
        """Export."""
        return ToolOutput("rows", [PNG])

    _, model = await run([export], [call("export", q="x"), "ok"])
    assert model.calls[1]["messages"][-1].attachments == [PNG]
