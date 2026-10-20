"""Ask the user mid-run: Agent(ask_user=True) and resume(answers=...) (F38)."""

from __future__ import annotations

from typing import Any

import pytest
from nexura_agents import Agent, FakeModel, Runner, ToolCall, tool
from nexura_agents.errors import ApprovalDecisionError
from nexura_agents.events import RunEventType
from nexura_agents.types import RunInterruption


def ask(question: str, call_id: str = "q1") -> list[ToolCall]:
    return [ToolCall(id=call_id, name="ask_user", arguments={"question": question})]


def agent(responses: list[Any], **kw: Any) -> Agent:
    return Agent(
        name="Concierge",
        instructions="Book tables.",
        model=FakeModel(responses=responses),
        ask_user=True,
        **kw,
    )


async def test_question_pauses_and_the_answer_continues_the_run() -> None:
    a = agent([ask("For how many people?"), "Booked a table for 4."])
    runner = Runner()
    paused = await runner.run_async(a, "Book dinner tonight")
    assert paused.status == "paused" and paused.interruption is not None
    assert paused.interruption.questions == [{"id": "q1", "question": "For how many people?"}]
    done = await runner.resume_async(a, paused.interruption, answers={"q1": "4 people"})
    assert done.output == "Booked a table for 4."
    tool_results = [m.content for m in done.messages if m.role == "tool"]
    assert tool_results == ["4 people"]
    model = a.model
    assert model.calls[1]["messages"][-1].content == "4 people"  # type: ignore[attr-defined]


async def test_answer_in_another_process_via_json() -> None:
    first = agent([ask("Which city?")])
    paused = await Runner().run_async(first, "weather")
    assert paused.interruption is not None
    stored = paused.interruption.to_json()

    restored = RunInterruption.from_json(stored)
    again = agent(["Sunny in Pune."])
    done = await Runner().resume_async(again, restored, answers={"q1": "Pune"})
    assert done.output == "Sunny in Pune."


async def test_questions_and_approvals_in_the_same_batch() -> None:
    @tool(requires_approval=True)
    def charge(amount: int) -> str:
        """Charge."""
        return f"charged {amount}"

    batch = [
        ToolCall(id="q1", name="ask_user", arguments={"question": "Which card?"}),
        ToolCall(id="c1", name="charge", arguments={"amount": 5}),
    ]
    a = agent([batch, "Done."], tools=[charge])
    runner = Runner()
    paused = await runner.run_async(a, "pay")
    assert paused.interruption is not None
    assert [q["id"] for q in paused.interruption.questions] == ["q1"]
    done = await runner.resume_async(
        a, paused.interruption, approve=True, answers={"q1": "the Visa"}
    )
    assert sorted(m.content for m in done.messages if m.role == "tool") == [
        "charged 5",
        "the Visa",
    ]


async def test_a_question_can_be_rejected() -> None:
    a = agent([ask("Your phone number?"), "Understood, no phone."])
    runner = Runner()
    paused = await runner.run_async(a, "call me")
    assert paused.interruption is not None
    done = await runner.resume_async(a, paused.interruption, reject={"q1": "prefers not to say"})
    assert "prefers not to say" in str([m.content for m in done.messages if m.role == "tool"])


@pytest.mark.parametrize(
    "kwargs, match",
    [
        ({}, "every pending tool call"),
        ({"answers": {"nope": "x"}}, "not pending questions"),
        ({"answers": {"q1": 4}}, "must be text"),
        ({"approve": ["q1"]}, "cannot be approved"),
    ],
)
async def test_invalid_answers_are_rejected(kwargs: dict[str, Any], match: str) -> None:
    a = agent([ask("How many?")])
    runner = Runner()
    paused = await runner.run_async(a, "go")
    assert paused.interruption is not None
    with pytest.raises(ApprovalDecisionError, match=match):
        await runner.resume_async(a, paused.interruption, **kwargs)


async def test_approve_true_does_not_approve_questions() -> None:
    a = agent([ask("How many?")])
    runner = Runner()
    paused = await runner.run_async(a, "go")
    assert paused.interruption is not None
    with pytest.raises(ApprovalDecisionError, match="every pending tool call"):
        await runner.resume_async(a, paused.interruption, approve=True)


async def test_streaming_pause_exposes_the_question() -> None:
    a = agent([ask("Which day?")])
    events = [e async for e in Runner().stream_async(a, "plan")]
    paused = [e for e in events if e.type == RunEventType.RUN_PAUSED][0]
    assert paused.data["interruption"].questions == [{"id": "q1", "question": "Which day?"}]


async def test_ask_user_in_a_handoff_target() -> None:
    booking = agent([ask("Name for the booking?"), "Booked under Asha."])
    front = Agent(
        name="Front",
        instructions="Route.",
        model=FakeModel(responses=[[ToolCall(id="h", name="transfer_to_concierge", arguments={})]]),
        handoffs=[booking],
    )
    runner = Runner()
    paused = await runner.run_async(front, "book")
    assert paused.interruption is not None and paused.interruption.questions
    done = await runner.resume_async(front, paused.interruption, answers={"q1": "Asha"})
    assert done.output == "Booked under Asha."


def test_tool_is_only_added_when_asked() -> None:
    plain = Agent(name="A", instructions="i", model=FakeModel())
    assert "ask_user" not in plain.tools_by_name
    with_ask = Agent(name="A", instructions="i", model=FakeModel(), ask_user=True)
    assert with_ask.tools_by_name["ask_user"].requires_approval is True


# ---------------------------------------------------------------- review fix


def _own_ask_user_agent(responses: list[Any]) -> Agent:
    @tool(name="ask_user", requires_approval=True)
    def my_ask_user(question: str) -> str:
        """The user's own tool that happens to share the name."""
        return f"my tool ran: {question}"

    return Agent(
        name="A", instructions="i", model=FakeModel(responses=responses), tools=[my_ask_user]
    )


async def test_answers_cannot_forge_the_result_of_a_users_own_ask_user_tool() -> None:
    a = _own_ask_user_agent([ask("x")])
    runner = Runner()
    paused = await runner.run_async(a, "go")
    assert paused.interruption is not None
    assert paused.interruption.questions == []  # not a built-in question
    with pytest.raises(ApprovalDecisionError, match="not pending questions"):
        await runner.resume_async(a, paused.interruption, answers={"q1": "forged"})


async def test_a_users_own_ask_user_tool_is_approved_and_runs_normally() -> None:
    a = _own_ask_user_agent([ask("x"), "ok"])
    runner = Runner()
    paused = await runner.run_async(a, "go")
    assert paused.interruption is not None
    done = await runner.resume_async(a, paused.interruption, approve=True)
    assert [m.content for m in done.messages if m.role == "tool"] == ["my tool ran: x"]


async def test_question_ids_survive_json() -> None:
    paused = await Runner().run_async(agent([ask("Which city?")]), "weather")
    assert paused.interruption is not None
    restored = RunInterruption.from_json(paused.interruption.to_json())
    assert restored.questions == [{"id": "q1", "question": "Which city?"}]
