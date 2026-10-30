# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Operator-enabled Linux namespaces with mandatory seccomp and explicit init teardown."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from nexura_runtime.integrations.sandbox.backends import BackendStatus
from nexura_runtime.integrations.sandbox.errors import (
    CapabilityUnavailableError,
    ExecutionTimeoutError,
    InputLimitExceededError,
    OutputLimitExceededError,
    SecurityViolationError,
)
from nexura_runtime.integrations.sandbox.models import (
    ExecutionRequest,
    ExecutionResult,
    IsolationLevel,
    SandboxConfig,
)
from nexura_runtime.integrations.sandbox.worker import (
    _check_cancellation,
    validate_request_language,
    validate_virtual_paths,
)
from nexura_sandbox._lifecycle import RunGuard, kill_cgroup, run_cgroup
from nexura_sandbox.bubblewrap_policy import default_bpf, setup_launcher


def _unavailable(reason: str) -> CapabilityUnavailableError:
    return CapabilityUnavailableError(reason, backend="bubblewrap")


def _runtime(config: SandboxConfig) -> tuple[str, list[str]]:
    python = Path(config.bubblewrap_python_path)
    if not python.is_absolute() or not python.is_file() or not os.access(python, os.X_OK):
        raise _unavailable("bubblewrap_python_path must name an absolute executable Python")
    mounts: list[str] = []
    runtime = Path(config.bubblewrap_runtime_path) if config.bubblewrap_runtime_path else None
    if runtime is not None:
        if not runtime.is_absolute() or not runtime.is_dir() or runtime.is_symlink():
            raise _unavailable("bubblewrap_runtime_path must name an absolute runtime directory")
        configured_runtime = runtime
        runtime = runtime.resolve()
        import pwd

        homes = [Path.home(), *(Path(entry.pw_dir) for entry in pwd.getpwall())]
        # Ignore service pseudo-homes such as / and /nonexistent, while refusing
        # every actual account home, including homes outside /home.
        protected = [
            Path("/home"),
            Path("/etc"),
            Path("/root"),
            Path("/var/run"),
            Path("/run"),
            Path("/proc"),
            Path("/sys"),
            Path.cwd(),
            *(p for p in homes if p != Path("/") and p.is_dir()),
        ]
        if runtime == Path("/tmp") or any(
            candidate == path.resolve()
            or candidate.is_relative_to(path.resolve())
            or path.resolve().is_relative_to(candidate)
            for path in protected
            for candidate in (configured_runtime, runtime)
        ):
            raise _unavailable("mount a dedicated runtime/venv, not a host directory tree")
        roots = [Path(root) for root in config.bubblewrap_runtime_roots]
        if not any(
            root.is_absolute()
            and root.is_dir()
            and root.resolve() != Path("/")
            and runtime.is_relative_to(root.resolve())
            for root in roots
        ):
            raise _unavailable("runtime must be inside an explicit bubblewrap_runtime_roots entry")
        mounts = ["--ro-bind", str(runtime), "/opt/nexura-runtime"]
    # Symlink targets must also exist inside the namespace; never mount their host parents.
    if not python.resolve().is_relative_to("/usr") and not (
        runtime and python.resolve().is_relative_to(runtime)
    ):
        raise _unavailable("Python symlink target is outside /usr and the configured runtime")
    if runtime and python.is_relative_to(runtime):
        return str(Path("/opt/nexura-runtime") / python.relative_to(runtime)), mounts
    if python.is_relative_to("/usr"):
        return str(python), mounts
    raise _unavailable("Python must be under /usr or bubblewrap_runtime_path")


def build_bubblewrap_args(
    config: SandboxConfig,
    binary: str,
    *,
    command: Sequence[str] | None = None,
    code_fd: int | None = None,
    input_fds: Sequence[tuple[str, int]] = (),
    seccomp_fd: int | None = None,
    additional_seccomp_fd: int | None = None,
    cpu_seconds: int | None = None,
    start_marker: bytes | None = None,
    setup_fds: tuple[int, int, int | None] | None = None,
    info_fd: int | None = None,
    block_fd: int | None = None,
) -> list[str]:
    """Build argv without shell interpolation; descriptors contain bounded input data.

    NPROC is applied by prlimit *inside* the namespace, after bwrap's trusted setup forks.
    The payload runs as nonzero UID with no capabilities, so RLIMIT_NPROC is effective.
    """
    python, runtime_mounts = _runtime(config)
    args = [
        binary,
        "--unshare-all",
        "--unshare-user",
        "--unshare-net",
        "--unshare-pid",
        "--disable-userns",
        "--die-with-parent",
        "--new-session",
        "--as-pid-1",
        "--clearenv",
        "--cap-drop",
        "ALL",
        "--uid",
        "65534",
        "--gid",
        "65534",
        "--ro-bind",
        "/usr",
        "/usr",
    ]
    if setup_fds is not None:
        # Only a fixed trusted launcher runs here. Inner bwrap maps the payload
        # to UID 65534, drops caps, disables userns and loads both BPF policies.
        args.remove("--disable-userns")
        args[args.index("--uid") + 1] = "0"
        args[args.index("--gid") + 1] = "0"
        args += ["--cap-add", "CAP_SYS_ADMIN", "--cap-add", "CAP_SETPCAP"]
    if info_fd is not None:
        args += ["--info-fd", str(info_fd)]
    if block_fd is not None:
        args += ["--block-fd", str(block_fd)]
    for name in ("lib", "lib64", "bin"):
        path = Path("/") / name
        if path.is_symlink():
            # Preserve merged-/usr layouts without binding host root or home.
            target = path.resolve()
            if not target.is_relative_to("/usr"):
                raise _unavailable(f"unsupported system symlink {path} -> {target}")
            args += ["--symlink", str(target), str(path)]
        elif path.is_dir():
            args += ["--ro-bind", str(path), str(path)]
    args += runtime_mounts
    args += [
        "--size",
        str(config.bubblewrap_tmpfs_bytes),
        "--tmpfs",
        "/tmp",
        "--dir",
        "/tmp/work",
        "--chdir",
        "/tmp/work",
        "--dir",
        "/sandbox",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        # /dev (including /dev/shm) and the namespace root offer no writable
        # filesystem scratch. Memfd/SysV allocation is denied separately by seccomp.
        "--remount-ro",
        "/dev",
        "--setenv",
        "PATH",
        "/usr/bin:/bin",
        "--setenv",
        "LANG",
        "C.UTF-8",
        "--setenv",
        "TMPDIR",
        "/tmp",
        "--setenv",
        "PYTHONHASHSEED",
        "0",
    ]
    if setup_fds is None:
        args += [
            "--ro-bind",
            "/dev/null",
            "/proc/keys",
            "--ro-bind",
            "/dev/null",
            "/proc/key-users",
            "--remount-ro",
            "/proc",
        ]
    else:
        launcher, policy, operator_policy = setup_fds
        for fd, name in ((launcher, "launcher.py"), (policy, "default.bpf")):
            args += ["--ro-bind-data", str(fd), f"/nexura-internal/{name}"]
        if operator_policy is not None:
            args += ["--ro-bind-data", str(operator_policy), "/nexura-internal/operator.bpf"]
        args += ["--ro-bind", binary, "/nexura-internal/bwrap"]
    # No payload needs a terminal. Hiding devpts also bounds PTY pool consumption.
    args += ["--ro-bind", "/dev/null", "/dev/pts/ptmx"]
    if code_fd is not None:
        args += ["--perms", "0400", "--file", str(code_fd), "/tmp/code.py"]
    for name, fd in input_fds:
        checked = validate_virtual_paths({name: ""})[0][0]
        args += ["--ro-bind-data", str(fd), f"/sandbox/{checked}"]
    args += ["--remount-ro", "/"]
    if seccomp_fd is not None and setup_fds is None:
        if additional_seccomp_fd is None:
            args += ["--seccomp", str(seccomp_fd)]
        else:
            args += [
                "--add-seccomp-fd",
                str(seccomp_fd),
                "--add-seccomp-fd",
                str(additional_seccomp_fd),
            ]
    if setup_fds is not None:
        # The trusted launcher remounts /tmp with nr_inodes before entering the
        # final user namespace. Payload NPROC/CPU apply after these setup forks.
        return args + [
            "--",
            "/usr/bin/python3",
            "-I",
            "-B",
            "/nexura-internal/launcher.py",
            *(command or ()),
        ]
    args += [
        "--",
        "/usr/bin/prlimit",
        f"--nofile={config.bubblewrap_max_open_files}:{config.bubblewrap_max_open_files}",
        "--",
        "/usr/bin/prlimit",
        f"--nproc={config.max_processes}:{config.max_processes}",
        "--",
    ]
    if cpu_seconds is not None:
        args.insert(args.index("/usr/bin/prlimit") + 1, f"--cpu={cpu_seconds}:{cpu_seconds}")
    if command is not None:
        args += list(command)
    elif start_marker is not None:
        args += [
            python,
            "-I",
            "-B",
            "-u",
            "-c",
            f"import os, runpy; os.write(1, {start_marker!r}); "
            "runpy.run_path('/tmp/code.py', run_name='__main__')",
        ]
    else:
        args += [python, "-I", "-B", "-u", "/tmp/code.py"]
    return args


def _limit_values(config: SandboxConfig) -> tuple[tuple[int, int, int], ...]:
    # Import only on Linux: importing the SDK must keep working on Windows.
    import resource

    cpu = config.bubblewrap_cpu_seconds
    memory = config.max_memory_mb * 1024 * 1024
    return (
        (resource.RLIMIT_CPU, cpu, cpu + 1),
        (resource.RLIMIT_AS, memory, memory),
        (resource.RLIMIT_FSIZE, config.bubblewrap_tmpfs_bytes, config.bubblewrap_tmpfs_bytes),
        (resource.RLIMIT_CORE, 0, 0),
    )


def _preexec_limits(config: SandboxConfig) -> Callable[[], None]:
    import resource

    limits = _limit_values(config)

    def apply() -> None:
        for kind, soft, hard in limits:
            # Trusted setup gets a hard backstop. Payload prlimit replaces this
            # with the exact configured soft AND hard payload CPU budget.
            resource.setrlimit(kind, (soft, hard))

    return apply


def _data_fd(stack: contextlib.ExitStack, data: bytes) -> int:
    file = stack.enter_context(os.fdopen(os.memfd_create("nexura-bwrap", os.MFD_CLOEXEC), "w+b"))
    file.write(data)
    file.seek(0)
    return file.fileno()


def _run_limits(config: SandboxConfig, cgroup: Path | None) -> Callable[[], None]:
    apply_limits = _preexec_limits(config)

    def apply() -> None:
        if cgroup is not None:
            (cgroup / "cgroup.procs").write_text(str(os.getpid()))
        apply_limits()

    return apply


def _seccomp_fd(stack: contextlib.ExitStack, config: SandboxConfig) -> int | None:
    if config.bubblewrap_seccomp_bpf_path is None:
        return None
    path = Path(config.bubblewrap_seccomp_bpf_path)
    if not path.is_absolute():
        raise _unavailable("seccomp BPF path must be absolute")
    with path.open("rb") as source:
        data = source.read(32_769)
    if not data or len(data) % 8 or len(data) > 32_768:
        raise _unavailable("seccomp BPF must contain 1..4096 native cBPF instructions")
    return _data_fd(stack, data)


def _kill_group(pid: int) -> None:
    # A supervisor must receive a teardown request, then kill/reap its own group.
    # Probe launchers are also trusted; namespace init is killed separately first.
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pid, signal.SIGTERM)


class _NamespaceInit:
    """Pin bwrap's trusted child-pid before allowing any request code to start."""

    def __init__(self, stack: contextlib.ExitStack) -> None:
        self.info_fd = _data_fd(stack, b"")
        self.block_fd, self.release_fd = os.pipe2(os.O_CLOEXEC)
        stack.callback(os.close, self.block_fd)
        stack.callback(os.close, self.release_fd)
        self.pidfd: int | None = None
        self.guard: RunGuard | None = None
        stack.callback(self.close)

    def capture(self) -> bool:
        if self.guard is not None and not self.guard.ready:
            return False
        if self.pidfd is not None:
            return True
        try:
            info = json.loads(os.pread(self.info_fd, 8192, 0))
        except (ValueError, UnicodeError):
            return False
        pid = info.get("child-pid")
        if not isinstance(pid, int) or pid <= 1:
            raise _unavailable("bwrap did not report a valid namespace init PID")
        try:
            self.pidfd = os.pidfd_open(pid)
        except ProcessLookupError:
            return False
        os.write(self.release_fd, b"1")
        return True

    def kill(self) -> None:
        if self.pidfd is not None:
            with contextlib.suppress(ProcessLookupError):
                signal.pidfd_send_signal(self.pidfd, signal.SIGKILL)

    def close(self) -> None:
        if self.pidfd is not None:
            # The dedicated supervisor owns reaping, including namespace init.
            os.close(self.pidfd)
            self.pidfd = None


def _hardened_args(
    stack: contextlib.ExitStack, config: SandboxConfig, binary: str, **kwargs: Any
) -> list[str]:
    try:
        policy = _data_fd(stack, default_bpf())
    except ValueError as exc:
        raise _unavailable(str(exc)) from exc
    operator = _seccomp_fd(stack, config)
    if config.bubblewrap_tmpfs_inodes is None:
        # Stay inside the operator-installed bwrap AppArmor profile for namespace
        # setup. Default payloads need no Python mount call or elevated setup stage.
        return build_bubblewrap_args(
            config,
            binary,
            seccomp_fd=policy,
            additional_seccomp_fd=operator,
            cpu_seconds=config.bubblewrap_cpu_seconds,
            **kwargs,
        )
    python, _ = _runtime(config)
    command = kwargs.get("command")
    marker = kwargs.get("start_marker")
    payload = (
        []
        if command
        else [
            python,
            "-I",
            "-B",
            "-u",
            "-c",
            f"import os, runpy; os.write(1, {marker!r}); "
            "runpy.run_path('/tmp/code.py', run_name='__main__')",
        ]
    )
    cpu = config.bubblewrap_cpu_seconds
    inner = [
        "/nexura-internal/bwrap",
        "--unshare-user",
        "--unshare-pid",
        "--as-pid-1",
        "--disable-userns",
        "--die-with-parent",
        "--uid",
        "65534",
        "--gid",
        "65534",
        "--cap-drop",
        "ALL",
        "--ro-bind",
        "/",
        "/",
        "--bind",
        "/tmp",
        "/tmp",
        "--proc",
        "/proc",
        "--ro-bind",
        "/dev/null",
        "/proc/keys",
        "--ro-bind",
        "/dev/null",
        "/proc/key-users",
        "--remount-ro",
        "/proc",
        "--remount-ro",
        "/",
        "--chdir",
        "/tmp/work",
        "--",
        "/usr/bin/prlimit",
        f"--nproc={config.max_processes}:{config.max_processes}",
        f"--nofile={config.bubblewrap_max_open_files}:{config.bubblewrap_max_open_files}",
        f"--cpu={cpu}:{cpu}",
        "--",
        *payload,
    ]
    launcher = _data_fd(
        stack,
        setup_launcher(
            inner, tmpfs_bytes=config.bubblewrap_tmpfs_bytes, inodes=config.bubblewrap_tmpfs_inodes
        ),
    )
    return build_bubblewrap_args(config, binary, setup_fds=(launcher, policy, operator), **kwargs)


def _argument_fds(args: Sequence[str]) -> tuple[int, ...]:
    fds = {
        int(args[i + 1])
        for i, arg in enumerate(args[:-1])
        if arg
        in {
            "--file",
            "--ro-bind-data",
            "--info-fd",
            "--block-fd",
            "--seccomp",
            "--add-seccomp-fd",
        }
    }
    if "--supervise" in args:
        fds.add(json.loads(args[args.index("--supervise") + 1])["parent"])
    return tuple(sorted(fds))


class BubblewrapWorker:
    """Native Python in private namespaces; optional delegated cgroup for lifecycle only."""

    backend_name = "bubblewrap"
    isolation: IsolationLevel = "os_sandbox"
    languages = frozenset({"python", "python3"})
    # Advertise the default profile's actual scope: AS is per process, tmpfs
    # bytes are bounded, and no inode cap or aggregate memory ceiling is enforced.
    # A successful inode opt-in is reported separately in result details.
    enforced_limits = frozenset(
        {"wall_time", "cpu", "address_space", "processes", "disk_bytes", "output", "open_files"}
    )

    def __init__(self) -> None:
        self._active_pids: set[int] = set()

    @property
    def active_worker_count(self) -> int:
        return len(self._active_pids)

    def _binary(self, config: SandboxConfig) -> str:
        if sys.platform != "linux":
            raise _unavailable("bubblewrap is Linux-only")
        if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
            raise _unavailable("bubblewrap requires Linux pidfd support for namespace teardown")
        binary = config.bubblewrap_path or shutil.which("bwrap")
        if not binary:
            raise _unavailable("bwrap is missing; install the operator-managed bubblewrap package")
        path = Path(binary)
        if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
            raise _unavailable("bubblewrap_path must name an absolute executable bwrap binary")
        if not Path("/usr/bin/prlimit").is_file():
            raise _unavailable("/usr/bin/prlimit (util-linux) is required for NPROC")
        return str(path)

    def probe(self, config: SandboxConfig) -> BackendStatus:
        """Actually attempt a bounded bwrap true run; a sysctl alone is not proof of denial."""
        try:
            binary = self._binary(config)
            with contextlib.ExitStack() as stack:
                namespace = _NamespaceInit(stack)
                args = _hardened_args(
                    stack,
                    config,
                    binary,
                    command=["/usr/bin/true"],
                    info_fd=namespace.info_fd,
                )
                proc = subprocess.Popen(
                    args,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env={},
                    cwd="/",
                    start_new_session=True,
                    close_fds=True,
                    pass_fds=_argument_fds(args),
                    preexec_fn=_preexec_limits(config),
                )
                try:
                    _, stderr = proc.communicate(timeout=min(3.0, config.timeout_seconds))
                except subprocess.TimeoutExpired:
                    namespace.capture()
                    namespace.kill()
                    _kill_group(proc.pid)
                    try:
                        proc.communicate(timeout=0.25)
                    except subprocess.TimeoutExpired:
                        for stream in (proc.stdout, proc.stderr):
                            if stream:
                                stream.close()
                        proc.wait(timeout=0.25)
                    return BackendStatus(
                        self.backend_name, False, "bwrap namespace probe timed out"
                    )
                if proc.returncode:
                    reason = stderr.decode("utf-8", "replace").strip()[:2000]
                    apparmor = Path("/proc/sys/kernel/apparmor_restrict_unprivileged_userns")
                    with contextlib.suppress(OSError):
                        if apparmor.read_text().strip() == "1":
                            reason += "; kernel.apparmor_restrict_unprivileged_userns=1"
                    return BackendStatus(
                        self.backend_name,
                        False,
                        f"bwrap namespace probe failed (exit {proc.returncode}); unprivileged "
                        f"user namespaces may be blocked by the host/AppArmor: {reason}",
                    )
            return BackendStatus(self.backend_name, True)
        except (CapabilityUnavailableError, OSError, subprocess.SubprocessError) as exc:
            return BackendStatus(self.backend_name, False, str(exc))

    async def execute(
        self,
        request: ExecutionRequest,
        config: SandboxConfig,
        *,
        cancellation_token: Any | None = None,
    ) -> ExecutionResult:
        _check_cancellation(cancellation_token)
        validate_request_language(request.language, self.backend_name)
        if request.env:
            raise SecurityViolationError("env", "bubblewrap does not accept caller environment")
        if config.allow_network:
            raise SecurityViolationError("network_policy", "bubblewrap always disables network")
        files = validate_virtual_paths(request.files)
        size = sum(
            len(s.encode("utf-8")) for s in (request.code, request.stdin, *request.files.values())
        )
        if size > config.max_input_bytes:
            raise InputLimitExceededError("input bytes", limit=config.max_input_bytes, actual=size)
        if len(files) > config.max_input_files:
            raise InputLimitExceededError(
                "file count",
                limit=config.max_input_files,
                actual=len(files),
            )
        binary = self._binary(config)
        worker_id = f"bwrap_{uuid.uuid4().hex[:12]}"
        marker = f"NEXURA_START_{uuid.uuid4().hex}\n".encode()
        capture_cap = config.max_output_bytes + len(marker)
        start = time.monotonic()
        stdout = bytearray()
        stderr = bytearray()
        total = 0
        tasks: list[asyncio.Task[Any]] = []
        proc: asyncio.subprocess.Process | None = None
        spawn_task: asyncio.Task[asyncio.subprocess.Process] | None = None
        stack = contextlib.ExitStack()
        namespace = _NamespaceInit(stack)
        guard: RunGuard | None = None
        cgroup: Path | None = None

        async def read(stream: asyncio.StreamReader, dest: bytearray) -> None:
            nonlocal total
            while chunk := await stream.read(4096):
                remaining = max(0, capture_cap - total)
                dest.extend(chunk[:remaining])
                total += len(chunk)
                if total > capture_cap:
                    raise OutputLimitExceededError(config.max_output_bytes, total - len(marker))

        async def feed() -> None:
            assert proc and proc.stdin
            try:
                proc.stdin.write(request.stdin.encode("utf-8"))
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                proc.stdin.close()

        try:
            cgroup = run_cgroup()
            guard = RunGuard(info_fd=namespace.info_fd, cgroup=cgroup)
            namespace.guard = guard
            code_fd = _data_fd(stack, request.code.encode("utf-8"))
            inputs = [(name, _data_fd(stack, value.encode("utf-8"))) for name, value in files]
            args = _hardened_args(
                stack,
                config,
                binary,
                code_fd=code_fd,
                input_fds=inputs,
                start_marker=marker,
                info_fd=namespace.info_fd,
                block_fd=namespace.block_fd,
            )
            # This child alone is a subreaper. It owns bwrap and any adopted
            # descendants, including a payload that clears its parent-death signal.
            parent_fd = os.pidfd_open(os.getpid())
            stack.callback(os.close, parent_fd)
            fds = _argument_fds(args)
            from nexura_sandbox import gvisor_worker

            command = [
                sys.executable,
                "-I",
                "-S",
                str(Path(gvisor_worker.__file__).resolve()),
                "--supervise",
                json.dumps(
                    {
                        "parent": parent_fd,
                        "fds": fds,
                        "limits": _limit_values(config),
                        "cgroup": str(cgroup) if cgroup else None,
                    }
                ),
                *args,
            ]
            spawn_task = asyncio.create_task(
                asyncio.create_subprocess_exec(
                    *command,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env={},
                    cwd="/",
                    close_fds=True,
                    pass_fds=_argument_fds(command),
                    start_new_session=True,
                )
            )
            try:
                proc = await asyncio.wait_for(
                    asyncio.shield(spawn_task),
                    timeout=max(0, start + config.timeout_seconds - time.monotonic()),
                )
            except TimeoutError as exc:
                raise ExecutionTimeoutError(config.timeout_seconds, worker_id=worker_id) from exc
            self._active_pids.add(proc.pid)
            assert proc.stdout and proc.stderr
            tasks = [
                asyncio.create_task(read(proc.stdout, stdout)),
                asyncio.create_task(read(proc.stderr, stderr)),
                asyncio.create_task(feed()),
                asyncio.create_task(proc.wait()),
            ]
            # Include stdin delivery and process exit in the same wall deadline.
            try:
                async with asyncio.timeout_at(start + config.timeout_seconds):
                    while not all(task.done() for task in tasks):
                        _check_cancellation(cancellation_token)
                        namespace.capture()
                        await asyncio.wait(tasks, timeout=0.05, return_when=asyncio.FIRST_EXCEPTION)
                        for task in tasks:
                            if task.done():
                                task.result()
            except TimeoutError as exc:
                raise ExecutionTimeoutError(config.timeout_seconds, worker_id=worker_id) from exc
            if proc.returncode in (
                -signal.SIGXCPU,
                128 + signal.SIGXCPU,
                -signal.SIGKILL,
                128 + signal.SIGKILL,
            ):
                # bwrap forwards signal exits in shell encoding. SIGKILL may
                # be the exact hard CPU cap (or another resource/operator kill).
                error = ExecutionTimeoutError(config.bubblewrap_cpu_seconds, worker_id=worker_id)
                error.details.update({"limit": "cpu_or_signal_kill", "returncode": proc.returncode})
                raise error
            if not stdout.startswith(marker):
                raise _unavailable(
                    "bwrap exited before the interpreter started "
                    f"(exit {proc.returncode}): {stderr.decode('utf-8', 'replace')[:2000]}"
                )
            del stdout[: len(marker)]
            return ExecutionResult(
                exit_code=proc.returncode if proc.returncode is not None else 1,
                stdout=stdout.decode("utf-8", "replace"),
                stderr=stderr.decode("utf-8", "replace"),
                duration_ms=round((time.monotonic() - start) * 1000, 2),
                worker_id=worker_id,
                isolated=True,
                backend=self.backend_name,
                isolation=self.isolation,
                details={
                    "seccomp": any(flag in args for flag in ("--seccomp", "--add-seccomp-fd"))
                    or "/nexura-internal/default.bpf" in args,
                    "cgroups": cgroup is not None,
                    "cgroup_scope": "lifecycle_only" if cgroup is not None else None,
                    "max_open_files": config.bubblewrap_max_open_files,
                    "network_isolated": True,
                    "tmpfs_bytes": config.bubblewrap_tmpfs_bytes,
                    "tmpfs_inodes": config.bubblewrap_tmpfs_inodes,
                    "tmpfs_inodes_enforced": config.bubblewrap_tmpfs_inodes is not None,
                    "enforced_limits": sorted(
                        self.enforced_limits
                        | (
                            {"tmpfs_inodes"}
                            if config.bubblewrap_tmpfs_inodes is not None
                            else set()
                        )
                    ),
                    "memory_scope": "per_process_address_space_plus_separate_tmpfs",
                    "max_processes": config.max_processes,
                },
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise _unavailable(str(exc)) from exc
        finally:

            async def cleanup() -> None:
                nonlocal proc
                errors: list[str] = []
                if proc is None and spawn_task is not None:
                    proc = await spawn_task
                # A pidfd targets this namespace init even after reparenting, and
                # cannot target a recycled PID. Kernel init death kills the namespace.
                try:
                    namespace.capture()
                    namespace.kill()
                except Exception as exc:
                    errors.append(f"namespace kill: {exc}")
                if proc:
                    try:
                        # Keep the supervisor alive to reap after killing bwrap.
                        if proc.returncode is None:
                            _kill_group(proc.pid)
                    except OSError as exc:
                        errors.append(f"supervisor stop: {exc}")
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                if proc:
                    # Allow the dedicated supervisor to reap first. Closing a live
                    # asyncio transport would SIGKILL the reaper itself.
                    try:
                        supervisor_fd = os.pidfd_open(proc.pid)
                    except ProcessLookupError:
                        pass  # Already reaped; inherited output pipes may still be open.
                    else:
                        try:
                            import selectors

                            with selectors.DefaultSelector() as selector:
                                selector.register(supervisor_fd, selectors.EVENT_READ)
                                async with asyncio.timeout(1.5):
                                    while not selector.select(0):
                                        await asyncio.sleep(0.01)
                        except TimeoutError:
                            errors.append("sandbox supervisor did not stop")
                        finally:
                            os.close(supervisor_fd)
                    transport = getattr(proc, "_transport", None)
                    if transport is not None:
                        transport.close()
                    await asyncio.wait_for(proc.wait(), timeout=0.5)
                if guard is not None:
                    guard.close()
                if errors:
                    raise _unavailable("; ".join(errors))

            primary = sys.exception()
            cleanup_task = asyncio.create_task(cleanup())
            interrupted = False
            try:
                while not cleanup_task.done():
                    try:
                        await asyncio.shield(cleanup_task)
                    except asyncio.CancelledError:
                        interrupted = True
                cleanup_task.result()
            except Exception as exc:
                if primary is None:
                    raise _unavailable(f"bubblewrap teardown failed: {exc}") from exc
                primary.add_note(f"bubblewrap teardown failed: {exc}")
            finally:
                if proc:
                    self._active_pids.discard(proc.pid)
                final_errors: list[str] = []
                try:
                    if guard is not None:
                        guard.close()
                except Exception as exc:
                    final_errors.append(f"crash watchdog: {exc}")
                try:
                    # Retry locally if the watchdog failed before removing its cgroup.
                    if cgroup is not None and cgroup.exists():
                        kill_cgroup(cgroup)
                except Exception as exc:
                    final_errors.append(f"cgroup cleanup: {exc}")
                finally:
                    try:
                        stack.close()
                    except Exception as exc:
                        final_errors.append(f"namespace cleanup: {exc}")
                if final_errors:
                    message = "bubblewrap teardown failed: " + "; ".join(final_errors)
                    if primary is not None:
                        primary.add_note(message)
                    else:
                        raise _unavailable(message)
            if interrupted and not isinstance(primary, asyncio.CancelledError):
                raise asyncio.CancelledError
