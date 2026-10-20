# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Document ingestion pipeline managing validation, parsing, idempotency, and lineage."""

from __future__ import annotations

import hashlib
import time
import uuid
from typing import Any

from nexura_runtime.knowledge.ingest.detector import detect_mime_type
from nexura_runtime.knowledge.ingest.errors import (
    OversizedDocumentError,
)
from nexura_runtime.knowledge.ingest.models import Document, IngestionJob
from nexura_runtime.knowledge.ingest.parsers import get_parser_for_mime


class IngestionPipeline:
    """Manages document ingestion, idempotency, batch transactions, and collection storage."""

    def __init__(
        self,
        *,
        max_file_size_bytes: int = 20_000_000,
        max_pages: int = 500,
        max_expansion_bytes: int = 50_000_000,
    ) -> None:
        self.max_file_size_bytes = max_file_size_bytes
        self.max_pages = max_pages
        self.max_expansion_bytes = max_expansion_bytes

        # Internal collections store: collection_id -> {document_id -> Document}
        self._collections: dict[str, dict[str, Document]] = {}
        # Hash index for idempotency, scoped per workspace so identical bytes uploaded by
        # different tenants never alias: (workspace_id, collection_id, hash) -> document_id
        self._hash_index: dict[tuple[str, str, str], str] = {}
        # Ingestion jobs audit log
        self._jobs: dict[str, IngestionJob] = {}

    def find_by_hash(
        self, collection_id: str, content_hash: str, workspace_id: str = "default"
    ) -> Document | None:
        """Find an existing non-deleted document by content hash in a workspace's collection."""
        key = (workspace_id, collection_id, content_hash)
        doc_id = self._hash_index.get(key)
        if doc_id is None:
            return None
        coll = self._collections.get(collection_id, {})
        doc = coll.get(doc_id)
        if doc and not doc.is_deleted:
            return doc
        return None

    def ingest_bytes(
        self,
        data: bytes,
        *,
        filename: str = "",
        mime_type: str | None = None,
        collection_id: str = "default",
        metadata: dict[str, Any] | None = None,
        workspace_id: str = "default",
        job_id: str | None = None,
    ) -> Document:
        """Ingest a single document binary with eager limits and idempotent deduplication."""
        # 1. Enforce file size limit
        if len(data) > self.max_file_size_bytes:
            raise OversizedDocumentError(
                filename or "payload", "file_size", len(data), self.max_file_size_bytes
            )

        # 2. Compute canonical content hash
        content_hash = hashlib.sha256(data).hexdigest()

        # 3. Idempotency check: if identical document already exists, return existing
        existing = self.find_by_hash(collection_id, content_hash, workspace_id)
        if existing is not None:
            return existing

        # 4. Resolve MIME type and parser
        effective_mime = mime_type or detect_mime_type(data, filename)
        parser = get_parser_for_mime(effective_mime)

        # 5. Parse document content
        parsed = parser.parse(
            data,
            filename=filename,
            max_pages=self.max_pages,
            max_expansion_bytes=self.max_expansion_bytes,
        )

        # 6. Build Document model
        doc_id = f"doc_{uuid.uuid4().hex[:12]}"
        now = time.time()
        combined_metadata = {**(metadata or {}), **parsed.metadata}
        lineage_data: dict[str, Any] = {
            "workspace_id": workspace_id,
            "job_id": job_id,
            "ingested_at": now,
            "deleted": False,
        }

        doc = Document(
            document_id=doc_id,
            collection_id=collection_id,
            origin=filename or f"bytes_{doc_id}",
            content_hash=content_hash,
            mime_type=effective_mime,
            text=parsed.text,
            pages=parsed.pages,
            metadata=combined_metadata,
            created_at=now,
            lineage=lineage_data,
        )

        # 7. Commit to collection store
        if collection_id not in self._collections:
            self._collections[collection_id] = {}
        self._collections[collection_id][doc_id] = doc
        self._hash_index[(workspace_id, collection_id, content_hash)] = doc_id

        return doc

    def ingest_batch(
        self,
        items: list[dict[str, Any]],
        *,
        collection_id: str = "default",
        workspace_id: str = "default",
    ) -> list[Document]:
        """Ingest a batch of documents transactionally.

        Guarantees that if any item fails validation or parsing, zero items are
        committed, leaving no half-ready collection.
        """
        job_id = f"job_{uuid.uuid4().hex[:10]}"
        job = IngestionJob(
            job_id=job_id,
            workspace_id=workspace_id,
            collection_id=collection_id,
            status="pending",
        )
        self._jobs[job_id] = job

        staged_docs: list[Document] = []
        try:
            for item in items:
                data = item["data"]
                filename = item.get("filename", "")
                mime_type = item.get("mime_type")
                metadata = item.get("metadata")

                # Parse document (uncommitted)
                if len(data) > self.max_file_size_bytes:
                    raise OversizedDocumentError(
                        filename or "payload", "file_size", len(data), self.max_file_size_bytes
                    )

                content_hash = hashlib.sha256(data).hexdigest()
                existing = self.find_by_hash(collection_id, content_hash, workspace_id)
                if existing is not None:
                    staged_docs.append(existing)
                    continue

                effective_mime = mime_type or detect_mime_type(data, filename)
                parser = get_parser_for_mime(effective_mime)
                parsed = parser.parse(
                    data,
                    filename=filename,
                    max_pages=self.max_pages,
                    max_expansion_bytes=self.max_expansion_bytes,
                )

                doc_id = f"doc_{uuid.uuid4().hex[:12]}"
                now = time.time()
                doc = Document(
                    document_id=doc_id,
                    collection_id=collection_id,
                    origin=filename or f"bytes_{doc_id}",
                    content_hash=content_hash,
                    mime_type=effective_mime,
                    text=parsed.text,
                    pages=parsed.pages,
                    metadata={**(metadata or {}), **parsed.metadata},
                    created_at=now,
                    lineage={
                        "workspace_id": workspace_id,
                        "job_id": job_id,
                        "ingested_at": now,
                        "deleted": False,
                    },
                )
                staged_docs.append(doc)

            # Atomic commit only after all items have parsed cleanly
            if collection_id not in self._collections:
                self._collections[collection_id] = {}

            for doc in staged_docs:
                self._collections[collection_id][doc.document_id] = doc
                self._hash_index[(workspace_id, collection_id, doc.content_hash)] = doc.document_id

            job.status = "completed"
            job.document_ids = [d.document_id for d in staged_docs]
            job.completed_at = time.time()
            return staged_docs

        except Exception as exc:
            # Transactional rollback: discard all staged documents
            job.status = "failed"
            job.error = str(exc)
            job.completed_at = time.time()
            raise

    def get_document(self, document_id: str, collection_id: str = "default") -> Document | None:
        """Retrieve document by id, returning None if not found or deleted."""
        coll = self._collections.get(collection_id)
        if not coll:
            return None
        doc = coll.get(document_id)
        if doc and not doc.is_deleted:
            return doc
        return None

    def list_documents(
        self, collection_id: str = "default", *, include_deleted: bool = False
    ) -> list[Document]:
        """List documents in a collection."""
        coll = self._collections.get(collection_id, {})
        return [doc for doc in coll.values() if include_deleted or not doc.is_deleted]

    def delete_document(self, document_id: str, collection_id: str = "default") -> bool:
        """Mark document deleted with lineage tracking."""
        coll = self._collections.get(collection_id)
        if not coll or document_id not in coll:
            return False
        doc = coll[document_id]
        doc.lineage["deleted"] = True
        doc.lineage["deleted_at"] = time.time()
        # Remove from active hash index
        doc_ws = str(doc.lineage.get("workspace_id", "default"))
        self._hash_index.pop((doc_ws, collection_id, doc.content_hash), None)
        return True
