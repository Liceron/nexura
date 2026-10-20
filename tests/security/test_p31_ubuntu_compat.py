"""Offline Ubuntu default-profile contracts; never probe or launch real bwrap."""

from __future__ import annotations

import asyncio
import contextlib
import os
import struct
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from nexura_sandbox import BubblewrapWorker
from nexura_sandbox import bubblewrap_worker as bw
from nexura_sandbox.bubblewrap_policy import default_bpf

from nexura_runtime.integrations.sandbox import ExecutionRequest, SandboxConfig


def test_default_profile_needs_no_remount_or_privileged_launcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_setup(*args: Any, **kwargs: Any) -> bytes:
        pytest.fail("default profile must not generate a privileged inode-remount launcher")

    monkeypatch.setattr(bw, "setup_launcher", unexpected_setup)
    config = SandboxConfig(bubblewrap_cpu_seconds=2)
    assert config.bubblewrap_tmpfs_inodes is None
    with contextlib.ExitStack() as stack:
        namespace = bw._NamespaceInit(stack)
        args = bw._hardened_args(
            stack,
            config,
            "/fake/bwrap",
            command=["/usr/bin/true"],
            info_fd=namespace.info_fd,
            block_fd=namespace.block_fd,
        )
        assert args[0] == "/fake/bwrap" and args[-1] == "/usr/bin/true"
        assert args[args.index("--uid") + 1] == "65534"
        assert args[args.index("--cap-drop") + 1] == "ALL"
        assert "--cap-add" not in args and "/nexura-internal/launcher.py" not in args
        for flag in ("--disable-userns", "--unshare-pid", "--as-pid-1", "--cpu=2:2"):
            assert flag in args
        assert args[args.index("--size") + 1] == str(config.bubblewrap_tmpfs_bytes)
        policy_fd = int(args[args.index("--seccomp") + 1])
        assert os.pread(policy_fd, 32768, 0) == default_bpf()
        assert {policy_fd, namespace.info_fd, namespace.block_fd} <= set(bw._argument_fds(args))
        remounts = [args[i + 1] for i, flag in enumerate(args[:-1]) if flag == "--remount-ro"]
        assert set(remounts) == {"/", "/dev", "/proc"}


def test_default_profile_stacks_operator_filter_without_replacing_keyring_policy(
    tmp_path: Path,
) -> None:
    operator = tmp_path / "allow.bpf"
    operator.write_bytes(struct.pack("=HBBI", 6, 0, 0, 0x7FFF0000))
    config = SandboxConfig(bubblewrap_seccomp_bpf_path=str(operator))
    with contextlib.ExitStack() as stack:
        args = bw._hardened_args(stack, config, "/fake/bwrap", command=["/usr/bin/true"])
        filters = [
            int(args[i + 1]) for i, flag in enumerate(args[:-1]) if flag == "--add-seccomp-fd"
        ]
        assert "--seccomp" not in args and len(filters) == 2
        assert os.pread(filters[0], 32768, 0) == default_bpf()
        assert os.pread(filters[1], 32768, 0) == operator.read_bytes()
        assert set(filters) <= set(bw._argument_fds(args))
        assert "/nexura-internal/launcher.py" not in args


@pytest.mark.parametrize("inodes", [None, 128])
def test_probe_uses_default_or_explicit_inode_profile_without_silent_fallback(
    monkeypatch: pytest.MonkeyPatch, inodes: int | None
) -> None:
    commands: list[list[str]] = []

    class FakeBwrap:
        pid = 987654321

        def __init__(self, args: list[str], **kwargs: Any) -> None:
            commands.append(args)
            self.inode_setup = "/nexura-internal/launcher.py" in args
            self.returncode = 1 if self.inode_setup else 0
            assert kwargs["pass_fds"] == bw._argument_fds(args)

        def communicate(self, timeout: float) -> tuple[bytes, bytes]:
            if self.inode_setup:
                return b"", b"PermissionError: [Errno 1] tmpfs inode remount failed"
            return b"", b""

    monkeypatch.setattr(BubblewrapWorker, "_binary", lambda *a: "/fake/bwrap")
    monkeypatch.setattr(bw.subprocess, "Popen", FakeBwrap)
    status = BubblewrapWorker().probe(SandboxConfig(bubblewrap_tmpfs_inodes=inodes))
    assert status.available is (inodes is None)
    if inodes is not None:
        assert "tmpfs inode remount failed" in status.reason
    assert len(commands) == 1


@pytest.mark.parametrize("inodes", [None, 128])
async def test_result_reports_only_enforced_limits(
    monkeypatch: pytest.MonkeyPatch, inodes: int | None
) -> None:
    """Fake successful transport; the request is never executed by a host process."""
    marker = b"NEXURA_START_" + b"a" * 32 + b"\n"
    monkeypatch.setattr(bw.uuid, "uuid4", lambda: SimpleNamespace(hex="a" * 32))
    monkeypatch.setattr(BubblewrapWorker, "_binary", lambda *a: "/fake/bwrap")
    monkeypatch.setattr(bw._NamespaceInit, "capture", lambda s: True)
    monkeypatch.setattr(bw, "_kill_group", lambda pid: None)

    async def spawn(*args: str, **kwargs: Any) -> Any:
        assert kwargs["pass_fds"] == bw._argument_fds(args)
        stdout, stderr = asyncio.StreamReader(), asyncio.StreamReader()
        stdout.feed_data(marker + b"ok\n")
        stdout.feed_eof()
        stderr.feed_eof()

        async def wait() -> int:
            return 0

        async def drain() -> None:
            pass

        return SimpleNamespace(
            pid=987654321,
            returncode=0,
            stdout=stdout,
            stderr=stderr,
            wait=wait,
            stdin=SimpleNamespace(write=lambda b: None, drain=drain, close=lambda: None),
        )

    monkeypatch.setattr(bw.asyncio, "create_subprocess_exec", spawn)
    worker = BubblewrapWorker()
    result = await worker.execute(
        ExecutionRequest(code="never executed"), SandboxConfig(bubblewrap_tmpfs_inodes=inodes)
    )
    assert result.stdout == "ok\n" and result.details["seccomp"] is True
    assert result.details["tmpfs_inodes"] == inodes
    assert result.details["tmpfs_inodes_enforced"] is (inodes is not None)
    assert "address_space" in worker.enforced_limits and "disk_bytes" in worker.enforced_limits
    assert not {"memory", "disk", "tmpfs_inodes"} & worker.enforced_limits
    assert ("tmpfs_inodes" in result.details["enforced_limits"]) is (inodes is not None)
    assert worker.active_worker_count == 0
