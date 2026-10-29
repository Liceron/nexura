# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Channel outbox persistence and deduplication tracking."""

from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
import time
from typing import Any, Protocol

from nexura_runtime.integrations.channels.models import (
    ChannelType,
    OutboxReceipt,
)
from nexura_runtime.store_retention import (
    DEFAULT_PRUNE_LIMIT,
    MIN_DEDUP_RETENTION_SECONDS,
    prune_cutoff,
    sqlite_prune_channels,
)


class ChannelStore(Protocol):
    """Storage protocol for outbound message receipts, dry-run outbox, and deduplication."""

    def is_duplicate(self, key: str) -> bool: ...

    def record_dedup(self, key: str, status: str = "processed") -> bool: ...

    def record_receipt(self, receipt: OutboxReceipt) -> None: ...

    def prune(
        self,
        older_than_seconds: float,
        *,
        now: float | None = None,
        limit: int = DEFAULT_PRUNE_LIMIT,
        dry_run: bool = False,
        dedup: bool = True,
        outbox: bool = True,
    ) -> dict[str, int]: ...

    def get_receipt(self, receipt_id: str) -> OutboxReceipt | None: ...

    def get_receipt_by_message_id(self, message_id: str) -> OutboxReceipt | None: ...

    def list_outbox(
        self,
        channel: ChannelType | None = None,
        dry_run: bool | None = None,
        *,
        workspace_id: str | None = None,
        include_unscoped: bool = False,
    ) -> list[OutboxReceipt]:
        """List receipts. With ``workspace_id`` only that workspace's receipts are returned
        (plus workspace-less legacy receipts when ``include_unscoped`` is set)."""
        ...


class MemoryChannelStore:
    """Thread-safe in-memory store for channel deduplication and outbox receipts."""

    blocking_io = False

    def __init__(
        self, *, delivery_retry_window_seconds: float = MIN_DEDUP_RETENTION_SECONDS
    ) -> None:
        prune_cutoff(delivery_retry_window_seconds, None, 1, minimum=MIN_DEDUP_RETENTION_SECONDS)
        self.delivery_retry_window_seconds = delivery_retry_window_seconds
        self._lock = threading.Lock()
        self._dedup: dict[str, float] = {}
        self._receipts: dict[str, OutboxReceipt] = {}

    def is_duplicate(self, key: str) -> bool:
        with self._lock:
            return key in self._dedup

    def prune(
        self,
        older_than_seconds: float,
        *,
        now: float | None = None,
        limit: int = DEFAULT_PRUNE_LIMIT,
        dry_run: bool = False,
        dedup: bool = True,
        outbox: bool = True,
    ) -> dict[str, int]:
        """Expire at most ``limit`` rows total, strictly older than the cutoff.

        Dedup retention must cover the configured delivery retry window (minimum seven
        days). Disable ``dedup`` to expire receipts with a shorter independent policy.
        """
        cutoff = prune_cutoff(
            older_than_seconds,
            now,
            limit,
            minimum=self.delivery_retry_window_seconds if dedup else 0,
        )
        with self._lock:
            keys = (
                sorted(
                    (k for k, stamp in self._dedup.items() if stamp < cutoff),
                    key=self._dedup.__getitem__,
                )[:limit]
                if dedup
                else []
            )
            receipts = (
                sorted(
                    (k for k, r in self._receipts.items() if r.created_at < cutoff),
                    key=lambda k: self._receipts[k].created_at,
                )[: limit - len(keys)]
                if outbox
                else []
            )
            if not dry_run:
                for key in keys:
                    del self._dedup[key]
                for key in receipts:
                    del self._receipts[key]
            return {"dedup_keys": len(keys), "outbox_receipts": len(receipts)}

    def record_dedup(self, key: str, status: str = "processed") -> bool:
        with self._lock:
            if key in self._dedup:
                return False
            self._dedup[key] = time.time()
            return True

    def record_receipt(self, receipt: OutboxReceipt) -> None:
        with self._lock:
            self._receipts[receipt.receipt_id] = receipt

    def get_receipt(self, receipt_id: str) -> OutboxReceipt | None:
        with self._lock:
            return self._receipts.get(receipt_id)

    def get_receipt_by_message_id(self, message_id: str) -> OutboxReceipt | None:
        with self._lock:
            for r in self._receipts.values():
                if r.message_id == message_id:
                    return r
            return None

    def list_outbox(
        self,
        channel: ChannelType | None = None,
        dry_run: bool | None = None,
        *,
        workspace_id: str | None = None,
        include_unscoped: bool = False,
    ) -> list[OutboxReceipt]:
        with self._lock:
            items = list(self._receipts.values())
            if workspace_id is not None:
                items = [
                    r
                    for r in items
                    if r.workspace_id == workspace_id
                    or (include_unscoped and r.workspace_id is None)
                ]
            if channel is not None:
                items = [r for r in items if r.channel == channel]
            if dry_run is not None:
                items = [r for r in items if r.dry_run == dry_run]
            items.sort(key=lambda r: r.created_at)
            return items


class SqliteChannelStore:
    """Crash-resilient persistent SQLite store for outbox and deduplication.

    ``nexura_postgres.PostgresChannelStore`` is the Postgres twin (MVP WP8). Receipts list by
    ``created_at``, then by insertion (``rowid``; a replaced receipt counts as new).
    """

    #: Local file I/O: async callers may run calls inline (Postgres: a network round trip).
    blocking_io = False

    def __init__(
        self,
        db_path: str = ":memory:",
        *,
        delivery_retry_window_seconds: float = MIN_DEDUP_RETENTION_SECONDS,
    ) -> None:
        prune_cutoff(delivery_retry_window_seconds, None, 1, minimum=MIN_DEDUP_RETENTION_SECONDS)
        self.delivery_retry_window_seconds = delivery_retry_window_seconds
        self.db_path = db_path
        self._lock = threading.RLock()
        self._connection: sqlite3.Connection | None = None
        self._init_db()

    @property
    def _conn(self) -> sqlite3.Connection:
        # Opened on first use and again after close(), so an app restarted after its
        # shutdown closed the store keeps working.
        with self._lock:
            if self._connection is None:
                self._connection = sqlite3.connect(self.db_path, check_same_thread=False)
                self._connection.row_factory = sqlite3.Row
            return self._connection

    def _init_db(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA journal_mode = WAL;")
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS channel_dedup (
                    key TEXT PRIMARY KEY,
                    processed_at REAL NOT NULL,
                    status TEXT NOT NULL
                );
            """)
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS channel_outbox (
                    receipt_id TEXT PRIMARY KEY,
                    message_id TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    recipient TEXT NOT NULL,
                    status TEXT NOT NULL,
                    dry_run INTEGER NOT NULL,
                    rendered_json TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    metadata_json TEXT NOT NULL,
                    workspace_id TEXT
                );
            """)
            columns = {r["name"] for r in self._conn.execute("PRAGMA table_info(channel_outbox)")}
            if "workspace_id" not in columns:
                with contextlib.suppress(sqlite3.OperationalError):
                    self._conn.execute("ALTER TABLE channel_outbox ADD COLUMN workspace_id TEXT")
            self._conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_outbox_msg ON channel_outbox(message_id);
            """)
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_dedup_retention ON channel_dedup(processed_at)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_outbox_retention ON channel_outbox(created_at)"
            )
            self._conn.commit()

    def prune(
        self,
        older_than_seconds: float,
        *,
        now: float | None = None,
        limit: int = DEFAULT_PRUNE_LIMIT,
        dry_run: bool = False,
        dedup: bool = True,
        outbox: bool = True,
    ) -> dict[str, int]:
        """Bounded row retention; see :meth:`MemoryChannelStore.prune`."""
        cutoff = prune_cutoff(
            older_than_seconds,
            now,
            limit,
            minimum=self.delivery_retry_window_seconds if dedup else 0,
        )
        with self._lock, self._conn:
            return sqlite_prune_channels(
                self._conn, cutoff, limit, dry_run=dry_run, dedup=dedup, outbox=outbox
            )

    def is_duplicate(self, key: str) -> bool:
        with self._lock:
            cur = self._conn.execute("SELECT 1 FROM channel_dedup WHERE key = ?", (key,))
            return cur.fetchone() is not None

    def record_dedup(self, key: str, status: str = "processed") -> bool:
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO channel_dedup (key, processed_at, status) VALUES (?, ?, ?)",
                    (key, time.time(), status),
                )
                self._conn.commit()
                return True
            except sqlite3.IntegrityError:
                # End the implicit transaction the failed INSERT opened, so this connection does
                # not keep the file's write lock until its next commit (MVP WP8).
                self._conn.rollback()
                return False

    def record_receipt(self, receipt: OutboxReceipt) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO channel_outbox (
                    receipt_id, message_id, channel, recipient, status,
                    dry_run, rendered_json, attempt_count, created_at, metadata_json,
                    workspace_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt.receipt_id,
                    receipt.message_id,
                    receipt.channel,
                    receipt.recipient,
                    receipt.status,
                    1 if receipt.dry_run else 0,
                    json.dumps(receipt.rendered_content),
                    receipt.attempt_count,
                    receipt.created_at,
                    json.dumps(receipt.metadata),
                    receipt.workspace_id,
                ),
            )
            self._conn.commit()

    def get_receipt(self, receipt_id: str) -> OutboxReceipt | None:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM channel_outbox WHERE receipt_id = ?", (receipt_id,)
            )
            row = cur.fetchone()
            if not row:
                return None
            return self._row_to_receipt(row)

    def get_receipt_by_message_id(self, message_id: str) -> OutboxReceipt | None:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM channel_outbox WHERE message_id = ? "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (message_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return self._row_to_receipt(row)

    def list_outbox(
        self,
        channel: ChannelType | None = None,
        dry_run: bool | None = None,
        *,
        workspace_id: str | None = None,
        include_unscoped: bool = False,
    ) -> list[OutboxReceipt]:
        with self._lock:
            query = "SELECT * FROM channel_outbox WHERE 1=1"
            params: list[Any] = []
            if workspace_id is not None:
                if include_unscoped:
                    query += " AND (workspace_id = ? OR workspace_id IS NULL)"
                else:
                    query += " AND workspace_id = ?"
                params.append(workspace_id)
            if channel is not None:
                query += " AND channel = ?"
                params.append(channel)
            if dry_run is not None:
                query += " AND dry_run = ?"
                params.append(1 if dry_run else 0)
            query += " ORDER BY created_at ASC, rowid ASC"
            cur = self._conn.execute(query, params)
            return [self._row_to_receipt(r) for r in cur.fetchall()]

    def _row_to_receipt(self, row: sqlite3.Row) -> OutboxReceipt:
        return OutboxReceipt(
            receipt_id=row["receipt_id"],
            message_id=row["message_id"],
            channel=row["channel"],
            recipient=row["recipient"],
            status=row["status"],
            dry_run=bool(row["dry_run"]),
            rendered_content=json.loads(row["rendered_json"]),
            attempt_count=row["attempt_count"],
            created_at=row["created_at"],
            metadata=json.loads(row["metadata_json"]),
            workspace_id=row["workspace_id"],
        )

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                # A file database reopens on next use; an in-memory one is gone, so later
                # use still fails instead of silently starting empty.
                if self.db_path != ":memory:":
                    self._connection = None
