"""Actual OCI isolation checks; no mock worker substitutes for this profile."""

import os
from pathlib import Path
from typing import Any

import pytest
from nexura_sandbox.gvisor_worker import GVisorOCIWorker

from nexura_runtime.integrations.sandbox.errors import (
    ExecutionTimeoutError,
    OutputLimitExceededError,
)
from nexura_runtime.integrations.sandbox.models import ExecutionRequest, SandboxConfig


@pytest.fixture
def real_config(make_gvisor_operator_config: Any) -> SandboxConfig:
    if os.environ.get("NEXURA_REAL_GVISOR") != "1":
        pytest.skip("Real configured gVisor profile not requested")
    # Explicit operator config (B37): runsc, rootfs and the command prefix come from the
    # conftest helper, never from a file in the current directory.
    config: SandboxConfig = make_gvisor_operator_config(timeout_seconds=10, max_processes=1)
    return config


@pytest.mark.asyncio
async def test_real_gvisor_cannot_read_host_file(
    real_config: SandboxConfig, tmp_path: Path
) -> None:
    sentinel = tmp_path / "host-only-secret.txt"
    sentinel.write_text("host-only-marker")
    code = f"print(open({str(sentinel)!r}).read())"
    worker = GVisorOCIWorker()
    result = await worker.execute(ExecutionRequest(code=code), real_config)
    assert result.isolated and result.backend == "gvisor_oci"
    assert result.exit_code != 0 and "host-only-marker" not in result.stdout
    assert worker.active_worker_count == 0


@pytest.mark.asyncio
async def test_real_gvisor_process_limit_applies_to_fork(real_config: SandboxConfig) -> None:
    result = await GVisorOCIWorker().execute(
        ExecutionRequest(code="import os\npid = os.fork()\nprint('FORK_SUCCEEDED', pid)"),
        real_config,
    )
    assert "FORK_SUCCEEDED" not in result.stdout
    assert result.exit_code != 0


@pytest.mark.asyncio
async def test_real_gvisor_low_level_socket_cannot_reach_host(
    real_config: SandboxConfig, review_infrastructure: dict[str, Any]
) -> None:
    port = review_infrastructure["qdrant"]["port"]
    code = (
        f"import _socket\ns = _socket.socket()\n"
        f"s.connect(('127.0.0.1', {port}))\nprint('HOST_CONNECTED')"
    )
    result = await GVisorOCIWorker().execute(ExecutionRequest(code=code), real_config)
    assert result.exit_code != 0 and "HOST_CONNECTED" not in result.stdout


@pytest.mark.asyncio
async def test_real_gvisor_timeout_and_output_cap_reap_worker(real_config: SandboxConfig) -> None:
    worker = GVisorOCIWorker()
    timeout_config = real_config.model_copy(update={"timeout_seconds": 0.3})
    with pytest.raises(ExecutionTimeoutError):
        await worker.execute(ExecutionRequest(code="while True: pass"), timeout_config)
    assert worker.active_worker_count == 0
    output_config = real_config.model_copy(update={"max_output_bytes": 1024})
    with pytest.raises(OutputLimitExceededError):
        await worker.execute(
            ExecutionRequest(code="while True: print('x' * 4096, flush=True)"), output_config
        )
    assert worker.active_worker_count == 0
