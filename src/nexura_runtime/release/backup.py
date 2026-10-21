# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Durable backup and restore engine for database checkpoints and artifact stores."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
import time
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class BackupManifest:
    """Metadata describing a generated backup archive."""

    version: int
    created_at: float
    database_file: str
    database_hash: str
    artifact_count: int
    archive_hash: str
    metadata: dict[str, Any]
    artifact_hashes: dict[str, str] = field(default_factory=dict)


class BackupIntegrityError(Exception):
    """Raised when a backup archive fails cryptographic integrity verification."""


class UnsupportedBackupVersionError(BackupIntegrityError):
    """Raised when an archive uses a manifest version this build cannot verify."""

    def __init__(self, version: object, supported: frozenset[int]) -> None:
        super().__init__(
            f"Backup manifest version {version!r} is not supported; this build restores "
            f"versions {sorted(supported)}. Re-create the backup with the current release."
        )
        self.version = version
        self.supported_versions = sorted(supported)


class BackupEngine:
    """Handles consistent point-in-time backups and verifiable restores.

    Guarantees:
    - Consistent DB Snapshot: Uses SQLite online backup API (`conn.backup`) to avoid dirty reads.
    - Cryptographic Manifest: Computes deterministic SHA-256 digests for all files.
    - Path Escape Defense: Verifies that archive member paths cannot escape restore
      target directories.
    - State Durability: Preserves pending approvals, checkpoints, and immutable artifacts.
    """

    MANIFEST_FILENAME = "manifest.json"
    # Version 1 archives had no per-artifact hashes, so they cannot be verified.
    SUPPORTED_MANIFEST_VERSIONS: frozenset[int] = frozenset({2})
    DB_MEMBER_NAME = "database.sqlite3"
    ARTIFACTS_MEMBER_PREFIX = "artifacts/"

    @staticmethod
    def _compute_sha256(path: Path) -> str:
        hasher = hashlib.sha256()
        with open(path, "rb") as f:
            while chunk := f.read(65536):
                hasher.update(chunk)
        return hasher.hexdigest()

    def create_backup(
        self,
        db_path: str | Path,
        artifact_dir: str | Path | None = None,
        *,
        target_archive_path: str | Path,
        metadata: dict[str, Any] | None = None,
    ) -> BackupManifest:
        """Create a point-in-time compressed backup archive."""
        source_db = Path(db_path).resolve()
        source_artifacts = Path(artifact_dir).resolve() if artifact_dir else None
        dest_archive = Path(target_archive_path).resolve()
        dest_archive.parent.mkdir(parents=True, exist_ok=True)

        with tempfile.TemporaryDirectory(prefix="nexura_backup_stage_") as stage_dir_str:
            stage_dir = Path(stage_dir_str)
            staged_db = stage_dir / self.DB_MEMBER_NAME

            # 1. Consistent SQLite online snapshot
            if source_db.exists():
                src_conn = sqlite3.connect(str(source_db), timeout=10.0)
                dest_conn = sqlite3.connect(str(staged_db))
                try:
                    src_conn.backup(dest_conn)
                finally:
                    dest_conn.close()
                    src_conn.close()
            else:
                # Create empty SQLite DB if none exists yet
                conn = sqlite3.connect(str(staged_db))
                conn.close()

            db_hash = self._compute_sha256(staged_db)

            # 2. Stage artifacts
            staged_artifacts = stage_dir / "artifacts"
            staged_artifacts.mkdir(parents=True, exist_ok=True)
            artifact_count = 0
            artifact_hashes: dict[str, str] = {}
            if source_artifacts and source_artifacts.exists():
                for root, _, files in os.walk(source_artifacts):
                    for file_name in files:
                        src_file = Path(root) / file_name
                        rel_path = src_file.relative_to(source_artifacts)
                        dest_file = staged_artifacts / rel_path
                        dest_file.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(src_file, dest_file)
                        artifact_hashes[rel_path.as_posix()] = self._compute_sha256(dest_file)
                        artifact_count += 1

            # 3. Create manifest (pre-archive)
            manifest_dict: dict[str, Any] = {
                "version": 2,
                "created_at": time.time(),
                "database_file": self.DB_MEMBER_NAME,
                "database_hash": db_hash,
                "artifact_count": artifact_count,
                "artifact_hashes": artifact_hashes,
                "archive_hash": "",  # Populated after packaging
                "metadata": metadata or {},
            }
            manifest_path = stage_dir / self.MANIFEST_FILENAME
            manifest_path.write_text(json.dumps(manifest_dict, indent=2), encoding="utf-8")

            # 4. Package into .tar.gz archive
            with tarfile.open(dest_archive, "w:gz") as tar:
                tar.add(manifest_path, arcname=self.MANIFEST_FILENAME)
                tar.add(staged_db, arcname=self.DB_MEMBER_NAME)
                if artifact_count > 0:
                    tar.add(staged_artifacts, arcname="artifacts")

            # 5. Compute final archive hash and write final manifest
            archive_hash = self._compute_sha256(dest_archive)
            manifest_dict["archive_hash"] = archive_hash

            return BackupManifest(**manifest_dict)

    def restore_backup(
        self,
        archive_path: str | Path,
        *,
        target_db_path: str | Path,
        target_artifact_dir: str | Path | None = None,
    ) -> BackupManifest:
        """Verify and restore database and artifacts from a backup archive."""
        archive = Path(archive_path).resolve()
        dest_db = Path(target_db_path).resolve()
        dest_artifacts = Path(target_artifact_dir).resolve() if target_artifact_dir else None

        if not archive.exists():
            raise FileNotFoundError(f"Backup archive not found at '{archive}'")

        dest_db.parent.mkdir(parents=True, exist_ok=True)
        if dest_artifacts:
            dest_artifacts.mkdir(parents=True, exist_ok=True)

        with tempfile.TemporaryDirectory(prefix="nexura_restore_stage_") as stage_dir_str:
            stage_dir = Path(stage_dir_str)

            # 1. Safe extraction
            try:
                with tarfile.open(archive, "r:gz") as tar:
                    for member in tar.getmembers():
                        # Reject path traversal attacks
                        target_path = (stage_dir / member.name).resolve()
                        if not target_path.is_relative_to(stage_dir):
                            raise BackupIntegrityError(
                                f"Suspicious path in backup archive: '{member.name}'"
                            )
                    tar.extractall(stage_dir, filter="data")
            except (tarfile.TarError, EOFError, OSError) as exc:
                raise BackupIntegrityError(f"Corrupted backup archive: {exc}") from exc

            # 2. Read and verify manifest
            manifest_file = stage_dir / self.MANIFEST_FILENAME
            if not manifest_file.exists():
                raise BackupIntegrityError("Archive missing required manifest.json")

            try:
                manifest_data = json.loads(manifest_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise BackupIntegrityError(f"Corrupted manifest.json in archive: {exc}") from exc
            if not isinstance(manifest_data, dict):
                raise BackupIntegrityError("Invalid backup manifest schema")
            if manifest_data.get("version") not in self.SUPPORTED_MANIFEST_VERSIONS:
                raise UnsupportedBackupVersionError(
                    manifest_data.get("version"), self.SUPPORTED_MANIFEST_VERSIONS
                )
            staged_db = stage_dir / self.DB_MEMBER_NAME
            if not staged_db.exists():
                raise BackupIntegrityError("Archive missing required database file")

            if not isinstance(manifest_data, dict) or "database_hash" not in manifest_data:
                raise BackupIntegrityError("Manifest is missing required 'database_hash' field")

            actual_db_hash = self._compute_sha256(staged_db)
            if actual_db_hash != manifest_data["database_hash"]:
                raise BackupIntegrityError(
                    f"Database hash mismatch! Expected {manifest_data['database_hash']}, "
                    f"got {actual_db_hash}"
                )

            # Verify every artifact and the database before publishing any restored state.
            staged_artifacts = stage_dir / "artifacts"
            artifact_files = {
                path.relative_to(staged_artifacts).as_posix(): path
                for path in staged_artifacts.rglob("*")
                if path.is_file()
            }
            expected_hashes = manifest_data.get("artifact_hashes", {})
            if not isinstance(expected_hashes, dict) or set(expected_hashes) != set(artifact_files):
                raise BackupIntegrityError("Artifact manifest does not match archive contents")
            for relative, path in artifact_files.items():
                if path.is_symlink() or self._compute_sha256(path) != expected_hashes[relative]:
                    raise BackupIntegrityError(f"Artifact hash mismatch: {relative}")
                if dest_artifacts and not (dest_artifacts / relative).resolve().is_relative_to(
                    dest_artifacts
                ):
                    raise BackupIntegrityError("Restore destination contains an escaping symlink")
            try:
                result_manifest = BackupManifest(**manifest_data)
            except (TypeError, ValueError) as exc:
                raise BackupIntegrityError("Invalid backup manifest schema") from exc
            with closing(sqlite3.connect(f"file:{staged_db}?mode=ro", uri=True)) as conn, conn:
                check_result = conn.execute("PRAGMA integrity_check").fetchone()
                if not check_result or check_result[0] != "ok":
                    raise BackupIntegrityError("Backup SQLite database failed integrity check")

            # 3. Restore database atomically
            temp_target_db = dest_db.with_suffix(".tmp_restore")
            shutil.copy2(staged_db, temp_target_db)
            temp_target_db.replace(dest_db)

            # 4. Restore artifacts
            if dest_artifacts and staged_artifacts.exists():
                for root, _, files in os.walk(staged_artifacts):
                    for file_name in files:
                        src_file = Path(root) / file_name
                        rel_path = src_file.relative_to(staged_artifacts)
                        dest_file = dest_artifacts / rel_path
                        dest_file.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(src_file, dest_file)

            # 5. Verify restored database opens
            with closing(sqlite3.connect(str(dest_db), timeout=5.0)) as conn, conn:
                cursor = conn.cursor()
                cursor.execute("PRAGMA integrity_check;")
                check_result = cursor.fetchone()
                if not check_result or check_result[0] != "ok":
                    raise BackupIntegrityError(f"SQLite integrity check failed: {check_result}")

            return result_manifest
