"""P28 step 10 stage 4b: the PostgreSQL backends are their own package, ``nexura-postgres``.

``get_storage_repository("postgresql://...")`` returns ``nexura_postgres.PostgresRepository``
when the package is installed; without it the runtime still imports, SQLite storage works, and
the Postgres URI raises ``MissingDependencyError`` naming "pip install nexura-postgres".
Absence is simulated in a subprocess with ``sys.modules["nexura_postgres"] = None``; the
gate's ``postgres-standalone-consumer`` step checks the same with real wheels.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EXPECTED = (
    "PostgreSQL storage is provided by the 'nexura-postgres' package, which is not installed. "
    "Install it with: pip install nexura-postgres"
)

ABSENT_PROBE = textwrap.dedent(
    """
    import sys
    sys.modules["nexura_postgres"] = None  # the package is not installed
    import json, tempfile, os
    from nexura_agents.contracts import MissingDependencyError
    import nexura_runtime
    from nexura_runtime.storage import SQLiteRepository, get_storage_repository

    sqlite = get_storage_repository(os.path.join(tempfile.mkdtemp(), "runs.db"))
    try:
        get_storage_repository("postgresql://u:p@localhost:5432/db")
        error = None
    except MissingDependencyError as exc:
        error = {"message": str(exc), "dependency": exc.dependency_name,
                 "code": str(exc.code)}
    print(json.dumps({
        "sqlite": isinstance(sqlite, SQLiteRepository),
        "error": error,
        "exported": hasattr(nexura_runtime, "PostgresRepository"),
    }))
    """
)


def _run(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120, check=False
    )


def test_runtime_without_postgres_package_names_the_install_command() -> None:
    out = _run(ABSENT_PROBE)
    assert out.returncode == 0, out.stderr
    data = json.loads(out.stdout)
    assert data["sqlite"] is True
    assert data["exported"] is False
    assert data["error"]["message"] == EXPECTED
    assert data["error"]["dependency"] == "nexura-postgres"
    assert "MISSING_DEPENDENCY" in data["error"]["code"]


def test_installed_package_is_returned_for_postgres_uris() -> None:
    from nexura_postgres import PgVectorStoreAdapter, PostgresRepository

    from nexura_runtime.knowledge.vector.store import VectorStore
    from nexura_runtime.storage import StorageRepository, get_storage_repository

    for uri in ("postgresql://u:p@localhost/db", "postgres://u:p@localhost/db"):
        repository = get_storage_repository(uri)
        assert type(repository) is PostgresRepository
        assert isinstance(repository, StorageRepository)
        assert repository.dsn == uri
    assert isinstance(PgVectorStoreAdapter(dsn="postgresql://u:p@localhost/db"), VectorStore)


def test_a_broken_postgres_install_is_not_hidden(tmp_path: Path) -> None:
    pkg = tmp_path / "nexura_postgres"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("import nexura_dependency_that_does_not_exist\n")
    code = (
        f"import sys; sys.path.insert(0, {str(tmp_path)!r})\n"
        "from nexura_runtime.storage import get_storage_repository\n"
        "get_storage_repository('postgresql://u:p@localhost/db')\n"
    )
    out = _run(code)
    assert out.returncode != 0
    assert "nexura_dependency_that_does_not_exist" in out.stderr


def test_runtime_sources_do_not_import_the_postgres_package() -> None:
    hits = [
        path.relative_to(ROOT).as_posix()
        for path in sorted((ROOT / "src" / "nexura_runtime").rglob("*.py"))
        if "import nexura_postgres" in path.read_text(encoding="utf-8")
        or "from nexura_postgres" in path.read_text(encoding="utf-8")
    ]
    assert hits == []
    assert not (ROOT / "src/nexura_runtime/knowledge/vector/adapters/pgvector.py").exists()
