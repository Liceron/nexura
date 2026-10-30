# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Stronger sandbox backends for nexura-runtime: WASI, bubblewrap and gVisor.

Distributed as ``nexura-sandbox`` (it depends on ``nexura-runtime``). The runtime keeps the
backend selection, ``SandboxConfig``, the ``nexura.tools.sandbox.execute`` module and the
default Monty backend; ``nexura_runtime.integrations.sandbox.build_backend`` loads these
classes when this package is installed:

- ``WasiWorker`` (``wasi``, isolation ``interpreter``): full CPython compiled to WebAssembly,
  run by wasmtime in a disposable child process. Needs the ``wasi`` extra (wasmtime) and the
  pinned runtime installed with ``nexura-runtime sandbox install-runtime wasi``.
- ``BubblewrapWorker`` (``bubblewrap``, isolation ``os_sandbox``): Linux namespaces with a
  mandatory seccomp filter. Needs the ``bwrap`` binary.
- ``GVisorOCIWorker`` (``gvisor_oci``, isolation ``gvisor``): a user-space kernel (runsc). Needs
  an operator-configured runsc binary and OCI rootfs. ``SandboxConfig.gvisor_production`` is
  the hardened profile; ``gvisor_preflight`` (or ``python -m nexura_sandbox.preflight``)
  checks a host for it and starts one tiny guest.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

from nexura_sandbox import wasi_runtime
from nexura_sandbox.bubblewrap_worker import BubblewrapWorker
from nexura_sandbox.gvisor_worker import GVisorOCIWorker
from nexura_sandbox.wasi_worker import WasiWorker

if TYPE_CHECKING:
    from nexura_sandbox.preflight import (
        PreflightCheck,
        PreflightReport,
        gvisor_preflight,
        gvisor_preflight_async,
    )

#: Loaded on first use, so ``python -m nexura_sandbox.preflight`` runs the module only once.
_PREFLIGHT_NAMES = frozenset(
    {"PreflightCheck", "PreflightReport", "gvisor_preflight", "gvisor_preflight_async"}
)


def __getattr__(name: str) -> Any:
    if name in _PREFLIGHT_NAMES:
        return getattr(importlib.import_module("nexura_sandbox.preflight"), name)
    raise AttributeError(f"module 'nexura_sandbox' has no attribute {name!r}")


__all__ = [
    "BubblewrapWorker",
    "GVisorOCIWorker",
    "PreflightCheck",
    "PreflightReport",
    "WasiWorker",
    "gvisor_preflight",
    "gvisor_preflight_async",
    "wasi_runtime",
]
