"""P28 step 10 stage 4b: the Studio launcher is its own package, ``nexura-studio``.

``nexura-studio`` holds the ``nexura-studio`` command, the FastAPI app and the Studio web page.
The runtime keeps what the REST service shares (store, built-in modules, traces, security
middleware) and its ``studio`` extra (FastAPI, uvicorn) for ``nexura-runtime serve``. Without
the package the runtime and the service still work, and ``nexura_runtime.studio.create_app``
names "pip install nexura-studio". Absence is simulated in a subprocess with
``sys.modules["nexura_studio"] = None``; the gate's ``studio-standalone-consumer`` step checks
the same with real wheels.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EXPECTED = (
    "Nexura Studio is provided by the 'nexura-studio' package, which is not installed. "
    "Install it with: pip install nexura-studio"
)

ABSENT_PROBE = textwrap.dedent(
    """
    import sys
    sys.modules["nexura_studio"] = None  # the package is not installed
    import json, tempfile, os
    from nexura_agents.contracts import MissingDependencyError
    import nexura_runtime.studio as studio
    from nexura_runtime.studio.builtin_modules import get_default_registry
    from nexura_runtime.studio.security import StudioAuthMiddleware
    from nexura_runtime.studio.store import StudioStore
    from nexura_runtime.service.api import create_api_app

    service = create_api_app(db_path=os.path.join(tempfile.mkdtemp(), "svc.sqlite3"))
    try:
        studio.create_app()
        error = None
    except MissingDependencyError as exc:
        error = str(exc)
    print(json.dumps({"service": type(service).__name__, "error": error,
                      "registry": type(get_default_registry()).__name__}))
    """
)


def _run(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120, check=False
    )


def test_runtime_without_studio_package_keeps_the_service_and_names_the_package() -> None:
    out = _run(ABSENT_PROBE)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == {
        "service": "FastAPI",
        "error": EXPECTED,
        "registry": "ModuleRegistry",
    }


def test_runtime_create_app_builds_the_installed_studio(tmp_path: Path) -> None:
    import nexura_studio
    import nexura_studio.server

    from nexura_runtime.studio import create_app

    app = create_app(db_path=tmp_path / "studio.sqlite3")
    assert app.title == "Nexura Studio"
    assert "/" in {getattr(route, "path", "") for route in app.routes}
    assert {"/api/v1/modules", "/api/v1/runs", "/api/v1/workflows"} <= set(app.openapi()["paths"])
    static = Path(nexura_studio.server.__file__).parent / "static"
    assert (static / "index.html").is_file() and (static / "app.js").is_file()
    assert (static / "fonts" / "OFL.txt").is_file()
    assert Path(nexura_studio.__file__).parent == static.parent


def test_launcher_files_moved_and_shared_pieces_stayed() -> None:
    runtime = ROOT / "src" / "nexura_runtime" / "studio"
    for moved in ("cli.py", "server.py", "static", "api/router.py", "api/runs.py"):
        assert not (runtime / moved).exists(), moved
    for kept in ("store.py", "builtin_modules.py", "traces.py", "security.py", "api/traces.py"):
        assert (runtime / kept).is_file(), kept


def test_a_broken_studio_install_is_not_hidden(tmp_path: Path) -> None:
    pkg = tmp_path / "nexura_studio"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("import nexura_dependency_that_does_not_exist\n")
    code = (
        f"import sys; sys.path.insert(0, {str(tmp_path)!r})\n"
        "from nexura_runtime.studio import create_app\n"
        "create_app()\n"
    )
    out = _run(code)
    assert out.returncode != 0
    assert "nexura_dependency_that_does_not_exist" in out.stderr


def test_runtime_sources_do_not_import_the_studio_package() -> None:
    hits = [
        path.relative_to(ROOT).as_posix()
        for path in sorted((ROOT / "src" / "nexura_runtime").rglob("*.py"))
        if "import nexura_studio" in path.read_text(encoding="utf-8")
        or "from nexura_studio" in path.read_text(encoding="utf-8")
    ]
    assert hits == []
