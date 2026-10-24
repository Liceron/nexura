# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Portable, bounded read-only snapshots of operator-controlled skill folders.

No workspace tools or execution capabilities are involved. Concurrent directory
renames by a local actor controlling the folder are outside this loader's boundary.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from nexura_agents.errors import SDKValidationError

MAX_ENTRIES = 256
MAX_DEPTH = 16


def _plain_info(info: os.stat_result) -> None:
    if stat.S_ISLNK(info.st_mode) or (
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    ):
        raise SDKValidationError("Skill directories refuse symlinks, junctions and reparse points.")
    if not stat.S_ISDIR(info.st_mode) and not (stat.S_ISREG(info.st_mode) and info.st_nlink == 1):
        raise SDKValidationError("Skills require ordinary directories and singly linked files.")


def checked_directory(path: Path) -> Path:
    """Check every component with lstat, without resolving links or Windows junctions."""
    absolute = path.absolute()
    if ".." in absolute.parts:
        raise SDKValidationError("Skill directory paths cannot contain '..'.")
    current = Path(absolute.anchor)
    for part in ("", *absolute.parts[1:]):
        if part:
            current /= part
        info = os.lstat(current)
        _plain_info(info)
        if not stat.S_ISDIR(info.st_mode):
            raise SDKValidationError("A skill directory component is not a directory.")
    return absolute


def directory_entries(path: Path) -> list[tuple[Path, os.stat_result]]:
    """Bound an immediate scandir listing and refuse links/special files before use."""
    directory = checked_directory(path)
    entries: list[tuple[Path, os.stat_result]] = []
    with os.scandir(directory) as listing:
        for entry in listing:
            if len(entries) >= MAX_ENTRIES:
                raise SDKValidationError("Skill directories may contain at most 256 entries.")
            child = Path(entry.path)
            info = os.lstat(child)
            _plain_info(info)
            entries.append((child, info))
    return sorted(entries, key=lambda item: item[0].name)


def _read_file(path: Path, expected: os.stat_result, limit: int) -> bytes:
    checked_directory(path.parent)
    current = os.lstat(path)
    _plain_info(current)
    if (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino):
        raise SDKValidationError("Skill file changed during discovery.")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as stream:
        opened = os.fstat(stream.fileno())
        _plain_info(opened)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
            current.st_dev,
            current.st_ino,
        ):
            raise SDKValidationError("Opened skill file differs from its lstat identity.")
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise SDKValidationError("Skill file size cap exceeded.")
    return data


def snapshot_skill(
    root: Path, *, file_limit: int, text_suffixes: tuple[str, ...]
) -> dict[str, str]:
    """Inventory all entries and snapshot only SKILL.md and allowed text resources."""
    root = checked_directory(root)
    inventory: dict[str, tuple[Path, os.stat_result]] = {}
    count, total = 0, 0
    total_limit = file_limit * 4

    def walk(directory: Path, prefix: str, depth: int) -> None:
        nonlocal count, total
        if depth > MAX_DEPTH:
            raise SDKValidationError("Skill directory depth cap exceeded.")
        for path, info in directory_entries(directory):
            count += 1
            relative = prefix + path.name
            if count > MAX_ENTRIES:
                raise SDKValidationError("A skill may contain at most 256 entries.")
            if len(relative) > 1024 or len(relative.split("/")) > MAX_DEPTH:
                raise SDKValidationError("Skill resource path length or depth cap exceeded.")
            if any(c in relative for c in "\\:\x00"):
                raise SDKValidationError("Invalid skill resource path.")
            if stat.S_ISDIR(info.st_mode):
                walk(path, relative + "/", depth + 1)
            else:
                total += info.st_size
                if info.st_size > file_limit or total > total_limit:
                    raise SDKValidationError("Skill size cap exceeded.")
                inventory[relative] = path, info

    walk(root, "", 0)
    if "SKILL.md" not in inventory:
        raise SDKValidationError("Skill directory has no regular SKILL.md.")
    result: dict[str, str] = {}
    for name, (path, info) in sorted(inventory.items()):
        if name == "SKILL.md" or (
            not name.startswith("scripts/") and name.lower().endswith(text_suffixes)
        ):
            data = _read_file(path, info, file_limit)
            total += len(data) - info.st_size
            if total > total_limit:
                raise SDKValidationError("Skill total size cap exceeded during loading.")
            result[name] = data.decode("utf-8")
    return result
