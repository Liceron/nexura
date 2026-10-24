# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Beyond the unchanged review repros: kernel operation checks and scoped grants."""

from __future__ import annotations

import copy
import os
import threading
from pathlib import Path
from typing import Any

import pytest
from nexura_agents import (
    Agent,
    ApproveWith,
    FakeModel,
    Harness,
    MemoryCheckpointStore,
    Message,
    RunInterruption,
    Runner,
    ScopedFilesystem,
    ToolCall,
    Usage,
    compact_context,
)
from nexura_agents.errors import ApprovalDecisionError


@pytest.mark.parametrize("operation", ["read", "replace"])
def test_kernel_denies_directory_move_after_last_path_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "sub").mkdir()
    (root / "sub/note").write_text("original")
    outside = tmp_path / "moved"
    ready, moved = threading.Event(), threading.Event()
    errors: list[BaseException] = []

    def attacker() -> None:
        try:
            assert ready.wait(5)
            (root / "sub").rename(outside)
            (root / "sub").mkdir()
            (outside / "note").write_text("PRIVATE OUTSIDE")
        except BaseException as exc:
            errors.append(exc)
        finally:
            moved.set()

    def pause() -> None:
        ready.set()
        assert moved.wait(5)

    with ScopedFilesystem(root) as files:
        original_open, original_replace = os.open, os.replace

        def raced_open(
            path: Any, flags: int, mode: int = 0o777, *, dir_fd: int | None = None
        ) -> int:
            if operation == "read" and path == "note":
                pause()
            return original_open(path, flags, mode, dir_fd=dir_fd)

        def raced_replace(source: Any, target: Any, **kwargs: Any) -> None:
            pause()
            original_replace(source, target, **kwargs)

        monkeypatch.setattr(os, "open", raced_open)
        monkeypatch.setattr(os, "replace", raced_replace)
        actor = threading.Thread(target=attacker)
        actor.start()
        try:
            with pytest.raises(PermissionError):
                if operation == "read":
                    files.read("sub/note")
                else:
                    files.write("sub/note", "must not escape")
        finally:
            actor.join(6)
        assert not actor.is_alive() and not errors
        assert (outside / "note").read_text() == "PRIVATE OUTSIDE"


@pytest.mark.parametrize(
    "tool,args,target",
    [
        (
            "fs_write",
            {"path": "first", "content": "approved"},
            {"path": "second", "content": "approved"},
        ),
        (
            "write_todos",
            {"items": [{"id": "one", "text": "approved"}]},
            {"items": [{"id": "other", "text": "approved"}]},
        ),
    ],
)
async def test_canonical_scoped_arguments_cannot_be_retargeted(
    tool: str,
    args: dict[str, Any],
    target: dict[str, Any],
) -> None:
    with ScopedFilesystem() as files:
        agent = Agent(
            "A",
            "local fake",
            FakeModel([Message("assistant", tool_calls=[ToolCall("c", tool, args)])]),
            harness=Harness(todo=True, filesystem=files, require_write_approval=True),
        )
        runner = Runner(checkpoint_store=MemoryCheckpointStore())
        result = await runner.run_async(agent, "go")
        assert result.interruption is not None
        paused = RunInterruption.from_json(result.interruption.to_json())
        assert (
            paused.tool_scopes and paused.pending_digests() == result.interruption.pending_digests()
        )
        digests = paused.pending_digests()
        tampered = copy.deepcopy(paused)
        tampered.pending[0].arguments.clear()
        tampered.pending[0].arguments.update(target)
        with pytest.raises(ApprovalDecisionError):
            await runner.resume_async(agent, tampered, approve=digests)
        with pytest.raises(ApprovalDecisionError, match="fresh approval"):
            await runner.resume_async(
                agent, paused, approve={"c": ApproveWith(target, digests["c"])}
            )
        assert files.list() == []
        done = await runner.resume_async(
            agent.clone(model=FakeModel(["done"])), paused, approve=digests
        )
        assert done.status == "completed"


@pytest.mark.parametrize("replaced", [False, True])
async def test_rebuilt_disk_scope_resumes_but_replaced_root_does_not(
    tmp_path: Path,
    replaced: bool,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    runner = Runner(checkpoint_store=MemoryCheckpointStore())
    with ScopedFilesystem(root) as first:
        agent = Agent(
            "A",
            "local fake",
            FakeModel(
                [
                    Message(
                        "assistant",
                        tool_calls=[ToolCall("c", "fs_write", {"path": "note", "content": "ok"})],
                    )
                ]
            ),
            harness=Harness(filesystem=first, require_write_approval=True),
        )
        paused = await runner.run_async(agent, "go")
    assert paused.interruption is not None
    if replaced:
        root.rename(tmp_path / "old-root")
        root.mkdir()
    with ScopedFilesystem(root) as rebuilt:
        resumed = agent.clone(
            model=FakeModel(["done"]),
            harness=Harness(filesystem=rebuilt, require_write_approval=True),
        )
        if replaced:
            with pytest.raises(ApprovalDecisionError, match="scope changed"):
                await runner.resume_async(
                    resumed, paused.interruption, approve=paused.interruption.pending_digests()
                )
            assert rebuilt.list() == []
        else:
            result = await runner.resume_async(
                resumed, paused.interruption, approve=paused.interruption.pending_digests()
            )
            assert result.status == "completed" and rebuilt.read("note") == "ok"


async def test_summary_checkpoint_replays_once_with_usage_and_no_duplicate_text() -> None:
    class Crash(BaseException):
        pass

    summary = FakeModel(["SAVED SUMMARY"], default_usage=Usage(4, 2, 6))
    store = MemoryCheckpointStore()
    history = [
        message
        for i in range(4)
        for message in (
            Message("user", f"old {i} " + "x" * 1500),
            Message("assistant", "ok"),
        )
    ]

    def build(model: FakeModel) -> Agent:
        return Agent(
            "A",
            "local fake",
            model,
            harness=Harness(compaction=compact_context(500, model=summary)),
        )

    def crash(messages: Any, tools: Any) -> str:
        raise Crash()

    runner = Runner(checkpoint_store=store)
    with pytest.raises(Crash):
        await runner.run_async(build(FakeModel(handler=crash)), "latest", history=history)
    (run_id,) = await store.list_incomplete()
    recovered = FakeModel(["done"], default_usage=Usage(3, 1, 4))
    result = await runner.recover_async(build(recovered), run_id)
    assert len(summary.calls) == 1
    assert result.usage.total_tokens == 10
    assert (
        sum((m.content or "").count("SAVED SUMMARY") for m in recovered.calls[0]["messages"]) == 1
    )
    assert not any("SAVED SUMMARY" in (m.content or "") for m in result.messages)


async def test_todo_approval_is_bound_to_the_run_state_target() -> None:
    def build() -> Agent:
        return Agent(
            "A",
            "local fake",
            FakeModel(
                [
                    Message(
                        "assistant",
                        tool_calls=[
                            ToolCall("c", "write_todos", {"items": [{"id": "one", "text": "task"}]})
                        ],
                    )
                ]
            ),
            harness=Harness(todo=True, require_write_approval=True),
        )

    runner = Runner(checkpoint_store=MemoryCheckpointStore())
    first = await runner.run_async(build(), "go")
    second = await runner.run_async(build(), "go")
    assert first.interruption is not None and second.interruption is not None
    assert first.interruption.pending_digests() != second.interruption.pending_digests()
    with pytest.raises(ApprovalDecisionError, match="does not match"):
        await runner.resume_async(
            build(), second.interruption, approve=first.interruption.pending_digests()
        )
