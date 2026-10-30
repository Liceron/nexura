# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Private one-shot WASI child. Only stdlib and optional wasmtime are imported here.

The control channel is the host stdin/stdout; neither is inherited by WASI. Guest output
uses bounded host callbacks. The serialized native module is trusted operator data, never
an artifact supplied by a guest. Each process owns one Engine and one fresh Store.
"""

from __future__ import annotations

import hashlib
import json
import os
import struct
import sys
import threading
import time
from pathlib import Path
from typing import Any

import wasmtime


def engine_and_module(root: Path, *, compile_only: bool = False) -> tuple[Any, Any]:
    """Compile once at install; deserialize only our operator-owned, checked cache."""
    config = wasmtime.Config()
    config.consume_fuel = True
    config.epoch_interruption = True
    engine = wasmtime.Engine(config)
    cache = root / "wasmtime-49.0.0.cwasm"
    checksum = root / "wasmtime-49.0.0.cwasm.sha256"
    if compile_only:
        module = wasmtime.Module.from_file(engine, str(root / "python.wasm"))
        data = module.serialize()
        cache.write_bytes(data)
        checksum.write_text(hashlib.sha256(data).hexdigest())
    else:
        # deserialize_file is unsafe for arbitrary bytes. Hash before handing to native API.
        with cache.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if checksum.read_text() != digest:
            raise ValueError("compiled WASI cache checksum mismatch; reinstall runtime")
        module = wasmtime.Module.deserialize_file(engine, str(cache))
    return engine, module


def execute(payload: dict[str, Any]) -> dict[str, Any]:
    root = Path(payload["runtime"])
    work = Path(payload["work"])
    config = payload["config"]
    request = payload["request"]
    engine, module = engine_and_module(root)
    store = wasmtime.Store(engine)
    store.set_limits(memory_size=config["max_memory_mb"] * 1024 * 1024)
    store.set_fuel(config["wasi_fuel"])
    store.set_epoch_deadline(1)
    wasi = wasmtime.WasiConfig()
    # -I ignores Python environment and user paths; -S avoids site initialization.
    bootstrap = (
        "import os,sys; "
        # CPython's C-locale coercion may synthesize LC_CTYPE at startup. Restore
        # precisely the configured guest environment before evaluating user code.
        f"os.environ.clear(); os.environ.update({request['env']!r}); "
        f"sys.setrecursionlimit({config['wasi_recursion_limit']}); "
        "os.chdir('/sandbox'); "
        "exec(compile(open('/sandbox/__nexura_code__.py').read(), '<sandbox>', 'exec'))"
    )
    wasi.argv = ["python", "-I", "-S", "-u", "-c", bootstrap]
    wasi.env = list(request["env"].items())
    wasi.preopen_dir(str(root / "lib"), "/lib", fs_mutable=False)
    wasi.preopen_dir(str(work / "inputs"), "/sandbox", fs_mutable=False)
    if config["wasi_scratch_enabled"]:
        wasi.preopen_dir(str(work / "scratch"), "/scratch", fs_mutable=True)
    wasi.stdin_file = str(work / "stdin")
    output = {"stdout": bytearray(), "stderr": bytearray()}
    outcome: str | None = None
    total = 0

    def write(stream: str, data: bytes) -> int:
        nonlocal outcome, total
        room = max(0, config["max_output_bytes"] - total)
        output[stream].extend(data[:room])
        total += min(len(data), room)
        if len(data) > room:
            outcome = "output"
            engine.increment_epoch()
            return -29  # WASI I/O error; a trusted flag survives any guest exception handling.
        return len(data)

    wasi.stdout_custom = lambda data: write("stdout", data)
    wasi.stderr_custom = lambda data: write("stderr", data)
    store.set_wasi(wasi)
    linker = wasmtime.Linker(engine)
    linker.define_wasi()
    linker.allow_shadowing = True

    # Enforce the writable capability at the WASI syscall boundary, not Python builtins.
    # Count cumulative requested write bytes (overwrites consume quota too). Deny sparse
    # allocation/resize so a seek/truncate cannot bypass the byte budget. Cap directory
    # entries before creation; guest code is single-threaded so no guest race is possible.
    written = 0
    writable_fds: set[int] = set()

    def wrap(name: str) -> None:
        found = linker.get(store, "wasi_snapshot_preview1", name)
        assert isinstance(found, wasmtime.Func)
        original: wasmtime.Func = found
        signature = original.type(store)
        parameters = " ".join(str(item) for item in signature.params)
        results = " ".join(str(item) for item in signature.results)
        arguments = " ".join(f"(local.get {index})" for index in range(len(signature.params)))
        # Calling a built-in WASI Func directly from Python loses the Wasm caller's
        # memory export. Reexport the same guest memory through a Wasm forwarder,
        # preserving Wasmtime's capability, rights and pointer checks.
        adapter = wasmtime.Module(
            engine,
            f"""
            (module
              (import "host" "original" (func $original (param {parameters}) (result {results})))
              (import "guest" "memory" (memory 0))
              (export "memory" (memory 0))
              (func (export "forward") (param {parameters}) (result {results})
                (call $original {arguments})))
        """,
        )
        forward: Any = None

        def guarded(caller: Any, *args: int) -> int:
            nonlocal written, outcome, forward
            if name in ("fd_allocate", "fd_filestat_set_size"):
                return 58  # ENOTSUP; prevents sparse-file quota bypass.
            if name == "fd_renumber":
                return 58  # Cannot replace stdout/stderr or evade writable-fd tracking.
            if name == "fd_seek" and args[0] in writable_fds and (args[1] != 0 or args[2] != 1):
                return 58  # Writable files are sequential; prohibit sparse seeks.
            if name in ("path_open", "path_create_directory", "path_symlink", "path_link"):
                count = sum(1 for _ in (work / "scratch").rglob("*"))
                if count >= config["wasi_scratch_files"]:
                    outcome = "scratch"
                    raise wasmtime.Trap("scratch entry quota exceeded")
            if name in ("fd_write", "fd_pwrite") and args[0] > 2:
                memory = caller.get("memory")
                assert isinstance(memory, wasmtime.Memory)
                start, count = args[1] & 0xFFFFFFFF, args[2] & 0xFFFFFFFF
                # WASI validates pointers too, but never allocate a guest-sized iovec copy.
                if start + count * 8 > memory.data_len(caller):
                    return 21  # EFAULT
                size = 0
                for offset in range(start, start + count * 8, 8):
                    size += struct.unpack("<II", memory.read(caller, offset, offset + 8))[1]
                    logical_end = size + (args[3] if name == "fd_pwrite" else 0)
                    if max(size + written, logical_end) > config["wasi_scratch_bytes"]:
                        outcome = "scratch"
                        raise wasmtime.Trap("scratch byte quota exceeded")
                written += size
            if forward is None:
                memory = caller.get("memory")
                assert isinstance(memory, wasmtime.Memory)
                bridge = wasmtime.Instance(caller, adapter, [original, memory])
                forward = bridge.exports(caller)["forward"]
            errno = int(forward(caller, *args))
            if errno == 0 and name == "path_open" and args[5] & 64:  # WASI FD_WRITE
                memory = caller.get("memory")
                assert isinstance(memory, wasmtime.Memory)
                writable_fds.add(struct.unpack("<I", memory.read(caller, args[8], args[8] + 4))[0])
            if errno == 0 and name == "fd_close":
                writable_fds.discard(args[0])
            return errno

        linker.define_func(
            "wasi_snapshot_preview1", name, original.type(store), guarded, access_caller=True
        )

    if config["wasi_scratch_enabled"]:
        for name in (
            "fd_write",
            "fd_pwrite",
            "fd_allocate",
            "fd_filestat_set_size",
            "fd_renumber",
            "fd_seek",
            "fd_close",
            "path_open",
            "path_create_directory",
            "path_symlink",
            "path_link",
        ):
            wrap(name)
    timer = threading.Timer(config["timeout_seconds"], engine.increment_epoch)
    timer.start()
    started = time.monotonic()
    exit_code = 0
    try:
        instance = linker.instantiate(store, module)
        entrypoint = instance.exports(store)["_start"]
        assert isinstance(entrypoint, wasmtime.Func)
        entrypoint(store)
    except wasmtime.ExitTrap as exc:
        exit_code = exc.code
    except wasmtime.Trap as exc:
        exit_code = 1
        if outcome is None:
            if exc.trap_code in (wasmtime.TrapCode.OUT_OF_FUEL, wasmtime.TrapCode.INTERRUPT):
                outcome = "timeout"
            elif exc.trap_code in (
                wasmtime.TrapCode.MEMORY_OUT_OF_BOUNDS,
                wasmtime.TrapCode.STACK_OVERFLOW,
            ):
                outcome = "memory"
    finally:
        timer.cancel()
        timer.join()
        # Release WASI callbacks while Python is still running, before native runtime
        # background threads encounter Python's interpreter finalization.
        store.close()
        linker.close()
        module.close()
        engine.close()
    # A CPython MemoryError is a normal guest exception, not a native trap. Do not classify
    # guest-written lookalike text as trusted resource/security failures.
    return {
        "exit_code": exit_code,
        "outcome": outcome,
        "stdout": output["stdout"].decode("utf-8", errors="ignore"),
        "stderr": output["stderr"].decode("utf-8", errors="ignore"),
        "duration_ms": (time.monotonic() - started) * 1000,
    }


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--compile":
        engine_and_module(Path(sys.argv[2]), compile_only=True)
        return 0
    try:
        result = execute(json.load(sys.stdin))
    except Exception as exc:
        result = {"outcome": "crash", "error": str(exc)[:1024]}
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    # WASI custom-output finalizers can run on native Tokio threads after a trap.
    # Python interpreter teardown races those callbacks in wasmtime-py 49.0.0.
    # This is a disposable process: exit after the flushed control reply, leaving
    # remaining native thread cleanup to the OS instead of finalizing Python.
    os._exit(code)
