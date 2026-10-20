"""P28 step 10 stage 4b: the Qdrant vector store is its own package, ``nexura-qdrant``.

The runtime does not depend on it and never imports it: without it the runtime imports and its
built-in vector stores work. The gate's ``qdrant-standalone-consumer`` step installs the real
wheels; here absence is simulated with ``sys.modules["nexura_qdrant"] = None``.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
IMPORT_STATEMENTS = (
    "import qdrant_client",
    "from qdrant_client",
    "import nexura_qdrant",
    "from nexura_qdrant",
)

ABSENT_PROBE = textwrap.dedent(
    """
    import sys
    sys.modules["nexura_qdrant"] = None  # the package is not installed
    import asyncio, json
    import nexura_runtime.knowledge.vector as vector
    import nexura_runtime.knowledge.vector.adapters as adapters
    from nexura_runtime.knowledge.vector.store import InMemoryVectorStore, VectorStore

    print(json.dumps({
        "builtin": isinstance(InMemoryVectorStore(), VectorStore),
        "adapters": sorted(adapters.__all__),
        "loaded": [m for m in sys.modules if m.startswith(("qdrant_client", "nexura_qdrant"))
                   and sys.modules[m] is not None],
    }))
    """
)


def test_runtime_without_qdrant_package_still_has_its_builtin_stores() -> None:
    out = subprocess.run(
        [sys.executable, "-c", ABSENT_PROBE],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert out.returncode == 0, out.stderr
    data = json.loads(out.stdout)
    assert data["builtin"] is True
    assert data["adapters"] == ["sanitize_and_validate_search_query", "validate_upsert_records"]
    assert data["loaded"] == []


async def test_installed_adapter_implements_the_runtime_protocol() -> None:
    from nexura_qdrant import QdrantVectorStoreAdapter

    from nexura_runtime.knowledge.vector.errors import VectorDimensionMismatchError
    from nexura_runtime.knowledge.vector.store import VectorStore

    store = QdrantVectorStoreAdapter(url="http://127.0.0.1:9")  # never contacted
    assert isinstance(store, VectorStore)
    assert store.is_available is True
    with pytest.raises(VectorDimensionMismatchError):
        await store.create_collection("ws", "c", dimensions=0)
    await store.close()


def test_runtime_sources_do_not_import_the_qdrant_package() -> None:
    hits = [
        path.relative_to(ROOT).as_posix()
        for path in sorted((ROOT / "src" / "nexura_runtime").rglob("*.py"))
        for statement in IMPORT_STATEMENTS
        if statement in path.read_text(encoding="utf-8")
    ]
    assert hits == []
    assert not (ROOT / "src/nexura_runtime/knowledge/vector/adapters/qdrant.py").exists()
