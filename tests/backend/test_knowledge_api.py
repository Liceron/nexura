"""Integration tests for Knowledge Ingestion, Scoped Retrieval, and Citations API."""

import pytest
from httpx import ASGITransport, AsyncClient

from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.studio.store import StudioStore


@pytest.fixture
def tmp_store(tmp_path):
    db_file = tmp_path / "test_knowledge.sqlite3"
    return StudioStore(db_file)


@pytest.mark.asyncio
async def test_knowledge_lifecycle_upload_retrieve_answer_and_delete(tmp_store):
    app = create_api_app(store=tmp_store)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 1. List collections initially has default
        resp = await client.get("/api/v1/knowledge/collections")
        assert resp.status_code == 200
        assert "default" in resp.json()["collections"]

        # 2. Create collection
        resp = await client.post(
            "/api/v1/knowledge/collections",
            json={"collection_id": "docs-coll", "name": "Documentation", "description": "SDK docs"},
        )
        assert resp.status_code == 201
        assert resp.json()["collection_id"] == "docs-coll"

        # 3. Negative case: Path traversal in filename rejected
        resp = await client.post(
            "/api/v1/knowledge/documents/upload",
            json={
                "collection_id": "docs-coll",
                "filename": "../../etc/passwd",
                "content": "Malicious payload",
            },
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["code"] == "PATH_TRAVERSAL_REJECTED"

        # 4. Negative case: Oversized document rejected
        huge_content = "X" * 5_500_000
        resp = await client.post(
            "/api/v1/knowledge/documents/upload",
            json={
                "collection_id": "docs-coll",
                "filename": "huge.txt",
                "content": huge_content,
            },
        )
        assert resp.status_code == 413
        assert resp.json()["detail"]["code"] == "OVERSIZED_DOCUMENT"

        # 5. Ingest English document
        en_content = (
            "Nexura AgentKit architecture overview. "
            "The standalone SDK operates independently without any external framework "
            "dependencies. "
            "It features deterministic local models, SQLite session persistence, and hybrid RAG "
            "retrieval."
        )
        resp = await client.post(
            "/api/v1/knowledge/documents/upload",
            json={
                "collection_id": "docs-coll",
                "filename": "architecture_guide.md",
                "content": en_content,
                "mime_type": "text/markdown",
            },
        )
        assert resp.status_code == 201
        en_doc = resp.json()
        en_doc_id = en_doc["document_id"]
        assert en_doc["chunk_count"] >= 1
        assert en_doc["indexed_records"] >= 1

        # 6. Ingest Multilingual Hindi document
        hi_content = (
            "नेक्सुरा एजेंटकिट एक स्वतंत्र और शक्तिशाली ढांचा है। "
            "यह स्थानीय निष्पादन, डेटा सुरक्षा और हिंदी और अंग्रेजी में बहुभाषी एजेंट का समर्थन करता है।"
        )
        resp = await client.post(
            "/api/v1/knowledge/documents/upload",
            json={
                "collection_id": "docs-coll",
                "filename": "hindi_overview.txt",
                "content": hi_content,
                "mime_type": "text/plain",
            },
        )
        assert resp.status_code == 201
        hi_doc = resp.json()
        assert "document_id" in hi_doc

        # 7. List documents
        resp = await client.get(
            "/api/v1/knowledge/documents", params={"collection_id": "docs-coll"}
        )
        assert resp.status_code == 200
        docs_list = resp.json()
        assert docs_list["total"] == 2
        filenames = [d["filename"] for d in docs_list["items"]]
        assert "architecture_guide.md" in filenames
        assert "hindi_overview.txt" in filenames

        # 8. Hybrid Retrieval: English query
        resp = await client.post(
            "/api/v1/knowledge/retrieve",
            json={
                "collection_id": "docs-coll",
                "query": "standalone SDK SQLite persistence",
                "top_k": 3,
            },
        )
        assert resp.status_code == 200
        ret_data = resp.json()
        assert ret_data["has_sufficient_evidence"] is True
        assert len(ret_data["citations"]) >= 1
        first_cit = ret_data["citations"][0]
        assert first_cit["origin"] == "architecture_guide.md"
        assert first_cit["start_char"] >= 0
        assert first_cit["end_char"] > first_cit["start_char"]
        assert "standalone SDK" in first_cit["snippet"]

        # 9. Hybrid Retrieval: Hindi query
        resp = await client.post(
            "/api/v1/knowledge/retrieve",
            json={
                "collection_id": "docs-coll",
                "query": "स्वतंत्र ढांचा बहुभाषी",
                "top_k": 3,
            },
        )
        assert resp.status_code == 200
        hi_ret_data = resp.json()
        assert hi_ret_data["has_sufficient_evidence"] is True
        assert any(c["origin"] == "hindi_overview.txt" for c in hi_ret_data["citations"])

        # 10. Grounded Answer Generation
        resp = await client.post(
            "/api/v1/knowledge/answer",
            json={
                "collection_id": "docs-coll",
                "query": "What is the standalone SDK architecture?",
                "language": "en",
            },
        )
        assert resp.status_code == 200
        ans_data = resp.json()
        assert ans_data["has_sufficient_evidence"] is True
        assert len(ans_data["citations"]) >= 1
        assert "[1]" in ans_data["answer"]
        top_cit = ans_data["citations"][0]
        assert top_cit["origin"] in ans_data["answer"]

        # 11. Delete English document
        resp = await client.delete(f"/api/v1/knowledge/documents/{en_doc_id}")
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True

        # 12. Verify document 404 after deletion
        resp = await client.get(f"/api/v1/knowledge/documents/{en_doc_id}")
        assert resp.status_code == 404

        # 13. Verify subsequent retrieval excludes the deleted document
        resp = await client.post(
            "/api/v1/knowledge/retrieve",
            json={
                "collection_id": "docs-coll",
                "query": "standalone SDK architecture overview",
                "top_k": 3,
            },
        )
        assert resp.status_code == 200
        post_del_ret = resp.json()
        # Deleted document must not appear anywhere in citations
        assert not any(c["document_id"] == en_doc_id for c in post_del_ret["citations"])


@pytest.mark.asyncio
async def test_zip_upload_refused_and_min_score_threshold_is_normalised(tmp_store):
    import base64
    import io
    import zipfile

    app = create_api_app(store=tmp_store)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("big.txt", "wombat " * 1_000_000)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post(
            "/api/v1/knowledge/documents/upload",
            json={
                "filename": "x.zip",
                "mime_type": "application/zip",
                "content_encoding": "base64",
                "content": base64.b64encode(buf.getvalue()).decode(),
            },
        )
        assert r.status_code >= 400
        up = await client.post(
            "/api/v1/knowledge/documents/upload",
            json={"filename": "h.txt", "content": "heron wades in marshes"},
        )
        assert up.status_code == 201
        ok = await client.post(
            "/api/v1/knowledge/retrieve", json={"query": "heron wades", "min_score_threshold": 0.5}
        )
        assert ok.json()["items"]
        none = await client.post(
            "/api/v1/knowledge/retrieve", json={"query": "heron wades", "min_score_threshold": 1.0}
        )
        assert none.status_code == 200
        cit = ok.json()["citations"][0]
        assert cit["highlights_utf16"]


@pytest.mark.asyncio
async def test_upload_filename_rules_dedup_status_and_error_envelope(tmp_store):
    app = create_api_app(store=tmp_store)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        url = "/api/v1/knowledge/documents/upload"
        for bad in ("../x.txt", "a/b.txt", "a\\b.txt", "..", ".", "a\x00b.txt"):
            r = await c.post(url, json={"filename": bad, "content": "hello"})
            assert r.status_code == 400 and r.json()["detail"]["code"] == "PATH_TRAVERSAL_REJECTED"
        body = {"filename": "a..b.txt", "content": "hello there"}
        assert (await c.post(url, json=body)).status_code == 201
        again = await c.post(url, json=body)
        assert again.status_code == 200 and again.json()["deduplicated"] is True
        r = await c.post("/api/v1/knowledge/collections", json={"collection_id": " ", "name": "x"})
        assert r.status_code == 400 and {"code", "message"} <= set(r.json()["detail"])


@pytest.mark.asyncio
async def test_answer_has_no_invented_page_and_retrieval_is_rate_limited(tmp_store):
    app = create_api_app(store=tmp_store)
    app.state.knowledge_rate_limit = 3
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        await c.post(
            "/api/v1/knowledge/documents/upload",
            json={"filename": "p.txt", "content": "The refund window is thirty days."},
        )
        r = await c.post("/api/v1/knowledge/answer", json={"query": "refund window"})
        assert "(Page" not in r.json()["answer"] and r.json()["citations"][0]["page_start"] is None
        for _ in range(2):
            await c.post("/api/v1/knowledge/retrieve", json={"query": "refund"})
        r = await c.post("/api/v1/knowledge/retrieve", json={"query": "refund"})
        assert r.status_code == 429 and r.json()["detail"]["code"] == "RATE_LIMITED"
        assert "retry-after" in r.headers
