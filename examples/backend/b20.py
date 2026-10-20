"""Standalone demonstration for Module B20: Document Ingestion and Parsers.

Runs 100% keylessly and offline:
  uv run python examples/backend/b20.py
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import zipfile

import pypdf
from nexura_runtime.contracts.context import ExecutionContext

from nexura_runtime.knowledge.ingest import (
    DecompressionBombError,
    DocumentIngestModule,
    EncryptedDocumentError,
    IngestionPipeline,
    MalformedDocumentError,
    OcrRequiredError,
    OversizedDocumentError,
)


def _make_synthetic_pdf(pages_text: list[str]) -> bytes:
    """Generate a clean, valid multi-page PDF with Helvetica font in memory."""
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

    # Page objects (3 .. 3+N-1)
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

    # Content streams (3+N .. 3+2N-1)
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


def _make_encrypted_pdf() -> bytes:
    """Generate an encrypted PDF that requires a password."""
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.encrypt("secret_pass")
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _make_scanned_pdf() -> bytes:
    """Generate a raster/scanned PDF containing zero extractable text."""
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def main() -> None:
    print("=== Module B20: Document Ingestion and Parsers Demonstration ===\n")

    pipeline = IngestionPipeline(
        max_file_size_bytes=10_000_000,
        max_pages=100,
        max_expansion_bytes=2_000_000,
    )

    # 1. Ingest Plain Text with Unicode NFC normalization
    print("1. Ingesting plain text document with Indic and accented Unicode...")
    text_content = (
        "Project Architecture Overview\n\n"
        "Nexura SDK provides resilient backend orchestration.\n"
        "Multi-lingual text support: हिंदी भाषा, தமிழ், Español, Français.\n"
        "\x0c"  # Form feed to demonstrate logical page split
        "Section 2: High Availability Infrastructure\n"
        "Storage uses compare-and-swap concurrency."
    )
    doc_text = pipeline.ingest_bytes(
        text_content.encode("utf-8"),
        filename="architecture_overview.txt",
        collection_id="tech_docs",
    )
    print(f"  [SUCCESS] Document ID: {doc_text.document_id}")
    print(f"  Origin: {doc_text.origin}")
    print(f"  MIME Type: {doc_text.mime_type}")
    print(f"  SHA-256 Hash: {doc_text.content_hash[:16]}...")
    print(f"  Total Pages Extracted: {len(doc_text.pages)}")
    for p in doc_text.pages:
        print(f"   - Page {p.page_number} (span: [{p.span_start}:{p.span_end}]): {p.text[:40]}...")

    # 2. Ingest CSV Dataset
    print("\n2. Ingesting structured CSV dataset...")
    csv_content = (
        "server_id,datacenter,status,load_pct\n"
        "srv-01,us-east,active,42.5\n"
        "srv-02,us-west,standby,10.0\n"
        "srv-03,eu-central,active,78.2\n"
        "srv-04,ap-south,active,55.1\n"
    )
    doc_csv = pipeline.ingest_bytes(
        csv_content.encode("utf-8"),
        filename="fleet_metrics.csv",
        collection_id="operations",
    )
    print(f"  [SUCCESS] Document ID: {doc_csv.document_id}")
    print(f"  Total Rows Extracted: {doc_csv.metadata.get('total_rows')}")
    print(f"  Columns: {doc_csv.metadata.get('columns')}")
    print(f"  Extracted Text Snippet:\n    {doc_csv.text.splitlines()[0]}")
    print(f"    {doc_csv.text.splitlines()[1]}")

    # 3. Ingest Multi-Page PDF with Page Spans
    print("\n3. Ingesting multi-page PDF document...")
    pdf_bytes = _make_synthetic_pdf(
        [
            "Nexura Q3 Executive Financial Summary. Total Revenue: $4.2M.",
            "Platform Scaling Metrics. Active Agents: 125,000 across 4 regions.",
        ]
    )
    doc_pdf = pipeline.ingest_bytes(
        pdf_bytes,
        filename="executive_summary.pdf",
        collection_id="reports",
    )
    print(f"  [SUCCESS] Document ID: {doc_pdf.document_id}")
    print(f"  Page Count: {doc_pdf.metadata.get('page_count')}")
    for p in doc_pdf.pages:
        print(f"   - Page {p.page_number} Citation (span: [{p.span_start}:{p.span_end}]): {p.text}")

    # 4. Verify Same-Digest Idempotency
    print("\n4. Verifying same-digest ingestion idempotency...")
    doc_pdf_again = pipeline.ingest_bytes(
        pdf_bytes,
        filename="executive_summary_copy.pdf",
        collection_id="reports",
    )
    assert doc_pdf.document_id == doc_pdf_again.document_id
    print(f"  [SUCCESS] Exact duplicate recognized! Returned existing doc {doc_pdf.document_id}")
    print(f"  Total documents in 'reports': {len(pipeline.list_documents('reports'))}")

    # 5. Verify Decompression Bomb Protection
    print("\n5. Verifying decompression bomb boundary...")
    bomb_buf = io.BytesIO()
    with zipfile.ZipFile(bomb_buf, "w", compression=zipfile.ZIP_DEFLATED) as z:
        # Create a compressed file with 5 MB uncompressed, exceeding pipeline 2 MB limit
        z.writestr("large_zeroes.txt", b"0" * 5_000_000)
    bomb_bytes = bomb_buf.getvalue()

    try:
        pipeline.ingest_bytes(bomb_bytes, filename="archive.zip", collection_id="archives")
        print("  [FAIL] Expected DecompressionBombError was not raised!")
    except DecompressionBombError as exc:
        print(f"  [SUCCESS] Decompression bomb bounded and halted: {exc}")

    # 6. Verify Explicit Rejection of Encrypted, Malformed, and Scanned Files
    print("\n6. Verifying handling of encrypted, malformed, and scanned documents...")

    # Encrypted PDF
    try:
        pipeline.ingest_bytes(_make_encrypted_pdf(), filename="protected.pdf")
        print("  [FAIL] Expected EncryptedDocumentError was not raised!")
    except EncryptedDocumentError as exc:
        print(f"  [SUCCESS] Encrypted document rejected explicitly: {exc}")

    # Malformed PDF
    try:
        pipeline.ingest_bytes(b"%PDF-1.4 corrupt byte stream garbage", filename="broken.pdf")
        print("  [FAIL] Expected MalformedDocumentError was not raised!")
    except MalformedDocumentError as exc:
        print(f"  [SUCCESS] Malformed document rejected explicitly: {exc}")

    # Scanned PDF (no text streams)
    try:
        pipeline.ingest_bytes(_make_scanned_pdf(), filename="scanned.pdf")
        print("  [FAIL] Expected OcrRequiredError was not raised!")
    except OcrRequiredError as exc:
        print(f"  [SUCCESS] Scanned PDF identified as requiring OCR: {exc}")

    # Oversized File
    try:
        pipeline.ingest_bytes(b"A" * 15_000_000, filename="huge.txt")
        print("  [FAIL] Expected OversizedDocumentError was not raised!")
    except OversizedDocumentError as exc:
        print(f"  [SUCCESS] Oversized document rejected explicitly: {exc}")

    # 7. Verify Transactional Batch Rollback (No Half-Ready Collection)
    print("\n7. Verifying transactional batch rollback on parser error...")
    batch_items = [
        {"data": b"Valid document 1 text", "filename": "doc1.txt"},
        {"data": _make_encrypted_pdf(), "filename": "doc2_encrypted.pdf"},  # will fail!
        {"data": b"Valid document 3 text", "filename": "doc3.txt"},
    ]
    try:
        pipeline.ingest_batch(batch_items, collection_id="tx_test")
        print("  [FAIL] Batch ingestion should have failed!")
    except EncryptedDocumentError:
        print("  [SUCCESS] Batch failed as expected due to encrypted item.")

    # Verify collection has ZERO documents
    remaining = pipeline.list_documents("tx_test")
    assert len(remaining) == 0
    print(
        f"  [SUCCESS] Collection 'tx_test' contains {len(remaining)} documents (zero partial state)"
    )

    # 8. B00/B01 Module and CLI Invocation
    print("\n8. Executing DocumentIngestModule via Python contract...")
    import asyncio

    mod = DocumentIngestModule(pipeline)
    ctx = ExecutionContext(run_id="run_ingest_demo", node_id="ingest_step")
    res = asyncio.run(
        mod.execute(
            ctx,
            inputs={"content": "Direct module ingest test content", "filename": "direct.txt"},
            config={},
        )
    )
    print(f"  Module Status: {res.status}")
    print(f"  Document ID: {res.outputs['document_id']}")
    print(f"  Extracted Text: {res.outputs['text']}")

    # 9. B01 CLI Subprocess Invocation
    print("\n9. Invoking document ingest module via B01 CLI subprocess...")
    cmd = [
        sys.executable,
        "-m",
        "nexura_runtime.modules",
        "run",
        "nexura.knowledge.ingest",
        "--input",
        json.dumps(
            {
                "content": "Release notes for Nexura v0.3.0: Full pipeline verified.",
                "filename": "release_notes.txt",
                "collection_id": "releases",
            }
        ),
    ]
    cli_proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    print(f"  CLI Exit Code: {cli_proc.returncode}")
    if cli_proc.returncode == 0:
        print(f"  CLI Output: {cli_proc.stdout.strip()}")
    else:
        print(f"  CLI Error: {cli_proc.stderr.strip()}")

    print("\n=== All B20 Demonstration Steps Completed Successfully ===")


if __name__ == "__main__":
    main()
