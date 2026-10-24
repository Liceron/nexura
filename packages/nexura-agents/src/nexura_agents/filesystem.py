# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Bounded, opt-in UTF-8 workspace tools. No shell or script execution."""

from __future__ import annotations

import builtins
import contextlib
import os
import stat
import threading
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TypeVar

from nexura_agents.errors import SDKValidationError
from nexura_agents.tools import Tool, tool

T = TypeVar("T")


def _parts(path: str) -> list[str]:
    if not isinstance(path, str) or not path or len(path) > 1024:
        raise SDKValidationError("Workspace path must be a non-empty relative path <= 1024 chars.")
    parts = path.split("/")
    if any(p in ("", ".", "..") for p in parts) or any(c in path for c in "\\:\x00"):
        raise SDKValidationError(
            "Workspace paths cannot be absolute or contain '.', '..', or '\\'."
        )
    if len(parts) > 16:
        raise SDKValidationError("Workspace paths may have at most 16 components.")
    return parts


class ScopedFilesystem:
    """Share one instance explicitly between parent and child agents.

    Without ``root`` files live in memory. With an existing ``root``, Linux Landlock
    ABI >= 3 and no-follow traversal enforce a directory capability. A private worker
    restricts only its own thread; unsupported disk isolation is refused.
    Files must be regular, singly linked UTF-8 files. Writes replace atomically, never
    follow links, and are conservatively non-idempotent for durable recovery.

    Caps cover UTF-8 bytes and directory entries. Use one instance per root to serialize
    cap checks. Landlock checks ancestry at opens/creates/renames, including both ends
    of replacements, even when a host actor moves a traversed directory out of the root.
    """

    def __init__(
        self,
        root: str | Path | None = None,
        *,
        max_file_bytes: int = 64 * 1024,
        max_total_bytes: int = 1024 * 1024,
        max_entries: int = 256,
    ) -> None:
        for value in (max_file_bytes, max_total_bytes, max_entries):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise SDKValidationError("Filesystem caps must be positive integers.")
        self.max_file_bytes = max_file_bytes
        self.max_total_bytes = max_total_bytes
        self.max_entries = max_entries
        self._files: dict[str, str] = {}
        self._lock = threading.RLock()
        self._root: int | None = None
        self._executor: ThreadPoolExecutor | None = None
        self._scope_id = "memory:" + uuid.uuid4().hex
        self._closed = False
        if root is not None:
            if not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd:
                raise SDKValidationError(
                    "Directory workspaces require POSIX no-follow dir_fd support; use memory mode."
                )
            absolute = Path(root).absolute()
            if ".." in absolute.parts:
                raise SDKValidationError("Workspace root cannot contain '..'.")
            fd = os.open(absolute.anchor, os.O_RDONLY | os.O_DIRECTORY)
            try:
                for part in absolute.parts[1:]:
                    child = os.open(part, self._dir_flags, dir_fd=fd)
                    os.close(fd)
                    fd = child
            except BaseException:
                os.close(fd)
                raise
            self._root = fd
            info = os.fstat(fd)
            self._scope_id = f"directory:{absolute}:{info.st_dev}:{info.st_ino}"
            from nexura_agents._filesystem_sandbox import restrict_worker

            self._executor = ThreadPoolExecutor(max_workers=1)
            try:
                # Restriction is installed on this dedicated thread before any work.
                self._executor.submit(restrict_worker, fd).result()
            except BaseException:
                self.close()
                raise

    @property
    def scope_id(self) -> str:
        """Stable directory identity, or unique memory-instance identity, for approvals."""
        return self._scope_id

    def _disk_call(self, operation: Callable[[], T]) -> T:
        assert self._executor is not None
        return self._executor.submit(operation).result()

    @property
    def _dir_flags(self) -> int:
        return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW

    def close(self) -> None:
        """Release the directory descriptor. Subsequent operations are refused."""
        with self._lock:
            if self._executor is not None:
                self._executor.shutdown(wait=True)
                self._executor = None
            if self._root is not None:
                os.close(self._root)
                self._root = None
            self._closed = True

    def __enter__(self) -> ScopedFilesystem:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _check_open(self) -> None:
        if self._closed:
            raise SDKValidationError("Workspace is closed.")

    @contextlib.contextmanager
    def _parent(self, parts: list[str], *, create: bool = False) -> Iterator[int]:
        assert self._root is not None
        fd = os.dup(self._root)
        try:
            for part in parts[:-1]:
                if create:

                    def mkdir(name: str = part, directory: int = fd) -> None:
                        os.mkdir(name, dir_fd=directory)

                    with contextlib.suppress(FileExistsError):
                        self._disk_call(mkdir)
                child = os.open(part, self._dir_flags, dir_fd=fd)
                os.close(fd)
                fd = child
            yield fd
        finally:
            os.close(fd)

    def _check_parent(self, parent: int, parts: list[str]) -> None:
        """Re-resolve without symlinks and reject a moved/replaced directory.

        This detects target drift. Landlock separately enforces ancestry at each
        final kernel operation, including both ends of the atomic replacement.
        """
        assert self._root is not None
        fd = os.dup(self._root)
        try:
            for part in parts[:-1]:
                child = os.open(part, self._dir_flags, dir_fd=fd)
                os.close(fd)
                fd = child
            expected, actual = os.fstat(fd), os.fstat(parent)
            if (expected.st_dev, expected.st_ino) != (actual.st_dev, actual.st_ino):
                raise SDKValidationError("Workspace parent moved or changed during the operation.")
        finally:
            os.close(fd)

    def _inventory(self) -> tuple[dict[str, int], set[str]]:
        if self._root is None:
            memory_entries = {
                "/".join(_parts(p)[:i]) for p in self._files for i in range(1, len(_parts(p)) + 1)
            }
            return {p: len(t.encode("utf-8")) for p, t in self._files.items()}, memory_entries
        sizes: dict[str, int] = {}
        entries: set[str] = set()

        def scan(fd: int, prefix: str, depth: int) -> None:
            if depth > 16:
                raise SDKValidationError("Workspace directory depth exceeded.")
            components = prefix.rstrip("/").split("/") if prefix else []
            self._check_parent(fd, [*components, ".inventory"])
            with os.scandir(fd) as listing:
                for entry in listing:
                    path = prefix + entry.name
                    _parts(path)
                    entries.add(path)
                    if len(entries) > self.max_entries:
                        raise SDKValidationError("Workspace entry cap exceeded.")
                    info = entry.stat(follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode):
                        child = os.open(entry.name, self._dir_flags, dir_fd=fd)
                        try:
                            scan(child, path + "/", depth + 1)
                        finally:
                            os.close(child)
                    elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                        sizes[path] = info.st_size
                    else:
                        raise SDKValidationError(
                            "Workspace refuses symlinks, hard links and special files."
                        )
            self._check_parent(fd, [*components, ".inventory"])

        root = self._root
        self._disk_call(lambda: scan(root, "", 0))
        return sizes, entries

    def _check_caps(self, sizes: dict[str, int], entries: set[str]) -> None:
        if (
            len(entries) > self.max_entries
            or sum(sizes.values()) > self.max_total_bytes
            or any(size > self.max_file_bytes for size in sizes.values())
        ):
            raise SDKValidationError("Workspace size or entry cap exceeded.")

    def list(self) -> list[str]:
        """List file paths, sorted, relative to the workspace."""
        with self._lock:
            self._check_open()
            sizes, entries = self._inventory()
            self._check_caps(sizes, entries)
            return sorted(sizes)

    def read(self, path: str) -> str:
        """Read one bounded UTF-8 file; never follow a symlink."""
        parts = _parts(path)
        with self._lock:
            self._check_open()
            if self._root is None:
                return self._files[path]
            with self._parent(parts) as parent:

                def read() -> bytes:
                    self._check_parent(parent, parts)
                    fd = os.open(
                        parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
                    )
                    with os.fdopen(fd, "rb") as stream:
                        info = os.fstat(stream.fileno())
                        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                            raise SDKValidationError(
                                "Workspace reads require regular, singly linked files."
                            )
                        value = stream.read(self.max_file_bytes + 1)
                    self._check_parent(parent, parts)
                    return value

                data = self._disk_call(read)
            if len(data) > self.max_file_bytes:
                raise SDKValidationError("Workspace file size cap exceeded.")
            return data.decode("utf-8")

    def write(self, path: str, content: str) -> str:
        """Replace a file, creating parent directories within the configured caps."""
        parts = _parts(path)
        data = content.encode("utf-8")
        with self._lock:
            self._check_open()
            sizes, entries = self._inventory()
            if any("/".join(parts[:i]) in sizes for i in range(1, len(parts))):
                raise SDKValidationError("A file cannot be used as a directory.")
            if path in entries and path not in sizes:
                raise SDKValidationError("A directory cannot be replaced by a file.")
            sizes[path] = len(data)
            entries.update("/".join(parts[:i]) for i in range(1, len(parts) + 1))
            self._check_caps(sizes, entries)
            if self._root is None:
                self._files[path] = content
            else:
                with self._parent(parts, create=True) as parent:

                    def write() -> None:
                        self._check_parent(parent, parts)
                        temporary = ".nexura-" + uuid.uuid4().hex
                        fd = os.open(
                            temporary,
                            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                            0o600,
                            dir_fd=parent,
                        )
                        try:
                            with os.fdopen(fd, "wb") as stream:
                                stream.write(data)
                                stream.flush()
                                os.fsync(stream.fileno())
                            self._check_parent(parent, parts)
                            os.replace(temporary, parts[-1], src_dir_fd=parent, dst_dir_fd=parent)
                            os.fsync(parent)
                        finally:
                            with contextlib.suppress(FileNotFoundError):
                                os.unlink(temporary, dir_fd=parent)

                    self._disk_call(write)
            return path

    def edit(self, path: str, old: str, new: str) -> str:
        """Replace exactly one literal match. Refuse empty or ambiguous matches."""
        with self._lock:
            content = self.read(path)
            if not old or content.count(old) != 1:
                raise SDKValidationError("Edit requires exactly one non-empty literal match.")
            return self.write(path, content.replace(old, new, 1))

    def build_tools(self, *, require_write_approval: bool = False) -> builtins.list[Tool]:
        """Tools use the normal runner approval, hook and guardrail pipeline."""

        @tool(effect="pure")
        def fs_read(path: str) -> str:
            """Read a UTF-8 file in the scoped workspace."""
            return self.read(path)

        @tool(effect="pure")
        def fs_list() -> builtins.list[str]:
            """List files in the scoped workspace."""
            return self.list()

        @tool(
            effect="non_idempotent",
            requires_approval=require_write_approval,
        )
        def fs_write(path: str, content: str) -> str:
            """Replace a workspace file with UTF-8 text."""
            return self.write(path, content)

        @tool(
            effect="non_idempotent",
            requires_approval=require_write_approval,
        )
        def fs_edit(path: str, old: str, new: str) -> str:
            """Replace exactly one literal match in a workspace file."""
            return self.edit(path, old, new)

        tools = [fs_read, fs_list, fs_write, fs_edit]
        for item in tools:
            item.origin = "harness:filesystem"
            item.approval_scope = self.scope_id
            item.approval_scope_per_run = True
        return tools
