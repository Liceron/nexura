# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Typed exception hierarchy for document ingestion and parsing."""

from __future__ import annotations

from nexura_agents.errors import NexuraError


class IngestionError(NexuraError):
    """Base exception for all document ingestion and parser failures."""


class MalformedDocumentError(IngestionError):
    """Raised when a document payload is corrupt, truncated, or unparseable."""

    def __init__(self, filename: str, mime_type: str, reason: str) -> None:
        super().__init__(f"Malformed document '{filename}' ({mime_type}): {reason}")
        self.filename = filename
        self.mime_type = mime_type
        self.reason = reason


class EncryptedDocumentError(IngestionError):
    """Raised when an encrypted or password-protected document cannot be opened."""

    def __init__(self, filename: str, mime_type: str) -> None:
        super().__init__(f"Document '{filename}' ({mime_type}) is encrypted or password-protected.")
        self.filename = filename
        self.mime_type = mime_type


class OversizedDocumentError(IngestionError):
    """Raised when document size or page count exceeds configured limits."""

    def __init__(
        self,
        filename: str,
        limit_name: str,
        actual_value: int | float,
        max_allowed: int | float,
    ) -> None:
        super().__init__(
            f"Document '{filename}' exceeded {limit_name} limit: {actual_value} > {max_allowed}."
        )
        self.filename = filename
        self.limit_name = limit_name
        self.actual_value = actual_value
        self.max_allowed = max_allowed


class DecompressionBombError(IngestionError):
    """Raised when an archive exceeds expansion limits or compression ratio bounds."""

    def __init__(
        self,
        filename: str,
        uncompressed_size: int,
        max_expansion_bytes: int,
    ) -> None:
        super().__init__(
            f"Decompression bomb detected in '{filename}': uncompressed size "
            f"{uncompressed_size} bytes exceeds limit {max_expansion_bytes} bytes."
        )
        self.filename = filename
        self.uncompressed_size = uncompressed_size
        self.max_expansion_bytes = max_expansion_bytes


class OcrRequiredError(IngestionError):
    """Raised when a PDF contains no text streams and requires OCR processing."""

    def __init__(self, filename: str, total_pages: int) -> None:
        super().__init__(
            f"Document '{filename}' contains {total_pages} page(s) with no extractable text "
            f"(scanned or raster-only; OCR is required)."
        )
        self.filename = filename
        self.total_pages = total_pages


class UnsupportedMimeTypeError(IngestionError):
    """Raised when attempting to ingest a file type without an available parser."""

    def __init__(self, mime_type: str, filename: str = "") -> None:
        file_hint = f" for '{filename}'" if filename else ""
        super().__init__(f"Unsupported MIME type '{mime_type}'{file_hint}.")
        self.mime_type = mime_type
        self.filename = filename


class CollectionNotFoundError(IngestionError):
    """Raised when target collection does not exist."""

    def __init__(self, collection_id: str) -> None:
        super().__init__(f"Knowledge collection '{collection_id}' not found.")
        self.collection_id = collection_id
