# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""TEST ONLY: blob store helpers.

- ``allow_local_blobs_on_cloud_run()`` lets a test that sets ``K_SERVICE``/``CLOUD_RUN_JOB``
  keep a local blob folder, which the service otherwise refuses on Cloud Run
  (``ServiceStores.require_blob_support``). It is a module flag; no environment variable
  exists for it. Never import this module from service code.
- ``FakeGCSClient`` stands in for ``google.cloud.storage.Client`` (in memory, no network). It
  implements only the calls ``GCSBlobStore`` makes and raises errors with the same ``code``
  attribute as ``google.api_core.exceptions`` (404 not found, 412 precondition failed).
"""

from __future__ import annotations

import datetime as dt
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from nexura_runtime.service import storage_backend


@contextmanager
def allow_local_blobs_on_cloud_run() -> Iterator[None]:
    previous = storage_backend._TEST_LOCAL_BLOBS_ON_CLOUD_RUN
    storage_backend._TEST_LOCAL_BLOBS_ON_CLOUD_RUN = True
    try:
        yield
    finally:
        storage_backend._TEST_LOCAL_BLOBS_ON_CLOUD_RUN = previous


class FakeGCSError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


class _FakeObject:
    def __init__(self, data: bytes, content_type: str, updated: float) -> None:
        self.data = data
        self.content_type = content_type
        self.updated = updated


class FakeBlob:
    def __init__(self, client: FakeGCSClient, bucket: str, name: str) -> None:
        self._client = client
        self.bucket_name = bucket
        self.name = name

    @property
    def _objects(self) -> dict[str, _FakeObject]:
        return self._client.buckets.setdefault(self.bucket_name, {})

    @property
    def size(self) -> int | None:
        obj = self._objects.get(self.name)
        return len(obj.data) if obj else None

    @property
    def updated(self) -> dt.datetime | None:
        obj = self._objects.get(self.name)
        return dt.datetime.fromtimestamp(obj.updated, tz=dt.UTC) if obj else None

    def upload_from_string(
        self,
        data: bytes,
        content_type: str | None = None,
        if_generation_match: int | None = None,
        timeout: float | None = None,
    ) -> None:
        with self._client.lock:
            self._client.calls.append(("upload", self.name, if_generation_match))
            if if_generation_match == 0 and self.name in self._objects:
                raise FakeGCSError(412, "PreconditionFailed")
            self._objects[self.name] = _FakeObject(
                bytes(data), content_type or "application/octet-stream", self._client.clock()
            )

    def download_as_bytes(self, timeout: float | None = None) -> bytes:
        with self._client.lock:
            self._client.downloads += 1
            if self._client.fail_downloads is not None:
                raise FakeGCSError(self._client.fail_downloads, "injected failure")
            obj = self._objects.get(self.name)
            if obj is None:
                raise FakeGCSError(404, "NotFound")
            return obj.data

    def delete(self, timeout: float | None = None) -> None:
        with self._client.lock:
            if self._objects.pop(self.name, None) is None:
                raise FakeGCSError(404, "NotFound")

    def exists(self, timeout: float | None = None) -> bool:
        with self._client.lock:
            return self.name in self._objects


class FakeBucket:
    def __init__(self, client: FakeGCSClient, name: str) -> None:
        self._client = client
        self.name = name

    def blob(self, name: str) -> FakeBlob:
        return FakeBlob(self._client, self.name, name)


class FakeGCSClient:
    """In-memory ``google.cloud.storage.Client`` double: ``buckets[bucket][object] = bytes``."""

    def __init__(self, clock: Any = time.time) -> None:
        self.buckets: dict[str, dict[str, _FakeObject]] = {}
        self.calls: list[tuple[Any, ...]] = []
        self.clock = clock
        self.lock = threading.RLock()
        #: Number of downloads (reads) so far, and an HTTP status every download fails with.
        self.downloads = 0
        self.fail_downloads: int | None = None

    def bucket(self, name: str) -> FakeBucket:
        return FakeBucket(self, name)

    def list_blobs(
        self, bucket_or_name: str, prefix: str | None = None, timeout: float | None = None
    ) -> Iterator[FakeBlob]:
        with self.lock:
            names = sorted(self.buckets.get(bucket_or_name, {}))
        for name in names:
            if prefix is None or name.startswith(prefix):
                yield FakeBlob(self, bucket_or_name, name)

    # Test conveniences.
    def raw(self, bucket: str, name: str) -> bytes:
        return self.buckets[bucket][name].data

    def set_raw(self, bucket: str, name: str, data: bytes) -> None:
        self.buckets[bucket][name].data = data

    def set_updated(self, bucket: str, name: str, when: float) -> None:
        self.buckets[bucket][name].updated = when
