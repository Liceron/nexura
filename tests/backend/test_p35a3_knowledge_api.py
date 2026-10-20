"""A3 API acceptance tests. Lead runs these; service tests hang in the worker sandbox."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from nexura_runtime.knowledge.embeddings.config import describe_embedder
from nexura_runtime.knowledge.embeddings.embedder import EmbeddingOrchestrator
from nexura_runtime.knowledge.embeddings.provider import DeterministicEmbeddingProvider
from nexura_runtime.knowledge.hybrid import HybridKnowledgeIndex
from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.studio.store import StudioStore


async def test_status_lineage_conflict_and_explicit_reindex(tmp_path: Path) -> None:
    store = StudioStore(tmp_path / "service.sqlite")
    app = create_api_app(store=store, embeddings="fixture")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        base = "/api/v1/knowledge"
        status = await client.get(base + "/status")
        assert status.status_code == 200
        assert status.json()["is_test_fixture"] is True
        collections = (await client.get(base + "/collections")).json()
        assert collections["embedder"]["provider"] == "deterministic"
        assert collections["collection_status"]["default"]["requires_reindex"] is False
        uploaded = await client.post(
            base + "/documents/upload",
            json={
                "filename": "test.txt",
                "content": "alpacas eat grass",
            },
        )
        assert uploaded.status_code == 201
        doc_id = uploaded.json()["document_id"]
        # A service restart with a different operator configuration uses this same DB.
        changed = EmbeddingOrchestrator(DeterministicEmbeddingProvider(model_id="fixture-v2"))
        app.state.knowledge_embedder = changed
        app.state.hybrid_knowledge_index = HybridKnowledgeIndex(
            app.state.vector_store._conn,
            embedder=changed,
        )
        assert (await client.get(base + "/status")).json()["requires_reindex"] is True
        for route, body in (
            ("/retrieve", {"query": "grass"}),
            ("/answer", {"query": "grass"}),
            ("/documents/upload", {"filename": "test.txt", "content": "alpacas eat grass"}),
        ):
            refused = await client.post(base + route, json=body)
            assert refused.status_code == 409
            assert refused.json()["detail"]["code"] == "EMBEDDING_LINEAGE_MISMATCH"
        reindexed = await client.post(base + "/collections/default/reindex")
        assert reindexed.status_code == 200
        assert reindexed.json()["requires_reindex"] is False
        assert reindexed.json()["reindexed_chunks"] == 1
        retrieved = await client.post(base + "/retrieve", json={"query": "grass"})
        assert retrieved.status_code == 200
        assert retrieved.json()["citations"][0]["document_id"] == doc_id


async def test_service_real_config_status_and_mock_embedding_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "mock-only-key-never-dispatched")
    monkeypatch.setenv("NEXURA_EMBEDDINGS", "openai:ignored")
    app = create_api_app(
        store=StudioStore(tmp_path / "service.sqlite"),
        embeddings="gemini:text-embedding-004",
    )
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        calls.append(request.url.path)
        assert request.headers["x-goog-api-key"] == "mock-only-key-never-dispatched"
        count = len(json.loads(request.content)["requests"])
        return httpx.Response(200, json={"embeddings": [{"values": [1.0] * 768}] * count})

    app.state.knowledge_embedder.provider.transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/api/v1/knowledge/status")
        assert response.status_code == 200
        assert response.json()["embedder"] == describe_embedder(app.state.knowledge_embedder)
        assert response.json()["embedder"]["dimension"] == 768
        assert response.json()["is_test_fixture"] is False
        assert calls == []  # Status never dispatches an embedding request.
        uploaded = await client.post(
            "/api/v1/knowledge/documents/upload",
            json={
                "filename": "mock.txt",
                "content": "alpacas eat grass",
            },
        )
        assert uploaded.status_code == 201
        retrieved = await client.post("/api/v1/knowledge/retrieve", json={"query": "grass"})
        assert retrieved.status_code == 200
        assert len(calls) == 2
        assert retrieved.json()["citations"][0]["document_id"] == uploaded.json()["document_id"]
    await app.state.knowledge_embedder.provider.close()
