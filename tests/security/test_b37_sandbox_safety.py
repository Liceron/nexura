"""B37: ``nexura.tools.sandbox.execute`` is gated, operator-configured and hidden.

Offline repros for the confirmed problems: no gVisor is needed. The workers used here are
``MockDisposableWorker`` (wrapped to record what the module asked it to run) and a fake
``runsc`` shell script that only records its arguments; nothing here is a live sandbox.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any

import pytest
from nexura_agents.contracts.context import ExecutionContext
from nexura_sandbox import GVisorOCIWorker
from nexura_sandbox.gvisor_worker import discover_oci_infrastructure

from nexura_runtime.integrations.sandbox import (
    CapabilityUnavailableError,
    ExecutionRequest,
    ExecutionResult,
    MockDisposableWorker,
    SandboxConfig,
    SandboxExecutionModule,
)
from nexura_runtime.modules.registry import default_registry
from nexura_runtime.workflow import (
    MemoryWorkflowStore,
    ModuleRegistry,
    WorkflowEngine,
    WorkflowNode,
    WorkflowSpec,
)

MODULE_ID = "nexura.tools.sandbox.execute"
OPERATOR = ExecutionContext(run_id="r", node_id="n", approval_granted=True)
PLAIN = ExecutionContext(run_id="r", node_id="n")


class RecordingWorker:
    """Offline fake: records each request/config, then delegates to MockDisposableWorker."""

    backend_name = "mock_disposable"

    def __init__(self) -> None:
        self.calls: list[tuple[ExecutionRequest, SandboxConfig]] = []
        self._inner = MockDisposableWorker()

    async def execute(
        self,
        request: ExecutionRequest,
        config: SandboxConfig,
        *,
        cancellation_token: Any | None = None,
    ) -> ExecutionResult:
        self.calls.append((request, config))
        return await self._inner.execute(request, config, cancellation_token=cancellation_token)


def _fake_runsc(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A fake runsc that appends its argv to a log and prints a marker (not a sandbox)."""
    log = tmp_path / "runsc-argv.log"
    runsc = tmp_path / "fake-bin" / "runsc"
    runsc.parent.mkdir()
    runsc.write_text(
        f'#!/bin/sh\necho "$@" >> "{log}"\necho FAKE_RUNSC_RAN\nexit 0\n', encoding="utf-8"
    )
    runsc.chmod(runsc.stat().st_mode | stat.S_IXUSR)
    rootfs = tmp_path / "rootfs"
    rootfs.mkdir()
    return runsc, rootfs, log


def _run_lines(log: Path) -> list[str]:
    if not log.is_file():
        return []
    return [line for line in log.read_text().splitlines() if " run " in f" {line} "]


# 1. The module is a write: it pauses in a workflow and refuses an unapproved direct call.


def test_manifest_declares_an_always_gated_write() -> None:
    manifest = SandboxExecutionModule(worker=RecordingWorker()).manifest
    assert manifest.always_write is True
    assert (
        manifest.effect_policy == "idempotent_write"
    )  # B37 review F4: network off; still always_write
    assert manifest.write_gate_inputs({"code": "print(1)"})


async def test_direct_call_without_approval_is_refused_and_nothing_runs() -> None:
    worker = RecordingWorker()
    module = SandboxExecutionModule(worker=worker)
    res = await module.invoke(PLAIN, {"code": "print('RAN')", "approved": True}, {})
    assert res.status == "failure" and res.error_code == "APPROVAL_REQUIRED"
    assert worker.calls == []
    ok = await module.invoke(OPERATOR, {"code": "print('RAN')"}, {})
    assert ok.status == "success", ok.error_message
    assert len(worker.calls) == 1


async def test_workflow_pauses_for_an_approval_bound_to_the_code() -> None:
    worker = RecordingWorker()
    reg = ModuleRegistry()
    reg.register("sandbox", SandboxExecutionModule(worker=worker))
    spec = WorkflowSpec(
        name="b37",
        entry_node="run",
        nodes={"run": WorkflowNode(id="run", handler="sandbox", input_mapping={"code": "src"})},
    )
    eng = WorkflowEngine(spec, reg, store=MemoryWorkflowStore())
    paused = await eng.run({"src": "print('first')"})
    assert paused.status == "paused" and worker.calls == []
    assert paused.pending_approval is not None

    # Changing the code after the pause needs a fresh approval for the new code.
    edited = await eng.resume(
        paused.workflow_run_id,
        paused.pending_approval.approval_id,
        state_update={"src": "print('edited')"},
    )
    assert edited.status == "paused" and worker.calls == []
    assert edited.pending_approval is not None
    assert edited.pending_approval.payload_digest != paused.pending_approval.payload_digest

    done = await eng.resume(edited.workflow_run_id, edited.pending_approval.approval_id)
    assert done.status == "completed"
    assert [req.code for req, _ in worker.calls] == ["print('edited')"]


# 2. Limits come from the operator; inputs may only lower them. No model-supplied env.


async def test_inputs_cannot_raise_operator_limits() -> None:
    worker = RecordingWorker()
    operator = SandboxConfig(timeout_seconds=5.0, max_memory_mb=128, max_output_bytes=4096)
    module = SandboxExecutionModule(worker=worker, config=operator)
    res = await module.invoke(
        OPERATOR,
        {
            "code": "print(1)",
            "timeout_seconds": 300,
            "max_memory_mb": 4096,
            "max_output_bytes": 10_485_760,
        },
        {},
    )
    assert res.status == "success", res.error_message
    _, used = worker.calls[-1]
    assert (used.timeout_seconds, used.max_memory_mb, used.max_output_bytes) == (5.0, 128, 4096)

    await module.invoke(
        OPERATOR,
        {"code": "print(1)", "timeout_seconds": 2, "max_memory_mb": 64, "max_output_bytes": 2048},
        {},
    )
    _, lowered = worker.calls[-1]
    assert (lowered.timeout_seconds, lowered.max_memory_mb, lowered.max_output_bytes) == (
        2.0,
        64,
        2048,
    )


async def test_default_operator_limits_are_the_safe_defaults() -> None:
    worker = RecordingWorker()
    module = SandboxExecutionModule(worker=worker)
    await module.invoke(
        OPERATOR, {"code": "print(1)", "timeout_seconds": 300, "max_memory_mb": 4096}, {}
    )
    _, used = worker.calls[-1]
    assert used.timeout_seconds == 10.0
    assert used.max_memory_mb == 256
    assert used.max_output_bytes == 1_048_576
    assert used.max_processes == 1
    assert used.allow_network is False


async def test_env_input_is_refused_and_never_passed() -> None:
    worker = RecordingWorker()
    module = SandboxExecutionModule(worker=worker)
    assert "env" not in module.manifest.input_schema["properties"]
    for ctx in (PLAIN, OPERATOR):
        res = await module.invoke(ctx, {"code": "print(1)", "env": {"PYTHONPATH": "/x"}}, {})
        assert res.status == "failure" and res.error_code == "SECURITY_VIOLATION_ENV"
    assert worker.calls == []
    ok = await module.invoke(OPERATOR, {"code": "print(1)", "env": {}}, {})
    assert ok.status == "success", ok.error_message
    assert worker.calls[-1][0].env == {}


async def test_network_needs_operator_permission_and_an_explicit_input() -> None:
    worker = RecordingWorker()
    denied = SandboxExecutionModule(worker=worker)
    res = await denied.invoke(OPERATOR, {"code": "print(1)", "allow_network": True}, {})
    assert res.error_code == "SECURITY_VIOLATION_NETWORK_POLICY" and worker.calls == []

    allowed = SandboxExecutionModule(worker=worker, config=SandboxConfig(allow_network=True))
    await allowed.invoke(OPERATOR, {"code": "print(1)"}, {})
    assert worker.calls[-1][1].allow_network is False
    await allowed.invoke(OPERATOR, {"code": "print(1)", "allow_network": True}, {})
    assert worker.calls[-1][1].allow_network is True


# 3. Node config cannot choose the backend or the binary that runs.


async def test_node_config_cannot_pick_the_runsc_binary(tmp_path: Path) -> None:
    runsc, rootfs, log = _fake_runsc(tmp_path)
    module = SandboxExecutionModule(worker=GVisorOCIWorker())
    res = await module.invoke(
        OPERATOR,
        {"code": "print(1)"},
        {"backend": "gvisor_oci", "runsc_path": str(runsc), "rootfs_path": str(rootfs)},
    )
    assert _run_lines(log) == [], "a flow author's runsc_path was executed"
    assert res.status == "failure" and res.error_code == "INVALID_CONFIG"
    assert "runsc_path" in (res.error_message or "")


async def test_each_operator_only_config_key_is_refused() -> None:
    worker = RecordingWorker()
    module = SandboxExecutionModule(worker=worker)
    for key, value in (
        ("backend", "trusted_subprocess"),
        ("runsc_path", "/tmp/evil"),
        ("rootfs_path", "/tmp/rootfs"),
        ("runsc_command_prefix", ["sudo", "-n"]),
    ):
        res = await module.invoke(OPERATOR, {"code": "print(1)"}, {key: value})
        assert res.status == "failure" and res.error_code == "INVALID_CONFIG", key
        assert key in (res.error_message or "")
    assert worker.calls == []


async def test_operator_config_chooses_the_runsc_binary(tmp_path: Path) -> None:
    runsc, rootfs, log = _fake_runsc(tmp_path)
    operator = SandboxConfig(runsc_path=str(runsc), rootfs_path=str(rootfs), timeout_seconds=5)
    module = SandboxExecutionModule(worker=GVisorOCIWorker(), config=operator)
    res = await module.invoke(OPERATOR, {"code": "print(1)"}, {})
    assert res.status == "success", res.error_message
    assert "FAKE_RUNSC_RAN" in res.outputs["stdout"]
    assert len(_run_lines(log)) == 1


# 4. The current directory never chooses runsc, the rootfs or a sudo prefix.


def _plant_infra_file(directory: Path, runsc: Path, rootfs: Path) -> None:
    agy = directory / ".agy"
    agy.mkdir()
    (agy / "review-infrastructure.json").write_text(
        json.dumps(
            {
                "runsc_path": str(runsc),
                "rootfs_path": str(rootfs),
                "runsc_command_prefix": ["sudo", "-n"],
                "sandbox": {"runsc_command_prefix": ["sudo", "-n"]},
            }
        ),
        encoding="utf-8",
    )


def test_discovery_ignores_infra_file_in_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runsc, rootfs, _ = _fake_runsc(tmp_path)
    _plant_infra_file(tmp_path, runsc, rootfs)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("NEXURA_RUNSC_PATH", raising=False)
    monkeypatch.delenv("NEXURA_OCI_ROOTFS_PATH", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path / "empty-path"))
    found_runsc, found_rootfs, prefix = discover_oci_infrastructure(SandboxConfig())
    assert prefix == []
    assert found_runsc is None and found_rootfs is None

    # A prefix comes only from explicit config, and only in the test profile (P31-S5).
    explicit = SandboxConfig(extra={"runsc_command_prefix": ["sudo", "-n"]})
    monkeypatch.delenv("NEXURA_REAL_GVISOR", raising=False)
    with pytest.raises(CapabilityUnavailableError, match="test-only"):
        discover_oci_infrastructure(explicit)
    monkeypatch.setenv("NEXURA_REAL_GVISOR", "1")
    assert discover_oci_infrastructure(explicit)[2] == ["sudo", "-n"]


async def test_worker_does_not_run_a_runsc_named_by_cwd_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runsc, rootfs, log = _fake_runsc(tmp_path)
    _plant_infra_file(tmp_path, runsc, rootfs)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("NEXURA_RUNSC_PATH", raising=False)
    monkeypatch.delenv("NEXURA_OCI_ROOTFS_PATH", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path / "empty-path"))
    with pytest.raises(CapabilityUnavailableError):
        await GVisorOCIWorker().execute(ExecutionRequest(code="print(1)"), SandboxConfig())
    assert _run_lines(log) == []


# 5. runsc applies cgroup limits unless the operator explicitly opts out.


async def test_cgroups_are_not_ignored_by_default(tmp_path: Path) -> None:
    runsc, rootfs, log = _fake_runsc(tmp_path)
    config = SandboxConfig(runsc_path=str(runsc), rootfs_path=str(rootfs), timeout_seconds=5)
    result = await GVisorOCIWorker().execute(ExecutionRequest(code="print(1)"), config)
    (run_line,) = _run_lines(log)
    assert "--ignore-cgroups" not in run_line.split()
    assert result.details["limits_advisory"] is False


async def test_cgroups_opt_out_is_explicit_and_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The cgroup opt-out is honoured only in the test profile (P31-S5); outside it the
    # worker refuses (covered by test_p31_gvisor_hardening.test_test_profile_is_explicit).
    monkeypatch.setenv("NEXURA_REAL_GVISOR", "1")
    runsc, rootfs, log = _fake_runsc(tmp_path)
    config = SandboxConfig(
        runsc_path=str(runsc),
        rootfs_path=str(rootfs),
        timeout_seconds=5,
        extra={"allow_without_cgroups": True},
    )
    result = await GVisorOCIWorker().execute(ExecutionRequest(code="print(1)"), config)
    (run_line,) = _run_lines(log)
    assert "--ignore-cgroups" in run_line.split()
    assert result.details["limits_advisory"] is True

    # A truthy non-boolean is not an opt-in.
    log.unlink()
    loose = config.model_copy(update={"extra": {"allow_without_cgroups": "yes"}})
    await GVisorOCIWorker().execute(ExecutionRequest(code="print(1)"), loose)
    (run_line,) = _run_lines(log)
    assert "--ignore-cgroups" not in run_line.split()

    module = SandboxExecutionModule(worker=GVisorOCIWorker(), config=config)
    res = await module.invoke(OPERATOR, {"code": "print(1)"}, {})
    assert res.status == "success", res.error_message
    assert res.outputs["limits_advisory"] is True


async def test_runsc_that_cannot_start_under_cgroups_fails_closed(tmp_path: Path) -> None:
    """Enforced cgroups can stop runsc from starting; that must not look like a code result."""
    runsc = tmp_path / "runsc"
    runsc.write_text(
        "#!/bin/sh\n"
        "echo 'running container: creating container: cannot create sandbox: cannot create "
        "sandbox process: starting sandbox: fork/exec sentry: resource temporarily "
        "unavailable' >&2\nexit 128\n",
        encoding="utf-8",
    )
    runsc.chmod(runsc.stat().st_mode | stat.S_IXUSR)
    rootfs = tmp_path / "rootfs"
    rootfs.mkdir()
    config = SandboxConfig(runsc_path=str(runsc), rootfs_path=str(rootfs), timeout_seconds=5)
    module = SandboxExecutionModule(worker=GVisorOCIWorker(), config=config)
    res = await module.invoke(OPERATOR, {"code": "print(1)"}, {})
    assert res.status == "failure" and res.error_code == "CAPABILITY_UNAVAILABLE"


# 6. Hidden from the Studio palette, still registered for saved workflows.


def test_module_is_internal_but_still_registered() -> None:
    assert SandboxExecutionModule(worker=RecordingWorker()).manifest.internal is True
    assert default_registry.has(MODULE_ID)
    assert default_registry.get(MODULE_ID).manifest.internal is True


async def test_saved_workflow_using_the_sandbox_still_validates(tmp_path: Path) -> None:
    from httpx import ASGITransport, AsyncClient

    from nexura_runtime.service.api.app import create_api_app
    from nexura_runtime.studio.builtin_modules import get_default_registry
    from nexura_runtime.studio.store import StudioStore

    db_file = tmp_path / "b37.sqlite3"
    app = create_api_app(
        registry=get_default_registry(),
        store=StudioStore(db_file),
        db_path=db_file,
        profile="local-owner",
    )
    node = {
        "id": "run",
        "handler": MODULE_ID,
        "module_id": MODULE_ID,
        "module_version": "1.0.0",
        "config": {},
        "input_mapping": {"code": "code"},
        "output_mapping": {},
        "transitions": {},
        "default_transition": None,
        "requires_approval": False,
    }
    spec = {"name": "SavedSandbox", "entry_node": "run", "spec_version": 1, "nodes": {"run": node}}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post("/api/v1/workflows/validate", json={"spec": spec})
        listed = await client.get(f"/api/v1/modules/{MODULE_ID}")
    assert res.status_code == 200
    assert res.json()["valid"] is True, res.json()["errors"]
    assert listed.status_code == 200
    descriptor = listed.json()
    assert descriptor["internal"] is True
    assert (
        descriptor["effect_policy"] == "idempotent_write"
    )  # B37 review F4: network off; still always_write
    assert descriptor["requires_approval"] is True


def test_retry_on_a_network_off_sandbox_node_still_validates() -> None:
    # B37 review F4: a disposable worker with network off has no outside effects, so it is an
    # idempotent write (still always approval-gated) and saved workflows with a retry validate.
    from nexura_runtime.integrations.sandbox.models import SandboxConfig
    from nexura_runtime.integrations.sandbox.module import SandboxExecutionModule

    assert SandboxExecutionModule().manifest.effect_policy == "idempotent_write"
    assert SandboxExecutionModule().manifest.always_write is True
    networked = SandboxExecutionModule(config=SandboxConfig(allow_network=True))
    assert networked.manifest.effect_policy == "non_idempotent_write"

    from nexura_runtime.studio.builtin_modules import get_default_registry
    from nexura_runtime.workflow import WorkflowNode, WorkflowSpec

    spec = WorkflowSpec(
        name="retrying",
        entry_node="run",
        nodes={
            "run": WorkflowNode(
                id="run",
                handler="nexura.tools.sandbox.execute",
                module_id="nexura.tools.sandbox.execute",
                retry={"max_attempts": 2},
                input_mapping={"code": "code"},
            )
        },
        terminal_nodes=["run"],
    )
    spec.validate(get_default_registry())
