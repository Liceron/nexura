# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Operator preflight for the ``gvisor_production`` sandbox profile.

The backend's ``probe`` only checks configuration. This preflight also checks the files and
the host, then starts one tiny guest and proves that it ran under enforced cgroups and left
nothing behind::

    python -m nexura_sandbox.preflight --runsc /usr/local/bin/runsc \\
        --rootfs /opt/nexura/gvisor-rootfs --cgroup-parent /nexura --runsc-sha512 <hex>

or, from Python, ``gvisor_preflight(SandboxConfig.gvisor_production(...))``. Each check
reports ``pass``, ``warn``, ``fail`` or ``skip`` with a fix hint. The guest run is skipped
when any earlier check failed: the preflight never launches a runsc binary (possibly as root)
that failed its ownership, mode or pin checks. ``--strict`` also fails on warnings.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import grp
import json
import os
import pwd
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import threading
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from nexura_runtime.integrations.sandbox.backends import sandbox_config_from_env
from nexura_runtime.integrations.sandbox.errors import CapabilityUnavailableError
from nexura_runtime.integrations.sandbox.models import (
    GVISOR_PRODUCTION_PROFILE,
    ExecutionRequest,
    SandboxConfig,
    gvisor_production_violations,
)
from nexura_sandbox import gvisor_worker
from nexura_sandbox.gvisor_worker import (
    REQUIRED_CGROUP_CONTROLLERS,
    GVisorOCIWorker,
    cgroup_parent_dir,
    cgroup_parent_problem,
    discover_oci_infrastructure,
    query_runsc,
    runsc_sha512,
)

Status = Literal["pass", "warn", "fail", "skip"]

#: UID/GID the guest runs as (see ``build_gvisor_oci_spec``).
GUEST_ID = 65534
#: The guest's PATH, in order; the OCI process runs ``python3`` from it.
GUEST_PATH = ("/usr/local/bin", "/usr/bin", "/bin")
#: Bound on rootfs entries inspected for unsafe permissions.
MAX_ROOTFS_ENTRIES = 200_000
#: Bound on the output read from one runsc helper command.
MAX_COMMAND_OUTPUT = 4 * 1024 * 1024
_VERSION = re.compile(r"runsc version (release-\d{8}\.\d+)")


@dataclass(frozen=True)
class PreflightCheck:
    """One preflight check: its name, outcome, what was found and how to fix a problem."""

    name: str
    status: Status
    detail: str
    fix: str = ""


@dataclass(frozen=True)
class PreflightReport:
    """All checks of one preflight run. ``ok`` is the verdict (``strict`` fails on warnings)."""

    checks: tuple[PreflightCheck, ...]
    strict: bool = False
    facts: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """True when nothing failed (and, in strict mode, nothing warned)."""
        bad: set[str] = {"fail", "warn"} if self.strict else {"fail"}
        return not any(check.status in bad for check in self.checks)

    def check(self, name: str) -> PreflightCheck:
        """The check called ``name`` (``KeyError`` when it did not run)."""
        for item in self.checks:
            if item.name == name:
                return item
        raise KeyError(name)

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready form, as printed by ``--json``."""
        return {
            "ok": self.ok,
            "strict": self.strict,
            "checks": [asdict(item) for item in self.checks],
            "facts": self.facts,
        }

    def format_text(self) -> str:
        """Human-readable lines, one per check, with the fix under each warning or failure."""
        lines = []
        for item in self.checks:
            lines.append(f"[{item.status.upper():4}] {item.name}: {item.detail}")
            if item.fix and item.status in ("warn", "fail", "skip"):
                lines.append(f"       fix: {item.fix}")
        verdict = "PASSED" if self.ok else "FAILED"
        mode = " (strict: warnings fail)" if self.strict else ""
        lines.append(f"gVisor production preflight {verdict}{mode}")
        return "\n".join(lines)


class _Checks:
    def __init__(self) -> None:
        self.items: list[PreflightCheck] = []

    def add(self, name: str, status: Status, detail: str, fix: str = "") -> PreflightCheck:
        item = PreflightCheck(name, status, detail, fix)
        self.items.append(item)
        return item

    @property
    def failed(self) -> bool:
        return any(item.status == "fail" for item in self.items)


# --- file ownership and modes ------------------------------------------------------------


def _other_group_members(gid: int, owner_uid: int) -> list[str]:
    """Users other than the owner who belong to ``gid`` (supplementary or primary)."""
    if gid == 0:
        return []
    try:
        owner = pwd.getpwuid(owner_uid).pw_name
    except KeyError:
        owner = str(owner_uid)
    members: set[str] = set()
    with contextlib.suppress(KeyError):
        members.update(grp.getgrgid(gid).gr_mem)
    members.update(entry.pw_name for entry in pwd.getpwall() if entry.pw_gid == gid)
    members.discard(owner)
    return sorted(members)


def _group_name(gid: int) -> str:
    try:
        return grp.getgrgid(gid).gr_name
    except KeyError:
        return str(gid)


def _chain(path: Path) -> list[Path]:
    """``path`` and each ancestor up to ``/``, nearest first."""
    return [path, *path.parents]


def _entry_problems(
    path: Path, st: os.stat_result, trusted: set[int], *, is_target: bool
) -> tuple[list[str], list[str]]:
    """(failures, warnings) for one file or directory on a path that root uses.

    Fails on an owner other than root or the service user, and on any group or world write
    permission: whoever has it can replace what is below. The single exception, reported as
    a warning (so ``--strict`` fails it), is a root-owned *sticky* world-writable ancestor
    directory such as ``/tmp``: there, other users cannot rename or delete an entry they do
    not own, and the next path component is itself checked for a trusted owner.
    """
    fails: list[str] = []
    warns: list[str] = []
    mode = st.st_mode
    if st.st_uid not in trusted:
        fails.append(f"{path} is owned by uid {st.st_uid}, not root or the service user")
    writable = mode & (stat.S_IWOTH | stat.S_IWGRP)
    if writable and stat.S_ISDIR(mode) and mode & stat.S_ISVTX and st.st_uid == 0 and not is_target:
        warns.append(
            f"{path} is a root-owned sticky directory writable by others (mode "
            f"{stat.S_IMODE(mode):o}; they cannot replace entries they do not own, but "
            "production paths should not pass through it)"
        )
        return fails, warns
    if mode & stat.S_IWOTH:
        fails.append(f"{path} is world-writable (mode {stat.S_IMODE(mode):o})")
    if mode & stat.S_IWGRP:
        members = _other_group_members(st.st_gid, st.st_uid)
        who = f", whose other members {members} can replace what is below it" if members else ""
        fails.append(
            f"{path} is group-writable (mode {stat.S_IMODE(mode):o}, group "
            f"{_group_name(st.st_gid)}{who})"
        )
    return fails, warns


def _trusted_path_problems(path: Path, trusted: set[int]) -> tuple[list[str], list[str]]:
    """Check ``path`` (as configured and resolved) and every ancestor directory."""
    fails: list[str] = []
    warns: list[str] = []
    seen: set[Path] = set()
    resolved = path.resolve()
    for chain_root in (path, resolved):
        for index, entry in enumerate(_chain(chain_root)):
            if entry in seen:
                continue
            seen.add(entry)
            try:
                st = entry.lstat()
            except OSError as exc:
                fails.append(f"cannot stat {entry}: {exc}")
                continue
            if stat.S_ISLNK(st.st_mode):
                # A symlink's own mode is meaningless; its directory is checked separately.
                if st.st_uid not in trusted:
                    fails.append(f"symlink {entry} is owned by uid {st.st_uid}")
                continue
            f, w = _entry_problems(entry, st, trusted, is_target=index == 0)
            fails += f
            warns += w
    return fails, warns


def _nobody_can(st: os.stat_result, *, read: bool, execute: bool) -> bool:
    if st.st_uid == GUEST_ID:
        r, x = stat.S_IRUSR, stat.S_IXUSR
    elif st.st_gid == GUEST_ID:
        r, x = stat.S_IRGRP, stat.S_IXGRP
    else:
        r, x = stat.S_IROTH, stat.S_IXOTH
    return (not read or bool(st.st_mode & r)) and (not execute or bool(st.st_mode & x))


def _resolve_in_rootfs(rootfs: Path, guest_path: str, hops: int = 40) -> Path | None:
    """Host path of ``guest_path`` inside ``rootfs``, following links as the guest would."""
    parts = [part for part in guest_path.split("/") if part]
    current = rootfs
    while parts:
        part = parts.pop(0)
        if part == ".":
            continue
        if part == "..":
            if current != rootfs:
                current = current.parent
            continue
        candidate = current / part
        try:
            st = candidate.lstat()
        except OSError:
            return None
        if stat.S_ISLNK(st.st_mode):
            hops -= 1
            if hops < 0:
                return None
            target = os.readlink(candidate)
            if target.startswith("/"):
                current = rootfs
            parts = [p for p in target.split("/") if p] + parts
            continue
        current = candidate
    return current


def _guest_search_problems(rootfs: Path, host_path: Path) -> list[str]:
    """Directories between the rootfs and ``host_path`` that UID 65534 cannot traverse."""
    problems = []
    for relative in reversed(host_path.relative_to(rootfs).parents):
        target = rootfs / relative
        try:
            st = target.stat()
        except OSError as exc:
            problems.append(f"cannot stat {target}: {exc}")
            continue
        if not _nobody_can(st, read=False, execute=True):
            problems.append(f"{target} is not searchable by UID {GUEST_ID}")
    return problems


# --- bounded helper commands --------------------------------------------------------------


def _run(args: Sequence[str], timeout: float = 10.0) -> tuple[int, str]:
    """A non-privileged runsc query through the reviewed launcher in ``gvisor_worker``."""
    return query_runsc(list(args), timeout=timeout, max_output=MAX_COMMAND_OUTPUT)


# --- leftovers ----------------------------------------------------------------------------


def _task_dirs(tmp_root: Path) -> set[Path]:
    with contextlib.suppress(OSError):
        return {path for path in tmp_root.glob("nexura_gvisor_*") if not path.is_symlink()}
    return set()


def _task_container(task: Path) -> str | None:
    with contextlib.suppress(OSError, ValueError, KeyError, TypeError):
        value = json.loads((task / "owner.json").read_text())["container"]
        return value if isinstance(value, str) else None
    return None


def _task_mounts() -> list[str]:
    mounts = []
    with contextlib.suppress(OSError):
        for line in Path("/proc/self/mountinfo").read_text().splitlines():
            point = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), line.split()[4])
            if "/nexura_gvisor_" in point:
                mounts.append(point)
    return mounts


def _processes_mentioning(token: str) -> list[int]:
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        with contextlib.suppress(OSError):
            if token.encode() in (entry / "cmdline").read_bytes():
                found.append(int(entry.name))
    return found


def gvisor_leftovers(
    container_id: str,
    *,
    cgroup_parent: Path | None = None,
    tmp_root: Path | None = None,
) -> dict[str, list[str]]:
    """Host state still referring to ``container_id``: task dirs, mounts, processes, cgroups.

    Every list is empty after a clean run. Task directories are found by their
    ``owner.json`` marker; a runsc container's state root lives inside its task directory, so
    no task directory also means no runsc container record. Mounts are those under any
    ``nexura_gvisor_*`` task directory that belongs to the container. ``cgroup_parent`` (a
    host path under the cgroup mount) limits the cgroup search to that parent's children.
    """
    root = tmp_root or Path(tempfile.gettempdir())
    tasks = [task for task in _task_dirs(root) if _task_container(task) == container_id]
    mounts = [
        point
        for point in _task_mounts()
        if any(point == str(task) or point.startswith(f"{task}/") for task in tasks)
    ]
    if cgroup_parent is not None:
        candidates: Iterable[Path] = [cgroup_parent / container_id]
    else:
        candidates = gvisor_worker.CGROUP_ROOT.glob(f"**/{container_id}")
    leaves = [str(path) for path in candidates if path.is_dir()]
    return {
        "task_dirs": [str(task) for task in tasks],
        "mounts": mounts,
        "processes": [str(pid) for pid in _processes_mentioning(container_id)],
        "cgroups": leaves,
    }


class _CgroupWatcher:
    """Polls the cgroup parent during the guest run and records each new leaf's limits."""

    def __init__(self, parent: Path) -> None:
        self.parent = parent
        self.before = self._leaves()
        self.seen: dict[str, dict[str, str]] = {}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def _leaves(self) -> set[str]:
        with contextlib.suppress(OSError):
            return {path.name for path in self.parent.glob("nex_*") if path.is_dir()}
        return set()

    def _loop(self) -> None:
        # Re-read every poll while a leaf exists: runsc creates the cgroup before it writes
        # the limits, so an early read can see the default ("max"). The last values read
        # before the leaf disappears are the ones the run had.
        while not self._stop.is_set():
            for name in self._leaves() - self.before:
                values = {}
                for key in ("memory.max", "pids.max"):
                    with contextlib.suppress(OSError):
                        values[key] = (self.parent / name / key).read_text().strip()
                if len(values) == 2 and all(values.values()):
                    self.seen[name] = values
            self._stop.wait(0.02)

    def __enter__(self) -> _CgroupWatcher:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=2)


# --- the preflight ------------------------------------------------------------------------


def _check_profile(checks: _Checks, config: SandboxConfig) -> None:
    if config.hardened_profile != GVISOR_PRODUCTION_PROFILE:
        checks.add(
            "profile",
            "fail",
            "the config is not the gvisor_production profile",
            "build it with SandboxConfig.gvisor_production(runsc_path=..., rootfs_path=..., "
            "cgroup_parent=...) or NEXURA_SANDBOX_PROFILE=gvisor_production",
        )
        return
    problems = gvisor_production_violations(config)
    if problems:
        checks.add("profile", "fail", "; ".join(problems), "use the profile's defaults")
    else:
        checks.add(
            "profile",
            "pass",
            f"gvisor_production: timeout {config.timeout_seconds}s, memory "
            f"{config.max_memory_mb} MB, {config.max_processes} guest process(es), no network",
        )


def _check_runsc(checks: _Checks, config: SandboxConfig, trusted: set[int]) -> Path | None:
    raw = config.runsc_path or ""
    path = Path(raw)
    if not raw or not path.is_absolute() or ".." in path.parts:
        checks.add(
            "runsc_path",
            "fail",
            f"runsc_path {raw!r} is not an explicit absolute path",
            "pass the absolute path of the installed runsc, e.g. /usr/local/bin/runsc",
        )
        return None
    if not path.is_file():
        checks.add(
            "runsc_path",
            "fail",
            f"{raw} does not exist or is not a regular file",
            "install runsc there (see docs/sandbox-production.md) or fix the path",
        )
        return None
    if not os.access(path, os.X_OK):
        checks.add("runsc_path", "fail", f"{raw} is not executable", "chmod 0755 the binary")
        return None
    checks.add("runsc_path", "pass", f"{raw} -> {path.resolve()}")
    fails, warns = _trusted_path_problems(path, trusted)
    owner = path.resolve().stat().st_uid
    if owner != 0:
        warns.append(f"runsc is owned by uid {owner}, not root, but runs as root")
    if fails:
        checks.add(
            "runsc_permissions",
            "fail",
            "; ".join(fails + warns),
            "runsc runs as root: install it and gvisor-bin/ root-owned, mode 0755, in a "
            "directory chain only root can write (for example tar -xf the release into "
            "/usr/local/bin as root), or remove group/other write from every listed path",
        )
    elif warns:
        checks.add(
            "runsc_permissions",
            "warn",
            "; ".join(warns),
            "production: install runsc root-owned, mode 0755, under /usr/local/bin",
        )
    else:
        checks.add(
            "runsc_permissions",
            "pass",
            "runsc and every directory above it are owned by root or the service user and "
            "not writable by group or others",
        )
    return path


def _check_sidecars(checks: _Checks, runsc: Path, trusted: set[int], facts: dict[str, Any]) -> None:
    """runsc releases from late 2026 start helper binaries from ``gvisor-bin/`` beside it.

    They run with runsc's privileges, so each must be as protected as runsc itself. The
    directory's ancestors are those of the resolved runsc, checked by ``runsc_permissions``.
    The ``runsc_sha512`` pin covers only runsc; the sidecars' hashes are reported in facts.
    """
    directory = runsc.resolve().parent / "gvisor-bin"
    if not directory.exists():
        checks.add(
            "runsc_sidecars",
            "warn",
            f"no {directory}; releases with sidecar binaries refuse to start without it",
            "extract the whole release archive (runsc and gvisor-bin/) into one directory",
        )
        return
    fails: list[str] = []
    hashes: dict[str, str] = {}
    try:
        st = directory.lstat()
    except OSError as exc:
        fails.append(f"cannot stat {directory}: {exc}")
    else:
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            fails.append(f"{directory} is not a real directory")
        else:
            fails += _entry_problems(directory, st, trusted, is_target=True)[0]
            for entry in sorted(directory.iterdir()):
                try:
                    est = entry.lstat()
                except OSError as exc:
                    fails.append(f"cannot stat {entry}: {exc}")
                    continue
                if not stat.S_ISREG(est.st_mode):
                    fails.append(f"{entry} is not a regular file")
                    continue
                fails += _entry_problems(entry, est, trusted, is_target=True)[0]
                try:
                    hashes[entry.name] = runsc_sha512(entry)
                except OSError as exc:
                    fails.append(f"cannot hash {entry}: {exc}")
    facts["sidecar_sha512"] = hashes
    if fails:
        checks.add(
            "runsc_sidecars",
            "fail",
            "; ".join(fails),
            "install gvisor-bin/ root-owned like runsc: chown -R root:root and chmod -R go-w",
        )
    else:
        checks.add(
            "runsc_sidecars",
            "pass",
            f"{len(hashes)} sidecar binaries in {directory}, none writable by group or "
            "others (its parent directories are covered by runsc_permissions; hashes in the "
            "report facts; the pin covers runsc only)",
        )


def _check_pin(checks: _Checks, config: SandboxConfig, runsc: Path) -> None:
    try:
        digest = runsc_sha512(runsc)
    except OSError as exc:
        checks.add("runsc_sha512", "fail", f"cannot hash runsc: {exc}", "check file permissions")
        return
    if config.gvisor_runsc_sha512 is None:
        checks.add(
            "runsc_sha512",
            "warn",
            f"no pin configured; this binary's sha512 is {digest}",
            "verify the download against the release's runsc.sha512, then pin it with "
            "runsc_sha512=... (NEXURA_SANDBOX_RUNSC_SHA512)",
        )
    elif digest == config.gvisor_runsc_sha512:
        checks.add("runsc_sha512", "pass", "runsc matches the pinned sha512")
    else:
        checks.add(
            "runsc_sha512",
            "fail",
            f"runsc sha512 {digest} does not match the pin {config.gvisor_runsc_sha512}",
            "reinstall the verified release or update the pin after verifying the new binary",
        )


def _check_runsc_runtime(
    checks: _Checks, runsc: Path, expected_version: str | None, facts: dict[str, Any]
) -> None:
    try:
        code, output = _run([str(runsc), "--version"])
    except (OSError, subprocess.SubprocessError) as exc:
        checks.add("runsc_version", "fail", f"runsc --version failed: {exc}", "reinstall runsc")
        return
    match = _VERSION.search(output)
    if code or not match:
        checks.add(
            "runsc_version",
            "fail",
            f"unexpected runsc --version output (exit {code}): {output[:200]!r}",
            "install an official gVisor release build of runsc",
        )
        return
    version = match.group(1)
    facts["runsc_version"] = version
    if expected_version and version != expected_version:
        checks.add(
            "runsc_version",
            "fail",
            f"runsc is {version}, expected {expected_version}",
            "install the pinned release, or update the expected version after review",
        )
    else:
        detail = version if expected_version else f"{version} (no expected version given)"
        checks.add("runsc_version", "pass", detail)

    try:
        code, output = _run([str(runsc), "features"])
        features = json.loads(output) if code == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        features = None
        output = str(exc)
    seccomp = (features or {}).get("linux", {}).get("seccomp", {}) if features else {}
    actions = seccomp.get("actions", []) if isinstance(seccomp, dict) else []
    missing = [name for name in ("SCMP_ACT_ALLOW", "SCMP_ACT_ERRNO") if name not in actions]
    try:
        flags_code, flags = _run([str(runsc), "flags"])
    except (OSError, subprocess.SubprocessError) as exc:
        flags_code, flags = 1, str(exc)
    has_flag = flags_code == 0 and re.search(r"^\s*-oci-seccomp\b", flags, re.M) is not None
    facts["seccomp_actions"] = actions
    if not features or not seccomp.get("enabled") or missing or not has_flag:
        problems = []
        if not features:
            problems.append(f"runsc features did not return JSON: {output[:200]!r}")
        elif not seccomp.get("enabled"):
            problems.append("runsc features reports seccomp disabled")
        if missing:
            problems.append(f"seccomp actions missing {missing}")
        if not has_flag:
            problems.append("runsc flags does not list -oci-seccomp")
        checks.add(
            "runsc_features",
            "fail",
            "; ".join(problems),
            "use a runsc release with OCI seccomp support (--oci-seccomp, ERRNO action)",
        )
    else:
        checks.add(
            "runsc_features",
            "pass",
            "seccomp enabled, SCMP_ACT_ERRNO/ALLOW supported, --oci-seccomp available",
        )


def _check_rootfs(checks: _Checks, config: SandboxConfig, trusted: set[int]) -> Path | None:
    raw = config.rootfs_path or ""
    path = Path(raw)
    if not raw or not path.is_absolute() or ".." in path.parts or not path.is_dir():
        checks.add(
            "rootfs_path",
            "fail",
            f"rootfs_path {raw!r} is not an existing absolute directory",
            "build the rootfs (see docs/sandbox-production.md) and pass its absolute path",
        )
        return None
    checks.add("rootfs_path", "pass", f"{raw} -> {path.resolve()}")
    rootfs = path.resolve()
    fails, warns = _trusted_path_problems(path, trusted)
    unsafe: list[str] = []
    foreign: list[str] = []
    count = 0
    truncated = False
    for directory, dirnames, filenames in os.walk(rootfs, followlinks=False):
        for name in [*dirnames, *filenames]:
            count += 1
            if count > MAX_ROOTFS_ENTRIES:
                truncated = True
                break
            entry = Path(directory) / name
            try:
                st = entry.lstat()
            except OSError:
                continue
            if stat.S_ISLNK(st.st_mode):
                continue
            if st.st_mode & (stat.S_IWOTH | stat.S_IWGRP):
                unsafe.append(str(entry.relative_to(rootfs)))
            if st.st_uid not in trusted:
                foreign.append(f"{entry.relative_to(rootfs)} (uid {st.st_uid})")
        if truncated:
            break
    if unsafe:
        fails.append(
            f"{len(unsafe)} rootfs entries are writable by group or others, e.g. {unsafe[:5]}"
        )
    if foreign:
        fails.append(
            f"{len(foreign)} rootfs entries are owned by uids other than root or the service "
            f"user, e.g. {foreign[:5]}"
        )
    if truncated:
        warns.append(f"only the first {MAX_ROOTFS_ENTRIES} rootfs entries were inspected")
    if fails:
        checks.add(
            "rootfs_permissions",
            "fail",
            "; ".join(fails + warns),
            "as root: chown -R root:root the rootfs, chmod -R go-w it, and keep it in a "
            "directory chain only root can write (for example /opt/nexura/gvisor-rootfs)",
        )
    elif warns:
        checks.add(
            "rootfs_permissions",
            "warn",
            "; ".join(warns),
            "production: keep the rootfs root-owned under a root-only directory",
        )
    else:
        checks.add(
            "rootfs_permissions",
            "pass",
            f"{count} entries and every directory above the rootfs inspected; none writable "
            "by group or others, all owned by root or the service user",
        )
    return rootfs


def _check_rootfs_python(checks: _Checks, rootfs: Path) -> None:
    python = None
    for directory in GUEST_PATH:
        candidate = _resolve_in_rootfs(rootfs, f"{directory}/python3")
        if candidate is not None and candidate.is_file():
            python = candidate
            break
    if python is None:
        checks.add(
            "rootfs_python",
            "fail",
            f"no python3 in the guest PATH {':'.join(GUEST_PATH)} inside the rootfs",
            "build the rootfs from a Python image (docs/sandbox-production.md)",
        )
        return
    problems = _guest_search_problems(rootfs, python)
    if not _nobody_can(python.stat(), read=True, execute=True):
        problems.append(f"{python} is not readable and executable by UID {GUEST_ID}")
    stdlib = sorted(rootfs.glob("usr/local/lib/python3*/os.py")) + sorted(
        rootfs.glob("usr/lib/python3*/os.py")
    )
    if not stdlib:
        problems.append("no Python standard library (os.py) under usr/local/lib or usr/lib")
    else:
        os_py = stdlib[0]
        problems += _guest_search_problems(rootfs, os_py)
        if not _nobody_can(os_py.stat(), read=True, execute=False):
            problems.append(f"{os_py} is not readable by UID {GUEST_ID}")
    if problems:
        checks.add(
            "rootfs_python",
            "fail",
            "; ".join(problems),
            "make the interpreter and standard library world-readable (chmod -R a+rX)",
        )
    else:
        checks.add(
            "rootfs_python",
            "pass",
            f"/{python.relative_to(rootfs)} and its standard library are usable by UID {GUEST_ID}",
        )


def _check_cgroups(checks: _Checks, config: SandboxConfig, trusted: set[int]) -> Path | None:
    root = gvisor_worker.CGROUP_ROOT
    if not (root / "cgroup.controllers").is_file():
        checks.add(
            "cgroup_v2",
            "fail",
            f"no cgroup v2 hierarchy at {root}",
            "boot with the unified cgroup v2 hierarchy (systemd.unified_cgroup_hierarchy=1)",
        )
        return None
    checks.add("cgroup_v2", "pass", f"cgroup v2 at {root}")
    problem = cgroup_parent_problem(config)
    if problem:
        name = config.gvisor_cgroup_parent
        checks.add(
            "cgroup_parent",
            "fail",
            problem,
            f"as root: mkdir {root}{name} && echo '+memory +pids' > "
            f"{root}{name}/cgroup.subtree_control (or a systemd slice with Delegate=yes); "
            "the parent's own parent must already enable memory and pids",
        )
        return None
    parent = cgroup_parent_dir(config)
    try:
        st = parent.stat()
    except OSError as exc:
        checks.add("cgroup_parent", "fail", f"cannot stat {parent}: {exc}", "check the path")
        return None
    fails, warns = _entry_problems(parent, st, trusted, is_target=False)
    if fails:
        checks.add(
            "cgroup_parent",
            "fail",
            "; ".join(fails),
            "the cgroup parent must be owned by root or the service user, mode 0755",
        )
        return None
    controllers = ", ".join(REQUIRED_CGROUP_CONTROLLERS)
    if warns:
        checks.add("cgroup_parent", "warn", "; ".join(warns), f"chmod 0755 {parent}")
    else:
        checks.add("cgroup_parent", "pass", f"{parent} delegates {controllers}")
    return parent


def _check_privilege(checks: _Checks, config: SandboxConfig) -> None:
    try:
        _, _, prefix = discover_oci_infrastructure(config)
    except CapabilityUnavailableError as exc:
        checks.add(
            "runsc_privilege",
            "fail",
            exc.reason,
            "production runs the service as root; a command prefix is test-only",
        )
        return
    if os.geteuid() == 0:
        checks.add("runsc_privilege", "pass", "the service runs as root, as runsc requires")
    elif prefix:
        checks.add(
            "runsc_privilege",
            "warn",
            f"runsc runs through the test-only command prefix {prefix} "
            "(NEXURA_REAL_GVISOR=1); the authenticated shared service refuses this",
            "run the service as root in a dedicated host or VM; drop the prefix",
        )
    else:
        checks.add(
            "runsc_privilege",
            "warn",
            "the service is not root; on the reviewed host non-root runsc failed to start "
            "(it writes /sys/fs/cgroup/cgroup.subtree_control)",
            "run the service as root; the guest run below is the deciding check",
        )


async def gvisor_preflight_async(
    config: SandboxConfig,
    *,
    run_guest: bool = True,
    strict: bool = False,
    expected_version: str | None = None,
    guest_hold_seconds: float = 1.0,
) -> PreflightReport:
    """Check the host for ``config`` and, unless something failed, run one tiny guest.

    The guest prints a random token and stays up for ``guest_hold_seconds`` so the cgroup
    leaf's ``memory.max`` and ``pids.max`` can be read while it runs. Afterwards no task
    directory, mount, runsc process or cgroup leaf may remain.
    """
    checks = _Checks()
    facts: dict[str, Any] = {"euid": os.geteuid()}
    try:
        await _preflight(checks, facts, config, run_guest, expected_version, guest_hold_seconds)
    except Exception as exc:
        # Never a traceback: an unexpected host state or configuration is a failed check.
        checks.add(
            "preflight_error",
            "fail",
            f"the preflight stopped: {type(exc).__name__}: {exc}"[:2000],
            "fix the configuration or host state named here and rerun",
        )
    return PreflightReport(tuple(checks.items), strict, facts)


async def _preflight(
    checks: _Checks,
    facts: dict[str, Any],
    config: SandboxConfig,
    run_guest: bool,
    expected_version: str | None,
    guest_hold_seconds: float,
) -> None:
    trusted = {0, os.geteuid()}
    if not sys.platform.startswith("linux"):
        checks.add("platform", "fail", f"{sys.platform} is not supported", "use a Linux host")
        return
    checks.add("platform", "pass", "linux")
    _check_profile(checks, config)
    runsc = _check_runsc(checks, config, trusted)
    if runsc is not None:
        _check_sidecars(checks, runsc, trusted, facts)
        _check_pin(checks, config, runsc)
    if runsc is not None and not checks.failed:
        _check_runsc_runtime(checks, runsc, expected_version, facts)
    elif runsc is not None:
        checks.add(
            "runsc_version",
            "skip",
            "not executed: an earlier runsc check failed",
            "fix the failed runsc checks first",
        )
    rootfs = _check_rootfs(checks, config, trusted)
    if rootfs is not None:
        _check_rootfs_python(checks, rootfs)
    parent = _check_cgroups(checks, config, trusted)
    _check_privilege(checks, config)

    if not run_guest:
        checks.add("guest_run", "skip", "disabled (--no-guest-run)", "rerun without it")
        return
    if checks.failed or parent is None:
        checks.add(
            "guest_run",
            "skip",
            "not started: an earlier check failed, and runsc is never launched after a failure",
            "fix the failed checks and rerun",
        )
        return

    token = f"nexura-preflight-{uuid.uuid4().hex}"
    code = f"import time\nprint({token!r}, flush=True)\ntime.sleep({guest_hold_seconds!r})\n"
    run_config = config.model_copy(
        update={"timeout_seconds": max(config.timeout_seconds, guest_hold_seconds + 15.0)}
    )
    container_id = ""
    with _CgroupWatcher(parent) as watcher:
        try:
            result = await GVisorOCIWorker().execute(ExecutionRequest(code=code), run_config)
        except Exception as exc:
            notes = " ".join(getattr(exc, "__notes__", []))
            checks.add(
                "guest_run",
                "fail",
                f"{type(exc).__name__}: {exc} {notes}".strip()[:2000],
                "read the runsc error above; check root, cgroup delegation and the rootfs",
            )
            result = None
    if result is not None:
        container_id = str(result.details.get("container_id", ""))
        facts["container_id"] = container_id
        ran = result.exit_code == 0 and result.stdout.strip() == token
        enforced = (
            result.details.get("cgroups") == "enforced"
            and result.details.get("limits_advisory") is False
        )
        if ran and enforced:
            checks.add(
                "guest_run",
                "pass",
                f"guest printed the token in {result.duration_ms:.0f} ms (the cgroup limits are "
                "verified from the host by guest_cgroup_limits)",
            )
        else:
            checks.add(
                "guest_run",
                "fail",
                f"exit {result.exit_code}, stdout {result.stdout[:200]!r}, stderr "
                f"{result.stderr[:500]!r}, cgroups {result.details.get('cgroups')!r}",
                "check the rootfs Python and the runsc logs",
            )
        observed = watcher.seen.get(container_id)
        expected = {
            "memory.max": str(config.max_memory_mb * 1024 * 1024),
            "pids.max": str(config.max_processes + config.gvisor_runtime_pids),
        }
        if observed is None:
            # A config label is not evidence: without the leaf's own limits, this fails.
            checks.add(
                "guest_cgroup_limits",
                "fail",
                f"the cgroup leaf {container_id} was not observed under {parent} during the "
                "run, so the memory and pids limits are unproven",
                "check that runsc uses the configured cgroup parent and is run as root, rerun",
            )
        elif observed == expected:
            checks.add(
                "guest_cgroup_limits",
                "pass",
                f"leaf {parent}/{container_id}: memory.max={observed['memory.max']}, "
                f"pids.max={observed['pids.max']}",
            )
        else:
            checks.add(
                "guest_cgroup_limits",
                "fail",
                f"leaf limits {observed} differ from the configured {expected}",
                "check that runsc applies OCI resources on this host",
            )
    if container_id:
        leftovers = gvisor_leftovers(container_id, cgroup_parent=parent)
        facts["leftovers"] = leftovers
        if any(leftovers.values()):
            checks.add(
                "cleanup",
                "fail",
                f"state left behind: {leftovers}",
                "remove the listed state as root after inspection; see docs/sandbox-production.md",
            )
        else:
            checks.add(
                "cleanup",
                "pass",
                "no task directory, runsc state, mount, process or cgroup leaf left behind",
            )
    stale = _task_mounts()
    if stale:
        checks.add(
            "stale_state",
            "warn",
            f"mounts under nexura_gvisor_* task directories exist: {stale[:5]}",
            "these may belong to running executions; inspect before removing",
        )


def gvisor_preflight(
    config: SandboxConfig,
    *,
    run_guest: bool = True,
    strict: bool = False,
    expected_version: str | None = None,
) -> PreflightReport:
    """Synchronous ``gvisor_preflight_async``; call the async form inside an event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(
            gvisor_preflight_async(
                config, run_guest=run_guest, strict=strict, expected_version=expected_version
            )
        )
    raise RuntimeError("gvisor_preflight() cannot run inside an event loop; await the async form")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m nexura_sandbox.preflight",
        description="Check this host for the gvisor_production sandbox profile and start "
        "one tiny guest.",
    )
    parser.add_argument(
        "--from-env",
        action="store_true",
        help="build the config from the "
        "NEXURA_SANDBOX_* environment (NEXURA_SANDBOX_PROFILE=gvisor_production)",
    )
    parser.add_argument("--runsc", help="absolute path of runsc")
    parser.add_argument("--rootfs", help="absolute path of the OCI rootfs directory")
    parser.add_argument("--cgroup-parent", default="/nexura", help="cgroup v2 parent path")
    parser.add_argument("--runsc-sha512", help="pinned SHA-512 of runsc (128 hex digits)")
    parser.add_argument("--expect-version", help="required runsc release, e.g. release-20260928.0")
    parser.add_argument("--strict", action="store_true", help="fail on warnings too")
    parser.add_argument("--no-guest-run", action="store_true", help="static checks only")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    parser.add_argument(
        "--test-command-prefix",
        help="TEST ONLY: run runsc through this prefix (e.g. 'sudo -n'); needs "
        "NEXURA_REAL_GVISOR=1 and is refused by the shared service",
    )
    return parser


def build_config(args: argparse.Namespace) -> SandboxConfig:
    """The ``gvisor_production`` config the CLI arguments describe (``ValueError`` if invalid)."""
    if args.from_env:
        config = sandbox_config_from_env()
        if config is None:
            raise ValueError("NEXURA_SANDBOX_PROFILE / NEXURA_SANDBOX_BACKENDS are not set")
    else:
        if not args.runsc or not args.rootfs:
            raise ValueError("--runsc and --rootfs are required (or use --from-env)")
        config = SandboxConfig.gvisor_production(
            runsc_path=args.runsc,
            rootfs_path=args.rootfs,
            cgroup_parent=args.cgroup_parent,
            runsc_sha512=args.runsc_sha512.lower() if args.runsc_sha512 else None,
        )
    if args.test_command_prefix:
        extra = {**config.extra, "runsc_command_prefix": shlex.split(args.test_command_prefix)}
        config = config.model_copy(update={"extra": extra})
    return config


def main(argv: Iterable[str] | None = None) -> int:
    """CLI entry point: exit 0 when the preflight passed, 1 when it failed, 2 on bad input."""
    parser = _parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        config = build_config(args)
    except ValueError as exc:
        print(f"invalid configuration: {exc}", file=sys.stderr)
        return 2
    report = gvisor_preflight(
        config,
        run_guest=not args.no_guest_run,
        strict=args.strict,
        expected_version=args.expect_version,
    )
    print(json.dumps(report.to_dict(), indent=2) if args.json else report.format_text())
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
