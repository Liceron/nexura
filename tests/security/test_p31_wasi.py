"""P31-S3 offline integration/negative tests with the real pinned CPython WASI build."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest
from nexura_agents.contracts.context import ExecutionContext
from nexura_sandbox import WasiWorker, wasi_runtime

from nexura_runtime.cli.main import main
from nexura_runtime.integrations.sandbox import (
    ExecutionRequest,
    ExecutionTimeoutError,
    MontyWorker,
    SandboxConfig,
    SandboxExecutionModule,
    SecurityViolationError,
    select_backend,
)
from nexura_runtime.integrations.sandbox.backends import sandbox_config_from_env

ROOT = Path(__file__).resolve().parents[2]
# The pinned runtime ZIP (wasi_runtime.RUNTIME_URL); CI downloads it and sets the variable.
LOCAL_ARCHIVE = ROOT / ".autopilot/p31s3/runtime/py.zip"
ARCHIVE = Path(os.environ.get("NEXURA_WASI_RUNTIME_ZIP") or LOCAL_ARCHIVE)
OPERATOR = ExecutionContext(run_id="p31-s3", node_id="wasi", approval_granted=True)


@pytest.fixture(scope="session")
def runtime(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A configured gate must fail, never skip, if its local prerequisites are missing."""
    assert ARCHIVE.is_file(), f"provide the pinned offline archive at {ARCHIVE}"
    assert importlib.util.find_spec("wasmtime"), "install sandbox-wasi before this profile"
    destination = tmp_path_factory.mktemp("p31-wasi") / "runtime"
    assert (
        main(
            [
                "sandbox",
                "install-runtime",
                "wasi",
                "--from-file",
                str(ARCHIVE),
                "--runtime-dir",
                str(destination),
            ]
        )
        == 0
    )
    return destination


def config(runtime: Path, **kwargs: Any) -> SandboxConfig:
    return SandboxConfig(backends=["wasi"], wasi_runtime_path=str(runtime), **kwargs)


async def run(runtime: Path, code: str, **kwargs: Any) -> Any:
    module = SandboxExecutionModule(config=config(runtime, **kwargs))
    result = await module.invoke(OPERATOR, {"code": code}, {})
    assert module.worker.active_worker_count == 0  # type: ignore[attr-defined]
    return result


def test_pin_and_install_marker(runtime: Path) -> None:
    assert wasi_runtime.file_sha256(ARCHIVE) == wasi_runtime.RUNTIME_SHA256
    wasi_runtime.verify_runtime(runtime)
    manifest = json.loads((runtime / "runtime.json").read_text())
    assert len(manifest["files"]) > 500
    assert (runtime / "LICENSE").is_file()
    assert (runtime / "wasmtime-49.0.0.cwasm").stat().st_size > 1_000_000
    assert WasiWorker().probe(config(runtime)).available


def test_install_rejects_wrong_digest_before_writing(tmp_path: Path) -> None:
    archive = tmp_path / "bad.zip"
    archive.write_bytes(b"untrusted artifact")
    destination = tmp_path / "runtime"
    with pytest.raises(ValueError, match="SHA256 mismatch"):
        wasi_runtime.install_runtime(archive, destination)
    assert not destination.exists()


def test_install_refuses_existing_destination(runtime: Path) -> None:
    with pytest.raises(ValueError, match="already exists"):
        wasi_runtime.install_runtime(ARCHIVE, runtime)


def test_install_without_file_never_downloads(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["sandbox", "install-runtime", "wasi"]) == 1
    assert "no download is performed" in capsys.readouterr().err


def test_probe_missing_package(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    status = WasiWorker().probe(SandboxConfig())
    assert not status.available and "wasmtime is missing" in status.reason


def test_probe_missing_runtime(tmp_path: Path) -> None:
    status = WasiWorker().probe(config(tmp_path / "missing"))
    assert not status.available and "install-runtime wasi --from-file ZIP" in status.reason


def test_probe_rejects_modified_runtime(runtime: Path, tmp_path: Path) -> None:
    # Copy only the marker and a deliberately modified wasm: probe must hash before use.
    shutil.copy(runtime / "runtime.json", tmp_path / "runtime.json")
    (tmp_path / "python.wasm").write_bytes(b"changed")
    status = WasiWorker().probe(config(tmp_path))
    assert not status.available


def test_operator_env_and_isolation_floor(runtime: Path) -> None:
    selected = sandbox_config_from_env(
        {"NEXURA_SANDBOX_BACKENDS": "wasi", "NEXURA_SANDBOX_WASI_RUNTIME": str(runtime)}
    )
    assert selected and selected.wasi_runtime_path == str(runtime)
    assert select_backend(selected).backend_name == "wasi"
    module = SandboxExecutionModule(config=config(runtime, min_isolation="os_sandbox"))
    assert module.selected_backend == "unavailable"
    assert "below the operator minimum" in (module.selection_error or "")


async def test_real_full_python_stdlib_classes_and_generator(runtime: Path) -> None:
    result = await run(
        runtime,
        r"""
import collections, fractions, json, pathlib, re, statistics, sys, xml.etree.ElementTree as ET
class Base:
    value = 42
class Child(Base):
    pass
def values():
    yield Child.value
assert list(values()) == [42]
assert fractions.Fraction(1, 3) * 3 == 1
assert statistics.mean([2, 4]) == 3
assert ET.fromstring('<root/>').tag == 'root'
assert re.fullmatch(r'\d+', '42')
assert sys.flags.isolated and sys.flags.no_site
assert sys.getrecursionlimit() == 200
print(json.dumps({'python': sys.version_info[:3], 'value': list(values())}))
""",
    )
    assert result.status == "success", result.error_message
    assert result.outputs["exit_code"] == 0, result.outputs
    assert json.loads(result.outputs["stdout"]) == {"python": [3, 13, 15], "value": [42]}
    assert result.outputs["backend"] == "wasi"
    assert result.outputs["isolation"] == "interpreter"


async def test_stdio_env_input_files_and_readonly_runtime(runtime: Path) -> None:
    worker = WasiWorker()
    result = await worker.execute(
        ExecutionRequest(
            code="""
import os, sys
assert dict(os.environ) == {'LANG': 'C'}
assert sys.stdin.read() == 'DATA'
assert open('/sandbox/nested/input.txt').read() == 'INPUT'
for path in ['/lib/python3.13/os.py', '/sandbox/nested/input.txt', '/scratch/x']:
    try:
        open(path, 'w').write('changed')
        raise AssertionError('writable: ' + path)
    except OSError:
        pass
print('READONLY_OK')
""",
            stdin="DATA",
            env={"LANG": "C"},
            files={"nested/input.txt": "INPUT"},
        ),
        config(runtime),
    )
    assert result.exit_code == 0, result.stderr
    assert result.stdout == "READONLY_OK\n"
    wasi_runtime.verify_runtime(runtime)


async def test_scratch_can_write_and_is_fresh(runtime: Path) -> None:
    code = (
        "assert not __import__('os').path.exists('/scratch/a')\n"
        "open('/scratch/a','w').write('ok')\nprint(open('/scratch/a').read())"
    )
    for _ in range(2):
        result = await run(runtime, code, wasi_scratch_enabled=True)
        assert result.outputs["exit_code"] == 0, result.outputs
        assert result.outputs["stdout"] == "ok\n"


@pytest.mark.parametrize(
    "code",
    [
        "open('/scratch/fill','wb',buffering=0).write(b'x'*2048)",
        "import os; f=os.open('/scratch/fill', os.O_CREAT|os.O_WRONLY); os.pwrite(f, b'x'*2048, 0)",
        "import os\nfor i in range(3): os.mkdir('/scratch/d'+str(i))",
    ],
)
async def test_scratch_byte_and_entry_caps(runtime: Path, code: str) -> None:
    result = await run(
        runtime, code, wasi_scratch_enabled=True, wasi_scratch_bytes=1024, wasi_scratch_files=2
    )
    assert result.error_code == "SECURITY_VIOLATION_DISK_LIMIT", result


async def test_sparse_allocation_cannot_bypass_cap(runtime: Path) -> None:
    result = await run(
        runtime,
        """
import os
with open('/scratch/sparse', 'wb') as f:
    try:
        f.truncate(10**9)
        print('BYPASS')
    except OSError:
        print('BLOCKED')
""",
        wasi_scratch_enabled=True,
        wasi_scratch_bytes=1024,
    )
    assert result.outputs["exit_code"] == 0, result
    assert result.outputs["stdout"] == "BLOCKED\n"


@pytest.mark.parametrize(
    "code",
    [
        "print('EXECUTION_TIMEOUT MEMORY_LIMIT_EXCEEDED SECURITY_VIOLATION')\n"
        "raise MemoryError('memory limit exceeded')",
        "import sys; sys.stderr.write('CAPABILITY_UNAVAILABLE'); raise TimeoutError('fuel')",
    ],
)
async def test_guest_cannot_forge_resource_results(runtime: Path, code: str) -> None:
    result = await run(runtime, code)
    assert result.status == "success" and result.outputs["exit_code"] == 1, result


async def test_fuel_is_a_timeout_independent_of_wall_timer(runtime: Path) -> None:
    with pytest.raises(ExecutionTimeoutError):
        await WasiWorker().execute(
            ExecutionRequest(code="while True: pass"),
            config(runtime, wasi_fuel=100_000, timeout_seconds=10),
        )


async def test_epoch_stops_loop_with_large_fuel_budget(runtime: Path) -> None:
    result = await run(runtime, "while True: pass", timeout_seconds=0.1, wasi_fuel=100_000_000_000)
    assert result.error_code == "EXECUTION_TIMEOUT", result


async def test_linear_memory_limit_returns_guest_memoryerror(runtime: Path) -> None:
    result = await run(runtime, "a = bytearray(512*1024*1024)", max_memory_mb=64)
    assert result.status == "success" and result.outputs["exit_code"] != 0, result
    assert "MemoryError" in result.outputs["stderr"]


@pytest.mark.parametrize(
    "code",
    [
        "import sys; sys.stderr.write('e'*10000)",
        "import os; os.write(1,b'o'*10000)",
        "print('o'*800); __import__('sys').stderr.write('e'*800)",
    ],
)
async def test_output_cap_covers_both_streams_and_os_write(runtime: Path, code: str) -> None:
    result = await run(runtime, code, max_output_bytes=1024)
    assert result.error_code == "OUTPUT_LIMIT_EXCEEDED", result


async def test_forbidden_direct_env_and_network(runtime: Path) -> None:
    with pytest.raises(SecurityViolationError, match="allowlist"):
        await WasiWorker().execute(
            ExecutionRequest(code="pass", env={"SECRET": "x"}), config(runtime)
        )
    with pytest.raises(SecurityViolationError, match="network"):
        await WasiWorker().execute(
            ExecutionRequest(code="pass"), config(runtime, allow_network=True)
        )


async def test_no_sockets_even_without_host_listener(runtime: Path) -> None:
    result = await run(
        runtime,
        """
import socket
try:
    socket.socket().connect(('127.0.0.1', 9))
    print('CONNECTED')
except OSError:
    print('BLOCKED')
""",
    )
    assert result.outputs["exit_code"] == 0 and result.outputs["stdout"] == "BLOCKED\n", result


async def test_monty_full_python_fallback_is_opt_in(runtime: Path) -> None:
    assert MontyWorker().probe(SandboxConfig()).available, (
        "install sandbox extra for fallback profile"
    )
    inputs = {"code": "print(input())", "stdin": "FALLBACK_OK\n"}
    only = SandboxExecutionModule(config=SandboxConfig(backends=["monty"]))
    refused = await only.invoke(OPERATOR, inputs, {})
    assert refused.error_code == "UNSUPPORTED_FEATURE"
    both = SandboxExecutionModule(
        config=SandboxConfig(backends=["monty", "wasi"], wasi_runtime_path=str(runtime))
    )
    result = await both.invoke(OPERATOR, inputs, {})
    assert result.status == "success", result
    assert result.outputs["exit_code"] == 0, result
    assert result.outputs["backend"] == "wasi" and result.outputs["stdout"] == "FALLBACK_OK\n"
    assert both.selected_backend == "monty"  # selection stays per module; outcome is per call


@pytest.mark.parametrize(
    "code",
    [
        "class Base: pass\nclass Child(Base): pass\nprint('FULL_PYTHON')",
        "def values():\n yield 'FULL_PYTHON'\nprint(next(values()))",
        "print(next(x for x in ['FULL_PYTHON']))",
    ],
)
async def test_full_python_syntax_selects_enabled_wasi(runtime: Path, code: str) -> None:
    module = SandboxExecutionModule(
        config=SandboxConfig(backends=["monty", "wasi"], wasi_runtime_path=str(runtime))
    )
    assert module.selected_backend == "monty"
    result = await module.invoke(OPERATOR, {"code": code}, {})
    assert result.status == "success" and result.outputs["exit_code"] == 0, result
    assert result.outputs["backend"] == "wasi"
    assert result.outputs["stdout"] == "FULL_PYTHON\n"


async def test_scratch_sparse_seek_and_renumber_are_refused(runtime: Path) -> None:
    result = await run(
        runtime,
        """
import os
with open('/scratch/file', 'wb') as f:
    try:
        f.seek(10**9)
        print('SEEK_BYPASS')
    except OSError:
        print('SEEK_BLOCKED')
    try:
        os.dup2(f.fileno(), 1)
        print('FD_BYPASS')
    except (OSError, AttributeError):
        print('FD_BLOCKED')
""",
        wasi_scratch_enabled=True,
        wasi_scratch_bytes=1024,
    )
    assert result.outputs["exit_code"] == 0, result
    assert result.outputs["stdout"] == "SEEK_BLOCKED\nFD_BLOCKED\n", result


async def test_cancel_reaps_child(runtime: Path) -> None:
    worker = WasiWorker()
    task = asyncio.create_task(
        worker.execute(ExecutionRequest(code="while True: pass"), config(runtime))
    )
    try:
        for _ in range(100):
            if worker.active_worker_count:
                break
            await asyncio.sleep(0.01)
        assert worker.active_worker_count == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert worker.active_worker_count == 0
