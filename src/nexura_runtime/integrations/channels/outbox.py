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


class ChannelStore(Protocol):
    """Storage protocol for outbound message receipts, dry-run outbox, and deduplication."""

    def is_duplicate(self, key: str) -> bool: ...

    def record_dedup(self, key: str, status: str = "processed") -> bool: ...

    def record_receipt(self, receipt: OutboxReceipt) -> None: ...

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

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._dedup: dict[str, float] = {}
        self._receipts: dict[str, OutboxReceipt] = {}

    def is_duplicate(self, key: str) -> bool:
        with self._lock:
            return key in self._dedup

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
    """Crash-resilient persistent SQLite store for outbox and deduplication."""

    def __init__(self, db_path: str = ":memory:") -> None:
        self.db_path = db_path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_db()

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
            self._conn.commit()

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
                "ORDER BY created_at DESC LIMIT 1",
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
            query += " ORDER BY created_at ASC"
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
            self._conn.close()
