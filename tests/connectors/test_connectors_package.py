"""P28 step 10 stage 4a: the connectors are their own package, ``nexura-connectors``.

The runtime imports ``nexura_connectors`` when it is installed; without it the runtime still
imports and runs, and the connector module ids report "pip install nexura-connectors".
Absence is simulated in a subprocess with ``sys.modules["nexura_connectors"] = None``; the
gate's ``connectors-standalone-consumer`` step checks the same with real wheels.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

ABSENT_PROBE = textwrap.dedent(
    """
    import sys
    sys.modules["nexura_connectors"] = None  # the package is not installed
    import asyncio, json
    from nexura_agents.contracts import MissingDependencyError
    from nexura_runtime.modules import default_registry
    from nexura_runtime.workflow import ModuleRegistry, WorkflowEngine, WorkflowNode, WorkflowSpec

    mods = [m for m in default_registry.list_modules()
            if m["module_id"].startswith("nexura.connectors.")]
    try:
        default_registry.get("nexura.connectors.slack")
        get_error = None
    except MissingDependencyError as exc:
        get_error = str(exc)
    node = WorkflowNode(id="send", handler="nexura.connectors.slack",
                        module_id="nexura.connectors.slack", config={"credential_ref": "cred"})
    spec = WorkflowSpec(name="c", entry_node="send", nodes={"send": node})
    try:
        asyncio.run(WorkflowEngine(spec, ModuleRegistry()).run({"operation": "list_channels"}))
        run_error = None
    except Exception as exc:
        run_error = f"{type(exc).__name__}: {exc}"
    print(json.dumps({
        "ids": sorted(m["module_id"] for m in mods),
        "available": [m["module_id"] for m in mods if m["is_available"]],
        "reasons": [m["missing_reason"] for m in mods],
        "get_error": get_error,
        "run_error": run_error,
    }))
    """
)


def _run(code: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        **kwargs,  # type: ignore[arg-type]
    )


def test_runtime_without_connectors_reports_them_unavailable() -> None:
    from nexura_connectors import CONNECTOR_CLASSES

    out = _run(ABSENT_PROBE)
    assert out.returncode == 0, out.stderr
    data = json.loads(out.stdout)
    assert data["ids"] == sorted(cls.MODULE_ID for cls in CONNECTOR_CLASSES)
    assert len(data["ids"]) == 15
    assert data["available"] == []
    for module_id, reason in zip(data["ids"], data["reasons"], strict=True):
        assert reason == (
            f"Module '{module_id}' is provided by the 'nexura-connectors' package, which is not "
            "installed. Install it with: pip install nexura-connectors"
        )
    assert "pip install nexura-connectors" in data["get_error"]
    assert data["run_error"].startswith("MissingDependencyError")
    assert "pip install nexura-connectors" in data["run_error"]


def test_placeholders_match_the_real_connector_manifests() -> None:
    from nexura_connectors import CONNECTOR_CLASSES

    from nexura_runtime.modules.optional_packages import (
        CONNECTOR_MODULES,
        CONNECTORS_MODULE_VERSION,
        connector_placeholder_manifest,
    )

    real = {cls.MODULE_ID: cls.build_manifest() for cls in CONNECTOR_CLASSES}
    assert set(CONNECTOR_MODULES) == set(real)
    for module_id, manifest in real.items():
        placeholder = connector_placeholder_manifest(module_id)
        assert placeholder.version == manifest.version == CONNECTORS_MODULE_VERSION
        assert placeholder.title == manifest.title
        assert placeholder.kind == manifest.kind


def test_installed_connectors_are_registered_and_available() -> None:
    from nexura_runtime.modules import default_registry

    mods = [
        m
        for m in default_registry.list_modules()
        if m["module_id"].startswith("nexura.connectors.")
    ]
    assert len(mods) == 15
    assert all(m["is_available"] and m["missing_reason"] is None for m in mods)


def test_importing_connectors_first_registers_them() -> None:
    code = (
        "import nexura_connectors\n"
        "from nexura_runtime.modules import default_registry as r\n"
        "print(sum(1 for m in r.list_modules()"
        " if m['module_id'].startswith('nexura.connectors.') and m['is_available']))"
    )
    out = _run(code, cwd=str(ROOT))
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "15"


def test_a_broken_connectors_install_is_not_hidden(tmp_path: Path) -> None:
    pkg = tmp_path / "nexura_connectors"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("import nexura_dependency_that_does_not_exist\n")
    code = f"import sys; sys.path.insert(0, {str(tmp_path)!r}); import nexura_runtime.modules"
    out = _run(code)
    assert out.returncode != 0
    assert "nexura_dependency_that_does_not_exist" in out.stderr


def test_runtime_sources_import_connectors_only_through_the_optional_loader() -> None:
    hits = [
        path.relative_to(ROOT).as_posix()
        for path in sorted((ROOT / "src" / "nexura_runtime").rglob("*.py"))
        if "import nexura_connectors" in path.read_text(encoding="utf-8")
        or "from nexura_connectors" in path.read_text(encoding="utf-8")
    ]
    assert hits == ["src/nexura_runtime/modules/optional_packages.py"]
