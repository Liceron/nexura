# Nexura Sandbox

WASI, bubblewrap and gVisor code-execution backends for nexura-runtime.

## Install

```bash
pip install nexura-sandbox
pip install "nexura-sandbox[wasi]"  # WASI extra
```

Alpha (0.4): `pip install nexura-sandbox`. See [package installation](https://github.com/Liceron/nexura/blob/main/docs/packages.md).

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

## Production gVisor profile

For servers, build the config with `SandboxConfig.gvisor_production(...)`. It enforces:
only `gvisor_oci`, `min_isolation="gvisor"`, no network, a read-only root, explicit
normalised absolute paths, host memory/pids cgroups (no test bypass), no command prefix
other than the test-only `sudo -n`, and ceilings of 60 s, 1024 MB, 16 processes, 256
runtime tasks, 128 MiB scratch and 4 MiB output. A config that breaks any of these fails
validation. Its limits default lower (10 s, 256 MB, one process with fork/clone denied,
32 MiB scratch, 1 MiB output) and may be raised up to the ceilings. Then check the host. The
preflight fails if any root-run file or its directories can be written by group or others,
starts one tiny guest, and verifies its observed cgroup limits and cleanup:

```bash
python -m nexura_sandbox.preflight --runsc /usr/local/bin/runsc \
  --rootfs /opt/nexura/gvisor-rootfs --cgroup-parent /nexura --runsc-sha512 <hex> --strict
```

From Python, call `nexura_sandbox.gvisor_preflight(config)`. The SHA-512 pin is a drift
check, made by the service user when the module is built and before each execution, not at
the moment root executes runsc; the `gvisor-bin/` sidecars are not pinned. Root-only
directories, which the preflight requires, are the real protection. See
[Production gVisor profile](https://github.com/Liceron/nexura/blob/main/docs/sandbox-production.md)
for the operator checklist: root, the cgroup parent, a pinned runsc with its `gvisor-bin/`
sidecars, and the rootfs.

## Status and limits

> Status: **alpha** (0.4), Python 3.12, Linux only. Escape tests ran against real bubblewrap,
> gVisor and WASI on one Ubuntu x86_64 host. On that host (cgroup v2, runsc
> `release-20260928.0`), the `gvisor_production` profile also ran with enforced memory and
> pids cgroups. runsc ran as root through a test-only `sudo -n` prefix, and the cgroup parent
> was under a systemd-delegated subtree. A root service with a top-level `/nexura` parent
> was not exercised. runsc requires root on the reviewed host. No external security review
> has been done.
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

[Main documentation](https://github.com/Liceron/nexura/blob/main/docs/README.md) · [Full guide](https://github.com/Liceron/nexura/blob/main/docs/tools.md#code-execution-sandbox-nexuratoolssandboxexecute). Apache-2.0, Copyright (c) 2026 Liceron. See [LICENSE](https://github.com/Liceron/nexura/blob/main/packages/nexura-sandbox/LICENSE).
