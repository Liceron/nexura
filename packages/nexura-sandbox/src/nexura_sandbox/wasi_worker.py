# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""CPython WASI interpreter boundary in a disposable child process, with no host mounts."""

from __future__ import annotations

import asyncio
import contextlib
import importlib.metadata
import importlib.util
import json
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from nexura_runtime.integrations.sandbox.backends import BackendStatus
from nexura_runtime.integrations.sandbox.errors import (
    CapabilityUnavailableError,
    ExecutionTimeoutError,
    InputLimitExceededError,
    MemoryLimitExceededError,
    OutputLimitExceededError,
    SecurityViolationError,
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
    validate_request_language,
    validate_virtual_paths,
)
from nexura_sandbox.wasi_runtime import (
    WASMTIME_VERSION,
    runtime_path,
    verify_runtime,
)


class WasiWorker:
    """Full CPython pure-Python stdlib, capability-scoped WASI I/O, no processes/sockets.

    Install the optional wasmtime dependency and the pinned runtime with the explicit
    operator command. Runtime and native cache must be operator-owned. Only lib and input
    copies are preopened read-only. Scratch is opt-in and quota checked at WASI syscalls.
    Store memory is a guest linear-memory cap, not a whole-process resident-memory cap.
    Native compilation/cache loading and all guest execution happen outside the service.
    """

    backend_name = "wasi"
    isolation: IsolationLevel = "interpreter"
    languages = frozenset({"python", "python3"})
    enforced_limits = frozenset(
        {
            "wall_time",
            "fuel",
            "memory",
            "output",
            "disk_bytes",
            "disk_files",
            "network",
            "processes",
        }
    )

    def __init__(self) -> None:
        self._active: set[int] = set()

    @property
    def active_worker_count(self) -> int:
        """Number of live child processes owned by this worker."""
        return len(self._active)

    def probe(self, config: SandboxConfig) -> BackendStatus:
        """Fail closed when the optional dependency or installed runtime/cache is absent."""
        if importlib.util.find_spec("wasmtime") is None:
            return BackendStatus("wasi", False, "wasmtime is missing; install wasmtime==49.0.0")
        try:
            version = importlib.metadata.version("wasmtime")
        except importlib.metadata.PackageNotFoundError:
            return BackendStatus("wasi", False, "wasmtime package metadata is missing; reinstall")
        if version != WASMTIME_VERSION:
            return BackendStatus(
                "wasi", False, f"requires wasmtime {WASMTIME_VERSION}, found {version}"
            )
        try:
            root = runtime_path(config.wasi_runtime_path)
            verify_runtime(root)
            if not (root / "wasmtime-49.0.0.cwasm").is_file():
                raise ValueError("compiled runtime cache is missing")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return BackendStatus(
                "wasi",
                False,
                f"CPython WASI runtime unavailable ({exc}); run "
                "nexura-runtime sandbox install-runtime wasi --from-file ZIP",
            )
        return BackendStatus("wasi", True, version=version)

    async def execute(
        self,
        request: ExecutionRequest,
        config: SandboxConfig,
        *,
        cancellation_token: Any | None = None,
    ) -> ExecutionResult:
        """Run once in a child; timeout, cancellation and failures always kill and reap it."""
        _check_cancellation(cancellation_token)
        validate_request_language(request.language, self.backend_name)
        files = validate_virtual_paths(request.files)
        if "__nexura_code__.py" in request.files:
            raise SecurityViolationError("path_escape", "reserved WASI input filename")
        if config.allow_network:
            raise SecurityViolationError("network_policy", "WASI provides no network capability")
        if any(key not in config.env_allowlist for key in request.env):
            raise SecurityViolationError("env", "environment key is outside the operator allowlist")
        size = sum(len(text.encode()) for text in (request.code, request.stdin))
        size += sum(len(name.encode()) + len(text.encode()) for name, text in files)
        if size > config.max_input_bytes or len(files) > config.max_input_files:
            raise InputLimitExceededError(
                "WASI inputs exceed caps", limit=config.max_input_bytes, actual=size
            )
        status = self.probe(config)
        if not status.available:
            raise CapabilityUnavailableError(status.reason, backend="wasi")
        worker_id = "wasi_" + uuid.uuid4().hex[:10]
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="nexura-wasi-") as directory:
            work = Path(directory)
            inputs = work / "inputs"
            inputs.mkdir()
            (work / "scratch").mkdir()
            (work / "stdin").write_text(request.stdin, encoding="utf-8")
            for name, content in files:
                target = inputs / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
            (inputs / "__nexura_code__.py").write_text(request.code, encoding="utf-8")
            payload = json.dumps(
                {
                    "runtime": str(runtime_path(config.wasi_runtime_path)),
                    "work": str(work),
                    "request": request.model_dump(),
                    "config": config.model_dump(),
                }
            ).encode()
            launch = asyncio.create_task(
                asyncio.create_subprocess_exec(
                    sys.executable,
                    "-I",
                    str(Path(__file__).with_name("wasi_runner.py")),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env={},
                    cwd=directory,
                )
            )
            try:
                process = await asyncio.shield(launch)
            except BaseException:
                # Cancellation can arrive after spawn but before asyncio returns the
                # handle. Retain the launch task, acquire that handle, kill and reap.
                process = await launch
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                await process.wait()
                raise
            self._active.add(process.pid)
            task = asyncio.create_task(process.communicate(payload))
            try:
                # Guest epoch timer is the execution clock; host includes cache/startup
                # overhead, and is also a backstop for a blocked host function/native crash.
                deadline = time.monotonic() + config.timeout_seconds + 3
                while not task.done():
                    _check_cancellation(cancellation_token)
                    if time.monotonic() >= deadline:
                        raise ExecutionTimeoutError(config.timeout_seconds, worker_id=worker_id)
                    await asyncio.wait({task}, timeout=0.02)
                stdout, stderr = task.result()
                if process.returncode:
                    raise WorkerCrashError(process.returncode)
                try:
                    result = json.loads(stdout)
                except (ValueError, TypeError) as exc:
                    raise WorkerCrashError(process.returncode or 1) from exc
                outcome = result.get("outcome")
                if outcome == "timeout":
                    raise ExecutionTimeoutError(config.timeout_seconds, worker_id=worker_id)
                if outcome == "output":
                    raise OutputLimitExceededError(
                        config.max_output_bytes, config.max_output_bytes + 1
                    )
                if outcome == "memory":
                    raise MemoryLimitExceededError(config.max_memory_mb, worker_id=worker_id)
                if outcome == "scratch":
                    raise SecurityViolationError("disk_limit", "WASI scratch quota exceeded")
                if outcome == "crash" or stderr:
                    raise WorkerCrashError(1)
                return ExecutionResult(
                    exit_code=result["exit_code"],
                    stdout=result["stdout"],
                    stderr=result["stderr"],
                    worker_id=worker_id,
                    backend="wasi",
                    isolation="interpreter",
                    isolated=True,
                    duration_ms=(time.monotonic() - started) * 1000,
                )
            finally:
                if process.returncode is None:
                    with contextlib.suppress(ProcessLookupError):
                        process.kill()
                await process.wait()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                self._active.discard(process.pid)
