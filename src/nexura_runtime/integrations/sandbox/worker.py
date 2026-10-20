# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Shared worker plumbing and the non-isolating workers.

Request validation, bounded output and process reaping used by every backend, plus the
trusted subprocess and mock workers (never selected automatically). The gVisor, bubblewrap
and WASI backends live in the ``nexura-sandbox`` package; Monty stays in this runtime.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Protocol

try:
    import resource
except ImportError:
    resource = None  # type: ignore[assignment]

from nexura_agents.errors import ExecutionCancelledError

from nexura_runtime.integrations.sandbox.backends import BackendStatus
from nexura_runtime.integrations.sandbox.errors import (
    CapabilityUnavailableError,
    ExecutionTimeoutError,
    OutputLimitExceededError,
    SecurityViolationError,
)
from nexura_runtime.integrations.sandbox.models import (
    ExecutionRequest,
    ExecutionResult,
    IsolationLevel,
    SandboxConfig,
)


def _start_temp_cleanup(directory: str, control_fd: int) -> subprocess.Popen[bytes]:
    """Remove Monty's owned launch directory after close or service pipe EOF.

    Kept with the reviewed host process launches. This fixed stdlib child receives
    no guest code, environment, stderr or service file descriptors.
    """
    code = (
        "import os, shutil, sys\n"
        "from pathlib import Path\n"
        "os.read(int(sys.argv[1]), 1)\n"
        "root = Path(sys.argv[2])\n"
        "try: (root / 'cwd').chmod(0o700)\n"
        "except FileNotFoundError: pass\n"
        "shutil.rmtree(root)\n"
    )
    return subprocess.Popen(
        [sys.executable, "-I", "-S", "-c", code, str(control_fd), directory],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        pass_fds=(control_fd,),
        start_new_session=True,
        cwd="/",
        env={},
    )


class WorkerBackend(Protocol):
    """Protocol for sandbox worker isolation backends."""

    async def execute(
        self,
        request: ExecutionRequest,
        config: SandboxConfig,
        *,
        cancellation_token: Any | None = None,
    ) -> ExecutionResult:
        """Execute request inside worker boundary."""
        ...


def validate_request_language(language: str, backend: str) -> None:
    """Ensure requested language runtime is supported before spawning."""
    normalized = language.strip().lower()
    if normalized not in ("python", "python3"):
        raise CapabilityUnavailableError(
            f"Unsupported execution language '{language}'. "
            "Sandbox execution currently only supports 'python'.",
            backend=backend,
        )


def validate_virtual_paths(files: dict[str, str]) -> list[tuple[str, str]]:
    """Check input file names without touching a disk; return ``(relative path, content)``.

    The same rules as ``validate_mount_files``: no NUL, no absolute path, no ``..`` part, no
    empty or ``.``-only name. Used by backends that hand files to the code as virtual
    (in-memory) files.
    """
    checked: list[tuple[str, str]] = []
    for rel_path, content in files.items():
        if not isinstance(rel_path, str) or not isinstance(content, str):
            raise SecurityViolationError(
                "path_escape", "Input file names and contents must be strings."
            )
        if "\x00" in rel_path:
            raise SecurityViolationError(
                "path_escape",
                f"Mount path '{rel_path}' contains null bytes.",
            )
        p = Path(rel_path)
        if p.is_absolute() or rel_path.startswith(("/", "\\")):
            raise SecurityViolationError(
                "path_escape",
                f"Mount path '{rel_path}' is an absolute path. "
                "Only relative paths inside scratch are permitted.",
            )
        if ".." in p.parts or ".." in rel_path.replace("\\", "/").split("/"):
            raise SecurityViolationError(
                "path_escape",
                f"Mount path '{rel_path}' contains parent directory traversal ('..').",
            )
        if not [part for part in p.parts if part not in ("", ".")]:
            raise SecurityViolationError(
                "path_escape", f"Mount path '{rel_path}' does not name a file."
            )
        checked.append((p.as_posix(), content))
    return checked


def validate_mount_files(files: dict[str, str], scratch_dir: Path) -> None:
    """Validate virtual mount paths using strict path ancestry and write them."""
    scratch_resolved = scratch_dir.resolve()
    for rel_path, content in files.items():
        if "\x00" in rel_path:
            raise SecurityViolationError(
                "path_escape",
                f"Mount path '{rel_path}' contains null bytes.",
            )
        p = Path(rel_path)
        if p.is_absolute():
            raise SecurityViolationError(
                "path_escape",
                f"Mount path '{rel_path}' is an absolute path. "
                "Only relative paths inside scratch are permitted.",
            )
        if ".." in p.parts:
            raise SecurityViolationError(
                "path_escape",
                f"Mount path '{rel_path}' contains parent directory traversal ('..').",
            )
        dest = (scratch_dir / rel_path).resolve()
        if not dest.is_relative_to(scratch_resolved) or dest == scratch_resolved:
            raise SecurityViolationError(
                "path_escape",
                f"Mount path '{rel_path}' resolves outside worker scratch directory.",
            )
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")


def build_scrubbed_env(
    config: SandboxConfig,
    request_env: dict[str, str],
    scratch_dir: Path,
) -> dict[str, str]:
    """Build scrubbed environment retaining only allowlisted keys and stripping credentials."""
    clean_env: dict[str, str] = {}
    for key in config.env_allowlist:
        if key in os.environ:
            clean_env[key] = os.environ[key]

    for k, v in request_env.items():
        if "\x00" in k or "\x00" in v or "=" in k:
            continue
        # Disallow host credential injection
        if any(
            hint in k.upper() for hint in ("KEY", "SECRET", "TOKEN", "AUTH", "PASS", "CREDENTIAL")
        ):
            continue
        # Disallow proxy or network variables when network is denied
        if not config.allow_network and any(p in k.upper() for p in ("PROXY", "SOCKS", "GATEWAY")):
            continue
        clean_env[k] = v

    clean_env["PYTHONUNBUFFERED"] = "1"
    clean_env["PYTHONDONTWRITEBYTECODE"] = "1"
    clean_env["TMPDIR"] = str(scratch_dir)
    return clean_env


class _OutputBuffer:
    """Thread-safe bounded output collector that kills process immediately on limit cap."""

    def __init__(self, max_bytes: int) -> None:
        self.max_bytes = max_bytes
        self.stdout_bytes = bytearray()
        self.stderr_bytes = bytearray()
        self.total_bytes = 0
        self.capped = False
        self.limit_reached = asyncio.Event()
        self._lock = asyncio.Lock()

    async def append_stdout(self, chunk: bytes, proc: asyncio.subprocess.Process) -> None:
        async with self._lock:
            self.total_bytes += len(chunk)
            if self.total_bytes > self.max_bytes:
                self.capped = True
                self.limit_reached.set()
                try:
                    if proc.returncode is None:
                        proc.kill()
                except (ProcessLookupError, OSError):
                    pass
                remaining = max(
                    0,
                    self.max_bytes - len(self.stdout_bytes) - len(self.stderr_bytes),
                )
                self.stdout_bytes.extend(chunk[:remaining])
            else:
                self.stdout_bytes.extend(chunk)

    async def append_stderr(self, chunk: bytes, proc: asyncio.subprocess.Process) -> None:
        async with self._lock:
            self.total_bytes += len(chunk)
            if self.total_bytes > self.max_bytes:
                self.capped = True
                self.limit_reached.set()
                try:
                    if proc.returncode is None:
                        proc.kill()
                except (ProcessLookupError, OSError):
                    pass
                remaining = max(
                    0,
                    self.max_bytes - len(self.stdout_bytes) - len(self.stderr_bytes),
                )
                self.stderr_bytes.extend(chunk[:remaining])
            else:
                self.stderr_bytes.extend(chunk)


async def _read_stream(
    stream: asyncio.StreamReader | None,
    is_stdout: bool,
    buf: _OutputBuffer,
    proc: asyncio.subprocess.Process,
) -> None:
    """Stream reader loop feeding the bounded output buffer."""
    if stream is None:
        return
    while True:
        chunk = await stream.read(4096)
        if not chunk:
            break
        if is_stdout:
            await buf.append_stdout(chunk, proc)
        else:
            await buf.append_stderr(chunk, proc)
        if buf.capped:
            break


async def _reap_process(
    proc: asyncio.subprocess.Process,
    reader_tasks: list[asyncio.Task[Any]] | None = None,
) -> None:
    """Ensure process is terminated, reader tasks are awaited, and pipes are fully reaped."""
    try:
        if proc.returncode is None:
            proc.kill()
    except (ProcessLookupError, OSError):
        pass

    if reader_tasks:
        for t in reader_tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*reader_tasks, return_exceptions=True)

    if proc.stdin and not proc.stdin.is_closing():
        with contextlib.suppress(Exception):
            proc.stdin.close()

    async def drain(stream: asyncio.StreamReader | None) -> None:
        if stream is not None:
            while await stream.read(4096):
                pass

    await asyncio.wait_for(asyncio.gather(drain(proc.stdout), drain(proc.stderr)), timeout=10)
    with contextlib.suppress(Exception):
        await proc.wait()


def _check_cancellation(cancellation_token: Any | None) -> None:
    """Check cancellation token from ExecutionContext or external caller."""
    if cancellation_token is None:
        return
    if hasattr(cancellation_token, "check_cancelled"):
        cancellation_token.check_cancelled()
    elif hasattr(cancellation_token, "is_cancelled") and cancellation_token.is_cancelled:
        raise ExecutionCancelledError("Execution was cancelled")


class TrustedSubprocessWorker:
    """Subprocess execution worker for explicitly trusted code only.

    Important Security Notice:
    Reports isolated=False. This mode executes Python in a child process with basic
    resource limits (RLIMIT_AS, timeout, output cap) and environment scrubbing.
    It does NOT provide genuine OCI sandbox isolation and makes no claims to safely
    contain untrusted or adversarial code.
    """

    #: Not an isolation boundary: never selected from ``SandboxConfig.backends`` and refused
    #: whenever the operator sets ``min_isolation``. Runs only when passed as ``worker=``.
    isolation: IsolationLevel = "none"
    languages = frozenset({"python", "python3"})
    enforced_limits = frozenset({"wall_time", "memory", "output"})

    def __init__(self, backend_name: str = "trusted_subprocess") -> None:
        self.backend_name = backend_name
        self._active_pids: set[int] = set()

    @property
    def active_worker_count(self) -> int:
        """Number of active running worker processes."""
        return len(self._active_pids)

    def probe(self, config: SandboxConfig) -> BackendStatus:
        """Always available, and always reported as not isolating."""
        return BackendStatus(self.backend_name, True, "trusted code only; not a sandbox")

    async def execute(
        self,
        request: ExecutionRequest,
        config: SandboxConfig,
        *,
        cancellation_token: Any | None = None,
    ) -> ExecutionResult:
        _check_cancellation(cancellation_token)
        validate_request_language(request.language, self.backend_name)

        worker_id = f"proc_{uuid.uuid4().hex[:12]}"
        temp_dir = Path(tempfile.mkdtemp(prefix="nexura_proc_"))
        proc: asyncio.subprocess.Process | None = None
        start_time = time.perf_counter()

        try:
            # 1. Validate and mount virtual files with real path ancestry
            validate_mount_files(request.files, temp_dir)

            # 2. Build scrubbed environment
            clean_env = build_scrubbed_env(config, request.env, temp_dir)

            # 3. Write runner wrapper
            runner_script = temp_dir / "_sandbox_runner.py"
            code_file = temp_dir / "user_code.py"
            code_file.write_text(request.code, encoding="utf-8")

            bootstrap_code = self._generate_bootstrap(config, code_file.name, temp_dir)
            runner_script.write_text(bootstrap_code, encoding="utf-8")

            # 4. Spawn child process in isolated mode (-I -s)
            cmd = [sys.executable, "-I", "-s", str(runner_script)]

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE if request.stdin else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(temp_dir),
                env=clean_env,
            )

            if proc.pid:
                self._active_pids.add(proc.pid)

            if request.stdin and proc.stdin:
                with contextlib.suppress(Exception):
                    proc.stdin.write(request.stdin.encode("utf-8"))
                    await proc.stdin.drain()
                with contextlib.suppress(Exception):
                    proc.stdin.close()

            # 5. Stream stdout and stderr with bounded buffers
            buf = _OutputBuffer(config.max_output_bytes)
            stdout_task = asyncio.create_task(_read_stream(proc.stdout, True, buf, proc))
            stderr_task = asyncio.create_task(_read_stream(proc.stderr, False, buf, proc))

            try:
                await asyncio.wait_for(
                    asyncio.gather(stdout_task, stderr_task),
                    timeout=config.timeout_seconds,
                )
            except TimeoutError as exc:
                await _reap_process(proc, [stdout_task, stderr_task])
                raise ExecutionTimeoutError(config.timeout_seconds, worker_id=worker_id) from exc
            except (asyncio.CancelledError, KeyboardInterrupt):
                await _reap_process(proc, [stdout_task, stderr_task])
                raise

            with contextlib.suppress(Exception):
                await proc.wait()

            if buf.capped:
                raise OutputLimitExceededError(config.max_output_bytes, buf.total_bytes)

            duration_ms = (time.perf_counter() - start_time) * 1000.0

            stdout_str = buf.stdout_bytes.decode("utf-8", errors="replace")
            stderr_str = buf.stderr_bytes.decode("utf-8", errors="replace")

            # Check defense-in-depth security violations
            if "SECURITY_VIOLATION_FORK" in stderr_str:
                raise SecurityViolationError(
                    "fork_attempt",
                    "Process attempted unauthorized fork or sub-process creation.",
                )
            if "SECURITY_VIOLATION_NETWORK" in stderr_str:
                raise SecurityViolationError(
                    "network_attempt",
                    "Process attempted unauthorized network connection.",
                )
            if "SECURITY_VIOLATION_PATH_ESCAPE" in stderr_str:
                raise SecurityViolationError(
                    "path_escape",
                    "Process attempted unauthorized path escape outside workspace.",
                )

            return ExecutionResult(
                exit_code=proc.returncode if proc.returncode is not None else 1,
                stdout=stdout_str,
                stderr=stderr_str,
                truncated=False,
                duration_ms=round(duration_ms, 2),
                worker_id=worker_id,
                isolated=False,  # Truthful! Subprocess is NOT genuine isolation
                backend=self.backend_name,
                isolation=self.isolation,
                details={
                    "isolated": False,
                    "mode": "trusted_code",
                    "max_memory_mb": config.max_memory_mb,
                    "allow_network": config.allow_network,
                },
            )

        finally:
            if proc:
                await _reap_process(proc)
                if proc.pid:
                    self._active_pids.discard(proc.pid)
            shutil.rmtree(temp_dir, ignore_errors=True)

    def _generate_bootstrap(
        self,
        config: SandboxConfig,
        code_filename: str,
        scratch_dir: Path,
    ) -> str:
        """Generate Python bootstrap enforcing defense-in-depth resource and safety checks."""
        bootstrap_lines = [
            "import os, sys",
        ]

        # 1. Apply memory limit (RLIMIT_AS) on Linux
        if hasattr(resource, "RLIMIT_AS"):
            mem_bytes = config.max_memory_mb * 1024 * 1024
            bootstrap_lines.append(
                f"try:\n"
                f"    import resource\n"
                f"    resource.setrlimit(resource.RLIMIT_AS, ({mem_bytes}, {mem_bytes}))\n"
                f"except Exception:\n"
                f"    pass"
            )

        # 2. Block fork/process creation
        bootstrap_lines.append(
            "def _deny_fork(*args, **kwargs):\n"
            "    sys.stderr.write('SECURITY_VIOLATION_FORK\\n')\n"
            "    sys.stderr.flush()\n"
            "    raise PermissionError('Process creation is disabled.')\n"
            "os.fork = _deny_fork\n"
            "if hasattr(os, 'forkpty'): os.forkpty = _deny_fork\n"
            "if hasattr(os, 'posix_spawn'): os.posix_spawn = _deny_fork\n"
            "if hasattr(os, 'posix_spawnp'): os.posix_spawnp = _deny_fork\n"
            "try:\n"
            "    import subprocess\n"
            "    subprocess.Popen = _deny_fork\n"
            "except Exception:\n"
            "    pass\n"
            "try:\n"
            "    import _posixsubprocess\n"
            "    _posixsubprocess.fork_exec = _deny_fork\n"
            "except Exception:\n"
            "    pass\n"
        )

        # 3. Block network access if allow_network=False
        if not config.allow_network:
            bootstrap_lines.append(
                "def _deny_socket(*args, **kwargs):\n"
                "    sys.stderr.write('SECURITY_VIOLATION_NETWORK\\n')\n"
                "    sys.stderr.flush()\n"
                "    raise PermissionError('Network access is disabled.')\n"
                "try:\n"
                "    import socket\n"
                "    socket.socket = _deny_socket\n"
                "    socket.create_connection = _deny_socket\n"
                "    socket.getaddrinfo = _deny_socket\n"
                "except Exception:\n"
                "    pass\n"
                "try:\n"
                "    import _socket\n"
                "    _socket.socket = _deny_socket\n"
                "except Exception:\n"
                "    pass\n"
            )

        # 4. Guard against path escape via open
        scratch_str = repr(str(scratch_dir.resolve()))
        escape_targets = "('/etc/shadow', '/etc/passwd', '/etc/sudoers', '../', '..\\\\')"
        bootstrap_lines.append(
            f"import builtins, os\n"
            f"_orig_open = builtins.open\n"
            f"_scratch_dir = {scratch_str}\n"
            f"def _guarded_open(file, *args, **kwargs):\n"
            f"    s = str(file)\n"
            f"    if any(k in s for k in {escape_targets}):\n"
            f"        sys.stderr.write('SECURITY_VIOLATION_PATH_ESCAPE\\n')\n"
            f"        sys.stderr.flush()\n"
            f"        raise PermissionError('Path escape outside workspace denied.')\n"
            f"    return _orig_open(file, *args, **kwargs)\n"
            f"builtins.open = _guarded_open\n"
        )

        # 5. Execute user code file
        bootstrap_lines.append(
            f"with open({code_filename!r}, 'rb') as f:\n"
            f"    code = compile(f.read(), {code_filename!r}, 'exec')\n"
            f"exec(code, {{'__name__': '__main__', '__file__': {code_filename!r}}})\n"
        )

        return "\n".join(bootstrap_lines)


class IsolatedProcessWorker(TrustedSubprocessWorker):
    """Subprocess execution worker for explicitly trusted code execution.

    Preserved for backward compatibility. Reports isolated=False.
    Does NOT isolate untrusted code against malicious escapes.
    """

    def __init__(self, backend_name: str = "isolated_process") -> None:
        super().__init__(backend_name=backend_name)


class MockDisposableWorker:
    """Mock worker for offline fixture testing and simulated environments.

    Clearly reports isolated=False and details['mock']=True.
    """

    #: A fixture, not a boundary: never selected; runs only when passed as ``worker=``.
    isolation: IsolationLevel = "none"
    languages = frozenset({"python", "python3"})
    enforced_limits: frozenset[str] = frozenset()

    def __init__(self, backend_name: str = "mock_disposable") -> None:
        self.backend_name = backend_name
        self.active_worker_count = 0

    def probe(self, config: SandboxConfig) -> BackendStatus:
        """Always available; a deterministic offline fixture."""
        return BackendStatus(self.backend_name, True, "offline fixture; not a sandbox")

    async def execute(
        self,
        request: ExecutionRequest,
        config: SandboxConfig,
        *,
        cancellation_token: Any | None = None,
    ) -> ExecutionResult:
        _check_cancellation(cancellation_token)
        validate_request_language(request.language, self.backend_name)

        # Reject path escapes in files
        for rel_path in request.files:
            if ".." in rel_path or rel_path.startswith("/"):
                raise SecurityViolationError(
                    "path_escape",
                    f"Mount path '{rel_path}' attempts path traversal outside scratch.",
                )

        worker_id = f"mock_{uuid.uuid4().hex[:10]}"
        self.active_worker_count += 1

        try:
            # Check for simulated infinite loop
            if "while True" in request.code and "break" not in request.code:
                raise ExecutionTimeoutError(config.timeout_seconds, worker_id=worker_id)

            # Check for simulated fork attempt
            if any(f in request.code for f in ("os.fork", "posix_spawn", "subprocess")):
                raise SecurityViolationError(
                    "fork_attempt",
                    "Process attempted unauthorized fork or sub-process creation.",
                )

            # Check for simulated network attempt
            if not config.allow_network and any(
                net in request.code
                for net in (
                    "_socket",
                    "socket",
                    "urllib",
                    "requests",
                    "http",
                    "aiohttp",
                    "httpx",
                )
            ):
                raise SecurityViolationError(
                    "network_attempt",
                    "Process attempted unauthorized network connection.",
                )

            # Check for simulated huge output
            if "100_000_000" in request.code:
                raise OutputLimitExceededError(config.max_output_bytes, 100_000_000)

            # Check for simulated path escape
            if (
                "/etc/shadow" in request.code
                or "/etc/passwd" in request.code
                or "../" in request.code
            ):
                raise SecurityViolationError(
                    "path_escape", "Access outside scoped sandbox mount denied."
                )

            stdout = "42\n"
            if "print(" in request.code:
                stdout = "Calculation result: 42\n"

            return ExecutionResult(
                exit_code=0,
                stdout=stdout,
                stderr="",
                duration_ms=1.5,
                worker_id=worker_id,
                isolated=False,  # Mock mode is NOT isolated
                backend=self.backend_name,
                isolation=self.isolation,
                details={"mock": True},
            )
        finally:
            self.active_worker_count -= 1
