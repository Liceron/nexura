"""P35 A3: local fixtures and mock HTTP only; no service/TestClient execution."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest

from nexura_runtime.knowledge.embeddings.config import configured_embedder, describe_embedder
from nexura_runtime.knowledge.embeddings.embedder import EmbeddingOrchestrator
from nexura_runtime.knowledge.embeddings.errors import DimensionMismatchError
from nexura_runtime.knowledge.embeddings.models import BatchEmbeddingResult
from nexura_runtime.knowledge.embeddings.provider import DeterministicEmbeddingProvider
from nexura_runtime.knowledge.hybrid import EmbeddingLineageMismatchError, HybridKnowledgeIndex

# Load the example by path: the repository root is not on sys.path in every checkout
# (the public tree has no root conftest.py), so ``import examples...`` is not portable.
_EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "knowledge" / "evaluate_retrieval.py"
_spec = importlib.util.spec_from_file_location("nexura_example_evaluate_retrieval", _EXAMPLE)
assert _spec is not None and _spec.loader is not None
_example = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_example)
evaluate = _example.evaluate


def mock_embedder(
    provider: str = "gemini", model: str = "mock", dimension: int = 3
) -> EmbeddingOrchestrator:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert request.headers.get("x-goog-api-key") == "local-test-key" or (
            request.headers.get("authorization") == "Bearer local-test-key"
        )
        count = len(payload.get("requests", payload.get("input", [])))
        if provider == "gemini":
            assert request.url.path.endswith(":batchEmbedContents")
            return httpx.Response(200, json={"embeddings": [{"values": [1.0] * dimension}] * count})
        assert request.url.path.endswith("/embeddings")
        return httpx.Response(
            200,
            json={
                "data": [{"index": i, "embedding": [1.0] * dimension} for i in range(count)],
                "usage": {"prompt_tokens": count, "total_tokens": count},
            },
        )

    return configured_embedder(
        f"{provider}:{model}",
        dimensions=dimension,
        environ={"GEMINI_API_KEY": "local-test-key", "OPENAI_API_KEY": "local-test-key"},
        transport=httpx.MockTransport(handler),
    )


async def close_provider(embedder: EmbeddingOrchestrator) -> None:
    close = getattr(embedder.provider, "close", None)
    assert close is not None
    await close()


def test_config_default_and_explicit_precedence() -> None:
    assert describe_embedder(configured_embedder(environ={})) == {
        "provider": "deterministic",
        "model": "deterministic-vector-fixture-non-semantic",
        "revision": "v1",
        "dimension": 8,
        "is_test_fixture": True,
    }
    assert (
        describe_embedder(
            configured_embedder(
                "fixture",
                environ={
                    "NEXURA_EMBEDDINGS": "gemini:text-embedding-004",
                },
            )
        )["is_test_fixture"]
        is True
    )
    configured = configured_embedder(
        environ={
            "NEXURA_EMBEDDINGS": "gemini:text-embedding-004",
            "GOOGLE_API_KEY": "test",
        }
    )
    assert describe_embedder(configured)["dimension"] == 768
    assert describe_embedder(configured)["is_test_fixture"] is False
    assert (
        describe_embedder(
            configured_embedder(
                "openai:text-embedding-3-small",
                environ={"OPENAI_API_KEY": "test"},
            )
        )["dimension"]
        == 1536
    )


def test_config_dimension_environment_and_explicit_override() -> None:
    env = {
        "NEXURA_EMBEDDINGS": "gemini:custom-embedding",
        "NEXURA_EMBEDDINGS_DIMENSIONS": "64",
        "GEMINI_API_KEY": "local-test-key",
    }
    assert describe_embedder(configured_embedder(environ=env))["dimension"] == 64
    assert describe_embedder(configured_embedder(dimensions=32, environ=env))["dimension"] == 32


@pytest.mark.parametrize(
    "spec,env,match",
    [
        ("bad", {}, "must be"),
        ("other:model", {}, "must be"),
        ("openai:", {}, "must be"),
        ("gemini:text-embedding-004", {}, "require.*GEMINI_API_KEY"),
        ("openai:text-embedding-3-small", {}, "require.*OPENAI_API_KEY"),
        ("gemini:unknown", {"GEMINI_API_KEY": "test"}, "DIMENSIONS"),
        ("gemini:unknown", {"NEXURA_EMBEDDINGS_DIMENSIONS": "0"}, "positive"),
    ],
)
def test_invalid_config_never_falls_back(spec: str, env: dict[str, str], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        configured_embedder(spec, environ=env)


@pytest.mark.parametrize("provider", ["openai", "gemini"])
async def test_real_provider_wire_contract_offline(provider: str) -> None:
    embedder = mock_embedder(provider)
    try:
        result = await embedder.embed_texts(["सूरज", "sun"])
        assert len(result.embedded_chunks) == 2
        assert result.embedded_chunks[0].embedding == [1.0] * 3
        assert describe_embedder(embedder)["provider"] == provider
    finally:
        await close_provider(embedder)


async def test_vertex_config_uses_existing_provider_with_mock_adc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class LocalADC:
        project = "test-project"
        quota_project = None
        description = "local test token fixture"

        def __call__(self) -> str:
            return "local-test-key"

    monkeypatch.setattr("nexura_vertex.provider._AdcTokenSource", LocalADC)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer local-test-key"
        assert request.url.path.endswith("/models/text-embedding-004:predict")
        return httpx.Response(200, json={"predictions": [{"embeddings": {"values": [0.5] * 768}}]})

    embedder = configured_embedder(
        "vertex:text-embedding-004",
        environ={},
        transport=httpx.MockTransport(handler),
    )
    try:
        assert len((await embedder.embed_texts(["local test"])).embedded_chunks[0].embedding) == 768
        assert describe_embedder(embedder)["provider"] == "vertex"
    finally:
        await close_provider(embedder)


async def populate(index: HybridKnowledgeIndex) -> str:
    result = await index.ingest(workspace_id="w", filename="guide.txt", data=b"alpacas eat grass")
    return result.document.document_id


@pytest.mark.parametrize("change", ["model", "dimension", "revision", "provider"])
async def test_lineage_persists_and_refuses_read_write(change: str, tmp_path: Path) -> None:
    db = tmp_path / "knowledge.sqlite"
    conn = sqlite3.connect(db)
    original = HybridKnowledgeIndex(conn)
    await populate(original)
    conn.close()
    conn = sqlite3.connect(db)
    if change == "provider":
        embedder = mock_embedder()
        embedder.lineage = original.embedder.lineage.model_copy()
    else:
        provider = DeterministicEmbeddingProvider(
            model_id="changed" if change == "model" else original.embedder.lineage.model_name,
            dimensions=16 if change == "dimension" else 8,
            revision="v2" if change == "revision" else "v1",
        )
        embedder = EmbeddingOrchestrator(provider)
    changed = HybridKnowledgeIndex(conn, embedder=embedder)
    assert changed.embedding_status("w")["requires_reindex"]
    assert not changed.embedding_status("other")["requires_reindex"]
    with pytest.raises(EmbeddingLineageMismatchError, match="Explicitly reindex"):
        await changed.search(workspace_id="w", query="grass", vector_weight=0)
    with pytest.raises(EmbeddingLineageMismatchError):
        await populate(changed)  # Even deduplicated uploads refuse a changed vector space.
    conn.close()


async def test_reindex_preserves_documents_citations_and_scope() -> None:
    conn = sqlite3.connect(":memory:")
    old = HybridKnowledgeIndex(conn)
    doc_id = await populate(old)
    await old.ingest(workspace_id="other", filename="other.txt", data=b"other collection")
    chunk_sql = "SELECT chunk_id, text, start_char, end_char FROM hk_chunks WHERE workspace_id='w'"
    before = conn.execute(chunk_sql).fetchall()
    new = HybridKnowledgeIndex(conn, embedder=mock_embedder())
    try:
        result = await new.reindex("w")
        assert result["reindexed_chunks"] == 1
        assert result["embedder"]["provider"] == "gemini"
        assert result["is_test_fixture"] is False
        assert result["requires_reindex"] is False
        assert new.get_document("w", doc_id) == old.get_document("w", doc_id)
        assert conn.execute(chunk_sql).fetchall() == before
        assert (await new.search(workspace_id="w", query="grass"))[0].citation.document_id == doc_id
        assert not old.embedding_status("other")["requires_reindex"]
        with pytest.raises(EmbeddingLineageMismatchError):
            await old.search(workspace_id="w", query="grass")
    finally:
        await close_provider(new.embedder)
        conn.close()


async def test_legacy_unknown_requires_reindex() -> None:
    index = HybridKnowledgeIndex()
    await populate(index)
    index._conn.execute("DELETE FROM hk_lineage")
    assert index.embedding_status("w")["indexed_embedder"] is None
    with pytest.raises(EmbeddingLineageMismatchError):
        await index.search(workspace_id="w", query="grass")
    assert (await index.reindex("w"))["requires_reindex"] is False
    assert await index.search(workspace_id="w", query="grass")


async def test_failed_reindex_retains_old_vectors_and_lineage() -> None:
    old = HybridKnowledgeIndex()
    await populate(old)
    before = old._conn.execute("SELECT * FROM hk_chunks").fetchall()
    broken = HybridKnowledgeIndex(
        old._conn,
        embedder=EmbeddingOrchestrator(
            DeterministicEmbeddingProvider(dimensions=8, dimension_override=7),
        ),
    )
    with pytest.raises(DimensionMismatchError):
        await broken.reindex("w")
    assert old._conn.execute("SELECT * FROM hk_chunks").fetchall() == before
    assert not old.embedding_status("w")["requires_reindex"]
    assert await old.search(workspace_id="w", query="grass")


async def test_concurrent_change_refuses_reindex(monkeypatch: pytest.MonkeyPatch) -> None:
    index = HybridKnowledgeIndex()
    doc_id = await populate(index)
    original = index.embedder.embed_texts

    async def during_embed(texts: Sequence[str]) -> BatchEmbeddingResult:
        result = await original(texts)
        index.delete_document("w", doc_id)
        return result

    monkeypatch.setattr(index.embedder, "embed_texts", during_embed)
    with pytest.raises(EmbeddingLineageMismatchError, match="changed during reindex"):
        await index.reindex("w")


async def test_reindex_database_failure_rolls_back_every_vector() -> None:
    old = HybridKnowledgeIndex()
    await populate(old)
    await old.ingest(workspace_id="w", filename="second.txt", data=b"another document")
    before = old._conn.execute("SELECT * FROM hk_chunks ORDER BY document_id").fetchall()
    # Abort on the second update, after the first vector has already been replaced.
    old._conn.execute(
        "CREATE TRIGGER fail_reindex BEFORE UPDATE OF embedding ON hk_chunks "
        "WHEN OLD.document_id = '" + before[1][1] + "' "
        "BEGIN SELECT RAISE(ABORT, 'local test write failure'); END"
    )
    new = HybridKnowledgeIndex(
        old._conn,
        embedder=EmbeddingOrchestrator(
            DeterministicEmbeddingProvider(model_id="changed", dimensions=16),
        ),
    )
    with pytest.raises(sqlite3.IntegrityError, match="local test write failure"):
        await new.reindex("w")
    assert old._conn.execute("SELECT * FROM hk_chunks ORDER BY document_id").fetchall() == before
    assert not old.embedding_status("w")["requires_reindex"]
    assert new.embedding_status("w")["requires_reindex"]


async def test_concurrent_embedder_change_refuses_inflight_ingest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = HybridKnowledgeIndex()
    new = HybridKnowledgeIndex(
        old._conn,
        embedder=EmbeddingOrchestrator(
            DeterministicEmbeddingProvider(model_id="changed"),
        ),
    )
    original = old.embedder.embed_chunks

    async def during_embed(*args: Any, **kwargs: Any) -> BatchEmbeddingResult:
        result = await original(*args, **kwargs)
        await new.reindex("w")  # Configure the empty collection while old ingestion is in flight.
        return result

    monkeypatch.setattr(old.embedder, "embed_chunks", during_embed)
    with pytest.raises(EmbeddingLineageMismatchError):
        await populate(old)
    assert old.list_documents("w") == []
    assert old._conn.execute("SELECT COUNT(*) FROM hk_chunks").fetchone()[0] == 0


async def test_empty_reindex_and_status_do_not_call_provider() -> None:
    provider = DeterministicEmbeddingProvider()
    index = HybridKnowledgeIndex(embedder=EmbeddingOrchestrator(provider))
    assert index.embedding_status("w")["is_test_fixture"] is True
    assert (await index.reindex("w"))["reindexed_chunks"] == 0
    assert provider.call_count == 0


async def test_evaluation_offline_ignores_ambient_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NEXURA_EMBEDDINGS", "invalid:would-fail-if-used")
    result = await evaluate()
    assert result["dataset_version"] == "synthetic-en-hi-v1"
    assert result["corpus_documents"] == 20
    assert set(result["results"]) == {"bm25_only", "fixture_hybrid"}
    for mode in result["results"].values():
        assert mode["metrics"]["en"]["queries"] == 30
        assert mode["metrics"]["hi"]["queries"] == 20
        assert 0 <= mode["metrics"]["all"]["recall@5"] <= 1
        assert 0 <= mode["metrics"]["all"]["mrr@5"] <= 1


async def test_evaluation_real_requires_explicit_credentials_and_refuses_ci(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CI", "true")
    with pytest.raises(ValueError, match="disabled in CI"):
        await evaluate(real=True)
    monkeypatch.delenv("CI")
    monkeypatch.setenv("NEXURA_EMBEDDINGS", "fixture")
    with pytest.raises(ValueError, match="explicit provider credentials"):
        await evaluate(real=True)


def test_dataset_gold_ids_and_language_counts() -> None:
    data: dict[str, Any] = json.loads(_example.DATASET.read_text())
    ids = {doc["id"] for doc in data["documents"]}
    assert len(ids) == len(data["documents"])
    assert len({q["id"] for q in data["queries"]}) == 50
    for query in data["queries"]:
        assert set(query["gold_document_ids"]) <= ids
        assert query["gold_document_ids"]
