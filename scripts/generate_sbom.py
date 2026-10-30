# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Generate a deterministic CycloneDX inventory offline from uv.lock.

Includes all published workspace packages, all their declared extras and their
transitive locked dependencies. Marker branches are unioned across platforms;
this is a release inventory, not an inventory of one installed environment.
Development groups, npm and OS dependencies are outside this scope. No license
claims are inferred from names or from a different installed package version.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def generate(root: Path = ROOT) -> dict[str, Any]:
    lock_bytes = (root / "uv.lock").read_bytes()
    lock = tomllib.loads(lock_bytes.decode())
    packages = lock["package"]
    members = set(lock["manifest"]["members"])
    by_name: dict[str, list[dict[str, Any]]] = {}
    for package in packages:
        by_name.setdefault(package["name"], []).append(package)

    selected: dict[tuple[str, str], dict[str, Any]] = {}
    edges: dict[str, set[str]] = {}
    visited: set[tuple[str, str, tuple[str, ...]]] = set()

    def visit(edge: dict[str, Any]) -> None:
        candidates = by_name[edge["name"]]
        if "version" in edge:
            candidates = [p for p in candidates if p["version"] == edge["version"]]
        if not candidates:
            raise ValueError(f"Missing locked dependency: {edge}")
        for package in candidates:
            name, version = package["name"], package["version"]
            optional = package.get("optional-dependencies", {})
            extras = tuple(sorted(optional if name in members else edge.get("extra", [])))
            key = (name, version, extras)
            if key in visited:
                continue
            visited.add(key)
            selected[name, version] = package
            dependencies = list(package.get("dependencies", []))
            for extra in extras:
                dependencies.extend(optional.get(extra, []))
            ref = f"pkg:pypi/{name}@{version}"
            targets = edges.setdefault(ref, set())
            for dependency in dependencies:
                visit(dependency)
                targets.update(
                    f"pkg:pypi/{n}@{v}"
                    for n, v in selected
                    if n == dependency["name"]
                    and ("version" not in dependency or v == dependency["version"])
                )

    for name in sorted(members):
        visit({"name": name})
    components = [
        {
            "type": "library",
            "bom-ref": f"pkg:pypi/{name}@{version}",
            "name": name,
            "version": version,
            "purl": f"pkg:pypi/{name}@{version}",
        }
        for name, version in sorted(selected)
    ]
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "version": 1,
        "metadata": {
            "component": {"type": "application", "name": "nexura-python-release"},
            "properties": [
                {
                    "name": "nexura:profile",
                    "value": "All published Python workspace packages and all extras; union of "
                    "locked platform/marker branches; excludes dev groups, npm and OS",
                },
                {"name": "nexura:lock-sha256", "value": hashlib.sha256(lock_bytes).hexdigest()},
                {"name": "nexura:generator", "value": "scripts/generate_sbom.py"},
            ],
        },
        "components": components,
        "dependencies": [
            {"ref": ref, "dependsOn": sorted(targets)} for ref, targets in sorted(edges.items())
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if the SBOM is stale")
    args = parser.parse_args()
    output = ROOT / "packaging/sbom.json"
    rendered = json.dumps(generate(), indent=2) + "\n"
    if args.check:
        if output.read_text() != rendered:
            print("SBOM differs from uv.lock; run python scripts/generate_sbom.py")
            return 1
        print("SBOM matches uv.lock")
    else:
        output.write_text(rendered)
        print(f"Wrote {output.relative_to(ROOT)} from uv.lock")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
