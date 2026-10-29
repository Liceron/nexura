# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Explicit, bounded retention shared by stores and the operator command.

No background cleanup is registered here. Cutoffs are exclusive UTC epoch seconds.
"""

from __future__ import annotations

import math
import sqlite3
import time

DEFAULT_PRUNE_LIMIT = 1000
MAX_PRUNE_LIMIT = 10_000
# Operational contract, not a provider guarantee: retain keys for at least seven days.
# Deployments with longer delivery/replay windows must configure a longer retry window.
MIN_DEDUP_RETENTION_SECONDS = 7 * 86400.0


def prune_cutoff(
    older_than_seconds: float,
    now: float | None,
    limit: int,
    *,
    minimum: float = 0,
) -> float:
    if not math.isfinite(older_than_seconds) or older_than_seconds <= 0:
        raise ValueError("older_than_seconds must be finite and positive")
    if not math.isfinite(minimum) or minimum < 0:
        raise ValueError("delivery retry window must be finite and nonnegative")
    if older_than_seconds < minimum:
        raise ValueError(
            f"older_than_seconds must be at least the delivery retry window ({minimum:g})"
        )
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_PRUNE_LIMIT:
        raise ValueError(f"limit must be an integer between 1 and {MAX_PRUNE_LIMIT}")
    stamp = time.time() if now is None else now
    if not math.isfinite(stamp) or not math.isfinite(stamp - older_than_seconds):
        raise ValueError("now and cutoff must be finite")
    return stamp - older_than_seconds


def sqlite_prune_channels(
    conn: sqlite3.Connection,
    cutoff: float,
    limit: int,
    *,
    dry_run: bool,
    dedup: bool,
    outbox: bool,
) -> dict[str, int]:
    """At most ``limit`` rows total. Each deletion selects and deletes atomically."""
    counts = {"dedup_keys": 0, "outbox_receipts": 0}
    for enabled, table, column, label in (
        (dedup, "channel_dedup", "processed_at", "dedup_keys"),
        (outbox, "channel_outbox", "created_at", "outbox_receipts"),
    ):
        remaining = limit - sum(counts.values())
        if not enabled or remaining == 0:
            continue
        selection = f"SELECT rowid FROM {table} WHERE {column} < ? ORDER BY {column}, rowid LIMIT ?"
        if dry_run:
            counts[label] = conn.execute(
                f"SELECT count(*) FROM ({selection})", (cutoff, remaining)
            ).fetchone()[0]
        else:
            counts[label] = conn.execute(
                f"DELETE FROM {table} WHERE rowid IN ({selection})", (cutoff, remaining)
            ).rowcount
    return counts


def sqlite_prune_traces(
    conn: sqlite3.Connection, cutoff: float, limit: int, *, dry_run: bool
) -> dict[str, int]:
    # Legacy traces without a summary or timestamp are deliberately retained. Never decode
    # trace_json: it may be a blob reference, and row retention must perform no blob I/O.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(eval_traces)")}
    # Drive the summary branch from the run-time index. CROSS JOIN fixes SQLite's
    # join order so a small batch never starts with a full scan of trace rows.
    selection = (
        "SELECT t.rowid AS trace_rowid, r.created_at AS retention_time "
        "FROM eval_runs r CROSS JOIN eval_traces t "
        "WHERE r.created_at < :cutoff "
        "AND t.run_id = r.run_id AND t.workspace_id = r.workspace_id "
        "ORDER BY r.created_at, t.rowid LIMIT :limit"
    )
    if "created_at" in columns:
        selection = (
            f"SELECT * FROM ({selection}) UNION ALL SELECT * FROM ("
            "SELECT t.rowid AS trace_rowid, t.created_at AS retention_time "
            "FROM eval_traces t WHERE t.created_at < :cutoff AND NOT EXISTS ("
            "SELECT 1 FROM eval_runs r "
            "WHERE r.run_id = t.run_id AND r.workspace_id = t.workspace_id "
            "AND r.created_at IS NOT NULL) "
            "ORDER BY t.created_at, t.rowid LIMIT :limit)"
        )
    selection = (
        f"SELECT trace_rowid FROM ({selection}) ORDER BY retention_time, trace_rowid LIMIT :limit"
    )
    params = {"cutoff": cutoff, "limit": limit}
    if dry_run:
        count = conn.execute(f"SELECT count(*) FROM ({selection})", params).fetchone()[0]
    else:
        count = conn.execute(
            f"DELETE FROM eval_traces WHERE rowid IN ({selection})", params
        ).rowcount
    return {"eval_traces": count}
