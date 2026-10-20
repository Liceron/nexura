"""P31 R1–R8 regressions and offline policy/lifecycle checks.

Real backends require NEXURA_REVIEW_HOST=1 (gVisor also NEXURA_REAL_GVISOR=1).
No backend is probed or launched by the default offline run. Original review
payloads are reproduced here with their own helpers; no .autopilot imports.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import errno
import json
import os
import platform
import signal
import struct
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from nexura_sandbox import BubblewrapWorker, GVisorOCIWorker
from nexura_sandbox import bubblewrap_worker as bw
from nexura_sandbox import gvisor_worker as worker_mod
from nexura_sandbox.bubblewrap_policy import default_bpf, setup_launcher
from nexura_sandbox.bubblewrap_worker import build_bubblewrap_args

from nexura_runtime.integrations.sandbox import (
    SandboxConfig,
    SandboxExecutionModule,
    assert_sandbox_allowed_for_profile,
)
from nexura_runtime.integrations.sandbox.errors import (
    CapabilityUnavailableError,
    ExecutionTimeoutError,
    SandboxError,
)
from nexura_runtime.integrations.sandbox.models import ExecutionRequest


# Host cases run wherever the backend itself is available (each one probes it); the release
# gate host has bubblewrap and the gVisor test profile, so they run there rather than skip.
def HOST(fn: Any) -> Any:  # noqa: N802 - kept as a decorator name for readability
    return fn


PAYLOAD_TAG = b"runpy.run_path('/tmp/code.py'"


def bwrap_config(**kw: object) -> SandboxConfig:
    config = SandboxConfig(backends=["bubblewrap"], **{"timeout_seconds": 5.0, **kw})
    status = BubblewrapWorker().probe(config)
    if not status.available:
        pytest.skip(f"bubblewrap unavailable here: {status.reason}")
    return config


def sandbox_payload_pids() -> set[int]:
    """Host PIDs of bubblewrap payload interpreters (identified by the worker's bootstrap)."""
    found: set[int] = set()
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/cmdline", "rb") as fh:
                cmd = fh.read()
        except OSError:
            continue
        if PAYLOAD_TAG in cmd and b"bwrap" not in cmd:
            found.add(int(name))
    return found


def kill_pids(pids: set[int]) -> None:
    for pid in pids:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)


def _operator_gvisor_config(**overrides: Any) -> SandboxConfig:
    data_path = Path(__file__).resolve().parents[2] / ".agy/review-infrastructure.json"
    data = json.loads(data_path.read_text()) if data_path.is_file() else {}
    info = data.get("sandbox") or data
    runsc = os.environ.get("NEXURA_RUNSC_PATH") or info.get("runsc_path")
    rootfs = os.environ.get("NEXURA_OCI_ROOTFS_PATH") or info.get("rootfs_path")
    if not runsc or not rootfs or not Path(runsc).is_file() or not Path(rootfs).is_dir():
        pytest.skip("lead must configure explicit runsc/rootfs paths")
    extra = {"allow_without_cgroups": True}
    prefix = info.get("runsc_command_prefix") or data.get("runsc_command_prefix")
    if prefix:
        extra["runsc_command_prefix"] = prefix
    return SandboxConfig(runsc_path=runsc, rootfs_path=rootfs, extra=extra, **overrides)


def _b30() -> Any:
    return SimpleNamespace(
        live_gvisor_available=lambda: True, operator_gvisor_config=_operator_gvisor_config
    )


CLEAR_PDEATHSIG = (
    "import ctypes, os, time\nctypes.CDLL(None).prctl(1, 0, 0, 0, 0)  # PR_SET_PDEATHSIG = 0\n"
)


def _run(code: str, timeout: float) -> float:
    config = bwrap_config(timeout_seconds=timeout)
    start = time.monotonic()
    with contextlib.suppress(ExecutionTimeoutError):
        asyncio.run(BubblewrapWorker().execute(ExecutionRequest(code=code), config))
    return time.monotonic() - start


@HOST
def test_r1_detached_payload_survives() -> None:
    before = sandbox_payload_pids()
    leaked: set[int] = set()
    try:
        _run(CLEAR_PDEATHSIG + "os.close(0); os.close(1); os.close(2)\ntime.sleep(6)\n", 1.5)
        time.sleep(0.3)
        leaked = sandbox_payload_pids() - before
        assert not leaked, f"sandbox payload outlived teardown: host pids {sorted(leaked)}"
    finally:
        kill_pids(leaked | sandbox_payload_pids() - before)


@HOST
def test_r1_payload_holding_stdout_blocks_execute() -> None:
    before = sandbox_payload_pids()
    try:
        elapsed = _run(CLEAR_PDEATHSIG + "print('x', flush=True)\ntime.sleep(6)\n", 1.5)
        assert elapsed < 4.0, f"execute took {elapsed:.1f}s despite timeout_seconds=1.5"
    finally:
        kill_pids(sandbox_payload_pids() - before)


SYS_ADD_KEY, SYS_KEYCTL = (248, 250)

KEYCTL_JOIN_SESSION_KEYRING, KEYCTL_REVOKE, KEY_SPEC_SESSION_KEYRING = (1, 3, -3)

SANDBOX_CODE = """
import ctypes
libc = ctypes.CDLL(None, use_errno=True)
kid = libc.syscall(250, 10, -3, b"user", b"nexura_review_probe", 0)   # KEYCTL_SEARCH
buf = ctypes.create_string_buffer(256)
n = libc.syscall(250, 11, kid, buf, 256)                             # KEYCTL_READ
print(buf.raw[:max(n, 0)].decode(errors="replace"))
"""


@HOST
@pytest.mark.skipif(platform.machine() != "x86_64", reason="syscall numbers are x86_64")
def test_r2_host_session_key_is_not_readable_in_bwrap() -> None:
    config = bwrap_config()
    libc = ctypes.CDLL(None, use_errno=True)
    assert libc.syscall(SYS_KEYCTL, KEYCTL_JOIN_SESSION_KEYRING, None) > 0
    secret = f"HOST_KEY_SECRET_{uuid.uuid4().hex}".encode()
    kid = libc.syscall(
        SYS_ADD_KEY, b"user", b"nexura_review_probe", secret, len(secret), KEY_SPEC_SESSION_KEYRING
    )
    assert kid > 0
    try:
        result = asyncio.run(
            BubblewrapWorker().execute(ExecutionRequest(code=SANDBOX_CODE), config)
        )
        assert secret.decode() not in result.stdout, "host session-keyring secret read in bwrap"
    finally:
        libc.syscall(SYS_KEYCTL, KEYCTL_REVOKE, kid)


R3_CODE = """
import os
chunk = b"x" * (1 << 20)
fds = []
for i in range(8):
    fd = os.memfd_create(f"m{i}")
    for _ in range(16):
        os.write(fd, chunk)
    fds.append(fd)
print(sum(os.fstat(fd).st_size for fd in fds) >> 20)
"""


@HOST
def test_r3_memfd_cannot_exceed_memory_limit() -> None:
    config = bwrap_config(max_memory_mb=32)
    result = asyncio.run(BubblewrapWorker().execute(ExecutionRequest(code=R3_CODE), config))
    held_mb = int(result.stdout.strip() or 0) if result.exit_code == 0 else 0
    assert held_mb <= 32, f"sandbox held {held_mb} MiB with max_memory_mb=32"


R4_CODE = """
import ctypes
libc = ctypes.CDLL(None, use_errno=True)
params = (ctypes.c_uint32 * 30)()
print(libc.syscall(425, 4, ctypes.byref(params)))   # io_uring_setup(4, &params)
"""


@HOST
@pytest.mark.skipif(platform.machine() != "x86_64", reason="syscall number is x86_64")
def test_r4_default_profile_denies_io_uring() -> None:
    config = bwrap_config()
    assert config.bubblewrap_seccomp_bpf_path is None
    result = asyncio.run(BubblewrapWorker().execute(ExecutionRequest(code=R4_CODE), config))
    fd = int(result.stdout.strip())
    assert fd < 0, f"io_uring_setup returned fd {fd} in the default bubblewrap sandbox"


R5_CODE = "import time\nt = time.time()\nwhile time.time() - t < 4.0:\n    pass\nprint('done')\n"


@HOST
def test_r5_cpu_limit_is_reported_as_timeout() -> None:
    config = bwrap_config(timeout_seconds=8.0, bubblewrap_cpu_seconds=1)
    with pytest.raises(ExecutionTimeoutError):
        result = asyncio.run(BubblewrapWorker().execute(ExecutionRequest(code=R5_CODE), config))
        pytest.fail(f"CPU-limit kill returned as a completed run: exit_code={result.exit_code}")


@HOST
def test_r3_tmpfs_inode_cap() -> None:
    base = SandboxConfig(backends=["bubblewrap"], timeout_seconds=5.0)
    if not BubblewrapWorker().probe(base).available:
        pytest.skip("bubblewrap unavailable here")
    config = base.model_copy(update={"bubblewrap_tmpfs_inodes": 128})
    status = BubblewrapWorker().probe(config)
    if not status.available:
        # Hosts that restrict unprivileged user namespaces (the Ubuntu default) cannot set
        # nr_inodes: the opt-in must then fail closed with that reason, never run uncapped.
        assert "inode" in (status.reason or "")
        return
    result = asyncio.run(
        BubblewrapWorker().execute(
            ExecutionRequest(
                code="""
import errno, os
assert os.statvfs('/tmp').f_files <= 128
for i in range(256):
    try:
        open(f'/tmp/work/inode_{i}', 'w').close()
    except OSError as exc:
        assert exc.errno == errno.ENOSPC
        print('inodes capped')
        break
else:
    raise AssertionError('inode cap absent')
"""
            ),
            config,
        )
    )
    assert result.exit_code == 0 and result.stdout.strip() == "inodes capped"


@HOST
def test_r5_exact_payload_cpu_budget() -> None:
    config = bwrap_config(timeout_seconds=8, bubblewrap_cpu_seconds=1)
    result = asyncio.run(
        BubblewrapWorker().execute(
            ExecutionRequest(
                code="""
import resource
assert resource.getrlimit(resource.RLIMIT_CPU) == (1, 1)
print('exact CPU budget')
"""
            ),
            config,
        )
    )
    assert result.exit_code == 0 and result.stdout.strip() == "exact CPU budget"


def _mounts_under(path: str) -> list[str]:
    with open("/proc/self/mountinfo", encoding="utf-8") as fh:
        return [line.split()[4] for line in fh if line.split()[4].startswith(path + "/")]


@HOST
@pytest.mark.skipif(os.environ.get("NEXURA_REAL_GVISOR") != "1", reason="real gVisor profile")
def test_r6_gvisor_run_leaves_no_mount_or_task_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    helper = _b30()
    if not helper.live_gvisor_available():
        pytest.skip("runsc + rootfs not installed")
    config = helper.operator_gvisor_config(timeout_seconds=8.0)
    made: list[str] = []
    real_mkdtemp = tempfile.mkdtemp

    def recording_mkdtemp(*args: object, **kwargs: object) -> str:
        path = real_mkdtemp(*args, **kwargs)
        made.append(path)
        return path

    monkeypatch.setattr(worker_mod.tempfile, "mkdtemp", recording_mkdtemp)
    try:
        result = asyncio.run(
            GVisorOCIWorker().execute(ExecutionRequest(code="print('ok')"), config)
        )
        assert result.stdout.strip() == "ok"
        assert made, "worker did not create a task directory"
        leaked_mounts = _mounts_under(made[0])
        assert not leaked_mounts, f"host mounts leaked: {leaked_mounts}"
        assert not Path(made[0]).exists(), f"task directory leaked: {made[0]}"
    finally:
        prefix = list(config.extra.get("runsc_command_prefix") or [])
        for path in made:
            if not Path(path).name.startswith("nexura_gvisor_"):
                continue
            for mount in reversed(_mounts_under(path)):
                subprocess.run([*prefix, "umount", mount], check=False, timeout=10)
            if Path(path).exists():
                subprocess.run([*prefix, "rm", "-rf", "--", path], check=False, timeout=10)


class _Registry:
    def __init__(self, module: SandboxExecutionModule) -> None:
        self._handlers = {"sandbox": module}


def test_r7_shared_profile_refuses_gvisor_test_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXURA_REAL_GVISOR", "1")
    config = SandboxConfig(
        min_isolation="gvisor",
        runsc_path="/opt/gvisor/runsc",
        rootfs_path="/opt/gvisor/rootfs",
        extra={"allow_without_cgroups": True, "runsc_command_prefix": ["sudo", "-n"]},
    )
    module = SandboxExecutionModule(worker=GVisorOCIWorker(), config=config)
    with pytest.raises(SandboxError):
        assert_sandbox_allowed_for_profile("authenticated-shared-service", _Registry(module))


def test_r8_runtime_inside_home_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    secrets = home / ".ssh"
    secrets.mkdir(parents=True)
    (secrets / "id_ed25519").write_text("not a real key", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    config = SandboxConfig(
        backends=["bubblewrap"],
        bubblewrap_python_path="/usr/bin/python3",
        bubblewrap_runtime_path=str(secrets),
    )
    with pytest.raises(CapabilityUnavailableError):
        args = build_bubblewrap_args(config, "/usr/bin/bwrap", command=["/usr/bin/true"])
        pytest.fail(f"home credentials accepted: {args[args.index(str(secrets)) - 1 :][:3]}")


def _bpf_result(blob: bytes, nr: int, arch: int, family: int = 1) -> int:
    """Small independent interpreter for the generated cBPF instruction subset."""
    data = struct.pack("=IIQ6Q", nr, arch, 0, family, 0, 0, 0, 0, 0)
    program = list(struct.iter_unpack("=HBBI", blob))
    pc, acc = 0, 0
    while pc < len(program):
        op, jt, jf, value = program[pc]
        pc += 1
        if op == 0x20:
            acc = struct.unpack_from("=I", data, value)[0]
        elif op == 0x15:
            pc += jt if acc == value else jf
        elif op == 0x45:
            pc += jt if acc & value else jf
        elif op == 0x06:
            return value
        else:
            pytest.fail(f"unexpected BPF opcode {op}")
    pytest.fail("filter fell off its end")


@pytest.mark.parametrize(
    "machine,arch,denied,sockets,ordinary",
    [
        (
            "x86_64",
            0xC000003E,
            [101, 165, 166, 248, 249, 250, 272, 298, 308, 319, 321, 323, 29, 30, 31],
            [41, 53],
            [0, 1, 2, 9, 59, 60],
        ),
        (
            "aarch64",
            0xC00000B7,
            [117, 40, 39, 217, 218, 219, 97, 241, 268, 279, 280, 282, 194, 195, 196],
            [198, 199],
            [63, 64, 56, 222, 221, 93],
        ),
    ],
)
def test_offline_r2_r3_r4_mandatory_policy(
    machine: str, arch: int, denied: list[int], sockets: list[int], ordinary: list[int]
) -> None:
    blob = default_bpf(machine)
    for nr in [*denied, 425, 426, 427, 428, 429, 430, 431, 432, 433, 442, 447]:
        assert _bpf_result(blob, nr, arch) == 0x50000 | errno.EPERM
    for nr in sockets:
        assert _bpf_result(blob, nr, arch, 1) == 0x7FFF0000
        for family in (0, 2, 10, 16, 17, 40, 0xFFFFFFFF):
            assert _bpf_result(blob, nr, arch, family) == 0x50000 | errno.EPERM
    for nr in ordinary:
        assert _bpf_result(blob, nr, arch) == 0x7FFF0000
        assert _bpf_result(blob, nr, 0x40000003) == 0x50000 | errno.EPERM
    if machine == "x86_64":
        assert _bpf_result(blob, 0x40000000 + 41, arch) == 0x50000 | errno.EPERM
    with pytest.raises(ValueError, match="No default"):
        default_bpf("unknown")


@pytest.mark.skipif(
    sys.platform != "linux" or platform.machine() != "x86_64",
    reason="native Linux x86-64 filter-loading check",
)
def test_offline_kernel_accepts_native_filter() -> None:
    """Fixed child installs cBPF; no bwrap, namespaces, mounts or request code."""
    script = r"""
import ctypes, errno, sys
class Insn(ctypes.Structure):
    _fields_ = [('code', ctypes.c_ushort), ('jt', ctypes.c_ubyte),
                ('jf', ctypes.c_ubyte), ('k', ctypes.c_uint)]
class Program(ctypes.Structure):
    _fields_ = [('len', ctypes.c_ushort), ('filter', ctypes.POINTER(Insn))]
blob = sys.stdin.buffer.read()
insns = (Insn * (len(blob) // 8)).from_buffer_copy(blob)
program = Program(len(insns), insns)
libc = ctypes.CDLL(None, use_errno=True)
assert libc.prctl(38, 1, 0, 0, 0) == 0
assert libc.prctl(22, 2, ctypes.byref(program)) == 0
for nr in (248, 249, 250, 319, 425, 426, 427):
    ctypes.set_errno(0)
    assert libc.syscall(nr, 0, 0, 0, 0, 0, 0) == -1
    assert ctypes.get_errno() == errno.EPERM, (nr, ctypes.get_errno())
print('filter accepted; EPERM denials observed')
"""
    child = subprocess.run(
        [sys.executable, "-I", "-c", script],
        input=default_bpf(),
        capture_output=True,
        timeout=3,
        check=False,
    )
    assert child.returncode == 0, child.stderr.decode()
    assert child.stdout == b"filter accepted; EPERM denials observed\n"


def test_offline_r3_r4_r5_trusted_setup_contract(tmp_path: Path) -> None:
    """Inspect real generated descriptors, including a supplemental ALLOW-only policy."""
    import ast

    operator = tmp_path / "operator.bpf"
    operator.write_bytes(struct.pack("=HBBI", 6, 0, 0, 0x7FFF0000))
    config = SandboxConfig(
        bubblewrap_seccomp_bpf_path=str(operator),
        bubblewrap_cpu_seconds=2,
        bubblewrap_tmpfs_inodes=4096,
    )
    with contextlib.ExitStack() as stack:
        args = bw._hardened_args(stack, config, "/usr/bin/bwrap", start_marker=b"START\n")
        mounts = {
            args[i + 2]: int(args[i + 1])
            for i, arg in enumerate(args[:-2])
            if arg == "--ro-bind-data"
        }
        assert os.pread(mounts["/nexura-internal/default.bpf"], 32768, 0) == default_bpf()
        assert os.pread(mounts["/nexura-internal/operator.bpf"], 32768, 0) == operator.read_bytes()
        launcher = os.pread(mounts["/nexura-internal/launcher.py"], 32768, 0).decode()
        assert "nr_inodes=4096" in launcher and "size=33554432" in launcher
        tree = ast.parse(launcher)
        inner = next(
            ast.literal_eval(n.value)
            for n in tree.body
            if isinstance(n, ast.Assign)
            and isinstance(n.targets[0], ast.Name)
            and n.targets[0].id == "args"
        )
        assert inner[inner.index("--uid") + 1] == "65534"
        assert inner[inner.index("--cap-drop") + 1] == "ALL"
        assert "--disable-userns" in inner and "--as-pid-1" in inner
        assert "--cpu=2:2" in inner and "--nproc=1:1" in inner
        assert "--add-seccomp-fd" in launcher
        assert set(mounts.values()) <= set(bw._argument_fds(args))


def test_offline_inputs_cannot_shadow_trusted_setup() -> None:
    with contextlib.ExitStack() as stack:
        fd = bw._data_fd(stack, b"request-controlled bytes")
        args = bw._hardened_args(
            stack,
            SandboxConfig(bubblewrap_tmpfs_inodes=4096),
            "/usr/bin/bwrap",
            start_marker=b"START\n",
            input_fds=[
                (name, fd) for name in ("launcher.py", "default.bpf", "operator.bpf", "bwrap")
            ],
        )
        destinations = [
            args[i + 2] for i, arg in enumerate(args[:-2]) if arg in {"--ro-bind-data", "--ro-bind"}
        ]
        assert len(destinations) == len(set(destinations))
        assert "/nexura-internal/launcher.py" in destinations
        assert "/sandbox/launcher.py" in destinations


@pytest.mark.parametrize("mount_rc", [0, -1])
def test_offline_inode_setup_fails_closed(monkeypatch: pytest.MonkeyPatch, mount_rc: int) -> None:
    calls: list[Any] = []
    monkeypatch.setattr(
        ctypes,
        "CDLL",
        lambda *a, **k: SimpleNamespace(mount=lambda *a: calls.append(a) or mount_rc),
    )
    monkeypatch.setattr(os.path, "exists", lambda p: False)
    monkeypatch.setattr(os, "execv", lambda *a: calls.append(a))
    monkeypatch.setattr(sys, "argv", ["launcher.py"])
    code = compile(
        setup_launcher(["/nexura-internal/bwrap"], tmpfs_bytes=1048576, inodes=128),
        "<trusted-setup>",
        "exec",
    )
    if mount_rc:
        with pytest.raises(OSError, match="inode remount failed"):
            exec(code, {})
        assert len(calls) == 1
    else:
        exec(code, {})
        assert calls[0] == (None, b"/tmp", b"tmpfs", 38, b"size=1048576,nr_inodes=128")
        assert calls[1][0] == "/nexura-internal/bwrap"


def test_offline_r1_pidfd_pins_namespace_before_release(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[Any] = []
    monkeypatch.setattr(os, "pidfd_open", lambda pid: events.append(pid) or os.open("/dev/null", 0))
    monkeypatch.setattr(signal, "pidfd_send_signal", lambda *a: events.append(a))
    with contextlib.ExitStack() as stack:
        namespace = bw._NamespaceInit(stack)
        assert not namespace.capture()
        os.write(namespace.info_fd, b'{"child-pid": 987654321}')
        assert namespace.capture()
        assert os.read(namespace.block_fd, 1) == b"1"
        assert namespace.capture()  # No second open/release on later polls.
        namespace.kill()
        assert events == [987654321, (namespace.pidfd, signal.SIGKILL)]


@pytest.mark.parametrize("returncode", [137, 152])
async def test_offline_r5_encoded_signal_exits_are_limit_errors(
    monkeypatch: pytest.MonkeyPatch, returncode: int
) -> None:
    """Fixed fake launcher exits with bwrap's signal encoding; never runs request code."""
    monkeypatch.setattr(BubblewrapWorker, "_binary", lambda *a: "/fake/bwrap")

    def fake_args(*a: Any, **kw: Any) -> list[str]:
        return [
            sys.executable,
            "-I",
            "-c",
            f"import os; os.write(1, {kw['start_marker']!r}); raise SystemExit({returncode})",
        ]

    monkeypatch.setattr(bw, "_hardened_args", fake_args)
    with pytest.raises(ExecutionTimeoutError) as exc:
        await BubblewrapWorker().execute(
            ExecutionRequest(code="never executed"),
            SandboxConfig(timeout_seconds=8, bubblewrap_cpu_seconds=1),
        )
    assert exc.value.details["returncode"] == returncode
    assert exc.value.timeout_seconds == 1


@pytest.mark.parametrize("cancel", [False, True])
async def test_offline_r1_held_output_pipes_are_abandoned(
    monkeypatch: pytest.MonkeyPatch, cancel: bool
) -> None:
    """Fake launcher never closes its output; namespace kill precedes group kill."""
    events: list[str] = []
    spawned = asyncio.Event()
    done = asyncio.Event()
    stdout, stderr = asyncio.StreamReader(), asyncio.StreamReader()
    proc: Any = SimpleNamespace(pid=987654321, returncode=None, stdout=stdout, stderr=stderr)

    def close() -> None:
        events.append("pipes closed")
        stdout.feed_eof()
        stderr.feed_eof()
        proc.returncode = -9
        done.set()

    async def wait() -> int:
        await done.wait()
        return proc.returncode

    async def drain() -> None:
        return None

    proc.wait = wait
    proc.stdin = SimpleNamespace(write=lambda b: None, drain=drain, close=lambda: None)
    proc._transport = SimpleNamespace(close=close)

    async def spawn(*args: Any, **kwargs: Any) -> Any:
        spawned.set()
        return proc

    monkeypatch.setattr(bw.BubblewrapWorker, "_binary", lambda *a: "/fake/bwrap")
    monkeypatch.setattr(bw, "_hardened_args", lambda *a, **kw: ["/fake/bwrap"])
    monkeypatch.setattr(bw.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(bw._NamespaceInit, "capture", lambda s: True)
    monkeypatch.setattr(bw._NamespaceInit, "kill", lambda s: events.append("namespace killed"))
    monkeypatch.setattr(bw, "_kill_group", lambda pid: events.append("group killed"))
    worker = BubblewrapWorker()
    start = time.monotonic()
    task = asyncio.create_task(
        worker.execute(ExecutionRequest(code="unused"), SandboxConfig(timeout_seconds=0.1))
    )
    await spawned.wait()
    if cancel:
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else ExecutionTimeoutError):
        await asyncio.wait_for(task, 1)
    assert time.monotonic() - start < 1
    assert events == ["namespace killed", "group killed", "pipes closed"]
    assert worker.active_worker_count == 0


@pytest.mark.parametrize(
    "extra", [{"allow_without_cgroups": True}, {"runsc_command_prefix": ["sudo", "-n"]}]
)
def test_offline_r7_each_test_bypass_is_refused(extra: dict[str, Any]) -> None:
    module = SandboxExecutionModule(worker=GVisorOCIWorker(), config=SandboxConfig(extra=extra))
    with pytest.raises(SandboxError, match="test-only"):
        assert_sandbox_allowed_for_profile("authenticated-shared-service", _Registry(module))


@pytest.mark.parametrize("path", ["/home", "/etc", "/root", "/var/run", "/proc", "/sys"])
def test_offline_r8_approval_cannot_override_protected_roots(path: str) -> None:
    if not Path(path).is_dir():
        pytest.skip("protected directory does not exist on this OS")
    config = SandboxConfig(bubblewrap_runtime_path=path, bubblewrap_runtime_roots=["/"])
    with pytest.raises(CapabilityUnavailableError):
        build_bubblewrap_args(config, "/fake/bwrap")


def test_offline_r8_explicit_read_only_root(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    config = SandboxConfig(bubblewrap_runtime_path=str(runtime))
    with pytest.raises(CapabilityUnavailableError, match="runtime_roots"):
        build_bubblewrap_args(config, "/fake/bwrap")
    config.bubblewrap_runtime_roots = [str(tmp_path)]
    args = build_bubblewrap_args(config, "/fake/bwrap")
    assert args[args.index(str(runtime)) - 1 : args.index(str(runtime)) + 2] == [
        "--ro-bind",
        str(runtime),
        "/opt/nexura-runtime",
    ]


def test_offline_r8_account_homes_and_symlink_parents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import pwd

    home = tmp_path / "account"
    home.mkdir()
    approved = tmp_path / "approved"
    runtime = approved / "runtime"
    runtime.mkdir(parents=True)
    monkeypatch.setattr(pwd, "getpwall", lambda: [SimpleNamespace(pw_dir=str(home))])
    (home / "link").symlink_to(approved, target_is_directory=True)
    for path in (home, home / "link/runtime"):
        config = SandboxConfig(
            bubblewrap_runtime_path=str(path), bubblewrap_runtime_roots=[str(tmp_path)]
        )
        with pytest.raises(CapabilityUnavailableError, match="dedicated runtime"):
            build_bubblewrap_args(config, "/fake/bwrap")


@pytest.mark.parametrize("failure", ["", "umount", "rmtree"])
@pytest.mark.parametrize("prefix", [False, True])
async def test_offline_r6_mount_cleanup_and_failure_reporting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str, prefix: bool
) -> None:
    """Fake mount table + runsc; no actual mounts, namespaces or processes."""
    import re
    import shutil

    commands: list[tuple[str, ...]] = []
    mounts: list[str] = []
    roots: list[Path] = []
    read_text = Path.read_text
    real_mkdtemp = tempfile.mkdtemp
    real_rmtree = shutil.rmtree

    def mountinfo(path: Path, *a: Any, **kw: Any) -> str:
        if str(path) == "/proc/self/mountinfo":
            return "\n".join(f"1 0 0:1 / {p} rw - nsfs nsfs rw" for p in mounts)
        return read_text(path, *a, **kw)

    def remove(path: Path, *a: Any, **kw: Any) -> None:
        if failure == "rmtree" and path in roots:
            raise OSError("fake directory busy")
        real_rmtree(path, *a, **kw)

    async def drain() -> None:
        return None

    async def spawn(*args: str, **kw: Any) -> Any:
        commands.append(args)
        stdout, stderr = asyncio.StreamReader(), asyncio.StreamReader()
        rc = 0
        if "run" in args:
            bundle = Path(next(a.split("=", 1)[1] for a in args if a.startswith("--bundle=")))
            roots.append(bundle.parent)
            state = bundle.parent / "state"
            state.mkdir()
            (state / "null-netns").touch()
            mounts.append(str(state / "null-netns"))
            spec = json.loads((bundle / "config.json").read_text())
            marker = re.search(r"__nexura_sandbox_started_[a-f0-9]+__", spec["process"]["args"][-1])
            assert marker
            stdout.feed_data(marker[0].encode() + b"\nok\n")
        if "/usr/bin/umount" in args:
            assert args[-1] == mounts[0]
            if failure == "umount":
                rc = 1
            else:
                mounts.clear()
        if "/usr/bin/rm" in args:
            assert prefix and not mounts
            assert args[-1] == str(roots[-1])
            if failure == "rmtree":
                rc = 1
            else:
                real_rmtree(roots[-1])
        stdout.feed_eof()
        stderr.feed_eof()

        async def wait() -> int:
            return rc

        return SimpleNamespace(
            pid=987654321,
            returncode=rc,
            stdout=stdout,
            stderr=stderr,
            wait=wait,
            stdin=SimpleNamespace(
                close=lambda: None, is_closing=lambda: True, write=lambda b: None, drain=drain
            ),
        )

    monkeypatch.delenv("NEXURA_REAL_GVISOR", raising=False)
    if prefix:
        monkeypatch.setenv("NEXURA_REAL_GVISOR", "1")
    monkeypatch.setattr(Path, "read_text", mountinfo)
    monkeypatch.setattr(tempfile, "mkdtemp", lambda *a, **kw: real_mkdtemp(*a, dir=tmp_path, **kw))
    monkeypatch.setattr(worker_mod.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(worker_mod.os, "killpg", lambda *a: None)
    monkeypatch.setattr(worker_mod.shutil, "rmtree", remove)
    rootfs = tmp_path / "rootfs"
    rootfs.mkdir()
    config = SandboxConfig(
        runsc_path=sys.executable,
        rootfs_path=str(rootfs),
        extra={"runsc_command_prefix": ["fake-sudo", "-n"]} if prefix else {},
    )
    worker = GVisorOCIWorker()
    try:
        if failure:
            with pytest.raises(CapabilityUnavailableError, match="gVisor teardown") as exc:
                await worker.execute(ExecutionRequest(code="unused"), config)
            expected = (
                "umount"
                if failure == "umount"
                else ("directory removal failed" if prefix else "fake directory busy")
            )
            assert expected in str(exc.value)
            assert roots[0].exists()
        else:
            for _ in range(2):
                result = await worker.execute(ExecutionRequest(code="unused"), config)
                assert result.stdout == "ok\n"
                assert not mounts and not roots[-1].exists()
        assert any("delete" in cmd for cmd in commands)
        assert any("/usr/bin/umount" in cmd for cmd in commands)
        if prefix:
            assert all(cmd[:2] == ("fake-sudo", "-n") for cmd in commands)
        assert worker.active_worker_count == 0
    finally:
        for root in roots:
            if root.exists():
                for item in root.rglob("*"):
                    if item.is_dir():
                        item.chmod(0o700)
                real_rmtree(root)
