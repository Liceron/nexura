# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Channel delivery dedup keys and the dry-run outbox on Postgres (MVP WP8, store #16).

``PostgresChannelStore`` has the API of
``nexura_runtime.integrations.channels.outbox.SqliteChannelStore`` (migration component
``channels``; the SQLite table names ``channel_dedup`` and ``channel_outbox``).

- **Dedup across instances.** ``record_dedup`` is one ``INSERT … ON CONFLICT (key) DO NOTHING
  RETURNING``: the insert itself detects the conflict (never a read followed by a write), so of
  any number of instances recording one key at once exactly one gets ``True``, and a key
  recorded on one instance is a duplicate on every other (design §2b-5).
- **Receipts are shared.** ``record_receipt`` replaces a receipt with the same id as SQLite's
  ``INSERT OR REPLACE`` does, including its place in the list: ``seq`` (the identity column that
  replaces SQLite's ``rowid`` as the tie-break of ``created_at``) is renewed.
- **NUL.** Postgres ``TEXT`` cannot hold NUL. The JSON columns never contain one; in every other
  text value (keys, recipients, ids) NUL is stored, and looked up, as U+FFFD.

Every call is a network round trip (``blocking_io = True``); the service's async routes run them
in a worker thread. Pooled-endpoint safety: one statement per transaction, no session state.
"""

from __future__ import annotations

import json
import time
from typing import Any

from nexura_postgres.migrations import MIGRATION_TIMEOUT, Migration, migrate_all, register
from nexura_runtime.integrations.channels.models import ChannelType, OutboxReceipt
from nexura_runtime.store_retention import (
    DEFAULT_PRUNE_LIMIT,
    MIN_DEDUP_RETENTION_SECONDS,
    prune_cutoff,
)

COMPONENT = "channels"
MIGRATIONS: tuple[Migration, ...] = (
    (
        1,
        "channel dedup keys and outbox receipts",
        (
            """CREATE TABLE channel_dedup (
                key TEXT PRIMARY KEY,
                processed_at DOUBLE PRECISION NOT NULL,
                status TEXT NOT NULL)""",
            """CREATE TABLE channel_outbox (
                receipt_id TEXT PRIMARY KEY,
                message_id TEXT NOT NULL,
                channel TEXT NOT NULL,
                recipient TEXT NOT NULL,
                status TEXT NOT NULL,
                dry_run BOOLEAN NOT NULL,
                rendered_json TEXT NOT NULL,
                attempt_count BIGINT NOT NULL,
                created_at DOUBLE PRECISION NOT NULL,
                metadata_json TEXT NOT NULL,
                workspace_id TEXT,
                seq BIGINT GENERATED ALWAYS AS IDENTITY)""",
            "CREATE INDEX channel_outbox_msg ON channel_outbox (message_id, created_at, seq)",
            "CREATE INDEX channel_outbox_ws ON channel_outbox (workspace_id, created_at, seq)",
        ),
    ),
)
register(COMPONENT, MIGRATIONS)

# Additive retention component: the WP8 v1 component and its checksums stay unchanged.
RETENTION_COMPONENT = "channel_retention"
RETENTION_MIGRATIONS: tuple[Migration, ...] = (
    (
        1,
        "channel retention indexes",
        (
            "CREATE INDEX channel_dedup_retention ON channel_dedup (processed_at, key)",
            "CREATE INDEX channel_outbox_retention ON channel_outbox (created_at, receipt_id)",
        ),
    ),
)
register(RETENTION_COMPONENT, RETENTION_MIGRATIONS)


def _t(value: str) -> str:
    """Postgres ``TEXT`` cannot hold NUL: stored (and looked up) as U+FFFD."""
    return value.replace("\x00", "\ufffd")


class PostgresChannelStore:
    """Dedup keys and outbox receipts in the shared Postgres ``Database``; the store never
    closes it. Opening the store applies its migrations (component ``channels``)."""

    blocking_io = True

    def __init__(
        self,
        database: Any,
        *,
        migrate: bool = True,
        delivery_retry_window_seconds: float = MIN_DEDUP_RETENTION_SECONDS,
    ) -> None:
        prune_cutoff(delivery_retry_window_seconds, None, 1, minimum=MIN_DEDUP_RETENTION_SECONDS)
        self.delivery_retry_window_seconds = delivery_retry_window_seconds
        self.database = database
        if migrate:

            async def apply(conn: Any) -> None:
                await migrate_all(
                    conn,
                    {COMPONENT: MIGRATIONS, RETENTION_COMPONENT: RETENTION_MIGRATIONS},
                )

            database.run(apply, timeout=MIGRATION_TIMEOUT + 30)

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
        """Delete at most ``limit`` rows total, across instances without double deletion.

        Each table batch selects, locks and deletes in one statement. Dry runs only count
        a bounded selection and take no row locks; concurrent writers can change the count.
        """
        cutoff = prune_cutoff(
            older_than_seconds,
            now,
            limit,
            minimum=self.delivery_retry_window_seconds if dedup else 0,
        )
        counts = {"dedup_keys": 0, "outbox_receipts": 0}
        for enabled, table, column, key, label in (
            (dedup, "channel_dedup", "processed_at", "key", "dedup_keys"),
            (outbox, "channel_outbox", "created_at", "receipt_id", "outbox_receipts"),
        ):
            remaining = limit - sum(counts.values())
            if not enabled or remaining == 0:
                continue
            selection = (
                f"SELECT {key} FROM {table} WHERE {column} < $1 ORDER BY {column}, {key} LIMIT $2"
            )
            query = (
                f"SELECT count(*) FROM ({selection}) candidates"
                if dry_run
                else f"WITH candidates AS ({selection} FOR UPDATE SKIP LOCKED), "
                f"deleted AS (DELETE FROM {table} t USING candidates c "
                f"WHERE t.{key} = c.{key} RETURNING 1) SELECT count(*) FROM deleted"
            )

            async def run(conn: Any, query: str = query, remaining: int = remaining) -> int:
                return int(await conn.fetchval(query, cutoff, remaining))

            counts[label] = self.database.run(run)
        return counts

    def is_duplicate(self, key: str) -> bool:
        async def run(conn: Any) -> bool:
            found = await conn.fetchval("SELECT 1 FROM channel_dedup WHERE key = $1", _t(key))
            return found is not None

        return bool(self.database.run(run))

    def record_dedup(self, key: str, status: str = "processed") -> bool:
        """``True`` when this call recorded ``key``; ``False`` when it was already recorded, by
        this or any other instance (insert-and-detect-conflict, one statement)."""

        async def run(conn: Any) -> bool:
            inserted = await conn.fetchval(
                "INSERT INTO channel_dedup (key, processed_at, status) VALUES ($1, $2, $3) "
                "ON CONFLICT (key) DO NOTHING RETURNING 1",
                _t(key),
                time.time(),
                _t(status),
            )
            return inserted is not None

        return bool(self.database.run(run))

    def record_receipt(self, receipt: OutboxReceipt) -> None:
        workspace = receipt.workspace_id

        async def run(conn: Any) -> None:
            await conn.execute(
                """INSERT INTO channel_outbox (
                    receipt_id, message_id, channel, recipient, status, dry_run, rendered_json,
                    attempt_count, created_at, metadata_json, workspace_id)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
                ON CONFLICT (receipt_id) DO UPDATE SET
                    message_id = EXCLUDED.message_id, channel = EXCLUDED.channel,
                    recipient = EXCLUDED.recipient, status = EXCLUDED.status,
                    dry_run = EXCLUDED.dry_run, rendered_json = EXCLUDED.rendered_json,
                    attempt_count = EXCLUDED.attempt_count, created_at = EXCLUDED.created_at,
                    metadata_json = EXCLUDED.metadata_json,
                    workspace_id = EXCLUDED.workspace_id, seq = DEFAULT""",
                _t(receipt.receipt_id),
                _t(receipt.message_id),
                _t(receipt.channel),
                _t(receipt.recipient),
                _t(receipt.status),
                bool(receipt.dry_run),
                json.dumps(receipt.rendered_content),
                receipt.attempt_count,
                receipt.created_at,
                json.dumps(receipt.metadata),
                None if workspace is None else _t(workspace),
            )

        self.database.run(run)

    def get_receipt(self, receipt_id: str) -> OutboxReceipt | None:
        async def run(conn: Any) -> Any:
            return await conn.fetchrow(
                "SELECT * FROM channel_outbox WHERE receipt_id = $1", _t(receipt_id)
            )

        row = self.database.run(run)
        return None if row is None else self._row_to_receipt(row)

    def get_receipt_by_message_id(self, message_id: str) -> OutboxReceipt | None:
        async def run(conn: Any) -> Any:
            return await conn.fetchrow(
                "SELECT * FROM channel_outbox WHERE message_id = $1 "
                "ORDER BY created_at DESC, seq DESC LIMIT 1",
                _t(message_id),
            )

        row = self.database.run(run)
        return None if row is None else self._row_to_receipt(row)

    def list_outbox(
        self,
        channel: ChannelType | None = None,
        dry_run: bool | None = None,
        *,
        workspace_id: str | None = None,
        include_unscoped: bool = False,
    ) -> list[OutboxReceipt]:
        clauses: list[str] = []
        params: list[Any] = []
        if workspace_id is not None:
            params.append(_t(workspace_id))
            if include_unscoped:
                clauses.append(f"(workspace_id = ${len(params)} OR workspace_id IS NULL)")
            else:
                clauses.append(f"workspace_id = ${len(params)}")
        if channel is not None:
            params.append(_t(channel))
            clauses.append(f"channel = ${len(params)}")
        if dry_run is not None:
            params.append(bool(dry_run))
            clauses.append(f"dry_run = ${len(params)}")
        # The SQL text comes from the fixed clauses above; values are parameters only.
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        query = f"SELECT * FROM channel_outbox{where} ORDER BY created_at ASC, seq ASC"

        async def run(conn: Any) -> Any:
            return await conn.fetch(query, *params)

        return [self._row_to_receipt(r) for r in self.database.run(run)]

    @staticmethod
    def _row_to_receipt(row: Any) -> OutboxReceipt:
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
        """Nothing to release: the shared ``Database`` belongs to the app."""


__all__ = ["COMPONENT", "MIGRATIONS", "PostgresChannelStore"]
