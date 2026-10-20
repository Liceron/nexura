# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Write the public open-source repository tree from an explicit allow-list.

The export copies only tracked files (``git ls-files``) that match ``ALLOW``, so internal
planning reports, local data and the closed edition can never leak by being forgotten in a
deny-list (EDITIONS_PLAN section 2.4, check 6). It then refuses the result if any exported
file is closed code: a path under ``enterprise/``, the ``nexura_enterprise`` package, or the
proprietary SPDX tag.

    uv run python scripts/export_oss_tree.py OUT_DIR            # export and check
    uv run python scripts/export_oss_tree.py OUT_DIR --list     # print what would be exported
"""

from __future__ import annotations

import argparse
import fnmatch
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Everything the public repository contains. Patterns are matched against tracked paths
# relative to the project root; a trailing "/**" means "everything under this directory".
ALLOW: tuple[str, ...] = (
    # Packages, tests, examples and docs
    "src/**",
    "packages/**",
    "tests/**",
    "examples/**",
    "docs/**",
    "scripts/add_spdx_headers.py",
    "scripts/gen_api_reference.py",
    "scripts/test_agents_wheel.py",
    "scripts/export_oss_tree.py",
    "scripts/generate_sbom.py",
    # Clients and Studio (sources and their own configs only; no build output)
    "clients/typescript/**",
    "studio-web/LICENSE",
    "studio-web/index.html",
    "studio-web/package.json",
    "studio-web/package-lock.json",
    "studio-web/eslint.config.js",
    "studio-web/playwright.config.ts",
    "studio-web/tsconfig.json",
    "studio-web/vite.config.ts",
    "studio-web/vitest.config.ts",
    "studio-web/public/**",
    "studio-web/src/**",
    "studio-web/tests/**",
    # Release gate, benchmark and single-user deployment
    "experiments/release-gate/**",
    "experiments/benchmark/**",
    "deployment/**",
    "packaging/**",
    # Project files
    ".github/**",
    ".gitignore",
    ".dockerignore",
    "pyproject.toml",
    "uv.lock",
    "README.md",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "CODE_OF_CONDUCT.md",
    "SECURITY.md",
    "SECURITY_MODEL.md",
    "THIRD_PARTY.md",
    "LICENSE",
    "CLA.md",
)

# Never exported even if an allow pattern matches (generated output and local data).
NEVER: tuple[str, ...] = (
    "**/__pycache__/**",
    "**/*.pyc",
    "**/*.db",
    "**/*.sqlite3",
    "**/*.key",
    "**/.env*",
    "experiments/benchmark/.venv/**",
    "studio-web/dist/**",
    "studio-web/node_modules/**",
    "studio-web/test-results/**",
)

# Keep the scanned signatures out of this exported checker's own source text.
# Adjacent literals preserve the exact marker values and the check for every file.
# Built from parts so this script, which is itself exported, does not contain the markers.
CLOSED_MARKERS: tuple[str, ...] = (
    "-".join(("LicenseRef", "Liceron", "Proprietary")),
    "_".join(("NEXURA", "ENTERPRISE", "BUILD")),
)


def _matches(path: str, patterns: tuple[str, ...]) -> bool:
    for pattern in patterns:
        if pattern.endswith("/**") and path.startswith(pattern[:-3] + "/"):
            return True
        if fnmatch.fnmatchcase(path, pattern):
            return True
    return False


def tracked_files(root: Path = ROOT) -> list[str]:
    """Tracked paths of the outer repository (the nested enterprise repo is never listed)."""
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, check=True, capture_output=True
    ).stdout.decode("utf-8")
    return sorted(p for p in out.split("\0") if p)


def select(paths: list[str]) -> list[str]:
    """The paths the public tree contains."""
    return [p for p in paths if _matches(p, ALLOW) and not _matches(p, NEVER)]


def closed_code_problems(root: Path, paths: list[str]) -> list[str]:
    """Reasons an exported tree is not open-source only (empty when it is clean)."""
    problems = []
    for rel in paths:
        if rel.startswith("enterprise/") or "nexura_enterprise" in rel:
            problems.append(f"{rel}: closed-edition path")
            continue
        try:
            text = (root / rel).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for marker in CLOSED_MARKERS:
            if marker in text:
                problems.append(f"{rel}: contains {marker}")
    return problems


def export(out: Path, root: Path = ROOT) -> list[str]:
    """Copy the selected files to ``out`` (which must not exist or be empty)."""
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"{out} exists and is not empty")
    paths = select(tracked_files(root))
    for rel in paths:
        dest = out / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / rel, dest)
    return paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("out", type=Path)
    parser.add_argument("--list", action="store_true", help="print the selection and stop")
    args = parser.parse_args(argv)
    if args.list:
        print("\n".join(select(tracked_files())))
        return 0
    paths = export(args.out)
    problems = closed_code_problems(args.out, paths)
    for problem in problems:
        print(f"closed code in export: {problem}", file=sys.stderr)
    print(f"exported {len(paths)} files to {args.out}; {len(problems)} problems")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
