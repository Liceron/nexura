"""Final audit S1–S4: offline contracts plus opt-in, self-contained host repros.

No imports from .autopilot or other test modules. Host workloads are the audit's
original bounded workloads; the gVisor case needs NEXURA_REAL_GVISOR=1.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import json
import os
import platform
import selectors
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import pytest
from nexura_sandbox import BubblewrapWorker
from nexura_sandbox import _lifecycle as life
from nexura_sandbox import bubblewrap_worker as bw

from nexura_runtime.integrations.sandbox import (
    ExecutionRequest,
    ExecutionTimeoutError,
    MontyWorker,
    SandboxConfig,
    sandbox_config_from_env,
)
from nexura_runtime.integrations.sandbox.monty_worker import _WorkerLaunch


# Host cases run wherever bubblewrap works (_bwrap_config probes it), so the release gate
# host runs them instead of skipping; there is no manual switch.
def HOST(fn: Any) -> Any:  # noqa: N802 - decorator name kept for readability
    return fn


GVISOR = pytest.mark.skipif(os.environ.get("NEXURA_REAL_GVISOR") != "1", reason="gVisor host only")
ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("backends", ["bubblewrap", "monty,bubblewrap", "gvisor_oci,bubblewrap"])
def test_s1_import_sandbox_first_does_no_probe(backends: str) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import subprocess; "
            "subprocess.Popen = lambda *a, **k: (_ for _ in ()).throw(AssertionError('probe')); "
            "import nexura_sandbox; from nexura_runtime.modules.registry import default_registry; "
            "assert default_registry.has('nexura.tools.sandbox.execute'); print('ok')",
        ],
        env={**os.environ, "NEXURA_SANDBOX_BACKENDS": backends},
        capture_output=True,
        text=True,
        timeout=10,
        cwd="/",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


@pytest.mark.parametrize("inodes", [None, 128])
def test_s3_s4_masks_and_payload_limits(inodes: int | None) -> None:
    config = SandboxConfig(bubblewrap_tmpfs_inodes=inodes, bubblewrap_max_open_files=64)
    with contextlib.ExitStack() as stack:
        args = bw._hardened_args(stack, config, "/fake/bwrap")
        if inodes is not None:
            launcher_fd = int(args[args.index("/nexura-internal/launcher.py") - 1])
            # Read the trusted launcher descriptor from its ro-bind-data entry.
            launcher = os.pread(launcher_fd, 65536, 0).decode()
            assert "--nofile=64:64" in launcher
            assert "'/proc/keys'" in launcher and "'/proc/key-users'" in launcher
        else:
            assert "--nofile=64:64" in args
            for path in ("/proc/keys", "/proc/key-users"):
                index = args.index(path)
                assert args[index - 2 : index] == ["--ro-bind", "/dev/null"]
        index = args.index("/dev/pts/ptmx")
        assert args[index - 2 : index] == ["--ro-bind", "/dev/null"]


# Crash tests start a fresh interpreter that imports the runtime; under a loaded release-gate
# run that can take seconds. These bound how long cleanup may take, not whether it happens.
_STARTUP_SECONDS = 30
_CLEANUP_SECONDS = 30


def _readable(stream: Any, timeout: float) -> bool:
    """Wait for a pipe to become readable; selectors (unlike select()) accept fds >= 1024."""
    with selectors.DefaultSelector() as sel:
        sel.register(stream, selectors.EVENT_READ)
        return bool(sel.select(timeout))


def test_s4_nofile_operator_cannot_raise_cap() -> None:
    assert SandboxConfig().bubblewrap_max_open_files == 128
    with pytest.raises(ValueError):
        SandboxConfig(bubblewrap_max_open_files=129)
    config = sandbox_config_from_env(
        {"NEXURA_SANDBOX_BACKENDS": "bubblewrap", "NEXURA_SANDBOX_BWRAP_MAX_OPEN_FILES": "64"}
    )
    assert config is not None and config.bubblewrap_max_open_files == 64
    with pytest.raises(ValueError):
        sandbox_config_from_env(
            {"NEXURA_SANDBOX_BACKENDS": "bubblewrap", "NEXURA_SANDBOX_BWRAP_MAX_OPEN_FILES": "129"}
        )


@pytest.mark.parametrize("cap", [16, 64, 128])
def test_s4_payload_prlimit_sets_both_kernel_limits(cap: int) -> None:
    # Run only the trusted payload launcher suffix, with fixed inspection code.
    # No bwrap, namespace, sandbox input, sockets or terminal allocation is used.
    args = bw.build_bubblewrap_args(
        SandboxConfig(bubblewrap_max_open_files=cap),
        "/fake/bwrap",
        command=[
            sys.executable,
            "-I",
            "-c",
            "import resource; print(*resource.getrlimit(resource.RLIMIT_NOFILE))",
        ],
    )
    result = subprocess.run(
        args[args.index("/usr/bin/prlimit") :], capture_output=True, text=True, timeout=3
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"{cap} {cap}"


def test_s2_delegated_cgroup_attachment_and_kill(tmp_path: Path, monkeypatch: Any) -> None:
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(bw, "_preexec_limits", lambda config: lambda: calls.append((1, 1)))
    bw._run_limits(SandboxConfig(), tmp_path)()
    assert (tmp_path / "cgroup.procs").read_text() == str(os.getpid())
    assert calls == [(1, 1)]
    monkeypatch.setattr(Path, "rmdir", lambda path: None)
    life.kill_cgroup(tmp_path)
    assert (tmp_path / "cgroup.kill").read_text() == "1"


@pytest.mark.parametrize("delegated", [True, False])
def test_s2_cgroup_is_under_service_delegation(monkeypatch: Any, delegated: bool) -> None:
    created: list[Path] = []
    monkeypatch.setattr(Path, "read_text", lambda p: "0::/service.slice/nexura.service\n")
    monkeypatch.setattr(Path, "is_file", lambda p: True)
    monkeypatch.setattr(Path, "exists", lambda p: True)

    def mkdir(path: Path, **kwargs: Any) -> None:
        if not delegated:
            raise PermissionError("no cgroup delegation")
        created.append(path)

    monkeypatch.setattr(Path, "mkdir", mkdir)
    cgroup = life.run_cgroup()
    if delegated:
        assert cgroup is not None
        assert cgroup.parent == Path("/sys/fs/cgroup/service.slice/nexura.service")
        assert created == [cgroup]
    else:
        assert cgroup is None


def test_s2_watchdog_constructor_failure_closes_fds(monkeypatch: Any) -> None:
    before = set(os.listdir("/proc/self/fd"))

    def fail(*args: Any, **kwargs: Any) -> None:
        raise OSError("spawn denied")

    monkeypatch.setattr(life.subprocess, "Popen", fail)
    with pytest.raises(OSError, match="spawn denied"):
        life.RunGuard()
    assert set(os.listdir("/proc/self/fd")) == before


async def test_s2_service_retries_cgroup_cleanup_if_watchdog_fails(
    tmp_path: Path, monkeypatch: Any
) -> None:
    cgroup = tmp_path / "fake-cgroup"
    cgroup.mkdir()
    killed: list[Path] = []

    class FailedGuard:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def close(self) -> None:
            raise OSError("fake watchdog failure")

    def kill(path: Path) -> None:
        killed.append(path)
        (path / "cgroup.procs").unlink()
        path.rmdir()

    def args(*args: Any, **kwargs: Any) -> list[str]:
        return [
            sys.executable,
            "-I",
            "-c",
            f"import os; os.write(1, {kwargs['start_marker']!r})",
        ]

    monkeypatch.setattr(bw, "run_cgroup", lambda: cgroup)
    monkeypatch.setattr(bw, "kill_cgroup", kill)
    monkeypatch.setattr(bw, "RunGuard", FailedGuard)
    monkeypatch.setattr(bw._NamespaceInit, "capture", lambda self: True)
    monkeypatch.setattr(bw, "_hardened_args", args)
    monkeypatch.setattr(BubblewrapWorker, "_binary", lambda *args: "/fake/bwrap")
    worker = BubblewrapWorker()
    with pytest.raises(bw.CapabilityUnavailableError, match="watchdog failure"):
        await worker.execute(ExecutionRequest(code="# never executed"), SandboxConfig())
    assert killed == [cgroup]
    assert worker.active_worker_count == 0


@pytest.mark.parametrize("failure", ["timeout", "output"])
def test_s2_cleanup_commands_are_bounded_and_reaped(failure: str) -> None:
    code = "import time; time.sleep(15)" if failure == "timeout" else "print('x' * 2**21)"
    before = set(os.listdir("/proc/self/fd"))
    started = time.monotonic()
    error = subprocess.TimeoutExpired if failure == "timeout" else OSError
    with pytest.raises(error):
        life.command([sys.executable, "-I", "-c", code], timeout=0.5)
    assert time.monotonic() - started < 3
    assert set(os.listdir("/proc/self/fd")) == before


def test_s2_gvisor_crash_watchdog_deletes_task(tmp_path: Path) -> None:
    life.enable_subreaper()
    task = tmp_path / "nexura_gvisor_fixed_fake"
    task.mkdir(mode=0o700)
    evidence = tmp_path / "delete-called"
    delete_script = (
        f"from pathlib import Path; import sys; "
        f"Path({str(evidence)!r}).write_text(' '.join(sys.argv[1:]))"
    )
    service = subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-u",
            "-c",
            "import sys, time\nfrom pathlib import Path\n"
            "from nexura_sandbox._lifecycle import RunGuard\n"
            f"guard = RunGuard(task=Path({str(task)!r}), "
            f"runtime=[sys.executable, '-I', '-c', {delete_script!r}], container='fixed_fake')\n"
            "print(guard.proc.pid, flush=True)\ntime.sleep(15)\n",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    guard_pid: int | None = None
    try:
        assert service.stdout and _readable(service.stdout, _STARTUP_SECONDS)
        guard_pid = int(service.stdout.readline())
        service.kill()
        service.wait(timeout=2)
        deadline = time.monotonic() + _CLEANUP_SECONDS
        while time.monotonic() < deadline:
            if os.waitpid(guard_pid, os.WNOHANG)[0]:
                guard_pid = None
                break
            time.sleep(0.01)
        assert guard_pid is None, "crash watchdog did not finish"
        assert evidence.read_text() == "delete --force fixed_fake"
        assert not task.exists()
    finally:
        if service.poll() is None:
            service.kill()
        if guard_pid is not None:
            with contextlib.suppress(ChildProcessError):
                if os.waitpid(guard_pid, os.WNOHANG)[0] == 0:
                    os.kill(guard_pid, signal.SIGKILL)
                    os.waitpid(guard_pid, 0)
        service.communicate(timeout=2)


def test_s2_guard_kills_pinned_namespace_on_service_sigkill(tmp_path: Path) -> None:
    """Trusted sleeping stand-in for namespace init; never executes sandbox input."""
    life.enable_subreaper()  # Reap only the two known children adopted by this test.
    service = subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-u",
            "-c",
            "import os, subprocess, sys, json, time\n"
            "from nexura_sandbox._lifecycle import RunGuard\n"
            "fd = os.memfd_create('fake-namespace')\n"
            "child = subprocess.Popen([sys.executable, '-I', '-c', "
            "'import time; time.sleep(15)'])\n"
            "os.write(fd, json.dumps({'child-pid': child.pid}).encode())\n"
            "guard = RunGuard(info_fd=fd)\n"
            "while not guard.ready: time.sleep(.01)\n"
            "print(child.pid, guard.proc.pid, flush=True)\n"
            "time.sleep(15)\n",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(tmp_path),
    )
    adopted: list[int] = []
    try:
        assert service.stdout
        assert _readable(service.stdout, _STARTUP_SECONDS)
        adopted = [int(x) for x in service.stdout.readline().split()]
        assert len(adopted) == 2
        service.kill()
        service.wait(timeout=2)
        deadline = time.monotonic() + _CLEANUP_SECONDS
        pending = set(adopted)
        while pending and time.monotonic() < deadline:
            for pid in list(pending):
                if os.waitpid(pid, os.WNOHANG)[0]:
                    pending.remove(pid)
            time.sleep(0.01)
        assert not pending, f"watchdog or pinned init survived: {pending}"
    finally:
        if service.poll() is None:
            service.kill()
        for pid in adopted:
            with contextlib.suppress(ChildProcessError):
                if os.waitpid(pid, os.WNOHANG)[0] == 0:
                    os.kill(pid, signal.SIGKILL)
                    os.waitpid(pid, 0)
        service.communicate(timeout=2)


@pytest.mark.parametrize("state", ["absent", "live", "unknown", "other_owner", "live_service"])
def test_s2_gvisor_startup_sweep_only_owned_absent_containers(
    tmp_path: Path,
    monkeypatch: Any,
    state: str,
) -> None:
    task = tmp_path / "nexura_gvisor_stale"
    task.mkdir(mode=0o700)
    container = "nex_" + "a" * 32
    life.mark_gvisor_task(task, "service-a", container)
    owner_path = task / "owner.json"
    owner = json.loads(owner_path.read_text())
    if state != "live_service":
        owner["start"] = "not-this-process"
    if state == "other_owner":
        owner["identity"] = "service-b"
    owner_path.write_text(json.dumps(owner))
    calls: list[list[str]] = []
    real_command = life.command

    def command(args: list[str], timeout: float = 3) -> tuple[int, bytes]:
        calls.append(args)
        if args[0] == "/usr/bin/rm":
            return real_command(args, timeout=timeout)
        if "list" in args:
            if state == "unknown":
                raise subprocess.TimeoutExpired(args, timeout)
            return 0, json.dumps([{"id": container}] if state == "live" else []).encode()
        return 1, b"already absent"

    monkeypatch.setattr(life, "command", command)
    life.sweep_gvisor_tasks("/operator/runsc", [], "service-a", tmp_path)
    assert task.exists() is (state != "absent")
    if state == "absent":
        assert any("delete" in args and "--force" in args for args in calls)
    else:
        assert not any("delete" in args for args in calls)


def test_s2_sweep_leaves_unmarked_symlink_and_mounted_tasks(
    tmp_path: Path, monkeypatch: Any
) -> None:
    task = tmp_path / "nexura_gvisor_unmarked"
    task.mkdir(mode=0o700)
    (tmp_path / "nexura_gvisor_link").symlink_to(task, target_is_directory=True)
    monkeypatch.setattr(life, "command", lambda *a, **k: pytest.fail("unknown owner"))
    life.sweep_gvisor_tasks("/runsc", [], "service", tmp_path)
    assert task.exists()
    monkeypatch.setattr(life, "task_mounts", lambda path: [str(path / "unexpected-mount")])
    with pytest.raises(OSError, match="mounts remain"):
        life.remove_gvisor_task(task, [])
    assert task.exists()


@pytest.mark.parametrize(
    "failure", ["success", "exec_error", "setup_error", "cancel", "drain_error", "guard_error"]
)
def test_monty_launch_temp_cleanup_all_exit_paths(
    tmp_path: Path, monkeypatch: Any, failure: str
) -> None:
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    launch = _WorkerLaunch(Path("/bin/true") if failure != "exec_error" else tmp_path / "missing")
    try:
        if failure in ("setup_error", "cancel", "guard_error"):

            def fail(*args: Any, **kwargs: Any) -> None:
                if failure == "cancel":
                    raise asyncio.CancelledError
                raise OSError("setup failed")

            if failure == "guard_error":
                monkeypatch.setattr(subprocess, "Popen", fail)
            else:
                monkeypatch.setattr(os, "mkfifo", fail)
            if failure == "cancel":
                with pytest.raises(asyncio.CancelledError):
                    launch.start()
            else:
                assert launch.start() == "/bin/true"
        else:
            wrapper = launch.start()
            assert launch.mode == "wrapped"
            result = subprocess.run([wrapper], env={}, capture_output=True, timeout=3)
            assert (result.returncode == 0) is (failure != "exec_error")
            # The pool may respawn its worker through the same wrapper until close.
            assert list(tmp_path.glob("nexura-monty-*"))
            if failure == "drain_error":

                def fail_drain() -> None:
                    raise OSError("stderr drain failed")

                monkeypatch.setattr(launch, "drain", fail_drain)
    finally:
        if failure == "drain_error":
            with pytest.raises(OSError, match="stderr drain failed"):
                launch.close()
        else:
            launch.close()
    assert not list(tmp_path.glob("nexura-monty-*"))


def test_monty_service_sigkill_reclaims_temp_dir(tmp_path: Path) -> None:
    life.enable_subreaper()
    service = subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-u",
            "-c",
            "import tempfile, time\nfrom pathlib import Path\n"
            "from nexura_runtime.integrations.sandbox.monty_worker import _WorkerLaunch\n"
            f"tempfile.tempdir = {str(tmp_path)!r}\n"
            "launch = _WorkerLaunch(Path('/bin/true'))\nlaunch.start()\n"
            "print(launch._cleanup_process.pid, flush=True)\ntime.sleep(15)\n",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    guardian: int | None = None
    try:
        assert service.stdout and _readable(service.stdout, _STARTUP_SECONDS)
        guardian = int(service.stdout.readline())
        assert list(tmp_path.glob("nexura-monty-*"))
        service.kill()
        service.wait(timeout=2)
        deadline = time.monotonic() + _CLEANUP_SECONDS
        while time.monotonic() < deadline:
            if os.waitpid(guardian, os.WNOHANG)[0]:
                guardian = None
                break
            time.sleep(0.01)
        assert guardian is None
        assert not list(tmp_path.glob("nexura-monty-*"))
    finally:
        if service.poll() is None:
            service.kill()
        if guardian is not None:
            with contextlib.suppress(ChildProcessError):
                if os.waitpid(guardian, os.WNOHANG)[0] == 0:
                    os.kill(guardian, signal.SIGKILL)
                    os.waitpid(guardian, 0)
        service.communicate(timeout=2)


@pytest.mark.parametrize("outcome", ["success", "error", "enter_error", "timeout", "cancel"])
async def test_monty_backend_reclaims_launch_dir(
    tmp_path: Path, monkeypatch: Any, outcome: str
) -> None:
    """Fake native pool, real launch directory and guardian; no sandbox input runs."""
    import pydantic_monty

    from nexura_runtime.integrations.sandbox import monty_worker

    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    feeding = asyncio.Event()

    class FakePool:
        worker_pid = 0  # Never used for a signal or resource operation.

        def __init__(self, **kwargs: Any) -> None:
            assert Path(kwargs["binary_path"]).is_file()

        async def __aenter__(self) -> FakePool:
            if outcome == "enter_error":
                raise RuntimeError("fake pool setup failure")
            return self

        async def __aexit__(self, *args: Any) -> None:
            pass

        def checkout(self, **kwargs: Any) -> FakePool:
            return self

        async def feed_run(self, *args: Any, **kwargs: Any) -> None:
            feeding.set()
            if outcome == "error":
                raise RuntimeError("fake pool execution failure")
            if outcome in ("timeout", "cancel"):
                await asyncio.Event().wait()

    monkeypatch.setattr(pydantic_monty, "AsyncMonty", FakePool)
    monkeypatch.setattr(monty_worker, "_apply_worker_rlimits", lambda *args: {})
    worker = MontyWorker()
    config = SandboxConfig(timeout_seconds=0.05 if outcome == "timeout" else 5)
    status = worker.probe(config)
    assert status.available, status.reason
    task = asyncio.create_task(worker.execute(ExecutionRequest(code="# fake pool input"), config))
    try:
        if outcome == "cancel":
            await asyncio.wait_for(feeding.wait(), timeout=3)
            assert list(tmp_path.glob("nexura-monty-*")), "Monty pool did not start"
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        elif outcome == "timeout":
            with pytest.raises(ExecutionTimeoutError):
                await asyncio.wait_for(task, timeout=5)
        elif outcome in ("error", "enter_error"):
            with pytest.raises(RuntimeError, match="fake pool"):
                await asyncio.wait_for(task, timeout=5)
        else:
            result = await asyncio.wait_for(task, timeout=5)
            assert result.exit_code == 0
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert worker.active_worker_count == 0
    assert not list(tmp_path.glob("nexura-monty-*"))


def _bwrap_config(**kwargs: Any) -> SandboxConfig:
    config = SandboxConfig(backends=["bubblewrap"], **kwargs)
    status = BubblewrapWorker().probe(config)
    assert status.available, status.reason  # Explicit host profile must really execute.
    return config


def _payload_pids(tag: bytes) -> set[int]:
    found = set()
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            with contextlib.suppress(OSError):
                cmd = (entry / "cmdline").read_bytes()
                if tag in cmd and b"bwrap" not in cmd:
                    found.add(int(entry.name))
    return found


@HOST
def test_s2_bwrap_payload_cannot_outlive_service() -> None:
    _bwrap_config()
    tag = b"runpy.run_path('/tmp/code.py'"
    before = _payload_pids(tag)
    code = "import ctypes, time\nctypes.CDLL(None).prctl(1, 0, 0, 0, 0)\ntime.sleep(10)\n"
    with subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-c",
            "import asyncio; from nexura_sandbox import BubblewrapWorker; "
            "from nexura_runtime.integrations.sandbox import ExecutionRequest, SandboxConfig; "
            f"asyncio.run(BubblewrapWorker().execute(ExecutionRequest(code={code!r}), "
            "SandboxConfig(backends=['bubblewrap'], timeout_seconds=20)))",
        ],
        cwd="/",
    ) as service:
        try:
            deadline = time.monotonic() + 10
            while not (_payload_pids(tag) - before) and time.monotonic() < deadline:
                time.sleep(0.1)
            assert _payload_pids(tag) - before, "payload never started"
            time.sleep(0.5)
            service.kill()
            service.wait(timeout=3)
            time.sleep(0.5)
            assert not (_payload_pids(tag) - before)
        finally:
            if service.poll() is None:
                service.kill()
                service.wait(timeout=3)
            for pid in _payload_pids(tag) - before:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)


@HOST
def test_s3_host_key_descriptions_hidden() -> None:
    config = _bwrap_config(timeout_seconds=5)
    add, keyctl = (248, 250) if platform.machine() == "x86_64" else (217, 219)
    libc = ctypes.CDLL(None, use_errno=True)
    assert libc.syscall(keyctl, 1, None) > 0
    description = f"nexura-audit-{uuid.uuid4().hex[:12]}".encode()
    key = libc.syscall(add, b"user", description, b"s3cret", 6, -3)
    assert key > 0
    try:
        result = asyncio.run(
            BubblewrapWorker().execute(
                ExecutionRequest(code="print(open('/proc/keys').read())"), config
            )
        )
        # /proc/keys is either empty or unreadable inside the sandbox; the key never shows.
        if result.exit_code != 0:
            assert "PermissionError" in result.stderr or "No such file" in result.stderr
        assert description.decode() not in result.stdout
        assert description.decode() not in result.stderr
    finally:
        libc.syscall(keyctl, 3, key)


SOCKET_CODE = """
import resource, socket
held, pairs = 0, []
try:
    while held < 96 * 2**20:
        a, b = socket.socketpair()
        a.setblocking(False)
        try:
            while True:
                held += a.send(b"x" * 65536)
        except BlockingIOError:
            pass
        pairs.append((a, b))
except OSError as exc:
    print("stopped:", exc)
print(held, len(pairs), resource.getrlimit(resource.RLIMIT_NOFILE)[0])
"""


@HOST
def test_s4_socket_memory_is_bounded_by_nofile() -> None:
    config = _bwrap_config(timeout_seconds=10, max_memory_mb=32)
    result = asyncio.run(BubblewrapWorker().execute(ExecutionRequest(code=SOCKET_CODE), config))
    held, pairs, nofile = map(int, result.stdout.split()[-3:])
    assert held <= 2 * 32 * 2**20, (held, pairs, nofile)
    assert nofile == 128


@GVISOR
def test_s2_gvisor_service_crash_removes_task_and_processes() -> None:
    data_path = ROOT / ".agy/review-infrastructure.json"
    data = json.loads(data_path.read_text()) if data_path.is_file() else {}
    info = data.get("sandbox") or data
    runsc = os.environ.get("NEXURA_RUNSC_PATH") or info.get("runsc_path")
    rootfs = os.environ.get("NEXURA_OCI_ROOTFS_PATH") or info.get("rootfs_path")
    assert runsc and rootfs, "lead must configure explicit runsc/rootfs"
    prefix = info.get("runsc_command_prefix") or data.get("runsc_command_prefix") or []
    config = SandboxConfig(
        runsc_path=runsc,
        rootfs_path=rootfs,
        timeout_seconds=30,
        extra={"allow_without_cgroups": True, "runsc_command_prefix": prefix},
    )
    before = set(Path("/tmp").glob("nexura_gvisor_*"))
    service = subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-c",
            "import asyncio; from nexura_sandbox import GVisorOCIWorker; "
            "from nexura_runtime.integrations.sandbox import ExecutionRequest, SandboxConfig; "
            "asyncio.run(GVisorOCIWorker().execute("
            "ExecutionRequest(code='import time; time.sleep(10)'), "
            f"SandboxConfig.model_validate_json({config.model_dump_json()!r})))",
        ],
        cwd="/",
    )
    created: set[Path] = set()
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            created = set(Path("/tmp").glob("nexura_gvisor_*")) - before
            if created and any(_payload_pids(str(task).encode()) for task in created):
                break
            time.sleep(0.1)
        assert created, "gVisor never started"
        time.sleep(3)
        service.kill()
        service.wait(timeout=3)
        time.sleep(1)
        assert not any(_payload_pids(str(task).encode()) for task in created)
        assert not any(task.exists() for task in created)
    finally:
        if service.poll() is None:
            service.kill()
            service.wait(timeout=3)
        for task in created:
            if task.exists():
                owner = json.loads((task / "owner.json").read_text())
                life.command(
                    [
                        *prefix,
                        runsc,
                        f"--root={task / 'state'}",
                        "delete",
                        "--force",
                        owner["container"],
                    ]
                )
                life.remove_gvisor_task(task, prefix)
