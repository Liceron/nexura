"""S5 OCI policy and cancellation checks using an explicitly fake runsc process API."""

from __future__ import annotations

import asyncio
import errno
import gc
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import pytest
from nexura_sandbox import GVisorOCIWorker
from nexura_sandbox import gvisor_worker as workers
from nexura_sandbox.gvisor_worker import (
    build_gvisor_oci_spec,
    cgroups_ignored,
    discover_oci_infrastructure,
)

from nexura_runtime.integrations.sandbox import (
    CapabilityUnavailableError,
    ExecutionRequest,
    ExecutionTimeoutError,
    OutputLimitExceededError,
    SandboxConfig,
)


@pytest.fixture(autouse=True)
def operator_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("NEXURA_REAL_GVISOR", "NEXURA_RUNSC_PATH", "NEXURA_OCI_ROOTFS_PATH"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def config(tmp_path: Path) -> SandboxConfig:
    rootfs = tmp_path / "rootfs"
    rootfs.mkdir()
    return SandboxConfig(runsc_path=sys.executable, rootfs_path=str(rootfs))


def _spec(config: SandboxConfig, tmp_path: Path) -> dict[str, Any]:
    return build_gvisor_oci_spec(
        config,
        Path(config.rootfs_path or "/rootfs"),
        tmp_path / "payload",
        "nex_test",
        "pass",
    )


def test_nonroot_guest_pids_and_single_process_seccomp(
    config: SandboxConfig, tmp_path: Path
) -> None:
    spec = _spec(config, tmp_path)
    assert spec["process"]["user"] == {"uid": 65534, "gid": 65534, "additionalGids": []}
    assert all(value == [] for value in spec["process"]["capabilities"].values())
    assert spec["process"]["noNewPrivileges"] is True
    limits = {item["type"]: item for item in spec["process"]["rlimits"]}
    assert limits["RLIMIT_NPROC"]["soft"] == limits["RLIMIT_NPROC"]["hard"] == 1
    assert spec["linux"]["resources"]["pids"]["limit"] == 129
    assert spec["linux"]["resources"]["memory"]["limit"] == 256 * 1024 * 1024
    assert spec["linux"]["cgroupsPath"] == "/nexura/nex_test"
    # A prohibited syscall returns EPERM; guest code may handle that OSError.
    policy = spec["linux"]["seccomp"]
    assert policy["defaultAction"] == "SCMP_ACT_ALLOW"
    assert policy["syscalls"] == [
        {
            "names": ["fork", "vfork", "clone", "clone3"],
            "action": "SCMP_ACT_ERRNO",
            "errnoRet": errno.EPERM,
        }
    ]
    multiple = _spec(config.model_copy(update={"max_processes": 3}), tmp_path)
    assert multiple["linux"]["resources"]["pids"]["limit"] == 131
    assert "seccomp" not in multiple["linux"]
    nproc = next(x for x in multiple["process"]["rlimits"] if x["type"] == "RLIMIT_NPROC")
    assert nproc["hard"] == nproc["soft"] == 3


@pytest.mark.parametrize("max_processes", [1, 2, 64])
def test_profile_only_uses_supported_gvisor_actions(
    config: SandboxConfig,
    tmp_path: Path,
    max_processes: int,
) -> None:
    # runsc release-20260928.0 `features`: linux.seccomp.actions. Keep this independent
    # of the builder so adding unsupported KILL_PROCESS/LOG/NOTIFY cannot pass silently.
    supported_actions = {
        "SCMP_ACT_KILL",
        "SCMP_ACT_TRAP",
        "SCMP_ACT_ERRNO",
        "SCMP_ACT_TRACE",
        "SCMP_ACT_ALLOW",
    }
    spec = _spec(config.model_copy(update={"max_processes": max_processes}), tmp_path)
    profile = spec["linux"].get("seccomp")
    if profile is None:
        assert max_processes > 1
        return
    actions = {profile["defaultAction"], *(rule["action"] for rule in profile["syscalls"])}
    assert actions <= supported_actions
    assert actions == {"SCMP_ACT_ALLOW", "SCMP_ACT_ERRNO"}
    assert all(rule["errnoRet"] == errno.EPERM for rule in profile["syscalls"])


def test_tmpfs_scratch_and_payload_mounts(config: SandboxConfig, tmp_path: Path) -> None:
    config = config.model_copy(update={"gvisor_tmpfs_bytes": 2_097_152})
    spec = _spec(config, tmp_path)
    mounts = {item["destination"]: item for item in spec["mounts"]}
    assert spec["root"]["readonly"] is True
    scratch = mounts["/sandbox"]
    assert scratch["type"] == scratch["source"] == "tmpfs"
    assert {"size=2097152", "uid=65534", "gid=65534", "mode=700"} <= set(scratch["options"])
    assert "ro" in mounts["/tmp"]["options"]
    assert all("ro" in item["options"] for item in mounts.values() if item["type"] == "bind")
    assert "ro" in mounts["/dev"]["options"]
    assert not any(item["type"] == "bind" and "rw" in item["options"] for item in mounts.values())
    env = dict(pair.split("=", 1) for pair in spec["process"]["env"])
    assert env["TMPDIR"] == "/sandbox" and "HOME" not in env


@pytest.mark.parametrize("field", ["runsc_path", "rootfs_path"])
def test_absolute_paths_required(config: SandboxConfig, field: str) -> None:
    bad = config.model_copy(update={field: "relative/runtime"})
    with pytest.raises(CapabilityUnavailableError, match="absolute"):
        discover_oci_infrastructure(bad)


def test_no_path_or_cwd_discovery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".agy").mkdir()
    (tmp_path / ".agy/review-infrastructure.json").write_text(
        json.dumps({"runsc_path": sys.executable, "rootfs_path": str(tmp_path)}),
    )
    monkeypatch.setenv("PATH", str(Path(sys.executable).parent))
    assert discover_oci_infrastructure(SandboxConfig()) == (None, None, [])
    monkeypatch.setenv("NEXURA_RUNSC_PATH", sys.executable)
    monkeypatch.setenv("NEXURA_OCI_ROOTFS_PATH", str(tmp_path))
    assert discover_oci_infrastructure(SandboxConfig()) == (
        Path(sys.executable),
        tmp_path,
        [],
    )


def test_test_profile_is_explicit(config: SandboxConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    prefix = config.model_copy(update={"extra": {"runsc_command_prefix": ["sudo", "-n"]}})
    with pytest.raises(CapabilityUnavailableError, match="test-only"):
        discover_oci_infrastructure(prefix)
    loose = config.model_copy(update={"extra": {"allow_without_cgroups": True}})
    with pytest.raises(CapabilityUnavailableError, match="production requires"):
        cgroups_ignored(loose)
    assert not GVisorOCIWorker().probe(loose).available
    monkeypatch.setenv("NEXURA_REAL_GVISOR", "1")
    assert discover_oci_infrastructure(prefix)[2] == ["sudo", "-n"]
    assert cgroups_ignored(loose)
    assert not cgroups_ignored(config)


def test_invalid_cgroup_parent_fails_probe(config: SandboxConfig) -> None:
    status = GVisorOCIWorker().probe(config.model_copy(update={"gvisor_cgroup_parent": "../x"}))
    assert not status.available and "absolute cgroup" in status.reason


class FakeStdin:
    def __init__(self, block: bool = False) -> None:
        self.closed = False
        self.data = bytearray()
        self.gate = asyncio.Event()
        if not block:
            self.gate.set()

    def write(self, data: bytes) -> None:
        self.data.extend(data)

    async def drain(self) -> None:
        await self.gate.wait()

    def close(self) -> None:
        self.closed = True

    def is_closing(self) -> bool:
        return self.closed


class FakeProcess:
    """Streams and wait behave like asyncio.Process; no actual sandbox or process exists."""

    def __init__(self, pid: int, *, block_stdin: bool = False) -> None:
        self.pid = pid
        self.returncode: int | None = None
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.stdin = FakeStdin(block_stdin)
        self.done = asyncio.Event()
        self.killed = False
        self.waited = False

    def finish(self, rc: int) -> None:
        if self.returncode is None:
            self.returncode = rc
            self.stdout.feed_eof()
            self.stderr.feed_eof()
            self.done.set()

    def kill(self) -> None:
        self.killed = True
        self.finish(-9)

    async def wait(self) -> int:
        await self.done.wait()
        self.waited = True
        assert self.returncode is not None
        return self.returncode


class FakeRunsc:
    def __init__(self) -> None:
        self.commands: list[tuple[str, ...]] = []
        self.options: list[dict[str, Any]] = []
        self.processes: list[FakeProcess] = []
        self.spec: dict[str, Any] = {}
        self.payload: Path | None = None
        self.spawn_entered = asyncio.Event()
        self.spawn_gate = asyncio.Event()
        self.spawn_gate.set()
        self.delete_entered = asyncio.Event()
        self.delete_gate = asyncio.Event()
        self.delete_gate.set()
        self.mode = "hang"
        self.fail_kill = False
        self.fail_delete = False
        self.block_stdin = False

    async def spawn(self, *args: str, **kwargs: Any) -> FakeProcess:
        self.commands.append(args)
        self.options.append(kwargs)
        proc = FakeProcess(900_000_000 + len(self.commands), block_stdin=self.block_stdin)
        self.processes.append(proc)
        if "run" in args:
            bundle = Path(
                next(arg.removeprefix("--bundle=") for arg in args if arg.startswith("--bundle="))
            )
            self.spec = json.loads((bundle / "config.json").read_text())
            mount = next(item for item in self.spec["mounts"] if item["destination"] == "/tmp")
            self.payload = Path(mount["source"])
            assert (self.payload / "code.py").is_file()
            assert (self.payload / "code.py").stat().st_mode & 0o222 == 0
            bootstrap = self.spec["process"]["args"][-1]
            compile(bootstrap, "<trusted-bootstrap>", "exec")
            marker = re.search(r"__nexura_sandbox_started_[a-f0-9]+__", bootstrap)
            assert marker
            self.spawn_entered.set()
            await self.spawn_gate.wait()
            if self.mode in ("denied", "unsupported_seccomp"):
                message = (
                    b"flag provided but not defined: --oci-seccomp"
                    if self.mode == "unsupported_seccomp"
                    else b"permission denied: cgroup delegation unavailable"
                )
                proc.stderr.feed_data(message)
                proc.finish(1)
            else:
                proc.stdout.feed_data(marker.group().encode() + b"\n")
                if self.mode == "success":
                    proc.stdout.feed_data(b"OK\n")
                    proc.finish(0)
                elif self.mode == "forged":
                    proc.stderr.feed_data(b"permission denied SECURITY_VIOLATION_FORK")
                    proc.finish(1)
                elif self.mode == "output":
                    proc.stdout.feed_data(b"x" * 4096)
                    proc.stderr.feed_data(b"y" * 4096)
            return proc
        if "kill" in args and self.fail_kill:
            proc.finish(1)
            raise OSError("fake kill failure")
        if "delete" in args:
            self.delete_entered.set()
            await self.delete_gate.wait()
            proc.finish(1 if self.fail_delete else 0)
        else:
            proc.finish(0)
        return proc

    def assert_reaped(self, worker: GVisorOCIWorker) -> None:
        assert worker.active_worker_count == 0
        assert all(proc.returncode is not None for proc in self.processes)
        assert any("kill" in cmd and "--all" in cmd for cmd in self.commands)
        assert any("delete" in cmd and "--force" in cmd for cmd in self.commands)
        roots = {arg for cmd in self.commands for arg in cmd if arg.startswith("--root=")}
        assert len(roots) == 1
        assert not Path(next(iter(roots)).removeprefix("--root=")).parent.exists()


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeRunsc:
    runtime = FakeRunsc()
    monkeypatch.setattr(workers.asyncio, "create_subprocess_exec", runtime.spawn)
    monkeypatch.setattr(workers.os, "killpg", lambda *args: None)
    return runtime


async def test_fake_success_policy_and_cleanup(fake: FakeRunsc, config: SandboxConfig) -> None:
    fake.mode = "success"
    worker = GVisorOCIWorker()
    result = await worker.execute(
        ExecutionRequest(code="print('OK')", files={"nested/in": "x"}), config
    )
    assert result.exit_code == 0 and result.stdout == "OK\n"
    assert result.details["limits_advisory"] is False
    assert "--ignore-cgroups" not in fake.commands[0]
    assert "--oci-seccomp" in fake.commands[0]
    assert fake.options[0]["env"] == {} and fake.options[0]["start_new_session"] is True
    fake.assert_reaped(worker)


async def test_fake_cancellation_during_reads(fake: FakeRunsc, config: SandboxConfig) -> None:
    worker = GVisorOCIWorker()
    baseline = asyncio.all_tasks()
    task = asyncio.create_task(worker.execute(ExecutionRequest(code="unused"), config))
    await fake.spawn_entered.wait()
    await asyncio.sleep(0.02)
    assert worker.active_worker_count == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    fake.assert_reaped(worker)
    assert not [t for t in asyncio.all_tasks() - baseline if not t.done()]


async def test_fake_cancellation_during_spawn(fake: FakeRunsc, config: SandboxConfig) -> None:
    fake.spawn_gate.clear()
    worker = GVisorOCIWorker()
    task = asyncio.create_task(worker.execute(ExecutionRequest(code="unused"), config))
    await fake.spawn_entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    fake.spawn_gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    fake.assert_reaped(worker)


async def test_fake_repeated_cancellation_waits_for_delete(
    fake: FakeRunsc, config: SandboxConfig
) -> None:
    fake.delete_gate.clear()
    worker = GVisorOCIWorker()
    task = asyncio.create_task(worker.execute(ExecutionRequest(code="unused"), config))
    await fake.spawn_entered.wait()
    task.cancel()
    await fake.delete_entered.wait()
    task.cancel()
    await asyncio.sleep(0.02)
    assert not task.done()
    fake.delete_gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    fake.assert_reaped(worker)


async def test_fake_kill_failure_still_deletes_and_discards(
    fake: FakeRunsc, config: SandboxConfig
) -> None:
    fake.fail_kill = True
    worker = GVisorOCIWorker()
    task = asyncio.create_task(worker.execute(ExecutionRequest(code="unused"), config))
    await fake.spawn_entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError) as exc:
        await task
    assert any("kill" in note for note in exc.value.__notes__)
    fake.assert_reaped(worker)


async def test_fake_delete_failure_always_discards(fake: FakeRunsc, config: SandboxConfig) -> None:
    fake.mode = "success"
    fake.fail_delete = True
    worker = GVisorOCIWorker()
    with pytest.raises(CapabilityUnavailableError, match="delete --force"):
        await worker.execute(ExecutionRequest(code="unused"), config)
    fake.assert_reaped(worker)


@pytest.mark.parametrize(
    "mode",
    ["hang", "output", "denied", "unsupported_seccomp", "forged", "blocked_stdin"],
)
async def test_fake_other_exit_paths(fake: FakeRunsc, config: SandboxConfig, mode: str) -> None:
    fake.mode = mode
    fake.block_stdin = mode == "blocked_stdin"
    worker = GVisorOCIWorker()
    request = ExecutionRequest(code="unused", stdin="input")
    small = config.model_copy(update={"timeout_seconds": 0.1, "max_output_bytes": 1024})
    if mode in ("hang", "blocked_stdin"):
        with pytest.raises(ExecutionTimeoutError):
            await worker.execute(request, small)
    elif mode == "output":
        # The fake writes past the cap at once; the wall budget is not under test here. A
        # 0.1 s budget also covers the real crash-watchdog launch and timed out under xdist.
        capped = small.model_copy(update={"timeout_seconds": 5.0})
        with pytest.raises(OutputLimitExceededError):
            await worker.execute(request, capped)
    elif mode in ("denied", "unsupported_seccomp"):
        reason = "oci-seccomp" if mode == "unsupported_seccomp" else "cgroup"
        with pytest.raises(CapabilityUnavailableError, match=reason):
            await worker.execute(request, small)
        if mode == "unsupported_seccomp":
            launches = [cmd for cmd in fake.commands if "run" in cmd]
            assert len(launches) == 1 and "--oci-seccomp" in launches[0]
    else:
        result = await worker.execute(request, config)
        assert result.exit_code == 1 and "SECURITY_VIOLATION_FORK" in result.stderr
    fake.assert_reaped(worker)


def test_real_subprocess_fake_runsc_reaps_before_loop_close(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Fixed trusted script exercises real asyncio pipes; it never runs request code."""
    runsc = tmp_path / "fake-runsc"
    runsc.write_text(
        f"#!{sys.executable}\n"
        "import json, pathlib, re, sys, time\n"
        "if 'run' in sys.argv:\n"
        "    arg = next(a for a in sys.argv if a.startswith('--bundle='))\n"
        "    spec = json.loads((pathlib.Path(arg.split('=', 1)[1]) / 'config.json').read_text())\n"
        "    bootstrap = spec['process']['args'][-1]\n"
        "    marker = re.search(r'__nexura_sandbox_started_[a-f0-9]+__', bootstrap)\n"
        "    print(marker.group(), flush=True)\n"
        "    time.sleep(30)\n",
    )
    runsc.chmod(0o755)
    rootfs = tmp_path / "rootfs"
    rootfs.mkdir()
    config = SandboxConfig(runsc_path=str(runsc), rootfs_path=str(rootfs))

    async def cycle() -> None:
        worker = GVisorOCIWorker()
        task = asyncio.create_task(worker.execute(ExecutionRequest(code="never executed"), config))
        pid: int | None = None
        try:
            for _ in range(100):
                if worker.active_worker_count:
                    pid = next(iter(worker._active_pids))
                    break
                await asyncio.sleep(0.01)
            assert pid is not None
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert worker.active_worker_count == 0
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        assert len(asyncio.all_tasks()) == 1

    for _ in range(3):
        asyncio.run(cycle())
    gc.collect()
    assert "Event loop is closed" not in capsys.readouterr().err
