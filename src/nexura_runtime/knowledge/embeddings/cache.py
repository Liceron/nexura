# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Content-digest embedding cache with in-memory and SQLite backends."""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class EmbeddingCache(Protocol):
    """Protocol for caching vector embeddings by index lineage and content hash."""

    def get(self, lineage_id: str, content_hash: str) -> list[float] | None:
        """Retrieve cached vector for a given lineage and content hash."""
        ...

    def get_many(
        self,
        lineage_id: str,
        content_hashes: Sequence[str],
    ) -> dict[str, list[float]]:
        """Retrieve multiple cached vectors for a given lineage."""
        ...

    def put(
        self,
        lineage_id: str,
        content_hash: str,
        embedding: list[float],
    ) -> None:
        """Store a computed embedding vector."""
        ...

    def put_many(
        self,
        lineage_id: str,
        entries: dict[str, list[float]],
    ) -> None:
        """Store multiple computed embeddings."""
        ...

    def clear(self) -> None:
        """Clear all cached entries."""
        ...


class InMemoryEmbeddingCache:
    """Thread-safe in-memory cache keyed by (lineage_id, content_hash)."""

    def __init__(self) -> None:
        self._store: dict[tuple[str, str], list[float]] = {}

    def get(self, lineage_id: str, content_hash: str) -> list[float] | None:
        return self._store.get((lineage_id, content_hash))

    def get_many(
        self,
        lineage_id: str,
        content_hashes: Sequence[str],
    ) -> dict[str, list[float]]:
        found: dict[str, list[float]] = {}
        for ch in content_hashes:
            vec = self._store.get((lineage_id, ch))
            if vec is not None:
                found[ch] = vec
        return found

    def put(
        self,
        lineage_id: str,
        content_hash: str,
        embedding: list[float],
    ) -> None:
        self._store[(lineage_id, content_hash)] = list(embedding)

    def put_many(
        self,
        lineage_id: str,
        entries: dict[str, list[float]],
    ) -> None:
        for ch, vec in entries.items():
            self._store[(lineage_id, ch)] = list(vec)

    def clear(self) -> None:
        self._store.clear()

    def __len__(self) -> int:
        return len(self._store)


class SqliteEmbeddingCache:
    """Persistent SQLite-backed embedding cache."""

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self.db_path = str(db_path)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._init_db()

    def _init_db(self) -> None:
        with self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS embedding_cache (
                    lineage_id TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    embedding_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (lineage_id, content_hash)
                )
                """
            )

    def get(self, lineage_id: str, content_hash: str) -> list[float] | None:
        cur = self._conn.cursor()
        cur.execute(
            "SELECT embedding_json FROM embedding_cache WHERE lineage_id = ? AND content_hash = ?",
            (lineage_id, content_hash),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return list(json.loads(row[0]))

    def get_many(
        self,
        lineage_id: str,
        content_hashes: Sequence[str],
    ) -> dict[str, list[float]]:
        if not content_hashes:
            return {}
        found: dict[str, list[float]] = {}
        placeholders = ",".join("?" for _ in content_hashes)
        params = [lineage_id, *content_hashes]
        cur = self._conn.cursor()
        cur.execute(
            f"SELECT content_hash, embedding_json FROM embedding_cache "
            f"WHERE lineage_id = ? AND content_hash IN ({placeholders})",
            params,
        )
        for ch, raw_json in cur.fetchall():
            found[ch] = list(json.loads(raw_json))
        return found

    def put(
        self,
        lineage_id: str,
        content_hash: str,
        embedding: list[float],
    ) -> None:
        now = time.time()
        raw_json = json.dumps(embedding)
        with self._conn:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO embedding_cache
                (lineage_id, content_hash, embedding_json, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (lineage_id, content_hash, raw_json, now),
            )

    def put_many(
        self,
        lineage_id: str,
        entries: dict[str, list[float]],
    ) -> None:
        if not entries:
            return
        now = time.time()
        rows = [(lineage_id, ch, json.dumps(vec), now) for ch, vec in entries.items()]
        with self._conn:
            self._conn.executemany(
                """
                INSERT OR REPLACE INTO embedding_cache
                (lineage_id, content_hash, embedding_json, created_at)
                VALUES (?, ?, ?, ?)
                """,
                rows,
            )

    def clear(self) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM embedding_cache")

    def close(self) -> None:
        self._conn.close()
