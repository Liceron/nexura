# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Local FakeModel child for todo SIGKILL/recovery rows; uses existing marker helpers."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

import durability_harness as h
from nexura_agents import (
    Agent,
    AgentHooks,
    FakeModel,
    Harness,
    Message,
    RunContext,
    Runner,
    SQLiteCheckpointStore,
    ToolCall,
    Usage,
    compact_context,
)
from nexura_agents.errors import RunRecoveryError

PLAN = [{"id": "task", "text": "Persist this plan", "status": "in_progress"}]


class PointStore(SQLiteCheckpointStore):
    """Same after-save kill point pattern as durability_child._point_checkpoint_store."""

    async def save(self, run_id: str, checkpoint_json: str, owner: str | None = None) -> None:
        await super().save(run_id, checkpoint_json, owner=owner)
        envelope = json.loads(checkpoint_json)
        if envelope["phase"] == "tools_pending":
            pending = json.loads(envelope["interruption"]).get("pending") or []
            if not pending:
                h.point("after_tool_result", run_id=run_id)


async def main(config: dict[str, Any]) -> None:
    """Run or recover a task list with the shared SQLite journal/checkpoint machinery."""
    h.set_child_context(h.ChildContext(config))
    active_run: dict[str, str] = {}

    class Points(AgentHooks):
        async def on_run_start(self, ctx: RunContext[Any]) -> None:
            active_run["id"] = ctx.run_id

        async def after_tool(self, ctx: RunContext[Any], call: ToolCall, result: str) -> None:
            if call.name == "write_todos":
                h.point("after_todo_mutation", run_id=ctx.run_id)

    def respond(messages: list[Message], tools: Any) -> Message | str:
        if any(m.role == "tool" and m.name == "write_todos" for m in messages):
            return "done"
        return Message("assistant", tool_calls=[ToolCall("todo", "write_todos", {"items": PLAN})])

    agent = Agent(
        "Todo", "Local fake model only.", FakeModel(handler=respond), harness=Harness(todo=True)
    )
    old_history: list[Message] | None = None
    if config.get("summary"):

        def summarize(messages: Any, tools: Any) -> str:
            with (Path(config["workdir"]) / "summary-calls.jsonl").open("a") as log:
                log.write(json.dumps({"text": "SAVED SUMMARY"}) + "\n")
            return "SAVED SUMMARY"

        def main_model(messages: list[Message], tools: Any) -> str:
            h.point("in_main_model", run_id=active_run.get("id", config.get("run_id")))
            with (Path(config["workdir"]) / "main-requests.jsonl").open("a") as log:
                log.write(json.dumps([m.to_dict() for m in messages]) + "\n")
            return "done"

        agent = Agent(
            "Summary",
            "Local fake model only.",
            FakeModel(handler=main_model, default_usage=Usage(3, 1, 4)),
            harness=Harness(
                compaction=compact_context(
                    500, model=FakeModel(handler=summarize, default_usage=Usage(4, 2, 6))
                )
            ),
        )
        old_history = [
            m
            for i in range(5)
            for m in (Message("user", f"old {i} " + "x" * 1500), Message("assistant", "ok"))
        ]
    runner = Runner(checkpoint_store=PointStore(config["db"]), lease_ttl=0.6, hooks=Points())
    if config["mode"] == "recover":
        deadline = time.monotonic() + 5
        while True:
            try:
                result = await runner.recover_async(agent, config["run_id"])
                break
            except RunRecoveryError as exc:
                if "already being recovered" not in str(exc) or time.monotonic() > deadline:
                    raise
                await asyncio.sleep(0.1)
    else:
        result = await runner.run_async(agent, "Plan", history=old_history)
    h.child().write_result(
        {
            "status": result.status,
            "state": result.state,
            "output": result.output,
            "total_tokens": result.usage.total_tokens,
            "events": [{"type": e.type.value, "data": e.data} for e in result.events],
        }
    )


if __name__ == "__main__":
    with open(sys.argv[1]) as config_file:
        asyncio.run(main(json.load(config_file)))
