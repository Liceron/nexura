# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Sandbox module for isolated and secure execution of untrusted code.

Importing this package registers ``nexura.tools.sandbox.execute`` in the default module
registry so saved workflows validate. It is configured from the operator environment
(``sandbox_config_from_env``): without ``NEXURA_SANDBOX_BACKENDS`` it keeps the legacy
fail-closed gVisor worker and stays hidden from Studio; with, for example,
``NEXURA_SANDBOX_BACKENDS=monty`` it selects the first available backend that meets
``NEXURA_SANDBOX_MIN_ISOLATION`` and becomes visible. Every call needs an approval.

Backends: Monty (the default, ``nexura-runtime[sandbox]``) is part of this runtime. The
WASI, bubblewrap and gVisor backends (``WasiWorker``, ``BubblewrapWorker``,
``GVisorOCIWorker``) are the ``nexura-sandbox`` package (import ``nexura_sandbox``);
``build_backend`` loads them when it is installed and otherwise returns a
``PackageNotInstalledBackend`` that probes unavailable with "pip install nexura-sandbox".
Selection, ``SandboxConfig`` and the module's refusal rules are the same either way.
"""

from __future__ import annotations

import importlib
import warnings

from nexura_runtime.integrations.sandbox.backends import (
    SHARED_SERVICE_MIN_ISOLATION,
    BackendStatus,
    PackageNotInstalledBackend,
    SandboxBackend,
    build_backend,
    isolation_rank,
    sandbox_config_from_env,
    select_backend,
)
from nexura_runtime.integrations.sandbox.errors import (
    CapabilityUnavailableError,
    ExecutionTimeoutError,
    InputLimitExceededError,
    MemoryLimitExceededError,
    OutputLimitExceededError,
    SandboxError,
    SecurityViolationError,
    UnsupportedFeatureError,
    WorkerCrashError,
)
from nexura_runtime.integrations.sandbox.models import (
    DEFAULT_BACKEND_ORDER,
    GVISOR_PRODUCTION_CEILINGS,
    GVISOR_PRODUCTION_PROFILE,
    ExecutionRequest,
    ExecutionResult,
    HardenedProfile,
    IsolationBackend,
    IsolationLevel,
    MinIsolation,
    SandboxConfig,
    SelectableBackend,
    gvisor_production_violations,
)
from nexura_runtime.integrations.sandbox.module import (
    SandboxExecutionModule,
    assert_sandbox_allowed_for_profile,
)
from nexura_runtime.integrations.sandbox.monty_worker import MontyWorker
from nexura_runtime.integrations.sandbox.worker import (
    IsolatedProcessWorker,
    MockDisposableWorker,
    TrustedSubprocessWorker,
    WorkerBackend,
)
from nexura_runtime.modules.optional_packages import SANDBOX_PACKAGE, find_package
from nexura_runtime.modules.registry import default_registry

__all__ = [
    "DEFAULT_BACKEND_ORDER",
    "GVISOR_PRODUCTION_CEILINGS",
    "GVISOR_PRODUCTION_PROFILE",
    "SHARED_SERVICE_MIN_ISOLATION",
    "BackendStatus",
    "CapabilityUnavailableError",
    "ExecutionRequest",
    "ExecutionResult",
    "ExecutionTimeoutError",
    "HardenedProfile",
    "InputLimitExceededError",
    "IsolatedProcessWorker",
    "IsolationBackend",
    "IsolationLevel",
    "MemoryLimitExceededError",
    "MinIsolation",
    "MockDisposableWorker",
    "MontyWorker",
    "OutputLimitExceededError",
    "PackageNotInstalledBackend",
    "SandboxBackend",
    "SandboxConfig",
    "SandboxError",
    "SandboxExecutionModule",
    "SecurityViolationError",
    "SelectableBackend",
    "TrustedSubprocessWorker",
    "UnsupportedFeatureError",
    "WorkerBackend",
    "WorkerCrashError",
    "assert_sandbox_allowed_for_profile",
    "build_backend",
    "gvisor_production_violations",
    "isolation_rank",
    "sandbox_config_from_env",
    "select_backend",
]


def _operator_module() -> SandboxExecutionModule:
    """The registered module, configured only from the operator environment."""
    try:
        config = sandbox_config_from_env()
    except ValueError as exc:
        # A bad operator setting must not break ``import nexura_runtime``: the sandbox is
        # enabled with no usable backend, so it stays hidden and refuses every call.
        warnings.warn(
            f"Ignoring invalid sandbox settings ({exc}); code execution is unavailable.",
            RuntimeWarning,
            stacklevel=2,
        )
        module = SandboxExecutionModule(config=SandboxConfig(backends=[]))
        module.settings_error = str(exc)
        module.selection_error = f"invalid operator sandbox settings: {exc}"
    else:
        module = SandboxExecutionModule(config=config)
    default_registry.get_entry("nexura.tools.sandbox.execute").manifest = module.manifest
    return module


# Register metadata without discovering backends or probing the host during import.
# The factory reads operator settings when the registry first needs the module.
if not default_registry.has("nexura.tools.sandbox.execute"):
    _manifest = SandboxExecutionModule(worker=MockDisposableWorker()).manifest
    default_registry.register(_operator_module, manifest=_manifest)

# Preserve the package contract: an installed but broken backend package raises its
# real import error. Import definitions only; never instantiate/select/probe here.
# Importing the top-level package is safe even while its first backend is loading.
if find_package(SANDBOX_PACKAGE) is not None:
    importlib.import_module("nexura_sandbox")
