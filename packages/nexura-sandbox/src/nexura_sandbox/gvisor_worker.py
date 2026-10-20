# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
# ruff: noqa: E402 -- stdlib-only watchdog entry point must precede runtime imports
"""gVisor (runsc) OCI sandbox backend: a user-space kernel boundary for untrusted code.

Moved unchanged from ``nexura_runtime.integrations.sandbox.worker`` into the
``nexura-sandbox`` package. The shared request validation, output bounding and process
reaping it uses stay in the runtime's ``worker`` module.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import importlib
import json
import os
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any


def _bounded_cleanup_command(args: list[str], timeout: float = 3.0) -> tuple[int, bytes]:
    """Bound cleanup wall time, output and inherited pipes; reap our own CLI group."""
    with (
        selectors.DefaultSelector() as selector,
        subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env={},
            cwd="/",
            close_fds=True,
        ) as proc,
    ):
        assert proc.stdout is not None
        deadline = time.monotonic() + timeout
        output = bytearray()
        try:
            selector.register(proc.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise subprocess.TimeoutExpired(args, timeout)
                chunk = os.read(proc.stdout.fileno(), 65536)
                if not chunk:
                    return proc.wait(timeout=max(0.001, deadline - time.monotonic())), bytes(output)
                output.extend(chunk)
                if len(output) > 1_048_576:
                    raise OSError("sandbox cleanup command output exceeded 1 MiB")
        finally:
            # On failure the CLI has not been reaped, so the new process group
            # still belongs to this invocation and cannot be a recycled PID.
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
                proc.kill()
            proc.wait(timeout=0.5)


def _start_crash_guard(data: dict[str, Any], fds: tuple[int, ...]) -> subprocess.Popen[bytes]:
    """Only fixed installed code and trusted lifecycle descriptors enter this child."""
    return subprocess.Popen(
        [sys.executable, "-I", "-S", str(Path(__file__).resolve()), json.dumps(data)],
        pass_fds=fds,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
        cwd="/",
        env={},
    )


if __name__ == "__main__":
    # The crash watchdog must start without site packages, registry initialization
    # or backend probing. Load only its stdlib sibling from this installed directory.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    lifecycle = importlib.import_module("_lifecycle")
    if sys.argv[1] == "--supervise":
        data = json.loads(sys.argv[2])
        if "parent" not in data:
            # sudo closes non-stdio descriptors. Pin and verify the original
            # service identity before launching anything in the explicit test profile.
            data["parent"] = os.pidfd_open(data["parent_pid"])
            if lifecycle.process_start(data["parent_pid"]) != data["parent_start"]:
                raise SystemExit("sandbox service identity changed before supervisor startup")
        lifecycle.enable_subreaper()

        def apply_limits() -> None:
            import resource

            if data.get("cgroup"):
                (Path(data["cgroup"]) / "cgroup.procs").write_text(str(os.getpid()))
            for kind, soft, hard in data.get("limits", []):
                resource.setrlimit(kind, (soft, hard))

        def launch() -> subprocess.Popen[bytes]:
            # Only this dedicated child adopts or reaps descendants. The service's
            # process-wide subreaper setting is never changed by a worker execution.
            child = subprocess.Popen(
                sys.argv[3:],
                close_fds=True,
                pass_fds=tuple(data["fds"]),
                start_new_session=True,
                preexec_fn=apply_limits if "limits" in data else None,
            )
            for fd in data["fds"]:
                os.close(fd)
            return child

        raise SystemExit(lifecycle.supervise(launch, data["parent"]))
    lifecycle._watch(json.loads(sys.argv[1]), _bounded_cleanup_command)
    raise SystemExit

try:
    import resource
except ImportError:
    resource = None  # type: ignore[assignment]

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
    _OutputBuffer,
    _read_stream,
    _reap_process,
    validate_mount_files,
    validate_request_language,
    validate_virtual_paths,
)
from nexura_sandbox._lifecycle import (
    RunGuard,
    mark_gvisor_task,
    process_start,
    service_identity,
    sweep_gvisor_tasks,
)


def discover_oci_infrastructure(
    config: SandboxConfig,
) -> tuple[Path | None, Path | None, list[str]]:
    """Explicit absolute operator paths only; no PATH or current-directory discovery.

    The legacy command prefix and cgroup opt-out require NEXURA_REAL_GVISOR=1. That
    environment variable is a test profile, never an isolation guarantee for deployment.
    """
    runsc_path: Path | None = None
    rootfs_path: Path | None = None
    cmd_prefix: list[str] = []

    if config.runsc_path:
        runsc_path = Path(config.runsc_path)
    if config.rootfs_path:
        rootfs_path = Path(config.rootfs_path)
    if "runsc_command_prefix" in config.extra:
        raw_prefix = config.extra["runsc_command_prefix"]
        if not isinstance(raw_prefix, (list, tuple)) or not all(
            isinstance(part, str) and part for part in raw_prefix
        ):
            raise CapabilityUnavailableError(
                "SandboxConfig.extra['runsc_command_prefix'] must be a list of non-empty strings.",
                backend="gvisor_oci",
            )
        cmd_prefix = list(raw_prefix)
        if cmd_prefix and os.environ.get("NEXURA_REAL_GVISOR") != "1":
            raise CapabilityUnavailableError(
                "runsc_command_prefix is test-only; requires NEXURA_REAL_GVISOR=1",
                backend="gvisor_oci",
            )

    if runsc_path is None and "NEXURA_RUNSC_PATH" in os.environ:
        runsc_path = Path(os.environ["NEXURA_RUNSC_PATH"])
    if rootfs_path is None and "NEXURA_OCI_ROOTFS_PATH" in os.environ:
        rootfs_path = Path(os.environ["NEXURA_OCI_ROOTFS_PATH"])

    for label, path in (("runsc_path", runsc_path), ("rootfs_path", rootfs_path)):
        if path is not None and (not path.is_absolute() or ".." in path.parts):
            raise CapabilityUnavailableError(
                f"{label} must be an explicit absolute path without traversal",
                backend="gvisor_oci",
            )
    cgroups_ignored(config)  # Validate the test-only opt-out even during probing.

    return runsc_path, rootfs_path, cmd_prefix


def cgroups_ignored(config: SandboxConfig) -> bool:
    """Only the explicitly enabled real-gVisor test profile may bypass host cgroups."""
    ignored = config.extra.get("allow_without_cgroups") is True
    if ignored and os.environ.get("NEXURA_REAL_GVISOR") != "1":
        raise CapabilityUnavailableError(
            "allow_without_cgroups is test-only; production requires delegated cgroups "
            "(NEXURA_REAL_GVISOR=1 is only for the existing test profile)",
            backend="gvisor_oci",
        )
    return ignored


def build_gvisor_oci_spec(
    config: SandboxConfig,
    rootfs: Path,
    payload: Path,
    container_id: str,
    bootstrap: str,
) -> dict[str, Any]:
    """Build the gVisor policy: non-root guest, exact guest NPROC and bounded host tasks.

    Host pids include sentry/gofer threads, so their finite cgroup allowance is separate
    from guest NPROC. Single-process guests deny fork/clone with OCI seccomp EPERM.
    Use runsc's supported ERRNO action, not Linux's broader set of seccomp actions;
    runsc --oci-seccomp remains required. Guest code may catch the resulting OSError.
    """
    parent = Path(config.gvisor_cgroup_parent)
    if not parent.is_absolute() or ".." in parent.parts:
        raise CapabilityUnavailableError(
            "gvisor_cgroup_parent must be an absolute cgroup hierarchy path",
            backend="gvisor_oci",
        )
    if not rootfs.is_absolute() or not payload.is_absolute():
        raise CapabilityUnavailableError("OCI paths must be absolute", backend="gvisor_oci")
    if not container_id or any(
        c not in "abcdefghijklmnopqrstuvwxyz0123456789_" for c in container_id
    ):
        raise ValueError("invalid internal container id")
    linux: dict[str, Any] = {
        "namespaces": [{"type": name} for name in ("pid", "network", "ipc", "uts", "mount")],
        "cgroupsPath": str(parent / container_id),
        "resources": {
            "memory": {"limit": config.max_memory_mb * 1024 * 1024},
            "pids": {"limit": config.max_processes + config.gvisor_runtime_pids},
        },
    }
    if config.max_processes == 1:
        linux["seccomp"] = {
            "defaultAction": "SCMP_ACT_ALLOW",
            "syscalls": [
                {
                    "names": ["fork", "vfork", "clone", "clone3"],
                    "action": "SCMP_ACT_ERRNO",
                    "errnoRet": errno.EPERM,
                }
            ],
        }
    return {
        "ociVersion": "1.0.2",
        "process": {
            "terminal": False,
            "user": {"uid": 65534, "gid": 65534, "additionalGids": []},
            "capabilities": {
                key: [] for key in ("bounding", "effective", "inheritable", "permitted", "ambient")
            },
            "args": ["python3", "-I", "-B", "-u", "-c", bootstrap],
            "env": [
                "PATH=/usr/local/bin:/usr/bin:/bin",
                "LANG=C.UTF-8",
                "PYTHONHASHSEED=0",
                "PYTHONUNBUFFERED=1",
                "TMPDIR=/sandbox",
            ],
            "cwd": "/sandbox",
            "rlimits": [
                {
                    "type": "RLIMIT_AS",
                    "hard": config.max_memory_mb * 1024 * 1024,
                    "soft": config.max_memory_mb * 1024 * 1024,
                },
                {
                    "type": "RLIMIT_NPROC",
                    "hard": config.max_processes,
                    "soft": config.max_processes,
                },
                {
                    "type": "RLIMIT_FSIZE",
                    "hard": config.gvisor_tmpfs_bytes,
                    "soft": config.gvisor_tmpfs_bytes,
                },
                {"type": "RLIMIT_CORE", "hard": 0, "soft": 0},
            ],
            "noNewPrivileges": True,
        },
        "root": {"path": str(rootfs.resolve()), "readonly": True},
        "mounts": [
            {
                "destination": "/proc",
                "type": "proc",
                "source": "proc",
                "options": ["nosuid", "nodev", "noexec", "ro"],
            },
            {
                "destination": "/dev",
                "type": "tmpfs",
                "source": "tmpfs",
                "options": ["nosuid", "noexec", "mode=755", "size=65536", "ro"],
            },
            {
                "destination": "/tmp",
                "type": "bind",
                "source": str(payload.resolve()),
                "options": ["rbind", "ro", "nosuid", "nodev", "noexec"],
            },
            {
                "destination": "/sandbox",
                "type": "tmpfs",
                "source": "tmpfs",
                "options": [
                    "nosuid",
                    "nodev",
                    "noexec",
                    "mode=700",
                    "uid=65534",
                    "gid=65534",
                    f"size={config.gvisor_tmpfs_bytes}",
                ],
            },
        ],
        "linux": linux,
    }


class GVisorOCIWorker:
    """gVisor boundary with non-root NPROC, host cgroups and bounded tmpfs scratch.

    Production requires explicit absolute runtime/rootfs paths and delegated cgroups.
    Only NEXURA_REAL_GVISOR=1 permits the legacy test prefix or cgroup opt-out. That
    profile still enforces guest NPROC and the single-process OCI seccomp policy.
    """

    isolation: IsolationLevel = "gvisor"
    languages = frozenset({"python", "python3"})
    enforced_limits = frozenset(
        {
            "wall_time",
            "memory",
            "processes",
            "disk",
            "output",
            "no_network",
            "read_only_root",
            "no_host_environment",
        }
    )

    def __init__(self, backend_name: str = "gvisor_oci") -> None:
        self.backend_name = backend_name
        self._active_pids: set[int] = set()
        self._swept: set[str] = set()

    @property
    def active_worker_count(self) -> int:
        """Active launchers; removed in finally even if runtime teardown fails."""
        return len(self._active_pids)

    def probe(self, config: SandboxConfig) -> BackendStatus:
        """Validate explicit infrastructure, without claiming a live cgroup/runtime probe."""
        try:
            runsc, rootfs, _ = discover_oci_infrastructure(config)
            if runsc is None or not runsc.is_file() or not os.access(runsc, os.X_OK):
                return BackendStatus(
                    self.backend_name,
                    False,
                    "gVisor runsc binary not found or not executable; configure an explicit "
                    "absolute runsc_path or NEXURA_RUNSC_PATH",
                )
            if rootfs is None or not rootfs.is_dir():
                return BackendStatus(
                    self.backend_name,
                    False,
                    "OCI rootfs directory requires absolute rootfs_path or NEXURA_OCI_ROOTFS_PATH",
                )
            build_gvisor_oci_spec(config, rootfs, rootfs, "probe", "")
        except CapabilityUnavailableError as exc:
            return BackendStatus(self.backend_name, False, exc.reason)
        return BackendStatus(self.backend_name, True)

    async def execute(
        self,
        request: ExecutionRequest,
        config: SandboxConfig,
        *,
        cancellation_token: Any | None = None,
    ) -> ExecutionResult:
        _check_cancellation(cancellation_token)
        validate_request_language(request.language, self.backend_name)
        status = self.probe(config)
        if not status.available:
            raise CapabilityUnavailableError(status.reason, backend=self.backend_name)
        runsc_path, rootfs_path, cmd_prefix = discover_oci_infrastructure(config)
        assert runsc_path and rootfs_path
        identity = service_identity(str(runsc_path), str(rootfs_path), cmd_prefix)
        if identity not in self._swept:
            sweep_gvisor_tasks(str(runsc_path), cmd_prefix, identity)
            self._swept.add(identity)
        if request.env:
            raise SecurityViolationError("env", "gVisor refuses caller environment variables")
        files = validate_virtual_paths(request.files)
        input_size = sum(
            len(value.encode("utf-8"))
            for value in (request.code, request.stdin, *request.files, *request.files.values())
        )
        if input_size > config.max_input_bytes:
            raise InputLimitExceededError(
                "input bytes",
                limit=config.max_input_bytes,
                actual=input_size,
            )
        if len(files) > config.max_input_files:
            raise InputLimitExceededError(
                "file count",
                limit=config.max_input_files,
                actual=len(files),
            )
        limits_advisory = cgroups_ignored(config)
        worker_id = f"gvisor_{uuid.uuid4().hex[:12]}"
        container_id = f"nex_{uuid.uuid4().hex}"
        temp_dir = Path(tempfile.mkdtemp(prefix="nexura_gvisor_"))
        payload = temp_dir / "payload"
        # The private root may contain runsc's pinned null-netns host mount. Delete
        # alone does not unmount it; cleanup below unmounts it before removing this root.
        runtime_cmd = [*cmd_prefix, str(runsc_path), f"--root={temp_dir / 'state'}"]
        proc: asyncio.subprocess.Process | None = None
        spawn_task: asyncio.Task[asyncio.subprocess.Process] | None = None
        io_tasks: list[asyncio.Task[Any]] = []
        start_time = time.monotonic()
        started = False
        primary_error: BaseException | None = None
        buf: _OutputBuffer | None = None
        marker_bytes = b""
        guard: RunGuard | None = None

        def kill_launcher(process: asyncio.subprocess.Process, *, supervised: bool = False) -> None:
            if process.returncode is not None:
                return
            with contextlib.suppress(ProcessLookupError, OSError):
                os.killpg(process.pid, signal.SIGTERM if supervised else signal.SIGKILL)
            if not supervised:
                with contextlib.suppress(ProcessLookupError, OSError):
                    if process.returncode is None:
                        process.kill()

        async def host_cleanup_command(command: list[str]) -> int:
            cleanup = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                stdin=asyncio.subprocess.DEVNULL,
                close_fds=True,
                start_new_session=True,
                cwd=str(temp_dir),
                env={},
            )
            try:
                return await asyncio.wait_for(cleanup.wait(), timeout=3)
            finally:
                kill_launcher(cleanup)
                await cleanup.wait()

        async def runtime_cleanup_command(*args: str) -> int:
            return await host_cleanup_command([*runtime_cmd, *args])

        def task_mounts() -> list[str]:
            # mountinfo escapes whitespace and backslashes as octal sequences.
            import re

            mounts = []
            for line in Path("/proc/self/mountinfo").read_text().splitlines():
                raw = line.split()[4]
                path = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), raw)
                if path.startswith(str(temp_dir) + "/") or path == str(temp_dir):
                    mounts.append(path)
            return mounts

        async def cleanup_all() -> list[str]:
            nonlocal proc
            errors: list[str] = []
            # Cancellation during spawn must still acquire and reap the new process.
            if proc is None and spawn_task is not None:
                try:
                    proc = await spawn_task
                    self._active_pids.add(proc.pid)
                except Exception as exc:
                    errors.append(f"spawn: {exc}")
            for task in io_tasks:
                task.cancel()
            if proc is not None:
                kill_launcher(proc, supervised=True)
            await asyncio.gather(*io_tasks, return_exceptions=True)
            if spawn_task is not None:
                # Always attempt both operations, even if kill fails or the guest exited.
                try:
                    await runtime_cleanup_command("kill", "--all", container_id, "KILL")
                except Exception as exc:
                    errors.append(f"kill: {exc}")
                try:
                    rc = await runtime_cleanup_command("delete", "--force", container_id)
                    if rc and (started or primary_error is not None):
                        errors.append(f"delete --force exited {rc}")
                except Exception as exc:
                    errors.append(f"delete: {exc}")
            if proc is not None:
                try:
                    # Runtime deletion also stops a privileged test-profile child
                    # that the service cannot signal directly. Let its supervisor reap.
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(proc.wait(), timeout=1.5)
                    await _reap_process(proc)
                except Exception as exc:
                    errors.append(f"reap: {exc}")
            # runsc --network=none pins this namespace in its state root, outside
            # the container lifecycle. Unmount only this run's known runtime mount.
            try:
                null_netns = str(temp_dir / "state" / "null-netns")
                if null_netns in task_mounts():
                    rc = await host_cleanup_command(
                        [*cmd_prefix, "/usr/bin/umount", "--", null_netns]
                    )
                    if rc:
                        errors.append(f"umount null-netns exited {rc}")
                remaining = task_mounts()
                if remaining:
                    raise OSError(f"host mounts remain under task directory: {remaining}")
                if payload.exists():
                    payload.chmod(0o700)
                    for item in payload.rglob("*"):
                        if item.is_dir():
                            item.chmod(0o700)
                if cmd_prefix:
                    # Test-only sudo runsc may leave root-owned state. This path
                    # was allocated by us, and every mount has been removed above.
                    rc = await host_cleanup_command(
                        [
                            *cmd_prefix,
                            "/usr/bin/rm",
                            "-rf",
                            "--one-file-system",
                            "--",
                            str(temp_dir),
                        ]
                    )
                    if rc or temp_dir.exists():
                        raise OSError(f"task directory removal failed (exit {rc}): {temp_dir}")
                else:
                    shutil.rmtree(temp_dir)
            except Exception as exc:
                errors.append(f"task directory cleanup: {exc}")
            return errors

        try:
            mark_gvisor_task(temp_dir, identity, container_id)
            guard = RunGuard(
                task=temp_dir, runtime=runtime_cmd, prefix=cmd_prefix, container=container_id
            )
            bundle_dir = temp_dir / "bundle"
            bundle_dir.mkdir()
            input_dir = payload / "files"
            input_dir.mkdir(parents=True)
            validate_mount_files(dict(files), input_dir)
            (payload / "code.py").write_text(request.code, encoding="utf-8")
            for item in payload.rglob("*"):
                item.chmod(0o555 if item.is_dir() else 0o444)
            payload.chmod(0o555)
            start_marker = f"__nexura_sandbox_started_{uuid.uuid4().hex}__"
            marker_bytes = (start_marker + "\n").encode()
            # Reuse the rootfs's standard /tmp mount point for read-only payloads, avoiding
            # a new mount-point directory in an operator-owned read-only rootfs. TMPDIR
            # points to /sandbox; only links are placed in that guest scratch tmpfs.
            bootstrap = (
                "import os, runpy, sys\n"
                "for name in os.listdir('/tmp/files'):\n"
                "    os.symlink('/tmp/files/' + name, '/sandbox/' + name)\n"
                f"sys.stdout.write({start_marker!r} + '\\n')\n"
                "sys.stdout.flush()\n"
                "sys.argv = ['/tmp/code.py']\n"
                "runpy.run_path('/tmp/code.py', run_name='__main__')\n"
            )
            oci_config = build_gvisor_oci_spec(
                config,
                rootfs_path,
                payload,
                container_id,
                bootstrap,
            )
            (bundle_dir / "config.json").write_text(json.dumps(oci_config), encoding="utf-8")
            cmd = [
                *cmd_prefix,
                sys.executable,
                "-I",
                "-S",
                str(Path(__file__).resolve()),
                "--supervise",
                json.dumps(
                    {
                        "parent_pid": os.getpid(),
                        "parent_start": process_start(os.getpid()),
                        "fds": [],
                    }
                ),
                str(runsc_path),
                f"--root={temp_dir / 'state'}",
                "--oci-seccomp",
                "--network=sandbox" if config.allow_network else "--network=none",
            ]
            if limits_advisory:
                cmd.append("--ignore-cgroups")
            cmd += ["run", f"--bundle={bundle_dir}", container_id]
            buf = _OutputBuffer(config.max_output_bytes + len(marker_bytes))

            async def feed_stdin() -> None:
                assert proc and proc.stdin
                try:
                    proc.stdin.write(request.stdin.encode("utf-8"))
                    await proc.stdin.drain()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    proc.stdin.close()

            try:
                async with asyncio.timeout_at(start_time + config.timeout_seconds):
                    spawn_task = asyncio.create_task(
                        asyncio.create_subprocess_exec(
                            *cmd,
                            stdin=asyncio.subprocess.PIPE,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE,
                            cwd=str(temp_dir),
                            env={},
                            close_fds=True,
                            start_new_session=True,
                        )
                    )
                    proc = await asyncio.shield(spawn_task)
                    self._active_pids.add(proc.pid)
                    io_tasks = [
                        asyncio.create_task(_read_stream(proc.stdout, True, buf, proc)),
                        asyncio.create_task(_read_stream(proc.stderr, False, buf, proc)),
                        asyncio.create_task(feed_stdin()),
                        asyncio.create_task(proc.wait()),
                    ]
                    while not all(task.done() for task in io_tasks):
                        if guard is not None:
                            _ = guard.ready  # Detect watchdog failure while the container runs.
                        _check_cancellation(cancellation_token)
                        await asyncio.wait(
                            io_tasks, timeout=0.05, return_when=asyncio.FIRST_EXCEPTION
                        )
                        for task in io_tasks:
                            if task.done():
                                task.result()
                        if buf.capped:
                            raise OutputLimitExceededError(config.max_output_bytes, buf.total_bytes)
            except TimeoutError as exc:
                raise ExecutionTimeoutError(config.timeout_seconds, worker_id=worker_id) from exc
            if buf.capped:
                raise OutputLimitExceededError(config.max_output_bytes, buf.total_bytes)
            started = bytes(buf.stdout_bytes).startswith(marker_bytes)
            stdout_raw = bytes(buf.stdout_bytes)
            if started:
                stdout_raw = stdout_raw[len(marker_bytes) :]
            stderr_str = buf.stderr_bytes.decode("utf-8", errors="replace")
            if proc.returncode != 0 and not started:
                reason = (
                    "Host denied gVisor isolation; runsc exited before the sandboxed "
                    f"interpreter started (exit code {proc.returncode}): {stderr_str[:2000]}. "
                    "Production requires a delegated cgroup parent and runsc --oci-seccomp support"
                )
                raise CapabilityUnavailableError(reason, backend=self.backend_name)
            return ExecutionResult(
                exit_code=proc.returncode if proc.returncode is not None else 1,
                stdout=stdout_raw.decode("utf-8", errors="replace"),
                stderr=stderr_str,
                duration_ms=round((time.monotonic() - start_time) * 1000, 2),
                worker_id=worker_id,
                isolated=True,
                backend=self.backend_name,
                isolation=self.isolation,
                details={
                    "container_id": container_id,
                    "readonly_root": True,
                    "network_isolated": not config.allow_network,
                    "cgroups": "ignored_test_profile" if limits_advisory else "enforced",
                    "limits_advisory": limits_advisory,
                    "guest_nproc": config.max_processes,
                    "host_pids_limit": config.max_processes + config.gvisor_runtime_pids,
                    "tmpfs_bytes": config.gvisor_tmpfs_bytes,
                    "single_process_seccomp": config.max_processes == 1,
                },
            )
        finally:
            primary_error = sys.exception()
            if buf is not None:
                started = bytes(buf.stdout_bytes).startswith(marker_bytes)
            cleanup_task = asyncio.create_task(cleanup_all())
            interrupted = False
            errors: list[str] = []
            try:
                # Repeated task.cancel() cannot abandon teardown or leave a background task
                # that later tries to use a closed event loop.
                while not cleanup_task.done():
                    try:
                        await asyncio.shield(cleanup_task)
                    except asyncio.CancelledError:
                        interrupted = True
                errors = cleanup_task.result()
            finally:
                if proc is not None:
                    self._active_pids.discard(proc.pid)
                if guard is not None:
                    try:
                        guard.close()
                    except Exception as exc:
                        errors.append(f"crash watchdog: {exc}")
            if errors:
                message = "gVisor teardown: " + "; ".join(errors)
                if primary_error is not None:
                    primary_error.add_note(message)
                else:
                    raise CapabilityUnavailableError(message, backend=self.backend_name)
            if interrupted and not isinstance(primary_error, asyncio.CancelledError):
                raise asyncio.CancelledError
