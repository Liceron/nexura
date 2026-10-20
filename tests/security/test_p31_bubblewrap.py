"""Offline argument/probe tests and explicitly fake transport lifecycle tests; no bwrap needed."""

from __future__ import annotations

import asyncio
import contextlib
import os
import resource
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from nexura_sandbox import BubblewrapWorker
from nexura_sandbox import bubblewrap_worker as bw

from nexura_runtime.integrations.sandbox import (
    DEFAULT_BACKEND_ORDER,
    BackendStatus,
    CapabilityUnavailableError,
    ExecutionRequest,
    ExecutionTimeoutError,
    OutputLimitExceededError,
    SandboxConfig,
    build_backend,
    sandbox_config_from_env,
    select_backend,
)


def test_builder_policy_and_no_host_mounts() -> None:
    config = SandboxConfig(bubblewrap_tmpfs_bytes=2_097_152, max_processes=2)
    args = bw.build_bubblewrap_args(
        config,
        "/fake/bwrap",
        code_fd=12,
        input_fds=[("nested/a.txt", 13)],
    )
    for flag in (
        "--unshare-all",
        "--unshare-user",
        "--unshare-net",
        "--disable-userns",
        "--die-with-parent",
        "--new-session",
        "--clearenv",
        "--as-pid-1",
    ):
        assert flag in args
    assert args[args.index("--cap-drop") + 1] == "ALL"
    assert args[args.index("--uid") + 1] == "65534"
    assert args[args.index("--size") : args.index("--size") + 4] == [
        "--size",
        "2097152",
        "--tmpfs",
        "/tmp",
    ]
    assert args[args.index("--chdir") + 1] == "/tmp/work"
    assert args[args.index("--file") : args.index("--file") + 3] == [
        "--file",
        "12",
        "/tmp/code.py",
    ]
    assert args[args.index("--ro-bind-data") + 2] == "/sandbox/nested/a.txt"
    assert args[-8:] == [
        "/usr/bin/prlimit",
        "--nproc=2:2",
        "--",
        "/usr/bin/python3",
        "-I",
        "-B",
        "-u",
        "/tmp/code.py",
    ]
    assert "/usr/bin/prlimit" in args
    assert "--seccomp" not in args
    for bad in ("--bind", "--share-net", "/home", str(Path.cwd()), str(Path.home())):
        assert bad not in args
    remounts = [args[i + 1] for i, arg in enumerate(args[:-1]) if arg == "--remount-ro"]
    assert set(remounts) == {"/", "/dev", "/proc"}


def test_runtime_venv_is_mapped_readonly(tmp_path: Path) -> None:
    runtime = tmp_path / "venv"
    (runtime / "bin").mkdir(parents=True)
    python = runtime / "bin" / "python"
    python.symlink_to("/usr/bin/python3")
    unapproved = SandboxConfig(
        bubblewrap_runtime_path=str(runtime),
        bubblewrap_python_path=str(python),
    )
    # The runtime must sit inside an operator-approved root (P31 review R8).
    with pytest.raises(CapabilityUnavailableError, match="bubblewrap_runtime_roots"):
        bw.build_bubblewrap_args(unapproved, "/fake/bwrap")
    config = unapproved.model_copy(update={"bubblewrap_runtime_roots": [str(tmp_path)]})
    args = bw.build_bubblewrap_args(config, "/fake/bwrap")
    assert args[-5:] == ["/opt/nexura-runtime/bin/python", "-I", "-B", "-u", "/tmp/code.py"]
    assert ["--ro-bind", str(runtime), "/opt/nexura-runtime"] == args[
        args.index(str(runtime)) - 1 : args.index(str(runtime)) + 2
    ]
    python.unlink()
    python.symlink_to(sys.executable)
    if not Path(sys.executable).resolve().is_relative_to("/usr"):
        with pytest.raises(CapabilityUnavailableError, match="symlink target"):
            bw.build_bubblewrap_args(config, "/fake/bwrap")


@pytest.mark.parametrize("path", ["../escape", "/etc/passwd", "x\x00y"])
def test_builder_rejects_path_escape(path: str) -> None:
    with pytest.raises(Exception, match="path"):
        bw.build_bubblewrap_args(SandboxConfig(), "/fake/bwrap", input_fds=[(path, 12)])


def test_limits_are_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    limits: dict[int, tuple[int, int]] = {}
    monkeypatch.setattr(resource, "setrlimit", lambda key, pair: limits.update({key: pair}))
    bw._preexec_limits(SandboxConfig(max_memory_mb=64, bubblewrap_cpu_seconds=2))()
    assert limits[resource.RLIMIT_CPU] == (2, 3)
    assert limits[resource.RLIMIT_AS] == (64 * 1024 * 1024,) * 2
    assert limits[resource.RLIMIT_FSIZE] == (33_554_432,) * 2
    assert limits[resource.RLIMIT_CORE] == (0, 0)


def _fake_probe(
    monkeypatch: pytest.MonkeyPatch, *, rc: int = 0, timeout: bool = False
) -> list[Any]:
    calls: list[Any] = []
    monkeypatch.setattr(BubblewrapWorker, "_binary", lambda *args: "/fake/bwrap")

    class FakeProcess:
        pid = 987654
        returncode = rc
        attempts = 0

        def communicate(self, timeout: float | None = None) -> tuple[bytes, bytes]:
            self.attempts += 1
            if timeout is not None and self.attempts == 1 and timeout_enabled:
                raise subprocess.TimeoutExpired("fake bwrap", timeout)
            return b"", b"bwrap: Creating new namespace failed: Operation not permitted"

    timeout_enabled = timeout

    def popen(args: list[str], **kwargs: Any) -> FakeProcess:
        calls.append((args, kwargs))
        return FakeProcess()

    monkeypatch.setattr(bw.subprocess, "Popen", popen)
    return calls


def test_probe_actually_launches_true_with_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_probe(monkeypatch)
    status = BubblewrapWorker().probe(SandboxConfig())
    assert status.available
    args, kwargs = calls[0]
    assert args[-1] == "/usr/bin/true"
    assert "--unshare-net" in args and "--size" in args
    assert kwargs["env"] == {} and kwargs["start_new_session"] is True
    assert kwargs["close_fds"] is True and callable(kwargs["preexec_fn"])


def test_probe_userns_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_probe(monkeypatch, rc=1)
    status = BubblewrapWorker().probe(SandboxConfig())
    assert not status.available
    assert "Operation not permitted" in status.reason
    assert "user namespaces" in status.reason and "AppArmor" in status.reason


def test_probe_timeout_kills_group(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_probe(monkeypatch, timeout=True)
    killed: list[int] = []
    monkeypatch.setattr(bw, "_kill_group", killed.append)
    status = BubblewrapWorker().probe(SandboxConfig())
    assert not status.available and "timed out" in status.reason
    assert killed == [987654]


def test_probe_missing_binary_and_nonlinux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bw.shutil, "which", lambda name: None)
    status = BubblewrapWorker().probe(SandboxConfig())
    assert not status.available and "missing" in status.reason
    monkeypatch.setattr(bw.sys, "platform", "darwin")
    status = BubblewrapWorker().probe(SandboxConfig())
    assert not status.available and "Linux-only" in status.reason


def test_seccomp_hook_is_explicit_and_invalid_filters_fail_closed(tmp_path: Path) -> None:
    bpf = tmp_path / "filter.bpf"
    bpf.write_bytes(b"\x06\x00\x00\x00\x00\x00\xff\x7f")  # cBPF return ALLOW
    config = SandboxConfig(bubblewrap_seccomp_bpf_path=str(bpf))
    with contextlib.ExitStack() as stack:
        fd = bw._seccomp_fd(stack, config)
        assert fd is not None
        assert os.read(fd, 8) == bpf.read_bytes()
        args = bw.build_bubblewrap_args(config, "/fake/bwrap", seccomp_fd=fd)
        assert args[args.index("--seccomp") + 1] == str(fd)
    bpf.write_bytes(b"invalid")
    with contextlib.ExitStack() as stack, pytest.raises(CapabilityUnavailableError, match="cBPF"):
        bw._seccomp_fd(stack, config)


def test_builder_rejects_host_home_as_runtime() -> None:
    with pytest.raises(CapabilityUnavailableError, match="dedicated runtime"):
        bw.build_bubblewrap_args(
            SandboxConfig(bubblewrap_runtime_path=str(Path.home())),
            "/fake/bwrap",
        )


async def test_launch_failure_is_not_reported_as_executed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(BubblewrapWorker, "_binary", lambda *args: "/fake/bwrap")
    monkeypatch.setattr(
        bw,
        "build_bubblewrap_args",
        lambda *args, **kwargs: [sys.executable, "-I", "-c", "import sys; sys.exit(1)"],
    )
    worker = BubblewrapWorker()
    with pytest.raises(CapabilityUnavailableError, match="before the interpreter started"):
        await worker.execute(ExecutionRequest(code="unused"), SandboxConfig())
    assert worker.active_worker_count == 0


def test_selection_opt_in_and_never_above_stronger_backend() -> None:
    assert "bubblewrap" not in DEFAULT_BACKEND_ORDER
    assert isinstance(build_backend("bubblewrap"), BubblewrapWorker)
    config = sandbox_config_from_env(
        {
            "NEXURA_SANDBOX_BACKENDS": "bubblewrap,gvisor_oci",
            "NEXURA_SANDBOX_BWRAP_PYTHON": "/usr/bin/python3",
            "NEXURA_SANDBOX_BWRAP_RUNTIME": "/opt/runtime",
        }
    )
    assert config and config.backends == ["bubblewrap", "gvisor_oci"]
    assert config.bubblewrap_runtime_path == "/opt/runtime"
    bwrap = SimpleNamespace(
        backend_name="bubblewrap",
        isolation="os_sandbox",
        probe=lambda config: BackendStatus("bubblewrap", True),
    )
    strong = SimpleNamespace(
        backend_name="gvisor_oci",
        isolation="gvisor",
        probe=lambda config: BackendStatus("gvisor_oci", True),
    )
    assert select_backend(config, [bwrap, strong]) is strong
    assert select_backend(config, [bwrap]) is bwrap
    with pytest.raises(CapabilityUnavailableError, match="below the operator minimum"):
        select_backend(SandboxConfig(min_isolation="gvisor"), [bwrap])


@pytest.fixture
def fake_transport(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Replace bwrap with fixed trusted Python for host-side lifecycle checks only."""
    monkeypatch.setattr(BubblewrapWorker, "_binary", lambda *args: "/fake/bwrap")

    def install(code: str) -> None:
        def fake_args(*args: Any, **kwargs: Any) -> list[str]:
            marker = kwargs["start_marker"]
            return [sys.executable, "-I", "-u", "-c", f"import os; os.write(1, {marker!r}); {code}"]

        monkeypatch.setattr(
            bw,
            "build_bubblewrap_args",
            fake_args,
        )

    return install


async def test_fake_transport_success_stdin_and_no_stderr_classification(
    fake_transport: Any,
) -> None:
    fake_transport(
        "import sys; print(sys.stdin.read()); "
        "sys.stderr.write('SECURITY_VIOLATION_NETWORK permission denied'); sys.exit(1)"
    )
    worker = BubblewrapWorker()
    result = await worker.execute(ExecutionRequest(code="unused", stdin="INPUT"), SandboxConfig())
    assert result.exit_code == 1 and result.stdout == "INPUT\n"
    assert result.details["seccomp"] is False
    assert worker.active_worker_count == 0


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
async def test_fake_transport_output_limit(fake_transport: Any, stream: str) -> None:
    fake_transport(f"import sys\nwhile True: sys.{stream}.write('x' * 4096)")
    worker = BubblewrapWorker()
    with pytest.raises(OutputLimitExceededError):
        await worker.execute(ExecutionRequest(code="unused"), SandboxConfig(max_output_bytes=1024))
    assert worker.active_worker_count == 0


async def test_fake_transport_wall_timeout_and_cancellation(fake_transport: Any) -> None:
    fake_transport("import time; time.sleep(30)")
    worker = BubblewrapWorker()
    with pytest.raises(ExecutionTimeoutError):
        await worker.execute(ExecutionRequest(code="unused"), SandboxConfig(timeout_seconds=0.1))
    assert worker.active_worker_count == 0
    task = asyncio.create_task(worker.execute(ExecutionRequest(code="unused"), SandboxConfig()))
    try:
        for _ in range(100):
            if worker.active_worker_count:
                break
            await asyncio.sleep(0.01)
        assert worker.active_worker_count == 1
        pid = next(iter(worker._active_pids))
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert worker.active_worker_count == 0
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


async def test_fake_transport_cpu_limit(fake_transport: Any) -> None:
    fake_transport("\nwhile True: pass")
    worker = BubblewrapWorker()
    with pytest.raises(ExecutionTimeoutError):
        await worker.execute(
            ExecutionRequest(code="unused"),
            SandboxConfig(timeout_seconds=5, bubblewrap_cpu_seconds=1),
        )
    assert worker.active_worker_count == 0
