# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""P38 C1: all model calls are explicitly local FakeModel simulations."""

from __future__ import annotations

import json
import os
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from nexura_agents import (
    Agent,
    AgentHooks,
    FakeModel,
    GuardrailResult,
    Harness,
    Message,
    RunContext,
    Runner,
    ScopedFilesystem,
    Skill,
    SkillLibrary,
    ToolCall,
    Usage,
    compact_context,
)
from nexura_agents.errors import SDKValidationError, ToolError
from nexura_agents.harness import TODO_STATE_KEY, todo_tools
from nexura_agents.history import apply_processors, message_tokens
from nexura_agents.skills import SkillError
from nexura_agents.tokenizers import default_token_estimator
from nexura_agents.usage import BudgetConfig, BudgetManager
from nexura_agents.usage.models import ModelPricing


def call(name: str, **arguments: Any) -> Message:
    return Message("assistant", tool_calls=[ToolCall(name, name, arguments)])


def context() -> RunContext[Any]:
    return RunContext(deps=None, run_id="local-fake", agent_name="test")


def skill_directory(tmp_path: Path) -> Path:
    root = tmp_path / "skills" / "review"
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text(
        "---\nname: review\ndescription: >-\n  Review changes\n  carefully.\n"
        'license: MIT\nmetadata:\n  version: "1.0"\n'
        "allowed-tools: Bash(*)\n---\nPRIVATE BODY\nSee references/check.md.\n"
    )
    (root / "references").mkdir()
    (root / "references" / "check.md").write_text("PRIVATE RESOURCE")
    (root / "scripts").mkdir()
    (root / "scripts" / "danger.py").write_text("raise AssertionError('must never execute')")
    return root


def test_skills_progressive_loading_and_no_execution(tmp_path: Path) -> None:
    root = skill_directory(tmp_path)
    library = SkillLibrary.from_directory(root.parent)
    skill = library.get("review")
    assert skill.description == "Review changes carefully."
    assert skill.metadata["metadata"]["version"] == "1.0"
    assert skill.tools == ()  # allowed-tools never grants capabilities
    restored = SkillLibrary()
    restored.import_versions(library.export())
    assert restored.get("review").sha256 == skill.sha256
    model = FakeModel(
        responses=[
            call("load_skill", skill="review"),
            call("read_skill_resource", skill="review", path="references/check.md"),
            "done",
        ]
    )
    agent = Agent("A", "System", model, harness=Harness(skills=library))
    result = agent.run("review")
    first = model.calls[0]["messages"]
    assert "Review changes carefully." in first[0].content
    assert "PRIVATE" not in str(first)
    assert "PRIVATE BODY" in str(model.calls[1]["messages"])
    assert "PRIVATE RESOURCE" not in str(model.calls[1]["messages"])
    assert "PRIVATE RESOURCE" in str(model.calls[2]["messages"])
    assert "scripts/" not in str(result.messages)
    assert all(t.effect_class().value == "pure" for t in agent.tools)
    assert set(agent.tools_by_name) == {"load_skill", "read_skill_resource"}


@pytest.mark.parametrize(
    "front",
    [
        "name: a\nname: b\ndescription: x",
        "name: a\ndescription: !python/object x",
        "name: a\ndescription: &alias x",
        "name: a\ndescription: *alias",
        "name: a\ndescription: [bad]",
        "name: a--b\ndescription: x",
        "name: A\ndescription: x",
        "name: a\ndescription: x\nmetadata:\n  x: a\n  x: b",
        "description: x",
        "name: a",
        "name: a\ndescription: x\n bad: invalid",
    ],
)
def test_spec_frontmatter_rejects_unsafe_or_ambiguous_yaml(front: str) -> None:
    with pytest.raises(SkillError):
        Skill.from_markdown(f"---\n{front}\n---\nbody", format="agent-skills")


def test_spec_scalars_bounds_and_legacy_still_strict(tmp_path: Path) -> None:
    s = Skill.from_markdown(
        "---\nname: a\ndescription: 'it''s safe' # comment\n---\nbody",
        format="agent-skills",
    )
    assert s.description == "it's safe"
    long = "x" * 1024
    assert (
        Skill.from_markdown(
            f"---\nname: a\ndescription: {long}\n---\nbody", format="agent-skills"
        ).description
        == long
    )
    with pytest.raises(SkillError):
        Skill.from_markdown(f"---\nname: a\ndescription: {long}x\n---\nbody", format="agent-skills")
    with pytest.raises(SkillError):
        Skill.from_markdown("---\nname: a\ndescription: >\n  old\n---\nbody")
    root = skill_directory(tmp_path)
    with pytest.raises(SkillError):
        Skill.from_directory(root)  # legacy strict text-only directory behavior
    (root / "references" / "escape.md").symlink_to(tmp_path / "outside.md")
    with pytest.raises(SkillError):
        Skill.from_directory(root, format="agent-skills")


def history() -> list[Message]:
    return (
        [Message("system", "Keep this system prompt")]
        + [
            m
            for i in range(5)
            for m in (
                Message("user", f"old {i} " + "x" * 1000),
                Message("assistant", tool_calls=[ToolCall(f"c{i}", "lookup", {})]),
                Message("tool", "result " + "y" * 400, tool_call_id=f"c{i}"),
            )
        ]
        + [Message("user", "LATEST")]
    )


async def test_compaction_trigger_pairs_latest_determinism_and_no_mutation() -> None:
    messages = history()
    original = [m.to_dict() for m in messages]
    proc = compact_context(600)
    out = await apply_processors([proc], context(), messages)
    assert out == await apply_processors([proc], context(), messages)
    assert out[0] is messages[0] and out[-1] is messages[-1]
    asked = {c.id for m in out for c in m.tool_calls or ()}
    assert asked == {m.tool_call_id for m in out if m.role == "tool"}
    assert len(out) < len(messages)
    assert [m.to_dict() for m in messages] == original
    assert await compact_context(100000)(context(), messages) is messages


async def test_compaction_cross_turn_pairs_pending_and_system_are_pinned() -> None:
    messages = history()
    # A delayed result crosses a real user boundary: the entire connected group stays
    # together. An unanswered approval and a later system message cannot be discarded.
    messages += [
        call("approval", amount=5),
        Message("user", "while awaiting approval"),
        Message("system", "later system"),
        call("cross"),
        Message("user", "LATEST AGAIN"),
        Message("tool", "ok", tool_call_id="cross"),
    ]
    run_ctx = context()
    run_ctx.state["pending_approval"] = {"digest": "unchanged"}
    out = await apply_processors([compact_context(10)], run_ctx, messages)
    assert messages[-7:] == out[-7:]
    assert out[0] == messages[0]
    assert run_ctx.state == {"pending_approval": {"digest": "unchanged"}}
    assert run_ctx._events.take_pending()[-1]["data"]["overflow"] is True


def budget(max_tokens: int = 100000) -> Any:
    manager = BudgetManager()
    manager.pricing_table.add_model_pricing(
        ModelPricing(
            model_id="fake-model",
            input_cost_per_million=Decimal(0),
            output_cost_per_million=Decimal(0),
        )
    )
    return manager.create_run_budget("local", "c1", config=BudgetConfig(max_tokens=max_tokens))


async def test_summary_accounting_cache_and_budget_refusal() -> None:
    usage = Usage(prompt_tokens=7, completion_tokens=3, total_tokens=10)
    summary = FakeModel(responses=["Earlier facts."], default_usage=usage)
    proc = compact_context(600, model=summary)
    run_ctx = context()
    one = await proc(run_ctx, history())
    assert one == await proc(run_ctx, history())
    assert len(summary.calls) == 1 and run_ctx.take_nested_usage() == [usage]
    assert sum(message_tokens(m, default_token_estimator) for m in one) <= 420
    summary2 = FakeModel(responses=["Earlier facts."], default_usage=usage)
    main = FakeModel(responses=["done"], default_usage=usage)
    agent = Agent(
        "A", "sys", main, harness=Harness(compaction=compact_context(600, model=summary2))
    )
    result = await Runner(budget=budget()).run_async(agent, "new", history=history())
    assert result.usage.total_tokens == 20
    refused_summary = FakeModel(responses=["cannot run"], default_usage=usage)
    refused_main = FakeModel(responses=["cannot run"], default_usage=usage)
    refused = Agent(
        "A",
        "sys",
        refused_main,
        harness=Harness(compaction=compact_context(600, model=refused_summary)),
    )
    with pytest.raises(SDKValidationError, match="History processor"):
        await Runner(budget=budget(1)).run_async(refused, "new", history=history())
    assert not refused_summary.calls and not refused_main.calls


@pytest.mark.parametrize("rooted", [False, True])
def test_filesystem_operations_containment_and_caps(tmp_path: Path, rooted: bool) -> None:
    with ScopedFilesystem(
        tmp_path if rooted else None, max_file_bytes=12, max_total_bytes=20, max_entries=4
    ) as fs:
        fs.write("dir/a.txt", "hello")
        fs.edit("dir/a.txt", "hello", "world")
        assert fs.read("dir/a.txt") == "world" and fs.list() == ["dir/a.txt"]
        for path in ("../out", "/absolute", "a/../b", "./x", "a//b", "C:\\x", "a\\x", "a/", ""):
            with pytest.raises(SDKValidationError):
                fs.write(path, "no")
            with pytest.raises(SDKValidationError):
                fs.read(path)
        with pytest.raises(SDKValidationError):
            fs.write("big", "x" * 13)
        fs.write("b", "x" * 12)
        with pytest.raises(SDKValidationError):
            fs.write("c", "x" * 4)
        with pytest.raises(SDKValidationError):
            fs.write("x/y/z", "x")
        with pytest.raises(SDKValidationError):
            fs.write("dir/a.txt/child", "x")
        with pytest.raises(SDKValidationError):
            fs.write("dir", "x")
        with pytest.raises(SDKValidationError):
            fs.edit("dir/a.txt", "", "x")
        with pytest.raises(SDKValidationError):
            fs.edit("dir/a.txt", "absent", "x")
        assert fs.read("dir/a.txt") == "world"
    with pytest.raises(SDKValidationError, match="closed"):
        fs.list()


def test_filesystem_symlinks_hardlinks_and_special_files(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("secret")
    (root / "link").symlink_to(outside)
    with ScopedFilesystem(root) as fs:
        for operation in (lambda: fs.read("link"), lambda: fs.write("link", "bad"), fs.list):
            with pytest.raises((OSError, SDKValidationError)):
                operation()
    (root / "link").unlink()
    (root / "dir").symlink_to(tmp_path, target_is_directory=True)
    with ScopedFilesystem(root) as fs:
        with pytest.raises(OSError):
            fs.read("dir/outside")
        with pytest.raises((OSError, SDKValidationError)):
            fs.write("dir/outside", "bad")
    with pytest.raises(OSError):
        ScopedFilesystem(root / "dir")
    (root / "dir").unlink()
    os.link(outside, root / "hard")
    with ScopedFilesystem(root) as fs:
        with pytest.raises(SDKValidationError):
            fs.write("hard", "bad")
        with pytest.raises(SDKValidationError):
            fs.read("hard")
    (root / "hard").unlink()
    os.mkfifo(root / "fifo")
    with ScopedFilesystem(root) as fs, pytest.raises(SDKValidationError):
        fs.read("fifo")
    assert outside.read_text() == "secret"


async def test_todos_events_isolation_and_validation() -> None:
    plan = [{"id": "one", "text": "Do work", "status": "in_progress"}]
    model = FakeModel(responses=[call("write_todos", items=plan), call("read_todos"), "done"])
    agent = Agent("A", "sys", model, harness=Harness(todo=True))
    result = await agent.run_async("go")
    assert result.state[TODO_STATE_KEY] == plan
    assert any(e.data.get("name") == "nexura.todos_updated" for e in result.events)
    assert agent.tools_by_name["write_todos"].effect_class().value == "idempotent"
    assert agent.tools_by_name["read_todos"].effect_class().value == "pure"
    other = await agent.clone(model=FakeModel(responses=["done"])).run_async("other")
    assert other.state == {}
    writer = todo_tools()[0]
    for items in (
        plan * 2,
        [{"id": "a", "text": "x", "status": "bad"}],
        [{"id": str(i), "text": "x"} for i in range(101)],
    ):
        with pytest.raises((ToolError, SDKValidationError)):
            await writer.execute({"items": items}, context())


async def test_subagent_shares_explicit_filesystem_and_inherits_hooks_budget() -> None:
    fs = ScopedFilesystem()
    log: list[str] = []

    class Policy(AgentHooks):
        async def before_tool(self, ctx: RunContext[Any], tool_call: ToolCall) -> None:
            log.append(ctx.agent_name + ":" + tool_call.name)

        def output_guardrails(self, ctx: RunContext[Any], agent: Agent) -> list[Any]:
            return [lambda ctx, value: GuardrailResult.passed()]

    usage = Usage(prompt_tokens=7, completion_tokens=3, total_tokens=10)
    child = Agent(
        "Child",
        "sys",
        FakeModel(
            responses=[
                call("fs_read", path="note"),
                call("fs_edit", path="note", old="hello", new="bye"),
                "child",
            ],
            default_usage=usage,
        ),
        harness=Harness(filesystem=fs),
    )
    parent = Agent(
        "Parent",
        "sys",
        FakeModel(
            responses=[
                call("fs_write", path="note", content="hello"),
                call("child", request="edit"),
                "done",
            ],
            default_usage=usage,
        ),
        tools=[child.as_tool()],
        harness=Harness(filesystem=fs),
    )
    allowance = budget()
    result = await Runner(hooks=Policy(), budget=allowance).run_async(parent, "go")
    assert fs.read("note") == "bye" and result.usage.total_tokens == 60
    assert log == ["Parent:fs_write", "Parent:child", "Child:fs_read", "Child:fs_edit"]
    assert len(allowance.manager.ledger._reservations) == 6
    assert parent.tools_by_name["fs_write"].effect_class().value == "non_idempotent"


async def test_harness_approvals_and_guardrails_prevent_mutations() -> None:
    fs = ScopedFilesystem()

    class Gate(AgentHooks):
        def tool_needs_approval(self, ctx: Any, tool: Any, arguments: Any) -> bool:
            return bool(tool.name == "fs_write")

    child = Agent(
        "Child",
        "sys",
        FakeModel(responses=[call("fs_write", path="x", content="y")]),
        harness=Harness(filesystem=fs),
    )
    parent = Agent(
        "Parent",
        "sys",
        FakeModel(responses=[call("child", request="write")]),
        tools=[child.as_tool()],
    )
    with pytest.raises(ToolError, match="need human"):
        await Runner(hooks=Gate()).run_async(parent, "go")
    assert fs.list() == []
    gated = child.clone(
        model=FakeModel(responses=[call("fs_write", path="x", content="y")]),
        harness=Harness(filesystem=fs, require_write_approval=True),
    )
    assert (await gated.run_async("go")).status == "paused"
    assert fs.list() == []
    tools = fs.build_tools()
    writer = next(t for t in tools if t.name == "fs_write")
    writer.input_guardrails = (lambda ctx, value: GuardrailResult.rejected("blocked"),)
    guarded = Agent(
        "A",
        "sys",
        FakeModel(responses=[call("fs_write", path="x", content="y"), "done"]),
        tools=tools,
    )
    await guarded.run_async("go")
    assert fs.list() == []


def test_no_opt_in_and_empty_harness_preserve_requests() -> None:
    first = FakeModel(responses=["done"])
    second = FakeModel(responses=["done"])
    plain = Agent("A", "sys", first)
    empty = Agent("A", "sys", second, harness=Harness())
    assert plain.run("go").output == empty.run("go").output
    assert first.calls == second.calls
    assert plain.tools == empty.tools and plain.history_processors == empty.history_processors
    assert plain.definition_digest() == empty.definition_digest()
    assert json.dumps(empty.clone(model=FakeModel(responses=["done"])).run("again").state) == "{}"
