"""S2 regression: high descriptors and scoped orphan reaping, all offline by default.

Fixed Python stand-ins below test host lifecycle only, never sandbox isolation.
The two parametrized backend cases run on the lead's provisioned host and skip
only when the backend's own probe reports it unavailable.
"""

from __future__ import annotations

import contextlib
import ctypes
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Literal

import pytest
from nexura_sandbox import BubblewrapWorker, GVisorOCIWorker
from nexura_sandbox import _lifecycle as life
from nexura_sandbox import bubblewrap_worker as bw
from nexura_sandbox.gvisor_worker import _bounded_cleanup_command

from nexura_runtime.integrations.sandbox import ExecutionRequest, SandboxConfig


@contextlib.contextmanager
def high_descriptors() -> Iterator[list[int]]:
    fds: list[int] = []
    try:
        for _ in range(1152):
            fds.append(os.open("/dev/null", os.O_RDONLY | os.O_CLOEXEC))
        assert min(fds[-32:]) > 1100
        # close_fds must also exclude explicitly inheritable unrelated handles.
        os.set_inheritable(fds[0], True)
        yield fds
    finally:
        for fd in fds:
            os.close(fd)


def subreaper() -> int:
    enabled = ctypes.c_int()
    assert ctypes.CDLL(None, use_errno=True).prctl(37, ctypes.byref(enabled), 0, 0, 0) == 0
    return enabled.value


def children() -> set[int]:
    # Include asyncio's threaded child watcher threads, if present.
    found: set[int] = set()
    for task in Path("/proc/self/task").iterdir():
        with contextlib.suppress(FileNotFoundError):
            found.update(map(int, (task / "children").read_text().split()))
    return found


@pytest.mark.parametrize("mode", ["success", "timeout", "output"])
def test_cleanup_high_descriptors_are_bounded(mode: str) -> None:
    code = {
        "success": "print('local fake cleanup')",
        "timeout": "import time; time.sleep(20)",
        "output": "print('x' * 2**21)",
    }[mode]
    before = children()
    with high_descriptors():
        started = time.monotonic()
        if mode == "success":
            assert _bounded_cleanup_command([sys.executable, "-I", "-c", code]) == (
                0,
                b"local fake cleanup\n",
            )
        else:
            error = subprocess.TimeoutExpired if mode == "timeout" else OSError
            with pytest.raises(error):
                _bounded_cleanup_command([sys.executable, "-I", "-c", code], timeout=0.3)
        assert time.monotonic() - started < 3
    assert children() == before


def test_watchdog_high_descriptors_and_no_unrelated_inheritance() -> None:
    before = set(os.listdir("/proc/self/fd"))
    with (
        high_descriptors(),
        subprocess.Popen(
            [sys.executable, "-I", "-c", "import time; time.sleep(20)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        ) as child,
    ):
        fd = os.memfd_create("local-fake-init", os.MFD_CLOEXEC)
        guard = None
        try:
            os.write(fd, json.dumps({"child-pid": child.pid}).encode())
            guard = life.RunGuard(info_fd=fd)
            assert guard.ready_fd > 1100
            deadline = time.monotonic() + 3
            while not guard.ready:
                assert time.monotonic() < deadline, "watchdog did not pin fake init"
                time.sleep(0.01)
            assert guard.proc is not None
            assert len(list(Path(f"/proc/{guard.proc.pid}/fd").iterdir())) < 16
            guard.close()
            assert child.poll() is None  # Explicit normal teardown does not kill fake init.
        finally:
            try:
                if guard is not None:
                    guard.close()
            finally:
                os.close(fd)
                child.kill()
                child.wait(timeout=2)
    assert set(os.listdir("/proc/self/fd")) == before


async def test_repeated_fake_bwrap_runs_reap_detached_orphans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Five real process trees; detached fake payloads clear PDEATHSIG themselves."""
    before, setting = children(), subreaper()
    pids: list[int] = []
    monkeypatch.setattr(BubblewrapWorker, "_binary", lambda *args: "/fake/bwrap")
    monkeypatch.setattr(bw, "run_cgroup", lambda: None)
    worker = BubblewrapWorker()
    try:
        for index in range(5):
            ready = tmp_path / f"orphan-{index}"
            orphan = (
                "import ctypes, os, time; from pathlib import Path; "
                "os.setsid(); ctypes.CDLL(None).prctl(1, 0, 0, 0, 0); "
                f"Path({str(ready)!r}).write_text(str(os.getpid())); time.sleep(20)"
            )

            def args(
                *args: Any, orphan: str = orphan, ready: Path = ready, **kwargs: Any
            ) -> list[str]:
                return [
                    SandboxConfig().bubblewrap_python_path,
                    "-I",
                    "-c",
                    "import os, subprocess, sys, time; from pathlib import Path\n"
                    f"os.write(1, {kwargs['start_marker']!r})\n"
                    "import resource\n"
                    "assert resource.getrlimit(resource.RLIMIT_AS) == (32 * 2**20,) * 2\n"
                    f"subprocess.Popen([sys.executable, '-I', '-c', {orphan!r}], "
                    "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, "
                    "stderr=subprocess.DEVNULL, close_fds=True)\n"
                    "deadline = time.monotonic() + 2\n"
                    f"while not Path({str(ready)!r}).exists():\n"
                    "    assert time.monotonic() < deadline\n"
                    "    time.sleep(.01)\n",
                ]

            monkeypatch.setattr(bw, "_hardened_args", args)
            result = await worker.execute(
                ExecutionRequest(code="# fixed fake"), SandboxConfig(max_memory_mb=32)
            )
            assert result.exit_code == 0, result.stderr
            pids.append(int(ready.read_text()))
            assert all(not Path(f"/proc/{pid}").exists() for pid in pids)
            assert children() == before
            assert subreaper() == setting
            assert worker.active_worker_count == 0
    finally:
        for ready in tmp_path.glob("orphan-*"):
            pid = int(ready.read_text())
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
            with contextlib.suppress(ChildProcessError):
                os.waitpid(pid, 0)


@pytest.mark.parametrize("with_prefix", [False, True])
async def test_repeated_fake_gvisor_runs_reap_detached_orphans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, with_prefix: bool
) -> None:
    """A fixed fake runsc never executes the request; it mimics detached gofers."""
    before, setting = children(), subreaper()
    ready = tmp_path / "orphan"
    orphan = (
        "import ctypes, os, time; from pathlib import Path; "
        "os.setsid(); ctypes.CDLL(None).prctl(1, 0, 0, 0, 0); "
        f"Path({str(ready)!r}).write_text(str(os.getpid())); time.sleep(20)"
    )
    runtime = tmp_path / "local_fake_runsc"
    runtime.write_text(
        f"#!{sys.executable}\n"
        "import json, os, re, subprocess, sys, time\nfrom pathlib import Path\n"
        "if 'run' in sys.argv:\n"
        "    bundle = next(a.split('=', 1)[1] for a in sys.argv if a.startswith('--bundle='))\n"
        "    spec = json.loads((Path(bundle) / 'config.json').read_text())\n"
        "    marker = re.search(r'__nexura_sandbox_started_[a-f0-9]+__', "
        "spec['process']['args'][-1])[0]\n"
        f"    subprocess.Popen([sys.executable, '-I', '-c', {orphan!r}], "
        "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, "
        "stderr=subprocess.DEVNULL, close_fds=True)\n"
        "    deadline = time.monotonic() + 2\n"
        f"    while not Path({str(ready)!r}).exists():\n"
        "        assert time.monotonic() < deadline\n        time.sleep(.01)\n"
        "    print(marker, flush=True)\n"
        "    print(len(list(Path('/proc/self/fd').iterdir())), flush=True)\n"
        "elif 'list' in sys.argv:\n    print('[]')\n"
    )
    runtime.chmod(0o700)
    rootfs = tmp_path / "rootfs"
    rootfs.mkdir()
    extra: dict[str, Any] = {}
    if with_prefix:
        monkeypatch.setenv("NEXURA_REAL_GVISOR", "1")
        # A local fake prefix models sudo's descriptor closure, without privilege.
        extra["runsc_command_prefix"] = [
            sys.executable,
            "-I",
            "-c",
            "import os, sys; os.closerange(3, 65536); os.execv(sys.argv[1], sys.argv[1:])",
        ]
    config = SandboxConfig(runsc_path=str(runtime), rootfs_path=str(rootfs), extra=extra)
    worker = GVisorOCIWorker()
    try:
        with high_descriptors():
            for _ in range(5):
                result = await worker.execute(ExecutionRequest(code="# fixed fake"), config)
                assert result.exit_code == 0, result.stderr
                assert int(result.stdout) < 16
                assert not Path(f"/proc/{int(ready.read_text())}").exists()
                ready.unlink()
                assert children() == before
                assert subreaper() == setting
                assert worker.active_worker_count == 0
    finally:
        if ready.exists():
            pid = int(ready.read_text())
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
            with contextlib.suppress(ChildProcessError):
                os.waitpid(pid, 0)


def host_backend(name: Literal["bubblewrap", "gvisor_oci"]) -> tuple[Any, SandboxConfig]:
    if name == "bubblewrap":
        return BubblewrapWorker(), SandboxConfig(backends=[name], timeout_seconds=15)
    record = Path(__file__).resolve().parents[2] / ".agy/review-infrastructure.json"
    data = json.loads(record.read_text()) if record.is_file() else {}
    info = data.get("sandbox") or data
    return GVisorOCIWorker(), SandboxConfig(
        backends=[name],
        timeout_seconds=20,
        runsc_path=os.environ.get("NEXURA_RUNSC_PATH") or info.get("runsc_path"),
        rootfs_path=os.environ.get("NEXURA_OCI_ROOTFS_PATH") or info.get("rootfs_path"),
        extra={
            "allow_without_cgroups": True,
            "runsc_command_prefix": info.get("runsc_command_prefix")
            or data.get("runsc_command_prefix")
            or [],
        },
    )


@pytest.mark.parametrize("name", ["bubblewrap", "gvisor_oci"])
async def test_real_backend_executes_with_over_1100_open_fds(
    name: Literal["bubblewrap", "gvisor_oci"],
) -> None:
    with high_descriptors():
        worker, config = host_backend(name)
        status = worker.probe(config)
        if not status.available:
            pytest.skip(status.reason)
        result = await worker.execute(ExecutionRequest(code="print(6 * 7)"), config)
        assert result.exit_code == 0, result.stderr
        assert result.stdout == "42\n"
        assert worker.active_worker_count == 0


@pytest.mark.parametrize("name", ["bubblewrap", "gvisor_oci"])
async def test_real_backend_leaves_no_adopted_children_after_five_runs(
    name: Literal["bubblewrap", "gvisor_oci"],
) -> None:
    worker, config = host_backend(name)
    status = worker.probe(config)
    if not status.available:
        pytest.skip(status.reason)
    before, setting = children(), subreaper()
    for _ in range(5):
        result = await worker.execute(ExecutionRequest(code="print('done')"), config)
        assert result.exit_code == 0, result.stderr
        assert result.stdout == "done\n"
        assert children() == before
        assert subreaper() == setting
        assert worker.active_worker_count == 0
