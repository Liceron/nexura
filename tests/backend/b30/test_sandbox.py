"""Focused unit and acceptance tests for B30 Isolated Code Execution Module."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from nexura_runtime.contracts.context import ExecutionContext
from nexura_sandbox import GVisorOCIWorker

from nexura_runtime.integrations.sandbox import (
    CapabilityUnavailableError,
    ExecutionRequest,
    ExecutionTimeoutError,
    IsolatedProcessWorker,
    MockDisposableWorker,
    OutputLimitExceededError,
    SandboxConfig,
    SandboxExecutionModule,
    SecurityViolationError,
    TrustedSubprocessWorker,
)


@pytest.mark.asyncio
async def test_default_backend_is_gvisor_oci() -> None:
    """The default backend for SandboxConfig and SandboxExecutionModule must be gvisor_oci."""
    config = SandboxConfig()
    assert config.backend == "gvisor_oci"

    module = SandboxExecutionModule()
    assert isinstance(module.worker, GVisorOCIWorker)
    assert module.worker.backend_name == "gvisor_oci"


@pytest.mark.asyncio
async def test_gvisor_oci_fails_closed_when_unconfigured() -> None:
    """gVisor OCI must fail closed when runsc or rootfs are unavailable or non-existent."""
    worker = GVisorOCIWorker()
    request = ExecutionRequest(code="print('should fail closed')\n")

    # 1. Missing runsc binary
    config_no_runsc = SandboxConfig(
        backend="gvisor_oci",
        runsc_path="/nonexistent/path/to/runsc",
        rootfs_path="/tmp",
    )
    with pytest.raises(CapabilityUnavailableError) as exc_info1:
        await worker.execute(request, config_no_runsc)
    assert exc_info1.value.backend == "gvisor_oci"
    assert "not found or not executable" in exc_info1.value.reason

    # 2. Missing rootfs directory
    config_no_rootfs = SandboxConfig(
        backend="gvisor_oci",
        runsc_path=sys.executable,  # exists, but rootfs missing
        rootfs_path="/nonexistent/rootfs/path",
    )
    with pytest.raises(CapabilityUnavailableError) as exc_info2:
        await worker.execute(request, config_no_rootfs)
    assert exc_info2.value.backend == "gvisor_oci"
    assert "rootfs" in exc_info2.value.reason
    assert worker.active_worker_count == 0


@pytest.mark.asyncio
async def test_module_fails_closed_with_capability_unavailable() -> None:
    """Invoking SandboxExecutionModule with unconfigured gVisor returns CAPABILITY_UNAVAILABLE."""
    worker = GVisorOCIWorker()
    # B37: runsc_path is an operator setting (node config can no longer set it).
    module = SandboxExecutionModule(
        worker=worker,
        config=SandboxConfig(backend="gvisor_oci", runsc_path="/nonexistent/runsc"),
    )
    context = ExecutionContext(
        run_id="run_b30_failclosed", node_id="node_sandbox_test", approval_granted=True
    )

    res = await module.invoke(
        context,
        inputs={"code": "print('hello')\n"},
        config={},
    )
    assert res.status == "failure"
    assert res.error_code == "CAPABILITY_UNAVAILABLE"


@pytest.mark.asyncio
async def test_live_gvisor_execution_under_genuine_isolation(
    gvisor_operator_config: SandboxConfig,
) -> None:
    """When configured via review-infra, worker executes code under genuine kernel isolation."""
    worker = GVisorOCIWorker()
    # B37: explicit operator config from the conftest helper (no cwd file discovery).
    config = gvisor_operator_config

    # 1. Successful math calculation in genuine gVisor container
    calc_code = "res = sum([x * x for x in range(1, 11)])\nprint(f'GVISOR_SUM:{res}')\n"
    request = ExecutionRequest(code=calc_code)

    res = await worker.execute(request, config)
    assert res.exit_code == 0
    assert res.isolated is True
    assert res.backend == "gvisor_oci"
    assert "GVISOR_SUM:385" in res.stdout
    assert worker.active_worker_count == 0

    # 2. Kernel network namespace isolation verification (default deny)
    net_code = (
        "import socket\n"
        "s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
        "s.settimeout(0.5)\n"
        "try:\n"
        "    s.connect(('1.1.1.1', 80))\n"
        "    print('NET:UNEXPECTED_CONNECTED')\n"
        "except Exception as e:\n"
        "    print('NET:DENIED:' + type(e).__name__)\n"
    )
    net_res = await worker.execute(ExecutionRequest(code=net_code), config)
    assert "NET:DENIED:" in net_res.stdout
    assert "NET:UNEXPECTED_CONNECTED" not in net_res.stdout

    # 3. Read-only rootfs enforcement
    ro_code = (
        "try:\n"
        "    with open('/etc/untrusted_write.txt', 'w') as f:\n"
        "        f.write('pwned')\n"
        "    print('FS:UNEXPECTED_WRITTEN')\n"
        "except Exception as e:\n"
        "    print('FS:DENIED:' + type(e).__name__)\n"
    )
    ro_res = await worker.execute(ExecutionRequest(code=ro_code), config)
    assert "FS:DENIED:" in ro_res.stdout
    assert "FS:UNEXPECTED_WRITTEN" not in ro_res.stdout


@pytest.mark.asyncio
async def test_gvisor_oci_bundle_generation() -> None:
    """GVisorOCIWorker correctly generates OCI runtime bundle specification (config.json)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        dummy_runsc = tmp_path / "runsc"
        # Synthetic script that captures and validates bundle config.json
        dummy_runsc.write_text(
            "#!/bin/sh\n"
            'for arg in "$@"; do\n'
            "  case $arg in\n"
            '    --bundle=*) BUNDLE_DIR="${arg#--bundle=}" ;;\n'
            "  esac\n"
            "done\n"
            'if [ -n "$BUNDLE_DIR" ] && [ -f "$BUNDLE_DIR/config.json" ]; then\n'
            '  cp "$BUNDLE_DIR/config.json" "' + str(tmp_path / "captured_config.json") + '"\n'
            "  echo 'OCI_BUNDLE_VALID'\n"
            "  exit 0\n"
            "fi\n"
            "exit 1\n",
            encoding="utf-8",
        )
        dummy_runsc.chmod(0o755)

        dummy_rootfs = tmp_path / "rootfs"
        dummy_rootfs.mkdir()

        worker = GVisorOCIWorker()
        config = SandboxConfig(
            backend="gvisor_oci",
            runsc_path=str(dummy_runsc),
            rootfs_path=str(dummy_rootfs),
            timeout_seconds=5.0,
            extra={"runsc_command_prefix": []},
        )
        request = ExecutionRequest(
            code="print('synthetic')\n",
            files={"nested/test.txt": "content"},
        )

        res = await worker.execute(request, config)
        assert res.exit_code == 0
        assert res.isolated is True
        assert res.backend == "gvisor_oci"
        assert "OCI_BUNDLE_VALID" in res.stdout
        assert worker.active_worker_count == 0

        # Inspect captured config.json
        captured_file = tmp_path / "captured_config.json"
        assert captured_file.is_file()
        oci_data = json.loads(captured_file.read_text(encoding="utf-8"))

        assert oci_data["ociVersion"] == "1.0.2"
        assert oci_data["root"]["readonly"] is True
        assert oci_data["root"]["path"] == str(dummy_rootfs.resolve())

        # Scoped scratch mount present
        mount_dests = [m["destination"] for m in oci_data["mounts"]]
        assert "/sandbox" in mount_dests
        assert "/proc" in mount_dests
        assert "/dev" in mount_dests
        # No host socket paths mounted
        assert not any("docker.sock" in m["destination"] for m in oci_data["mounts"])
        assert not any("var/run" in m["destination"] for m in oci_data["mounts"])

        # Namespaces present
        ns_types = [ns["type"] for ns in oci_data["linux"]["namespaces"]]
        assert "pid" in ns_types
        assert "network" in ns_types
        assert "mount" in ns_types


@pytest.mark.asyncio
async def test_synthetic_gvisor_host_denial_fails_closed() -> None:
    """Worker fails closed with CapabilityUnavailableError if runsc denies isolation."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        dummy_runsc = tmp_path / "runsc_failing"
        dummy_runsc.write_text(
            "#!/bin/sh\n"
            ">&2 echo 'creating container: creating sandbox: unshare: operation not permitted'\n"
            "exit 1\n",
            encoding="utf-8",
        )
        dummy_runsc.chmod(0o755)

        dummy_rootfs = tmp_path / "rootfs"
        dummy_rootfs.mkdir()

        worker = GVisorOCIWorker()
        config = SandboxConfig(
            backend="gvisor_oci",
            runsc_path=str(dummy_runsc),
            rootfs_path=str(dummy_rootfs),
            timeout_seconds=5.0,
            extra={"runsc_command_prefix": []},
        )
        request = ExecutionRequest(code="print('denied')\n")

        with pytest.raises(CapabilityUnavailableError) as exc_info:
            await worker.execute(request, config)

        assert "Host denied gVisor isolation" in exc_info.value.reason
        assert worker.active_worker_count == 0


@pytest.mark.asyncio
async def test_no_execution_in_api_process() -> None:
    """TrustedSubprocessWorker executes code in a distinct OS process and reports isolated=False."""
    worker = TrustedSubprocessWorker()
    config = SandboxConfig(timeout_seconds=5.0, backend="trusted_subprocess")
    request = ExecutionRequest(
        code="import os\nprint(f'WORKER_PID:{os.getpid()}')\n",
    )

    result = await worker.execute(request, config)
    assert result.exit_code == 0
    # Ordinary subprocess mode truthfully reports isolated=False
    assert result.isolated is False
    assert result.backend == "trusted_subprocess"
    assert "WORKER_PID:" in result.stdout

    caller_pid = os.getpid()
    worker_pid_str = result.stdout.strip().split("WORKER_PID:")[1]
    worker_pid = int(worker_pid_str)
    assert worker_pid != caller_pid
    assert worker.active_worker_count == 0


@pytest.mark.asyncio
async def test_isolated_process_worker_compat() -> None:
    """IsolatedProcessWorker is preserved as backward-compatible alias reporting isolated=False."""
    worker = IsolatedProcessWorker()
    assert worker.backend_name == "isolated_process"
    config = SandboxConfig(timeout_seconds=5.0, backend="isolated_process")
    request = ExecutionRequest(code="print('compat_test')\n")

    res = await worker.execute(request, config)
    assert res.exit_code == 0
    assert res.isolated is False
    assert "compat_test" in res.stdout
    assert worker.active_worker_count == 0


@pytest.mark.asyncio
async def test_bounded_infinite_loop() -> None:
    """Infinite loop terminates within timeout and raises ExecutionTimeoutError."""
    worker = TrustedSubprocessWorker()
    config = SandboxConfig(timeout_seconds=0.4, backend="trusted_subprocess")
    request = ExecutionRequest(code="while True:\n    pass\n")

    with pytest.raises(ExecutionTimeoutError) as exc_info:
        await worker.execute(request, config)

    assert exc_info.value.timeout_seconds == 0.4
    assert worker.active_worker_count == 0


@pytest.mark.asyncio
async def test_fork_attempt_denied() -> None:
    """Code attempting os.fork() is blocked and raises SecurityViolationError."""
    worker = TrustedSubprocessWorker()
    config = SandboxConfig(timeout_seconds=5.0, backend="trusted_subprocess")
    request = ExecutionRequest(
        code="import os\nos.fork()\nprint('forked')\n",
    )

    with pytest.raises(SecurityViolationError) as exc_info:
        await worker.execute(request, config)

    assert exc_info.value.violation_type == "fork_attempt"
    assert worker.active_worker_count == 0


@pytest.mark.asyncio
async def test_subprocess_attempt_denied() -> None:
    """Code attempting subprocess execution raises SecurityViolationError."""
    worker = TrustedSubprocessWorker()
    config = SandboxConfig(timeout_seconds=5.0, backend="trusted_subprocess")
    request = ExecutionRequest(
        code="import subprocess\nsubprocess.Popen(['echo', 'hello'])\n",
    )

    with pytest.raises(SecurityViolationError) as exc_info:
        await worker.execute(request, config)

    assert exc_info.value.violation_type == "fork_attempt"
    assert worker.active_worker_count == 0


@pytest.mark.asyncio
async def test_huge_output_capped() -> None:
    """Streaming stdout/stderr terminates on output cap and raises OutputLimitExceededError."""
    worker = TrustedSubprocessWorker()
    # 1024 byte cap
    config = SandboxConfig(
        timeout_seconds=5.0,
        max_output_bytes=1024,
        backend="trusted_subprocess",
    )
    # Output over cap (20,000 bytes)
    request = ExecutionRequest(
        code="print('A' * 20000)\n",
    )

    with pytest.raises(OutputLimitExceededError) as exc_info:
        await worker.execute(request, config)

    assert exc_info.value.max_bytes == 1024
    assert exc_info.value.actual_bytes > 1024
    assert worker.active_worker_count == 0


@pytest.mark.asyncio
async def test_path_escape_virtual_mount_denied() -> None:
    """Virtual mount paths escaping worker scratchpad raise SecurityViolationError."""
    worker = TrustedSubprocessWorker()
    config = SandboxConfig(timeout_seconds=5.0, backend="trusted_subprocess")
    request = ExecutionRequest(
        code="print('ok')\n",
        files={"../../escaped_secret.txt": "compromised"},
    )

    with pytest.raises(SecurityViolationError) as exc_info:
        await worker.execute(request, config)

    assert exc_info.value.violation_type == "path_escape"
    assert worker.active_worker_count == 0


@pytest.mark.asyncio
async def test_path_escape_absolute_path_denied() -> None:
    """Absolute virtual mount paths raise SecurityViolationError before spawning."""
    worker = TrustedSubprocessWorker()
    config = SandboxConfig(timeout_seconds=5.0, backend="trusted_subprocess")
    request = ExecutionRequest(
        code="print('ok')\n",
        files={"/etc/shadow": "hacked"},
    )

    with pytest.raises(SecurityViolationError) as exc_info:
        await worker.execute(request, config)

    assert exc_info.value.violation_type == "path_escape"
    assert worker.active_worker_count == 0


@pytest.mark.asyncio
async def test_path_escape_null_bytes_denied() -> None:
    """Virtual mount paths with null bytes raise SecurityViolationError."""
    worker = TrustedSubprocessWorker()
    config = SandboxConfig(timeout_seconds=5.0, backend="trusted_subprocess")
    request = ExecutionRequest(
        code="print('ok')\n",
        files={"foo\x00bar.txt": "payload"},
    )

    with pytest.raises(SecurityViolationError) as exc_info:
        await worker.execute(request, config)

    assert exc_info.value.violation_type == "path_escape"
    assert worker.active_worker_count == 0


@pytest.mark.asyncio
async def test_path_escape_open_denied() -> None:
    """Code attempting to open sensitive host system files raises SecurityViolationError."""
    worker = TrustedSubprocessWorker()
    config = SandboxConfig(timeout_seconds=5.0, backend="trusted_subprocess")
    request = ExecutionRequest(
        code="with open('/etc/shadow', 'r') as f:\n    print(f.read())\n",
    )

    with pytest.raises(SecurityViolationError) as exc_info:
        await worker.execute(request, config)

    assert exc_info.value.violation_type == "path_escape"
    assert worker.active_worker_count == 0


@pytest.mark.asyncio
async def test_network_attempt_denied() -> None:
    """Code attempting socket network connections with allow_network=False is denied."""
    worker = TrustedSubprocessWorker()
    config = SandboxConfig(
        timeout_seconds=5.0,
        allow_network=False,
        backend="trusted_subprocess",
    )
    request = ExecutionRequest(
        code="import socket\ns = socket.socket()\ns.connect(('127.0.0.1', 80))\n",
    )

    with pytest.raises(SecurityViolationError) as exc_info:
        await worker.execute(request, config)

    assert exc_info.value.violation_type == "network_attempt"
    assert worker.active_worker_count == 0


@pytest.mark.asyncio
async def test_socket_bypass_via_underscore_socket_denied() -> None:
    """Code attempting socket bypass via import _socket is blocked."""
    worker = TrustedSubprocessWorker()
    config = SandboxConfig(
        timeout_seconds=5.0,
        allow_network=False,
        backend="trusted_subprocess",
    )
    request = ExecutionRequest(
        code="import _socket\ns = _socket.socket()\n",
    )

    with pytest.raises(SecurityViolationError) as exc_info:
        await worker.execute(request, config)

    assert exc_info.value.violation_type == "network_attempt"
    assert worker.active_worker_count == 0


@pytest.mark.asyncio
async def test_cancelled_jobs_leave_no_worker() -> None:
    """Cancelling a running execution task cleans up worker process and active count."""
    worker = TrustedSubprocessWorker()
    config = SandboxConfig(timeout_seconds=10.0, backend="trusted_subprocess")
    request = ExecutionRequest(
        code="import time\ntime.sleep(5.0)\n",
    )

    task = asyncio.create_task(worker.execute(request, config))
    await asyncio.sleep(0.1)  # Allow process to start

    assert worker.active_worker_count == 1

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Process must be reaped and count must be 0
    assert worker.active_worker_count == 0


@pytest.mark.asyncio
async def test_secret_resolver_inaccessible() -> None:
    """Host environment secrets are stripped from worker process."""
    worker = TrustedSubprocessWorker()
    config = SandboxConfig(timeout_seconds=5.0, backend="trusted_subprocess")

    with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-secret-live-never-leak-12345"}):
        request = ExecutionRequest(
            code="import os\nprint('KEY_FOUND:' + str('OPENAI_API_KEY' in os.environ))\n",
        )
        result = await worker.execute(request, config)

        assert result.exit_code == 0
        assert "KEY_FOUND:False" in result.stdout
        assert "sk-secret-live-never-leak" not in result.stdout


@pytest.mark.asyncio
async def test_unsupported_language_rejected_before_spawn() -> None:
    """Requesting an unsupported language runtime fails before spawning."""
    worker = TrustedSubprocessWorker()
    config = SandboxConfig(timeout_seconds=5.0, backend="trusted_subprocess")
    request = ExecutionRequest(code="echo 'bash'", language="bash")

    with pytest.raises(CapabilityUnavailableError) as exc_info:
        await worker.execute(request, config)

    assert "Unsupported execution language 'bash'" in exc_info.value.reason
    assert worker.active_worker_count == 0


@pytest.mark.asyncio
async def test_untrusted_input_cannot_enable_policy_denied_network() -> None:
    """Untrusted inputs requesting allow_network=True cannot override policy-denied network."""
    module = SandboxExecutionModule(worker=TrustedSubprocessWorker())
    assert module.manifest.capabilities["network"] is False

    context = ExecutionContext(run_id="run_net_probe", node_id="node_sandbox_test")

    res = await module.invoke(
        context,
        inputs={"code": "print('exploit')", "allow_network": True},
        config={},
    )
    assert res.status == "failure"
    assert res.error_code == "SECURITY_VIOLATION_NETWORK_POLICY"


@pytest.mark.asyncio
async def test_execution_context_deadline_expired() -> None:
    """ExecutionContext deadline expired before invocation fails with EXECUTION_TIMEOUT."""
    module = SandboxExecutionModule(worker=TrustedSubprocessWorker())
    # Deadline in the past
    context = ExecutionContext(
        run_id="run_expired",
        node_id="node_sandbox_test",
        deadline=time.time() - 10.0,
    )

    res = await module.invoke(
        context,
        inputs={"code": "print('ok')"},
        config={},
    )
    assert res.status == "failure"
    assert res.error_code == "EXECUTION_TIMEOUT"


@pytest.mark.asyncio
async def test_mock_disposable_worker() -> None:
    """MockDisposableWorker executes offline deterministic test runs reporting isolated=False."""
    mock_worker = MockDisposableWorker()
    config = SandboxConfig(timeout_seconds=2.0)

    # 1. Normal run
    res = await mock_worker.execute(ExecutionRequest(code="print(42)"), config)
    assert res.exit_code == 0
    assert "42" in res.stdout
    assert res.isolated is False
    assert mock_worker.active_worker_count == 0

    # 2. Timeout
    with pytest.raises(ExecutionTimeoutError):
        await mock_worker.execute(ExecutionRequest(code="while True: pass"), config)

    # 3. Fork attempt
    with pytest.raises(SecurityViolationError):
        await mock_worker.execute(ExecutionRequest(code="os.fork()"), config)

    # 4. Network attempt (including _socket)
    with pytest.raises(SecurityViolationError):
        await mock_worker.execute(ExecutionRequest(code="_socket.socket()"), config)

    # 5. Path escape
    with pytest.raises(SecurityViolationError):
        await mock_worker.execute(ExecutionRequest(code="open('/etc/shadow')"), config)


@pytest.mark.asyncio
async def test_sandbox_execution_module_contract_with_trusted_subprocess() -> None:
    """SandboxExecutionModule conforms to BaseModule with TrustedSubprocessWorker."""
    module = SandboxExecutionModule(worker=TrustedSubprocessWorker())
    assert module.manifest.module_id == "nexura.tools.sandbox.execute"
    assert module.manifest.capabilities["sandbox_isolated"] is True

    # B37: every call runs code, so it needs an approval granted by a trusted caller.
    context = ExecutionContext(
        run_id="run_b30_test", node_id="node_sandbox_test", approval_granted=True
    )

    # 1. Successful execution
    res = await module.invoke(
        context,
        inputs={"code": "res = 21 * 2\nprint(f'ANSWER:{res}')\n"},
        config={},
    )
    assert res.status == "success"
    assert res.outputs["status"] == "completed"
    assert res.outputs["exit_code"] == 0
    assert "ANSWER:42" in res.outputs["stdout"]
    assert res.outputs["isolated"] is False
    assert res.outputs["backend"] == "trusted_subprocess"

    # 2. Security violation translates to clean ModuleResult failure
    res_violation = await module.invoke(
        context,
        inputs={"code": "import os\nos.fork()\n"},
        config={},
    )
    assert res_violation.status == "failure"
    assert res_violation.error_code == "SECURITY_VIOLATION_FORK_ATTEMPT"


@pytest.mark.asyncio
async def test_sandbox_execution_module_contract_with_gvisor(
    gvisor_operator_config: SandboxConfig,
) -> None:
    """SandboxExecutionModule conforms to BaseModule with GVisorOCIWorker under isolation."""
    # B37: operator config passed explicitly; approval granted by the (operator) caller.
    module = SandboxExecutionModule(worker=GVisorOCIWorker(), config=gvisor_operator_config)
    context = ExecutionContext(
        run_id="run_b30_gvisor_test", node_id="node_sandbox_gvisor", approval_granted=True
    )

    res = await module.invoke(
        context,
        inputs={"code": "print('GVISOR_CONTRACT_OK: ' + str(7 * 8))"},
        config={},
    )
    assert res.status == "success"
    assert res.outputs["status"] == "completed"
    assert res.outputs["exit_code"] == 0
    assert "GVISOR_CONTRACT_OK: 56" in res.outputs["stdout"]
    assert res.outputs["isolated"] is True
    assert res.outputs["backend"] == "gvisor_oci"
