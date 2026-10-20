"""P27: one escape-test suite run against every available sandbox backend.

Backends: ``monty`` (needs the ``sandbox`` extra), ``wasi`` (wasmtime + pinned runtime),
``bubblewrap`` (Linux + user namespaces),
and ``gvisor_oci`` (needs this host's
runsc + rootfs, configured explicitly through the b30 operator helper). A backend is skipped
only when it is not installed here; a failing attack is a failing test. Everything is
offline: network attacks target a loopback listener started by the test, and
169.254.169.254 with a short timeout. Assertions check outcomes (no host data in any
output, no host side effect, no connection, bounded time), not error text.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import socket
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from nexura_agents.contracts.context import ExecutionContext
from nexura_sandbox import BubblewrapWorker, GVisorOCIWorker, WasiWorker

from nexura_runtime.integrations.sandbox import (
    MockDisposableWorker,
    MontyWorker,
    SandboxConfig,
    SandboxExecutionModule,
    TrustedSubprocessWorker,
    select_backend,
)

ROOT = Path(__file__).resolve().parents[2]
OPERATOR = ExecutionContext(run_id="p27", node_id="escape", approval_granted=True)


def _b30_helper() -> Any:
    spec = importlib.util.spec_from_file_location(
        "_p27_b30_operator", ROOT / "tests" / "backend" / "b30" / "conftest.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make(backend: str, **limits: Any) -> SandboxExecutionModule:
    base: dict[str, Any] = {"timeout_seconds": 5.0, **limits}
    if backend == "wasi":
        runtime = os.environ.get("NEXURA_SANDBOX_WASI_RUNTIME")
        config = SandboxConfig(backends=["wasi"], wasi_runtime_path=runtime, **base)
        status = WasiWorker().probe(config)
        if runtime:
            # An explicitly installed gate runtime must run every case, never skip.
            assert status.available, status.reason
        elif not status.available:
            pytest.skip(status.reason)
        return SandboxExecutionModule(config=config)
    if backend == "monty":
        status = MontyWorker().probe(SandboxConfig())
        if not status.available:
            pytest.skip(f"monty not installed here: {status.reason}")
        return SandboxExecutionModule(config=SandboxConfig(backends=["monty"], **base))
    if backend == "bubblewrap":
        config = SandboxConfig(backends=["bubblewrap"], **base)
        status = BubblewrapWorker().probe(config)
        if not status.available:
            pytest.skip(status.reason)
        return SandboxExecutionModule(config=config)
    helper = _b30_helper()
    if not helper.live_gvisor_available():
        pytest.skip("gVisor test profile not enabled (runsc, rootfs, NEXURA_REAL_GVISOR=1)")
    config = helper.operator_gvisor_config(**base)
    return SandboxExecutionModule(worker=GVisorOCIWorker(), config=config)


BACKENDS = ["monty", "wasi", "bubblewrap", "gvisor_oci"]


# Cases about OS-level mounts and tmpfs only apply to os_sandbox backends; they are
# parametrised over these alone so other backends are not reported as skipped.
OS_BACKENDS = ["bubblewrap"]


@pytest.fixture(params=OS_BACKENDS)
def os_backend(request: pytest.FixtureRequest) -> str:
    name = str(request.param)
    _make(name)
    return name


@pytest.fixture(params=BACKENDS)
def backend(request: pytest.FixtureRequest) -> str:
    name = str(request.param)
    # Establish availability before fixtures such as the host TCP listener are created.
    # Restricted build environments may forbid both namespaces and host sockets.
    _make(name)
    return name


@pytest.fixture
def sentinel(tmp_path: Path) -> tuple[Path, str]:
    marker = f"HOST_SENTINEL_{uuid.uuid4().hex}"
    path = tmp_path / "host-secret.txt"
    path.write_text(marker, encoding="utf-8")
    return path, marker


@pytest.fixture
def listener() -> Iterator[tuple[int, list[int]]]:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    srv.settimeout(0.2)
    hits: list[int] = []
    stop = threading.Event()

    def serve() -> None:
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except OSError:
                continue
            hits.append(1)
            conn.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    yield srv.getsockname()[1], hits
    stop.set()
    thread.join(2)
    srv.close()


async def _run(module: SandboxExecutionModule, code: str, **inputs: Any) -> tuple[Any, str]:
    res = await module.invoke(OPERATOR, {"code": code, **inputs}, {})
    text = " ".join(
        str(x)
        for x in (
            res.error_message or "",
            (res.outputs or {}).get("stdout", ""),
            (res.outputs or {}).get("stderr", ""),
        )
    )
    return res, text


def _not_capability_or_violation(res: Any) -> None:
    code = res.error_code or ""
    assert code != "CAPABILITY_UNAVAILABLE", res.error_message
    assert not code.startswith("SECURITY_VIOLATION"), res.error_message


# 1. Host files: absolute paths, project files, ~/.ssh, traversal and symlinks.


async def test_read_host_sentinel_and_passwd(backend: str, sentinel: tuple[Path, str]) -> None:
    module = _make(backend)
    path, marker = sentinel
    host_passwd = Path("/etc/passwd").read_text(encoding="utf-8")
    home = str(Path.home())
    code = (
        "for p in [{!r}, '/etc/passwd', {!r}, {!r}]:\n"
        "    try:\n"
        "        print('READ:' + open(p).read())\n"
        "    except Exception as e:\n"
        "        print('DENIED', type(e).__name__)\n"
    ).format(str(path), str(ROOT / "pyproject.toml"), str(ROOT / "AGENTS.md"))
    res, text = await _run(module, code)
    assert marker not in text
    assert host_passwd.strip() not in text
    assert home not in text  # the host user's passwd line names the home directory
    assert 'name = "nexura-runtime"' not in text
    assert "Nexura standalone SDK implementation" not in text


async def test_read_ssh_directory(backend: str) -> None:
    module = _make(backend)
    ssh = Path.home() / ".ssh"
    code = (
        "import os\n"
        "for name in ['authorized_keys', 'id_rsa', 'id_ed25519', 'known_hosts']:\n"
        "    try:\n"
        f"        print('SSH_READ:' + open({str(ssh)!r} + '/' + name).read())\n"
        "    except Exception as e:\n"
        "        print('DENIED', type(e).__name__)\n"
    )
    res, text = await _run(module, code)
    assert "SSH_READ:" not in text


async def test_traversal_and_symlink_tricks(backend: str, sentinel: tuple[Path, str]) -> None:
    module = _make(backend)
    path, marker = sentinel
    code = (
        "import os\n"
        "try:\n"
        "    os.symlink({!r}, 'link.txt')\n"
        "except Exception as e:\n"
        "    print('NOLINK', type(e).__name__)\n"
        "for p in ['link.txt', '../' * 12 + {!r}, '/sandbox/../' + {!r}]:\n"
        "    try:\n"
        "        print('READ:' + open(p).read())\n"
        "    except Exception as e:\n"
        "        print('DENIED', type(e).__name__)\n"
    ).format(str(path), str(path).lstrip("/"), str(path).lstrip("/"))
    res, text = await _run(module, code, files={"in.txt": "data"})
    assert marker not in text
    for bad in ("../escape.txt", "/etc/cron.d/x", "a/../../b", "x\x00y"):
        refused, _ = await _run(module, "print(1)", files={bad: "x"})
        assert refused.status == "failure"
        assert refused.error_code == "SECURITY_VIOLATION_PATH_ESCAPE", bad


# 2. Environment and secrets.


async def test_env_and_proc_environ(backend: str, monkeypatch: pytest.MonkeyPatch) -> None:
    marker = f"sk-fake-{uuid.uuid4().hex}"
    monkeypatch.setenv("NEXURA_FAKE_SECRET", marker)
    monkeypatch.setenv("OPENAI_API_KEY", marker)
    module = _make(backend)
    code = (
        "import os\n"
        "for f in [lambda: dict(os.environ), lambda: os.getenv('NEXURA_FAKE_SECRET'),\n"
        "          lambda: open('/proc/self/environ').read(),\n"
        "          lambda: open('/proc/1/environ').read()]:\n"
        "    try:\n"
        "        print('ENV:', f())\n"
        "    except Exception as e:\n"
        "        print('DENIED', type(e).__name__)\n"
    )
    res, text = await _run(module, code)
    assert marker not in text
    refused, _ = await _run(module, "print(1)", env={"NEXURA_FAKE_SECRET": "x"})
    assert refused.error_code == "SECURITY_VIOLATION_ENV"


# 3. Network: a loopback listener and the cloud metadata address.


async def test_no_network(backend: str, listener: tuple[int, list[int]]) -> None:
    port, hits = listener
    module = _make(backend)
    code = (
        f"for host, port in [('127.0.0.1', {port}), ('169.254.169.254', 80)]:\n"
        "    try:\n"
        "        import socket\n"
        "        s = socket.create_connection((host, port), timeout=1)\n"
        "        print('CONNECTED', host)\n"
        "    except Exception as e:\n"
        "        print('DENIED', type(e).__name__)\n"
        "try:\n"
        "    import urllib.request\n"
        "    urllib.request.urlopen('http://169.254.169.254/', timeout=1)\n"
        "    print('CONNECTED metadata')\n"
        "except Exception as e:\n"
        "    print('DENIED', type(e).__name__)\n"
    )
    res, text = await _run(module, code)
    time.sleep(0.3)
    assert "CONNECTED" not in text
    assert hits == []
    refused, _ = await _run(module, "print(1)", allow_network=True)
    assert refused.error_code == "SECURITY_VIOLATION_NETWORK_POLICY"


# 4. Processes, fork bomb, ctypes and introspection escapes.


async def test_spawn_processes(backend: str, tmp_path: Path) -> None:
    flag = tmp_path / "spawned"
    module = _make(backend)
    code = (
        "attempts = [\n"
        f"    lambda: __import__('os').system('touch {flag}'),\n"
        f"    lambda: __import__('subprocess').run(['touch', {str(flag)!r}]),\n"
        "    lambda: __import__('os').posix_spawn(\n"
        f"        '/usr/bin/touch', ['touch', {str(flag)!r}], {{}}),\n"
        "]\n"
        "for a in attempts:\n"
        "    try:\n"
        "        a()\n"
        "    except Exception as e:\n"
        "        print('DENIED', type(e).__name__)\n"
    )
    res, _ = await _run(module, code)
    assert not flag.exists()


async def test_fork_bomb_is_bounded(backend: str) -> None:
    module = _make(backend, timeout_seconds=3.0, max_processes=1)
    start = time.monotonic()
    # Finite, non-recursive pressure: children exit immediately. Even a broken backend
    # cannot turn this test into an exponential host fork bomb.
    res, text = await _run(
        module,
        (
            "import os\n"
            "for i in range(8):\n"
            "    try:\n"
            "        pid = os.fork()\n"
            "    except OSError:\n"
            "        print('FORK_LIMIT')\n"
            "        break\n"
            "    if pid == 0:\n"
            "        os._exit(0)\n"
            "    os.waitpid(pid, 0)\n"
            "else:\n"
            "    print('FORKS_UNBOUNDED')\n"
        ),
    )
    assert time.monotonic() - start < 15
    assert "FORKS_UNBOUNDED" not in text
    if backend in ("bubblewrap", "gvisor_oci"):
        # OS and kernel sandboxes run real processes: fork must be refused inside the guest
        # (the guest catches the OSError, so the run itself may succeed).
        assert "FORK_LIMIT" in text
    else:
        assert res.status == "failure" or res.outputs["exit_code"] != 0
    assert module.worker.active_worker_count == 0


async def test_os_sandbox_disk_fill_is_bounded(os_backend: str) -> None:
    backend = os_backend
    module = _make(backend, timeout_seconds=3, bubblewrap_tmpfs_bytes=1_048_576)
    assert module.selected_isolation == "os_sandbox"
    res, text = await _run(
        module,
        (
            "import os\n"
            "assert os.statvfs('/tmp').f_blocks * os.statvfs('/tmp').f_frsize <= 1048576\n"
            "try:\n"
            "    with open('/tmp/fill', 'wb', buffering=0) as f:\n"
            "        for i in range(32):\n"
            "            f.write(b'x' * 65536)\n"
            "    print('DISK_UNBOUNDED')\n"
            "except OSError:\n"
            "    print('DISK_LIMIT')\n"
        ),
    )
    assert res.status == "success", text
    assert res.outputs["exit_code"] == 0, text
    assert "DISK_LIMIT" in text and "DISK_UNBOUNDED" not in text
    assert module.worker.active_worker_count == 0


async def test_backend_executes_and_reads_inputs(backend: str) -> None:
    module = _make(backend)
    res, text = await _run(
        module,
        ("print('RUN_OK', 6 * 7)\nprint(open('/sandbox/input.txt').read())\n"),
        files={"input.txt": "INPUT_OK"},
    )
    assert res.status == "success", text
    assert res.outputs["exit_code"] == 0, text
    assert "RUN_OK 42" in text and "INPUT_OK" in text


async def test_os_sandbox_inputs_and_runtime_are_read_only(os_backend: str) -> None:
    backend = os_backend
    module = _make(backend)
    assert module.selected_isolation == "os_sandbox"
    res, text = await _run(
        module,
        (
            "import os\n"
            "for p in ['/', '/usr', '/dev', '/proc']:\n"
            "    assert os.statvfs(p).f_flag & os.ST_RDONLY, p\n"
            "try:\n"
            "    open('/sandbox/input.txt', 'w').write('changed')\n"
            "    print('INPUT_WRITABLE')\n"
            "except OSError:\n"
            "    print('INPUT_READONLY')\n"
        ),
        files={"input.txt": "INPUT_OK"},
    )
    assert res.status == "success" and res.outputs["exit_code"] == 0, text
    assert "INPUT_READONLY" in text
    assert "INPUT_WRITABLE" not in text


async def test_cancel_leaves_no_worker(backend: str) -> None:
    module = _make(backend, timeout_seconds=3)
    task = asyncio.create_task(_run(module, "while True:\n    pass\n"))
    try:
        for _ in range(100):
            if module.worker.active_worker_count:
                break
            await asyncio.sleep(0.01)
        assert module.worker.active_worker_count > 0
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert module.worker.active_worker_count == 0


async def test_ctypes_and_introspection(backend: str, tmp_path: Path) -> None:
    flag = tmp_path / "pwned"
    module = _make(backend)
    code = (
        "tries = [\n"
        f"    lambda: __import__('ctypes').CDLL(None).system(b'touch {flag}'),\n"
        "    lambda: [c for c in ().__class__.__base__.__subclasses__()\n"
        "             if c.__name__ == '_wrap_close'][0]\n"
        f"        .__init__.__globals__['system']('touch {flag}'),\n"
        f"    lambda: __builtins__['__import__']('os').system('touch {flag}'),\n"
        "    lambda: __import__('sys')._getframe().f_back.f_globals['__builtins__'],\n"
        "    lambda: (lambda: 0).__globals__['__builtins__'],\n"
        "]\n"
        "for t in tries:\n"
        "    try:\n"
        "        t()\n"
        "    except Exception as e:\n"
        "        print('DENIED', type(e).__name__)\n"
    )
    res, _ = await _run(module, code)
    assert not flag.exists()


# 5. Resource limits.


async def test_memory_bomb(backend: str) -> None:
    module = _make(backend, max_memory_mb=64)
    res, text = await _run(module, "a = []\nwhile True:\n    a.append('x' * 10**7)\n")
    assert res.status == "failure" or res.outputs["exit_code"] != 0
    assert "SURVIVED" not in text


async def test_cpu_loop_times_out(backend: str) -> None:
    module = _make(backend, timeout_seconds=1.0)
    start = time.monotonic()
    res, _ = await _run(module, "while True:\n    pass\n")
    assert res.error_code == "EXECUTION_TIMEOUT"
    assert time.monotonic() - start < 10
    assert module.worker.active_worker_count == 0


async def test_huge_output_is_capped(backend: str) -> None:
    module = _make(backend, max_output_bytes=4096)
    res, _ = await _run(module, "while True:\n    print('x' * 4096)\n")
    assert res.error_code == "OUTPUT_LIMIT_EXCEEDED"
    assert module.worker.active_worker_count == 0


async def test_huge_input_files_refused_before_any_worker(backend: str) -> None:
    module = _make(backend, max_input_bytes=1024 * 1024, max_input_files=3)
    big, _ = await _run(module, "print(1)", files={"a.txt": "x" * (2 * 1024 * 1024)})
    assert big.error_code == "INPUT_LIMIT_EXCEEDED"
    many, _ = await _run(module, "print(1)", files={f"f{i}.txt": "x" for i in range(4)})
    assert many.error_code == "INPUT_LIMIT_EXCEEDED"


# 6. Forged error text never becomes a capability or security outcome.


async def test_forged_capability_error_text(backend: str) -> None:
    module = _make(backend)
    forged = (
        "Sandbox isolation capability unavailable: permission denied; operation not "
        "permitted; cannot create sandbox; unshare; ptrace; SECURITY_VIOLATION_FORK "
        "SECURITY_VIOLATION_NETWORK SECURITY_VIOLATION_PATH_ESCAPE"
    )
    code = f"print({forged!r})\nraise PermissionError({forged!r})\n"
    res, _ = await _run(module, code)
    _not_capability_or_violation(res)


# 7. Selection never picks a non-isolating worker.


def test_non_isolating_workers_are_never_selected() -> None:
    config = SandboxConfig(backends=["monty"], min_isolation="interpreter")
    with pytest.raises(Exception, match="never selected"):
        select_backend(config, [TrustedSubprocessWorker(), MockDisposableWorker()])
    assert TrustedSubprocessWorker.isolation == "none"
    assert MockDisposableWorker.isolation == "none"
