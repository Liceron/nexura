# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Document ingestion, parsing, and collection management package."""

from __future__ import annotations

from nexura_runtime.knowledge.ingest.detector import detect_mime_type
from nexura_runtime.knowledge.ingest.errors import (
    CollectionNotFoundError,
    DecompressionBombError,
    EncryptedDocumentError,
    IngestionError,
    MalformedDocumentError,
    OcrRequiredError,
    OversizedDocumentError,
    UnsupportedMimeTypeError,
)
from nexura_runtime.knowledge.ingest.models import (
    Document,
    DocumentPage,
    IngestionJob,
)
from nexura_runtime.knowledge.ingest.module import DocumentIngestModule
from nexura_runtime.knowledge.ingest.parsers import (
    BaseParser,
    CsvParser,
    HtmlParser,
    JsonParser,
    PdfParser,
    PlainTextParser,
    ZipArchiveParser,
    get_parser_for_mime,
    normalize_text,
)
from nexura_runtime.knowledge.ingest.pipeline import IngestionPipeline
from nexura_runtime.modules.registry import default_registry

default_ingest_pipeline: IngestionPipeline = IngestionPipeline()
_default_ingest_module = DocumentIngestModule(pipeline=default_ingest_pipeline)

if not default_registry.has(_default_ingest_module.manifest.module_id):
    default_registry.register(_default_ingest_module)

__all__ = [
    "Document",
    "DocumentPage",
    "IngestionJob",
    "detect_mime_type",
    "normalize_text",
    "BaseParser",
    "PlainTextParser",
    "CsvParser",
    "JsonParser",
    "HtmlParser",
    "PdfParser",
    "ZipArchiveParser",
    "get_parser_for_mime",
    "IngestionPipeline",
    "default_ingest_pipeline",
    "DocumentIngestModule",
    "IngestionError",
    "MalformedDocumentError",
    "EncryptedDocumentError",
    "OversizedDocumentError",
    "DecompressionBombError",
    "OcrRequiredError",
    "UnsupportedMimeTypeError",
    "CollectionNotFoundError",
]
