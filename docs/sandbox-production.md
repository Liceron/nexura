# Production gVisor profile

This page is for operators who run model-written code on a server and want the strongest
isolation the SDK offers: the `gvisor_oci` backend of the `nexura-sandbox` package, run as
the hardened `gvisor_production` profile. It covers the profile, a host checklist, the
preflight check and its limits. The backend itself, its OCI policy and cleanup are described
in [Tools: gVisor OCI](tools.md#gvisor-oci).

> **Status.** Verified on one Linux host only: Ubuntu x86_64, kernel 7.0, cgroup v2,
> runsc `release-20260928.0`, with runsc run as root through `sudo -n` (a test-only command
> prefix) and the cgroup parent placed under a systemd-delegated subtree (Ubuntu 26.04.1,
> kernel 7.0.0-1014-gcp). A service running
> directly as root with a top-level `/nexura` cgroup was **not** exercised. The profile has
> **not** been externally audited. macOS and Windows are not supported; gVisor needs Linux.

## The profile

```python
import os

from nexura_runtime.integrations.sandbox import SandboxConfig, SandboxExecutionModule

config = SandboxConfig.gvisor_production(
    runsc_path="/usr/local/bin/runsc",
    rootfs_path="/opt/nexura/gvisor-rootfs",
    cgroup_parent="/nexura",
    # The output of `sha512sum /usr/local/bin/runsc` (128 hex digits); None skips the pin.
    runsc_sha512=os.environ.get("NEXURA_SANDBOX_RUNSC_SHA512"),
)
module = SandboxExecutionModule(config=config)
# On a host without runsc or the cgroup parent, nothing qualifies: the module stays hidden,
# refuses every call with CAPABILITY_UNAVAILABLE and names what is missing.
print(module.selection_error or "gvisor_oci selected")
```

Or from the operator environment, before the service starts:

```text
NEXURA_SANDBOX_PROFILE=gvisor_production
NEXURA_RUNSC_PATH=/usr/local/bin/runsc
NEXURA_OCI_ROOTFS_PATH=/opt/nexura/gvisor-rootfs
NEXURA_SANDBOX_GVISOR_CGROUP_PARENT=/nexura          # default /nexura
NEXURA_SANDBOX_RUNSC_SHA512=<128 hex digits>         # optional, recommended
```

`NEXURA_SANDBOX_PROFILE` enables the sandbox by itself; `NEXURA_SANDBOX_BACKENDS` is not
needed. The limit variables (`NEXURA_SANDBOX_TIMEOUT_SECONDS`, `_MAX_MEMORY_MB`,
`_MAX_OUTPUT_BYTES`) still apply.

What the profile **enforces**: `SandboxConfig` validation fails, and the backend refuses to
run, if any of these does not hold.

| Setting | Rule |
| --- | --- |
| Backends | exactly `["gvisor_oci"]`, nothing to fall back to |
| `min_isolation` | `gvisor` |
| Network | none (`--network=none`; the guest sees only `lo`) |
| Root filesystem | read-only |
| Host cgroups | required: memory and pids limits per execution; `allow_without_cgroups` is refused even with `NEXURA_REAL_GVISOR=1` |
| Command prefix | none, or exactly the test-only `["sudo", "-n"]` (which also needs `NEXURA_REAL_GVISOR=1` and is refused by the shared service); anything else is refused |
| Paths | `runsc_path`, `rootfs_path` and the cgroup parent explicit, absolute and normalised: one leading `/`, no `..`, `.`, `//` or trailing `/`; no `PATH` or environment fallback inside the constructor |
| Cgroup parent | below the cgroup root, must already exist |
| Ceilings | `timeout_seconds` ≤ 60, `max_memory_mb` ≤ 1024, `max_processes` ≤ 16, `gvisor_runtime_pids` ≤ 256, `gvisor_tmpfs_bytes` ≤ 128 MiB, `max_output_bytes` ≤ 4 MiB (`GVISOR_PRODUCTION_CEILINGS`) |

What the profile only **defaults** (you may change these within the ceilings):

| Setting | Default |
| --- | --- |
| Guest processes | 1: fork, vfork, clone and clone3 are denied by OCI seccomp, so threads too. With `max_processes` above 1 that filter is omitted; guest NPROC and the pids cgroup still apply |
| Limits | 10 s wall time, 256 MB memory, 1 MiB output, 32 MiB scratch tmpfs, 128 sentry/gofer task allowance |

Pass other fields as keyword arguments, for example a lower `timeout_seconds`, or
`max_processes=4` if the code needs subprocesses. Anything that breaks a rule above raises
`ValueError`. The gVisor backend re-checks the rules at every probe and execution, because
`SandboxConfig.model_copy(update=...)` skips validation. (A `model_copy` that also removes
`hardened_profile` drops the rules with it; no SDK code path does that with untrusted input.)

On top of the configuration checks every backend does, the profile's availability probe
checks that the cgroup parent exists under a cgroup v2 mount and lists `memory` and `pids`
in both `cgroup.controllers` and `cgroup.subtree_control`. When the probe fails, the module
selects no backend, stays hidden and returns `CAPABILITY_UNAVAILABLE` naming the problem;
nothing falls back to a weaker sandbox.

**The SHA-512 pin is a drift check, not an exec-time integrity check.** If
`runsc_sha512` (`gvisor_runsc_sha512`, `NEXURA_SANDBOX_RUNSC_SHA512`) is set, the service
user hashes the bytes at `runsc_path` (following symlinks) when the module is built and at
the start of every execution, and refuses on a mismatch. The hash is cached per file
identity, size and timestamps, so a replaced or rewritten binary is hashed again. root then
opens and executes the path separately, later; the crash watchdog and teardown run runsc
without re-checking the pin. The `gvisor-bin/` sidecars, which also run as root, are not
pinned at all. What actually protects these files is a directory chain only root can write,
which the preflight requires (see below). The pin catches upgrades and drift. It works with
or without the profile, and setting the variable never fails open: an invalid value is an
error, and setting it (or `NEXURA_SANDBOX_GVISOR_CGROUP_PARENT`) without enabling the sandbox
is an error too.

The `authenticated-shared-service` profile of `create_api_app` accepts this profile. It
still refuses the test-only `runsc_command_prefix` and cgroup bypass, and it refuses to start
when the operator's sandbox settings are invalid or a registered sandbox module cannot be
built.

## Operator checklist

1. **Run the service as root on a dedicated host or VM.** runsc needs root to create its
   sandbox (namespaces, cgroups, its gofer). A non-root runsc failed on the reviewed host while
   writing `/sys/fs/cgroup/cgroup.subtree_control`. Rootless runsc was not evaluated. The
   `sudo -n` command prefix used by the SDK's own live tests is test-only and is refused by
   the shared service.

2. **Install and pin runsc.** Follow the
   [gVisor install guide](https://gvisor.dev/docs/user_guide/install/), but use a dated
   release URL (`release/<yyyymmdd>` or `release/<yyyymmdd>.<rc>`) instead of `latest`:

   ```bash
   ARCH=$(uname -m)
   URL=https://storage.googleapis.com/gvisor/releases/release/20260928.0/${ARCH}
   wget ${URL}/gvisor.tar.zstd ${URL}/gvisor.tar.zstd.sha512   # or gvisor.tar.bz2 without zstd
   sha512sum -c gvisor.tar.zstd.sha512
   sudo tar --zstd -xf gvisor.tar.zstd -C /usr/local/bin      # runsc and gvisor-bin/
   sudo chown -R root:root /usr/local/bin/runsc /usr/local/bin/gvisor-bin
   sudo chmod -R go-w /usr/local/bin/runsc /usr/local/bin/gvisor-bin
   sha512sum /usr/local/bin/runsc   # use this value as runsc_sha512
   ```

   Extract the whole archive, not only `runsc`. `release-20260928.0` starts sidecar
   binaries (`gvisor_sentry`, `gvisor-sentry-prewarmer` and others) from a `gvisor-bin/`
   directory next to runsc and refuses to start without them: on the reviewed host a
   copied `runsc` alone failed with `sidecar "gvisor_sentry" not usable ... and
   --sidecar-usage-policy is set to STRICT`. The sidecars run with runsc's privileges.

   Keep runsc and `gvisor-bin/` root-owned, not writable by group or others, in a
   directory only root can write. The pin
   is the hash of the installed `runsc` binary, not of the release archive (the archive's
   `.sha512` file only proves the download). Upgrade by installing the new release,
   updating the pin and rerunning the preflight. The pin covers `runsc` only; the
   sidecars are protected by their file permissions (checked by the preflight, which also
   reports their hashes). runsc's own `--sidecar-release-enforcement-policy` (default
   `IF_RELEASE_BUILD`) says it checks that spawned sidecars match its release; the SDK does
   not rely on or verify that mechanism. The reviewed host used the
   `gvisor.tar.bz2` archive of `release-20260928.0`; the archive layout shown above is
   from the install guide and was not re-downloaded for this page.

3. **Build the rootfs.** Any OCI image's unpacked filesystem with Python 3 in
   `/usr/local/bin`, `/usr/bin` or `/bin` works. The reviewed host used a Debian-based
   CPython 3.12 image exported to a tar file:

   ```bash
   # On a build machine (needs an image tool; Docker shown):
   docker create --name nexura-rootfs python:3.12-slim
   docker export nexura-rootfs -o python-rootfs.tar && docker rm nexura-rootfs
   # On the sandbox host, as root:
   mkdir -p /opt/nexura/gvisor-rootfs
   tar -xf python-rootfs.tar -C /opt/nexura/gvisor-rootfs
   chown -R root:root /opt/nexura/gvisor-rootfs
   chmod -R go-w /opt/nexura/gvisor-rootfs
   ```

   The guest runs as UID/GID 65534 (`nobody`), so the interpreter and standard library
   must be readable (and the interpreter executable) by others. Install any packages the
   code may import into the image before export; the guest has no network to install them.
   Pin the image by digest so rebuilds are reproducible.

4. **Provide the cgroup parent.** The parent must exist before the service starts, with
   the memory and pids controllers enabled for its children. runsc creates one child per
   execution, named after the container, and the backend removes it in teardown.

   - Root service, top-level parent (not exercised on the reviewed host):

     ```bash
     mkdir /sys/fs/cgroup/nexura
     echo '+memory +pids' > /sys/fs/cgroup/nexura/cgroup.subtree_control
     ```

     Recreate it at boot, for example with a systemd `ExecStartPre=` or a tmpfiles
     rule. The cgroup root must already enable `memory` and `pids` in its own
     `cgroup.subtree_control`; systemd does this on current distributions.
   - Under a delegated systemd subtree (the layout the live tests used): with
     `Delegate=yes`, systemd hands the unit's cgroup to the service. cgroup v2 forbids
     processes in a cgroup that delegates controllers to children, so the service's own
     processes must live in a leaf child, and the sandbox parent is a sibling of that
     leaf. Pass the parent's path relative to the cgroup mount, for example
     `/system.slice/nexura.service/sandboxes`.

5. **Run the preflight** (next section) after every install, upgrade, rootfs rebuild or
   host change, and before serving.

6. **Keep the service in its own cgroup and supervise it.** Use systemd with
   `KillMode=control-group` (or a container) so a service crash also stops its runsc
   processes. Each execution also has a crash watchdog, but that cannot help if the
   watchdog itself is killed.

7. **Set `TMPDIR` to a root-only directory** (for example `/var/lib/nexura/tmp`, mode
   0700). Task directories (`nexura_gvisor_*`) are created in the service's temporary
   directory and hold the payload, the OCI bundle and runsc state.

## The preflight

The availability probe checks configuration. The preflight also checks the files and the
host, then starts one tiny guest:

```bash
python -m nexura_sandbox.preflight \
  --runsc /usr/local/bin/runsc --rootfs /opt/nexura/gvisor-rootfs \
  --cgroup-parent /nexura --runsc-sha512 <hex> --expect-version release-20260928.0
# or build the config from the operator environment:
python -m nexura_sandbox.preflight --from-env --strict
```

From Python: `nexura_sandbox.gvisor_preflight(config)` (or `await gvisor_preflight_async(config)`
inside an event loop) returns a `PreflightReport`; it never raises for a bad configuration or
host state, which becomes a failed check instead. Each `PreflightCheck` has a name, a status
(`pass`, `warn`, `fail`, `skip`), what was found and a fix. `--json` prints the report as JSON.
The exit code is 0 when the preflight passed, 1 when it failed and 2 for an invalid
configuration.

Every path that root executes or uses must be safe, and **an unsafe one fails the default
preflight**: runsc, the `gvisor-bin/` sidecars, the rootfs and every directory above them
must be owned by root or the service user and not writable by group or others. A
group-writable ancestor fails even when the group has no other members today. Warnings remain
only for things that are not a replacement path: runsc owned by the service user rather than
root, no pin, the test-only command prefix or a non-root service, a missing `gvisor-bin/`
(older releases embed the sidecars), and a root-owned sticky directory such as `/tmp` above
the install (others cannot replace entries they do not own, but production paths should not
pass through it). `--strict` fails on those warnings too; use it in deployment pipelines.

| Check | What it verifies |
| --- | --- |
| `profile` | the config is `gvisor_production` and still satisfies every rule |
| `runsc_path`, `runsc_permissions` | absolute, existing, executable; the binary and every directory above it (as configured and resolved) are owned by root or the service user and not writable by group or others (fail); runsc not owned by root warns |
| `runsc_sidecars` | `gvisor-bin/` next to the resolved runsc holds only regular files, none writable by group or others; its parent directories are those checked by `runsc_permissions`; their SHA-512 hashes are in the report facts (missing directory: warning) |
| `runsc_sha512` | the binary matches the pin (no pin: warning with the current hash) |
| `runsc_version`, `runsc_features` | `runsc --version` parses, optionally equals `--expect-version`; `runsc features` reports seccomp enabled with the `ERRNO` and `ALLOW` actions, and `runsc flags` lists `-oci-seccomp` |
| `rootfs_path`, `rootfs_permissions` | absolute directory; the rootfs, its entries (up to 200,000) and every directory above it are owned by root or the service user and not writable by group or others |
| `rootfs_python` | `python3` on the guest `PATH` and the standard library are readable/executable by UID 65534, including every directory on the way |
| `cgroup_v2`, `cgroup_parent` | cgroup v2 is mounted; the parent path is normalised, exists, delegates memory and pids, and is owned by root or the service user and not writable by group or others |
| `runsc_privilege` | the service is root (a test prefix or a non-root service warns) |
| `guest_run` | a guest prints a random token |
| `guest_cgroup_limits` | while the guest runs, the preflight reads its cgroup leaf's `memory.max` and `pids.max` from the host and they equal the configured limits; a leaf that is never observed, or other values, **fail** (a configuration label is not evidence) |
| `cleanup` | afterwards no task directory, runsc state, mount, runsc process or cgroup child of that execution remains |
| `preflight_error` | present only when the preflight hit an unexpected error; always a failure |

The preflight never executes a runsc binary that failed its path, ownership, mode, sidecar
or pin check. If any check fails, the guest run is skipped. It does not change any file, mode or
cgroup.

On the reviewed host the install lives under a project directory that is
group-writable, so the honest preflight result there is **FAILED**, naming that directory in
`runsc_permissions` and `rootfs_permissions`. A hard-linked copy of the same runsc,
`gvisor-bin/` and rootfs in a directory chain only the service user can write passes every
check (see the live tests).

## Monitoring and cleanup

- **Per execution:** results carry `details.cgroups == "enforced"`,
  `limits_advisory == false` and `hardened_profile == "gvisor_production"`. An exit code of
  137 with no output past a point usually means the memory cgroup's OOM killer stopped the
  sandbox. The parent's `memory.events` (`oom_kill`) and `pids.events` (`max`) count limit
  hits.
- **Leftovers:** after the service stops, none of these should remain. Check for task
  directories with `ls -d "$TMPDIR"/nexura_gvisor_*`, null-netns mounts with
  `findmnt -rn | grep nexura_gvisor_`, cgroup children with `ls /sys/fs/cgroup/nexura`, and
  runsc processes with `pgrep -af runsc`.
- **Startup sweep:** on its first execution a worker removes stale task directories it
  can attribute to the same service identity, after `runsc list` proves the container
  absent, together with that run's recorded cgroup child once it is empty. Unmarked or
  ambiguous state is left for the operator.
- **Manual cleanup**, as root, only after confirming the service is stopped: for a task
  directory `T`, `runsc --root=T/state delete --force <container>`, then
  `umount T/state/null-netns` if it is mounted, then `rm -rf --one-file-system T`. Remove
  an empty cgroup child with `rmdir`; a cgroup that still has processes cannot be removed,
  so stop the processes first.
- **Teardown removes the cgroup child.** A cancelled execution can stop runsc before it
  deletes its own cgroup. Normal teardown, the per-run crash watchdog and the startup sweep
  therefore remove the run's child `<parent>/nex_<32 hex>` once it is empty (through the
  same privilege as runsc). A populated child is never touched; teardown and the watchdog
  report it if it remains after 3 seconds. If the watchdog itself is killed, an empty child
  can remain until the next startup sweep or manual `rmdir`.

## Limits

- Verified on one host only (see the status note above). Other kernels, distributions,
  cgroup layouts, architectures and runsc releases are untested. arm64 is untested.
- The live tests ran runsc as root through `sudo -n`, a test-only prefix. They did not run
  a root service with a top-level `/nexura` parent.
- gVisor is a user-space kernel. It reduces, but does not remove, exposure to host kernel
  bugs and to bugs in gVisor itself. Keep runsc up to date.
- The SHA-512 pin covers the `runsc` binary, not the `gvisor-bin/` sidecars or the rootfs,
  and it is checked by the service user before execution, not at the moment root executes
  runsc. Root-only directories, which the preflight requires, are the real protection.
- The memory cgroup bounds the whole sandbox (sentry, gofer and guest). Scratch files on
  the `/sandbox` tmpfs count against it. `RLIMIT_AS` additionally caps a single guest
  allocation.
- CPU time is bounded only by the wall-clock timeout; there is no CPU quota (`cpu.max`).
- Not externally audited. No production-readiness or certification claim is made.
- macOS and Windows: unsupported.
