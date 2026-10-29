# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Blob store contract: write-once bytes outside the database, addressed by a ``BlobRef``.

The database keeps only the ``BlobRef`` (key, sha256, size, content type) and, where useful, a
short preview. Keys are generated here, never taken from users: ``<workspace>/<namespace>/<id>``
with a random 128-bit hex id. Random ids (not content addresses) mean a blob is never shared by
two rows, so deleting an unreferenced blob can never race with a new row that reuses it.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

#: Workspace ids the service API accepts are used as they are (``workspaces.py`` pattern).
_SAFE_WORKSPACE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}")
#: Any other workspace id is replaced by a digest; ``_h`` cannot start a safe id.
_HASHED_WORKSPACE = re.compile(r"_h[0-9a-f]{40}")
_NAMESPACE = re.compile(r"[a-z][a-z0-9-]{0,31}")
_BLOB_ID = re.compile(r"[0-9a-f]{32}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_CONTENT_TYPE = re.compile(r"[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*")


#: Where a local store keeps its identity: ``<root>/_nexura/store-id`` (never a valid key). The
#: GCS store keeps it beside its prefix (``gcs.GCS_IDENTITY_SUFFIX``).
IDENTITY_DIR = "_nexura"
IDENTITY_FILE = "store-id"
#: The id of the database that owns the store (MVP WP3, R3-1), beside the identity.
OWNER_FILE = "store-owner"
_STORE_ID = re.compile(r"bs_[0-9a-f]{32}")
_DATABASE_ID = re.compile(r"db_[0-9a-f]{32}")


def new_store_id() -> str:
    return "bs_" + uuid.uuid4().hex


def validate_store_id(value: Any) -> str:
    if not isinstance(value, str) or not _STORE_ID.fullmatch(value):
        raise BlobStoreError("Invalid blob store identity")
    return value


def new_database_id() -> str:
    """A random id for a database that binds blob stores (``db_<32 hex>``)."""
    return "db_" + uuid.uuid4().hex


def validate_database_id(value: Any) -> str:
    if not isinstance(value, str) or not _DATABASE_ID.fullmatch(value):
        raise BlobStoreError("Invalid database identity")
    return value


def owner_record_text(database_id: str, claimed_at: float) -> bytes:
    """The owner object's content: the owning database id and when it claimed the store."""
    validate_database_id(database_id)
    return json.dumps(
        {"database_id": database_id, "claimed_at": float(claimed_at)}, sort_keys=True
    ).encode("ascii")


def parse_owner_record(data: bytes) -> tuple[str, float]:
    """``(database id, claimed_at)`` from an owner object; ``BlobStoreError`` when malformed."""
    try:
        value = json.loads(data.decode("ascii"))
        database_id = validate_database_id(value["database_id"])
        claimed_at = float(value["claimed_at"])
    except (ValueError, KeyError, TypeError, UnicodeDecodeError) as exc:
        raise BlobStoreError("Malformed blob store owner record") from exc
    if not math.isfinite(claimed_at):
        raise BlobStoreError("Malformed blob store owner record")
    return database_id, claimed_at


def store_has_blobs(store: Any) -> bool:
    """True when ``store`` lists at least one blob (reads one listing page at most)."""
    return next(iter(store.list("")), None) is not None


class BlobStoreError(Exception):
    """Base class for blob store failures."""


class InvalidBlobKeyError(BlobStoreError, ValueError):
    """A key or reference that this module did not generate (for example a traversal attempt)."""


class BlobNotFoundError(BlobStoreError, KeyError):
    """The referenced blob does not exist (never written, or deleted by retention)."""

    def __str__(self) -> str:  # KeyError would quote the message
        return str(self.args[0]) if self.args else "blob not found"


class BlobIntegrityError(BlobStoreError):
    """The stored bytes do not match the reference's sha256 or size (corruption or tampering)."""


def workspace_segment(workspace_id: str) -> str:
    """The key segment for ``workspace_id``: the id itself when safe, else a stable digest."""
    if not isinstance(workspace_id, str) or not workspace_id:
        raise InvalidBlobKeyError("A blob needs a workspace id")
    if _SAFE_WORKSPACE.fullmatch(workspace_id):
        return workspace_id
    return "_h" + hashlib.sha256(workspace_id.encode("utf-8")).hexdigest()[:40]


def make_key(workspace_id: str, namespace: str) -> str:
    """A new random key ``<workspace>/<namespace>/<32 hex>``."""
    if not isinstance(namespace, str) or not _NAMESPACE.fullmatch(namespace):
        raise InvalidBlobKeyError(f"Invalid blob namespace {namespace!r}")
    return f"{workspace_segment(workspace_id)}/{namespace}/{uuid.uuid4().hex}"


def validate_key(key: Any) -> str:
    """Return ``key`` when it has exactly the generated shape, else raise ``InvalidBlobKeyError``.

    The strict shape rejects traversal (``..``, absolute paths, backslashes, NUL, extra or empty
    segments) before any backend sees the key.
    """
    if not isinstance(key, str):
        raise InvalidBlobKeyError("A blob key must be a string")
    parts = key.split("/")
    if (
        len(parts) != 3
        or not (_SAFE_WORKSPACE.fullmatch(parts[0]) or _HASHED_WORKSPACE.fullmatch(parts[0]))
        or parts[0] in {".", ".."}
        or not _NAMESPACE.fullmatch(parts[1])
        or not _BLOB_ID.fullmatch(parts[2])
    ):
        raise InvalidBlobKeyError(f"Invalid blob key {key[:200]!r}")
    return key


def key_namespace(key: str) -> str:
    """The namespace segment of a valid key."""
    return validate_key(key).split("/")[1]


def validate_prefix(prefix: str) -> str:
    """A listing prefix: empty, or whole leading key segments ending with ``/``."""
    if prefix == "":
        return prefix
    if not isinstance(prefix, str) or not prefix.endswith("/"):
        raise InvalidBlobKeyError("A blob listing prefix must be empty or end with '/'")
    segments = prefix[:-1].split("/")
    if len(segments) > 2:
        raise InvalidBlobKeyError("A blob listing prefix has at most two segments")
    first = segments[0]
    if first in {".", ".."} or not (
        _SAFE_WORKSPACE.fullmatch(first) or _HASHED_WORKSPACE.fullmatch(first)
    ):
        raise InvalidBlobKeyError(f"Invalid blob listing prefix {prefix[:200]!r}")
    if len(segments) == 2 and not _NAMESPACE.fullmatch(segments[1]):
        raise InvalidBlobKeyError(f"Invalid blob listing prefix {prefix[:200]!r}")
    return prefix


@dataclass(frozen=True)
class BlobRef:
    """What the database stores for a blob."""

    key: str
    sha256: str
    size: int
    content_type: str

    def __post_init__(self) -> None:
        validate_key(self.key)
        if not isinstance(self.sha256, str) or not _SHA256.fullmatch(self.sha256):
            raise InvalidBlobKeyError("Invalid blob sha256")
        if isinstance(self.size, bool) or not isinstance(self.size, int) or self.size < 0:
            raise InvalidBlobKeyError("Invalid blob size")
        if not isinstance(self.content_type, str) or not _CONTENT_TYPE.fullmatch(self.content_type):
            raise InvalidBlobKeyError("Invalid blob content type")

    @property
    def workspace(self) -> str:
        return self.key.split("/")[0]

    @property
    def namespace(self) -> str:
        return self.key.split("/")[1]

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "sha256": self.sha256,
            "size": self.size,
            "content_type": self.content_type,
        }

    @classmethod
    def from_dict(cls, value: Any) -> BlobRef:
        if not isinstance(value, dict) or set(value) != {"key", "sha256", "size", "content_type"}:
            raise InvalidBlobKeyError("Invalid blob reference")
        return cls(
            key=value["key"],
            sha256=value["sha256"],
            size=value["size"],
            content_type=value["content_type"],
        )


@dataclass(frozen=True)
class BlobInfo:
    """One listed blob: its key, size and last write time (epoch seconds, backend clock)."""

    key: str
    size: int
    updated_at: float


def check_content_type(content_type: str) -> str:
    if not isinstance(content_type, str) or not _CONTENT_TYPE.fullmatch(content_type):
        raise InvalidBlobKeyError(f"Invalid blob content type {content_type!r}")
    return content_type


def verify(ref: BlobRef, data: bytes) -> bytes:
    """Return ``data`` when it matches ``ref``; raise ``BlobIntegrityError`` otherwise."""
    if len(data) != ref.size or hashlib.sha256(data).hexdigest() != ref.sha256:
        raise BlobIntegrityError(
            f"Blob {ref.key} failed its integrity check: the stored bytes do not match the "
            "sha256/size recorded in the database (corrupted or tampered with)"
        )
    return data


@runtime_checkable
class BlobStore(Protocol):
    """Write-once blob storage. Implementations: ``LocalBlobStore``, ``GCSBlobStore``."""

    #: ``"file"`` or ``"gs"``; the service refuses ``"file"`` on Cloud Run.
    scheme: str

    def put(self, workspace_id: str, namespace: str, data: bytes, content_type: str) -> BlobRef:
        """Store ``data`` under a new random key in ``workspace_id``/``namespace``."""
        ...

    def get(self, ref: BlobRef) -> bytes:
        """The bytes of ``ref``, verified against its sha256 and size.

        Raises ``BlobNotFoundError`` or ``BlobIntegrityError``.
        """
        ...

    def delete(self, ref_or_key: BlobRef | str) -> bool:
        """Delete a blob; ``False`` when it was already gone (idempotent)."""
        ...

    def exists(self, ref_or_key: BlobRef | str) -> bool: ...

    def list(self, prefix: str = "") -> Iterator[BlobInfo]:
        """Every blob whose key starts with ``prefix`` (``""``, ``"<ws>/"`` or ``"<ws>/<ns>/"``)."""
        ...

    def describe(self) -> str:
        """A credential-free description for logs (``file:///path`` or ``gs://bucket/prefix``)."""
        ...

    def identity(self, *, create: bool = True) -> str | None:
        """The store's id (``bs_<32 hex>``), kept in the store itself; created on first use when
        ``create``. Every blob reference records it, and ``db blob-gc`` refuses a database whose
        references name another store (or none)."""
        ...

    # Optional (both built-in stores have them; the codec and retention skip the owner checks
    # for a store without them):
    #
    # ``owner() -> str | None``: the id of the database that owns this store, if recorded.
    # ``owner_record() -> tuple[str, float] | None``: the owner and when it claimed the store.
    # ``claim_owner(database_id) -> str``: record ``database_id`` (and the time) as the owner
    # unless an owner is already recorded (create-only), and return the recorded owner.


def key_of(ref_or_key: BlobRef | str) -> str:
    return validate_key(ref_or_key.key if isinstance(ref_or_key, BlobRef) else ref_or_key)


def new_ref(key: str, data: bytes, content_type: str) -> BlobRef:
    return BlobRef(
        key=key,
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
        content_type=check_content_type(content_type),
    )
