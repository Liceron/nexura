# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Monty backend: pydantic's Rust Python-subset interpreter in a separate worker process.

Isolation level ``interpreter``. Monty has no filesystem, network, subprocess or environment
access unless the host hands one in, and this backend hands in none: no host functions, no
mounts, no environment. Input files are read-only in-memory copies. Each call starts a fresh
pool with one ``monty`` worker process (spawned by pydantic-monty with an empty environment),
runs the code once and discards the worker, so nothing is shared between calls.

Limits come from the operator ``SandboxConfig``: ``timeout_seconds`` bounds execution time
(Monty's ``max_feed_duration_secs``), total sleep, the pool watchdog (``request_timeout``)
and a host-side deadline; ``max_memory_mb`` bounds Monty's heap and, as a backstop on
Linux, the worker's address space (``RLIMIT_AS``, with headroom); ``max_output_bytes`` caps
``print()`` output plus error/traceback text returned to the caller. The worker also gets
``RLIMIT_FSIZE=0``, ``RLIMIT_CORE=0`` and a small ``RLIMIT_NOFILE`` where ``prlimit`` exists.

On POSIX the worker is started through a one-shot exec wrapper so it inherits no host file
descriptors beyond its stdin/stdout pipes, runs in an empty scratch directory, and writes its
own stderr (crash messages) to a pipe this process reads, never to the host's stderr. The
value of a trailing bare expression is never sent back: the code runs with ``None`` as its
last expression. Limit outcomes (timeout, memory) are recognised only from errors Monty
raises outside any user frame, so code that raises look-alike errors cannot fake them.

Monty runs a subset of Python (no class inheritance, no generators, no third-party
packages, a partial standard library, no stdin); unsupported features raise
``UnsupportedFeatureError`` (module error ``UNSUPPORTED_FEATURE``), not a crash. It needs the
optional extra: ``pip install "nexura-runtime[sandbox]"``.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shlex
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Literal

try:
    import resource
except ImportError:  # pragma: no cover - Windows
    resource = None  # type: ignore[assignment]

from nexura_runtime.integrations.sandbox.backends import BackendStatus
from nexura_runtime.integrations.sandbox.errors import (
    CapabilityUnavailableError,
    ExecutionTimeoutError,
    MemoryLimitExceededError,
    OutputLimitExceededError,
    UnsupportedFeatureError,
    WorkerCrashError,
)
from nexura_runtime.integrations.sandbox.models import (
    ExecutionRequest,
    ExecutionResult,
    IsolationLevel,
    SandboxConfig,
)
from nexura_runtime.integrations.sandbox.worker import (
    _check_cancellation,
    _start_temp_cleanup,
    validate_request_language,
    validate_virtual_paths,
)

_MIB = 1024 * 1024
#: Virtual directory the input files appear under; also the code's working directory.
SANDBOX_ROOT = "/sandbox"
#: Seconds past ``timeout_seconds`` before the pool watchdog kills the worker.
_WATCHDOG_GRACE_SECONDS = 1.0
#: Seconds past ``timeout_seconds`` before this process abandons the worker (last backstop).
_HOST_GRACE_SECONDS = 3.0
#: Address-space headroom over the heap limit for the worker binary, its stacks and maps.
_ADDRESS_SPACE_HEADROOM_MB = 512
_WORKER_MAX_OPEN_FILES = 16
#: Most bytes of the worker's own stderr (crash diagnostics) kept per call.
_WORKER_STDERR_BYTES = 4096
#: Appended to error text that was cut to fit the output limit.
_TRUNCATION_NOTE = b"\n[truncated: output limit reached]\n"
#: Appended to the code so its last expression is ``None``: the value of a trailing bare
#: expression stays in the worker instead of being serialised back into this process.
_DISCARD_RESULT_SUFFIX = "\n\nNone\n"


def installed_monty_binary() -> Path | None:
    """The ``monty`` binary pydantic-monty installed next to this interpreter, if any.

    Only this environment's scripts directory is checked: ``PATH``, ``MONTY_BIN`` and the
    current directory never choose the worker binary.
    """
    name = "monty.exe" if sys.platform == "win32" else "monty"
    candidate = Path(sysconfig.get_path("scripts")) / name
    return candidate if candidate.is_file() else None


def resolve_monty_binary(config: SandboxConfig) -> Path | None:
    """Operator ``monty_binary_path`` if set, else the binary installed with pydantic-monty."""
    if config.monty_binary_path:
        return Path(config.monty_binary_path)
    return installed_monty_binary()


class _BoundedOutput:
    """Collects ``print()`` output up to ``max_bytes``; past it, flags the cap and stops.

    pydantic-monty may call the print callback from a worker I/O thread, so the buffers are
    guarded by a lock and the event loop is woken with ``call_soon_threadsafe``.
    """

    class CapReached(Exception):
        """Raised from the print callback once the cap is passed, to end the feed."""

    def __init__(self, max_bytes: int, loop: asyncio.AbstractEventLoop) -> None:
        self.max_bytes = max_bytes
        self.total_bytes = 0
        self.stdout = bytearray()
        self.stderr = bytearray()
        self.capped = False
        self.limit_reached = asyncio.Event()
        self._loop = loop
        self._lock = threading.Lock()

    def write(self, stream: Literal["stdout", "stderr"], text: str) -> None:
        with self._lock:
            if self.capped:
                raise self.CapReached("output limit reached")
            data = text.encode("utf-8", errors="replace")
            self.total_bytes += len(data)
            room = max(0, self.max_bytes - len(self.stdout) - len(self.stderr))
            target = self.stdout if stream == "stdout" else self.stderr
            target.extend(data[:room])
            if self.total_bytes <= self.max_bytes:
                return
            self.capped = True
        with contextlib.suppress(RuntimeError):  # the loop is already closed
            self._loop.call_soon_threadsafe(self.limit_reached.set)
        raise self.CapReached("output limit reached")


def _apply_worker_rlimits(pid: int | None, config: SandboxConfig) -> dict[str, Any]:
    """Lower the worker's OS limits before any code runs (Linux ``prlimit``; else no-op)."""
    if pid is None or resource is None or not hasattr(resource, "prlimit"):
        return {"applied": False}
    address_space = (config.max_memory_mb + _ADDRESS_SPACE_HEADROOM_MB) * _MIB
    limits = {
        "RLIMIT_AS": address_space,
        "RLIMIT_FSIZE": 0,
        "RLIMIT_CORE": 0,
        "RLIMIT_NOFILE": _WORKER_MAX_OPEN_FILES,
    }
    for name, value in limits.items():
        resource.prlimit(pid, getattr(resource, name), (value, value))
    return {"applied": True, **limits}


def _clip_text(text: str, room: int) -> tuple[bytes, bool]:
    """``text`` as UTF-8 cut to ``room`` bytes (with a note when cut); True when cut."""
    data = text.encode("utf-8", errors="replace")
    if len(data) <= room:
        return data, False
    head = data[: max(0, room - len(_TRUNCATION_NOTE))]
    # Drop a partial trailing UTF-8 sequence before adding the note.
    head = head.decode("utf-8", errors="ignore").encode("utf-8")
    return (head + _TRUNCATION_NOTE)[:room], True


def _frame_count(exc: Any) -> int:
    """Traceback frames of a Monty error; -1 when unknown (treated as user code)."""
    try:
        return len(exc.traceback())
    except Exception:
        return -1


def _is_limit_message(message: str, kind: str) -> bool:
    lowered = message.lower()
    return f"{kind} limit exceeded" in lowered


def _is_memory_limit_message(message: str) -> bool:
    """Monty's heap limit error, or the pool's report that it killed the worker for memory."""
    lowered = message.lower()
    return "memory limit exceeded" in lowered or "exceeded its memory limit" in lowered


class _WorkerLaunch:
    """Starts the ``monty`` worker with no inherited host state (POSIX).

    pydantic-monty spawns the worker itself with an empty environment but the host's working
    directory, stderr and every inheritable file descriptor. This writes a one-shot wrapper
    (``/bin/sh`` running this interpreter with ``-I -S``) into a private temporary directory;
    the wrapper changes to an empty, read-only scratch directory, points stderr at a FIFO
    this process reads, closes every descriptor above 2 and then ``exec``s the real binary,
    so the worker keeps its pid and only its stdin/stdout pipes. ``close`` removes it
    all. Where this is unavailable (Windows, no ``/bin/sh``) the binary is spawned directly
    and ``mode`` reports ``direct``.
    """

    def __init__(self, binary: Path) -> None:
        self.binary = binary
        self.mode = "direct"
        self._dir: str | None = None
        self._stderr_fd: int | None = None
        self._stderr = bytearray()
        self._cleanup_process: subprocess.Popen[bytes] | None = None
        self._cleanup_write: int | None = None

    def start(self) -> str:
        """Prepare the wrapper; returns the path to hand pydantic-monty as the binary."""
        shell = Path("/bin/sh")
        if (
            os.name != "posix"
            or not hasattr(os, "mkfifo")
            or not sys.executable
            or not shell.is_file()
        ):
            return str(self.binary)
        try:
            self._dir = tempfile.mkdtemp(prefix="nexura-monty-")
            scratch = Path(self._dir) / "cwd"
            scratch.mkdir(mode=0o700)
            scratch.chmod(0o500)  # empty and not writable
            fifo = Path(self._dir) / "stderr"
            os.mkfifo(fifo, 0o600)
            # Opened before the spawn so the wrapper's blocking open for writing returns.
            self._stderr_fd = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
            wrapper = Path(self._dir) / "monty-launch"
            wrapper_code = (
                "import os, sys\n"
                f"os.chdir({str(scratch)!r})\n"
                f"fd = os.open({str(fifo)!r}, os.O_WRONLY)\n"
                "os.dup2(fd, 2)\n"
                "os.closerange(3, max(int(os.sysconf('SC_OPEN_MAX')), 1024))\n"
                f"os.execv({str(self.binary)!r}, [{str(self.binary)!r}, *sys.argv[1:]])\n"
            )
            wrapper.write_text(
                f"#!{shell}\n"
                f"exec {shlex.quote(sys.executable)} -I -S -c {shlex.quote(wrapper_code)} "
                '"$@"\n',
                encoding="utf-8",
            )
            wrapper.chmod(0o700)
            # Monty's pool can respawn through this wrapper. Retain it until pool
            # teardown, but independently remove the tree if the service crashes.
            cleanup_read, self._cleanup_write = os.pipe()  # Non-inheritable, also on macOS.
            try:
                self._cleanup_process = _start_temp_cleanup(self._dir, cleanup_read)
            finally:
                os.close(cleanup_read)
        except BaseException as exc:
            self._cleanup()
            if isinstance(exc, OSError):
                return str(self.binary)
            raise
        self.mode = "wrapped"
        return str(wrapper)

    def drain(self) -> None:
        """Keep up to ``_WORKER_STDERR_BYTES`` of what the worker wrote to stderr so far."""
        if self._stderr_fd is None:
            return
        while True:
            try:
                chunk = os.read(self._stderr_fd, 65536)
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                return
            if not chunk:
                return
            room = _WORKER_STDERR_BYTES - len(self._stderr)
            if room > 0:
                self._stderr.extend(chunk[:room])

    @property
    def stderr(self) -> str:
        return self._stderr.decode("utf-8", errors="replace")

    def _cleanup(self) -> None:
        try:
            if self._cleanup_write is not None:
                os.close(self._cleanup_write)
                self._cleanup_write = None
            if self._cleanup_process is not None:
                try:
                    self._cleanup_process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    self._cleanup_process.kill()
                    self._cleanup_process.wait(timeout=0.5)
                finally:
                    self._cleanup_process = None
        finally:
            if self._stderr_fd is not None:
                with contextlib.suppress(OSError):
                    os.close(self._stderr_fd)
                self._stderr_fd = None
            if self._dir is not None:
                with contextlib.suppress(FileNotFoundError):
                    (Path(self._dir) / "cwd").chmod(0o700)
                with contextlib.suppress(FileNotFoundError):
                    shutil.rmtree(self._dir)
                self._dir = None

    def close(self) -> None:
        """Collect the worker's remaining stderr and remove the temporary directory."""
        try:
            self.drain()
        finally:
            self._cleanup()


def _is_unsupported(message: str) -> bool:
    """Monty's own "not supported yet" errors (e.g. class inheritance, ``yield``)."""
    lowered = message.lower()
    return "monty" in lowered and ("not yet" in lowered or "not supported" in lowered)


class MontyWorker:
    """Monty backend (``interpreter`` isolation): Python subset in a disposable worker."""

    isolation: IsolationLevel = "interpreter"
    languages = frozenset({"python", "python3"})
    enforced_limits = frozenset(
        {
            "wall_time",
            "cpu_time",
            "memory",
            "output",
            "recursion",
            "sleep",
            "no_filesystem",
            "no_network",
            "no_subprocess",
            "no_environment",
        }
    )

    def __init__(self, backend_name: str = "monty") -> None:
        self.backend_name = backend_name
        self._active = 0

    @property
    def active_worker_count(self) -> int:
        """Number of Monty worker pools currently open (one worker each)."""
        return self._active

    def probe(self, config: SandboxConfig) -> BackendStatus:
        """Available when pydantic-monty is installed and its worker binary is pinned."""
        try:
            import pydantic_monty
        except ImportError:
            return BackendStatus(
                self.backend_name,
                False,
                "pydantic-monty is not installed; install the 'sandbox' extra "
                '(pip install "nexura-runtime[sandbox]")',
            )
        binary = resolve_monty_binary(config)
        if binary is None:
            return BackendStatus(
                self.backend_name,
                False,
                "the monty worker binary was not found next to this interpreter; set "
                "SandboxConfig.monty_binary_path",
            )
        if not binary.is_absolute():
            return BackendStatus(
                self.backend_name, False, f"monty_binary_path '{binary}' must be absolute"
            )
        if not binary.is_file() or not os.access(binary, os.X_OK):
            return BackendStatus(
                self.backend_name, False, f"monty worker binary '{binary}' is not executable"
            )
        return BackendStatus(self.backend_name, True, version=pydantic_monty.__version__)

    async def execute(
        self,
        request: ExecutionRequest,
        config: SandboxConfig,
        *,
        cancellation_token: Any | None = None,
    ) -> ExecutionResult:
        """Run ``request.code`` once in a fresh Monty worker within ``config``'s limits."""
        _check_cancellation(cancellation_token)
        validate_request_language(request.language, self.backend_name)
        status = self.probe(config)
        if not status.available:
            raise CapabilityUnavailableError(status.reason, backend=self.backend_name)
        if request.stdin:
            raise UnsupportedFeatureError(
                "stdin (Monty has no sys.stdin or input(); pass data as an input file)",
                backend=self.backend_name,
            )
        files = validate_virtual_paths(request.files)
        # Request env is never used: Monty code has no environment and the worker starts
        # with an empty one.

        import pydantic_monty as monty

        os_handler: Any = None
        if files:
            from nexura_runtime.integrations.sandbox._monty_files import ReadOnlyInputFiles

            os_handler = ReadOnlyInputFiles(files, root=SANDBOX_ROOT)

        timeout = config.timeout_seconds
        limits: monty.ResourceLimits = {
            "max_feed_duration_secs": timeout,
            "max_total_sleep_secs": timeout,
            "max_memory": config.max_memory_mb * _MIB,
        }
        os_policy: monty.OSPolicy = {
            "sleep": "system",
            "sleep_system_max": timeout,
            "process_time": "zero",
        }
        binary = resolve_monty_binary(config)
        assert binary is not None  # probe() checked it
        worker_id = f"monty_{uuid.uuid4().hex[:12]}"
        output = _BoundedOutput(config.max_output_bytes, asyncio.get_running_loop())
        exit_code = 0
        error_text = ""
        rlimits: dict[str, Any] = {}
        launch = _WorkerLaunch(binary)
        start_time = time.perf_counter()

        self._active += 1
        try:
            pool = monty.AsyncMonty(
                binary_path=launch.start(),
                min_processes=1,
                max_processes=1,
                checkout_timeout=timeout + _HOST_GRACE_SECONDS,
                request_timeout=timeout + _WATCHDOG_GRACE_SECONDS,
                max_checkouts_per_worker=1,
            )
            async with (
                pool,
                pool.checkout(
                    script_name="user_code.py", limits=limits, os_policy=os_policy
                ) as session,
            ):
                rlimits = _apply_worker_rlimits(session.worker_pid, config)
                feed = asyncio.ensure_future(
                    session.feed_run(
                        request.code + _DISCARD_RESULT_SUFFIX,
                        print_callback=output.write,
                        os=os_handler,
                        cwd=SANDBOX_ROOT if files else None,
                    )
                )
                capped = asyncio.ensure_future(output.limit_reached.wait())
                try:
                    done, _ = await asyncio.wait(
                        {feed, capped},
                        timeout=timeout + _HOST_GRACE_SECONDS,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    for task in (feed, capped):
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(feed, capped, return_exceptions=True)
                if output.capped:
                    raise OutputLimitExceededError(config.max_output_bytes, output.total_bytes)
                if feed not in done:
                    raise ExecutionTimeoutError(timeout, worker_id=worker_id)
                try:
                    feed.result()
                except monty.MontyCrashedError as exc:
                    if exc.timed_out:
                        raise ExecutionTimeoutError(timeout, worker_id=worker_id) from exc
                    crash = WorkerCrashError(exc.exit_status if exc.exit_status is not None else -1)
                    launch.drain()
                    crash.details["worker_stderr"] = launch.stderr
                    raise crash from exc
                except monty.MontySyntaxError as exc:
                    exit_code, error_text = 1, exc.display("traceback")
                except monty.MontyRuntimeError as exc:
                    inner = exc.exception()
                    message = exc.display("msg")
                    # Monty raises its own limit errors outside any user frame; a user
                    # ``raise`` always has one, so look-alike text cannot fake a limit.
                    interpreter_raised = _frame_count(exc) == 0
                    if (
                        interpreter_raised
                        and isinstance(inner, TimeoutError)
                        and (
                            _is_limit_message(message, "time")
                            or _is_limit_message(message, "sleep")
                        )
                    ):
                        raise ExecutionTimeoutError(timeout, worker_id=worker_id) from exc
                    if (
                        interpreter_raised
                        and isinstance(inner, MemoryError)
                        and _is_memory_limit_message(message)
                    ):
                        raise MemoryLimitExceededError(
                            config.max_memory_mb, worker_id=worker_id
                        ) from exc
                    if isinstance(inner, NotImplementedError) and _is_unsupported(message):
                        raise UnsupportedFeatureError(message, backend=self.backend_name) from exc
                    exit_code, error_text = 1, exc.display("traceback")
                except monty.MontyError as exc:
                    exit_code, error_text = 1, str(exc)
        except FileNotFoundError as exc:
            raise CapabilityUnavailableError(
                f"could not start the monty worker: {exc}", backend=self.backend_name
            ) from exc
        finally:
            self._active -= 1
            launch.close()

        # Error and traceback text shares the output cap with print() output: it is cut to
        # the room left (with a note) so it cannot carry more than max_output_bytes back.
        stdout_bytes = bytes(output.stdout)
        stderr_bytes = bytes(output.stderr)
        truncated = False
        if error_text:
            room = max(0, config.max_output_bytes - len(stdout_bytes) - len(stderr_bytes))
            clipped, truncated = _clip_text(f"{error_text}\n", room)
            stderr_bytes += clipped
        return ExecutionResult(
            exit_code=exit_code,
            stdout=stdout_bytes.decode("utf-8", errors="replace"),
            stderr=stderr_bytes.decode("utf-8", errors="replace"),
            truncated=truncated,
            duration_ms=round((time.perf_counter() - start_time) * 1000.0, 2),
            worker_id=worker_id,
            isolated=True,
            backend=self.backend_name,
            isolation=self.isolation,
            details={
                "engine": "monty",
                "engine_version": monty.__version__,
                "python_subset": True,
                "network_isolated": True,
                "filesystem": "read-only input files" if files else "none",
                "worker_rlimits": rlimits,
                "worker_spawn": launch.mode,
                "worker_stderr": launch.stderr,
                "limits_advisory": False,
            },
        )
