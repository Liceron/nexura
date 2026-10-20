# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Explicit, offline installation of the pinned operator-owned CPython WASI runtime."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

RUNTIME_VERSION = "3.13.15"
WASMTIME_VERSION = "49.0.0"
# Operator release source; choose the CPython 3.13.15 archive matching the hash below.
# This command never fetches the page, nor guesses a release asset filename.
RUNTIME_URL = "https://github.com/brettcannon/cpython-wasi-build/releases"
RUNTIME_SHA256 = "67e1c32a85d5e0600c0939f5fd4023ca0127f1a9e81f5499697467bda67af78d"


def runtime_path(configured: str | None = None) -> Path:
    """Resolve only an explicit operator path or the fixed per-user runtime location."""
    if configured:
        path = Path(configured)
        if not path.is_absolute():
            raise ValueError("wasi_runtime_path must be absolute")
        return path
    return Path.home() / ".local" / "share" / "nexura" / "runtimes" / "cpython-wasi-3.13.15"


def file_sha256(path: Path) -> str:
    """Hash without loading a runtime or compiled artifact into service memory."""
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify_runtime(path: Path) -> None:
    """Check the installation marker and all pinned runtime files before use.

    The operator owns this directory, including the native serialized-code cache. Like a
    Python installation it must never be writable by untrusted tenants or guest code.
    """
    manifest = json.loads((path / "runtime.json").read_text())
    if manifest["archive_sha256"] != RUNTIME_SHA256:
        raise ValueError("WASI runtime pin mismatch; reinstall from the pinned archive")
    files = manifest["files"]
    if "python.wasm" not in files or "lib/python3.13/encodings/__init__.py" not in files:
        raise ValueError("incomplete CPython WASI runtime")
    for name, digest in files.items():
        relative = PurePosixPath(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("invalid runtime manifest path")
        candidate = path / name
        if candidate.is_symlink() or file_sha256(candidate) != digest:
            raise ValueError(f"WASI runtime file changed: {name}")


def install_runtime(archive: Path, destination: Path) -> Path:
    """Verify, extract and compile a local ZIP; never download anything.

    Installs atomically into a new directory. Reinstallation into an existing directory
    is refused so an active runtime/cache cannot be replaced underneath a worker.
    """
    if file_sha256(archive) != RUNTIME_SHA256:
        raise ValueError(f"WASI runtime SHA256 mismatch; expected {RUNTIME_SHA256}")
    destination = destination.absolute()
    if destination.exists():
        raise ValueError(f"runtime destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".wasi-install-", dir=destination.parent) as temporary:
        staging = Path(temporary) / "runtime"
        staging.mkdir(mode=0o700)
        files: dict[str, str] = {}
        with zipfile.ZipFile(archive) as zipped:
            expanded = 0
            for entry in zipped.infolist():
                relative = PurePosixPath(entry.filename)
                mode = entry.external_attr >> 16
                expanded += entry.file_size
                if (
                    relative.is_absolute()
                    or ".." in relative.parts
                    or "\\" in entry.filename
                    or stat.S_ISLNK(mode)
                    or expanded > 100 * 1024 * 1024
                ):
                    raise ValueError("unsafe WASI runtime ZIP entry")
                target = staging / relative
                if entry.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with zipped.open(entry) as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output)
                files[str(relative)] = file_sha256(target)
        (staging / "runtime.json").write_text(
            json.dumps({"archive_sha256": RUNTIME_SHA256, "files": files}), encoding="utf-8"
        )
        verify_runtime(staging)
        # Compilation is native code and happens in a bounded child, never the service.
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                str(Path(__file__).with_name("wasi_runner.py")),
                "--compile",
                str(staging),
            ],
            env={},
            cwd=staging,
            capture_output=True,
            timeout=120,
            check=False,
        )
        if result.returncode:
            raise ValueError(
                "WASI compilation failed: " + result.stderr.decode(errors="replace")[:2048]
            )
        os.rename(staging, destination)
    return destination
