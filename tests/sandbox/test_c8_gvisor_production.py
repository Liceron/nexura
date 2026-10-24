"""P38 C8: the ``gvisor_production`` profile and the operator preflight, with fakes only.

No runsc, rootfs or cgroup of this host is used: runsc is a small fake script, the rootfs a
temporary tree and ``gvisor_worker.CGROUP_ROOT`` a temporary directory. The fake runsc
records every invocation, so tests can prove the preflight never executes a binary that
failed its ownership or mode checks. Live host checks are in
``tests/backend/b30/test_c8_gvisor_production_live.py``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shlex
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from nexura_sandbox import GVisorOCIWorker, gvisor_worker
from nexura_sandbox import preflight as pf

from nexura_runtime.integrations.sandbox import (
    GVISOR_PRODUCTION_PROFILE,
    CapabilityUnavailableError,
    ExecutionRequest,
    ExecutionResult,
    SandboxConfig,
    SandboxError,
    SandboxExecutionModule,
    assert_sandbox_allowed_for_profile,
    gvisor_production_violations,
    sandbox_config_from_env,
)

FEATURES = {
    "ociVersionMin": "1.0.0",
    "linux": {
        "seccomp": {
            "enabled": True,
            "actions": [
                "SCMP_ACT_KILL",
                "SCMP_ACT_TRAP",
                "SCMP_ACT_ERRNO",
                "SCMP_ACT_TRACE",
                "SCMP_ACT_ALLOW",
            ],
        }
    },
}


@pytest.fixture(autouse=True)
def operator_umask() -> Iterator[None]:
    """Operator-style 022 umask, so fake trees are not group-writable on 002-umask hosts."""
    previous = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(previous)


@pytest.fixture(autouse=True)
def operator_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "NEXURA_REAL_GVISOR",
        "NEXURA_RUNSC_PATH",
        "NEXURA_OCI_ROOTFS_PATH",
        "NEXURA_SANDBOX_PROFILE",
        "NEXURA_SANDBOX_BACKENDS",
    ):
        monkeypatch.delenv(name, raising=False)


def _fake_runsc(directory: Path, *, oci_seccomp: bool = True) -> Path:
    """An executable stand-in for runsc that logs each call next to itself.

    A ``/bin/sh`` script, not Python: it starts in a millisecond, so this file adds little
    CPU load to parallel runs of timing-sensitive sandbox tests.
    """
    directory.mkdir(parents=True, exist_ok=True)
    data = directory / "data"
    data.mkdir(exist_ok=True)
    (data / "features.json").write_text(json.dumps(FEATURES) + "\n")
    flags = "  -network value\n" + (
        "  -oci-seccomp\n    \tEnables OCI seccomp.\n" if oci_seccomp else ""
    )
    (data / "flags.txt").write_text(flags)
    script = directory / "runsc"
    script.write_text(
        "#!/bin/sh\n"
        f'echo "$*" >> {shlex.quote(str(directory / "calls.log"))}\n'
        'case "$1" in\n'
        "  --version) echo 'runsc version release-20260928.0'; echo 'spec: 1.2.1' ;;\n"
        f"  features) cat {shlex.quote(str(data / 'features.json'))} ;;\n"
        f"  flags) cat {shlex.quote(str(data / 'flags.txt'))} >&2 ;;\n"
        "  *) exit 3 ;;\n"
        "esac\n"
    )
    script.chmod(0o755)
    directory.chmod(0o755)
    return script


def _calls(runsc: Path) -> list[str]:
    log = runsc.parent / "calls.log"
    return log.read_text().splitlines() if log.exists() else []


def _fake_rootfs(root: Path) -> Path:
    rootfs = root / "rootfs"
    bin_dir = rootfs / "usr" / "local" / "bin"
    bin_dir.mkdir(parents=True)
    (rootfs / "usr" / "bin").mkdir()
    (rootfs / "bin").symlink_to("usr/bin")
    python = bin_dir / "python3.12"
    python.write_text("not really python")
    python.chmod(0o755)
    (bin_dir / "python3").symlink_to("python3.12")
    stdlib = rootfs / "usr" / "local" / "lib" / "python3.12"
    stdlib.mkdir(parents=True)
    (stdlib / "os.py").write_text("# stdlib")
    (stdlib / "os.py").chmod(0o644)
    return rootfs


def _fake_cgroups(
    root: Path, *, controllers: str = "memory pids", delegated: str = "memory pids"
) -> Path:
    cgroups = root / "cgroup"
    parent = cgroups / "nexura"
    parent.mkdir(parents=True)
    (cgroups / "cgroup.controllers").write_text("cpu memory pids\n")
    (parent / "cgroup.controllers").write_text(controllers + "\n")
    (parent / "cgroup.subtree_control").write_text(delegated + "\n")
    parent.chmod(0o755)
    return cgroups


class Host:
    """A fake host: runsc script, rootfs tree and cgroup v2 tree under ``tmp_path``."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.root = tmp_path / "host"
        self.root.mkdir()
        self.root.chmod(0o755)
        self.runsc = _fake_runsc(self.root / "bin")
        self.sidecars = self.root / "bin" / "gvisor-bin"
        self.sidecars.mkdir()
        (self.sidecars / "gvisor_sentry").write_text("#!/bin/sh\nexit 9\n")
        (self.sidecars / "gvisor_sentry").chmod(0o755)
        self.rootfs = _fake_rootfs(self.root)
        self.cgroups = _fake_cgroups(self.root)
        monkeypatch.setattr(gvisor_worker, "CGROUP_ROOT", self.cgroups)

    def config(self, **overrides: Any) -> SandboxConfig:
        return SandboxConfig.gvisor_production(
            runsc_path=str(self.runsc), rootfs_path=str(self.rootfs), **overrides
        )


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Host:
    return Host(tmp_path, monkeypatch)


class FakeWorker:
    """Replaces ``GVisorOCIWorker`` in the preflight; creates a cgroup leaf while it 'runs'."""

    calls = 0
    leave_leaf = False
    details: dict[str, Any] = {}
    stdout_override: str | None = None

    def __init__(self, backend_name: str = "gvisor_oci") -> None:
        self.backend_name = backend_name

    async def execute(self, request: ExecutionRequest, config: SandboxConfig) -> ExecutionResult:
        type(self).calls += 1
        token = re.search(r"print\('([^']+)'", request.code)
        assert token is not None
        container = "nex_" + "a" * 32
        leaf = gvisor_worker.CGROUP_ROOT / config.gvisor_cgroup_parent.lstrip("/") / container
        leaf.mkdir()
        (leaf / "memory.max").write_text(f"{config.max_memory_mb * 1024 * 1024}\n")
        (leaf / "pids.max").write_text(f"{config.max_processes + config.gvisor_runtime_pids}\n")
        await asyncio.sleep(0.3)
        if not type(self).leave_leaf:
            for item in leaf.iterdir():
                item.unlink()
            leaf.rmdir()
        stdout = token.group(1) + "\n" if self.stdout_override is None else self.stdout_override
        return ExecutionResult(
            exit_code=0,
            stdout=stdout,
            stderr="",
            worker_id="fake",
            isolated=True,
            backend="gvisor_oci",
            details={
                "container_id": container,
                "cgroups": "enforced",
                "limits_advisory": False,
                **type(self).details,
            },
        )


@pytest.fixture
def fake_worker(monkeypatch: pytest.MonkeyPatch) -> type[FakeWorker]:
    class Worker(FakeWorker):
        calls = 0
        leave_leaf = False
        details: dict[str, Any] = {}
        stdout_override: str | None = None

    monkeypatch.setattr(pf, "GVisorOCIWorker", Worker)
    return Worker


# --- the profile --------------------------------------------------------------------------


def test_profile_defaults_are_the_strictest_existing_defaults() -> None:
    base = SandboxConfig()
    config = SandboxConfig.gvisor_production(runsc_path="/opt/runsc", rootfs_path="/opt/rootfs")
    assert config.hardened_profile == GVISOR_PRODUCTION_PROFILE
    assert config.backends == ["gvisor_oci"] and config.backend == "gvisor_oci"
    assert config.min_isolation == "gvisor"
    assert config.allow_network is False and config.readonly_root is True
    assert config.allow_unisolated is False
    assert config.max_processes == 1
    assert config.gvisor_cgroup_parent == "/nexura"
    assert "allow_without_cgroups" not in config.extra
    # No limit is looser than the general defaults.
    for name in (
        "timeout_seconds",
        "max_memory_mb",
        "max_output_bytes",
        "max_processes",
        "max_input_bytes",
        "max_input_files",
        "gvisor_tmpfs_bytes",
        "gvisor_runtime_pids",
        "max_concurrent_executions",
    ):
        assert getattr(config, name) <= getattr(base, name), name
    assert gvisor_production_violations(config) == []


def test_general_defaults_are_unchanged() -> None:
    config = SandboxConfig()
    assert config.hardened_profile is None and config.gvisor_runsc_sha512 is None
    assert (config.timeout_seconds, config.max_memory_mb, config.max_processes) == (10.0, 256, 1)
    assert config.allow_network is False and config.gvisor_cgroup_parent == "/nexura"
    assert config.backends is None and config.min_isolation == "interpreter"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"allow_network": True}, "allow_network"),
        ({"extra": {"allow_without_cgroups": True}}, "allow_without_cgroups"),
        ({"min_isolation": "interpreter"}, "min_isolation"),
        ({"min_isolation": "microvm"}, "min_isolation"),
        ({"backends": ["monty", "gvisor_oci"]}, "backends"),
        ({"backends": ["bubblewrap"]}, "backends"),
        ({"allow_unisolated": True}, "allow_unisolated"),
        ({"readonly_root": False}, "readonly_root"),
        ({"cgroup_parent": "nexura"}, "gvisor_cgroup_parent"),
        ({"cgroup_parent": "/"}, "gvisor_cgroup_parent"),
        ({"cgroup_parent": "/a/../b"}, "gvisor_cgroup_parent"),
        ({"runsc_path": "runsc"}, "runsc_path"),
        ({"runsc_path": ""}, "runsc_path"),
        ({"rootfs_path": "/opt/../etc"}, "rootfs_path"),
        ({"runsc_sha512": "abc"}, "gvisor_runsc_sha512"),
        ({"runsc_sha512": "g" * 128}, "gvisor_runsc_sha512"),
        # Round 2 (L4): only normalised absolute paths.
        ({"cgroup_parent": "//nexura"}, "gvisor_cgroup_parent"),
        ({"cgroup_parent": "/nexura/"}, "gvisor_cgroup_parent"),
        ({"cgroup_parent": "/a/./b"}, "gvisor_cgroup_parent"),
        ({"cgroup_parent": "/a//b"}, "gvisor_cgroup_parent"),
        ({"runsc_path": "//opt/runsc"}, "runsc_path"),
        ({"rootfs_path": "/opt/rootfs/"}, "rootfs_path"),
        # Round 2 (L1): only no prefix or the test-only sudo -n.
        ({"extra": {"runsc_command_prefix": ["/bin/sh", "-c", "x", "--"]}}, "command_prefix"),
        ({"extra": {"runsc_command_prefix": ["sudo"]}}, "command_prefix"),
        ({"extra": {"runsc_command_prefix": ["sudo", "-n", "-u", "x"]}}, "command_prefix"),
        ({"extra": {"runsc_command_prefix": "sudo -n"}}, "command_prefix"),
        # Round 2 (L7): enforced ceilings.
        ({"timeout_seconds": 60.5}, "timeout_seconds"),
        ({"max_memory_mb": 1025}, "max_memory_mb"),
        ({"max_processes": 17}, "max_processes"),
        ({"gvisor_runtime_pids": 257}, "gvisor_runtime_pids"),
        ({"gvisor_tmpfs_bytes": 134_217_729}, "gvisor_tmpfs_bytes"),
        ({"max_output_bytes": 4_194_305}, "max_output_bytes"),
    ],
)
def test_profile_refuses_anything_weaker(
    overrides: dict[str, Any], message: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NEXURA_REAL_GVISOR", "1")  # The test switch never relaxes the profile.
    fields: dict[str, Any] = {"runsc_path": "/opt/runsc", "rootfs_path": "/opt/rootfs"}
    fields.update(overrides)
    with pytest.raises(ValueError, match=message):
        SandboxConfig.gvisor_production(**fields)


def test_pin_is_normalised_to_lowercase() -> None:
    config = SandboxConfig(gvisor_runsc_sha512=" " + "AB" * 64 + "\n")
    assert config.gvisor_runsc_sha512 == "ab" * 64


def test_hardened_profile_field_alone_is_validated() -> None:
    with pytest.raises(ValueError, match="backends must be exactly"):
        SandboxConfig(hardened_profile="gvisor_production", runsc_path="/a", rootfs_path="/b")


def test_model_copy_bypass_is_caught_by_the_backend(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = GVisorOCIWorker()
    assert worker.probe(host.config()).available
    for update in (
        {"allow_network": True},
        {"min_isolation": "interpreter"},
        {"extra": {"allow_without_cgroups": True}},
    ):
        status = worker.probe(host.config().model_copy(update=update))
        assert not status.available and "gvisor_production profile violated" in status.reason
    monkeypatch.setenv("NEXURA_REAL_GVISOR", "1")
    bypass = host.config().model_copy(update={"extra": {"allow_without_cgroups": True}})
    with pytest.raises(CapabilityUnavailableError, match="refuses allow_without_cgroups"):
        gvisor_worker.cgroups_ignored(bypass)


@pytest.mark.parametrize(
    ("controllers", "delegated", "reason"),
    [
        ("memory", "memory", "lacks controllers ['pids']"),
        ("memory pids", "memory", "does not enable ['pids']"),
        ("memory pids", "", "does not enable ['memory', 'pids']"),
    ],
)
def test_probe_requires_delegated_memory_and_pids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, controllers: str, delegated: str, reason: str
) -> None:
    host = Host(tmp_path, monkeypatch)
    (host.cgroups / "nexura" / "cgroup.controllers").write_text(controllers)
    (host.cgroups / "nexura" / "cgroup.subtree_control").write_text(delegated)
    status = GVisorOCIWorker().probe(host.config())
    assert not status.available and reason in status.reason


def test_probe_requires_existing_cgroup_parent_and_v2(host: Host) -> None:
    status = GVisorOCIWorker().probe(host.config(cgroup_parent="/missing"))
    assert not status.available and "does not exist" in status.reason
    (host.cgroups / "cgroup.controllers").unlink()
    status = GVisorOCIWorker().probe(host.config())
    assert not status.available and "no cgroup v2 hierarchy" in status.reason


def test_non_profile_config_keeps_its_old_probe(host: Host) -> None:
    # Without the profile, a missing cgroup parent is still only found at launch (unchanged).
    config = SandboxConfig(
        runsc_path=str(host.runsc), rootfs_path=str(host.rootfs), gvisor_cgroup_parent="/missing"
    )
    assert GVisorOCIWorker().probe(config).available


def test_runsc_sha512_pin(host: Host) -> None:
    digest = hashlib.sha512(host.runsc.read_bytes()).hexdigest()
    assert GVisorOCIWorker().probe(host.config(runsc_sha512=digest)).available
    status = GVisorOCIWorker().probe(host.config(runsc_sha512="0" * 128))
    assert not status.available and "does not match the pinned" in status.reason
    # The pin also applies outside the profile.
    plain = SandboxConfig(
        runsc_path=str(host.runsc), rootfs_path=str(host.rootfs), gvisor_runsc_sha512="0" * 128
    )
    assert not GVisorOCIWorker().probe(plain).available
    # A replaced binary is re-hashed (the cache key includes size and timestamps).
    host.runsc.write_text(host.runsc.read_text() + "# changed\n")
    status = GVisorOCIWorker().probe(host.config(runsc_sha512=digest))
    assert not status.available


@pytest.mark.asyncio
async def test_execute_refuses_without_launching(host: Host) -> None:
    config = host.config(cgroup_parent="/missing")
    with pytest.raises(CapabilityUnavailableError, match="does not exist"):
        await GVisorOCIWorker().execute(ExecutionRequest(code="print(1)"), config)
    assert _calls(host.runsc) == []


def test_module_selects_profile_only_when_the_host_qualifies(host: Host) -> None:
    module = SandboxExecutionModule(config=host.config())
    assert module.selection_error is None and module.selected_backend == "gvisor_oci"
    assert module.selected_isolation == "gvisor"
    missing = SandboxExecutionModule(config=host.config(cgroup_parent="/missing"))
    assert missing.selection_error is not None and "does not exist" in missing.selection_error


class _Registry:
    def __init__(self, module: SandboxExecutionModule) -> None:
        self._handlers = {"sandbox": module}


def test_shared_service_accepts_profile_and_refuses_test_prefix(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = SandboxExecutionModule(config=host.config())
    assert_sandbox_allowed_for_profile("authenticated-shared-service", _Registry(module))
    monkeypatch.setenv("NEXURA_REAL_GVISOR", "1")
    prefixed = SandboxExecutionModule(
        config=host.config(extra={"runsc_command_prefix": ["sudo", "-n"]})
    )
    with pytest.raises(SandboxError, match="test-only command prefix"):
        assert_sandbox_allowed_for_profile("authenticated-shared-service", _Registry(prefixed))


def test_profile_from_operator_environment() -> None:
    env = {
        "NEXURA_SANDBOX_PROFILE": "gvisor_production",
        "NEXURA_RUNSC_PATH": "/opt/gvisor/runsc",
        "NEXURA_OCI_ROOTFS_PATH": "/opt/gvisor/rootfs",
        "NEXURA_SANDBOX_GVISOR_CGROUP_PARENT": "/nexura-sandbox",
        "NEXURA_SANDBOX_RUNSC_SHA512": "AB" * 64,
        "NEXURA_SANDBOX_TIMEOUT_SECONDS": "5",
    }
    config = sandbox_config_from_env(env)
    assert config is not None and config.hardened_profile == "gvisor_production"
    assert config.gvisor_cgroup_parent == "/nexura-sandbox" and config.timeout_seconds == 5.0
    assert config.gvisor_runsc_sha512 == "ab" * 64
    with pytest.raises(ValueError, match="backends must be exactly"):
        sandbox_config_from_env({**env, "NEXURA_SANDBOX_BACKENDS": "monty,gvisor_oci"})
    with pytest.raises(ValueError, match="min_isolation"):
        sandbox_config_from_env({**env, "NEXURA_SANDBOX_MIN_ISOLATION": "interpreter"})
    with pytest.raises(ValueError, match="runsc_path must be set"):
        sandbox_config_from_env({"NEXURA_SANDBOX_PROFILE": "gvisor_production"})
    with pytest.raises(ValueError, match="must be 'gvisor_production'"):
        sandbox_config_from_env({"NEXURA_SANDBOX_PROFILE": "relaxed"})
    assert sandbox_config_from_env({}) is None


# --- the preflight ------------------------------------------------------------------------


def _statuses(report: pf.PreflightReport) -> dict[str, str]:
    return {check.name: check.status for check in report.checks}


@pytest.mark.asyncio
async def test_preflight_passes_on_a_good_fake_host(
    host: Host, fake_worker: type[FakeWorker]
) -> None:
    digest = hashlib.sha512(host.runsc.read_bytes()).hexdigest()
    report = await pf.gvisor_preflight_async(
        host.config(runsc_sha512=digest), expected_version="release-20260928.0"
    )
    statuses = _statuses(report)
    assert report.ok, report.format_text()
    for name in (
        "platform",
        "profile",
        "runsc_path",
        "runsc_sha512",
        "runsc_version",
        "runsc_features",
        "runsc_sidecars",
        "rootfs_path",
        "rootfs_python",
        "cgroup_v2",
        "cgroup_parent",
        "guest_run",
        "guest_cgroup_limits",
        "cleanup",
    ):
        assert statuses[name] == "pass", (name, report.format_text())
    assert fake_worker.calls == 1
    assert report.facts["runsc_version"] == "release-20260928.0"
    assert {"--version", "features", "flags"} <= set(_calls(host.runsc))
    assert json.loads(json.dumps(report.to_dict()))["ok"] is True


@pytest.mark.asyncio
async def test_group_writable_runsc_is_never_executed(
    host: Host, fake_worker: type[FakeWorker]
) -> None:
    host.runsc.chmod(0o775)
    report = await pf.gvisor_preflight_async(host.config())
    statuses = _statuses(report)
    assert not report.ok
    assert statuses["runsc_permissions"] == "fail"
    assert "group-writable" in report.check("runsc_permissions").detail
    assert statuses["runsc_version"] == "skip" and statuses["guest_run"] == "skip"
    assert _calls(host.runsc) == [] and fake_worker.calls == 0


@pytest.mark.asyncio
async def test_writable_sidecar_is_refused_before_runsc_runs(
    host: Host, fake_worker: type[FakeWorker]
) -> None:
    (host.sidecars / "gvisor_sentry").chmod(0o775)
    report = await pf.gvisor_preflight_async(host.config())
    check = report.check("runsc_sidecars")
    assert check.status == "fail" and "gvisor_sentry" in check.detail
    assert report.check("runsc_version").status == "skip"
    assert report.check("guest_run").status == "skip"
    assert _calls(host.runsc) == [] and fake_worker.calls == 0


@pytest.mark.asyncio
async def test_sidecar_symlink_and_missing_directory(
    host: Host, fake_worker: type[FakeWorker]
) -> None:
    (host.sidecars / "planted").symlink_to("/bin/sh")
    report = await pf.gvisor_preflight_async(host.config())
    assert report.check("runsc_sidecars").status == "fail"
    assert "not a regular file" in report.check("runsc_sidecars").detail
    for item in host.sidecars.iterdir():
        item.unlink()
    host.sidecars.rmdir()
    report = await pf.gvisor_preflight_async(host.config())
    assert report.check("runsc_sidecars").status == "warn"  # older releases embed them
    assert report.ok


@pytest.mark.asyncio
async def test_sidecar_hashes_are_reported(host: Host, fake_worker: type[FakeWorker]) -> None:
    report = await pf.gvisor_preflight_async(host.config(), run_guest=False)
    expected = hashlib.sha512((host.sidecars / "gvisor_sentry").read_bytes()).hexdigest()
    assert report.facts["sidecar_sha512"] == {"gvisor_sentry": expected}


@pytest.mark.asyncio
async def test_world_writable_runsc_directory_fails(
    host: Host, fake_worker: type[FakeWorker]
) -> None:
    host.runsc.parent.chmod(0o777)
    report = await pf.gvisor_preflight_async(host.config())
    assert report.check("runsc_permissions").status == "fail"
    assert "world-writable" in report.check("runsc_permissions").detail
    assert fake_worker.calls == 0 and _calls(host.runsc) == []


@pytest.mark.asyncio
async def test_wrong_paths_fail_clearly(host: Host, fake_worker: type[FakeWorker]) -> None:
    config = SandboxConfig.gvisor_production(
        runsc_path=str(host.root / "missing" / "runsc"), rootfs_path=str(host.root / "nope")
    )
    report = await pf.gvisor_preflight_async(config)
    assert report.check("runsc_path").status == "fail"
    assert "does not exist" in report.check("runsc_path").detail
    assert report.check("runsc_path").fix
    assert report.check("rootfs_path").status == "fail"
    assert report.check("guest_run").status == "skip" and fake_worker.calls == 0


@pytest.mark.asyncio
async def test_missing_cgroup_parent_fails_with_fix(
    host: Host, fake_worker: type[FakeWorker]
) -> None:
    report = await pf.gvisor_preflight_async(host.config(cgroup_parent="/absent"))
    check = report.check("cgroup_parent")
    assert check.status == "fail" and "does not exist" in check.detail
    assert "cgroup.subtree_control" in check.fix
    assert report.check("guest_run").status == "skip" and fake_worker.calls == 0


@pytest.mark.asyncio
async def test_unsafe_rootfs_and_unreadable_python_fail(
    host: Host, fake_worker: type[FakeWorker]
) -> None:
    (host.rootfs / "usr" / "local" / "bin" / "python3.12").chmod(0o700)
    planted = host.rootfs / "etc"
    planted.mkdir()
    planted.chmod(0o777)
    report = await pf.gvisor_preflight_async(host.config())
    assert report.check("rootfs_permissions").status == "fail"
    assert "etc" in report.check("rootfs_permissions").detail
    assert report.check("rootfs_python").status == "fail"
    assert "UID 65534" in report.check("rootfs_python").detail
    assert fake_worker.calls == 0


@pytest.mark.asyncio
async def test_missing_python_fails(host: Host, fake_worker: type[FakeWorker]) -> None:
    (host.rootfs / "usr" / "local" / "bin" / "python3").unlink()
    report = await pf.gvisor_preflight_async(host.config())
    assert report.check("rootfs_python").status == "fail"


@pytest.mark.asyncio
async def test_runsc_without_oci_seccomp_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_worker: type[FakeWorker]
) -> None:
    host = Host(tmp_path, monkeypatch)
    _fake_runsc(host.runsc.parent, oci_seccomp=False)
    report = await pf.gvisor_preflight_async(host.config())
    assert report.check("runsc_features").status == "fail"
    assert "-oci-seccomp" in report.check("runsc_features").detail
    assert fake_worker.calls == 0


@pytest.mark.asyncio
async def test_version_and_pin_mismatch_fail(host: Host, fake_worker: type[FakeWorker]) -> None:
    report = await pf.gvisor_preflight_async(
        host.config(runsc_sha512="f" * 128), expected_version="release-20990101.0"
    )
    assert report.check("runsc_sha512").status == "fail"
    assert report.check("runsc_version").status == "skip"  # never executed after a pin failure
    assert _calls(host.runsc) == [] and fake_worker.calls == 0
    report = await pf.gvisor_preflight_async(host.config(), expected_version="release-20990101.0")
    assert report.check("runsc_version").status == "fail"
    assert report.check("runsc_sha512").status == "warn"


@pytest.mark.asyncio
async def test_non_profile_config_fails_profile_check(
    host: Host, fake_worker: type[FakeWorker]
) -> None:
    config = SandboxConfig(runsc_path=str(host.runsc), rootfs_path=str(host.rootfs))
    report = await pf.gvisor_preflight_async(config)
    assert report.check("profile").status == "fail" and not report.ok


@pytest.mark.asyncio
async def test_guest_without_enforced_cgroups_fails(
    host: Host, fake_worker: type[FakeWorker]
) -> None:
    fake_worker.details = {"cgroups": "ignored_test_profile", "limits_advisory": True}
    report = await pf.gvisor_preflight_async(host.config())
    assert report.check("guest_run").status == "fail"
    fake_worker.details = {}
    fake_worker.stdout_override = "something else\n"
    report = await pf.gvisor_preflight_async(host.config())
    assert report.check("guest_run").status == "fail"


@pytest.mark.asyncio
async def test_leftover_cgroup_leaf_fails_cleanup(
    host: Host, fake_worker: type[FakeWorker]
) -> None:
    fake_worker.leave_leaf = True
    report = await pf.gvisor_preflight_async(host.config())
    check = report.check("cleanup")
    assert check.status == "fail" and "nex_" in check.detail
    assert report.check("guest_cgroup_limits").status == "pass"


@pytest.mark.asyncio
async def test_strict_mode_fails_on_warnings(host: Host, fake_worker: type[FakeWorker]) -> None:
    report = await pf.gvisor_preflight_async(host.config(), strict=True)
    assert report.check("runsc_sha512").status == "warn"  # no pin configured
    assert not report.ok and "strict" in report.format_text()
    assert (await pf.gvisor_preflight_async(host.config())).ok


@pytest.mark.asyncio
async def test_launch_error_is_reported(host: Host, monkeypatch: pytest.MonkeyPatch) -> None:
    class Failing:
        def __init__(self, backend_name: str = "gvisor_oci") -> None:
            pass

        async def execute(
            self, request: ExecutionRequest, config: SandboxConfig
        ) -> ExecutionResult:
            raise CapabilityUnavailableError("runsc exited before start", backend="gvisor_oci")

    monkeypatch.setattr(pf, "GVisorOCIWorker", Failing)
    report = await pf.gvisor_preflight_async(host.config())
    assert report.check("guest_run").status == "fail"
    assert "runsc exited before start" in report.check("guest_run").detail
    assert "cleanup" not in {check.name for check in report.checks}


@pytest.mark.asyncio
async def test_sync_entry_point_refuses_inside_a_loop(host: Host) -> None:
    with pytest.raises(RuntimeError, match="event loop"):
        pf.gvisor_preflight(host.config(), run_guest=False)


def test_cli_json_and_exit_codes(
    host: Host, fake_worker: type[FakeWorker], capsys: pytest.CaptureFixture[str]
) -> None:
    args = ["--runsc", str(host.runsc), "--rootfs", str(host.rootfs), "--json"]
    assert pf.main(args) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True and any(c["name"] == "guest_run" for c in data["checks"])
    assert pf.main([*args, "--cgroup-parent", "/absent"]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False
    assert pf.main(["--runsc", str(host.runsc)]) == 2
    assert "invalid configuration" in capsys.readouterr().err
    assert pf.main([*args, "--runsc-sha512", "xyz"]) == 2
    assert pf.main([*args[:-1], "--no-guest-run"]) == 0
    assert "[SKIP] guest_run" in capsys.readouterr().out


def test_cli_test_prefix_needs_the_test_switch(
    host: Host, fake_worker: type[FakeWorker], capsys: pytest.CaptureFixture[str]
) -> None:
    args = ["--runsc", str(host.runsc), "--rootfs", str(host.rootfs), "--json"]
    assert pf.main([*args, "--test-command-prefix", "sudo -n"]) == 1
    data = json.loads(capsys.readouterr().out)
    privilege = next(c for c in data["checks"] if c["name"] == "runsc_privilege")
    assert privilege["status"] == "fail" and "NEXURA_REAL_GVISOR" in privilege["detail"]
    assert fake_worker.calls == 0


def test_cli_from_env(
    host: Host, fake_worker: type[FakeWorker], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NEXURA_SANDBOX_PROFILE", "gvisor_production")
    monkeypatch.setenv("NEXURA_RUNSC_PATH", str(host.runsc))
    monkeypatch.setenv("NEXURA_OCI_ROOTFS_PATH", str(host.rootfs))
    assert pf.main(["--from-env", "--no-guest-run"]) == 0
    monkeypatch.delenv("NEXURA_SANDBOX_PROFILE")
    assert pf.main(["--from-env"]) == 2


def test_resolve_in_rootfs_cannot_escape(tmp_path: Path) -> None:
    rootfs = _fake_rootfs(tmp_path)
    (rootfs / "usr" / "bin" / "escape").symlink_to("../../../../../../etc/passwd")
    resolved = pf._resolve_in_rootfs(rootfs, "/usr/bin/escape")
    assert resolved is None or rootfs in resolved.parents
    (rootfs / "usr" / "bin" / "abs").symlink_to("/usr/local/bin/python3")
    assert pf._resolve_in_rootfs(rootfs, "/bin/abs") == rootfs / "usr/local/bin/python3.12"
    (rootfs / "usr" / "bin" / "loop").symlink_to("loop")
    assert pf._resolve_in_rootfs(rootfs, "/usr/bin/loop") is None


def test_leftovers_report_task_dirs_by_marker(tmp_path: Path, host: Host) -> None:
    container = "nex_" + "b" * 32
    task = tmp_path / "nexura_gvisor_x"
    task.mkdir()
    (task / "owner.json").write_text(json.dumps({"container": container}))
    other = tmp_path / "nexura_gvisor_y"
    other.mkdir()
    (other / "owner.json").write_text(json.dumps({"container": "nex_" + "c" * 32}))
    found = pf.gvisor_leftovers(container, tmp_root=tmp_path)
    assert found["task_dirs"] == [str(task)]
    assert found["mounts"] == [] and found["cgroups"] == []
    assert os.getpid() not in [int(p) for p in found["processes"]]


# --- round 2 (security review fixes) --------------------------------------------------------


def test_profile_ceilings_are_inclusive_and_exported() -> None:
    from nexura_runtime.integrations.sandbox import GVISOR_PRODUCTION_CEILINGS

    config = SandboxConfig.gvisor_production(
        runsc_path="/opt/runsc",
        rootfs_path="/opt/rootfs",
        extra={"runsc_command_prefix": ["sudo", "-n"]},
        **GVISOR_PRODUCTION_CEILINGS,  # type: ignore[arg-type]
    )
    assert config.timeout_seconds == 60.0 and config.max_processes == 16
    assert SandboxConfig.gvisor_production(
        runsc_path="/opt/runsc", rootfs_path="/opt/rootfs", extra={"runsc_command_prefix": []}
    )


def test_profile_prefix_rule_is_rechecked_by_the_backend(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NEXURA_REAL_GVISOR", "1")
    sneaky = host.config().model_copy(
        update={"extra": {"runsc_command_prefix": ["/bin/sh", "-c", "x", "--"]}}
    )
    status = GVisorOCIWorker().probe(sneaky)
    assert not status.available and "runsc_command_prefix" in status.reason
    raised = host.config().model_copy(update={"max_memory_mb": 4096})
    status = GVisorOCIWorker().probe(raised)
    assert not status.available and "ceiling" in status.reason


@pytest.mark.parametrize("parent", ["//nexura", "nexura", "/a/../b", "/"])
def test_bad_cgroup_parent_never_raises_from_probe_or_module(host: Host, parent: str) -> None:
    config = host.config().model_copy(update={"gvisor_cgroup_parent": parent})
    status = GVisorOCIWorker().probe(config)
    assert not status.available and "gvisor_cgroup_parent" in status.reason
    module = SandboxExecutionModule(config=config)
    assert module.selection_error is not None


def test_pin_and_cgroup_parent_env_are_honoured_without_the_profile() -> None:
    env = {
        "NEXURA_SANDBOX_BACKENDS": "gvisor_oci",
        "NEXURA_SANDBOX_MIN_ISOLATION": "gvisor",
        "NEXURA_SANDBOX_RUNSC_SHA512": "AB" * 64,
        "NEXURA_SANDBOX_GVISOR_CGROUP_PARENT": "/elsewhere",
    }
    config = sandbox_config_from_env(env)
    assert config is not None and config.hardened_profile is None
    assert config.gvisor_runsc_sha512 == "ab" * 64
    assert config.gvisor_cgroup_parent == "/elsewhere"
    for bad in ("xyz", "  "):
        with pytest.raises(ValueError, match="gvisor_runsc_sha512"):
            sandbox_config_from_env({**env, "NEXURA_SANDBOX_RUNSC_SHA512": bad})


@pytest.mark.parametrize(
    "name", ["NEXURA_SANDBOX_RUNSC_SHA512", "NEXURA_SANDBOX_GVISOR_CGROUP_PARENT"]
)
def test_pin_or_parent_without_an_enabled_sandbox_is_an_error(name: str) -> None:
    value = "ab" * 64 if name.endswith("SHA512") else "/nexura"
    with pytest.raises(ValueError, match=f"{name} set, but the sandbox is not enabled"):
        sandbox_config_from_env({name: value})


def test_pinned_runsc_mismatch_from_env_fails_closed(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Outside the profile the worker reads the runsc/rootfs paths from the process env.
    monkeypatch.setenv("NEXURA_RUNSC_PATH", str(host.runsc))
    monkeypatch.setenv("NEXURA_OCI_ROOTFS_PATH", str(host.rootfs))
    config = sandbox_config_from_env(
        {
            "NEXURA_SANDBOX_BACKENDS": "gvisor_oci",
            "NEXURA_SANDBOX_MIN_ISOLATION": "gvisor",
            "NEXURA_SANDBOX_RUNSC_SHA512": "0" * 128,
        }
    )
    assert config is not None
    module = SandboxExecutionModule(config=config)
    assert module.selection_error is not None and "pinned" in module.selection_error


class _BrokenRegistry:
    """A versioned registry whose sandbox entry raises when it is built."""

    def has(self, module_id: str) -> bool:
        return True

    def get_entry(self, module_id: str) -> object:
        return object()

    def get(self, module_id: str) -> object:
        raise ValueError("'//nexura' is not in the subpath of '/'")


def test_shared_service_fails_on_a_sandbox_it_cannot_build() -> None:
    with pytest.raises(SandboxError, match="could not be built"):
        assert_sandbox_allowed_for_profile("authenticated-shared-service", _BrokenRegistry())
    assert_sandbox_allowed_for_profile("local-dev", _BrokenRegistry())  # other profiles: no check


def test_shared_service_fails_on_invalid_operator_sandbox_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import nexura_runtime.integrations.sandbox as sandbox_pkg

    monkeypatch.setenv("NEXURA_SANDBOX_PROFILE", "gvisor_production")
    monkeypatch.setenv("NEXURA_RUNSC_PATH", "/opt/gvisor/runsc")
    monkeypatch.setenv("NEXURA_OCI_ROOTFS_PATH", "/opt/gvisor/rootfs")
    monkeypatch.setenv("NEXURA_SANDBOX_GVISOR_CGROUP_PARENT", "//nexura")
    with pytest.warns(RuntimeWarning, match="Ignoring invalid sandbox settings"):
        module = sandbox_pkg._operator_module()
    assert module.settings_error is not None and "gvisor_cgroup_parent" in module.settings_error
    assert module.selection_error is not None and "invalid operator" in module.selection_error
    with pytest.raises(SandboxError, match="invalid operator sandbox settings"):
        assert_sandbox_allowed_for_profile("authenticated-shared-service", _Registry(module))


@pytest.mark.asyncio
async def test_group_writable_ancestor_fails_even_without_other_members(
    host: Host, fake_worker: type[FakeWorker]
) -> None:
    host.root.chmod(0o775)  # ancestor of bin/, gvisor-bin/ and rootfs/
    report = await pf.gvisor_preflight_async(host.config())
    assert not report.ok
    for name in ("runsc_permissions", "rootfs_permissions"):
        check = report.check(name)
        assert check.status == "fail" and f"{host.root} is group-writable" in check.detail
        assert "only root can write" in check.fix
    assert report.check("runsc_version").status == "skip"
    assert report.check("guest_run").status == "skip"
    assert _calls(host.runsc) == [] and fake_worker.calls == 0


@pytest.mark.asyncio
async def test_root_sticky_ancestor_only_warns_and_strict_fails_it(
    host: Host, fake_worker: type[FakeWorker]
) -> None:
    # tmp_path lives below /tmp (root-owned, mode 1777) on Linux test hosts.
    sticky = [d for d in host.root.parents if d.stat().st_mode & 0o1000 and d.stat().st_uid == 0]
    if not sticky:
        pytest.skip("the test tree is not below a root-owned sticky directory here")
    digest = hashlib.sha512(host.runsc.read_bytes()).hexdigest()
    report = await pf.gvisor_preflight_async(host.config(runsc_sha512=digest))
    assert report.ok, report.format_text()
    check = report.check("runsc_permissions")
    assert check.status == "warn" and "sticky" in check.detail
    assert not (await pf.gvisor_preflight_async(host.config(runsc_sha512=digest), strict=True)).ok


@pytest.mark.asyncio
async def test_group_writable_rootfs_entry_fails(host: Host, fake_worker: type[FakeWorker]) -> None:
    (host.rootfs / "var").mkdir()
    (host.rootfs / "var").chmod(0o775)
    report = await pf.gvisor_preflight_async(host.config())
    check = report.check("rootfs_permissions")
    assert check.status == "fail" and "var" in check.detail
    assert fake_worker.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("parent", ["nexura", "//nexura", "/"])
async def test_preflight_reports_a_bad_cgroup_parent_without_a_traceback(
    host: Host, fake_worker: type[FakeWorker], parent: str
) -> None:
    config = host.config().model_copy(update={"gvisor_cgroup_parent": parent})
    report = await pf.gvisor_preflight_async(config, run_guest=False)
    assert not report.ok
    assert report.check("profile").status == "fail"
    assert report.check("cgroup_parent").status == "fail"
    assert "gvisor_cgroup_parent" in report.check("cgroup_parent").detail


@pytest.mark.asyncio
async def test_unexpected_error_becomes_a_failed_check(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("unexpected host state")

    monkeypatch.setattr(pf, "_check_rootfs_python", boom)
    report = await pf.gvisor_preflight_async(host.config())
    check = report.check("preflight_error")
    assert check.status == "fail" and "unexpected host state" in check.detail
    assert not report.ok and "FAILED" in report.format_text()


class _LeaflessWorker(FakeWorker):
    """Reports enforced cgroups (a config label) but never creates a cgroup leaf."""

    async def execute(self, request: ExecutionRequest, config: SandboxConfig) -> ExecutionResult:
        token = re.search(r"print\('([^']+)'", request.code)
        assert token is not None
        await asyncio.sleep(0.2)
        return ExecutionResult(
            exit_code=0,
            stdout=token.group(1) + "\n",
            stderr="",
            worker_id="fake",
            isolated=True,
            backend="gvisor_oci",
            details={
                "container_id": "nex_" + "d" * 32,
                "cgroups": "enforced",
                "limits_advisory": False,
            },
        )


@pytest.mark.asyncio
async def test_unobserved_cgroup_limits_fail(host: Host, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pf, "GVisorOCIWorker", _LeaflessWorker)
    report = await pf.gvisor_preflight_async(host.config())
    assert report.check("guest_run").status == "pass"
    check = report.check("guest_cgroup_limits")
    assert check.status == "fail" and "unproven" in check.detail
    assert not report.ok


@pytest.mark.asyncio
async def test_wrong_observed_limits_fail(
    host: Host, fake_worker: type[FakeWorker], monkeypatch: pytest.MonkeyPatch
) -> None:
    real_execute = fake_worker.execute

    async def lower(self: Any, request: ExecutionRequest, config: SandboxConfig) -> Any:
        return await real_execute(self, request, config.model_copy(update={"max_memory_mb": 64}))

    monkeypatch.setattr(fake_worker, "execute", lower)
    report = await pf.gvisor_preflight_async(host.config())
    assert report.check("guest_cgroup_limits").status == "fail"
