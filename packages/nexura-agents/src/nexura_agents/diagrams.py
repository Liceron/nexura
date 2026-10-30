# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Mermaid diagrams of agents, teams and their tools.

``agent.to_mermaid()`` (or ``team.to_mermaid()``) returns a Mermaid ``flowchart`` that
GitHub, GitLab, Notion and most Markdown viewers render. It shows:

- agents (rectangles) and the tools they can call (rounded);
- handoffs (dotted arrows labelled ``handoff``);
- agents or teams used as tools (``as tool``);
- teams as boxes: sequential members in order, parallel members side by side, a loop
  with its iteration cap.

Tools that need approval are marked ``(approval)``; tools with ``enabled=False`` are
``(disabled)`` and tools with an ``enabled`` rule are ``(conditional)``. The diagram is
static: it describes what the code allows, not one particular run.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexura_agents.agent import Agent
    from nexura_agents.teams import Team
    from nexura_agents.tools import Tool


def mermaid_label(text: str) -> str:
    """Quote text for a Mermaid node or edge label (quotes, brackets, backticks escaped)."""
    escaped = (
        text.replace("&", "#amp;")
        .replace('"', "#quot;")
        .replace("<", "#lt;")
        .replace(">", "#gt;")
        .replace("`", "#96;")  # a leading backtick would open a markdown string
    )
    # Any line break or control character would end the label and start a new statement
    # (for example a "click ... href" directive), so they all become spaces.
    escaped = "".join(" " if (ch.isspace() and ch != " ") or ord(ch) < 32 else ch for ch in escaped)
    return f'"{escaped}"'


class _Builder:
    def __init__(self, include_tools: bool) -> None:
        self.include_tools = include_tools
        self.lines: list[str] = []
        self.ids: dict[int, str] = {}  # id(object) -> mermaid node id
        self.count = 0

    def new_id(self, prefix: str) -> str:
        self.count += 1
        return f"{prefix}{self.count}"

    def add(self, line: str, depth: int = 1) -> None:
        self.lines.append("    " * depth + line)

    def node(self, obj: Any, depth: int) -> str:
        """Draw an agent or team once and return its node id (cycles stop here)."""
        from nexura_agents.teams import Team

        key = id(obj)
        if key in self.ids:
            return self.ids[key]
        if isinstance(obj, Team):
            return self.team(obj, depth)
        return self.agent(obj, depth)

    def agent(self, agent: Agent, depth: int) -> str:
        node_id = self.new_id("agent")
        self.ids[id(agent)] = node_id
        self.add(f"{node_id}[{mermaid_label(agent.name)}]", depth)
        self.add(f"class {node_id} agent", depth)
        if self.include_tools:
            for tool in agent.tools:
                self.tool_edge(node_id, tool, depth)
        for handoff in agent.handoffs:
            target = self.node(handoff.agent, depth)
            rule = getattr(handoff, "enabled", True)
            mark = " (disabled)" if rule is False else " (conditional)" if callable(rule) else ""
            label = "handoff" if not mark else mermaid_label("handoff" + mark)
            self.add(f"{node_id} -.->|{label}| {target}", depth)
        return node_id

    def tool_edge(self, owner: str, tool: Tool, depth: int) -> None:
        inner = getattr(tool, "nexura_agent", None)
        if inner is not None:  # an agent or team exposed with as_tool()
            target = self.node(inner, depth)
            self.add(f"{owner} -->|{mermaid_label('as tool: ' + tool.name)}| {target}", depth)
            return
        marks = []
        if tool.requires_approval:
            marks.append("approval")
        if tool.enabled is False:
            marks.append("disabled")
        elif callable(tool.enabled):
            marks.append("conditional")
        label = tool.name + (f" ({', '.join(marks)})" if marks else "")
        tool_id = self.new_id("tool")
        self.add(f"{tool_id}([{mermaid_label(label)}])", depth)
        self.add(f"class {tool_id} tool", depth)
        self.add(f"{owner} --> {tool_id}", depth)

    def team(self, team: Team, depth: int) -> str:
        from nexura_agents.teams import LoopAgent, ParallelAgent

        node_id = self.new_id("team")
        self.ids[id(team)] = node_id
        kind = type(team).__name__
        self.add(f"subgraph {node_id}[{mermaid_label(f'{team.name} ({kind})')}]", depth)
        self.add("direction LR", depth + 1)
        members = [self.node(member, depth + 1) for member in team.members]
        if isinstance(team, ParallelAgent):
            pass  # members side by side, no order between them
        elif isinstance(team, LoopAgent):
            first = members[0]
            label = mermaid_label(f"repeat up to {team.max_iterations}x")
            self.add(f"{first} -->|{label}| {first}", depth + 1)
        else:
            for before, after in zip(members, members[1:], strict=False):
                self.add(f"{before} --> {after}", depth + 1)
        self.add("end", depth)
        return node_id


def agent_mermaid(root: Agent | Team, *, include_tools: bool = True) -> str:
    """A Mermaid ``flowchart`` of ``root``, everything it hands off to, and its tools."""
    builder = _Builder(include_tools)
    builder.lines.append("flowchart LR")
    builder.node(root, 1)
    builder.add("classDef agent fill:#eef4ff,stroke:#3b6fd8")
    builder.add("classDef tool fill:#f6f6f6,stroke:#999")
    return "\n".join(builder.lines) + "\n"
