# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""``GCSBlobStore``: blobs as objects in a Google Cloud Storage bucket.

Uses the official ``google-cloud-storage`` client, an optional dependency
(``pip install "nexura-runtime[gcs]"``) imported only when a ``gs://`` store is opened without
an injected client. Credentials always come from Application Default Credentials: on Cloud Run
that is the service's own service account. No key file or key material is read from Nexura
configuration.

Objects are created with ``if_generation_match=0`` (create only), so an existing object is never
overwritten. Reads verify the sha256 and size recorded in the database.
"""

from __future__ import annotations

import importlib
import time
from collections.abc import Iterator
from typing import Any

from nexura_runtime.blobs.base import (
    BlobInfo,
    BlobNotFoundError,
    BlobRef,
    BlobStoreError,
    key_of,
    make_key,
    new_ref,
    new_store_id,
    owner_record_text,
    parse_owner_record,
    validate_database_id,
    validate_prefix,
    validate_store_id,
    verify,
)

GCS_EXTRA_MESSAGE = (
    "gs:// blob storage needs the google-cloud-storage package, which is not installed. "
    'Install it with: pip install "nexura-runtime[gcs]"'
)
DEFAULT_TIMEOUT = 60.0
#: The identity object's name beside the prefix (outside ``<prefix>/``).
GCS_IDENTITY_SUFFIX = "nexura-store-id"
#: The owner object's name beside the prefix: the id of the database that owns the store.
GCS_OWNER_SUFFIX = "nexura-store-owner"


def _status(exc: BaseException) -> int | None:
    """The HTTP status of a ``google.api_core`` error (``NotFound.code == 404``), if any."""
    code = getattr(exc, "code", None)
    return code if isinstance(code, int) else None


def import_storage_module() -> Any:
    """``google.cloud.storage``, or a ``BlobStoreError`` naming the extra to install."""
    try:
        storage = importlib.import_module("google.cloud.storage")
    except ImportError as exc:
        raise BlobStoreError(GCS_EXTRA_MESSAGE) from exc
    return storage


class GCSBlobStore:
    """Blobs in ``gs://<bucket>/<prefix>/<workspace>/<namespace>/<id>``."""

    scheme = "gs"

    def __init__(
        self,
        bucket: str,
        prefix: str = "",
        *,
        client: Any = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.bucket_name = bucket
        self.prefix = prefix.strip("/")
        self.timeout = timeout
        self._client = client
        self._bucket: Any = None
        self._identity: str | None = None

    def describe(self) -> str:
        return (
            f"gs://{self.bucket_name}/{self.prefix}" if self.prefix else f"gs://{self.bucket_name}"
        )

    @property
    def client(self) -> Any:
        if self._client is None:
            # ADC only: Cloud Run's service account, or `gcloud auth application-default login`.
            self._client = import_storage_module().Client()
        return self._client

    @property
    def bucket(self) -> Any:
        if self._bucket is None:
            self._bucket = self.client.bucket(self.bucket_name)
        return self._bucket

    def identity(self, *, create: bool = True) -> str | None:
        """This store's id, created on first use.

        It is kept *beside* the prefix (``<prefix>.nexura-store-id``, or ``nexura-store-id``
        without a prefix), so a bucket lifecycle rule on ``<prefix>/`` never deletes it.
        """
        if self._identity is not None:
            return self._identity
        blob = self.bucket.blob(self.identity_object)
        try:
            self._identity = validate_store_id(
                bytes(blob.download_as_bytes(timeout=self.timeout)).decode("ascii").strip()
            )
            return self._identity
        except Exception as exc:
            if _status(exc) != 404:
                raise
        if not create:
            return None
        try:
            blob.upload_from_string(
                new_store_id().encode("ascii"),
                content_type="text/plain",
                if_generation_match=0,
                timeout=self.timeout,
            )
        except Exception as exc:
            if _status(exc) != 412:  # 412: another instance created it first
                raise
        self._identity = validate_store_id(
            bytes(blob.download_as_bytes(timeout=self.timeout)).decode("ascii").strip()
        )
        return self._identity

    @property
    def identity_object(self) -> str:
        return f"{self.prefix}.{GCS_IDENTITY_SUFFIX}" if self.prefix else GCS_IDENTITY_SUFFIX

    @property
    def owner_object(self) -> str:
        return f"{self.prefix}.{GCS_OWNER_SUFFIX}" if self.prefix else GCS_OWNER_SUFFIX

    def owner_record(self) -> tuple[str, float] | None:
        """``(database id, claimed_at)`` of the owner (``<prefix>.nexura-store-owner``)."""
        blob = self.bucket.blob(self.owner_object)
        try:
            data = blob.download_as_bytes(timeout=self.timeout)
        except Exception as exc:
            if _status(exc) == 404:
                return None
            raise
        return parse_owner_record(bytes(data))

    def owner(self) -> str | None:
        """The id of the database that owns this store, if recorded."""
        record = self.owner_record()
        return None if record is None else record[0]

    def claim_owner(self, database_id: str) -> str:
        """Record ``database_id`` as the owner unless one is recorded (create-only)."""
        validate_database_id(database_id)
        current = self.owner()
        if current is not None:
            return current
        try:
            self.bucket.blob(self.owner_object).upload_from_string(
                owner_record_text(database_id, time.time()),
                content_type="application/json",
                if_generation_match=0,
                timeout=self.timeout,
            )
        except Exception as exc:
            if _status(exc) != 412:  # 412: another instance recorded one first
                raise
        recorded = self.owner()
        if recorded is None:
            raise BlobStoreError(f"Blob store {self.describe()} has no owner after recording one")
        return recorded

    def _object_name(self, key: str) -> str:
        return f"{self.prefix}/{key}" if self.prefix else key

    def put(self, workspace_id: str, namespace: str, data: bytes, content_type: str) -> BlobRef:
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError("Blob data must be bytes")
        data = bytes(data)
        ref = new_ref(make_key(workspace_id, namespace), data, content_type)
        blob = self.bucket.blob(self._object_name(ref.key))
        blob.upload_from_string(
            data, content_type=ref.content_type, if_generation_match=0, timeout=self.timeout
        )
        return ref

    def get(self, ref: BlobRef) -> bytes:
        blob = self.bucket.blob(self._object_name(key_of(ref)))
        try:
            data = blob.download_as_bytes(timeout=self.timeout)
        except Exception as exc:
            if _status(exc) == 404:
                raise BlobNotFoundError(f"Blob {ref.key} does not exist") from exc
            raise
        return verify(ref, bytes(data))

    def delete(self, ref_or_key: BlobRef | str) -> bool:
        blob = self.bucket.blob(self._object_name(key_of(ref_or_key)))
        try:
            blob.delete(timeout=self.timeout)
        except Exception as exc:
            if _status(exc) == 404:
                return False
            raise
        return True

    def exists(self, ref_or_key: BlobRef | str) -> bool:
        blob = self.bucket.blob(self._object_name(key_of(ref_or_key)))
        return bool(blob.exists(timeout=self.timeout))

    def list(self, prefix: str = "") -> Iterator[BlobInfo]:
        validate_prefix(prefix)
        base = f"{self.prefix}/" if self.prefix else ""
        for blob in self.client.list_blobs(
            self.bucket_name, prefix=base + prefix, timeout=self.timeout
        ):
            name = str(blob.name)
            if not name.startswith(base):
                continue
            key = name[len(base) :]
            try:
                key_of(key)
            except BlobStoreError:
                continue  # an object this store did not write
            updated = blob.updated
            yield BlobInfo(
                key=key,
                size=int(blob.size or 0),
                # Unknown time counts as "just written", so retention never deletes it early.
                updated_at=float(updated.timestamp()) if updated is not None else time.time(),
            )
