# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Read-only virtual input files for the Monty backend (imports ``pydantic_monty``).

Imported only when a Monty run has input files, so ``nexura_runtime`` itself never needs
the optional ``sandbox`` extra. Contents live in this process's memory; the sandboxed code
can read them and list ``/sandbox`` but every write, append, mkdir, unlink, rmdir or rename
is refused, so code cannot grow host memory through the virtual filesystem. No real file,
directory, environment variable or host callback is reachable through it.
"""

from __future__ import annotations

from pathlib import PurePosixPath

from pydantic_monty import MemoryFile, MontyFileHandle, OSAccess


def _refuse(path: object) -> PermissionError:
    return PermissionError(
        f"[Errno 13] Permission denied: '{path}' (sandbox input files are read-only)"
    )


class ReadOnlyInputFiles(OSAccess):
    """``OSAccess`` over in-memory input files with every mutating call refused."""

    def __init__(self, files: list[tuple[str, str]], *, root: str) -> None:
        super().__init__(
            [MemoryFile(PurePosixPath(root) / rel, content) for rel, content in files],
            environ={},
            root_dir=root,
            max_urandom_bytes=0,
            max_sleep=0,
        )

    def path_open(self, path: PurePosixPath, mode: str) -> MontyFileHandle:
        canonical = MontyFileHandle(str(path), mode).mode  # 'rt' -> 'r', 'r+b' -> 'rb+'
        if not canonical.startswith("r") or "+" in canonical:
            raise _refuse(path)
        return super().path_open(path, mode)

    def path_write_text(self, path: PurePosixPath | MontyFileHandle, data: str) -> int:
        raise _refuse(path)

    def path_write_bytes(self, path: PurePosixPath | MontyFileHandle, data: bytes) -> int:
        raise _refuse(path)

    def path_append_text(self, path: PurePosixPath | MontyFileHandle, data: str) -> int:
        raise _refuse(path)

    def path_append_bytes(self, path: PurePosixPath | MontyFileHandle, data: bytes) -> int:
        raise _refuse(path)

    def path_mkdir(self, path: PurePosixPath, parents: bool, exist_ok: bool) -> None:
        raise _refuse(path)

    def path_unlink(self, path: PurePosixPath) -> None:
        raise _refuse(path)

    def path_rmdir(self, path: PurePosixPath) -> None:
        raise _refuse(path)

    def path_rename(self, path: PurePosixPath, target: PurePosixPath) -> None:
        raise _refuse(path)

    def getenv(self, key: str, default: str | None = None) -> str | None:
        return default

    def get_environ(self) -> dict[str, str]:
        return {}
