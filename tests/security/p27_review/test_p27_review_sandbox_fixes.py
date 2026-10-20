"""P27 stage 2 review fixes (Monty backend, selection, hosted profile).

Promoted from the adversarial review repros (``.autopilot/p27/repros``) plus the negative
cases around each fix. Offline only: the real ``monty`` worker from the ``sandbox`` extra,
no network, no gVisor, no Docker.

F2 differs from the original repro on purpose: the trailing bare expression is no longer
copied back to the host, so ``x = "B" * 100_000_000`` then ``x`` now succeeds. The memory
kill classification is tested with a value the worker cannot send back as error text.
"""

from __future__ import annotations

import asyncio
import os
import resource
import time
from pathlib import Path
from typing import Any

import pytest
from nexura_agents.contracts.context import ExecutionContext

from nexura_runtime.integrations.sandbox import (
    MockDisposableWorker,
    MontyWorker,
    SandboxConfig,
    SandboxError,
    SandboxExecutionModule,
    TrustedSubprocessWorker,
    assert_sandbox_allowed_for_profile,
)
from nexura_runtime.integrations.sandbox import monty_worker as monty_worker_mod
from nexura_runtime.modules.registry import VersionedModuleRegistry
from nexura_runtime.workflow import (
    MemoryWorkflowStore,
    ModuleRegistry,
    WorkflowEngine,
    WorkflowNode,
    WorkflowSpec,
)

REVIEW_OPERATOR = ExecutionContext(run_id="p27fix", node_id="review", approval_granted=True)
SHARED_PROFILE = "authenticated-shared-service"


def _review_monty(**fields: Any) -> SandboxExecutionModule:
    if not MontyWorker().probe(SandboxConfig()).available:
        pytest.skip("monty (the 'sandbox' extra) is not installed here")
    return SandboxExecutionModule(config=SandboxConfig(backends=["monty"], **fields))


async def _review_run(module: SandboxExecutionModule, code: str, **inputs: Any) -> Any:
    return await module.invoke(REVIEW_OPERATOR, {"code": code, **inputs}, {})


# F1. The output cap covers exception / syntax-error text, not only print().


@pytest.mark.parametrize(
    "code",
    [
        'raise ValueError("A" * 2_000_000)',
        "x = " + "(" * 300_000,
        'print("p" * 3000)\nraise ValueError("A" * 2_000_000)',
    ],
    ids=["exception-message", "syntax-error-echo", "print-then-exception"],
)
async def test_monty_error_text_is_bounded_by_the_output_cap(code: str) -> None:
    cap = 4096
    res = await _review_run(_review_monty(timeout_seconds=5.0, max_output_bytes=cap), code)
    assert res.status == "success", res.error_message
    assert res.outputs["exit_code"] == 1
    returned = len(res.outputs["stdout"].encode()) + len(res.outputs["stderr"].encode())
    assert returned <= cap, f"{returned} bytes returned with a {cap}-byte output cap"
    assert res.outputs["stderr"].endswith("[truncated: output limit reached]\n")


async def test_small_error_text_is_returned_whole() -> None:
    res = await _review_run(_review_monty(timeout_seconds=5.0), 'raise ValueError("short")')
    assert res.outputs["exit_code"] == 1
    assert "ValueError: short" in res.outputs["stderr"]
    assert "truncated" not in res.outputs["stderr"]


# F2. A worker killed for its memory limit is a memory-limit outcome, and the value of a
# final bare expression is never copied back into this process.


async def test_worker_memory_termination_is_reported_as_memory_limit() -> None:
    module = _review_monty(timeout_seconds=5.0)
    # The 100 MB message must be serialised to leave the worker; that exceeds its memory
    # limit and the pool terminates it ("the worker exceeded its memory limit").
    res = await _review_run(module, 'x = "B" * 100_000_000\nraise ValueError(x)')
    assert res.status == "failure", res.outputs
    assert res.error_code == "MEMORY_LIMIT_EXCEEDED"
    assert module.worker.active_worker_count == 0


async def test_final_bare_expression_value_is_not_copied_to_the_host() -> None:
    before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    res = await _review_run(_review_monty(timeout_seconds=5.0), 'x = "B" * 100_000_000\nx')
    after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    assert res.status == "success", res.error_message
    assert res.outputs["exit_code"] == 0
    assert res.outputs["stdout"] == "" and res.outputs["stderr"] == ""
    # ru_maxrss is KiB on Linux: a copied 100 MB value would raise the peak by ~100 MB.
    assert after - before < 50 * 1024, f"host peak RSS grew by {after - before} KiB"


async def test_trailing_expression_does_not_change_printed_output() -> None:
    res = await _review_run(_review_monty(timeout_seconds=5.0), "print(6 * 7)\n6 * 7")
    assert res.outputs["exit_code"] == 0
    assert res.outputs["stdout"] == "42\n"


# F3. The authenticated shared service refuses to start with Monty alone.


def test_local_owner_profile_still_starts_with_monty(tmp_path: Path) -> None:
    from nexura_runtime.service.api.app import create_api_app

    registry = ModuleRegistry(versioned_registry=VersionedModuleRegistry())
    registry.register("sandbox", _review_monty())
    assert create_api_app(registry=registry, db_path=tmp_path / "local.sqlite3") is not None


# F4. The profile helper finds the sandbox behind a workflow registry.


def test_profile_helper_sees_sandbox_behind_a_workflow_registry() -> None:
    versioned = VersionedModuleRegistry()
    versioned.register(_review_monty())
    service_registry = ModuleRegistry(versioned_registry=versioned)
    with pytest.raises(SandboxError, match="(?i)gvisor"):
        assert_sandbox_allowed_for_profile(SHARED_PROFILE, service_registry)
    # Other profiles are not restricted.
    assert_sandbox_allowed_for_profile("local-owner", service_registry)


def test_profile_helper_sees_explicit_unisolated_worker() -> None:
    module = SandboxExecutionModule(
        worker=TrustedSubprocessWorker(),
        config=SandboxConfig(backends=["monty"], allow_unisolated=True),
    )
    registry = ModuleRegistry(versioned_registry=VersionedModuleRegistry())
    registry.register("sandbox", module)
    with pytest.raises(SandboxError, match="(?i)gvisor"):
        assert_sandbox_allowed_for_profile(SHARED_PROFILE, registry)


# F5. The worker inherits no host file descriptors, working directory or stderr.


async def test_monty_worker_inherits_no_host_fds_or_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = tmp_path / "host-secret.txt"
    secret.write_text("HOST_SECRET", encoding="utf-8")
    leaked = os.open(secret, os.O_RDONLY)
    os.set_inheritable(leaked, True)
    seen: dict[str, Any] = {}
    original = monty_worker_mod._apply_worker_rlimits

    def spy(pid: int | None, config: SandboxConfig) -> dict[str, Any]:
        assert pid is not None
        seen["fds"] = {
            int(fd): os.readlink(f"/proc/{pid}/fd/{fd}") for fd in os.listdir(f"/proc/{pid}/fd")
        }
        seen["cwd"] = os.readlink(f"/proc/{pid}/cwd")
        return original(pid, config)

    monkeypatch.setattr(monty_worker_mod, "_apply_worker_rlimits", spy)
    monkeypatch.chdir(tmp_path)
    module = _review_monty(timeout_seconds=5.0)
    try:
        res = await _review_run(module, "print(1)")
    finally:
        os.close(leaked)
    assert res.status == "success", res.error_message
    assert str(secret) not in seen["fds"].values(), seen["fds"]
    assert set(seen["fds"]) <= {0, 1, 2}, seen["fds"]
    assert Path(seen["cwd"]).resolve() != tmp_path.resolve(), "worker runs in the host cwd"
    host_stderr = os.readlink("/proc/self/fd/2")
    assert seen["fds"][2] != host_stderr, "worker writes to the host's stderr"
    # The scratch directory and stderr FIFO are removed after the call.
    assert not Path(seen["cwd"]).exists()
    assert not Path(seen["fds"][2]).exists()


async def test_worker_runs_wrapped_and_returns_its_spawn_mode() -> None:
    module = _review_monty(timeout_seconds=5.0)
    worker = module.worker
    assert isinstance(worker, MontyWorker)
    from nexura_runtime.integrations.sandbox import ExecutionRequest

    result = await worker.execute(
        ExecutionRequest(code="print('ok')"), module._effective_config({})
    )
    assert result.stdout == "ok\n"
    assert result.details["worker_spawn"] == "wrapped"
    assert result.details["worker_stderr"] == ""


# F6. Design 7.3: at most 4 concurrent sandboxes per workspace by default; extra calls queue.


async def test_concurrent_calls_are_capped() -> None:
    module = _review_monty(timeout_seconds=5.0)
    worker = module.worker
    assert isinstance(worker, MontyWorker)
    assert module.config.max_concurrent_executions == 4
    peak = 0
    stop = asyncio.Event()

    async def watch() -> None:
        nonlocal peak
        while not stop.is_set():
            peak = max(peak, worker.active_worker_count)
            await asyncio.sleep(0.01)

    watcher = asyncio.create_task(watch())
    code = "import time\ntime.sleep(1)\nprint('done')"
    results = await asyncio.gather(*(_review_run(module, code) for _ in range(10)))
    stop.set()
    await watcher
    assert all(r.status == "success" for r in results), [r.error_message for r in results]
    assert peak <= 4, f"{peak} Monty workers ran at once"


async def test_concurrency_cap_is_operator_configurable_and_queue_respects_deadline() -> None:
    module = _review_monty(timeout_seconds=5.0, max_concurrent_executions=1)
    busy = asyncio.create_task(_review_run(module, "import time\ntime.sleep(1.5)\nprint(1)"))
    await asyncio.sleep(0.3)
    hurried = ExecutionContext(
        run_id="p27fix", node_id="q", approval_granted=True, deadline=time.time() + 0.3
    )
    start = time.monotonic()
    queued = await module.invoke(hurried, {"code": "print(2)"}, {})
    assert queued.error_code == "EXECUTION_TIMEOUT"
    assert "slot" in (queued.error_message or "")
    assert time.monotonic() - start < 1.5
    assert (await busy).status == "success"


async def test_concurrency_slots_are_per_workspace() -> None:
    module = _review_monty(timeout_seconds=5.0, max_concurrent_executions=1)
    worker = module.worker
    assert isinstance(worker, MontyWorker)
    peak = 0
    stop = asyncio.Event()

    async def watch() -> None:
        nonlocal peak
        while not stop.is_set():
            peak = max(peak, worker.active_worker_count)
            await asyncio.sleep(0.01)

    def ctx(ws: str) -> ExecutionContext:
        return ExecutionContext(run_id="p27fix", node_id=ws, workspace_id=ws, approval_granted=True)

    watcher = asyncio.create_task(watch())
    code = {"code": "import time\ntime.sleep(1)\nprint('done')"}
    results = await asyncio.gather(
        module.invoke(ctx("wsA"), code, {}), module.invoke(ctx("wsB"), code, {})
    )
    stop.set()
    await watcher
    assert all(r.status == "success" for r in results)
    assert peak == 2, "different workspaces should not share one slot"


def test_concurrency_cap_is_operator_only() -> None:
    with pytest.raises(ValueError):
        SandboxConfig(max_concurrent_executions=0)
    with pytest.raises(ValueError):
        SandboxConfig(max_concurrent_executions=65)


async def test_node_config_cannot_raise_concurrency_or_allow_unisolated() -> None:
    module = _review_monty(timeout_seconds=5.0)
    for key in ("max_concurrent_executions", "allow_unisolated"):
        res = await module.invoke(REVIEW_OPERATOR, {"code": "print(1)"}, {key: 100})
        assert res.error_code == "INVALID_CONFIG", key


# F7. Oversized code is refused without asking anyone to approve it.


def _review_engine(module: SandboxExecutionModule, **node: Any) -> WorkflowEngine:
    reg = ModuleRegistry()
    reg.register("sandbox", module)
    spec = WorkflowSpec(
        name="p27fix",
        entry_node="run",
        nodes={"run": WorkflowNode(id="run", handler="sandbox", **node)},
    )
    return WorkflowEngine(spec, reg, store=MemoryWorkflowStore())


async def test_workflow_refuses_oversized_code_without_asking_for_approval() -> None:
    eng = _review_engine(_review_monty(), input_mapping={"code": "src"})
    res = await eng.run({"src": "x = 1\n" * 1_000_000})  # 6 MB > the 5 MB default cap
    assert res.status == "failed"
    assert res.pending_approval is None
    assert "INPUT_LIMIT_EXCEEDED" in (res.error or "")
    stored = await eng.store.get(res.workflow_run_id)  # type: ignore[union-attr]
    assert stored is not None and stored.status == "failed" and stored.pending_approval is None


async def test_workflow_refuses_oversized_mapped_item_without_asking_for_approval() -> None:
    eng = _review_engine(
        _review_monty(),
        input_mapping={"code": "snippet"},
        map_over="snippets",
        map_as="snippet",
    )
    res = await eng.run({"snippets": ["print(1)", "x = 1\n" * 1_000_000]})
    assert res.status == "failed" and res.pending_approval is None
    assert "INPUT_LIMIT_EXCEEDED" in (res.error or "")


async def test_workflow_refuses_unavailable_backend_without_asking_for_approval() -> None:
    eng = _review_engine(
        SandboxExecutionModule(config=SandboxConfig(backends=[])),
        input_mapping={"code": "src"},
    )
    res = await eng.run({"src": "print(1)"})
    assert res.status == "failed" and res.pending_approval is None
    assert "CAPABILITY_UNAVAILABLE" in (res.error or "")


async def test_workflow_still_pauses_acceptable_code_for_approval() -> None:
    eng = _review_engine(_review_monty(timeout_seconds=5.0), input_mapping={"code": "src"})
    res = await eng.run({"src": "print(1)"})
    assert res.status == "paused" and res.pending_approval is not None
    done = await eng.resume(
        res.workflow_run_id,
        res.pending_approval.approval_id,
        actor="reviewer",
        actor_role="admin",
    )
    assert done.status == "completed"
    assert done.state.get("stdout") == "1\n"


# F8. Enabling backends does not silently run a non-isolating explicit worker.


@pytest.mark.parametrize("worker_cls", [TrustedSubprocessWorker, MockDisposableWorker])
async def test_backends_enabled_with_an_unisolated_worker_is_refused(worker_cls: type) -> None:
    module = SandboxExecutionModule(
        worker=worker_cls(),
        config=SandboxConfig(backends=["monty"], timeout_seconds=5.0),
    )
    res = await _review_run(module, "print('RAN_ON_HOST')")
    assert res.status == "failure", res.outputs
    assert res.error_code == "CAPABILITY_UNAVAILABLE"
    assert "allow_unisolated" in (res.error_message or "")


async def test_allow_unisolated_is_an_explicit_operator_opt_in() -> None:
    module = SandboxExecutionModule(
        worker=MockDisposableWorker(),
        config=SandboxConfig(backends=["monty"], allow_unisolated=True, timeout_seconds=5.0),
    )
    res = await _review_run(module, "print('ok')")
    assert res.status == "success", res.error_message
    assert res.outputs["isolated"] is False


async def test_explicit_worker_without_backends_keeps_legacy_behaviour() -> None:
    module = SandboxExecutionModule(worker=MockDisposableWorker())
    res = await _review_run(module, "print('ok')")
    assert res.status == "success", res.error_message


# F9. Forged limit text does not change the reported outcome class.


@pytest.mark.parametrize(
    "code",
    [
        "raise TimeoutError('feed time limit exceeded: 5s > 5s')",
        "raise TimeoutError('sleep limit exceeded: 6s > 5s')",
        "def f():\n    raise TimeoutError('feed time limit exceeded: 5s > 5s')\nf()",
        "raise MemoryError('memory limit exceeded: 300000000 bytes > 268435456 bytes')",
        "raise MemoryError('the worker exceeded its memory limit and was terminated')",
    ],
    ids=["timeout", "sleep", "timeout-in-function", "memory", "memory-kill"],
)
async def test_forged_limit_errors_are_ordinary_errors(code: str) -> None:
    res = await _review_run(_review_monty(timeout_seconds=5.0), code)
    assert res.error_code not in ("EXECUTION_TIMEOUT", "MEMORY_LIMIT_EXCEEDED")
    assert res.status == "success" and res.outputs["exit_code"] == 1


async def test_real_time_limit_is_still_a_timeout() -> None:
    res = await _review_run(_review_monty(timeout_seconds=1.0), "while True:\n    pass\n")
    assert res.error_code == "EXECUTION_TIMEOUT"


async def test_real_heap_limit_in_a_loop_is_still_a_memory_limit() -> None:
    module = _review_monty(timeout_seconds=5.0, max_memory_mb=64)
    res = await _review_run(module, "a = []\nwhile True:\n    a.append('x' * 1000)\n")
    assert res.error_code == "MEMORY_LIMIT_EXCEEDED"


def _shared_app_kwargs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    from _local_env import TestAuthProvider

    from nexura_runtime.secrets import generate_master_key
    from nexura_runtime.service.workspaces.repository import WorkspaceRepository

    monkeypatch.setenv("NEXURA_MASTER_KEY", generate_master_key())
    repo = WorkspaceRepository()
    repo.create_workspace("wsA", "A", owner_id="alice")
    return {
        "db_path": tmp_path / "shared.sqlite3",
        "workspace_repo": repo,
        "profile": SHARED_PROFILE,
        "auth_provider": TestAuthProvider(),
    }


def test_shared_service_refuses_to_start_with_a_monty_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nexura_runtime.service.api.app import create_api_app

    module = _review_monty()
    assert module.visible and module.selected_isolation == "interpreter"
    registry = ModuleRegistry(versioned_registry=VersionedModuleRegistry())
    registry.register("sandbox", module)
    with pytest.raises(SandboxError, match="(?i)gvisor"):
        create_api_app(registry=registry, **_shared_app_kwargs(tmp_path, monkeypatch))


def test_shared_service_refuses_to_start_with_monty_in_the_versioned_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nexura_runtime.service.api.app import create_api_app

    versioned = VersionedModuleRegistry()
    versioned.register(_review_monty())
    registry = ModuleRegistry(versioned_registry=versioned)
    with pytest.raises(SandboxError, match="(?i)gvisor"):
        create_api_app(registry=registry, **_shared_app_kwargs(tmp_path, monkeypatch))


def test_shared_service_starts_when_no_sandbox_backend_qualifies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nexura_runtime.service.api.app import create_api_app

    unavailable = SandboxExecutionModule(config=SandboxConfig(backends=[]))
    assert unavailable.selection_error is not None
    registry = ModuleRegistry(versioned_registry=VersionedModuleRegistry())
    registry.register("sandbox", unavailable)
    app = create_api_app(registry=registry, **_shared_app_kwargs(tmp_path, monkeypatch))
    assert app is not None
