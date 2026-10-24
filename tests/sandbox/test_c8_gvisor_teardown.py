"""P38 C8: cgroup-leaf teardown regression tests (security review round 2, L6 and L8).

runsc creates one cgroup leaf per run, ``<parent>/nex_<32 hex>``. If a run is cancelled or
the service dies, runsc may not remove it. These tests prove, without runsc, root or a real
cgroup, that:

- normal teardown removes an empty leaf;
- a populated leaf is never removed, and the run reports "cgroup leaf remains";
- repeated cancellation does not abandon the bounded wait;
- the test-only cgroup bypass never touches cgroups;
- the crash watchdog and the startup sweep remove an absent run's empty leaf, too.

The process API is the P31 fake runsc (``tests/security/test_p31_gvisor_hardening.py``);
the cgroup tree is a temporary directory set as ``gvisor_worker.CGROUP_ROOT``. Cases 1-4
follow the independent review's repro cases.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from nexura_sandbox import GVisorOCIWorker
from nexura_sandbox import _lifecycle as life
from nexura_sandbox import gvisor_worker as workers

from nexura_runtime.integrations.sandbox import (
    CapabilityUnavailableError,
    ExecutionRequest,
    SandboxConfig,
)

ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "c8_teardown_p31_fakes", ROOT / "tests" / "security" / "test_p31_gvisor_hardening.py"
)
assert _SPEC and _SPEC.loader
p31 = importlib.util.module_from_spec(_SPEC)
sys.modules["c8_teardown_p31_fakes"] = p31
_SPEC.loader.exec_module(p31)

CONTAINER = "nex_" + "e" * 32


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("NEXURA_REAL_GVISOR", "NEXURA_RUNSC_PATH", "NEXURA_OCI_ROOTFS_PATH"):
        monkeypatch.delenv(name, raising=False)


def _make_leaf(leaf: Path, populated: str) -> Path:
    leaf.mkdir(parents=True)
    (leaf / "cgroup.events").write_text(f"populated {populated}\nfrozen 0\n")
    return leaf


class LeafRunsc(p31.FakeRunsc):  # type: ignore[misc,name-defined]
    """Creates ``<CGROUP_ROOT>/<parent>/<container>`` on ``run`` with a chosen state."""

    def __init__(self, parent: str, populated: str) -> None:
        super().__init__()
        self.parent = parent
        self.populated = populated
        self.leaf: Path | None = None
        self.owner: dict[str, Any] = {}

    async def spawn(self, *args: str, **kwargs: Any) -> Any:
        if "run" in args:
            container = args[-1]
            self.leaf = _make_leaf(
                workers.CGROUP_ROOT / self.parent.lstrip("/") / container, self.populated
            )
            bundle = next(a.removeprefix("--bundle=") for a in args if a.startswith("--bundle="))
            self.owner = json.loads((Path(bundle).parent / "owner.json").read_text())
        return await super().spawn(*args, **kwargs)


def _setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, populated: str
) -> tuple[SandboxConfig, LeafRunsc, list[Path]]:
    rootfs = tmp_path / "rootfs"
    rootfs.mkdir()
    cgroups = tmp_path / "cg"
    cgroups.mkdir()
    monkeypatch.setattr(workers, "CGROUP_ROOT", cgroups)
    config = SandboxConfig(
        runsc_path=sys.executable, rootfs_path=str(rootfs), gvisor_cgroup_parent="/nexura"
    )
    fake = LeafRunsc(config.gvisor_cgroup_parent, populated)
    fake.mode = "success"
    monkeypatch.setattr(workers.asyncio, "create_subprocess_exec", fake.spawn)
    monkeypatch.setattr(workers.os, "killpg", lambda *args: None)
    removed: list[Path] = []
    real_rmdir = Path.rmdir

    def cgroupfs_rmdir(self: Path) -> None:
        # cgroupfs removes a directory that still lists its interface files.
        if self.name.startswith("nex_"):
            removed.append(self)
            shutil.rmtree(self)
        else:
            real_rmdir(self)

    monkeypatch.setattr(Path, "rmdir", cgroupfs_rmdir)
    return config, fake, removed


@pytest.mark.asyncio
async def test_empty_leaf_left_by_runsc_is_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, fake, removed = _setup(tmp_path, monkeypatch, "0")
    result = await GVisorOCIWorker().execute(ExecutionRequest(code="x"), config)
    assert result.stdout == "OK\n"
    assert fake.leaf is not None and removed == [fake.leaf] and not fake.leaf.exists()
    # The leaf is recorded for the crash watchdog and a later startup sweep.
    assert fake.owner["cgroup_leaf"] == str(fake.leaf)


@pytest.mark.asyncio
async def test_populated_leaf_is_never_removed_and_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, fake, removed = _setup(tmp_path, monkeypatch, "1")
    start = time.monotonic()
    with pytest.raises(CapabilityUnavailableError, match="cgroup leaf remains"):
        await GVisorOCIWorker().execute(ExecutionRequest(code="x"), config)
    elapsed = time.monotonic() - start
    assert removed == [] and fake.leaf is not None and fake.leaf.exists()
    assert 2.9 <= elapsed < 8.0, elapsed  # the wait is bounded (3 s)


@pytest.mark.asyncio
async def test_repeated_cancellation_does_not_abandon_the_leaf_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, fake, removed = _setup(tmp_path, monkeypatch, "1")
    fake.mode = "hang"
    task = asyncio.create_task(GVisorOCIWorker().execute(ExecutionRequest(code="x"), config))
    await fake.spawn_entered.wait()
    task.cancel()
    for _ in range(10):
        await asyncio.sleep(0.2)
        task.cancel()
    with pytest.raises(asyncio.CancelledError) as exc:
        await task
    notes = getattr(exc.value, "__notes__", [])
    assert any("cgroup leaf remains" in note for note in notes), notes
    assert removed == []


@pytest.mark.asyncio
async def test_test_profile_cgroup_bypass_never_touches_cgroups(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, fake, removed = _setup(tmp_path, monkeypatch, "0")
    monkeypatch.setenv("NEXURA_REAL_GVISOR", "1")
    advisory = config.model_copy(update={"extra": {"allow_without_cgroups": True}})
    await GVisorOCIWorker().execute(ExecutionRequest(code="x"), advisory)
    assert removed == [] and "cgroup_leaf" not in fake.owner


# --- the shared helper, the crash watchdog and the startup sweep -----------------------------


def _prefix_runner(calls: list[list[str]]) -> Any:
    """A stand-in for ``sudo -n``: rmdir is cgroupfs-like, rm runs for real."""

    def run(args: list[str], timeout: float = 3.0) -> tuple[int, bytes]:
        calls.append(args)
        assert args[0] == "fake-sudo"
        command = args[1:]
        if command[0] == "/usr/bin/rmdir":
            shutil.rmtree(command[-1])
            return 0, b""
        if command[0] == "/usr/bin/rm":
            return subprocess.run(command, check=False).returncode, b""
        return 0, b"[]" if "list" in command else b""

    return run


def test_remove_empty_cgroup_leaf_rules(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    run = _prefix_runner(calls)
    empty = _make_leaf(tmp_path / "nexura" / CONTAINER, "0")
    assert life.remove_empty_cgroup_leaf(empty, ["fake-sudo"], runner=run)
    assert not empty.exists() and calls == [["fake-sudo", "/usr/bin/rmdir", "--", str(empty)]]
    busy = _make_leaf(tmp_path / "nexura" / ("nex_" + "f" * 32), "1")
    assert not life.remove_empty_cgroup_leaf(busy, ["fake-sudo"], runner=run, timeout=0.2)
    assert busy.exists() and len(calls) == 1
    assert life.remove_empty_cgroup_leaf(tmp_path / "nexura" / CONTAINER, ["fake-sudo"])
    for bad in (tmp_path / "nexura", tmp_path / "nex_short", Path("relative") / CONTAINER):
        with pytest.raises(ValueError, match="not a gVisor run cgroup leaf"):
            life.remove_empty_cgroup_leaf(bad, ["fake-sudo"], runner=run)


def _stale_task(tmp_path: Path, leaf: Path | None) -> Path:
    task = tmp_path / "tmp" / "nexura_gvisor_stale"
    task.mkdir(parents=True, mode=0o700)
    life.mark_gvisor_task(task, "service-a", CONTAINER, cgroup_leaf=leaf)
    owner = json.loads((task / "owner.json").read_text())
    owner["start"] = "not-this-process"
    (task / "owner.json").write_text(json.dumps(owner))
    return task


@pytest.mark.parametrize("populated", ["0", "1"])
def test_startup_sweep_removes_an_absent_runs_empty_leaf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, populated: str
) -> None:
    cgroups = tmp_path / "cg"
    leaf = _make_leaf(cgroups / "nexura" / CONTAINER, populated)
    task = _stale_task(tmp_path, leaf)
    calls: list[list[str]] = []
    monkeypatch.setattr(life, "command", _prefix_runner(calls))
    life.sweep_gvisor_tasks("/runsc", ["fake-sudo"], "service-a", task.parent, cgroup_root=cgroups)
    assert not task.exists()
    assert leaf.exists() is (populated == "1")  # a populated leaf is never removed


def test_startup_sweep_ignores_a_leaf_outside_the_cgroup_root_or_misnamed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    elsewhere = _make_leaf(tmp_path / "elsewhere" / CONTAINER, "0")
    task = _stale_task(tmp_path, elsewhere)
    calls: list[list[str]] = []
    monkeypatch.setattr(life, "command", _prefix_runner(calls))
    life.sweep_gvisor_tasks(
        "/runsc", ["fake-sudo"], "service-a", task.parent, cgroup_root=tmp_path / "cg"
    )
    assert elsewhere.exists() and not any("/usr/bin/rmdir" in c for c in calls)


def test_crash_watchdog_removes_the_runs_empty_leaf(tmp_path: Path) -> None:
    leaf = _make_leaf(tmp_path / "cg" / "nexura" / CONTAINER, "0")
    task = tmp_path / "nexura_gvisor_crashed"
    task.mkdir(mode=0o700)
    calls: list[list[str]] = []
    exited = subprocess.Popen([sys.executable, "-c", ""])
    parent = os.pidfd_open(exited.pid)
    exited.wait()  # the "service" is gone: its pidfd is readable
    control_read, control_write = os.pipe()
    ready = os.memfd_create("c8-ready")
    try:
        life._watch(
            {
                "parent": parent,
                "control": control_read,
                "ready": ready,
                "info": None,
                "cgroup": None,
                "task": str(task),
                "runtime": ["fake-sudo", "/runsc", f"--root={task / 'state'}"],
                "prefix": ["fake-sudo"],
                "container": CONTAINER,
                "cgroup_leaf": str(leaf),
            },
            _prefix_runner(calls),
        )
    finally:
        for fd in (parent, control_read, control_write, ready):
            os.close(fd)
    assert not leaf.exists() and not task.exists()
    assert ["fake-sudo", "/usr/bin/rmdir", "--", str(leaf)] in calls
