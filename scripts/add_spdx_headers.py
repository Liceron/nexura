#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Add or check the Apache-2.0 SPDX header on every first-party open-source source file.

Header (Python uses ``#``, TypeScript uses ``//``)::

    # SPDX-License-Identifier: Apache-2.0
    # Copyright (c) 2026 Liceron

The header goes on the first lines of the file, after a shebang and a PEP 263
encoding line when present. A module docstring stays a docstring because the
header is a comment above it, and ``from __future__`` imports stay valid.

Usage::

    python scripts/add_spdx_headers.py           # add missing headers
    python scripts/add_spdx_headers.py --check   # exit 1 if any file lacks it

The script is idempotent. A file that already carries a different
``SPDX-License-Identifier`` is reported and never rewritten.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Iterable
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SPDX_ID = "SPDX-License-Identifier: Apache-2.0"
COPYRIGHT = "Copyright (c) 2026 Liceron"

# (base directory, glob patterns, comment prefix)
TARGETS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("src", ("**/*.py",), "#"),
    ("packages/nexura-agents/src", ("**/*.py",), "#"),
    ("studio-web/src", ("**/*.ts", "**/*.tsx"), "//"),
    ("clients/typescript/src", ("**/*.ts",), "//"),
)

SKIP_DIRS = {"__pycache__", "node_modules", "dist", "build"}
ENCODING_RE = re.compile(r"^[ \t\f]*#.*?coding[:=][ \t]*[-\w.]+")
# How many leading lines may hold the header (after shebang/encoding lines).
HEADER_WINDOW = 6


def iter_files(root: Path = ROOT) -> Iterable[tuple[Path, str]]:
    for base, patterns, prefix in TARGETS:
        base_dir = root / base
        if not base_dir.is_dir():
            continue
        seen: set[Path] = set()
        for pattern in patterns:
            for path in sorted(base_dir.glob(pattern)):
                if path in seen or not path.is_file():
                    continue
                if SKIP_DIRS.intersection(path.relative_to(base_dir).parts):
                    continue
                seen.add(path)
                yield path, prefix


def _preamble_length(lines: list[str], prefix: str) -> int:
    """Number of leading lines that must stay before the header."""
    count = 0
    if lines and lines[0].startswith("#!"):
        count = 1
    if prefix == "#":
        # PEP 263: the encoding declaration must be on line 1 or 2.
        for idx in range(count, min(2, len(lines))):
            if ENCODING_RE.match(lines[idx]):
                count = idx + 1
                break
    return count


def header_lines(prefix: str) -> list[str]:
    return [f"{prefix} {SPDX_ID}\n", f"{prefix} {COPYRIGHT}\n"]


def inspect(text: str, prefix: str) -> str:
    """Return ``"ok"``, ``"missing"`` or ``"conflict"`` for one file's text."""
    lines = text.splitlines(keepends=True)
    start = _preamble_length(lines, prefix)
    window = [line.strip() for line in lines[start : start + HEADER_WINDOW]]
    spdx = [line for line in window if "SPDX-License-Identifier:" in line]
    if not spdx:
        return "missing"
    if spdx[0] != f"{prefix} {SPDX_ID}":
        return "conflict"
    if f"{prefix} {COPYRIGHT}" not in window:
        return "missing"
    return "ok"


def add_header(text: str, prefix: str) -> str:
    lines = text.splitlines(keepends=True)
    start = _preamble_length(lines, prefix)
    if start and not lines[start - 1].endswith("\n"):
        lines[start - 1] += "\n"
    window = lines[start : start + HEADER_WINDOW]
    has_spdx = any(line.strip() == f"{prefix} {SPDX_ID}" for line in window)
    if has_spdx:
        # SPDX present but copyright missing: add it right after the SPDX line.
        idx = next(i for i, line in enumerate(window) if line.strip() == f"{prefix} {SPDX_ID}")
        lines.insert(start + idx + 1, f"{prefix} {COPYRIGHT}\n")
        return "".join(lines)
    return "".join(lines[:start] + header_lines(prefix) + lines[start:])


def run(check: bool, root: Path = ROOT) -> int:
    total = changed = 0
    missing: list[str] = []
    conflicts: list[str] = []
    for path, prefix in iter_files(root):
        total += 1
        text = path.read_text(encoding="utf-8")
        status = inspect(text, prefix)
        rel = path.relative_to(root).as_posix()
        if status == "ok":
            continue
        if status == "conflict":
            conflicts.append(rel)
            continue
        missing.append(rel)
        if not check:
            new_text = add_header(text, prefix)
            path.write_text(new_text, encoding="utf-8")
            changed += 1
    for rel in conflicts:
        print(f"conflicting SPDX identifier (not changed): {rel}", file=sys.stderr)
    if check:
        for rel in missing:
            print(f"missing SPDX header: {rel}", file=sys.stderr)
        print(f"checked {total} files: {len(missing)} missing, {len(conflicts)} conflicting")
        return 1 if missing or conflicts else 0
    print(f"checked {total} files: {changed} updated, {len(conflicts)} conflicting")
    return 1 if conflicts else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument(
        "--check", action="store_true", help="report files without the header; exit 1 if any"
    )
    args = parser.parse_args(argv)
    return run(check=args.check)


if __name__ == "__main__":
    raise SystemExit(main())
