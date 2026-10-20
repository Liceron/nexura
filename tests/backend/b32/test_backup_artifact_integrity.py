"""Tampering with a valid archive must not publish corrupted restore state."""

import io
import sqlite3
import tarfile
from pathlib import Path

import pytest

from nexura_runtime.release.backup import BackupEngine, BackupIntegrityError


def test_repacked_artifact_tampering_rejected_before_database_restore(tmp_path: Path) -> None:
    database = tmp_path / "source.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE sample (value TEXT)")
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "sample.txt").write_text("original")
    archive = tmp_path / "backup.tar.gz"
    BackupEngine().create_backup(database, artifacts, target_archive_path=archive)
    tampered = tmp_path / "tampered.tar.gz"
    with tarfile.open(archive, "r:gz") as source, tarfile.open(tampered, "w:gz") as target:
        for member in source.getmembers():
            stream = source.extractfile(member) if member.isfile() else None
            data = stream.read() if stream is not None else None
            if member.name == "artifacts/sample.txt":
                data = b"changed"
                member.size = len(data)
            target.addfile(member, io.BytesIO(data) if data is not None else None)
    destination = tmp_path / "restored.db"
    with pytest.raises(BackupIntegrityError, match="Artifact hash mismatch"):
        BackupEngine().restore_backup(tampered, target_db_path=destination)
    assert not destination.exists()
