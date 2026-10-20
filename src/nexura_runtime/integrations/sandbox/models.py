# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Data models for isolated sandbox execution and worker configurations."""

from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field

IsolationBackend = Literal[
    "gvisor_oci",
    "monty",
    "wasi",
    "bubblewrap",
    "trusted_subprocess",
    "isolated_process",
    "mock_disposable",
]

#: How strong a backend's boundary is, weakest first. ``none`` (trusted subprocess, mock) is
#: not a boundary; ``interpreter`` is a language-level sandbox in a separate worker process
#: (Monty/WASI); ``os_sandbox`` is Linux namespaces (bubblewrap, optional seccomp); ``gvisor``
#: is a user-space kernel; ``microvm`` is a hardware virtual machine (not built yet).
IsolationLevel = Literal["none", "interpreter", "os_sandbox", "gvisor", "microvm"]

#: The minimum isolation an operator may require. ``none`` is not a choice: a non-isolating
#: worker is never selected; it runs only when the operator passes it as ``worker=``.
MinIsolation = Literal["interpreter", "os_sandbox", "gvisor", "microvm"]

#: Rank of each isolation level; a backend qualifies when its rank >= the operator minimum.
ISOLATION_RANK: dict[str, int] = {
    "none": 0,
    "interpreter": 1,
    "os_sandbox": 2,
    "gvisor": 3,
    "microvm": 4,
}

#: Backends the module may select from ``SandboxConfig.backends``. Non-isolating workers
#: (``trusted_subprocess``, ``isolated_process``, ``mock_disposable``) are not selectable.
SelectableBackend = Literal["monty", "wasi", "bubblewrap", "gvisor_oci"]

#: Order used when the operator enables the sandbox without naming backends: Monty (the
#: default, every OS, no root) first, then gVisor for operators that require it.
DEFAULT_BACKEND_ORDER: tuple[SelectableBackend, ...] = ("monty", "gvisor_oci")


class SandboxConfig(BaseModel):
    """Resource boundaries and security policy for disposable execution workers."""

    timeout_seconds: float = Field(default=10.0, gt=0.0, le=300.0)
    max_memory_mb: int = Field(default=256, ge=16, le=4096)
    max_output_bytes: int = Field(default=1_048_576, ge=1024, le=10_485_760)  # 1MB default
    max_processes: int = Field(default=1, ge=1, le=64)  # Prevents fork bombs
    allow_network: bool = False  # Strict default deny
    readonly_root: bool = True
    backend: IsolationBackend = "gvisor_oci"
    #: Operator-enabled backends, in preference order. ``None`` (the default) means the
    #: operator has not enabled the sandbox: the module keeps the legacy fail-closed gVisor
    #: worker and stays hidden from Studio. When set, the module selects the first backend
    #: that is available and meets ``min_isolation``, or refuses with CAPABILITY_UNAVAILABLE.
    backends: list[SelectableBackend] | None = None
    #: Weakest isolation the operator accepts. ``interpreter`` (the local default) allows
    #: Monty; the authenticated shared service requires ``gvisor`` or stronger.
    min_isolation: MinIsolation = "interpreter"
    #: With ``backends`` set, an explicit ``worker=`` that is not an isolation boundary
    #: (``TrustedSubprocessWorker``, ``MockDisposableWorker``) or is below ``min_isolation``
    #: is refused with CAPABILITY_UNAVAILABLE unless the operator sets this to ``True``.
    allow_unisolated: bool = False
    #: Most sandbox executions that run at once per workspace (per module instance). Further
    #: calls wait in a queue for a free slot, bounded by the caller's deadline.
    max_concurrent_executions: int = Field(default=4, ge=1, le=64)
    #: Caps on what a call may hand the sandbox: code + stdin + input file contents (UTF-8
    #: bytes) and the number of input files. Checked before approval and before any worker.
    max_input_bytes: int = Field(default=5_242_880, ge=1024, le=52_428_800)  # 5 MB default
    max_input_files: int = Field(default=20, ge=0, le=100)
    runsc_path: str | None = None
    rootfs_path: str | None = None
    #: Guest scratch is a tmpfs; no writable host directory is mounted.
    gvisor_tmpfs_bytes: int = Field(default=33_554_432, ge=1_048_576, le=536_870_912)
    #: Host cgroup task allowance for sentry/gofer threads, additional to guest NPROC.
    gvisor_runtime_pids: int = Field(default=128, ge=64, le=4096)
    #: Absolute cgroup hierarchy path where runsc may create a per-container child.
    gvisor_cgroup_parent: str = "/nexura"
    #: Absolute path of the ``monty`` worker binary. ``None`` uses the binary that
    #: ``pydantic-monty`` installed next to this interpreter; ``PATH`` and ``MONTY_BIN`` are
    #: never consulted.
    monty_binary_path: str | None = None
    #: Operator-owned CPython WASI runtime installed by the explicit CLI command.
    wasi_runtime_path: str | None = None
    wasi_fuel: int = Field(default=4_000_000_000, ge=1, le=100_000_000_000)
    wasi_scratch_enabled: bool = False
    wasi_scratch_bytes: int = Field(default=33_554_432, ge=1024, le=536_870_912)
    wasi_scratch_files: int = Field(default=200, ge=1, le=10000)
    wasi_recursion_limit: int = Field(default=200, ge=50, le=1000)
    #: Linux-only, explicitly enabled backend; never part of DEFAULT_BACKEND_ORDER.
    bubblewrap_path: str | None = None
    bubblewrap_python_path: str = "/usr/bin/python3"
    #: Optional operator-owned runtime/venv directory, mounted read-only at a fixed path.
    #: Python must be under /usr or this directory, with its base runtime available there.
    bubblewrap_runtime_path: str | None = None
    #: Explicit operator-approved roots for extra read-only runtimes. Protected host
    #: trees (including all passwd homes) are refused even when listed here.
    bubblewrap_runtime_roots: list[str] = Field(default_factory=list)
    bubblewrap_cpu_seconds: int = Field(default=5, ge=1, le=60)
    #: Payload-only descriptor cap. Operators may lower, never exceed the hard cap 128.
    bubblewrap_max_open_files: int = Field(default=128, ge=16, le=128)
    bubblewrap_tmpfs_bytes: int = Field(default=33_554_432, ge=1_048_576, le=536_870_912)
    #: Optional inode-cap opt-in. Requires a namespace remount that Ubuntu's default
    #: AppArmor policy can deny. None uses bwrap's byte cap without claiming an inode cap.
    #: An explicitly requested cap fails closed if the host cannot enforce it.
    bubblewrap_tmpfs_inodes: int | None = Field(default=None, ge=128, le=65536)
    #: Optional additional native-architecture cBPF policy. Cannot weaken the built-in filter.
    bubblewrap_seccomp_bpf_path: str | None = None
    env_allowlist: list[str] = Field(
        default_factory=lambda: [
            "PATH",
            "LANG",
            "LC_ALL",
            "PYTHONHASHSEED",
            "PYTHONUNBUFFERED",
        ]
    )
    extra: dict[str, Any] = Field(default_factory=dict)


class ExecutionRequest(BaseModel):
    """Payload to execute inside an isolated disposable worker."""

    code: str
    language: str = "python"
    stdin: str = ""
    files: dict[str, str] = Field(default_factory=dict)
    env: dict[str, str] = Field(default_factory=dict)
    request_id: str = Field(default_factory=lambda: f"req_{uuid.uuid4().hex[:10]}")


class ExecutionResult(BaseModel):
    """Structured result returned from a sandbox worker."""

    exit_code: int
    stdout: str
    stderr: str
    truncated: bool = False
    duration_ms: float = 0.0
    worker_id: str
    isolated: bool = False
    backend: str
    #: Isolation level of the backend that ran the code (see ``IsolationLevel``).
    isolation: str | None = None
    peak_memory_bytes: int | None = None
    details: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_success(self) -> bool:
        """True if the worker process exited normally with code 0."""
        return self.exit_code == 0
