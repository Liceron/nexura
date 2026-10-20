"""P28 step 10 stage 4b: the WASI, bubblewrap and gVisor backends are ``nexura-sandbox``.

The runtime keeps the selection, ``SandboxConfig``, the ``nexura.tools.sandbox.execute`` module
and the Monty backend. ``build_backend`` loads the moved classes when the package is installed;
without it each moved backend is a ``PackageNotInstalledBackend`` that declares the same name
and isolation, probes unavailable and refuses with "pip install nexura-sandbox", so selection
behaves as on a host where that backend is missing. Absence is simulated in a subprocess with
``sys.modules["nexura_sandbox"] = None``; the gate's ``sandbox-standalone-consumer`` step
checks the same with real wheels.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _reason(name: str) -> str:
    return (
        f"The '{name}' sandbox backend is provided by the 'nexura-sandbox' package, which is "
        "not installed. Install it with: pip install nexura-sandbox"
    )


ABSENT_PROBE = textwrap.dedent(
    """
    import sys
    sys.modules["nexura_sandbox"] = None  # the package is not installed
    import asyncio, io, json, contextlib
    from nexura_agents.contracts.context import ExecutionContext
    from nexura_runtime.integrations.sandbox import (
        CapabilityUnavailableError, PackageNotInstalledBackend, SandboxConfig,
        SandboxExecutionModule, build_backend, select_backend,
    )
    from nexura_runtime.modules import default_registry

    out = {"backends": {}}
    for name in ("wasi", "bubblewrap", "gvisor_oci"):
        backend = build_backend(name)
        out["backends"][name] = {
            "placeholder": isinstance(backend, PackageNotInstalledBackend),
            "isolation": backend.isolation,
            "reason": backend.probe(SandboxConfig()).reason,
        }
    module = default_registry.get("nexura.tools.sandbox.execute")
    out["default_worker"] = [type(module.worker).__name__, module.worker.backend_name]
    out["default_internal"] = module.manifest.internal
    approved = ExecutionContext(run_id="r", node_id="n", approval_granted=True)
    result = asyncio.run(module.execute(approved, {"code": "print(1)"}, {}))
    out["default_call"] = [result.status, result.error_code, result.error_message]
    out["monty_selected"] = select_backend(
        SandboxConfig(backends=["bubblewrap", "gvisor_oci", "monty"])
    ).backend_name
    try:
        select_backend(SandboxConfig(backends=["monty", "gvisor_oci"], min_isolation="gvisor"))
        out["floor"] = None
    except CapabilityUnavailableError as exc:
        out["floor"] = exc.reason
    floor_module = SandboxExecutionModule(
        config=SandboxConfig(backends=["wasi", "bubblewrap"], min_isolation="interpreter")
    )
    out["floor_module"] = [floor_module.visible, floor_module.selection_error]
    from nexura_runtime.cli.main import main
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        code = main(["sandbox", "install-runtime", "wasi", "--from-file", "/nonexistent.zip"])
    out["cli"] = [code, err.getvalue()]
    print(json.dumps(out))
    """
)


def _run(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120, check=False
    )


def test_runtime_without_sandbox_package_reports_the_backends_unavailable() -> None:
    out = _run(ABSENT_PROBE)
    assert out.returncode == 0, out.stderr
    data = json.loads(out.stdout)
    assert data["backends"] == {
        "wasi": {"placeholder": True, "isolation": "interpreter", "reason": _reason("wasi")},
        "bubblewrap": {
            "placeholder": True,
            "isolation": "os_sandbox",
            "reason": _reason("bubblewrap"),
        },
        "gvisor_oci": {"placeholder": True, "isolation": "gvisor", "reason": _reason("gvisor_oci")},
    }
    # The legacy default (no NEXURA_SANDBOX_BACKENDS) stays hidden and fails closed.
    assert data["default_worker"] == ["PackageNotInstalledBackend", "gvisor_oci"]
    assert data["default_internal"] is True
    status, error_code, message = data["default_call"]
    assert (status, error_code) == ("failure", "CAPABILITY_UNAVAILABLE")
    assert "pip install nexura-sandbox" in message
    # Selection falls through to Monty, and an isolation floor Monty cannot meet is refused.
    assert data["monty_selected"] == "monty"
    assert "monty: isolation 'interpreter' is below the operator minimum 'gvisor'" in data["floor"]
    assert f"gvisor_oci: {_reason('gvisor_oci')}" in data["floor"]
    visible, selection_error = data["floor_module"]
    assert visible is False
    assert _reason("wasi") in selection_error and _reason("bubblewrap") in selection_error
    code, stderr = data["cli"]
    assert code == 1
    assert stderr.strip() == (
        "Error: The WASI sandbox backend is provided by the 'nexura-sandbox' package, which is "
        "not installed. Install it with: pip install nexura-sandbox"
    )


def test_installed_package_provides_the_same_classes_and_isolation() -> None:
    from nexura_sandbox import BubblewrapWorker, GVisorOCIWorker, WasiWorker

    from nexura_runtime.integrations.sandbox import PackageNotInstalledBackend, build_backend
    from nexura_runtime.integrations.sandbox.backends import PACKAGE_BACKENDS
    from nexura_runtime.modules import default_registry

    classes = {"wasi": WasiWorker, "bubblewrap": BubblewrapWorker, "gvisor_oci": GVisorOCIWorker}
    for name, cls in classes.items():
        backend = build_backend(name)
        assert type(backend) is cls
        assert not isinstance(backend, PackageNotInstalledBackend)
        module_name, class_name, isolation = PACKAGE_BACKENDS[name]
        assert (cls.__module__, cls.__name__) == (module_name, class_name)
        # The placeholder declares the real class's isolation, so selection is unchanged.
        assert backend.isolation == cls.isolation == isolation
        assert backend.backend_name == name
    module = default_registry.get("nexura.tools.sandbox.execute")
    assert type(module.worker) is GVisorOCIWorker


def test_monty_and_the_selection_stay_in_the_runtime() -> None:
    import nexura_runtime.integrations.sandbox as sandbox

    for name in ("MontyWorker", "SandboxConfig", "select_backend", "SandboxExecutionModule"):
        assert getattr(sandbox, name).__module__.startswith("nexura_runtime.integrations.sandbox")
    for name in ("BubblewrapWorker", "GVisorOCIWorker", "WasiWorker"):
        assert name not in sandbox.__all__
    for moved in ("bubblewrap_worker", "bubblewrap_policy", "wasi_worker", "wasi_runner"):
        assert not (ROOT / f"src/nexura_runtime/integrations/sandbox/{moved}.py").exists()
    assert not (ROOT / "src/nexura_runtime/integrations/sandbox/wasi_runtime.py").exists()


def test_a_broken_sandbox_install_is_not_hidden(tmp_path: Path) -> None:
    pkg = tmp_path / "nexura_sandbox"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("import nexura_dependency_that_does_not_exist\n")
    code = f"import sys; sys.path.insert(0, {str(tmp_path)!r}); import nexura_runtime.modules"
    out = _run(code)
    assert out.returncode != 0
    assert "nexura_dependency_that_does_not_exist" in out.stderr


@pytest.mark.parametrize("statement", ["import nexura_sandbox", "from nexura_sandbox"])
def test_runtime_sources_import_the_sandbox_package_only_after_checking(statement: str) -> None:
    hits = [
        path.relative_to(ROOT).as_posix()
        for path in sorted((ROOT / "src" / "nexura_runtime").rglob("*.py"))
        if statement in path.read_text(encoding="utf-8")
    ]
    # backends.build_backend imports by module name after find_package(); the CLI's
    # `sandbox install-runtime` imports wasi_runtime after the same check.
    assert hits == (["src/nexura_runtime/cli/main.py"] if statement.startswith("from") else [])
