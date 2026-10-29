"""Hybrid knowledge index tests (fake deterministic embedder; no network, no model calls).

They run on the SQLite index by default and on the Postgres index under
``--service-backend=postgres`` (MVP WP7, see ``tests/_knowledge_backend.py``).
"""

from __future__ import annotations

import base64
import json

import _knowledge_backend as kb
import pytest

from nexura_runtime.knowledge import (
    EmbeddingOrchestrator,
    FileTooLargeError,
    WorkspaceQuotaExceededError,
    create_hybrid_search_tool,
    weighted_rrf,
)
from nexura_runtime.knowledge.embeddings import DeterministicEmbeddingProvider
from nexura_runtime.knowledge.hybrid import (
    EmptyDocumentError,
    KnowledgeIndexBase,
    extract_query_terms,
)


def make_index(**kw: object) -> KnowledgeIndexBase:
    """Index on an in-memory DB (or the test's Postgres schema) with the labelled
    deterministic (non-semantic) fake embedder."""
    embedder = EmbeddingOrchestrator(DeterministicEmbeddingProvider())
    return kb.new_index(embedder=embedder, **kw)


def fts_count(index: KnowledgeIndexBase, needle: str) -> int:
    return kb.fts_count(index, needle)


async def ingest(index: KnowledgeIndexBase, name: str, text: str, ws: str = "w1", **kw: object):
    return await index.ingest(workspace_id=ws, filename=name, data=text.encode(), **kw)  # type: ignore[arg-type]


async def test_hybrid_search_returns_citation_with_all_fields() -> None:
    idx = make_index()
    out = await ingest(
        idx, "guide.md", "# Guide\nAlpacas eat grass.\n", source_uri="https://x/guide"
    )
    hits = await idx.search(workspace_id="w1", query="alpacas grass")
    assert hits
    c = hits[0].citation
    assert c.document_id == out.document.document_id
    assert c.title == "guide.md" and c.chunk_index == 0
    assert c.source_uri == "https://x/guide"
    assert c.end_char > c.start_char >= 0
    assert "keyword" in hits[0].matched_by
    assert hits[0].highlights and hits[0].snippet[
        hits[0].highlights[0][0] : hits[0].highlights[0][1]
    ].lower() in ("alpacas", "grass")


async def test_fusion_uses_weights() -> None:
    fused = weighted_rrf({"a": ["x", "y"], "b": ["y", "x"]}, {"a": 1.0, "b": 0.0})
    assert fused["x"] > fused["y"]
    fused = weighted_rrf({"a": ["x", "y"], "b": ["y", "x"]}, {"a": 0.0, "b": 1.0})
    assert fused["y"] > fused["x"]


async def test_keyword_only_and_vector_only_weights() -> None:
    idx = make_index()
    await ingest(idx, "a.txt", "quasar nebula pulsar")
    kw = await idx.search(workspace_id="w1", query="nebula", vector_weight=0)
    assert kw and kw[0].vector_rank is None and kw[0].keyword_rank == 1
    assert await idx.search(workspace_id="w1", query="zzzunseen", vector_weight=0) == []
    vec = await idx.search(workspace_id="w1", query="nebula", keyword_weight=0)
    assert all(h.keyword_rank is None for h in vec)


async def test_cross_workspace_search_returns_nothing() -> None:
    idx = make_index()
    await ingest(idx, "secret.txt", "launch codes are swordfish", ws="tenant-a")
    assert await idx.search(workspace_id="tenant-b", query="swordfish launch codes") == []
    assert idx.list_documents("tenant-b") == []
    assert await idx.search(workspace_id="tenant-a", query="swordfish")


async def test_oversized_file_refused_and_nothing_stored() -> None:
    idx = make_index(max_file_bytes=100)
    with pytest.raises(FileTooLargeError):
        await idx.ingest(workspace_id="w1", filename="big.txt", data=b"a " * 100)
    assert idx.list_documents("w1") == []


async def test_workspace_quota_refused() -> None:
    idx = make_index(max_file_bytes=1000, max_workspace_bytes=60)
    await ingest(idx, "one.txt", "x" * 40)
    with pytest.raises(WorkspaceQuotaExceededError):
        await ingest(idx, "two.txt", "y" * 40)
    await ingest(idx, "two.txt", "y" * 40, ws="other")  # other workspace unaffected
    await ingest(idx, "one.txt", "z" * 50)  # replacing frees the old bytes
    # text + title ("one.txt") + empty metadata JSON ("{}")
    assert idx.workspace_usage("w1") == 50 + len("one.txt") + 2


async def test_empty_and_whitespace_refused() -> None:
    idx = make_index()
    with pytest.raises(EmptyDocumentError):
        await idx.ingest(workspace_id="w1", filename="e.txt", data=b"")
    with pytest.raises(EmptyDocumentError):
        await idx.ingest(workspace_id="w1", filename="e.txt", data=b"   \n  ")


async def test_html_script_and_style_stripped() -> None:
    idx = make_index()
    html = (
        "<html><head><style>.x{}</style></head><body><p>Visible badger text</p>"
        "<script>alert('pwnedmarker')</script></body></html>"
    )
    out = await idx.ingest(
        workspace_id="w1", filename="p.html", data=html.encode(), mime_type="text/html"
    )
    assert out.document.mime_type == "text/html"
    text = idx.get_document_text("w1", out.document.document_id) or ""
    assert "badger" in text and "pwnedmarker" not in text and "<p>" not in text
    assert await idx.search(workspace_id="w1", query="pwnedmarker", vector_weight=0) == []
    assert fts_count(idx, "pwnedmarker") == 0


async def test_markdown_and_pdf_missing_content_handled() -> None:
    idx = make_index()
    out = await idx.ingest(
        workspace_id="w1", filename="n.md", data=b"# Title\n\nbody", mime_type="text/markdown"
    )
    assert out.document.status == "indexed"
    from nexura_runtime.knowledge.hybrid import DocumentParseError

    with pytest.raises(DocumentParseError):
        await idx.ingest(workspace_id="w1", filename="bad.pdf", data=b"%PDF-1.4 not really")


async def test_dedup_same_content_does_not_duplicate() -> None:
    idx = make_index()
    first = await ingest(idx, "a.txt", "same words here")
    again = await ingest(idx, "renamed.txt", "same words here")
    assert again.deduplicated and again.document.document_id == first.document.document_id
    assert len(idx.list_documents("w1")) == 1
    assert fts_count(idx, "words") == 1


async def test_reingest_replaces_chunks_without_stale_hits() -> None:
    idx = make_index()
    first = await ingest(idx, "doc.txt", "oldmarker content about pelicans")
    second = await ingest(idx, "doc.txt", "newmarker content about herons")
    assert second.replaced and second.document.document_id == first.document.document_id
    assert len(idx.list_documents("w1")) == 1
    assert await idx.search(workspace_id="w1", query="oldmarker", vector_weight=0) == []
    assert fts_count(idx, "oldmarker") == 0
    assert await idx.search(workspace_id="w1", query="newmarker")
    n = kb.count_chunks(idx)
    assert n == second.document.chunk_count


async def test_reingest_failure_keeps_old_version() -> None:
    idx = make_index()
    await ingest(idx, "doc.txt", "keepme original content")
    original_embed = idx.embedder.embed_chunks

    async def boom(*a: object, **k: object) -> None:
        raise RuntimeError("embedder down")

    idx.embedder.embed_chunks = boom  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        await ingest(idx, "doc.txt", "replacement content")
    idx.embedder.embed_chunks = original_embed  # type: ignore[method-assign]
    assert await idx.search(workspace_id="w1", query="keepme", vector_weight=0)


async def test_atomic_rollback_on_db_failure() -> None:
    idx = make_index()
    first = await ingest(idx, "doc.txt", "stable original text")
    kb.break_document_inserts(idx)
    before = kb.count_chunks(idx)
    assert first.document.chunk_count == before
    with pytest.raises(kb.database_error(idx)):
        await ingest(idx, "other.txt", "failing insert text")
    after = kb.count_chunks(idx)
    assert after == before
    assert fts_count(idx, "failing") == 0


async def test_delete_removes_vectors_and_keyword_entries() -> None:
    idx = make_index()
    out = await ingest(idx, "gone.txt", "ephemeral zeppelin content")
    assert fts_count(idx, "zeppelin") == 1
    assert idx.delete_document("w1", out.document.document_id)
    assert fts_count(idx, "zeppelin") == 0
    assert kb.count_chunks(idx) == 0
    assert await idx.search(workspace_id="w1", query="zeppelin") == []
    assert not idx.delete_document("w1", out.document.document_id)


async def test_delete_other_workspace_document_refused() -> None:
    idx = make_index()
    out = await ingest(idx, "a.txt", "tenant data", ws="a")
    assert not idx.delete_document("b", out.document.document_id)
    assert await idx.search(workspace_id="a", query="tenant")


@pytest.mark.parametrize(
    "query",
    [
        'x" OR "1"="1',
        "NEAR(a b) AND NOT *",
        "hello) OR (world",
        "'; DROP TABLE hk_chunks; --",
        '"unterminated',
        "col:text * ^start",
    ],
)
async def test_query_injection_is_inert(query: str) -> None:
    idx = make_index()
    await ingest(idx, "a.txt", "ordinary content about hello world")
    hits = await idx.search(workspace_id="w1", query=query)
    assert isinstance(hits, list)
    assert kb.count_chunks(idx) >= 1
    assert all(re_ok(t) for t in extract_query_terms(query))


def re_ok(term: str) -> bool:
    return all(ch.isalnum() or ch == "_" or "ऀ" <= ch <= "ॿ" for ch in term)


async def test_empty_or_symbol_query_returns_nothing() -> None:
    idx = make_index()
    await ingest(idx, "a.txt", "content")
    assert await idx.search(workspace_id="w1", query="  !!! ") == []


async def test_top_k_cap() -> None:
    idx = make_index(max_top_k=3)
    for i in range(8):
        await ingest(idx, f"d{i}.txt", f"shared keyword number{i}")
    assert len(await idx.search(workspace_id="w1", query="shared", top_k=100)) == 3
    assert len(await idx.search(workspace_id="w1", query="shared", top_k=2)) == 2
    assert len(await idx.search(workspace_id="w1", query="shared", top_k=0)) == 1


async def test_hindi_keyword_search() -> None:
    idx = make_index()
    await ingest(idx, "hi.txt", "नेक्सुरा एजेंटकिट एक स्वतंत्र और शक्तिशाली ढांचा है")
    hits = await idx.search(workspace_id="w1", query="स्वतंत्र ढांचा", vector_weight=0)
    assert hits and "keyword" in hits[0].matched_by
    s, e = hits[0].highlights[0]
    assert hits[0].snippet[s:e] in ("स्वतंत्र", "ढांचा")


async def test_collections_isolated() -> None:
    idx = make_index()
    await ingest(idx, "a.txt", "walrus facts", collection_id="c1")
    assert await idx.search(workspace_id="w1", query="walrus", collection_id="c2") == []


async def test_get_chunk_and_workspace_scope() -> None:
    idx = make_index()
    out = await ingest(idx, "a.txt", "chunk target text")
    chunk = idx.get_chunk("w1", out.document.document_id, 0)
    assert chunk and chunk["title"] == "a.txt"
    assert idx.get_chunk("w2", out.document.document_id, 0) is None


async def test_search_tool_emits_json_citations() -> None:
    idx = make_index()
    await ingest(idx, "a.txt", "tool findable fact")
    tool = create_hybrid_search_tool(idx, workspace_id="w1")
    raw = await tool.fn("findable")  # type: ignore[misc]
    payload = json.loads(raw)
    assert payload["citations"][0]["title"] == "a.txt"
    other = create_hybrid_search_tool(idx, workspace_id="w2")
    assert json.loads(await other.fn("findable"))["citations"] == []  # type: ignore[misc]


def tiny_pdf(text: str) -> bytes:
    """Hand-built single-page PDF containing ``text`` (no external generator needed)."""
    stream = f"BT /F1 12 Tf 20 100 Td ({text}) Tj ET".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF" % (len(objs) + 1, xref)
    return out


async def test_pdf_ingest_searchable() -> None:
    idx = make_index()
    out = await idx.ingest(
        workspace_id="w1", filename="r.pdf", data=tiny_pdf("flamingo colony report")
    )
    assert out.document.mime_type == "application/pdf"
    assert await idx.search(workspace_id="w1", query="flamingo", vector_weight=0)


# -- HTTP API (mock ASGI transport, in-process app, deterministic fake embedder) ----------------


@pytest.fixture
def api_client(tmp_path):
    from httpx import ASGITransport, AsyncClient

    from nexura_runtime.service.api.app import create_api_app
    from nexura_runtime.studio.store import StudioStore

    app = create_api_app(store=StudioStore(tmp_path / "k.sqlite3"))
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_api_upload_search_citation_footnote_target_and_delete(api_client) -> None:
    async with api_client as c:
        lim = (await c.get("/api/v1/knowledge/limits")).json()
        assert lim["max_file_bytes"] == 5_000_000 and lim["workspace_used_bytes"] == 0
        r = await c.post(
            "/api/v1/knowledge/documents/upload",
            json={
                "filename": "a.md",
                "content": "# Heron\nThe heron wades in marshes.",
                "source_uri": "https://example.test/a",
            },
        )
        assert r.status_code == 201 and r.json()["status"] == "indexed"
        doc_id = r.json()["document_id"]
        again = await c.post(
            "/api/v1/knowledge/documents/upload",
            json={"filename": "a.md", "content": "# Heron\nThe heron wades in marshes."},
        )
        assert again.json()["deduplicated"] is True and again.json()["document_id"] == doc_id
        res = (await c.post("/api/v1/knowledge/retrieve", json={"query": "heron marshes"})).json()
        cit = res["citations"][0]
        assert cit["document_id"] == doc_id and cit["title"] == "a.md"
        assert cit["source_uri"] == "https://example.test/a" and cit["highlights"]
        chunk = await c.get(f"/api/v1/knowledge/documents/{doc_id}/chunks/{cit['chunk_index']}")
        assert chunk.status_code == 200 and "heron" in chunk.json()["text"]
        assert (await c.delete(f"/api/v1/knowledge/documents/{doc_id}")).status_code == 200
        res = (await c.post("/api/v1/knowledge/retrieve", json={"query": "heron"})).json()
        assert res["citations"] == [] and res["has_sufficient_evidence"] is False
        assert (await c.get(f"/api/v1/knowledge/documents/{doc_id}/chunks/0")).status_code == 404


async def test_api_rejects_oversize_bad_base64_and_top_k(api_client) -> None:
    async with api_client as c:
        big = await c.post(
            "/api/v1/knowledge/documents/upload",
            json={"filename": "big.txt", "content": "a" * 5_000_001},
        )
        assert big.status_code == 413 and big.json()["detail"]["code"] == "OVERSIZED_DOCUMENT"
        bad = await c.post(
            "/api/v1/knowledge/documents/upload",
            json={"filename": "x.pdf", "content": "!!notb64", "content_encoding": "base64"},
        )
        assert bad.status_code == 400 and bad.json()["detail"]["code"] == "INVALID_ENCODING"
        pdf = await c.post(
            "/api/v1/knowledge/documents/upload",
            json={
                "filename": "x.pdf",
                "content": base64.b64encode(b"%PDF-1.4 junk").decode(),
                "content_encoding": "base64",
            },
        )
        assert pdf.status_code == 400 and pdf.json()["detail"]["code"] == "DOCUMENT_PARSE_FAILED"
        cap = await c.post("/api/v1/knowledge/retrieve", json={"query": "x", "top_k": 51})
        assert cap.status_code == 422


# -- P11 review regressions ----------------------------------------------------------------------


async def test_archives_refused_and_text_size_counts() -> None:
    import io
    import zipfile

    from nexura_runtime.knowledge.hybrid import DocumentParseError

    idx = make_index(max_file_bytes=20_000, max_workspace_bytes=50_000)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("big.txt", "wombat " * 100_000)
    with pytest.raises(DocumentParseError):
        await idx.ingest(
            workspace_id="w", filename="x.zip", data=buf.getvalue(), mime_type="application/zip"
        )
    with pytest.raises(DocumentParseError):
        await idx.ingest(workspace_id="w", filename="y.zip", data=buf.getvalue())


async def test_workspace_usage_counts_stored_text() -> None:
    idx = make_index()
    out = await idx.ingest(workspace_id="w", filename="a.html", data=b"<html><p>hello</p></html>")
    assert out.document.byte_size > out.document.text_length
    # text + title ("a.html") + empty metadata JSON ("{}")
    assert idx.workspace_usage("w") == out.document.text_length + len("a.html") + 2


async def test_reupload_same_title_with_other_docs_content_replaces() -> None:
    idx = make_index()
    await idx.ingest(workspace_id="w", filename="a.txt", data=b"alpha original wombat")
    await idx.ingest(workspace_id="w", filename="b.txt", data=b"beta replacement quokka")
    out = await idx.ingest(workspace_id="w", filename="a.txt", data=b"beta replacement quokka")
    assert out.replaced and not out.deduplicated
    assert not any(h.title == "a.txt" for h in await idx.search(workspace_id="w", query="wombat"))


async def test_highlights_are_utf16_and_cp_kept() -> None:
    idx = make_index()
    await idx.ingest(workspace_id="w", filename="e.txt", data="\U0001f600 heron".encode())
    h = (await idx.search(workspace_id="w", query="heron"))[0]
    assert h.highlights_cp[0] == (2, 7) and h.highlights[0] == (3, 8)


async def test_long_query_token_matches_up_to_256() -> None:
    idx = make_index()
    tok = "b" * 200
    await idx.ingest(workspace_id="w", filename="t.txt", data=f"id {tok} end".encode())
    assert await idx.search(workspace_id="w", query=tok, vector_weight=0)
    assert extract_query_terms("c" * 300)[0] == "c" * 256


async def test_min_score_is_normalised() -> None:
    idx = make_index()
    await idx.ingest(workspace_id="w", filename="t.txt", data=b"heron wades in marshes")
    top = (await idx.search(workspace_id="w", query="heron wades"))[0]
    assert 0.9 <= top.score <= 1.0
    assert await idx.search(workspace_id="w", query="heron wades", min_score=0.5)
    assert await idx.search(workspace_id="w", query="heron wades", min_score=1.01) == []


async def test_quota_counts_metadata_source_uri_and_title() -> None:
    idx = make_index()
    await idx.ingest(
        workspace_id="w",
        filename="n.txt",
        data=b"tiny",
        source_uri="u" * 1000,
        metadata={"k": "m" * 500},
    )
    assert idx.workspace_usage("w") >= 4 + 1000 + 500 + len("n.txt")


async def test_metadata_and_source_uri_are_capped() -> None:
    from nexura_runtime.knowledge.hybrid import DocumentLimitError

    idx = make_index(max_metadata_bytes=100, max_source_uri_chars=50)
    with pytest.raises(DocumentLimitError):
        await idx.ingest(workspace_id="w", filename="a.txt", data=b"x", metadata={"k": "m" * 200})
    with pytest.raises(DocumentLimitError):
        await idx.ingest(workspace_id="w", filename="a.txt", data=b"x", source_uri="u" * 51)


async def test_vector_scan_is_bounded_by_max_scan_newest_first() -> None:
    idx = make_index(max_scan=2)
    for i in range(4):
        await idx.ingest(workspace_id="w", filename=f"d{i}.txt", data=f"zebra{i} shared".encode())
    hits = await idx.search(workspace_id="w", query="shared", keyword_weight=0, top_k=10)
    assert {h.title for h in hits} <= {"d2.txt", "d3.txt"}


@pytest.mark.sqlite_only  # legacy SQLite storage format (JSON text vectors); Postgres never had it
async def test_legacy_json_embeddings_still_score() -> None:
    idx = make_index()
    await idx.ingest(workspace_id="w", filename="a.txt", data=b"heron marsh")
    blob = idx._conn.execute("SELECT embedding FROM hk_chunks").fetchone()[0]
    from nexura_runtime.knowledge.hybrid import _unpack

    idx._conn.execute("UPDATE hk_chunks SET embedding = ?", (json.dumps(_unpack(blob)),))
    assert await idx.search(workspace_id="w", query="heron marsh", keyword_weight=0)


async def test_reingest_same_content_updates_uri_and_metadata_without_new_chunks() -> None:
    idx = make_index()
    first = await idx.ingest(
        workspace_id="w", filename="a.txt", data=b"hello world", source_uri="https://o"
    )
    out = await idx.ingest(
        workspace_id="w",
        filename="a.txt",
        data=b"hello world",
        source_uri="https://n",
        metadata={"v": 2},
    )
    assert out.deduplicated and out.metadata_updated
    assert out.document.source_uri == "https://n" and out.document.metadata == {"v": 2}
    assert out.document.document_id == first.document.document_id
    assert kb.count_chunks(idx) == first.document.chunk_count
    again = await idx.ingest(
        workspace_id="w", filename="a.txt", data=b"hello world", source_uri="https://n"
    )
    assert again.deduplicated and not again.metadata_updated


async def test_pdf_less_chunks_have_no_page() -> None:
    idx = make_index()
    await idx.ingest(workspace_id="w", filename="a.txt", data=b"heron marsh")
    hit = (await idx.search(workspace_id="w", query="heron"))[0]
    assert hit.citation.page_start is None


@pytest.mark.parametrize("attempt", range(8))
async def test_fused_tie_prefers_the_keyword_match(
    monkeypatch: pytest.MonkeyPatch, attempt: int
) -> None:
    """A chunk found only by a weak vector match must not outrank an exact keyword match that
    has the same fused score (seen in Studio: a Hindi guide ranked above 'quokka.html').

    Chunk ids are random, so several attempts guard against a lucky id-order tie-break.
    On Postgres the similarity is computed in SQL, so the same vector ranking is set up with
    stored vectors instead of a patched scorer (cosine 1 for the Hindi chunk, 0 for quokka).
    """
    import nexura_runtime.knowledge.hybrid as hybrid

    index = make_index()
    await ingest(index, "hindi_guide.txt", "नेक्सुरा एजेंटकिट एक स्वतंत्र एसडीके है", ws="w1")
    await ingest(index, "quokka.html", "Quokka habitat notes for fusion.", ws="w1")
    if kb.on_postgres(index):
        await kb.pin_vector_scores(
            index, query="quokka", similar="hindi_guide.txt", dissimilar=["quokka.html"]
        )
    else:
        hindi_blob = index._conn.execute(  # type: ignore[attr-defined]
            "SELECT c.embedding FROM hk_chunks c JOIN hk_documents d "
            "ON d.document_id = c.document_id WHERE d.title = 'hindi_guide.txt'"
        ).fetchone()[0]

        # Vector ranker: only the Hindi chunk is similar; the quokka chunk is below the floor.
        def scores(qvec: list[float], blobs: list[bytes | str]) -> list[float]:
            return [0.9 if b == hindi_blob else 0.0 for b in blobs]

        monkeypatch.setattr(hybrid, "_cosine_scores", scores)
    hits = await index.search(workspace_id="w1", query="quokka", top_k=5)
    assert [h.title for h in hits][0] == "quokka.html", [h.title for h in hits]
