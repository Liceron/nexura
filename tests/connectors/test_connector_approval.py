"""B22 regression: connector writes cannot be self-approved through inputs.

A flow author (spec / state mapping) or a model (tool-call arguments) controls the module
inputs, so ``approved=true`` there must grant nothing. Only the engine after a human
approval, the agent runner after a tool approval, or an operator-set context flag may let a
write run. All HTTP goes through ``httpx.MockTransport``; no network.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
from connector_helpers import FakeResolver, Recorder, json_response, make_context
from nexura_agents import Agent, FakeModel, Runner, ToolCall
from nexura_agents.module_tools import module_tool
from nexura_connectors import SlackConnector

from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.workflow import (
    ModuleRegistry,
    SQLiteWorkflowStore,
    WorkflowEngine,
    WorkflowNode,
    WorkflowSpec,
)

TOKEN = "tok_SuperSecretValue_12345678"
POST = {"channel": "C1", "text": "hello"}


def slack_recorder() -> Recorder:
    return Recorder(
        lambda r: json_response({"ok": True, "channel": "C1", "ts": "1.2", "channels": []})
    )


def engine_for(rec: Recorder, tmp_path: Path) -> WorkflowEngine:
    registry = ModuleRegistry()
    registry.register("slack", SlackConnector(transport=rec.transport))
    spec = WorkflowSpec(
        name="B22",
        entry_node="send",
        nodes={
            "send": WorkflowNode(id="send", handler="slack", config={"credential_ref": "cred"}),
        },
    )
    return WorkflowEngine(
        spec,
        registry,
        store=SQLiteWorkflowStore(tmp_path / "b22.db"),
        credential_resolver=FakeResolver({"cred": TOKEN}),
    )


def posts(rec: Recorder) -> list[httpx.Request]:
    return [r for r in rec.requests if r.method == "POST"]


async def test_a_self_approved_write_pauses_and_sends_nothing(tmp_path: Path) -> None:
    rec = slack_recorder()
    res = await engine_for(rec, tmp_path).run(
        {"operation": "post_message", "params": POST, "approved": True}
    )
    assert res.status == "paused" and res.pending_approval is not None
    assert posts(rec) == [], "the approved input must not let a write run"


async def test_b_engine_approval_sends_exactly_one_post(tmp_path: Path) -> None:
    rec = slack_recorder()
    engine = engine_for(rec, tmp_path)
    paused = await engine.run({"operation": "post_message", "params": POST, "approved": True})
    assert paused.pending_approval is not None
    done = await engine.resume(
        paused.workflow_run_id, paused.pending_approval.approval_id, decision="approve"
    )
    assert done.status == "completed", done
    assert len(posts(rec)) == 1


async def test_a2_rejection_sends_nothing(tmp_path: Path) -> None:
    rec = slack_recorder()
    engine = engine_for(rec, tmp_path)
    paused = await engine.run({"operation": "post_message", "params": POST})
    assert paused.pending_approval is not None
    await engine.resume(
        paused.workflow_run_id, paused.pending_approval.approval_id, decision="reject"
    )
    assert rec.requests == []


async def test_c_read_operation_runs_without_approval(tmp_path: Path) -> None:
    rec = slack_recorder()
    res = await engine_for(rec, tmp_path).run({"operation": "list_channels", "params": {}})
    assert res.status == "completed", res
    assert len(rec.requests) == 1 and posts(rec) == []
    assert rec.last.url.path.endswith("conversations.list")


async def test_unknown_operation_value_is_gated_conservatively(tmp_path: Path) -> None:
    rec = slack_recorder()
    res = await engine_for(rec, tmp_path).run({"params": POST})  # operation missing from state
    assert res.status == "paused" and rec.requests == []


async def test_direct_use_ignores_the_input_flag_but_honours_the_operator_flag() -> None:
    rec = slack_recorder()
    mod = SlackConnector(transport=rec.transport)
    inputs = {"operation": "post_message", "params": POST, "approved": True}
    cfg = {"credential_ref": "cred"}
    res = await mod.execute(make_context(), inputs, cfg)
    assert res.error_code == "APPROVAL_REQUIRED" and rec.requests == []
    res = await mod.execute(make_context(approval_granted=True), inputs, cfg)
    assert res.status == "success" and len(rec.requests) == 1


async def test_d_agent_tool_call_cannot_self_approve() -> None:
    rec = slack_recorder()
    tool = module_tool(
        SlackConnector(transport=rec.transport), config={"credential_ref": "cred"}, name="slack"
    )
    args: dict[str, Any] = {"operation": "post_message", "params": POST, "approved": True}
    agent = Agent(
        name="Bot",
        instructions="i",
        model=FakeModel(responses=[[ToolCall(id="c1", name="slack", arguments=args)], "done"]),
        tools=[tool],
    )
    runner = Runner()
    paused = await runner.run_async(agent, "post it")
    assert paused.status == "paused"
    assert rec.requests == [], "a model-requested write ran without human approval"
    # Even if a caller invokes the tool body directly, the module refuses (no resolver here).
    ctx = make_context()
    res = await SlackConnector(transport=rec.transport).execute(
        ctx, args, {"credential_ref": "cred"}
    )
    assert res.error_code == "APPROVAL_REQUIRED" and rec.requests == []


async def test_d2_agent_write_runs_only_after_human_approval() -> None:
    rec = slack_recorder()
    tool = module_tool(
        SlackConnector(transport=rec.transport), config={"credential_ref": "cred"}, name="slack"
    )
    args = {"operation": "post_message", "params": POST}
    agent = Agent(
        name="Bot",
        instructions="i",
        model=FakeModel(responses=[[ToolCall(id="c1", name="slack", arguments=args)], "done"]),
        tools=[tool],
    )
    runner = Runner()
    paused = await runner.run_async(agent, "post it")
    assert paused.status == "paused" and paused.interruption is not None
    done = await runner.resume_async(agent, paused.interruption, approve=["c1"])
    # No credential resolver exists in the agent context, so the module reports an error to
    # the model, but it was reached only after approval and never sent without a credential.
    assert done.status == "completed"


async def test_d3_agent_read_needs_no_approval() -> None:
    rec = slack_recorder()
    tool = module_tool(SlackConnector(transport=rec.transport), name="slack")
    assert tool.needs_approval is not None
    ctx: Any = None
    assert not tool.needs_approval(ctx, {"operation": "list_channels", "params": {}})
    assert tool.needs_approval(ctx, {"operation": "post_message", "params": POST})
    assert tool.needs_approval(ctx, {"approved": True, "params": POST})


async def test_e_modules_api_reports_write_gate(tmp_path: Path) -> None:
    app = create_api_app(db_path=tmp_path / "a.db")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        items = (await client.get("/api/v1/modules?limit=500")).json()["items"]
    connectors = [m for m in items if m["module_id"].startswith("nexura.connectors.")]
    assert len(connectors) == 15
    for m in connectors:
        # read-only connectors (imap) have no writes and no gate; every other one is gated
        assert m["requires_approval"] is bool(m["write_operations"]), m["module_id"]
    gated = {m["module_id"] for m in connectors if m["requires_approval"]}
    assert len(gated) == 14 and "nexura.connectors.imap" not in gated
    slack = next(m for m in connectors if m["module_id"] == "nexura.connectors.slack")
    assert slack["write_operations"] == ["post_message"]
