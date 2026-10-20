# Nexura Sandbox

WASI, bubblewrap and gVisor code-execution backends for nexura-runtime.

## Install

```bash
pip install nexura-sandbox
pip install "nexura-sandbox[wasi]"  # WASI extra
```

Alpha and unpublished: install from the [source checkout](https://github.com/Liceron/nexura) or local wheels until publication. See [package installation](https://github.com/Liceron/nexura/blob/main/docs/packages.md).

## Minimal example

This example only probes host capabilities; it never runs user code. Execution needs approval and host dependencies: WASI needs `[wasi]` plus the pinned CPython runtime; bubblewrap needs Linux user namespaces and `bwrap`; gVisor needs `runsc`, an OCI rootfs and delegated cgroups. Missing backends fall through or fail with `CAPABILITY_UNAVAILABLE`; an isolation floor prevents weaker fallbacks.

```python
from nexura_runtime.integrations.sandbox import SandboxConfig, build_backend

from nexura_sandbox import BubblewrapWorker, GVisorOCIWorker, WasiWorker

config = SandboxConfig()
for name in ("wasi", "bubblewrap", "gvisor_oci"):
    backend = build_backend(name)  # the runtime returns this package's class
    assert isinstance(backend, (WasiWorker, BubblewrapWorker, GVisorOCIWorker))
    status = backend.probe(config)
    print(f"{name:<11} isolation={backend.isolation:<12} available={status.available}")
```

## Status and limits

> Status: **alpha** (0.4), Python 3.12, Linux only. Escape tests ran against real bubblewrap,
> gVisor and WASI on one Ubuntu x86_64 host. Real gVisor
> verification used only the explicit sudo test profile with host cgroups ignored; the
> production profile with delegated memory/pids cgroups has not been exercised. runsc
> requires root on the reviewed host. No external security review has been done.
> WASI's complete listener-based escape profile still needs a host permitting local sockets;
> the recorded shared escape run had one host-listener setup error (`socket()` denied).

Bubblewrap enforces a tmpfs byte cap but no inode cap unless `bubblewrap_tmpfs_inodes`
is set. It shares the host kernel with no aggregate memory/CPU ceiling; address-space
and CPU limits are per process, and `max_memory_mb` is not total RAM.

Use a service supervisor (systemd `KillMode=control-group` or a container) to stop all
descendants on service exit. Per-run crash watchdogs and delegated bubblewrap lifecycle
cgroups add cleanup, but cannot replace the supervisor if the watchdog is also killed.

Each bubblewrap/gVisor launcher runs under a dedicated stdlib supervisor child,
which alone enables subreaping and kills/reaps its own descendants. Worker calls
leave the hosting process's subreaper flag unchanged. Watchdog and cleanup pipe
polling supports descriptors above 1023. All helpers close unrelated descriptors;
only the explicit lifecycle and bubblewrap input descriptors are inherited.
In the legacy privileged gVisor test profile, the configured command prefix also
invokes the fixed supervisor script with the current Python interpreter, so the
operator's test command permissions must cover that invocation.
gVisor startup sweeps only attributable stale task directories for absent containers;
unmarked older directories need operator inspection. See [sandbox details](https://github.com/Liceron/nexura/blob/main/docs/tools.md#code-execution-sandbox-nexuratoolssandboxexecute) and
[SECURITY_MODEL.md](https://github.com/Liceron/nexura/blob/main/SECURITY_MODEL.md) for resource and isolation limitations.

The shared-service startup guard refuses registered sandbox modules below `gvisor`; it is a startup check, not a continuous guard over later registry changes.

## Documentation and licence

[Main documentation](https://github.com/Liceron/nexura/blob/main/docs/README.md) · [Full guide](https://github.com/Liceron/nexura/blob/main/docs/tools.md#code-execution-sandbox-nexuratoolssandboxexecute). MIT, Copyright (c) 2026 Liceron. See [LICENSE](https://github.com/Liceron/nexura/blob/main/packages/nexura-sandbox/LICENSE).
