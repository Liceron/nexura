# SPDX-License-Identifier: MIT
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
  an operator-configured runsc binary and OCI rootfs.
"""

from __future__ import annotations

from nexura_sandbox import wasi_runtime
from nexura_sandbox.bubblewrap_worker import BubblewrapWorker
from nexura_sandbox.gvisor_worker import GVisorOCIWorker
from nexura_sandbox.wasi_worker import WasiWorker

__all__ = ["BubblewrapWorker", "GVisorOCIWorker", "WasiWorker", "wasi_runtime"]
