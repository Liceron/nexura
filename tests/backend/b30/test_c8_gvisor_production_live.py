"""P38 C8: the ``gvisor_production`` profile on a real host, with host cgroups enforced.

Skipped unless the live gVisor test profile is present (``NEXURA_REAL_GVISOR=1`` and the
runsc/rootfs record read by this directory's conftest), exactly like the other b30 live tests.
Unlike them, nothing here sets ``allow_without_cgroups``: every run applies OCI memory and
pids limits through a real cgroup v2 parent.

The cgroup parent is ``NEXURA_C8_GVISOR_CGROUP_PARENT`` when set (for example ``/nexura``,
provisioned by the operator). Otherwise the module creates a temporary child of the nearest
ancestor of this process's cgroup that is owned by this user and already delegates memory
and pids (a systemd user manager's ``user@UID.service``), enables both controllers in it and
removes it afterwards. With ``NEXURA_REAL_GVISOR=1`` and neither available, the tests FAIL
rather than skip, so a live run never reports green without C8 coverage. runsc still runs
as root through the review host's ``sudo -n`` command prefix, which is test-only. After
each run the tests assert that no task directory, runsc state, null-netns mount, runsc
process or cgroup leaf is left behind.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import shutil
import socket
import stat
import tempfile
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from nexura_sandbox import GVisorOCIWorker
from nexura_sandbox.gvisor_worker import CGROUP_ROOT
from nexura_sandbox.preflight import _CgroupWatcher, gvisor_leftovers, gvisor_preflight_async

from nexura_runtime.integrations.sandbox.errors import ExecutionTimeoutError
from nexura_runtime.integrations.sandbox.models import ExecutionRequest, SandboxConfig

MiB = 1024 * 1024
ROOT = Path(__file__).resolve().parents[3]
INFRA_FILE = ROOT / ".agy" / "review-infrastructure.json"


def load_review_infrastructure() -> dict[str, Any]:
    """The review host's runsc/rootfs record (project root, never the current directory)."""
    if not INFRA_FILE.is_file():
        return {}
    data = json.loads(INFRA_FILE.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def live_gvisor_available() -> bool:
    """Same switch as this directory's conftest: NEXURA_REAL_GVISOR=1 plus runsc and rootfs."""
    if os.environ.get("NEXURA_REAL_GVISOR") != "1":
        return False
    data = load_review_infrastructure()
    runsc, rootfs = data.get("runsc_path"), data.get("rootfs_path")
    return bool(runsc and rootfs and Path(runsc).is_file() and Path(rootfs).is_dir())


pytestmark = pytest.mark.skipif(
    not live_gvisor_available(),
    reason="no live gVisor test profile here (runsc + rootfs and NEXURA_REAL_GVISOR=1)",
)


def _delegated_ancestor() -> Path | None:
    """Nearest ancestor cgroup owned by this user that already delegates memory and pids."""
    try:
        line = next(
            s for s in Path("/proc/self/cgroup").read_text().splitlines() if s.startswith("0::/")
        )
    except (OSError, StopIteration):
        return None
    current = CGROUP_ROOT / line[4:].lstrip("/")
    for candidate in current.parents:
        if candidate == CGROUP_ROOT or CGROUP_ROOT not in candidate.parents:
            return None
        with contextlib.suppress(OSError):
            delegated = (candidate / "cgroup.subtree_control").read_text().split()
            if (
                candidate.stat().st_uid == os.geteuid()
                and os.access(candidate, os.W_OK)
                and {"memory", "pids"} <= set(delegated)
            ):
                return candidate
    return None


@pytest.fixture(scope="module")
def cgroup_parent() -> Iterator[str]:
    """The cgroup parent (relative to the cgroup mount) for this module's runs."""
    configured = os.environ.get("NEXURA_C8_GVISOR_CGROUP_PARENT")
    if configured:
        yield configured
        return
    ancestor = _delegated_ancestor()
    if ancestor is None:
        # Live mode was requested: a silent skip would report green with no C8 coverage.
        pytest.fail(
            "NEXURA_REAL_GVISOR=1 but no cgroup parent for the C8 live tests: set "
            "NEXURA_C8_GVISOR_CGROUP_PARENT to an existing cgroup v2 parent that delegates "
            "memory and pids (for example /nexura), or run from a systemd user session whose "
            "user@UID.service delegates them"
        )
    parent = ancestor / f"nexura-c8-test-{uuid.uuid4().hex[:12]}"
    parent.mkdir(mode=0o755)
    parent.chmod(0o755)
    try:
        (parent / "cgroup.subtree_control").write_text("+memory +pids")
        yield "/" + str(parent.relative_to(CGROUP_ROOT))
    finally:
        deadline = time.monotonic() + 5
        while True:
            try:
                parent.rmdir()
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.1)


def production_config(cgroup_parent: str, **overrides: Any) -> SandboxConfig:
    """``SandboxConfig.gvisor_production`` for this host, plus the test-only sudo prefix."""
    data = load_review_infrastructure()
    sandbox = data.get("sandbox") or {}
    runsc = sandbox.get("runsc_path") or data.get("runsc_path")
    rootfs = sandbox.get("rootfs_path") or data.get("rootfs_path")
    prefix = sandbox.get("runsc_command_prefix") or data.get("runsc_command_prefix")
    fields: dict[str, Any] = {"timeout_seconds": 20.0}
    if prefix:
        fields["extra"] = {"runsc_command_prefix": list(prefix)}
    fields.update(overrides)
    return SandboxConfig.gvisor_production(
        runsc_path=runsc, rootfs_path=rootfs, cgroup_parent=cgroup_parent, **fields
    )


def _parent_dir(cgroup_parent: str) -> Path:
    return CGROUP_ROOT / cgroup_parent.lstrip("/")


def assert_clean(container_id: str, cgroup_parent: str) -> None:
    """No task dir, runsc state, null-netns mount, runsc process or cgroup leaf remains."""
    assert container_id.startswith("nex_")
    leftovers = gvisor_leftovers(container_id, cgroup_parent=_parent_dir(cgroup_parent))
    assert leftovers == {"task_dirs": [], "mounts": [], "processes": [], "cgroups": []}


async def run(
    config: SandboxConfig, code: str, cgroup_parent: str
) -> tuple[Any, dict[str, dict[str, str]]]:
    """Execute ``code``; return the result and the cgroup limits observed while it ran."""
    with _CgroupWatcher(_parent_dir(cgroup_parent)) as watcher:
        result = await GVisorOCIWorker().execute(ExecutionRequest(code=code), config)
    assert_clean(result.details["container_id"], cgroup_parent)
    return result, watcher.seen


@pytest.mark.asyncio
async def test_profile_runs_code_with_enforced_cgroups(cgroup_parent: str) -> None:
    config = production_config(cgroup_parent)
    token = f"c8-{uuid.uuid4().hex}"
    result, seen = await run(
        config, f"import time\nprint({token!r})\ntime.sleep(0.5)\n", cgroup_parent
    )
    assert result.exit_code == 0 and result.stdout.strip() == token
    assert result.isolated and result.isolation == "gvisor"
    details = result.details
    assert details["cgroups"] == "enforced" and details["limits_advisory"] is False
    assert details["hardened_profile"] == "gvisor_production"
    assert details["network_isolated"] is True and details["single_process_seccomp"] is True
    assert seen[details["container_id"]] == {"memory.max": str(256 * MiB), "pids.max": "129"}


@pytest.mark.asyncio
async def test_network_is_denied(cgroup_parent: str) -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.setblocking(False)
    port = listener.getsockname()[1]
    code = (
        "import json, socket\n"
        "out = {'interfaces': [name for _, name in socket.if_nameindex()]}\n"
        "for label, target, kind in (\n"
        f"    ('host_tcp', ('127.0.0.1', {port}), socket.SOCK_STREAM),\n"
        "    ('public_tcp', ('1.1.1.1', 443), socket.SOCK_STREAM),\n"
        "    ('public_udp', ('8.8.8.8', 53), socket.SOCK_DGRAM),\n"
        "):\n"
        "    s = socket.socket(socket.AF_INET, kind)\n"
        "    s.settimeout(2)\n"
        "    try:\n"
        "        if kind == socket.SOCK_DGRAM:\n"
        "            s.sendto(b'x', target)\n"
        "        else:\n"
        "            s.connect(target)\n"
        "        out[label] = 'CONNECTED'\n"
        "    except OSError as exc:\n"
        "        out[label] = exc.errno\n"
        "print(json.dumps(out))\n"
    )
    try:
        result, _ = await run(production_config(cgroup_parent), code, cgroup_parent)
        with pytest.raises(BlockingIOError):
            listener.accept()
    finally:
        listener.close()
    out = json.loads(result.stdout)
    assert out["interfaces"] == ["lo"]
    assert "CONNECTED" not in out.values(), out


@pytest.mark.asyncio
async def test_writes_outside_sandbox_fail(cgroup_parent: str) -> None:
    code = (
        "import json\n"
        "out = {}\n"
        "for path in ('/etc/c8', '/tmp/c8', '/tmp/code.py', '/c8', '/usr/c8', '/usr/bin/c8',\n"
        "             '/dev/c8', '/proc/c8', '/root/c8', '/var/c8', '/sandbox/c8'):\n"
        "    try:\n"
        "        with open(path, 'w') as handle:\n"
        "            handle.write('x')\n"
        "        out[path] = 'WRITTEN'\n"
        "    except OSError as exc:\n"
        "        out[path] = exc.errno\n"
        "print(json.dumps(out))\n"
    )
    result, _ = await run(production_config(cgroup_parent), code, cgroup_parent)
    out = json.loads(result.stdout)
    assert out.pop("/sandbox/c8") == "WRITTEN"
    assert out and all(value != "WRITTEN" for value in out.values()), out


@pytest.mark.asyncio
async def test_fork_and_threads_are_denied_at_one_process(cgroup_parent: str) -> None:
    code = (
        "import os, threading\n"
        "try:\n"
        "    pid = os.fork()\n"
        "    if pid == 0:\n"
        "        os._exit(0)\n"
        "    print('FORK_SUCCEEDED')\n"
        "except OSError as exc:\n"
        "    print('FORK_DENIED', exc.errno)\n"
        "try:\n"
        "    threading.Thread(target=lambda: None).start()\n"
        "    print('THREAD_STARTED')\n"
        "except RuntimeError:\n"
        "    print('THREAD_DENIED')\n"
    )
    result, _ = await run(production_config(cgroup_parent), code, cgroup_parent)
    assert result.stdout.split("\n")[:2] == ["FORK_DENIED 1", "THREAD_DENIED"]  # EPERM


@pytest.mark.asyncio
async def test_memory_cgroup_kills_the_sandbox(cgroup_parent: str) -> None:
    # tmpfs pages live in the sentry, outside the guest's RLIMIT_AS: only the host memory
    # cgroup can stop 120 MiB of scratch at a 64 MiB limit (128 MiB is the profile's
    # tmpfs ceiling).
    config = production_config(
        cgroup_parent, max_memory_mb=64, gvisor_tmpfs_bytes=128 * MiB, timeout_seconds=60.0
    )
    code = (
        "import time\n"
        "time.sleep(0.5)\n"
        "chunk = b'x' * (1024 * 1024)\n"
        "for index in range(3):\n"
        "    with open(f'/sandbox/f{index}', 'wb') as handle:\n"
        "        for _ in range(40):\n"
        "            handle.write(chunk)\n"
        "    print('WROTE_MB', (index + 1) * 40, flush=True)\n"
        "print('WROTE_ALL')\n"
    )
    result, seen = await run(config, code, cgroup_parent)
    assert "WROTE_ALL" not in result.stdout
    assert result.exit_code == 137  # SIGKILL from the cgroup OOM killer
    assert seen[result.details["container_id"]]["memory.max"] == str(64 * MiB)


@pytest.mark.asyncio
async def test_guest_memory_limit_denies_large_allocation(cgroup_parent: str) -> None:
    config = production_config(cgroup_parent, max_memory_mb=64)
    code = (
        "try:\n"
        "    block = bytearray(256 * 1024 * 1024)\n"
        "    print('ALLOCATED')\n"
        "except MemoryError:\n"
        "    print('MEMORY_DENIED')\n"
    )
    result, _ = await run(config, code, cgroup_parent)
    assert result.stdout.strip() == "MEMORY_DENIED"


@pytest.mark.asyncio
async def test_process_cap_and_pids_cgroup(cgroup_parent: str) -> None:
    config = production_config(cgroup_parent, max_processes=4)
    code = (
        "import os, time\n"
        "children = 0\n"
        "for _ in range(8):\n"
        "    try:\n"
        "        pid = os.fork()\n"
        "    except OSError:\n"
        "        continue\n"
        "    if pid == 0:\n"
        "        time.sleep(1)\n"
        "        os._exit(0)\n"
        "    children += 1\n"
        "print('CHILDREN', children)\n"
        "for _ in range(children):\n"
        "    os.wait()\n"
    )
    result, seen = await run(config, code, cgroup_parent)
    assert result.stdout.strip() == "CHILDREN 3"  # guest NPROC 4 includes the parent
    assert seen[result.details["container_id"]]["pids.max"] == str(4 + 128)
    assert result.details["single_process_seccomp"] is False


def _task_dir_of(container_id: str) -> Path | None:
    # The worker allocates task directories in tempfile.gettempdir() (tests redirect it).
    for task in Path(tempfile.gettempdir()).glob("nexura_gvisor_*"):
        with contextlib.suppress(OSError, ValueError, KeyError):
            if json.loads((task / "owner.json").read_text())["container"] == container_id:
                return task
    return None


async def _wait_for_container(cgroup_parent: str, before: set[str]) -> tuple[str, Path | None]:
    parent = _parent_dir(cgroup_parent)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        new = {p.name for p in parent.glob("nex_*")} - before
        if new:
            container = new.pop()
            task = _task_dir_of(container)
            if task is not None and (task / "state").exists():
                return container, task
        await asyncio.sleep(0.05)
    raise AssertionError("the guest container never appeared")


async def _finish(task: asyncio.Task[Any]) -> None:
    """Never leave a running execution behind a failed assertion: cancel and await it."""
    if not task.done():
        task.cancel()
    with contextlib.suppress(BaseException):
        await task


def _null_netns_mounts(task: Path) -> list[str]:
    return [
        line
        for line in Path("/proc/self/mountinfo").read_text().splitlines()
        if str(task) in line and "null-netns" in line
    ]


@pytest.mark.asyncio
async def test_timeout_cleans_up_fully(cgroup_parent: str) -> None:
    config = production_config(cgroup_parent, timeout_seconds=3.0)
    before = {p.name for p in _parent_dir(cgroup_parent).glob("nex_*")}
    task = asyncio.create_task(
        GVisorOCIWorker().execute(ExecutionRequest(code="while True:\n    pass\n"), config)
    )
    try:
        container, task_dir = await _wait_for_container(cgroup_parent, before)
        assert task_dir is not None
        with pytest.raises(ExecutionTimeoutError):
            await task
    finally:
        await _finish(task)
    assert not task_dir.exists() and _null_netns_mounts(task_dir) == []
    assert_clean(container, cgroup_parent)


@pytest.mark.asyncio
async def test_cancellation_cleans_up_fully(cgroup_parent: str) -> None:
    config = production_config(cgroup_parent, timeout_seconds=60.0)
    before = {p.name for p in _parent_dir(cgroup_parent).glob("nex_*")}
    task = asyncio.create_task(
        GVisorOCIWorker().execute(
            ExecutionRequest(code="import time\nwhile True:\n    time.sleep(0.1)\n"), config
        )
    )
    try:
        container, task_dir = await _wait_for_container(cgroup_parent, before)
        assert task_dir is not None
        # The cgroup appears slightly before runsc mounts the null netns; wait for it.
        deadline = time.monotonic() + 5
        while not (seen_mounts := _null_netns_mounts(task_dir)) and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        await _finish(task)
    assert seen_mounts, "the runsc null-netns mount should exist while the guest runs"
    assert not task_dir.exists() and _null_netns_mounts(task_dir) == []
    assert_clean(container, cgroup_parent)


def _replaceable_ancestors(path: Path) -> list[Path]:
    """Directories above ``path`` that another user could use to replace it.

    Mirrors the preflight's rule: group- or world-writable, except a root-owned sticky
    directory (such as /tmp), which only warns.
    """
    found = []
    for directory in [*Path(path).parents, *Path(path).resolve().parents]:
        st = directory.stat()
        writable = st.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        root_sticky = st.st_mode & stat.S_ISVTX and st.st_uid == 0
        if writable and not root_sticky and directory not in found:
            found.append(directory)
    return found


def _pin(config: SandboxConfig) -> str:
    return hashlib.sha512(Path(config.runsc_path or "").read_bytes()).hexdigest()


@pytest.mark.asyncio
async def test_preflight_on_this_hosts_install(cgroup_parent: str) -> None:
    """The honest verdict for the review install under .agy/gvisor.

    On the review host the project directory is group-writable (group ubuntu has other
    members), so another local user could replace the root-run runsc, its gvisor-bin/
    sidecars or the rootfs. The preflight must FAIL on exactly that, without running runsc;
    every other check passes (or warns for the test-only sudo prefix). On a host whose
    install is in a safe directory chain, the whole preflight must pass instead.
    """
    infra = load_review_infrastructure()
    config = production_config(cgroup_parent, timeout_seconds=10.0)
    config = config.model_copy(update={"gvisor_runsc_sha512": _pin(config)})
    report = await gvisor_preflight_async(config, expected_version=infra.get("runsc_release"))
    text = report.format_text()
    unsafe_runsc = _replaceable_ancestors(Path(config.runsc_path or ""))
    unsafe_rootfs = _replaceable_ancestors(Path(config.rootfs_path or ""))
    if not unsafe_runsc and not unsafe_rootfs:
        assert report.ok, text
        for name in ("guest_run", "guest_cgroup_limits", "cleanup"):
            assert report.check(name).status == "pass", text
        return
    assert not report.ok, text
    expected_fail: set[str] = set()
    for name, unsafe in (
        ("runsc_permissions", unsafe_runsc),
        ("rootfs_permissions", unsafe_rootfs),
    ):
        if not unsafe:
            continue
        expected_fail.add(name)
        check = report.check(name)
        assert check.status == "fail", text
        for directory in unsafe:
            assert f"{directory} is group-writable" in check.detail or (
                f"{directory} is world-writable" in check.detail
            ), text
        assert "only root can write" in check.fix, text
    statuses = {check.name: check.status for check in report.checks}
    assert {name for name, status in statuses.items() if status == "fail"} == expected_fail, text
    # runsc is never executed after a failed check, so these are skipped, not passed.
    assert statuses.pop("runsc_version") == "skip" and statuses.pop("guest_run") == "skip", text
    assert statuses.pop("runsc_privilege") == "warn", text  # the test-only sudo -n prefix
    for name in expected_fail:
        statuses.pop(name)
    assert set(statuses.values()) == {"pass"}, text
    assert {
        "platform",
        "profile",
        "runsc_path",
        "runsc_sidecars",
        "runsc_sha512",
        "rootfs_path",
        "rootfs_python",
        "cgroup_v2",
        "cgroup_parent",
    } <= set(statuses), text


def _link_tree(source: Path, target: Path) -> None:
    """Hard-link ``source`` into a new ``target`` tree with fresh, non-writable directories.

    Hard links share the original inodes (and modes), so no file mode is ever changed here;
    only the new directories differ. Falls back to copying across filesystems.
    """
    target.mkdir(mode=0o755)
    for directory, dirnames, filenames in os.walk(source, followlinks=False):
        relative = Path(directory).relative_to(source)
        for name in dirnames:
            src_dir = Path(directory) / name
            dst_dir = target / relative / name
            if src_dir.is_symlink():
                dst_dir.symlink_to(os.readlink(src_dir))
            else:
                dst_dir.mkdir()
                dst_dir.chmod(stat.S_IMODE(src_dir.lstat().st_mode) & 0o755)
        for name in filenames:
            src_file = Path(directory) / name
            dst_file = target / relative / name
            if src_file.is_symlink():
                dst_file.symlink_to(os.readlink(src_file))
                continue
            try:
                os.link(src_file, dst_file)
            except OSError:
                shutil.copy2(src_file, dst_file)
    # Directory modes were set top-down; os.walk skipped symlinked dirs (recreated above).


@pytest.mark.asyncio
async def test_preflight_passes_for_a_safe_copy_of_the_install(cgroup_parent: str) -> None:
    """A hard-linked copy of runsc, gvisor-bin/ and the rootfs in a directory chain that no
    other user can write (by default under ~/.cache, mode 0700) passes the whole preflight,
    including the guest run, its observed cgroup limits and cleanup. No sudo beyond the
    backend's own test prefix is used, and no original file is modified."""
    base = Path(os.environ.get("NEXURA_C8_SAFE_BASE") or Path.home() / ".cache")
    if _replaceable_ancestors(base / "x"):
        pytest.skip(
            f"{base} is not in a safe directory chain: {_replaceable_ancestors(base / 'x')}"
        )
    original = production_config(cgroup_parent)
    runsc = Path(original.runsc_path or "").resolve()
    rootfs = Path(original.rootfs_path or "").resolve()
    safe = Path(tempfile.mkdtemp(prefix="nexura-c8-safe-", dir=base))
    try:
        safe.chmod(0o700)
        (safe / "bin").mkdir(mode=0o755)
        os.link(runsc, safe / "bin" / "runsc")
        _link_tree(runsc.parent / "gvisor-bin", safe / "bin" / "gvisor-bin")
        _link_tree(rootfs, safe / "rootfs")
        config = original.model_copy(
            update={
                "runsc_path": str(safe / "bin" / "runsc"),
                "rootfs_path": str(safe / "rootfs"),
                "timeout_seconds": 10.0,
            }
        )
        config = config.model_copy(update={"gvisor_runsc_sha512": _pin(config)})
        report = await gvisor_preflight_async(
            config, expected_version=load_review_infrastructure().get("runsc_release")
        )
        text = report.format_text()
        assert report.ok, text
        for name in (
            "runsc_sidecars",
            "runsc_sha512",
            "runsc_version",
            "runsc_features",
            "rootfs_permissions",
            "rootfs_python",
            "cgroup_parent",
            "guest_run",
            "guest_cgroup_limits",
            "cleanup",
        ):
            assert report.check(name).status == "pass", text
        assert report.check("runsc_privilege").status == "warn", text  # test-only prefix
        # Owned by the service user, not root: allowed, but a warning (strict fails it).
        permissions = report.check("runsc_permissions")
        assert permissions.status == "warn", text
        assert permissions.detail == (
            f"runsc is owned by uid {os.geteuid()}, not root, but runs as root"
        ), text
        assert not (await gvisor_preflight_async(config, run_guest=False, strict=True)).ok
    finally:
        shutil.rmtree(safe)
    assert runsc.stat().st_mode & 0o7777 == 0o755  # the real binary is untouched


@pytest.mark.asyncio
async def test_preflight_fails_for_wrong_runsc_path(cgroup_parent: str, tmp_path: Path) -> None:
    config = production_config(cgroup_parent).model_copy(
        update={"runsc_path": str(tmp_path / "no-such-runsc")}
    )
    report = await gvisor_preflight_async(config)
    assert not report.ok
    assert report.check("runsc_path").status == "fail"
    assert report.check("guest_run").status == "skip"


@pytest.mark.asyncio
async def test_preflight_fails_for_group_writable_runsc_copy(
    cgroup_parent: str, tmp_path: Path
) -> None:
    real = Path(production_config(cgroup_parent).runsc_path or "")
    real_mode = real.stat().st_mode
    directory = tmp_path / "bin"
    directory.mkdir(mode=0o755)
    directory.chmod(0o755)
    copy = directory / "runsc"
    shutil.copy2(real, copy)
    copy.chmod(0o775)  # Only the temporary copy is changed, never the real binary.
    try:
        config = production_config(cgroup_parent).model_copy(update={"runsc_path": str(copy)})
        report = await gvisor_preflight_async(config)
        assert not report.ok
        check = report.check("runsc_permissions")
        assert check.status == "fail" and "group-writable" in check.detail
        assert report.check("runsc_version").status == "skip"
        assert report.check("guest_run").status == "skip"
    finally:
        copy.unlink()
    assert real.stat().st_mode == real_mode


@pytest.mark.asyncio
async def test_preflight_fails_for_missing_cgroup_parent(cgroup_parent: str) -> None:
    missing = f"{cgroup_parent}/absent-{uuid.uuid4().hex[:8]}"
    report = await gvisor_preflight_async(production_config(missing))
    assert not report.ok
    check = report.check("cgroup_parent")
    assert check.status == "fail" and "does not exist" in check.detail
    assert report.check("guest_run").status == "skip"
    assert not _parent_dir(missing).exists()


def test_parent_has_no_leftover_leaves(cgroup_parent: str) -> None:
    """Runs last in this module (file order): every run removed its cgroup leaf."""
    assert list(_parent_dir(cgroup_parent).glob("nex_*")) == []
