"""Round-2 C1 adversarial checks; local fake models and temporary files only."""

from __future__ import annotations

import ctypes
import errno
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, cast

import pytest
from _c1_platform import requires_landlock, requires_linux_landlock_api
from nexura_agents import (
    Agent,
    FakeModel,
    GuardrailResult,
    Harness,
    MemoryCheckpointStore,
    Message,
    RunInterruption,
    Runner,
    ScopedFilesystem,
    ToolCall,
)
from nexura_agents.errors import ApprovalDecisionError, SDKValidationError


@pytest.mark.parametrize("guard", ["tool", "g4"])
async def test_todo_identifier_cannot_bypass_guardrails(guard: str) -> None:
    secret = "PRIVATE-CUSTOMER-ACCOUNT-4821"
    writer = Message(
        "assistant",
        tool_calls=[
            ToolCall("todo", "write_todos", {"items": [{"id": secret, "text": "ordinary task"}]})
        ],
    )
    options: dict[str, Any] = (
        {
            "tool_use_behavior": "stop_on_first_tool",
            "output_guardrails": [lambda ctx, value: GuardrailResult.rewritten("[checked]")],
        }
        if guard == "g4"
        else {}
    )
    agent = Agent(
        "A", "local fake", FakeModel([writer, "done"]), harness=Harness(todo=True), **options
    )
    if guard == "tool":
        agent.tools_by_name["write_todos"].output_guardrails = (
            lambda ctx, value: GuardrailResult.rejected("private identifier"),
        )
    events = [e async for e in Runner().stream_async(agent, "go", stream_text=True)]
    custom = [e.data for e in events if e.type.value == "custom"]
    assert secret not in str(custom), f"unchecked todo id leaked: {custom}"


@pytest.mark.parametrize("disk", [False, pytest.param(True, marks=requires_landlock)])
async def test_filesystem_approval_digest_cannot_be_reused_for_another_run(
    tmp_path: Path, disk: bool
) -> None:
    with ScopedFilesystem(tmp_path if disk else None) as fs:
        runner = Runner(checkpoint_store=MemoryCheckpointStore())

        def build() -> Agent[str]:
            writer = Message(
                "assistant",
                tool_calls=[ToolCall("write", "fs_write", {"path": "note", "content": "approved"})],
            )
            return Agent(
                "A",
                "local fake",
                FakeModel([writer, "done"]),
                harness=Harness(filesystem=fs, require_write_approval=True),
            )

        first_agent, second_agent = build(), build()
        first = await runner.run_async(first_agent, "first run")
        second = await runner.run_async(second_agent, "second run")
        first.interruption = cast(RunInterruption, first.interruption)
        second.interruption = cast(RunInterruption, second.interruption)
        reviewed = first.interruption.pending_digests()
        await runner.resume_async(first_agent, first.interruption, approve=reviewed)
        # First grant has been consumed. A fresh run needs a fresh review decision.
        with pytest.raises(ApprovalDecisionError):
            await runner.resume_async(second_agent, second.interruption, approve=reviewed)


@pytest.mark.parametrize("abi", [-1, 0, 1, 2])
@requires_linux_landlock_api
def test_unavailable_or_old_landlock_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, abi: int
) -> None:
    import nexura_agents._filesystem_sandbox as sandbox

    class StubLibc:
        def syscall(self, number: int, *arguments: Any) -> int:
            assert number == 444 and arguments == (0, 0, 1), "must not install an insufficient ABI"
            if abi < 0:
                ctypes.set_errno(errno.ENOSYS)
            return abi

    monkeypatch.setattr(cast(Any, sandbox).ctypes, "CDLL", lambda *args, **kwargs: StubLibc())
    with pytest.raises((OSError, SDKValidationError)):
        ScopedFilesystem(tmp_path)
    # There is no fallback to unconfined disk; explicit memory mode still works.
    with ScopedFilesystem() as memory:
        memory.write("note", "ok")
        assert memory.read("note") == "ok"


@requires_landlock
def test_landlock_worker_isolation_does_not_restrict_sibling_threads(tmp_path: Path) -> None:
    first_root, second_root = tmp_path / "one", tmp_path / "two"
    first_root.mkdir()
    second_root.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("host")
    libc = ctypes.CDLL(None, use_errno=True)
    before = libc.prctl(39, 0, 0, 0, 0)  # PR_GET_NO_NEW_PRIVS
    with ScopedFilesystem(first_root) as first, ScopedFilesystem(second_root) as second:
        first.write("note", "first")
        second.write("note", "second")
        assert first.read("note") == "first" and second.read("note") == "second"
        for filesystem, other in ((first, second_root), (second, first_root)):

            def read_other(root: Path = other) -> str:
                return (root / "note").read_text()

            with pytest.raises(PermissionError):
                filesystem._disk_call(lambda: outside.read_text())
            with pytest.raises(PermissionError):
                filesystem._disk_call(read_other)
            with pytest.raises(PermissionError):
                filesystem._disk_call(lambda: outside.write_text("escape"))
        assert outside.read_text() == "host"
        with ThreadPoolExecutor(max_workers=1) as unrelated:
            assert unrelated.submit(outside.read_text).result() == "host"
            unrelated.submit(outside.write_text, "sibling allowed").result()
    assert libc.prctl(39, 0, 0, 0, 0) == before
    assert outside.read_text() == "sibling allowed"


@requires_landlock
def test_landlock_uapi_layout_and_running_kernel() -> None:
    from nexura_agents._filesystem_sandbox import _Beneath, _Ruleset

    assert ctypes.sizeof(_Ruleset) == 8
    assert _Ruleset.handled_access_fs.offset == 0
    assert ctypes.sizeof(_Beneath) == 12
    assert _Beneath.allowed_access.offset == 0 and _Beneath.parent_fd.offset == 8
    abi = ctypes.CDLL(None, use_errno=True).syscall(444, 0, 0, 1)
    assert abi >= 3
