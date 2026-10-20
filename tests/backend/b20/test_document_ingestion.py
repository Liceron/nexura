"""Comprehensive unit and acceptance tests for Module B20: Document Ingestion and Parsers."""

from __future__ import annotations

import io
import zipfile

import pypdf
import pytest
from nexura_runtime.contracts.context import ExecutionContext

from nexura_runtime.knowledge.ingest import (
    DecompressionBombError,
    DocumentIngestModule,
    EncryptedDocumentError,
    IngestionPipeline,
    MalformedDocumentError,
    OcrRequiredError,
    OversizedDocumentError,
    detect_mime_type,
    normalize_text,
)


def _build_test_pdf(pages_text: list[str]) -> bytes:
    """Helper to generate a clean, valid multi-page PDF in memory."""
    buf = io.BytesIO()
    buf.write(b"%PDF-1.4\n")
    n = len(pages_text)
    font_id = 3 + 2 * n

    obj_offsets: list[int] = []

    # 1: Catalog
    obj_offsets.append(buf.tell())
    buf.write(b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n")

    # 2: Pages
    obj_offsets.append(buf.tell())
    kids = " ".join(f"{3 + i} 0 R" for i in range(n))
    buf.write(f"2 0 obj\n<< /Type /Pages /Kids [{kids}] /Count {n} >>\nendobj\n".encode("ascii"))

    # Page objects
    for i in range(n):
        obj_offsets.append(buf.tell())
        p_id = 3 + i
        c_id = 3 + n + i
        buf.write(
            f"{p_id} 0 obj\n"
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 400 400] "
            f"/Contents {c_id} 0 R /Resources << /Font << /F1 {font_id} 0 R >> >> >>\n"
            f"endobj\n".encode("ascii")
        )

    # Content streams
    for i, txt in enumerate(pages_text):
        obj_offsets.append(buf.tell())
        c_id = 3 + n + i
        stream_data = f"BT /F1 12 Tf 50 300 Td ({txt}) Tj ET\n".encode()
        buf.write(f"{c_id} 0 obj\n<< /Length {len(stream_data)} >>\nstream\n".encode("ascii"))
        buf.write(stream_data)
        buf.write(b"endstream\nendobj\n")

    # Font object
    obj_offsets.append(buf.tell())
    buf.write(
        f"{font_id} 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n".encode(
            "ascii"
        )
    )

    # Cross-reference table
    xref_offset = buf.tell()
    total_objs = font_id + 1
    buf.write(f"xref\n0 {total_objs}\n0000000000 65535 f \n".encode("ascii"))
    for off in obj_offsets:
        buf.write(f"{off:010d} 00000 n \n".encode("ascii"))

    buf.write(
        f"trailer\n<< /Size {total_objs} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode(
            "ascii"
        )
    )
    return buf.getvalue()


def _build_encrypted_pdf() -> bytes:
    """Helper to generate an encrypted PDF."""
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.encrypt("pass123")
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _build_scanned_pdf() -> bytes:
    """Helper to generate a raster PDF with no text streams."""
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=100, height=100)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


class TestDocumentIngestionAcceptance:
    """Acceptance test suite for Module B20: Document Ingestion and Parsers."""

    def test_mime_type_detection(self) -> None:
        """Detector must accurately identify formats from magic bytes and extensions."""
        assert detect_mime_type(b"%PDF-1.4...", "doc.pdf") == "application/pdf"
        assert detect_mime_type(b"PK\x03\x04...", "file.zip") == "application/zip"
        assert detect_mime_type(b"<html><body>test</body></html>", "index.html") == "text/html"
        assert detect_mime_type(b'{"key": "value"}', "data.json") == "application/json"
        assert detect_mime_type(b"a,b,c\n1,2,3", "table.csv") == "text/csv"
        assert detect_mime_type(b"plain text content", "notes.txt") == "text/plain"

    def test_malformed_files_explicit(self) -> None:
        """Malformed or corrupt files must raise explicit MalformedDocumentError."""
        pipeline = IngestionPipeline()

        # Corrupt PDF
        with pytest.raises(MalformedDocumentError) as exc_info:
            pipeline.ingest_bytes(b"%PDF-1.4 corrupt header garbage", filename="corrupt.pdf")
        assert exc_info.value.filename == "corrupt.pdf"
        assert exc_info.value.mime_type == "application/pdf"

        # Corrupt JSON
        with pytest.raises(MalformedDocumentError) as exc_info:
            pipeline.ingest_bytes(b'{"unclosed_key": 123', filename="corrupt.json")
        assert exc_info.value.filename == "corrupt.json"
        assert exc_info.value.mime_type == "application/json"

    def test_encrypted_files_explicit(self) -> None:
        """Encrypted PDF documents must raise explicit EncryptedDocumentError."""
        pipeline = IngestionPipeline()

        with pytest.raises(EncryptedDocumentError) as exc_info:
            pipeline.ingest_bytes(_build_encrypted_pdf(), filename="confidential.pdf")

        assert exc_info.value.filename == "confidential.pdf"
        assert exc_info.value.mime_type == "application/pdf"

    def test_oversized_files_explicit(self) -> None:
        """Documents exceeding byte size or page count limits must raise OversizedDocumentError."""
        pipeline = IngestionPipeline(max_file_size_bytes=1000, max_pages=2)

        # File size exceeded
        with pytest.raises(OversizedDocumentError) as exc_info:
            pipeline.ingest_bytes(b"A" * 1500, filename="large.txt")
        assert exc_info.value.limit_name == "file_size"
        assert exc_info.value.actual_value == 1500
        assert exc_info.value.max_allowed == 1000

        # Page count exceeded
        pipeline_pages = IngestionPipeline(max_file_size_bytes=100_000, max_pages=2)
        three_page_pdf = _build_test_pdf(["Page 1", "Page 2", "Page 3"])
        with pytest.raises(OversizedDocumentError) as exc_info:
            pipeline_pages.ingest_bytes(three_page_pdf, filename="three_pages.pdf")
        assert exc_info.value.limit_name == "page_count"
        assert exc_info.value.actual_value == 3
        assert exc_info.value.max_allowed == 2

    def test_scanned_pdf_ocr_required_explicit(self) -> None:
        """Scanned or raster-only PDFs with no text streams must raise OcrRequiredError."""
        pipeline = IngestionPipeline()

        with pytest.raises(OcrRequiredError) as exc_info:
            pipeline.ingest_bytes(_build_scanned_pdf(), filename="scanned_receipt.pdf")

        assert exc_info.value.filename == "scanned_receipt.pdf"
        assert exc_info.value.total_pages == 1

    def test_decompression_bombs_bounded(self) -> None:
        """Zip archives exceeding expansion bytes limits must raise DecompressionBombError."""
        pipeline = IngestionPipeline(max_expansion_bytes=100_000)

        # Construct a zip with 500,000 bytes uncompressed
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as z:
            z.writestr("huge_file.txt", b"x" * 500_000)
        bomb_bytes = buf.getvalue()

        with pytest.raises(DecompressionBombError) as exc_info:
            pipeline.ingest_bytes(bomb_bytes, filename="bomb.zip")

        assert exc_info.value.filename == "bomb.zip"
        assert exc_info.value.uncompressed_size >= 500_000
        assert exc_info.value.max_expansion_bytes == 100_000

    def test_same_digest_ingest_is_idempotent(self) -> None:
        """Ingesting the same binary content must be idempotent and return existing document."""
        pipeline = IngestionPipeline()
        pdf_data = _build_test_pdf(["Section 1: Introduction", "Section 2: Analysis"])

        doc1 = pipeline.ingest_bytes(pdf_data, filename="report_v1.pdf", collection_id="finance")
        doc2 = pipeline.ingest_bytes(pdf_data, filename="report_copy.pdf", collection_id="finance")

        # Same document ID returned
        assert doc1.document_id == doc2.document_id
        assert doc1.content_hash == doc2.content_hash
        assert len(pipeline.list_documents("finance")) == 1

    def test_page_citations_survive(self) -> None:
        """Ingested documents must maintain accurate page citations and text spans."""
        pipeline = IngestionPipeline()
        p1 = "First page summary of quarterly revenue."
        p2 = "Second page detailed regional breakdown."
        pdf_data = _build_test_pdf([p1, p2])

        doc = pipeline.ingest_bytes(pdf_data, filename="financials.pdf")

        assert len(doc.pages) == 2
        page_1 = doc.pages[0]
        page_2 = doc.pages[1]

        assert page_1.page_number == 1
        assert page_1.text == p1
        assert doc.text[page_1.span_start : page_1.span_end] == p1

        assert page_2.page_number == 2
        assert page_2.text == p2
        assert doc.text[page_2.span_start : page_2.span_end] == p2

    def test_parser_failure_leaves_no_half_ready_collection(self) -> None:
        """Batch ingestion failure must rollback transactionally, leaving zero partial records."""
        pipeline = IngestionPipeline()

        batch = [
            {"data": b"Valid document 1 text", "filename": "doc1.txt"},
            {"data": _build_encrypted_pdf(), "filename": "doc2_encrypted.pdf"},  # will fail
            {"data": b"Valid document 3 text", "filename": "doc3.txt"},
        ]

        with pytest.raises(EncryptedDocumentError):
            pipeline.ingest_batch(batch, collection_id="strict_coll")

        # Zero documents in collection
        assert len(pipeline.list_documents("strict_coll")) == 0

    def test_html_parser_offline_security(self) -> None:
        """HTML parser must extract clean text without fetching remote assets or scripts."""
        pipeline = IngestionPipeline()
        html_content = (
            "<!DOCTYPE html><html><head><script>alert('xss');</script>"
            "<style>body { color: red; }</style></head>"
            "<body><h1>Documentation Portal</h1><p>Welcome to <b>Nexura</b>!</p>"
            "<img src='https://malicious-tracking.com/pixel.png' /></body></html>"
        )
        doc = pipeline.ingest_bytes(html_content.encode("utf-8"), filename="portal.html")

        assert "Documentation Portal" in doc.text
        assert "Welcome to Nexura" in doc.text
        assert "alert" not in doc.text
        assert "color: red" not in doc.text

    def test_unicode_normalization_preserves_characters(self) -> None:
        """Normalization must convert to NFC without corrupting Indic or accented text."""
        raw_text = "हिंदी पाठ और तमिल உரை, Español, Français \r\nNew line."
        norm = normalize_text(raw_text)
        assert "\r" not in norm
        assert "हिंदी पाठ" in norm
        assert "உரை" in norm
        assert "Español" in norm

    @pytest.mark.asyncio
    async def test_document_ingest_module_contract(self) -> None:
        """DocumentIngestModule must conform to B00/B01 contract and execute cleanly."""
        pipeline = IngestionPipeline()
        module = DocumentIngestModule(pipeline)

        assert module.manifest.kind == "knowledge"
        assert module.manifest.module_id == "nexura.knowledge.ingest"
        assert module.manifest.effect_policy == "idempotent_write"

        ctx = ExecutionContext(workspace_id="ws_test", run_id="run_ingest_1", node_id="ingest_step")
        res = await module.execute(
            ctx,
            inputs={"content": "Direct module ingest test content", "filename": "module.txt"},
            config={},
        )

        assert res.status == "success"
        assert res.outputs["mime_type"] == "text/plain"
        assert res.outputs["pages_count"] == 1
        assert "Direct module ingest" in res.outputs["text"]
