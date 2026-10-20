"""Trajectory and multi-turn evaluation scorers (FakeModel only, offline)."""

from __future__ import annotations

from typing import Any

import pytest
from nexura_agents import (
    Agent,
    Case,
    FakeModel,
    ParallelAgent,
    SequentialAgent,
    ToolCall,
    evaluate,
    tool,
)
from nexura_agents.errors import SDKValidationError
from nexura_agents.evals import (
    EvalOutput,
    Score,
    agent_path,
    contains,
    every_turn,
    exact_match,
    for_turn,
    tool_called,
    tool_trajectory,
)


@tool
def search(query: str) -> str:
    """Search."""
    return f"found {query}"


@tool
def fetch(url: str) -> str:
    """Fetch."""
    return f"page {url}"


@tool
def summarize(text: str) -> str:
    """Summarize."""
    return "short"


def calls_agent(calls: list[tuple[str, dict[str, Any]]], name: str = "bot") -> Agent:
    """An agent that makes ``calls`` (one per model turn) and then answers ``done``."""
    queue: list[Any] = [
        [ToolCall(id=f"c{i}", name=n, arguments=a)] for i, (n, a) in enumerate(calls)
    ]
    queue.append("done")
    return Agent(
        name=name,
        instructions="x",
        model=FakeModel(responses=queue),
        tools=[search, fetch, summarize],
    )


CALLS = [("search", {"query": "cats"}), ("fetch", {"url": "u1"}), ("summarize", {"text": "t"})]


def score_of(
    scorer: Any, calls: list[tuple[str, dict[str, Any]]] = CALLS, expected: Any = None
) -> Score:
    report = evaluate(calls_agent(calls), [Case("c", "go", expected=expected)], [scorer])
    assert report.cases[0].status in {"passed", "failed"}
    return report.cases[0].scores[0]


# --------------------------------------------------------------- modes


def test_strict_exact_order_passes() -> None:
    s = score_of(tool_trajectory(["search", "fetch", "summarize"]))
    assert s.passed and s.value == 1.0 and s.reason == "" and s.name == "tool_trajectory"


def test_strict_wrong_order_fails_with_reason_and_partial_credit() -> None:
    s = score_of(tool_trajectory(["fetch", "search", "summarize"]))
    assert not s.passed
    assert "wrong order" in s.reason and "mode=strict" in s.reason
    assert s.value == pytest.approx(2 / 3)


def test_strict_missing_call() -> None:
    s = score_of(tool_trajectory(["search", "fetch", "summarize", "search"]))
    assert not s.passed
    assert "missing calls: ['search']" in s.reason
    assert s.value == pytest.approx(3 / 4)


def test_strict_extra_call() -> None:
    s = score_of(tool_trajectory(["search", "fetch"]))
    assert not s.passed
    assert "unexpected calls: ['summarize']" in s.reason  # arguments are not checked here
    assert s.value == pytest.approx(2 / 3)


def test_unordered_accepts_any_order_but_not_extras() -> None:
    assert score_of(tool_trajectory(["summarize", "search", "fetch"], mode="unordered")).passed
    s = score_of(tool_trajectory(["search", "fetch"], mode="unordered"))
    assert not s.passed and s.value == pytest.approx(2 / 3)


def test_subset_allows_fewer_calls_but_not_unexpected_ones() -> None:
    allowed = ["search", "fetch", "summarize", "other"]
    assert score_of(tool_trajectory(allowed, mode="subset")).passed
    s = score_of(tool_trajectory(["search"], mode="subset"))
    assert not s.passed
    assert "unexpected calls" in s.reason and "fetch" in s.reason
    assert s.value == pytest.approx(1 / 3)


def test_superset_allows_extra_calls_but_not_missing_ones() -> None:
    assert score_of(tool_trajectory(["fetch", "search"], mode="superset")).passed
    s = score_of(tool_trajectory(["search", "nope"], mode="superset"))
    assert not s.passed
    assert "missing calls: ['nope']" in s.reason
    assert s.value == 0.5
    assert "unexpected" not in s.reason


def test_duplicate_calls_are_counted_individually() -> None:
    two = [("search", {"query": "a"}), ("search", {"query": "b"})]
    assert score_of(tool_trajectory(["search", "search"]), two).passed
    s = score_of(tool_trajectory(["search"], mode="superset"), two)
    assert s.passed
    s = score_of(tool_trajectory(["search"], mode="unordered"), two)
    assert not s.passed and s.value == 0.5


def test_empty_expected_means_no_calls() -> None:
    assert score_of(tool_trajectory([]), []).passed
    for mode in ("strict", "unordered", "subset"):
        s = score_of(tool_trajectory([], mode=mode), CALLS)  # type: ignore[arg-type]
        assert not s.passed and s.value == 0.0
    assert score_of(tool_trajectory([], mode="superset"), CALLS).passed


def test_bad_mode_and_bad_arguments_raise_value_error() -> None:
    with pytest.raises(ValueError, match="mode must be one of"):
        tool_trajectory(["a"], mode="loose")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="check_arguments"):
        tool_trajectory(["a"], check_arguments="yes")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="mode must be one of"):
        agent_path(["a"], mode="nope")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="name or a"):
        tool_trajectory([42])
    with pytest.raises(ValueError, match="name or a"):
        tool_trajectory([("search", "cats")])
    with pytest.raises(ValueError, match="list of agent names"):
        agent_path("Triage")  # type: ignore[arg-type]


def test_expected_taken_from_case_when_not_given() -> None:
    assert score_of(tool_trajectory(), expected=["search", "fetch", "summarize"]).passed
    s = score_of(tool_trajectory(), expected="search")
    assert not s.passed and "no usable expected trajectory" in s.reason
    s = score_of(tool_trajectory(), expected=None)
    assert not s.passed and "no usable expected trajectory" in s.reason


def test_custom_name() -> None:
    assert score_of(tool_trajectory(["search"], mode="subset", name="mine")).name == "mine"


# --------------------------------------------------------------- arguments


def test_check_arguments_false_ignores_arguments() -> None:
    s = score_of(tool_trajectory([("search", {"query": "dogs"}), "fetch", "summarize"]))
    assert s.passed


def test_check_arguments_true_requires_equal_arguments() -> None:
    ok = [("search", {"query": "cats"}), ("fetch", {"url": "u1"}), "summarize"]
    assert score_of(tool_trajectory(ok, check_arguments=True)).passed
    bad = [("search", {"query": "dogs"}), "fetch", "summarize"]
    s = score_of(tool_trajectory(bad, check_arguments=True))
    assert not s.passed
    assert "arguments differ for search" in s.reason
    assert 'search({"query": "dogs"})' in s.reason
    assert s.value == pytest.approx(2 / 3)
    # an extra key on the actual call is a mismatch for True ...
    s = score_of(tool_trajectory([("search", {})], mode="superset", check_arguments=True))
    assert not s.passed


def test_check_arguments_subset_accepts_extra_actual_keys() -> None:
    calls = [("search", {"query": "cats", "limit": 5})]
    assert not score_of(
        tool_trajectory([("search", {"query": "cats"})], check_arguments=True), calls
    ).passed
    assert score_of(
        tool_trajectory([("search", {"query": "cats"})], check_arguments="subset"), calls
    ).passed
    s = score_of(tool_trajectory([("search", {"query": "dogs"})], check_arguments="subset"), calls)
    assert not s.passed
    s = score_of(tool_trajectory([("search", {"missing": 1})], check_arguments="subset"), calls)
    assert not s.passed


def test_tool_call_objects_and_name_only_items_in_expected() -> None:
    expected = [ToolCall(id="x", name="search", arguments={"query": "cats"}), "fetch"]
    s = score_of(tool_trajectory(expected, mode="superset", check_arguments=True))
    assert s.passed


def test_order_matters_with_arguments_in_strict_mode() -> None:
    calls = [("search", {"query": "a"}), ("search", {"query": "b"})]
    expected = [("search", {"query": "b"}), ("search", {"query": "a"})]
    s = score_of(tool_trajectory(expected, check_arguments=True), calls)
    assert not s.passed and "wrong order" in s.reason and s.value == 0.5
    assert score_of(tool_trajectory(expected, mode="unordered", check_arguments=True), calls).passed


# --------------------------------------------------------------- agent path


def handoff_agents() -> Agent:
    billing = Agent(
        name="Billing",
        instructions="b",
        model=FakeModel(
            responses=[[ToolCall(id="t1", name="search", arguments={"query": "A1"})], "shipped"]
        ),
        tools=[search],
    )
    return Agent(
        name="Triage",
        instructions="t",
        model=FakeModel(
            responses=[[ToolCall(id="h1", name="transfer_to_billing", arguments={"reason": "x"})]]
        ),
        handoffs=[billing],
    )


def test_agent_path_with_handoff_modes() -> None:
    cases = [Case("h", "where is A1?")]
    report = evaluate(
        handoff_agents(),
        cases,
        [
            agent_path(["Triage", "Billing"]),
            agent_path(["Billing", "Triage"], name="reversed"),
            agent_path(["Billing", "Triage"], mode="unordered", name="unordered"),
            agent_path(["Triage"], mode="superset", name="superset"),
            agent_path(["Triage"], mode="subset", name="subset"),
        ],
    )
    by_name = {s.name: s for s in report.cases[0].scores}
    assert by_name["agent_path"].passed and by_name["agent_path"].value == 1.0
    assert not by_name["reversed"].passed and "wrong order" in by_name["reversed"].reason
    assert by_name["unordered"].passed
    assert by_name["superset"].passed
    assert (
        not by_name["subset"].passed
        and "unexpected agents: ['Billing']" in by_name["subset"].reason
    )


def test_agent_path_uses_case_expected_and_handoff_tool_is_a_call() -> None:
    good = evaluate(
        handoff_agents(), [Case("h", "x", expected=["Triage", "Billing"])], [agent_path()]
    )
    assert good.cases[0].passed
    bad = evaluate(handoff_agents(), [Case("h", "x", expected="Triage")], [agent_path()])
    assert not bad.cases[0].passed
    assert bad.cases[0].scores[0].reason == "case has no expected agent path"
    calls = evaluate(
        handoff_agents(),
        [Case("h", "x")],
        [tool_trajectory(["transfer_to_billing", "search"], check_arguments="subset")],
    )
    assert calls.cases[0].passed


# --------------------------------------------------------------- teams


def test_team_runs_score_over_every_member() -> None:
    first = calls_agent([("search", {"query": "cats"})], name="first")
    second = calls_agent([("fetch", {"url": "u"})], name="second")
    team = SequentialAgent("pipe", [first, second])
    report = evaluate(
        team,
        [Case("t", "go")],
        [
            tool_trajectory(["search", "fetch"]),
            tool_trajectory(
                [("fetch", {"url": "u"}), ("search", {"query": "cats"})],
                mode="unordered",
                check_arguments=True,
                name="unordered",
            ),
            tool_trajectory(["fetch", "search"], name="wrong"),
            agent_path(["first", "second"]),
        ],
    )
    scores = {s.name: s for s in report.cases[0].scores}
    assert scores["tool_trajectory"].passed and scores["unordered"].passed
    assert not scores["wrong"].passed
    assert scores["agent_path"].passed


def test_parallel_team_unordered() -> None:
    a = calls_agent([("search", {"query": "a"})], name="a")
    b = calls_agent([("fetch", {"url": "b"})], name="b")
    report = evaluate(
        ParallelAgent("par", [a, b]),
        [Case("p", "go")],
        [
            tool_trajectory(["fetch", "search"], mode="unordered"),
            agent_path(["b", "a"], mode="unordered"),
        ],
    )
    assert report.cases[0].passed


# --------------------------------------------------------------- multi-turn


def chat_agent() -> Agent:
    """Turn 1 searches; turn 2 fetches; turn 3 answers without tools."""

    def handler(messages: list[Any], tools: Any) -> Any:
        users = [m for m in messages if m.role == "user"]
        last_is_tool = messages[-1].role == "tool"
        n = len(users)
        if n == 1 and not last_is_tool:
            return [ToolCall(id="s1", name="search", arguments={"query": "cats"})]
        if n == 2 and not last_is_tool:
            return [ToolCall(id="f1", name="fetch", arguments={"url": "u1"})]
        return f"answer {n} (saw {users[0].content})"

    return Agent(
        name="chat", instructions="x", model=FakeModel(handler=handler), tools=[search, fetch]
    )


def test_multi_turn_case_shape_and_validation() -> None:
    c = Case("m", "first", turns=["second", "third"])
    assert c.turns == ["second", "third"]
    assert Case("s", "only").turns == []
    with pytest.raises(SDKValidationError, match="turns"):
        Case("bad", "x", turns="second")  # type: ignore[arg-type]
    with pytest.raises(SDKValidationError, match="Agent target"):
        evaluate(SequentialAgent("t", [chat_agent()]), [c], [exact_match()])


def test_multi_turn_runs_one_conversation_and_scores_whole_run() -> None:
    case = Case("m", "first", turns=["second", "third"])
    report = evaluate(
        chat_agent(),
        [case],
        [
            tool_trajectory(["search", "fetch"]),
            contains("answer 3 (saw first)"),
            tool_called("fetch", times=1),
        ],
    )
    r = report.cases[0]
    assert r.status == "passed", [s.reason for s in r.scores]
    assert r.output_text == "answer 3 (saw first)"


def test_for_turn_and_every_turn_see_one_turn_each() -> None:
    case = Case("m", "first", turns=["second", "third"])
    report = evaluate(
        chat_agent(),
        [case],
        [
            for_turn(tool_trajectory(["search"]), 0),
            for_turn(tool_trajectory(["fetch"]), 1),
            for_turn(tool_trajectory([]), -1),
            for_turn(contains("answer 2"), 1, name="turn2-text"),
            for_turn(tool_trajectory(["fetch"]), 0, name="wrong-turn"),
            for_turn(tool_trajectory([]), 7, name="out-of-range"),
            every_turn(tool_trajectory(["search", "fetch"], mode="subset")),
            every_turn(tool_trajectory([], mode="strict"), name="all-empty"),
        ],
    )
    s = {x.name: x for x in report.cases[0].scores}
    assert s["tool_trajectory@turn0"].passed
    assert s["tool_trajectory@turn1"].passed
    assert s["tool_trajectory@turn-1"].passed
    assert s["turn2-text"].passed
    assert not s["wrong-turn"].passed
    assert not s["out-of-range"].passed and "out of range (3 turns)" in s["out-of-range"].reason
    assert s["tool_trajectory@every_turn"].passed
    assert s["tool_trajectory@every_turn"].value == 1.0
    assert not s["all-empty"].passed
    assert s["all-empty"].value == pytest.approx(1 / 3)
    assert "turn 0:" in s["all-empty"].reason and "turn 1:" in s["all-empty"].reason


def test_turn_views_expose_per_turn_output() -> None:
    from nexura_agents import Runner

    agent = chat_agent()
    seen: dict[str, Any] = {}

    def probe(case: Case, result: EvalOutput) -> bool:
        seen["finals"] = [t.final_output for t in result.turns]
        seen["details"] = [t.tool_call_details for t in result.turns]
        seen["all"] = result.tool_call_details
        seen["usage_total"] = result.usage.total_tokens
        return True

    report = evaluate(
        agent, [Case("m", "first", turns=["second", "third"])], [probe], runner=Runner()
    )
    assert report.cases[0].passed
    assert seen["finals"] == [
        "answer 1 (saw first)",
        "answer 2 (saw first)",
        "answer 3 (saw first)",
    ]
    assert seen["details"] == [[("search", {"query": "cats"})], [("fetch", {"url": "u1"})], []]
    assert seen["all"] == [("search", {"query": "cats"}), ("fetch", {"url": "u1"})]


def test_single_turn_case_for_turn_zero_and_every_turn() -> None:
    report = evaluate(
        calls_agent(CALLS),
        [Case("s", "go")],
        [for_turn(tool_trajectory(["search", "fetch", "summarize"]), 0), every_turn(exact_match())],
    )
    scores = report.cases[0].scores
    assert scores[0].passed
    assert not scores[1].passed  # exact_match without expected


def test_for_turn_rejects_non_integer_turn() -> None:
    with pytest.raises(ValueError, match="integer"):
        for_turn(exact_match(), "0")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="integer"):
        for_turn(exact_match(), True)


def test_multi_turn_stops_on_failed_turn_as_error() -> None:
    def boom(messages: list[Any], tools: Any) -> Any:
        if len([m for m in messages if m.role == "user"]) == 2:
            raise RuntimeError("model down")
        return "ok"

    agent = Agent(name="b", instructions="x", model=FakeModel(handler=boom))
    report = evaluate(agent, [Case("m", "one", turns=["two", "three"])], [exact_match()])
    assert report.cases[0].status == "error"


# Review findings (F55 adversarial review), each reproduced before the fix.


def _counter_agent(value: int) -> Agent:
    @tool
    def count(n: int) -> str:
        """Count."""
        return "ok"

    calls = [[ToolCall(id="c1", name="count", arguments={"n": value})], "done"]
    return Agent(name="bot", instructions="x", model=FakeModel(responses=calls), tools=[count])


@pytest.mark.parametrize("check", [True, "subset"])
def test_argument_match_is_type_strict(check: Any) -> None:
    report = evaluate(
        _counter_agent(1),
        [Case("c", "go")],
        [tool_trajectory([("count", {"n": True})], check_arguments=check)],
    )
    assert report.cases[0].scores[0].passed is False  # True is not the integer 1
    report = evaluate(
        _counter_agent(1),
        [Case("c", "go")],
        [tool_trajectory([("count", {"n": 1})], check_arguments=check)],
    )
    assert report.cases[0].scores[0].passed is True


def test_reasons_do_not_print_unchecked_or_secret_arguments() -> None:
    @tool
    def search(query: str) -> str:
        """Search."""
        return "r"

    secret = "sk-SECRET-1234567890abcdef"
    calls = [[ToolCall(id="c1", name="search", arguments={"query": secret})], "done"]
    agent = Agent(name="bot", instructions="x", model=FakeModel(responses=calls), tools=[search])
    unchecked = evaluate(agent, [Case("c", "go")], [tool_trajectory(["fetch"])])
    assert secret not in unchecked.cases[0].scores[0].reason
    agent = Agent(
        name="bot",
        instructions="x",
        model=FakeModel(responses=[list(calls[0]), "done"]),
        tools=[search],
    )
    checked = evaluate(
        agent,
        [Case("c", "go")],
        [tool_trajectory([("search", {"query": "x"})], check_arguments=True)],
    )
    reason = checked.cases[0].scores[0].reason
    assert "search(" in reason and secret not in reason  # shown, but redacted


@pytest.mark.parametrize("bad", [[123], [None], ["ok", 5]])
def test_case_turns_items_are_validated(bad: Any) -> None:
    with pytest.raises(SDKValidationError, match="Case turns"):
        Case("c", "go", turns=bad)
