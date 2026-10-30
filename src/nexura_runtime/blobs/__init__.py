# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Blob storage for large write-once data (MVP WP2b).

Large payloads go to a ``BlobStore`` (a local directory, or a Google Cloud Storage bucket); the
database keeps a ``BlobRef`` and a short preview. See ``payloads`` for the column format and
``retention`` for deleting unreferenced and expired blobs.
"""

from __future__ import annotations

from nexura_runtime.blobs.base import (
    BlobInfo,
    BlobIntegrityError,
    BlobNotFoundError,
    BlobRef,
    BlobStore,
    BlobStoreError,
    InvalidBlobKeyError,
)
from nexura_runtime.blobs.local import LocalBlobStore
from nexura_runtime.blobs.payloads import (
    DEFAULT_OFFLOAD_BYTES,
    EVAL_TRACE_NAMESPACE,
    RUN_EVENT_NAMESPACE,
    BlobPayloads,
)
from nexura_runtime.blobs.url import (
    BLOB_URL_ENV,
    BlobLocation,
    open_blob_store,
    parse_blob_url,
)

__all__ = [
    "BLOB_URL_ENV",
    "DEFAULT_OFFLOAD_BYTES",
    "EVAL_TRACE_NAMESPACE",
    "RUN_EVENT_NAMESPACE",
    "BlobInfo",
    "BlobIntegrityError",
    "BlobLocation",
    "BlobNotFoundError",
    "BlobPayloads",
    "BlobRef",
    "BlobStore",
    "BlobStoreError",
    "InvalidBlobKeyError",
    "LocalBlobStore",
    "open_blob_store",
    "parse_blob_url",
]
