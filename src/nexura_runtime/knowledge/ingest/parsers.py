# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Extensible document parsers for text, CSV, JSON, HTML, PDF, and archives."""

from __future__ import annotations

import csv
import html.parser
import io
import json
import unicodedata
import zipfile
from dataclasses import dataclass, field
from typing import Any

from nexura_runtime.knowledge.ingest.errors import (
    DecompressionBombError,
    EncryptedDocumentError,
    MalformedDocumentError,
    OcrRequiredError,
    OversizedDocumentError,
    UnsupportedMimeTypeError,
)
from nexura_runtime.knowledge.ingest.models import DocumentPage


def normalize_text(text: str) -> str:
    """Normalize text into Unicode NFC representation and clean control characters."""
    if not text:
        return ""
    # Standardize newline characters
    clean = text.replace("\r\n", "\n").replace("\r", "\n")
    # Normalize Unicode to canonical decomposition / composition (NFC)
    clean = unicodedata.normalize("NFC", clean)
    # Strip non-printable control characters, preserving whitespace and newlines
    filtered = "".join(
        ch
        for ch in clean
        if ch in ("\n", "\t", "\f") or not unicodedata.category(ch).startswith("C")
    )
    return filtered


@dataclass
class ParsedContent:
    """Outcome of parsing a raw document binary."""

    text: str
    pages: list[DocumentPage] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


class BaseParser:
    """Abstract base class for document format parsers."""

    def parse(
        self,
        data: bytes,
        *,
        filename: str = "",
        max_pages: int = 500,
        max_expansion_bytes: int = 50_000_000,
    ) -> ParsedContent:
        raise NotImplementedError


class PlainTextParser(BaseParser):
    """Parser for plain text, Markdown, and source files."""

    def parse(
        self,
        data: bytes,
        *,
        filename: str = "",
        max_pages: int = 500,
        max_expansion_bytes: int = 50_000_000,
    ) -> ParsedContent:
        try:
            raw_text = data.decode("utf-8")
        except UnicodeDecodeError:
            try:
                raw_text = data.decode("latin-1")
            except Exception as exc:
                raise MalformedDocumentError(filename, "text/plain", str(exc)) from exc

        normalized = normalize_text(raw_text)

        # Detect form-feed page boundaries if present
        raw_pages = normalized.split("\x0c") if "\x0c" in normalized else [normalized]
        pages: list[DocumentPage] = []
        current_span = 0

        for idx, p_text in enumerate(raw_pages, start=1):
            span_start = current_span
            span_end = current_span + len(p_text)
            pages.append(
                DocumentPage(
                    page_number=idx,
                    text=p_text,
                    span_start=span_start,
                    span_end=span_end,
                )
            )
            current_span = span_end + 1

        return ParsedContent(text=normalized, pages=pages)


class CsvParser(BaseParser):
    """Parser for CSV datasets with row-indexed page structuring."""

    def parse(
        self,
        data: bytes,
        *,
        filename: str = "",
        max_pages: int = 500,
        max_expansion_bytes: int = 50_000_000,
    ) -> ParsedContent:
        try:
            raw_text = data.decode("utf-8")
        except UnicodeDecodeError:
            try:
                raw_text = data.decode("latin-1")
            except Exception as exc:
                raise MalformedDocumentError(filename, "text/csv", str(exc)) from exc

        try:
            reader = csv.reader(io.StringIO(raw_text))
            rows = list(reader)
        except Exception as exc:
            raise MalformedDocumentError(filename, "text/csv", str(exc)) from exc

        if not rows:
            return ParsedContent(text="", pages=[])

        headers = rows[0]
        data_rows = rows[1:]

        formatted_lines: list[str] = [", ".join(headers)]
        for row in data_rows:
            formatted_lines.append(", ".join(row))

        full_text = normalize_text("\n".join(formatted_lines))

        # Chunk into logical pages of 100 rows
        chunk_size = 100
        pages: list[DocumentPage] = []
        span_cursor = 0

        total_chunks = max(1, (len(data_rows) + chunk_size - 1) // chunk_size)
        if total_chunks > max_pages:
            raise OversizedDocumentError(filename, "page_count", total_chunks, max_pages)

        for page_idx in range(1, total_chunks + 1):
            start_row = (page_idx - 1) * chunk_size
            end_row = start_row + chunk_size
            page_slice = [", ".join(headers)] + [", ".join(r) for r in data_rows[start_row:end_row]]
            page_text = normalize_text("\n".join(page_slice))
            span_start = span_cursor
            span_end = span_cursor + len(page_text)
            pages.append(
                DocumentPage(
                    page_number=page_idx,
                    text=page_text,
                    span_start=span_start,
                    span_end=span_end,
                    metadata={"row_start": start_row, "row_end": min(end_row, len(data_rows))},
                )
            )
            span_cursor = span_end

        return ParsedContent(
            text=full_text,
            pages=pages,
            metadata={"total_rows": len(data_rows), "columns": headers},
        )


class JsonParser(BaseParser):
    """Parser for JSON structures."""

    def parse(
        self,
        data: bytes,
        *,
        filename: str = "",
        max_pages: int = 500,
        max_expansion_bytes: int = 50_000_000,
    ) -> ParsedContent:
        try:
            parsed = json.loads(data.decode("utf-8"))
        except Exception as exc:
            raise MalformedDocumentError(filename, "application/json", str(exc)) from exc

        formatted = normalize_text(json.dumps(parsed, indent=2, ensure_ascii=False))
        pages = [
            DocumentPage(
                page_number=1,
                text=formatted,
                span_start=0,
                span_end=len(formatted),
            )
        ]
        return ParsedContent(text=formatted, pages=pages)


class _HTMLTextExtractor(html.parser.HTMLParser):
    """Local HTML text extractor that ignores scripts, styles, and never fetches URLs."""

    def __init__(self) -> None:
        super().__init__()
        self._ignore_tags = {"script", "style", "head", "meta", "link"}
        self._current_ignored = 0
        self.text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in self._ignore_tags:
            self._current_ignored += 1
        elif tag.lower() in ("p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "br"):
            self.text_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in self._ignore_tags and self._current_ignored > 0:
            self._current_ignored -= 1
        elif tag.lower() in ("p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li"):
            self.text_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._current_ignored == 0:
            cleaned = data.strip()
            if cleaned:
                self.text_parts.append(cleaned + " ")


class HtmlParser(BaseParser):
    """Local offline parser for HTML documents."""

    def parse(
        self,
        data: bytes,
        *,
        filename: str = "",
        max_pages: int = 500,
        max_expansion_bytes: int = 50_000_000,
    ) -> ParsedContent:
        try:
            raw_text = data.decode("utf-8")
        except UnicodeDecodeError:
            try:
                raw_text = data.decode("latin-1")
            except Exception as exc:
                raise MalformedDocumentError(filename, "text/html", str(exc)) from exc

        extractor = _HTMLTextExtractor()
        try:
            extractor.feed(raw_text)
            extracted = "".join(extractor.text_parts)
        except Exception as exc:
            raise MalformedDocumentError(filename, "text/html", str(exc)) from exc

        normalized = normalize_text(extracted)
        pages = [
            DocumentPage(
                page_number=1,
                text=normalized,
                span_start=0,
                span_end=len(normalized),
            )
        ]
        return ParsedContent(text=normalized, pages=pages)


class PdfParser(BaseParser):
    """Parser for PDF documents extracting text, page spans, and enforcing limits."""

    def parse(
        self,
        data: bytes,
        *,
        filename: str = "",
        max_pages: int = 500,
        max_expansion_bytes: int = 50_000_000,
    ) -> ParsedContent:
        try:
            import pypdf
        except ImportError as exc:
            raise UnsupportedMimeTypeError(
                "application/pdf",
                filename=f"{filename} (pypdf extra 'knowledge' is required for PDF parsing)",
            ) from exc

        try:
            reader = pypdf.PdfReader(io.BytesIO(data))
        except Exception as exc:
            raise MalformedDocumentError(filename, "application/pdf", str(exc)) from exc

        # Encrypted check
        if reader.is_encrypted:
            raise EncryptedDocumentError(filename, "application/pdf")

        total_pages = len(reader.pages)
        if total_pages > max_pages:
            raise OversizedDocumentError(filename, "page_count", total_pages, max_pages)

        pages: list[DocumentPage] = []
        all_text_parts: list[str] = []
        span_cursor = 0

        for idx, page in enumerate(reader.pages, start=1):
            try:
                extracted = page.extract_text() or ""
            except Exception as exc:
                raise MalformedDocumentError(
                    filename, "application/pdf", f"Error on page {idx}: {exc}"
                ) from exc

            p_norm = normalize_text(extracted)
            span_start = span_cursor
            span_end = span_cursor + len(p_norm)
            pages.append(
                DocumentPage(
                    page_number=idx,
                    text=p_norm,
                    span_start=span_start,
                    span_end=span_end,
                )
            )
            all_text_parts.append(p_norm)
            span_cursor = span_end + 2

        full_text = "\n\n".join(all_text_parts)

        # Scanned / No-text check
        if not full_text.strip():
            raise OcrRequiredError(filename, total_pages=total_pages)

        return ParsedContent(
            text=full_text,
            pages=pages,
            metadata={"page_count": total_pages},
        )


class ZipArchiveParser(BaseParser):
    """Parser for Zip archives enforcing decompression bomb bounds."""

    def parse(
        self,
        data: bytes,
        *,
        filename: str = "",
        max_pages: int = 500,
        max_expansion_bytes: int = 50_000_000,
    ) -> ParsedContent:
        try:
            zip_file = zipfile.ZipFile(io.BytesIO(data))
        except Exception as exc:
            raise MalformedDocumentError(filename, "application/zip", str(exc)) from exc

        # 1. Fast pre-flight check using uncompressed headers
        declared_total = sum(item.file_size for item in zip_file.infolist())
        if declared_total > max_expansion_bytes:
            raise DecompressionBombError(filename, declared_total, max_expansion_bytes)

        # 2. Streaming decompression check for bomb protection against deceptive headers
        cumulative_decompressed = 0
        extracted_files: dict[str, bytes] = {}

        for item in zip_file.infolist():
            if item.is_dir():
                continue
            with zip_file.open(item) as stream:
                buffer = io.BytesIO()
                while chunk := stream.read(65536):
                    cumulative_decompressed += len(chunk)
                    if cumulative_decompressed > max_expansion_bytes:
                        raise DecompressionBombError(
                            filename, cumulative_decompressed, max_expansion_bytes
                        )
                    buffer.write(chunk)
                extracted_files[item.filename] = buffer.getvalue()

        # Merge extracted text from text-like files inside archive
        pages: list[DocumentPage] = []
        merged_texts: list[str] = []
        cursor = 0

        for item_name, item_bytes in extracted_files.items():
            parser = get_parser_for_mime("text/plain")
            res = parser.parse(item_bytes, filename=item_name, max_pages=max_pages)
            for p in res.pages:
                p_len = len(p.text)
                pages.append(
                    DocumentPage(
                        page_number=len(pages) + 1,
                        text=p.text,
                        span_start=cursor,
                        span_end=cursor + p_len,
                        metadata={"archive_item": item_name},
                    )
                )
                cursor += p_len
            merged_texts.append(f"--- File: {item_name} ---\n" + res.text)

        full_text = "\n\n".join(merged_texts)
        return ParsedContent(
            text=full_text,
            pages=pages,
            metadata={"files_count": len(extracted_files)},
        )


_PARSERS: dict[str, BaseParser] = {
    "text/plain": PlainTextParser(),
    "text/csv": CsvParser(),
    "application/json": JsonParser(),
    "text/html": HtmlParser(),
    "application/pdf": PdfParser(),
    "application/zip": ZipArchiveParser(),
}


def get_parser_for_mime(mime_type: str) -> BaseParser:
    """Resolve the appropriate parser for a given MIME type."""
    parser = _PARSERS.get(mime_type)
    if parser is None:
        raise UnsupportedMimeTypeError(mime_type)
    return parser
