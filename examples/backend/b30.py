"""B30 Standalone Runnable Example: Isolated Code Execution Module.

Demonstrates:
1. Default genuine gVisor OCI backend (gvisor_oci) failing closed when host lacks runsc/rootfs.
2. Explicitly opted-in trusted subprocess mode reporting isolated=False.
3. Strict resource boundaries (CPU/time, output caps, secret scrubbing, fork and network denial).
4. BaseModule manifest contract invocation via ExecutionContext.

Run keylessly:
    python examples/backend/b30.py
    uv run python examples/backend/b30.py
"""

from __future__ import annotations

import asyncio
import os

from nexura_runtime.contracts.context import ExecutionContext
from nexura_sandbox import GVisorOCIWorker

from nexura_runtime.integrations.sandbox import (
    CapabilityUnavailableError,
    ExecutionRequest,
    ExecutionTimeoutError,
    OutputLimitExceededError,
    SandboxConfig,
    SandboxExecutionModule,
    SecurityViolationError,
    TrustedSubprocessWorker,
)


async def main() -> None:
    print("=== Nexura B30: Isolated Code Execution Demonstration ===\n")

    # 1. Default Backend: Genuine gVisor OCI (Fails Closed if unconfigured)
    print("--- 1. Default Backend: Genuine gVisor OCI Selection ---")
    default_config = SandboxConfig()
    print(f"Default SandboxConfig backend: {default_config.backend}")

    default_worker = GVisorOCIWorker()
    print(f"Default Module worker: {default_worker.backend_name}")

    try:
        gvisor_res = await default_worker.execute(
            ExecutionRequest(code="print('GVISOR_CONTAINER_EXECUTION_SUCCESS')"),
            default_config,
        )
        print(f"gVisor Output: {gvisor_res.stdout.strip()}")
        print(f"gVisor Isolated Boundary: {gvisor_res.isolated} (backend: {gvisor_res.backend})")
    except CapabilityUnavailableError as exc:
        print(f"Fail-Closed Defense Triggered: {exc.message}")

    # Verify fail-closed defense when runtime is missing
    unconfigured_config = SandboxConfig(
        backend="gvisor_oci",
        runsc_path="/nonexistent/missing_runsc",
    )
    try:
        await default_worker.execute(
            ExecutionRequest(code="print('should fail')"),
            unconfigured_config,
        )
    except CapabilityUnavailableError as exc:
        print(f"Verified Fail-Closed Defense (unconfigured runtime): {exc.reason}\n")

    # 2. Explicitly Opted-In Trusted Subprocess Mode
    print("--- 2. Explicitly Opted-In Trusted Subprocess Execution ---")
    trusted_worker = TrustedSubprocessWorker()
    trusted_config = SandboxConfig(
        timeout_seconds=5.0,
        max_memory_mb=128,
        backend="trusted_subprocess",
    )

    print(f"Caller API Process PID: {os.getpid()}")
    print(f"Trusted Worker Backend: {trusted_worker.backend_name}")

    calc_code = """
import json

# Read data from isolated scratchpad file
with open("dataset.json", "r") as f:
    data = json.load(f)

numbers = data.get("numbers", [])
total = sum(numbers)
mean = total / len(numbers) if numbers else 0

result = {
    "count": len(numbers),
    "sum": total,
    "mean": mean,
    "squared_sum": sum(x * x for x in numbers),
}

print("CALCULATION_RESULT:" + json.dumps(result))
"""

    dataset_json = '{"numbers": [10, 25, 42, 88, 105, 3]}'

    request = ExecutionRequest(
        code=calc_code,
        files={"dataset.json": dataset_json},
    )

    result = await trusted_worker.execute(request, trusted_config)
    print(f"Worker Process Exit Code: {result.exit_code}")
    print(f"Isolated Boundary: {result.isolated} (Truthful: Subprocess is NOT an OCI sandbox)")
    print(f"Execution Duration: {result.duration_ms:.2f} ms")
    print(f"Worker Output:\n  {result.stdout.strip()}")
    print(f"Active Workers Left: {trusted_worker.active_worker_count}\n")

    # 3. Security Boundary: Secret Scrubbing Verification
    print("--- 3. Host Secret Isolation Boundary ---")
    secret_probe_code = """
import os
has_openai_key = 'OPENAI_API_KEY' in os.environ
has_anthropic_key = 'ANTHROPIC_API_KEY' in os.environ
print(f"Probe Result: OPENAI={has_openai_key}, ANTHROPIC={has_anthropic_key}")
"""
    secret_req = ExecutionRequest(code=secret_probe_code)
    secret_res = await trusted_worker.execute(secret_req, trusted_config)
    print(f"Secret Scrubbing Output: {secret_res.stdout.strip()}\n")

    # 4. Security Boundary: Fork Bomb Denial
    print("--- 4. Process Boundary: Fork Attempt Denial ---")
    fork_probe_code = "import os\nos.fork()\nprint('Forked successfully')\n"
    try:
        await trusted_worker.execute(ExecutionRequest(code=fork_probe_code), trusted_config)
        print("ERROR: Fork attempt unexpectedly succeeded!")
    except SecurityViolationError as exc:
        print(f"Blocked Security Violation: [{exc.violation_type}] {exc}")

    # 5. Security Boundary: Socket Bypass Denial (_socket)
    print("\n--- 5. Network Boundary: Socket Bypass Denial (_socket) ---")
    socket_probe_code = "import _socket\ns = _socket.socket()\n"
    try:
        await trusted_worker.execute(ExecutionRequest(code=socket_probe_code), trusted_config)
        print("ERROR: Socket attempt unexpectedly succeeded!")
    except SecurityViolationError as exc:
        print(f"Blocked Security Violation: [{exc.violation_type}] {exc}")

    # 6. Resource Boundary: Timeout Enforcement
    print("\n--- 6. Resource Boundary: Timeout Enforcement ---")
    infinite_loop_code = "while True:\n    pass\n"
    timeout_config = SandboxConfig(timeout_seconds=0.3, backend="trusted_subprocess")
    try:
        await trusted_worker.execute(ExecutionRequest(code=infinite_loop_code), timeout_config)
        print("ERROR: Infinite loop unexpectedly survived!")
    except ExecutionTimeoutError as exc:
        print(f"Bounded Timeout: {exc}")
        print(f"Active Workers After Timeout Kill: {trusted_worker.active_worker_count}")

    # 7. Resource Boundary: Output Capping
    print("\n--- 7. Resource Boundary: Output Limit Capping ---")
    huge_output_code = "print('A' * 20000)\n"
    output_config = SandboxConfig(
        timeout_seconds=5.0,
        max_output_bytes=1024,
        backend="trusted_subprocess",
    )
    try:
        await trusted_worker.execute(ExecutionRequest(code=huge_output_code), output_config)
        print("ERROR: Huge output unexpectedly survived!")
    except OutputLimitExceededError as exc:
        print(f"Bounded Output: {exc}")

    # 8. Contract Invocation via ExecutionContext
    print("\n--- 8. BaseModule Manifest Contract Invocation ---")
    # Limits come from the operator SandboxConfig; inputs may only lower them (B37).
    trusted_module = SandboxExecutionModule(worker=trusted_worker, config=trusted_config)
    # Every sandbox call runs code, so it needs approval. In a workflow the engine pauses
    # for a human; this script is the operator, so it grants approval explicitly.
    ctx = ExecutionContext(
        run_id="run_b30_example", node_id="node_isolated_exec", approval_granted=True
    )
    mod_result = await trusted_module.invoke(
        ctx,
        inputs={"code": "print('Contract execution successful: ' + str(6 * 7))"},
        config={},
    )
    print(f"Module Status: {mod_result.status}")
    print(f"Module Output: {mod_result.outputs.get('stdout', '').strip()}")
    print(f"Module Result Isolated Flag: {mod_result.outputs.get('isolated')}")

    # 9. Policy-Denied Network Prevention
    print("\n--- 9. Policy-Denied Network Input Rejection ---")
    net_result = await trusted_module.invoke(
        ctx,
        inputs={"code": "print('exploit')", "allow_network": True},
        config={},
    )
    print(f"Policy Rejection Status: {net_result.status}")
    print(f"Policy Error Code: {net_result.error_code}")

    print("\n=== All B30 isolated execution demonstrations completed successfully! ===")


if __name__ == "__main__":
    asyncio.run(main())
