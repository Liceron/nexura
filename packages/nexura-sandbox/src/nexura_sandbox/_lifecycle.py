# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Bounded Linux crash cleanup, loaded by the worker's stdlib-only watchdog.

The watchdog holds a pidfd for its service, not a numeric PID to poll. It pins
bwrap's trusted namespace init before the service releases the payload. It never
searches /proc for victims or signals a PID learned from untrusted code.
"""

from __future__ import annotations

import contextlib
import ctypes
import hashlib
import json
import os
import re
import selectors
import signal
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any


def enable_subreaper() -> None:
    """For dedicated supervisors (and explicit tests), never the hosting service."""
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0):  # PR_SET_CHILD_SUBREAPER
        raise OSError(ctypes.get_errno(), "PR_SET_CHILD_SUBREAPER failed")


def supervise(spawn: Callable[[], subprocess.Popen[bytes]], parent_fd: int) -> int:
    """Run in a dedicated child; kill and reap only its own sandbox descendants.

    A pidfd pins the service even if it dies before this interpreter starts.
    SIGTERM requests bounded teardown without killing the reaper itself. No
    waitpid(-1) or subreaper attribute is used in the embedding application.
    """
    stopping = False

    def stop(signum: int, frame: Any) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    with selectors.DefaultSelector() as selector:
        selector.register(parent_fd, selectors.EVENT_READ)
        # Do not launch after a service crash or an early teardown request.
        if stopping or selector.select(0):
            return 1
        proc = spawn()
        child_fd = os.pidfd_open(proc.pid)
        try:
            selector.register(child_fd, selectors.EVENT_READ)
            while not stopping and not selector.select(0.01):
                pass
        finally:
            # Do this before wait(): an unreaped direct child pins the group ID.
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            os.close(child_fd)
        rc = proc.wait(timeout=0.5)
    # Parent exits can strand descendants that used setsid(). Only this
    # supervisor's direct/adopted children are eligible, never global /proc PIDs.
    deadline = time.monotonic() + 0.5
    while True:
        try:
            while os.waitpid(-1, os.WNOHANG)[0]:
                pass
        except ChildProcessError:
            return 128 - rc if rc < 0 else rc
        for value in Path(f"/proc/self/task/{os.getpid()}/children").read_text().split():
            try:
                fd = os.pidfd_open(int(value))
            except ProcessLookupError:
                continue
            try:
                with contextlib.suppress(ProcessLookupError):
                    signal.pidfd_send_signal(fd, signal.SIGKILL)
            finally:
                os.close(fd)
        if time.monotonic() >= deadline:
            raise OSError("sandbox supervisor could not reap descendants")
        time.sleep(0.01)


def run_cgroup() -> Path | None:
    """Create a lifecycle cgroup only under this service's delegated v2 cgroup."""
    import uuid

    try:
        line = next(
            s for s in Path("/proc/self/cgroup").read_text().splitlines() if s.startswith("0::/")
        )
        relative = Path(line[4:])
        if ".." in relative.parts:
            return None
        parent = Path("/sys/fs/cgroup") / relative
        if not (parent / "cgroup.controllers").is_file():
            return None
        child = parent / f"nexura_bwrap_{uuid.uuid4().hex}"
        child.mkdir(mode=0o700)
        if not (child / "cgroup.kill").exists():
            child.rmdir()
            return None
        return child
    except (OSError, StopIteration):
        return None


def kill_cgroup(path: Path) -> None:
    """Only callers holding an allocated run directory may use this helper."""
    (path / "cgroup.kill").write_text("1")
    deadline = time.monotonic() + 0.5
    while True:
        try:
            path.rmdir()
            return
        except OSError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.01)


def command(args: list[str], timeout: float = 3.0) -> tuple[int, bytes]:
    """Keep host launches in the reviewed gVisor worker; import only on use."""
    from nexura_sandbox.gvisor_worker import _bounded_cleanup_command

    return _bounded_cleanup_command(args, timeout)


def task_mounts(task: Path) -> list[str]:
    mounts = []
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        path = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), line.split()[4])
        if path == str(task) or path.startswith(str(task) + "/"):
            mounts.append(path)
    return mounts


def remove_gvisor_task(
    task: Path,
    prefix: list[str],
    *,
    runner: Callable[..., tuple[int, bytes]] | None = None,
) -> None:
    run = runner or command
    null_netns = str(task / "state/null-netns")
    if null_netns in task_mounts(task):
        rc, _ = run([*prefix, "/usr/bin/umount", "--", null_netns])
        if rc:
            raise OSError("null-netns unmount failed")
    if task_mounts(task):
        raise OSError("host mounts remain under gVisor task")
    if not prefix:
        payload = task / "payload"
        if payload.exists():
            payload.chmod(0o700)
            for item in payload.rglob("*"):
                if item.is_dir() and not item.is_symlink():
                    item.chmod(0o700)
    rc, _ = run([*prefix, "/usr/bin/rm", "-rf", "--one-file-system", "--", str(task)])
    if rc or task.exists():
        raise OSError("gVisor task removal failed")


def service_identity(runsc: str, rootfs: str, prefix: list[str]) -> str:
    """Stable across service restarts; separate installations/cgroups are different owners."""
    value = [
        os.getuid(),
        str(Path(__file__).resolve()),
        Path("/proc/self/cgroup").read_text(),
        runsc,
        rootfs,
        prefix,
    ]
    return hashlib.sha256(json.dumps(value).encode()).hexdigest()


def process_start(pid: int) -> str:
    # comm may contain spaces and parentheses; starttime is field 22.
    return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]


def mark_gvisor_task(task: Path, identity: str, container: str) -> None:
    (task / "owner.json").write_text(
        json.dumps(
            {
                "identity": identity,
                "container": container,
                "pid": os.getpid(),
                "start": process_start(os.getpid()),
                "boot": Path("/proc/sys/kernel/random/boot_id").read_text(),
            }
        )
    )


def sweep_gvisor_tasks(
    runsc: str, prefix: list[str], identity: str, root: Path | None = None
) -> None:
    """Conservative bounded startup sweep: unknown, live or ambiguous state is untouched.

    Old unmarked directories cannot be attributed safely and need operator cleanup.
    A successful runsc list is required before deciding a container no longer exists.
    """
    root = root or Path(tempfile.gettempdir())
    deadline = time.monotonic() + 5
    with os.scandir(root) as entries:
        for index, entry in enumerate(entries):
            if index >= 65536 or time.monotonic() >= deadline:
                break
            if not entry.name.startswith("nexura_gvisor_") or entry.is_symlink():
                continue
            task = Path(entry.path)
            try:
                stat = task.stat()
                marker = task / "owner.json"
                if (
                    not task.is_dir()
                    or stat.st_uid != os.getuid()
                    or stat.st_mode & 0o077
                    or marker.is_symlink()
                    or marker.stat().st_uid != os.getuid()
                    or marker.stat().st_size > 4096
                ):
                    continue
                owner = json.loads(marker.read_text())
                container = owner["container"]
                if owner["identity"] != identity or not re.fullmatch(
                    r"nex_[a-f0-9]{32}", container
                ):
                    continue
                try:
                    if (
                        owner["boot"] == Path("/proc/sys/kernel/random/boot_id").read_text()
                        and process_start(int(owner["pid"])) == owner["start"]
                    ):
                        continue  # Includes a live worker still preparing its container.
                except (OSError, ValueError):
                    pass
                runtime = [*prefix, runsc, f"--root={task / 'state'}"]
                rc, output = command(
                    [*runtime, "list", "--format=json"],
                    timeout=max(0.01, min(1, deadline - time.monotonic())),
                )
                if rc or len(output) > 1_048_576:
                    continue
                containers = json.loads(output)
                if not isinstance(containers, list) or any(
                    not isinstance(item, dict) or "id" not in item for item in containers
                ):
                    continue
                if any(item["id"] == container for item in containers):
                    continue  # Never delete or unmount a live/stopped-but-existing container.
                command([*runtime, "delete", "--force", container], timeout=1)
                remove_gvisor_task(task, prefix)
            except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
                continue  # Failure is not proof of stale state; leave it for the operator.


class RunGuard:
    """Independent per-run crash watchdog; normal teardown closes and reaps it."""

    def __init__(
        self,
        *,
        info_fd: int | None = None,
        cgroup: Path | None = None,
        task: Path | None = None,
        runtime: list[str] | None = None,
        prefix: list[str] | None = None,
        container: str | None = None,
    ) -> None:
        from nexura_sandbox.gvisor_worker import _start_crash_guard

        owned: list[int] = []
        self.proc: subprocess.Popen[bytes] | None = None
        try:
            parent_fd = os.pidfd_open(os.getpid())
            owned.append(parent_fd)
            self.ready_fd = os.memfd_create("nexura-guard-ready", os.MFD_CLOEXEC)
            owned.append(self.ready_fd)
            self.control, self.write_fd = os.pipe2(os.O_CLOEXEC)
            owned.extend((self.control, self.write_fd))
            data = {
                "parent": parent_fd,
                "control": self.control,
                "ready": self.ready_fd,
                "info": info_fd,
                "cgroup": str(cgroup) if cgroup else None,
                "task": str(task) if task else None,
                "runtime": runtime,
                "prefix": prefix or [],
                "container": container,
            }
            self.proc = _start_crash_guard(
                data,
                (
                    parent_fd,
                    self.control,
                    self.ready_fd,
                    *((info_fd,) if info_fd is not None else ()),
                ),
            )
        except BaseException:
            for fd in owned:
                os.close(fd)
            raise
        else:
            os.close(parent_fd)
            os.close(self.control)

    @property
    def ready(self) -> bool:
        if self.proc is None or self.proc.poll() is not None:
            raise OSError("sandbox crash watchdog exited before teardown")
        return bool(os.pread(self.ready_fd, 1, 0))

    def close(self) -> None:
        proc, self.proc = self.proc, None
        if proc is None:
            return
        with contextlib.suppress(BrokenPipeError):
            os.write(self.write_fd, b"D")
        os.close(self.write_fd)
        try:
            proc.wait(timeout=1)
            if proc.returncode:
                raise OSError(f"sandbox crash watchdog failed (exit {proc.returncode})")
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=0.5)
            raise OSError("sandbox crash watchdog did not stop") from None
        finally:
            os.close(self.ready_fd)


def _watch(data: dict[str, Any], runner: Callable[..., tuple[int, bytes]]) -> None:
    namespace: int | None = None
    selector = selectors.DefaultSelector()
    try:
        selector.register(data["parent"], selectors.EVENT_READ)
        selector.register(data["control"], selectors.EVENT_READ)
        while True:
            events = {key.fd for key, _ in selector.select(0.01)}
            if data["parent"] in events:
                break
            if data["control"] in events:
                if os.read(data["control"], 1) == b"D":
                    return  # Explicit normal teardown, not crash-induced pipe EOF.
                break
            if namespace is None and data["info"] is not None:
                try:
                    info = json.loads(os.pread(data["info"], 8192, 0))
                    pid = info["child-pid"]
                    if not isinstance(pid, int) or pid <= 1:
                        raise ValueError("invalid namespace PID")
                    namespace = os.pidfd_open(pid)
                    os.pwrite(data["ready"], b"1", 0)
                except (ValueError, KeyError, ProcessLookupError):
                    pass
        if namespace is not None:
            with contextlib.suppress(ProcessLookupError):
                signal.pidfd_send_signal(namespace, signal.SIGKILL)
        if data["runtime"]:
            # Delete-force kills the container, including sentry/gofer outside run's session.
            rc, _ = runner([*data["runtime"], "delete", "--force", data["container"]])
            if rc:
                rc, output = runner([*data["runtime"], "list", "--format=json"])
                containers = json.loads(output)
                if (
                    rc
                    or not isinstance(containers, list)
                    or any(
                        not isinstance(item, dict)
                        or item.get("id") == data["container"]
                        or "id" not in item
                        for item in containers
                    )
                ):
                    raise OSError("cannot prove crashed gVisor container was deleted")
            remove_gvisor_task(Path(data["task"]), data["prefix"], runner=runner)
    finally:
        selector.close()
        if namespace is not None:
            os.close(namespace)
        if data["cgroup"]:
            kill_cgroup(Path(data["cgroup"]))
