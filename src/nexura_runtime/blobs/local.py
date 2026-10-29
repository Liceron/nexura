# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""``LocalBlobStore``: blobs as files under one owner-only directory.

Writes are atomic: the bytes go to a temporary file in the target directory (mode 0600), are
flushed with ``fsync`` and then hard-linked into place, which fails if the name exists, so a
reader never sees a partial blob and a blob is never overwritten. Directories are created with
mode 0700. Keys are validated before any path is built, and the resolved directory of every
blob must stay inside the resolved root (a symlinked workspace or namespace directory is
refused). A root that group or others can write is refused; a readable one logs a warning.
"""

from __future__ import annotations

import contextlib
import errno
import logging
import os
import stat
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

from nexura_runtime.blobs.base import (
    IDENTITY_DIR,
    IDENTITY_FILE,
    OWNER_FILE,
    BlobInfo,
    BlobNotFoundError,
    BlobRef,
    BlobStoreError,
    InvalidBlobKeyError,
    key_of,
    make_key,
    new_ref,
    new_store_id,
    owner_record_text,
    parse_owner_record,
    validate_database_id,
    validate_key,
    validate_prefix,
    validate_store_id,
    verify,
)

logger = logging.getLogger(__name__)

_TMP_PREFIX = ".tmp-"


class LocalBlobStore:
    """Blobs in a local directory (the default; not durable on Cloud Run)."""

    scheme = "file"

    def __init__(self, root: str | Path) -> None:
        path = Path(root)
        if not path.is_absolute():
            path = path.resolve()
        self.root = path
        self._identity: str | None = None
        self._warned_readable = False

    def describe(self) -> str:
        return self.root.as_uri()

    # -- paths ------------------------------------------------------------------------

    def _check_root(self) -> None:
        """Refuse a root others can write; warn once when others can read it."""
        try:
            mode = self.root.stat().st_mode
        except FileNotFoundError:
            return
        if not stat.S_ISDIR(mode):
            raise BlobStoreError(f"Blob root {self.root} is not a directory")
        if mode & 0o022:
            raise BlobStoreError(
                f"Unsafe blob root permissions at '{self.root}': group/other write access is "
                f"forbidden; fix: chmod go-w '{self.root}' (0700 is recommended)"
            )
        if mode & 0o044 and not self._warned_readable:
            self._warned_readable = True
            logger.warning(
                "Blob root %s is readable by group/other; chmod 700 is recommended", self.root
            )

    def _inside(self, directory: Path, what: str) -> None:
        """``directory`` (which exists) must resolve to the root or below it."""
        root = self.root.resolve()
        resolved = directory.resolve()
        if resolved != root and root not in resolved.parents:
            raise InvalidBlobKeyError(f"Blob {what} escapes the blob root")

    def _path(self, key: str) -> Path:
        validate_key(key)
        path = self.root.joinpath(*key.split("/"))
        # Defence in depth: the key shape already excludes traversal; a symlinked directory on
        # the way is refused once it exists.
        parent = path.parent
        while not parent.exists() and parent != self.root and parent != parent.parent:
            parent = parent.parent
        if parent.exists():
            self._inside(parent, f"key {key!r}")
        return path

    def _mkdirs(self, directory: Path) -> None:
        # ``mkdir(parents=True)`` gives missing parents default permissions, so create the root
        # and each key directory one level at a time with mode 0700.
        if not self.root.exists():
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        missing: list[Path] = []
        current = directory
        while not current.exists() and current != self.root:
            missing.append(current)
            current = current.parent
        for path in reversed(missing):
            path.mkdir(exist_ok=True, mode=0o700)

    def _write_new(self, path: Path, data: bytes, name: str) -> None:
        """Write ``data`` to the new file ``path`` atomically; ``FileExistsError`` if it exists."""
        self._mkdirs(path.parent)
        self._inside(path.parent, name)  # after mkdirs: a symlinked parent is refused here
        tmp = path.parent / f"{_TMP_PREFIX}{uuid.uuid4().hex}"
        fd = os.open(
            tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(tmp, path)  # atomic create: fails with EEXIST, never replaces
            except OSError as exc:
                if exc.errno == errno.EEXIST:
                    raise FileExistsError(f"{name} already exists") from exc
                if exc.errno not in (errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP):
                    raise
                # A file system without hard links: rename (128-bit random names never clash).
                if path.exists():
                    raise FileExistsError(f"{name} already exists") from exc
                os.rename(tmp, path)
        finally:
            with contextlib.suppress(FileNotFoundError):
                tmp.unlink()
        with contextlib.suppress(OSError):  # make the new name durable where supported
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)

    # -- identity ---------------------------------------------------------------------

    def identity(self, *, create: bool = True) -> str | None:
        """This store's id (``_nexura/store-id``), created on first use when ``create``."""
        if self._identity is not None:
            return self._identity
        path = self.root / IDENTITY_DIR / IDENTITY_FILE
        if path.is_file():
            self._inside(path.parent, "identity")
            self._identity = validate_store_id(path.read_text(encoding="ascii").strip())
            return self._identity
        if not create:
            return None
        self._check_root()
        with contextlib.suppress(FileExistsError):  # another process created it first
            self._write_new(path, new_store_id().encode("ascii"), "Blob store identity")
        self._identity = validate_store_id(path.read_text(encoding="ascii").strip())
        return self._identity

    def owner_record(self) -> tuple[str, float] | None:
        """``(database id, claimed_at)`` of the owner (``_nexura/store-owner``), if recorded."""
        path = self.root / IDENTITY_DIR / OWNER_FILE
        if not path.is_file():
            return None
        self._inside(path.parent, "owner")
        return parse_owner_record(path.read_bytes())

    def owner(self) -> str | None:
        """The id of the database that owns this store, if recorded."""
        record = self.owner_record()
        return None if record is None else record[0]

    def claim_owner(self, database_id: str) -> str:
        """Record ``database_id`` as the owner unless one is recorded; the recorded owner."""
        validate_database_id(database_id)
        current = self.owner()
        if current is not None:
            return current
        self._check_root()
        path = self.root / IDENTITY_DIR / OWNER_FILE
        with contextlib.suppress(FileExistsError):  # another process recorded one first
            self._write_new(path, owner_record_text(database_id, time.time()), "Blob store owner")
        return parse_owner_record(path.read_bytes())[0]

    # -- blobs ------------------------------------------------------------------------

    def put(self, workspace_id: str, namespace: str, data: bytes, content_type: str) -> BlobRef:
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError("Blob data must be bytes")
        data = bytes(data)
        self._check_root()
        ref = new_ref(make_key(workspace_id, namespace), data, content_type)
        self._write_new(self._path(ref.key), data, f"Blob {ref.key}")
        return ref

    def get(self, ref: BlobRef) -> bytes:
        path = self._path(ref.key)
        try:
            data = path.read_bytes()
        except FileNotFoundError as exc:
            raise BlobNotFoundError(f"Blob {ref.key} does not exist") from exc
        return verify(ref, data)

    def delete(self, ref_or_key: BlobRef | str) -> bool:
        path = self._path(key_of(ref_or_key))
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        return True

    def exists(self, ref_or_key: BlobRef | str) -> bool:
        return self._path(key_of(ref_or_key)).is_file()

    def list(self, prefix: str = "") -> Iterator[BlobInfo]:
        validate_prefix(prefix)
        if not self.root.is_dir():
            return
        for dirpath, dirnames, filenames in os.walk(self.root, followlinks=False):
            dirnames.sort()
            for name in sorted(filenames):
                if name.startswith(_TMP_PREFIX):
                    continue
                full = Path(dirpath) / name
                key = full.relative_to(self.root).as_posix()
                try:
                    validate_key(key)
                except InvalidBlobKeyError:
                    continue  # not a blob this store wrote (for example its identity file)
                if not key.startswith(prefix):
                    continue
                try:
                    info = full.lstat()
                except FileNotFoundError:
                    continue  # deleted while listing
                yield BlobInfo(key=key, size=info.st_size, updated_at=info.st_mtime)
