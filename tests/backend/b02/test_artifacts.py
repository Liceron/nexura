"""Focused acceptance tests for B02: Artifact and file storage."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from nexura_runtime.artifacts import (
    ArtifactHashMismatchError,
    ArtifactLimitExceededError,
    ArtifactNotFoundError,
    ArtifactSecurityError,
    LocalArtifactStore,
)


@pytest.mark.asyncio
async def test_atomic_writes_and_interrupted_upload_isolation(tmp_path: Path) -> None:
    """Interrupted uploads never expose partial blobs or metadata."""
    store = LocalArtifactStore(root_dir=tmp_path)
    ws = "ws_test"
    art_id = "art_partial"

    # Simulate an error during atomic replace
    with (
        patch("os.replace", side_effect=OSError("Disk write error")),
        pytest.raises(OSError, match="Disk write error"),
    ):
        await store.put(ws, art_id, b"some content that failed mid-write")

    # Verify that target blob does not exist in the store
    assert not await store.exists(ws, art_id)
    ws_dir = tmp_path / ws
    if ws_dir.exists():
        assert not (ws_dir / f"{art_id}.bin").exists()
        assert not (ws_dir / f"{art_id}.meta.json").exists()


@pytest.mark.asyncio
async def test_traversal_and_symlink_escapes_rejected(tmp_path: Path) -> None:
    """Path traversal attacks and symlink escapes are strictly rejected."""
    store = LocalArtifactStore(root_dir=tmp_path)

    # 1. Invalid characters / directory traversal in workspace_id
    with pytest.raises(ArtifactSecurityError):
        await store.put("../../etc", "art_1", b"secret")

    # 2. Invalid characters / directory traversal in artifact_id
    with pytest.raises(ArtifactSecurityError):
        await store.put("ws_1", "../escape", b"secret")

    # 3. Symlink escape check
    ws_dir = tmp_path / "ws_sym"
    ws_dir.mkdir(parents=True, exist_ok=True)
    symlink_target = ws_dir / "art_sym.bin"
    external_file = tmp_path.parent / "escape.txt"
    external_file.touch()

    try:
        os.symlink(external_file, symlink_target)
        with pytest.raises(ArtifactSecurityError, match="Symlink escape detected"):
            await store.get("ws_sym", "art_sym")
    finally:
        if symlink_target.is_symlink():
            symlink_target.unlink()
        if external_file.exists():
            external_file.unlink()


@pytest.mark.asyncio
async def test_workspace_isolation_and_mismatch_rejected(tmp_path: Path) -> None:
    """Artifact stored in workspace A cannot be read or accessed by workspace B."""
    store = LocalArtifactStore(root_dir=tmp_path)

    await store.put("workspace_alpha", "doc_1", b"alpha data")

    # Alpha can read it
    data = await store.get("workspace_alpha", "doc_1")
    assert data == b"alpha data"

    # Beta cannot find it
    with pytest.raises(ArtifactNotFoundError) as exc_info:
        await store.get("workspace_beta", "doc_1")
    assert "not found in workspace 'workspace_beta'" in exc_info.value.message


@pytest.mark.asyncio
async def test_content_hashes_and_tamper_detection(tmp_path: Path) -> None:
    """Content SHA-256 digest is verified, and file tampering raises ArtifactHashMismatchError."""
    store = LocalArtifactStore(root_dir=tmp_path)

    ref = await store.put("ws_hash", "doc_hash", b"original pristine data")
    assert ref.size_bytes == len(b"original pristine data")
    assert len(ref.digest) == 64

    # Direct retrieval matches
    data = await store.get("ws_hash", "doc_hash")
    assert data == b"original pristine data"

    # Tamper with the raw blob file on disk
    blob_path = tmp_path / "ws_hash" / "doc_hash.bin"
    blob_path.write_bytes(b"tampered corrupted data")

    with pytest.raises(ArtifactHashMismatchError) as exc_info:
        await store.get("ws_hash", "doc_hash")
    assert "checksum verification failed" in exc_info.value.message


@pytest.mark.asyncio
async def test_oversized_files_terminate_within_bounds(tmp_path: Path) -> None:
    """Files exceeding max_size_bytes are rejected before writing."""
    store = LocalArtifactStore(root_dir=tmp_path, max_size_bytes=1024)  # 1 KB limit

    oversized_data = b"x" * 2048
    with pytest.raises(ArtifactLimitExceededError) as exc_info:
        await store.put("ws_limit", "doc_huge", oversized_data)
    assert exc_info.value.details["size_bytes"] == 2048
    assert exc_info.value.details["max_bytes"] == 1024


@pytest.mark.asyncio
async def test_range_reads_and_deletion(tmp_path: Path) -> None:
    """Range reads and deletion operate properly."""
    store = LocalArtifactStore(root_dir=tmp_path)

    await store.put("ws_ops", "doc_ops", b"0123456789ABCDEF")

    # Range read
    chunk1 = await store.get_range("ws_ops", "doc_ops", start=0, length=4)
    assert chunk1 == b"0123"

    chunk2 = await store.get_range("ws_ops", "doc_ops", start=10, length=6)
    assert chunk2 == b"ABCDEF"

    # Deletion
    existed = await store.delete("ws_ops", "doc_ops")
    assert existed is True
    assert not await store.exists("ws_ops", "doc_ops")

    # Deleting non-existent returns False
    assert await store.delete("ws_ops", "doc_ops") is False
