# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""BlobStore contract (MVP WP2b), run against ``LocalBlobStore`` and ``GCSBlobStore``.

The GCS store runs on ``FakeGCSClient`` (in memory): no network, no credentials, no paid
calls. The real ``google-cloud-storage`` client is not installed in this environment.
"""

from __future__ import annotations

import hashlib
import os
import stat
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from nexura_runtime.blobs import (
    BlobIntegrityError,
    BlobNotFoundError,
    BlobRef,
    BlobStore,
    BlobStoreError,
    InvalidBlobKeyError,
    LocalBlobStore,
    open_blob_store,
    parse_blob_url,
)
from nexura_runtime.blobs.base import make_key, validate_key, workspace_segment
from nexura_runtime.blobs.gcs import GCS_EXTRA_MESSAGE, GCSBlobStore
from nexura_runtime.testing.blob_store import FakeGCSClient

BUCKET = "nexura-test-bucket"


class Harness:
    """A store plus raw access to what it stored, for tamper and timing tests."""

    def __init__(
        self,
        store: BlobStore,
        read_raw: Callable[[str], bytes],
        write_raw: Callable[[str], Callable[[bytes], None]],
    ) -> None:
        self.store = store
        self.read_raw = read_raw
        self.write_raw = write_raw


@pytest.fixture(params=["local", "gcs"])
def harness(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Harness]:
    if request.param == "local":
        root = tmp_path / "blobs"
        store = LocalBlobStore(root)

        def local_write(key: str) -> Callable[[bytes], None]:
            return (root / key).write_bytes

        yield Harness(store, lambda key: (root / key).read_bytes(), local_write)
        return
    client = FakeGCSClient()
    gcs = GCSBlobStore(BUCKET, "nexura/prod", client=client)

    def gcs_write(key: str) -> Callable[[bytes], None]:
        return lambda data: client.set_raw(BUCKET, f"nexura/prod/{key}", data)

    yield Harness(gcs, lambda key: client.raw(BUCKET, f"nexura/prod/{key}"), gcs_write)


def test_put_get_round_trip_and_reference(harness: Harness) -> None:
    store = harness.store
    data = b'{"hello": "world"}' * 1000
    ref = store.put("ws-1", "run-events", data, "application/json")
    assert ref.sha256 == hashlib.sha256(data).hexdigest()
    assert ref.size == len(data)
    assert ref.content_type == "application/json"
    workspace, namespace, blob_id = ref.key.split("/")
    assert (workspace, namespace) == ("ws-1", "run-events")
    assert len(blob_id) == 32
    assert store.get(ref) == data
    assert store.exists(ref) and store.exists(ref.key)
    assert harness.read_raw(ref.key) == data
    assert BlobRef.from_dict(ref.to_dict()) == ref


def test_keys_are_random_never_content_addressed(harness: Harness) -> None:
    store = harness.store
    first = store.put("ws", "run-events", b"same", "application/json")
    second = store.put("ws", "run-events", b"same", "application/json")
    assert first.key != second.key and first.sha256 == second.sha256
    assert store.get(first) == store.get(second) == b"same"


def test_delete_is_idempotent(harness: Harness) -> None:
    store = harness.store
    ref = store.put("ws", "eval-traces", b"x", "application/json")
    assert store.delete(ref) is True
    assert store.delete(ref) is False
    assert store.delete(ref.key) is False
    assert not store.exists(ref)
    with pytest.raises(BlobNotFoundError, match="does not exist"):
        store.get(ref)


def test_listing_by_prefix(harness: Harness) -> None:
    store = harness.store
    a = store.put("ws-a", "run-events", b"1", "application/json")
    b = store.put("ws-a", "eval-traces", b"22", "application/json")
    c = store.put("ws-b", "run-events", b"333", "application/json")
    everything = {info.key: info for info in store.list()}
    assert set(everything) == {a.key, b.key, c.key}
    assert everything[c.key].size == 3
    assert all(info.updated_at > 0 for info in everything.values())
    assert {i.key for i in store.list("ws-a/")} == {a.key, b.key}
    assert {i.key for i in store.list("ws-a/run-events/")} == {a.key}
    assert list(store.list("ws-c/")) == []
    for bad in ("ws-a", "../", "ws-a/../", "/ws-a/", "ws-a/run-events/x/", "ws-a/Bad/"):
        with pytest.raises(InvalidBlobKeyError):
            list(store.list(bad))


def test_sha256_tamper_is_detected_on_read(harness: Harness) -> None:
    store = harness.store
    ref = store.put("ws", "run-events", b'{"amount": 100}', "application/json")
    harness.write_raw(ref.key)(b'{"amount": 900}')  # same size, different bytes
    with pytest.raises(BlobIntegrityError, match="integrity check"):
        store.get(ref)
    harness.write_raw(ref.key)(b'{"amount": 100} ')  # size differs
    with pytest.raises(BlobIntegrityError):
        store.get(ref)
    # A reference whose recorded hash was altered fails the same way.
    forged = BlobRef(ref.key, "0" * 64, ref.size, ref.content_type)
    harness.write_raw(ref.key)(b'{"amount": 100}')
    with pytest.raises(BlobIntegrityError):
        store.get(forged)
    assert store.get(ref) == b'{"amount": 100}'


TRAVERSAL_KEYS = [
    "../outside/run-events/" + "a" * 32,
    "ws/../run-events/" + "a" * 32,
    "ws/run-events/../../../etc/passwd",
    "/etc/run-events/" + "a" * 32,
    "ws/run-events/" + "a" * 31 + "/",
    "ws\\..\\x/run-events/" + "a" * 32,
    "ws/run-events/" + "A" * 32,
    "ws//run-events/" + "a" * 32,
    "ws/run-events/" + "a" * 32 + "/extra",
    "ws/run-events",
    ".../run-events/" + "a" * 32,
    "..",
    "",
    "ws/run-events/" + "a" * 30 + "\x00b",
]


@pytest.mark.parametrize("key", TRAVERSAL_KEYS)
def test_traversal_and_foreign_keys_are_rejected(harness: Harness, key: str) -> None:
    store = harness.store
    with pytest.raises(InvalidBlobKeyError):
        store.delete(key)
    with pytest.raises(InvalidBlobKeyError):
        store.exists(key)
    with pytest.raises(InvalidBlobKeyError):
        BlobRef(key, "0" * 64, 1, "application/json")


def test_traversal_rejection_touches_nothing_outside_the_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    victim = tmp_path / "victim.txt"
    victim.write_text("keep")
    store = LocalBlobStore(root)
    store.put("ws", "run-events", b"x", "application/json")
    for key in ("../victim.txt", "ws/../../victim.txt", str(victim)):
        with pytest.raises(InvalidBlobKeyError):
            store.delete(key)
    assert victim.read_text() == "keep"


def test_local_store_refuses_a_symlinked_workspace_escaping_the_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside" / "run-events"
    outside.mkdir(parents=True)
    root.mkdir(mode=0o700)
    (root / "ws").symlink_to(tmp_path / "outside")
    (outside / ("b" * 32)).write_bytes(b"secret")
    store = LocalBlobStore(root)
    key = "ws/run-events/" + "b" * 32
    with pytest.raises(InvalidBlobKeyError, match="escapes"):
        store.exists(key)
    with pytest.raises(InvalidBlobKeyError, match="escapes"):
        store.get(BlobRef(key, hashlib.sha256(b"secret").hexdigest(), 6, "application/json"))
    with pytest.raises(InvalidBlobKeyError, match="escapes"):
        store.delete(key)
    assert (outside / ("b" * 32)).read_bytes() == b"secret"
    # put: the namespace directory does not exist yet behind the link, and nothing is written.
    with pytest.raises(InvalidBlobKeyError, match="escapes"):
        store.put("ws", "eval-traces", b"x", "application/json")
    assert not (tmp_path / "outside" / "eval-traces").exists()
    # A symlinked namespace directory is refused the same way.
    (root / "ws2").mkdir(mode=0o700)
    (root / "ws2" / "run-events").symlink_to(outside)
    with pytest.raises(InvalidBlobKeyError, match="escapes"):
        store.put("ws2", "run-events", b"x", "application/json")
    assert sorted(p.name for p in outside.iterdir()) == ["b" * 32]


def test_local_root_permissions_are_checked(tmp_path: Path, caplog: Any) -> None:
    root = tmp_path / "open"
    root.mkdir()
    root.chmod(0o777)
    with pytest.raises(BlobStoreError, match="group/other write"):
        LocalBlobStore(root).put("ws", "run-events", b"x", "application/json")
    assert list(root.iterdir()) == []
    root.chmod(0o755)
    with caplog.at_level("WARNING"):
        LocalBlobStore(root).put("ws", "run-events", b"x", "application/json")
    assert "readable by group/other" in caplog.text


def test_local_write_never_replaces_an_existing_file(tmp_path: Path) -> None:
    store = LocalBlobStore(tmp_path / "blobs")
    ref = store.put("ws", "run-events", b"original", "application/json")
    path = store.root / ref.key
    with pytest.raises(FileExistsError):
        store._write_new(path, b"replacement", "test")
    assert path.read_bytes() == b"original" and store.get(ref) == b"original"
    assert [p.name for p in path.parent.iterdir()] == [path.name]  # no temporary file left


def test_namespaces_and_workspaces_are_validated(harness: Harness) -> None:
    store = harness.store
    for namespace in ("", "Run", "../x", "a/b", "x" * 40):
        with pytest.raises(InvalidBlobKeyError):
            store.put("ws", namespace, b"x", "application/json")
    with pytest.raises(InvalidBlobKeyError):
        store.put("", "run-events", b"x", "application/json")
    with pytest.raises(InvalidBlobKeyError):
        store.put("ws", "run-events", b"x", "not a type")
    # Workspace ids outside the API pattern are filed under a stable digest, never as a path.
    for odd in ("../evil", "a/b", ".hidden", "x" * 300, "ü"):
        ref = store.put(odd, "run-events", b"x", "application/json")
        assert ref.workspace == workspace_segment(odd)
        assert ref.workspace.startswith("_h") and "/" not in ref.workspace
        assert validate_key(ref.key) == ref.key
        assert store.get(ref) == b"x"


def test_unknown_listed_objects_are_ignored(harness: Harness, tmp_path: Path) -> None:
    store = harness.store
    ref = store.put("ws", "run-events", b"x", "application/json")
    if isinstance(store, LocalBlobStore):
        (store.root / "ws" / "run-events" / ".tmp-leftover").write_bytes(b"partial")
        (store.root / "README").write_text("not a blob")
    else:
        client = store.client
        client.bucket(BUCKET).blob("nexura/prod/ws/run-events/not-a-blob").upload_from_string(b"z")
        client.bucket(BUCKET).blob("nexura/other/ws/run-events/" + "c" * 32).upload_from_string(
            b"z"
        )
    assert [info.key for info in store.list()] == [ref.key]


def test_local_writes_are_atomic_and_private(tmp_path: Path) -> None:
    root = tmp_path / "blobs"
    store = LocalBlobStore(root)
    assert not root.exists()  # nothing is created before the first write
    ref = store.put("ws", "run-events", b"payload", "application/json")
    path = root / ref.key
    if sys.platform != "win32":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(root.stat().st_mode) == 0o700
        assert stat.S_IMODE((root / "ws").stat().st_mode) == 0o700
    assert [p.name for p in path.parent.iterdir()] == [path.name]  # no temporary file left


def test_local_write_failure_leaves_no_partial_blob(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = LocalBlobStore(tmp_path / "blobs")

    def broken_fsync(fd: int) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "fsync", broken_fsync)
    with pytest.raises(OSError, match="disk full"):
        store.put("ws", "run-events", b"payload", "application/json")
    monkeypatch.undo()
    assert list(store.list()) == []
    leftovers = [p for p in (tmp_path / "blobs").rglob("*") if p.is_file()]
    assert leftovers == []


def test_local_blob_is_invisible_until_fully_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = LocalBlobStore(tmp_path / "blobs")
    seen_while_writing: list[list[str]] = []
    real_fsync = os.fsync

    def observing_fsync(fd: int) -> None:
        if not seen_while_writing:  # the data file's fsync, before the rename
            seen_while_writing.append([info.key for info in store.list()])
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", observing_fsync)
    ref = store.put("ws", "run-events", b"payload", "application/json")
    assert seen_while_writing == [[]]  # written under a temporary name, then renamed
    assert [info.key for info in store.list()] == [ref.key]


def test_gcs_creates_objects_without_overwriting_and_uses_the_prefix() -> None:
    client = FakeGCSClient()
    store = GCSBlobStore(BUCKET, "nexura/prod", client=client)
    ref = store.put("ws", "run-events", b"x", "application/json")
    assert client.calls[-1] == ("upload", f"nexura/prod/{ref.key}", 0)
    assert set(client.buckets[BUCKET]) == {f"nexura/prod/{ref.key}"}
    assert client.buckets[BUCKET][f"nexura/prod/{ref.key}"].content_type == "application/json"
    assert store.describe() == f"gs://{BUCKET}/nexura/prod"
    no_prefix = GCSBlobStore(BUCKET, client=client)
    other = no_prefix.put("ws", "run-events", b"y", "application/json")
    assert f"{other.key}" in client.buckets[BUCKET]


def test_gcs_other_errors_propagate() -> None:
    class Boom(Exception):
        code = 503

    class FailingClient(FakeGCSClient):
        def bucket(self, name: str) -> Any:
            bucket = super().bucket(name)
            original = bucket.blob

            def blob(object_name: str) -> Any:
                inner = original(object_name)

                def fail(*args: Any, **kwargs: Any) -> Any:
                    raise Boom("unavailable")

                inner.download_as_bytes = fail  # type: ignore[method-assign]
                inner.delete = fail  # type: ignore[method-assign]
                return inner

            bucket.blob = blob  # type: ignore[method-assign]
            return bucket

    store = GCSBlobStore(BUCKET, client=FailingClient())
    key = make_key("ws", "run-events")
    with pytest.raises(Boom):
        store.get(BlobRef(key, "0" * 64, 1, "application/json"))
    with pytest.raises(Boom):
        store.delete(key)


def test_gcs_without_the_client_library_names_the_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    # google-cloud-storage is an optional extra; it is imported only when a gs:// store opens.
    monkeypatch.setitem(sys.modules, "google.cloud.storage", None)
    with pytest.raises(BlobStoreError, match=r"nexura-runtime\[gcs\]") as excinfo:
        open_blob_store("gs://bucket-name/prefix")
    assert str(excinfo.value) == GCS_EXTRA_MESSAGE
    # An injected client needs no library.
    assert open_blob_store("gs://bucket-name/prefix", client=FakeGCSClient()).scheme == "gs"
    with pytest.raises(BlobStoreError, match="google-cloud-storage"):
        _ = GCSBlobStore("bucket-name").client


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("gs://my-bucket", ("gs", "my-bucket", "", "")),
        ("gs://my-bucket/", ("gs", "my-bucket", "", "")),
        ("gs://my_bucket.v2/nexura/prod/", ("gs", "my_bucket.v2", "nexura/prod", "")),
        ("file:///srv/nexura/blobs", ("file", "", "", "/srv/nexura/blobs")),
        ("file://localhost/srv/b%20c", ("file", "", "", "/srv/b c")),
    ],
)
def test_blob_url_parsing(url: str, expected: tuple[str, str, str, str]) -> None:
    location = parse_blob_url(url)
    assert (location.scheme, location.bucket, location.prefix, location.path) == expected


@pytest.mark.parametrize(
    "url",
    [
        "",
        "s3://bucket/x",
        "gs://",
        "gs://Upper",
        "gs://ab",
        "gs://a..b",
        "gs://bucket/../x",
        "gs://bucket/a/./b",
        "gs://bucket/a b",
        "gs://user:pw@bucket/x",
        "gs://bucket/x?y=1",
        "gs://bucket/x#f",
        "file://host/srv/x",
        "file:relative/path",
        "file:///srv/../etc",
        "/srv/plain/path",
    ],
)
def test_bad_blob_urls_are_refused(url: str) -> None:
    with pytest.raises(ValueError):
        parse_blob_url(url)
