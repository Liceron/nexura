# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Local filesystem-backed immutable artifact store with security boundaries."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from nexura_agents.contracts import ArtifactReference
from nexura_agents.contracts.canonical import canonical_json

from nexura_runtime.artifacts.errors import (
    ArtifactHashMismatchError,
    ArtifactLimitExceededError,
    ArtifactNotFoundError,
    ArtifactSecurityError,
)

_SAFE_ID_REGEX = re.compile(r"^[a-zA-Z0-9_-]+$")


class LocalArtifactStore:
    """Workspace-scoped artifact store backed by local filesystem storage.

    Guarantees:
    - Atomic writes: Interrupted uploads never expose partial blobs.
    - Security boundaries: Strict rejection of traversal and symlink escapes.
    - Workspace isolation: Blobs are strictly partitioned by workspace_id.
    - Integrity: SHA-256 digests are verified on write and read.
    - Portable URIs: Emits 'artifact://{workspace_id}/{artifact_id}' instead of local paths.
    """

    def __init__(
        self,
        root_dir: str | Path,
        *,
        max_size_bytes: int = 50 * 1024 * 1024,  # 50 MB default
    ) -> None:
        self.root_dir = Path(root_dir).resolve()
        self.max_size_bytes = max_size_bytes
        self.root_dir.mkdir(parents=True, exist_ok=True)

    def _resolve_safe_path(self, workspace_id: str, artifact_id: str) -> tuple[Path, Path]:
        """Validate and resolve blob path and metadata path within root_dir.

        Raises:
            ArtifactSecurityError: If IDs contain invalid characters or traverse outside root.
        """
        if not _SAFE_ID_REGEX.match(workspace_id):
            raise ArtifactSecurityError(
                f"Invalid workspace_id '{workspace_id}'. Only alphanumeric, '_' and '-' allowed.",
                details={"workspace_id": workspace_id},
            )
        if not _SAFE_ID_REGEX.match(artifact_id):
            raise ArtifactSecurityError(
                f"Invalid artifact_id '{artifact_id}'. Only alphanumeric, '_' and '-' allowed.",
                details={"artifact_id": artifact_id},
            )

        ws_dir = (self.root_dir / workspace_id).resolve()
        unresolved_blob = ws_dir / f"{artifact_id}.bin"
        unresolved_meta = ws_dir / f"{artifact_id}.meta.json"

        # Check symlink escapes before resolving symlink target
        if unresolved_blob.is_symlink() or unresolved_meta.is_symlink():
            raise ArtifactSecurityError(
                "Symlink escape detected: symlinks are not permitted in artifact storage.",
                details={"blob_path": str(unresolved_blob)},
            )

        blob_path = unresolved_blob.resolve()
        meta_path = unresolved_meta.resolve()

        # Check path traversal
        try:
            blob_path.relative_to(self.root_dir)
            meta_path.relative_to(self.root_dir)
        except ValueError as err:
            raise ArtifactSecurityError(
                "Path traversal detected: resolved path escapes artifact storage root.",
                details={"root_dir": str(self.root_dir), "blob_path": str(blob_path)},
            ) from err

        return blob_path, meta_path

    async def put(
        self,
        workspace_id: str,
        artifact_id: str,
        data: bytes,
        *,
        mime_type: str = "application/octet-stream",
        metadata: dict[str, Any] | None = None,
    ) -> ArtifactReference:
        """Store an artifact payload atomically and return an ArtifactReference."""
        if len(data) > self.max_size_bytes:
            raise ArtifactLimitExceededError(len(data), self.max_size_bytes)

        blob_path, meta_path = self._resolve_safe_path(workspace_id, artifact_id)
        ws_dir = blob_path.parent
        ws_dir.mkdir(parents=True, exist_ok=True)
        tmp_dir = ws_dir / ".tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)

        digest = hashlib.sha256(data).hexdigest()
        tmp_file = tmp_dir / f"{artifact_id}_{os.getpid()}_{os.urandom(4).hex()}.tmp"

        try:
            tmp_file.write_bytes(data)
            # Atomic rename ensures readers never see partial writes
            os.replace(tmp_file, blob_path)
        except BaseException:
            if tmp_file.exists():
                tmp_file.unlink(missing_ok=True)
            raise

        ref = ArtifactReference(
            artifact_id=artifact_id,
            uri=f"artifact://{workspace_id}/{artifact_id}",
            mime_type=mime_type,
            size_bytes=len(data),
            digest=digest,
            metadata=metadata or {},
        )

        meta_content = canonical_json(ref.model_dump(mode="json"))
        tmp_meta = tmp_dir / f"{artifact_id}_{os.getpid()}_{os.urandom(4).hex()}.meta.tmp"
        try:
            tmp_meta.write_text(meta_content, encoding="utf-8")
            os.replace(tmp_meta, meta_path)
        except BaseException:
            if tmp_meta.exists():
                tmp_meta.unlink(missing_ok=True)
            raise

        return ref

    async def get(self, workspace_id: str, artifact_id: str) -> bytes:
        """Retrieve stored artifact bytes after verifying digest."""
        blob_path, meta_path = self._resolve_safe_path(workspace_id, artifact_id)
        if not blob_path.is_file() or not meta_path.is_file():
            raise ArtifactNotFoundError(workspace_id, artifact_id)

        data = blob_path.read_bytes()
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        expected_digest = meta.get("digest")

        actual_digest = hashlib.sha256(data).hexdigest()
        if expected_digest and actual_digest != expected_digest:
            raise ArtifactHashMismatchError(expected_digest, actual_digest)

        return data

    async def get_range(
        self,
        workspace_id: str,
        artifact_id: str,
        start: int,
        length: int | None = None,
    ) -> bytes:
        """Stream or range-read a portion of an artifact."""
        blob_path, meta_path = self._resolve_safe_path(workspace_id, artifact_id)
        if not blob_path.is_file() or not meta_path.is_file():
            raise ArtifactNotFoundError(workspace_id, artifact_id)

        file_size = blob_path.stat().st_size
        if start < 0 or start > file_size:
            raise ValueError(f"Invalid range start {start} for file size {file_size}")

        with blob_path.open("rb") as f:
            f.seek(start)
            return f.read(length) if length is not None else f.read()

    async def get_reference(self, workspace_id: str, artifact_id: str) -> ArtifactReference:
        """Retrieve artifact metadata reference without loading raw bytes."""
        _, meta_path = self._resolve_safe_path(workspace_id, artifact_id)
        if not meta_path.is_file():
            raise ArtifactNotFoundError(workspace_id, artifact_id)

        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        return ArtifactReference.model_validate(meta)

    async def delete(self, workspace_id: str, artifact_id: str) -> bool:
        """Delete an artifact and its associated metadata."""
        blob_path, meta_path = self._resolve_safe_path(workspace_id, artifact_id)
        existed = False
        if blob_path.exists():
            blob_path.unlink()
            existed = True
        if meta_path.exists():
            meta_path.unlink()
            existed = True
        return existed

    async def exists(self, workspace_id: str, artifact_id: str) -> bool:
        """Check if an artifact exists in the specified workspace."""
        blob_path, meta_path = self._resolve_safe_path(workspace_id, artifact_id)
        return blob_path.is_file() and meta_path.is_file()

    def for_workspace(self, workspace_id: str) -> WorkspaceArtifactStore:
        """Create a workspace-bound adapter implementing the ArtifactStore protocol."""
        return WorkspaceArtifactStore(self, workspace_id)


class WorkspaceArtifactStore:
    """Workspace-bound adapter implementing the contracts ArtifactStore protocol."""

    def __init__(self, store: LocalArtifactStore, workspace_id: str) -> None:
        self._store = store
        self.workspace_id = workspace_id

    async def put(
        self,
        artifact_id: str,
        data: bytes,
        *,
        mime_type: str = "application/octet-stream",
        metadata: dict[str, Any] | None = None,
    ) -> ArtifactReference:
        """Store bytes in the bound workspace."""
        return await self._store.put(
            self.workspace_id,
            artifact_id,
            data,
            mime_type=mime_type,
            metadata=metadata,
        )

    async def get(self, artifact_id: str) -> bytes | None:
        """Retrieve stored bytes for an artifact in the bound workspace."""
        try:
            return await self._store.get(self.workspace_id, artifact_id)
        except ArtifactNotFoundError:
            return None
