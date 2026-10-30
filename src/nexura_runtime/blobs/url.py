# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Blob store location from configuration: ``NEXURA_BLOB_URL`` or ``blob_url=``.

- ``gs://<bucket>[/<prefix>]``: Google Cloud Storage (credentials from ADC, never from the URL);
- ``file:///<absolute path>``: a local directory.

Like ``storage_url.normalize_database_url``, this module imports no optional client library.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from nexura_runtime.blobs.base import BlobStore

BLOB_URL_ENV = "NEXURA_BLOB_URL"

_BUCKET = re.compile(r"[a-z0-9][a-z0-9_.-]{1,61}[a-z0-9]")
_PREFIX_SEGMENT = re.compile(r"[A-Za-z0-9_.-]{1,128}")


@dataclass(frozen=True)
class BlobLocation:
    scheme: str  # "gs" or "file"
    bucket: str = ""
    prefix: str = ""
    path: str = ""

    def describe(self) -> str:
        if self.scheme == "gs":
            return f"gs://{self.bucket}/{self.prefix}" if self.prefix else f"gs://{self.bucket}"
        return Path(self.path).as_uri()


def parse_blob_url(url: str) -> BlobLocation:
    """Parse and validate a blob URL; ``ValueError`` names the rule that failed."""
    if not isinstance(url, str) or not url.strip():
        raise ValueError("blob URL must be gs://<bucket>[/<prefix>] or file:///<absolute path>")
    parts = urlsplit(url.strip())
    if parts.query or parts.fragment:
        raise ValueError("blob URL must not have a query or fragment")
    if parts.username is not None or parts.password is not None:
        raise ValueError("blob URL must not contain credentials (GCS uses ADC)")
    if parts.scheme == "gs":
        bucket = parts.netloc
        if not _BUCKET.fullmatch(bucket) or ".." in bucket:
            raise ValueError("gs:// blob URL needs a valid bucket name")
        raw = parts.path.strip("/")
        segments = raw.split("/") if raw else []
        for segment in segments:
            if segment in {".", ".."} or not _PREFIX_SEGMENT.fullmatch(segment):
                raise ValueError(
                    "gs:// blob URL prefix segments may only use letters, digits, '_', '.', '-' "
                    "(no '.' or '..' segments)"
                )
        return BlobLocation(scheme="gs", bucket=bucket, prefix="/".join(segments))
    if parts.scheme == "file":
        if parts.netloc not in {"", "localhost"}:
            raise ValueError("file:// blob URL must name a local path (file:///abs/path)")
        path = unquote(parts.path)
        if not path.startswith("/") or "\x00" in path:
            raise ValueError("file:// blob URL needs an absolute path")
        if any(segment == ".." for segment in path.split("/")):
            raise ValueError("file:// blob URL must not contain '..'")
        return BlobLocation(scheme="file", path=path)
    raise ValueError("blob URL must be gs://<bucket>[/<prefix>] or file:///<absolute path>")


def resolve_blob_url(blob_url: str | None) -> str | None:
    """``blob_url`` if given, else ``NEXURA_BLOB_URL``; ``None`` when neither is set."""
    url = blob_url if blob_url is not None else os.environ.get(BLOB_URL_ENV)
    return url or None


def open_blob_store(location: BlobLocation | str, *, client: Any = None) -> BlobStore:
    """The store for ``location``. A ``gs://`` store without ``client`` checks that the client
    library is installed now (the client itself is created on first use)."""
    if isinstance(location, str):
        location = parse_blob_url(location)
    if location.scheme == "gs":
        from nexura_runtime.blobs.gcs import GCSBlobStore, import_storage_module

        if client is None:
            import_storage_module()
        return GCSBlobStore(location.bucket, location.prefix, client=client)
    from nexura_runtime.blobs.local import LocalBlobStore

    return LocalBlobStore(location.path)
