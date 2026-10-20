"""GroupChat: selection, termination, transcript, usage, approvals, streaming, validation."""

from __future__ import annotations

from typing import Any

import pytest
from nexura_agents import (
    Agent,
    FakeModel,
    Runner,
    SequentialAgent,
    ToolCall,
    Usage,
    UsageLimits,
    tool,
)
from nexura_agents.errors import SDKValidationError, UsageLimitExceededError
from nexura_agents.events import RunEventType
from nexura_agents.teams import (
    GroupChat,
    GroupChatTurn,
    MaxTurns,
    TeamInterruption,
    TextMention,
)

USAGE = Usage(prompt_tokens=3, completion_tokens=2, total_tokens=5)


def say(name: str, *replies: Any, description: str = "") -> Agent:
    model = FakeModel(responses=list(replies), default_usage=USAGE)
    return Agent(name=name, instructions="", model=model, description=description)


def last_prompt(agent: Agent, index: int = -1) -> str:
    return agent.model.calls[index]["messages"][-1].content  # type: ignore[attr-defined, no-any-return]


def speakers(result: Any) -> list[str]:
    return [s.path for s in result.steps]


def test_round_robin_order_and_transcript() -> None:
    a = say("A", "a1", "a2")
    b = say("B", "b1")
    chat = GroupChat("Chat", [a, b], max_turns=3)
    result = chat.run("topic")
    assert result.status == "completed"
    assert speakers(result) == ["turn-1:A", "turn-2:B", "turn-3:A"]
    assert result.output == "a2" and result.stop_reason == "max_turns"
    assert last_prompt(a, 0) == "topic"
    assert last_prompt(b) == "topic\n\nConversation so far:\nA: a1"
    assert last_prompt(a, 1) == "topic\n\nConversation so far:\nA: a1\nB: b1"
    assert result.usage.total_tokens == 15


def test_callable_selector_gets_turns_and_names() -> None:
    a, b = say("A", "a1", "a2"), say("B", "b1")
    seen: list[tuple[list[GroupChatTurn], list[str]]] = []

    def pick(transcript: list[GroupChatTurn], names: list[str]) -> str:
        seen.append((list(transcript), names))
        return "B" if len(transcript) == 1 else "A"

    result = GroupChat("C", [a, b], max_turns=3, select=pick).run("go")
    assert speakers(result) == ["turn-1:A", "turn-2:B", "turn-3:A"]
    assert seen[0] == ([], ["A", "B"])
    assert seen[1][0] == [GroupChatTurn(1, "A", "a1", "a1")]


def test_callable_selector_bad_name_fails() -> None:
    chat = GroupChat("C", [say("A", "x")], max_turns=2, select=lambda t, n: "Nobody")
    with pytest.raises(SDKValidationError, match="Nobody"):
        chat.run("go")


def test_manager_selector_valid_and_tolerant_and_counts_usage() -> None:
    a = say("Writer", "draft", description="writes")
    b = say("Critic", "notes", description="critiques")
    manager = say("Boss", "  critic \n", "WRITER")
    chat = GroupChat("C", [a, b], max_turns=2, select=manager)
    result = chat.run("go")
    assert speakers(result) == ["select-1", "turn-1:Critic", "select-2", "turn-2:Writer"]
    asked = last_prompt(manager, 0)
    assert "- Writer: writes" in asked and "- Critic: critiques" in asked
    assert "nobody has spoken yet" in asked
    assert "Critic: notes" in last_prompt(manager, 1)
    assert result.output == "draft"
    assert result.usage.total_tokens == 20  # two members + two manager answers


def test_manager_invalid_answer_fails() -> None:
    manager = say("Boss", "Somebody else")
    chat = GroupChat("C", [say("A", "x"), say("B", "y")], max_turns=2, select=manager)
    with pytest.raises(SDKValidationError, match="Somebody else"):
        chat.run("go")


def test_manager_usage_counts_toward_usage_limits() -> None:
    manager = say("Boss", "A", "A")
    runner = Runner(usage_limits=UsageLimits(total_tokens_limit=12))
    chat = GroupChat("C", [say("A", "x", "y")], max_turns=2, select=manager, runner=runner)
    with pytest.raises(UsageLimitExceededError):
        chat.run("go")


def test_text_mention_case_insensitive() -> None:
    chat = GroupChat(
        "C",
        [say("A", "hello", "all Done now"), say("B", "ok")],
        max_turns=10,
        terminate=TextMention("done"),
    )
    result = chat.run("go")
    assert speakers(result) == ["turn-1:A", "turn-2:B", "turn-3:A"]
    assert result.stop_reason == "text_mention('done')"


def test_text_mention_only_checks_last_speaker() -> None:
    chat = GroupChat(
        "C", [say("A", "DONE", "x"), say("B", "ok")], max_turns=3, terminate=TextMention("done")
    )
    assert chat.run("go").stop_reason == "text_mention('done')"
    chat = GroupChat(
        "C", [say("A", "hello", "DONE"), say("B", "ok")], max_turns=2, terminate=TextMention("done")
    )
    assert chat.run("go").stop_reason == "max_turns"


def test_max_turns_condition_and_callable() -> None:
    chat = GroupChat("C", [say("A", "1", "2", "3")], max_turns=10, terminate=MaxTurns(2))
    result = chat.run("go")
    assert len(result.steps) == 2 and result.stop_reason == "max_turns(2)"
    chat = GroupChat(
        "C", [say("A", "1", "2", "3")], max_turns=10, terminate=lambda t: t[-1].text == "2"
    )
    assert len(chat.run("go").steps) == 2


def test_or_and_combinations() -> None:
    either = TextMention("stop") | MaxTurns(2)
    r = GroupChat("C", [say("A", "a", "b", "c")], max_turns=9, terminate=either).run("go")
    assert len(r.steps) == 2 and r.stop_reason == "max_turns(2)"
    both = TextMention("c") & MaxTurns(3)
    r = GroupChat("C", [say("A", "a", "b", "c")], max_turns=9, terminate=both).run("go")
    assert len(r.steps) == 3
    assert r.stop_reason == "text_mention('c') and max_turns(3)"
    # Plain callables mix in on either side.
    r = GroupChat(
        "C", [say("A", "a", "b", "c")], max_turns=9, terminate=(lambda t: len(t) == 1) | MaxTurns(5)
    ).run("go")
    assert len(r.steps) == 1
    r = GroupChat(
        "C", [say("A", "a", "b", "c")], max_turns=9, terminate=MaxTurns(2) & (lambda t: True)
    ).run("go")
    assert len(r.steps) == 2


def test_and_needs_both() -> None:
    chat = GroupChat(
        "C", [say("A", "x", "x", "x")], max_turns=3, terminate=TextMention("zzz") & MaxTurns(1)
    )
    assert chat.run("go").stop_reason == "max_turns"


def test_hard_cap_wins_over_unmet_condition() -> None:
    chat = GroupChat("C", [say("A", "1", "2")], max_turns=2, terminate=MaxTurns(50))
    result = chat.run("go")
    assert len(result.steps) == 2 and result.stop_reason == "max_turns"


def test_history_is_bounded() -> None:
    a = say("A", *[f"line{i}" for i in range(6)])
    GroupChat("C", [a], max_turns=5, history_turns=2).run("t")
    assert (
        last_prompt(a) == "t\n\nConversation so far:\n[earlier turns omitted]\nA: line2\nA: line3"
    )
    b = say("B", "x" * 50, "y" * 50, "z")
    GroupChat("C", [b], max_turns=3, history_chars=60).run("t")
    tail = last_prompt(b)
    assert "[earlier turns omitted]" in tail and "x" * 10 not in tail and "B: " + "y" * 50 in tail


def test_attachment_style_prompt_keeps_parts() -> None:
    a = say("A", "one", "two")
    GroupChat("C", [a], max_turns=2).run(["look", "closely"])
    assert a.model.calls[1]["messages"][-1].content  # type: ignore[attr-defined]


class Shop:
    def __init__(self) -> None:
        self.refunds: list[str] = []


def gated(shop: Shop, resumed: bool = False) -> Agent:
    @tool(requires_approval=True)
    def refund(order: str) -> str:
        """Refund."""
        shop.refunds.append(order)
        return "ok"

    replies: list[Any] = ["refunded"]
    if not resumed:
        replies.insert(0, [ToolCall(id="c1", name="refund", arguments={"order": "A1"})])
    model = FakeModel(responses=replies, default_usage=USAGE)
    return Agent(name="Gated", instructions="", model=model, tools=[refund])


def test_pause_and_resume_continues_the_same_turn_and_rest() -> None:
    runner = Runner(approval_signing_secret=b"offline-group-chat-roundtrip")
    shop = Shop()
    first = say("First", "hi", "again")
    manager = say("Boss", "First", "Gated", "First")
    chat = GroupChat("C", [first, gated(shop)], max_turns=3, select=manager)
    paused = chat.run("go", runner=runner)
    assert paused.status == "paused" and paused.interruption is not None
    assert list(paused.interruption.pending) == ["turn-2:Gated"]
    assert shop.refunds == []
    saved = paused.interruption.to_json()
    # A new process: rebuilt agents; the selector must not be asked again for turns 1 and 2.
    shop2 = Shop()
    first2 = say("First", "again")
    manager2 = say("Boss", "First")
    chat2 = GroupChat("C", [first2, gated(shop2, resumed=True)], max_turns=3, select=manager2)
    interruption = TeamInterruption.from_json(saved)
    final = chat2.resume(
        interruption,
        approve=interruption.pending_digests(),
        runner=Runner(approval_signing_secret=b"offline-group-chat-roundtrip"),
    )
    assert final.status == "completed"
    assert shop2.refunds == ["A1"]
    assert [s.path for s in final.steps] == [
        "select-1",
        "turn-1:First",
        "select-2",
        "turn-2:Gated",
        "select-3",
        "turn-3:First",
    ]
    assert len(manager2.model.calls) == 1  # type: ignore[attr-defined]  # only turn 3
    assert len(first2.model.calls) == 1  # type: ignore[attr-defined]  # turn 1 not rerun
    assert final.output == "again" and final.stop_reason == "max_turns"
    # 3 manager + first + gated (before and after approval) + first
    assert final.usage.total_tokens == 7 * 5


def test_pause_with_round_robin_and_terminate_not_asked_twice() -> None:
    shop = Shop()
    calls: list[int] = []

    def stop(transcript: list[GroupChatTurn]) -> bool:
        calls.append(len(transcript))
        return False

    chat = GroupChat("C", [say("First", "hi"), gated(shop)], max_turns=2, terminate=stop)
    paused = chat.run("go")
    assert paused.status == "paused" and paused.output == "hi"
    assert calls == [1]
    chat2 = GroupChat(
        "C", [say("First", "hi"), gated(Shop(), resumed=True)], max_turns=2, terminate=stop
    )
    final = chat2.resume(paused.interruption, approve=True)  # type: ignore[arg-type]
    assert final.status == "completed" and final.output == "refunded"
    assert calls == [1, 2]  # turn 1 was not checked again


async def test_stream_async_events() -> None:
    chat = GroupChat("C", [say("A", "a1"), say("B", "b1")], max_turns=2)
    events = [e async for e in chat.stream_async("go")]
    types = [e.type for e in events]
    assert types[0] == RunEventType.TEAM_STARTED and types[-1] == RunEventType.TEAM_COMPLETED
    started = [e.data["member_path"] for e in events if e.type == RunEventType.MEMBER_STARTED]
    assert started == [["turn-1:A"], ["turn-2:B"]]
    assert events[-1].data["result"].output == "b1"


def test_nested_in_sequential() -> None:
    chat = GroupChat("Chat", [say("A", "a1"), say("B", "b1")], max_turns=2)
    last = say("Last", "final")
    result = SequentialAgent("Pipe", [chat, last]).run("go")
    assert result.output == "final"
    assert last_prompt(last) == "b1"
    assert result.usage.total_tokens == 15


def test_nested_group_chat_pauses_and_resumes_through_parent() -> None:
    shop = Shop()
    inner = GroupChat("Chat", [say("A", "a1"), gated(shop)], max_turns=2)
    pipe = SequentialAgent("Pipe", [inner, say("Last", "final")])
    paused = pipe.run("go")
    assert paused.interruption is not None
    assert list(paused.interruption.pending) == ["0/turn-2:Gated"]
    inner2 = GroupChat("Chat", [say("A", "a1"), gated(shop, resumed=True)], max_turns=2)
    pipe2 = SequentialAgent("Pipe", [inner2, say("Last", "final")])
    final = pipe2.resume(paused.interruption, approve=True)
    assert final.output == "final" and shop.refunds == ["A1"]


def test_invalid_arguments() -> None:
    a, b = say("A", "x"), say("B", "y")
    for bad in (0, 101, True, 2.5, None):
        with pytest.raises(SDKValidationError, match="max_turns"):
            GroupChat("C", [a], max_turns=bad)  # type: ignore[arg-type]
    GroupChat("C", [a], max_turns=100)
    with pytest.raises(SDKValidationError, match="at least one"):
        GroupChat("C", [], max_turns=1)
    with pytest.raises(SDKValidationError, match="Duplicate"):
        GroupChat("C", [a, say("A", "z")], max_turns=1)
    with pytest.raises(SDKValidationError, match="ignoring case"):
        GroupChat("C", [a, say("a", "z")], max_turns=1)
    with pytest.raises(SDKValidationError, match="'/'"):
        GroupChat("C", [say("x/y", "z")], max_turns=1)
    with pytest.raises(SDKValidationError, match="select"):
        GroupChat("C", [a, b], max_turns=1, select="random")  # type: ignore[arg-type]
    with pytest.raises(SDKValidationError, match="manager"):
        GroupChat("C", [a, b], max_turns=1, select=a)
    with pytest.raises(SDKValidationError, match="condition"):
        GroupChat("C", [a], max_turns=1, terminate="DONE")  # type: ignore[arg-type]
    with pytest.raises(SDKValidationError):
        MaxTurns(0)
    with pytest.raises(SDKValidationError):
        TextMention("")
    for kw in ("history_turns", "history_chars"):
        with pytest.raises(SDKValidationError, match=kw):
            GroupChat("C", [a], max_turns=1, **{kw: 0})
