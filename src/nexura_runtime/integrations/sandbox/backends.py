# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""P27 sandbox backend interface: isolation levels, probing and operator-driven selection.

Every backend declares how strong its boundary is (``isolation``), which languages it runs
and which limits it enforces itself. The module picks the first operator-enabled backend
that is available and meets the operator's ``SandboxConfig.min_isolation``; if none
qualifies it refuses with ``CAPABILITY_UNAVAILABLE`` and names what is missing. A worker
whose isolation is ``none`` (trusted subprocess, mock) is never selected.
"""

from __future__ import annotations

import importlib
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from nexura_runtime.integrations.sandbox.errors import CapabilityUnavailableError
from nexura_runtime.integrations.sandbox.models import (
    DEFAULT_BACKEND_ORDER,
    ISOLATION_RANK,
    ExecutionRequest,
    ExecutionResult,
    IsolationLevel,
    SandboxConfig,
)

#: The authenticated shared (multi-tenant) service never runs code with less than this:
#: gVisor or a micro-VM, never Monty alone (founder decision, 2026-10-18).
SHARED_SERVICE_MIN_ISOLATION: IsolationLevel = "gvisor"

#: Operator environment variables read by ``sandbox_config_from_env``.
ENV_BACKENDS = "NEXURA_SANDBOX_BACKENDS"
ENV_MIN_ISOLATION = "NEXURA_SANDBOX_MIN_ISOLATION"
ENV_TIMEOUT_SECONDS = "NEXURA_SANDBOX_TIMEOUT_SECONDS"
ENV_MAX_MEMORY_MB = "NEXURA_SANDBOX_MAX_MEMORY_MB"
ENV_MAX_OUTPUT_BYTES = "NEXURA_SANDBOX_MAX_OUTPUT_BYTES"
ENV_MONTY_BIN = "NEXURA_SANDBOX_MONTY_BIN"
#: ``gvisor_production`` selects ``SandboxConfig.gvisor_production`` (see that constructor).
ENV_PROFILE = "NEXURA_SANDBOX_PROFILE"
ENV_RUNSC_PATH = "NEXURA_RUNSC_PATH"
ENV_ROOTFS_PATH = "NEXURA_OCI_ROOTFS_PATH"
ENV_GVISOR_CGROUP_PARENT = "NEXURA_SANDBOX_GVISOR_CGROUP_PARENT"
ENV_RUNSC_SHA512 = "NEXURA_SANDBOX_RUNSC_SHA512"


@dataclass(frozen=True)
class BackendStatus:
    """Result of ``SandboxBackend.probe``: whether the backend can run here, and why not."""

    backend: str
    available: bool
    reason: str = ""
    version: str | None = None


@runtime_checkable
class SandboxBackend(Protocol):
    """A code-execution backend that declares its isolation level, languages and limits."""

    backend_name: str
    isolation: IsolationLevel
    languages: frozenset[str]
    enforced_limits: frozenset[str]

    def probe(self, config: SandboxConfig) -> BackendStatus:
        """Report availability; a bounded disposable process may test host capabilities."""
        ...

    async def execute(
        self,
        request: ExecutionRequest,
        config: SandboxConfig,
        *,
        cancellation_token: Any | None = None,
    ) -> ExecutionResult:
        """Run ``request`` within ``config``'s limits."""
        ...


def isolation_rank(level: str) -> int:
    """Rank of an isolation level; unknown levels rank as ``none`` (0)."""
    return ISOLATION_RANK.get(level, 0)


def backend_isolation(worker: Any) -> str:
    """The isolation level a worker declares; a worker that declares none is ``none``."""
    level = getattr(worker, "isolation", "none")
    return level if isinstance(level, str) and level in ISOLATION_RANK else "none"


#: Backends provided by the separate ``nexura-sandbox`` package (import ``nexura_sandbox``):
#: name -> (module, class, the isolation level that class declares).
PACKAGE_BACKENDS: dict[str, tuple[str, str, IsolationLevel]] = {
    "wasi": ("nexura_sandbox.wasi_worker", "WasiWorker", "interpreter"),
    "bubblewrap": ("nexura_sandbox.bubblewrap_worker", "BubblewrapWorker", "os_sandbox"),
    "gvisor_oci": ("nexura_sandbox.gvisor_worker", "GVisorOCIWorker", "gvisor"),
}


class PackageNotInstalledBackend:
    """Stands in for a ``nexura-sandbox`` backend when that package is not installed.

    It declares the real backend's name and isolation level, so selection treats it exactly
    like the real backend on a host where it is unavailable: ``probe`` reports unavailable with
    "pip install nexura-sandbox" and ``execute`` refuses with ``CAPABILITY_UNAVAILABLE``.
    """

    languages = frozenset({"python", "python3"})
    enforced_limits: frozenset[str] = frozenset()

    def __init__(self, backend_name: str, isolation: IsolationLevel, reason: str) -> None:
        self.backend_name = backend_name
        self.isolation: IsolationLevel = isolation
        self.reason = reason

    def probe(self, config: SandboxConfig) -> BackendStatus:
        """Always unavailable: the package that provides this backend is not installed."""
        return BackendStatus(self.backend_name, False, self.reason)

    async def execute(
        self,
        request: ExecutionRequest,
        config: SandboxConfig,
        *,
        cancellation_token: Any | None = None,
    ) -> ExecutionResult:
        """Refuse: nothing runs without the backend's package."""
        raise CapabilityUnavailableError(self.reason, backend=self.backend_name)


def build_backend(name: str) -> SandboxBackend:
    """Construct a registered backend; bubblewrap always needs explicit enablement.

    Monty is built in. WASI, bubblewrap and gVisor come from the ``nexura-sandbox`` package;
    without it they are ``PackageNotInstalledBackend`` placeholders that never qualify.
    """
    if name == "monty":
        from nexura_runtime.integrations.sandbox.monty_worker import MontyWorker

        return MontyWorker()
    if name in PACKAGE_BACKENDS:
        # Imported here: ``nexura_runtime.modules`` imports this package on start-up.
        from nexura_runtime.modules.optional_packages import (
            SANDBOX_PACKAGE,
            find_package,
            package_missing_message,
        )

        module_name, class_name, isolation = PACKAGE_BACKENDS[name]
        if find_package(SANDBOX_PACKAGE) is None:
            reason = package_missing_message(f"The '{name}' sandbox backend", SANDBOX_PACKAGE)
            return PackageNotInstalledBackend(name, isolation, reason)
        backend: SandboxBackend = getattr(importlib.import_module(module_name), class_name)()
        return backend
    raise CapabilityUnavailableError(
        f"unknown sandbox backend '{name}' (selectable: monty, wasi, bubblewrap, gvisor_oci)",
        backend=name,
    )


def select_backend(
    config: SandboxConfig,
    candidates: Sequence[SandboxBackend] | None = None,
) -> SandboxBackend:
    """Pick the first candidate that is available and meets ``config.min_isolation``.

    ``candidates`` defaults to ``config.backends`` (or ``DEFAULT_BACKEND_ORDER`` when that is
    ``None``), built with ``build_backend``. Workers that declare isolation ``none`` are
    skipped, never chosen as a fallback. Raises ``CapabilityUnavailableError`` naming every
    rejected backend when nothing qualifies.
    Bubblewrap is deferred behind enabled stronger backends, even if listed first.
    """
    if candidates is None:
        names = config.backends if config.backends is not None else list(DEFAULT_BACKEND_ORDER)
        candidates = [build_backend(name) for name in names]
    floor = isolation_rank(config.min_isolation)
    # An OS sandbox must not displace an enabled stronger boundary, even when listed first.
    candidates = list(candidates)
    for index, candidate in enumerate(candidates):
        if candidate.backend_name == "bubblewrap":
            stronger = [
                item
                for item in candidates[index + 1 :]
                if isolation_rank(backend_isolation(item)) > isolation_rank("os_sandbox")
            ]
            candidates = (
                candidates[:index]
                + stronger
                + [candidate]
                + [item for item in candidates[index + 1 :] if item not in stronger]
            )
            break
    rejected: list[str] = []
    for backend in candidates:
        name = getattr(backend, "backend_name", type(backend).__name__)
        level = backend_isolation(backend)
        if isolation_rank(level) == 0:
            rejected.append(f"{name}: not an isolation boundary (never selected)")
            continue
        if isolation_rank(level) < floor:
            rejected.append(
                f"{name}: isolation '{level}' is below the operator minimum "
                f"'{config.min_isolation}'"
            )
            continue
        status = backend.probe(config)
        if not status.available:
            rejected.append(f"{name}: {status.reason}")
            continue
        return backend
    detail = "; ".join(rejected) if rejected else "no backends are enabled"
    raise CapabilityUnavailableError(
        f"no enabled sandbox backend is available at min_isolation='{config.min_isolation}' "
        f"({detail})",
        backend="selection",
    )


def sandbox_config_from_env(environ: Mapping[str, str] | None = None) -> SandboxConfig | None:
    """Operator ``SandboxConfig`` from environment variables, or ``None`` when not enabled.

    ``NEXURA_SANDBOX_BACKENDS`` enables the sandbox: a comma-separated preference list such
    as ``monty`` or ``monty,gvisor_oci``. Optional: ``NEXURA_SANDBOX_MIN_ISOLATION``
    (``interpreter``, ``os_sandbox``, ``gvisor``, ``microvm``),
    ``NEXURA_SANDBOX_TIMEOUT_SECONDS``, ``NEXURA_SANDBOX_MAX_MEMORY_MB``,
    ``NEXURA_SANDBOX_MAX_OUTPUT_BYTES`` and ``NEXURA_SANDBOX_MONTY_BIN`` (an absolute path).
    These are operator settings: nothing a flow author or model sends can set them. Raises
    ``ValueError`` for an invalid value.

    Bubblewrap additionally accepts ``NEXURA_SANDBOX_BWRAP_BIN``, ``_PYTHON``, ``_RUNTIME``
    and ``_SECCOMP_BPF`` absolute paths (the latter three share the same BWRAP prefix).
    ``NEXURA_SANDBOX_BWRAP_MAX_OPEN_FILES`` may lower the payload descriptor cap.

    ``NEXURA_SANDBOX_GVISOR_CGROUP_PARENT`` and ``NEXURA_SANDBOX_RUNSC_SHA512`` set the gVisor
    cgroup parent and the runsc SHA-512 pin, with or without a profile; neither is ever
    ignored (an invalid value raises ``ValueError``).

    ``NEXURA_SANDBOX_PROFILE=gvisor_production`` enables the sandbox on its own and builds
    ``SandboxConfig.gvisor_production`` from ``NEXURA_RUNSC_PATH`` and
    ``NEXURA_OCI_ROOTFS_PATH`` (both required), the cgroup parent (default ``/nexura``) and
    the optional pin. The limit variables above still apply; a backend list, isolation
    floor or limit that breaks the profile raises ``ValueError``.
    """
    env = os.environ if environ is None else environ
    raw = (env.get(ENV_BACKENDS) or "").strip()
    profile = (env.get(ENV_PROFILE) or "").strip().lower()
    if profile and profile != "gvisor_production":
        raise ValueError(f"{ENV_PROFILE} must be 'gvisor_production' (got {profile!r})")
    if not raw and not profile:
        stray = [name for name in (ENV_GVISOR_CGROUP_PARENT, ENV_RUNSC_SHA512) if env.get(name)]
        if stray:
            # A pin or cgroup parent without an enabled sandbox would otherwise be dropped.
            raise ValueError(
                f"{', '.join(stray)} set, but the sandbox is not enabled: set "
                f"{ENV_BACKENDS} or {ENV_PROFILE}=gvisor_production"
            )
        return None
    fields: dict[str, Any] = {}
    if raw:
        fields["backends"] = [part.strip().lower() for part in raw.split(",") if part.strip()]
    if env.get(ENV_MIN_ISOLATION):
        fields["min_isolation"] = env[ENV_MIN_ISOLATION].strip().lower()
    if env.get(ENV_TIMEOUT_SECONDS):
        fields["timeout_seconds"] = float(env[ENV_TIMEOUT_SECONDS])
    if env.get(ENV_MAX_MEMORY_MB):
        fields["max_memory_mb"] = int(env[ENV_MAX_MEMORY_MB])
    if env.get(ENV_MAX_OUTPUT_BYTES):
        fields["max_output_bytes"] = int(env[ENV_MAX_OUTPUT_BYTES])
    if env.get(ENV_MONTY_BIN):
        fields["monty_binary_path"] = env[ENV_MONTY_BIN].strip()
    if env.get("NEXURA_SANDBOX_WASI_RUNTIME"):
        fields["wasi_runtime_path"] = env["NEXURA_SANDBOX_WASI_RUNTIME"].strip()
    if env.get("NEXURA_SANDBOX_BWRAP_MAX_OPEN_FILES"):
        fields["bubblewrap_max_open_files"] = int(env["NEXURA_SANDBOX_BWRAP_MAX_OPEN_FILES"])
    for suffix, field in (
        ("BIN", "bubblewrap_path"),
        ("PYTHON", "bubblewrap_python_path"),
        ("RUNTIME", "bubblewrap_runtime_path"),
        ("SECCOMP_BPF", "bubblewrap_seccomp_bpf_path"),
    ):
        if env.get(f"NEXURA_SANDBOX_BWRAP_{suffix}"):
            fields[field] = env[f"NEXURA_SANDBOX_BWRAP_{suffix}"].strip()
    if env.get(ENV_GVISOR_CGROUP_PARENT):
        fields["gvisor_cgroup_parent"] = env[ENV_GVISOR_CGROUP_PARENT].strip()
    if env.get(ENV_RUNSC_SHA512):
        # Set but blank after stripping still reaches validation, which refuses it.
        fields["gvisor_runsc_sha512"] = env[ENV_RUNSC_SHA512].strip().lower()
    # pydantic's ValidationError is a ValueError.
    if profile:
        return SandboxConfig.gvisor_production(
            runsc_path=(env.get(ENV_RUNSC_PATH) or "").strip(),
            rootfs_path=(env.get(ENV_ROOTFS_PATH) or "").strip(),
            cgroup_parent=fields.pop("gvisor_cgroup_parent", "/nexura"),
            runsc_sha512=fields.pop("gvisor_runsc_sha512", None),
            **fields,
        )
    return SandboxConfig(**fields)
