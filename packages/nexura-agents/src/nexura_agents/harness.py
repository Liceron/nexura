# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Optional agent harness, assembled from normal tools and history processors."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from nexura_agents.contracts.canonical import canonical_digest
from nexura_agents.errors import SDKValidationError
from nexura_agents.filesystem import ScopedFilesystem
from nexura_agents.history import HistoryProcessor
from nexura_agents.run_context import RunContext
from nexura_agents.skills import SkillLibrary
from nexura_agents.tools import Tool, tool

TODO_STATE_KEY = "nexura.todos"
_TODO_REVISION_KEY = "nexura.todos.revision"


class TodoItem(BaseModel):
    """One task, identified across plan replacements by its unique id."""

    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(min_length=1, max_length=64)
    text: str = Field(min_length=1, max_length=1000)
    status: Literal["pending", "in_progress", "completed"] = "pending"


def todo_tools(*, requires_approval: bool = False) -> list[Tool]:
    """Replace/read the run's plan. Replacement is idempotent by construction.

    State is checkpointed with the tool result; the normal effect journal admits replay
    after a crash before that checkpoint. Re-emitted progress events may repeat on replay.
    Notifications contain only a monotonic version, total count and fixed status counts;
    replay of a recorded call retains the current version and cannot revert newer state.
    Configure sequential tool calls when ordering multiple plan replacements matters.
    """

    @tool(effect="idempotent", requires_approval=requires_approval)
    async def write_todos(ctx: RunContext[Any], items: list[TodoItem]) -> list[dict[str, Any]]:
        """Replace the task list (at most 100 unique items); use [] to clear it."""
        if len(items) > 100 or len({item.id for item in items}) != len(items):
            raise SDKValidationError("Todos require at most 100 items with unique ids.")
        plan = [item.model_dump(mode="json") for item in items]
        revision = ctx.state.get(_TODO_REVISION_KEY, {})
        if not isinstance(revision, dict):
            raise SDKValidationError("Invalid saved todo revision.")
        version = revision.get("version", 0)
        if isinstance(version, bool) or not isinstance(version, int) or version < 0:
            raise SDKValidationError("Saved todo version must be a non-negative integer.")
        applied = revision.get("applied", {}) if revision.get("run_id") == ctx.run_id else {}
        if not isinstance(applied, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in applied.items()
        ):
            raise SDKValidationError("Invalid saved todo call receipts.")
        applied = dict(applied)
        identity = ctx.idempotency_key
        if identity is None and ctx.tool_call_id is not None:
            identity = f"{ctx.run_id}:{ctx.turn}:{ctx.tool_call_id}"
        digest = canonical_digest(plan)
        if identity is not None and identity in applied:
            if applied[identity] != digest:
                raise SDKValidationError(
                    "A todo call identity cannot be reused with different arguments."
                )
        else:
            version += 1
            if identity is not None:
                applied[identity] = digest
            ctx.state[TODO_STATE_KEY] = plan
            ctx.state[_TODO_REVISION_KEY] = {
                "version": version,
                "run_id": ctx.run_id,
                "applied": applied,
            }
        current = [TodoItem.model_validate(item) for item in ctx.state[TODO_STATE_KEY]]
        # Never publish strings supplied by the model, even opaque call/item ids.
        # This built-in omits ctx.emit's agent/call correlation fields for the same reason.
        ctx._events.put(
            {
                "name": "nexura.todos_updated",
                "data": {
                    "version": version,
                    "count": len(current),
                    "status_counts": {
                        "pending": sum(item.status == "pending" for item in current),
                        "in_progress": sum(item.status == "in_progress" for item in current),
                        "completed": sum(item.status == "completed" for item in current),
                    },
                },
            }
        )
        return copy.deepcopy(plan)

    @tool(effect="pure")
    async def read_todos(ctx: RunContext[Any]) -> list[dict[str, Any]]:
        """Read the current run's task list."""
        plan: list[dict[str, Any]] = copy.deepcopy(ctx.state.get(TODO_STATE_KEY, []))
        return plan

    write_todos.origin = read_todos.origin = "harness:todo"
    write_todos.approval_scope = "run-state:" + TODO_STATE_KEY
    write_todos.approval_scope_per_run = True
    return [write_todos, read_todos]


@dataclass(frozen=True)
class Harness:
    """``Agent(harness=Harness(...))``; every capability is separately opt-in.

    Share the same filesystem explicitly with child harnesses, then delegate with
    ``child.as_tool()``. Existing G2 hooks and budget inheritance remain authoritative.
    No shell is installed. Skill ``allowed-tools`` metadata grants no permissions.
    """

    skills: SkillLibrary | None = None
    compaction: HistoryProcessor | None = None
    todo: bool = False
    filesystem: ScopedFilesystem | None = None
    require_write_approval: bool = False

    def __post_init__(self) -> None:
        if self.skills is not None and not isinstance(self.skills, SkillLibrary):
            raise SDKValidationError("Harness skills must be a SkillLibrary.")
        if self.compaction is not None and not callable(self.compaction):
            raise SDKValidationError("Harness compaction must be a history processor.")
        if self.filesystem is not None and not isinstance(self.filesystem, ScopedFilesystem):
            raise SDKValidationError("Harness filesystem must be a ScopedFilesystem.")
        if not isinstance(self.todo, bool) or not isinstance(self.require_write_approval, bool):
            raise SDKValidationError("Harness flags must be booleans.")

    def build_tools(self, available_tools: list[str]) -> list[Tool]:
        """Build normal SDK tools for the enabled capabilities, with effect metadata."""
        tools: list[Tool] = []
        if self.todo:
            tools.extend(todo_tools(requires_approval=self.require_write_approval))
        if self.filesystem is not None:
            tools.extend(
                self.filesystem.build_tools(require_write_approval=self.require_write_approval)
            )
        if self.skills is not None and self.skills.names():
            tools.extend(
                [
                    self.skills.loader_tool(
                        progressive=True, available_tools=available_tools + [t.name for t in tools]
                    ),
                    self.skills.resource_tool(),
                ]
            )
        return tools

    def catalog(self) -> str:
        """Only skill names/descriptions enter the system prompt."""
        return self.skills.catalog() if self.skills is not None and self.skills.names() else ""
