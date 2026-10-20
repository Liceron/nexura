"""Mermaid diagrams of agents, teams and workflows."""

from __future__ import annotations

import re
from typing import Any

from nexura_agents import Agent, FakeModel, LoopAgent, ParallelAgent, SequentialAgent, tool
from nexura_agents.diagrams import mermaid_label


def make(name: str, **kw: Any) -> Agent:
    return Agent(name=name, instructions="i", model=FakeModel(responses=["x"]), **kw)


@tool
def lookup(q: str) -> str:
    """Lookup."""
    return q


@tool(requires_approval=True)
def refund(amount: int) -> str:
    """Refund."""
    return "ok"


@tool(enabled=lambda ctx: True)
def admin(x: str) -> str:
    """Admin."""
    return x


@tool(enabled=False)
def off(x: str) -> str:
    """Off."""
    return x


def node_ids(diagram: str) -> set[str]:
    return set(re.findall(r"^\s*(\w+)[\[(]", diagram, re.M))


def test_agent_with_tools_and_markers() -> None:
    diagram = make("Support", tools=[lookup, refund, admin, off]).to_mermaid()
    assert diagram.startswith("flowchart LR\n")
    assert '["Support"]' in diagram
    assert '(["lookup"])' in diagram
    assert '(["refund (approval)"])' in diagram
    assert '(["admin (conditional)"])' in diagram
    assert '(["off (disabled)"])' in diagram
    assert diagram.count(" --> tool") == 4


def test_handoffs_including_a_cycle_are_drawn_once() -> None:
    billing = make("Billing")
    triage = make("Triage", handoffs=[billing])
    billing.add_handoffs(triage)  # hands back: a cycle
    diagram = triage.to_mermaid()
    assert diagram.count('["Triage"]') == 1 and diagram.count('["Billing"]') == 1
    assert diagram.count("-.->|handoff|") == 2


def test_agents_and_teams_used_as_tools() -> None:
    researcher = make("Researcher", tools=[lookup])
    reviewers = ParallelAgent("Reviewers", [make("A"), make("B")])
    boss = make("Boss", tools=[researcher.as_tool(), reviewers.as_tool(name="review")])
    diagram = boss.to_mermaid()
    assert '-->|"as tool: researcher"|' in diagram
    assert '-->|"as tool: review"|' in diagram
    assert '["Researcher"]' in diagram and '(["lookup"])' in diagram
    assert "subgraph team" in diagram and "Reviewers (ParallelAgent)" in diagram


def test_sequential_and_loop_teams() -> None:
    a, b, c = make("A"), make("B"), make("C")
    pipeline = SequentialAgent("Pipeline", [a, b, c])
    seq = pipeline.to_mermaid()
    assert "Pipeline (SequentialAgent)" in seq
    assert seq.count(" --> agent") == 2  # A --> B --> C
    loop = LoopAgent("Refine", make("Writer"), max_iterations=3).to_mermaid()
    assert '"repeat up to 3x"' in loop


def test_include_tools_false_hides_plain_tools_and_agent_tools() -> None:
    diagram = make("A", tools=[lookup, make("Sub").as_tool()]).to_mermaid(include_tools=False)
    assert "lookup" not in diagram and "Sub" not in diagram


def test_labels_are_escaped() -> None:
    assert mermaid_label('say "hi" <b> & x\ny') == '"say #quot;hi#quot; #lt;b#gt; #amp; x y"'
    diagram = make('Quote "Bot"').to_mermaid()
    assert '["Quote #quot;Bot#quot;"]' in diagram


def test_node_ids_are_unique() -> None:
    shared = make("Shared")
    root = make("Root", handoffs=[shared], tools=[shared.as_tool(name="ask_shared"), lookup])
    diagram = root.to_mermaid()
    declared = re.findall(r"^\s*(agent\d+|tool\d+)[\[(]", diagram, re.M)
    assert len(declared) == len(set(declared))


# ---------------------------------------------------------------- workflows


def test_workflow_to_mermaid() -> None:
    from nexura_runtime.workflow import WorkflowNode, WorkflowSpec

    inner = WorkflowSpec(
        name="check",
        entry_node="score",
        nodes={"score": WorkflowNode(id="score", handler="h")},
        terminal_nodes=["score"],
    )
    spec = WorkflowSpec(
        name="orders",
        entry_node="classify",
        nodes={
            "classify": WorkflowNode(
                id="classify",
                handler="h",
                transitions={"refund": "approve", "info": "answer"},
            ),
            "approve": WorkflowNode(
                id="approve", handler="h", requires_approval=True, default_transition="done"
            ),
            "answer": WorkflowNode(id="answer", handler="h", parallel=["a", "b"]),
            "review": WorkflowNode(id="review", handler="", subgraph=inner, map_over="items"),
        },
        terminal_nodes=["done"],
    )
    diagram = spec.to_mermaid()
    assert diagram.startswith("flowchart TD\n")
    assert "w_start((start)) --> w_0" in diagram
    assert '-->|"refund"|' in diagram and '-->|"info"|' in diagram
    assert '"approve (approval)"' in diagram
    assert "-.->|default|" in diagram and '(("done"))' in diagram
    assert '"answer - parallel: a, b"' in diagram
    assert 'subgraph w_3["review - for each items"]' in diagram
    assert '["score"]' in diagram


def test_backtick_cannot_open_a_markdown_label() -> None:
    assert mermaid_label("`x`") == '"#96;x#96;"'
    diagram = make("`x").to_mermaid()
    assert '["`' not in diagram and '["#96;x"]' in diagram


def test_line_breaks_and_control_characters_cannot_inject_directives() -> None:
    evil = 'x"]\rclick agent1 href "javascript:alert(1)"\rz["'
    for separator in ("\r", "\n", " ", " ", "\x0b", "\x0c", "\x85", "\x00"):
        label = mermaid_label(evil.replace("\r", separator))
        assert separator not in label
    diagram = make(evil).to_mermaid()
    lines = diagram.splitlines()
    assert not any(line.lstrip().startswith("click") for line in lines)
