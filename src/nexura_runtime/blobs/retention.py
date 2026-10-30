# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Blob retention: delete blobs no database row references, and expire old payload blobs.

``collect_blob_garbage`` is safe to run while the service writes:

1. It takes its start time ``t0`` *before* scanning the database for references.
2. Writers put a blob first and commit the row that references it afterwards.
3. A blob is deleted as unreferenced only when it is older than ``t0 - grace``.

So a blob whose row commits after the scan started was written after ``t0 - grace`` unless the
writer took longer than the grace period between its put and its commit, and it is kept. Keys
are random, never content addresses, so no new row can start referencing an old blob. Running it
twice is harmless: deleting a blob that is already gone counts as ``already_gone``.

**Wrong-database protection.** A database records the id of the store its references use
(``nexura_blob_store_binding``, see ``blobs.binding``); every reference records it as well.
Unless ``force`` is given, a store that holds blobs is refused when:

- a scanned database is bound to a different store;
- no scanned database is bound, and none of their references names this store (a dev or
  staging database, a fresh database, a store configured for another database); or
- no scanned database is bound, and none of the listed blobs is referenced;
- the store records an owning database (two-way binding, MVP WP3) and that database is not
  among the scanned ones;
- the store holds unreferenced blobs newer than the newest run activity of the scanned
  databases (``STALE_COPY_SLACK_SECONDS`` of slack): the scanned database looks like a stale
  copy, restore or branch of the one that writes into the store (review M2).

Unreferenced blobs written before the store's owner claimed it are never deleted without
``force``: they cannot be known to belong to the owner (review H1).

With a matching binding the third check is skipped: after run retention has deleted every row
that held a reference, every blob is an orphan and deleting them all is the intended result.

A stale copy or an old backup of the *right* database still passes these checks; never run
retention against one.

Age-based payload retention (``payload_max_age``) deletes run-event and eval-trace payload blobs
older than the limit even while rows still reference them, except the payloads of runs that are
not finished (pending approval, running, waiting for input, interrupted and so on), which the
run retention engine protects as well. Those rows keep their preview, and readers get
``payload_unavailable`` (run events) or skip the trace (eval traces).

The database tables that may hold references are listed in ``REFERENCE_COLUMNS``. A Postgres
port of one of these stores must keep the table and column names and store the codec's text in
a ``TEXT`` column (never ``JSONB``, which reorders keys); the Postgres scan
(``nexura_postgres.referenced_blob_keys``) refuses any other column type.
"""

from __future__ import annotations

import math
import sqlite3
import time
from collections.abc import Callable, Iterable
from contextlib import closing
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from nexura_runtime.blobs.base import BlobStore, key_namespace
from nexura_runtime.blobs.binding import read_sqlite_binding
from nexura_runtime.blobs.payloads import (
    MARKER_PREFIX,
    PAYLOAD_NAMESPACES,
    parse_marker_with_store,
)

#: (table, column) pairs whose values may be blob markers.
REFERENCE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("nexura_studio_run_events", "data_json"),
    ("eval_traces", "trace_json"),
)
REFERENCE_TABLES = frozenset(table for table, _ in REFERENCE_COLUMNS)
#: Runs in these statuses are finished; payloads of every other run are never expired.
FINISHED_RUN_STATUSES = ("completed", "failed", "rejected", "cancelled")
#: Unreferenced blobs this much newer than the newest scanned run activity mean a stale copy.
STALE_COPY_SLACK_SECONDS = 600.0

DEFAULT_GRACE_SECONDS = 24 * 3600.0


class BlobRetentionRefused(ValueError):
    """Retention would be unsafe with these inputs; nothing was deleted."""


@dataclass(frozen=True)
class ReferenceScan:
    """What one or more databases reference.

    ``keys``: referenced blob keys; ``tables``: reference tables present; ``stores``: store ids
    recorded in the references; ``protected``: keys of unfinished runs (never expired);
    ``bindings``: store ids the databases are bound to.
    """

    keys: frozenset[str]
    tables: frozenset[str]
    stores: frozenset[str] = frozenset()
    protected: frozenset[str] = frozenset()
    #: Store ids the scanned databases are bound to (``nexura_blob_store_binding``).
    bindings: frozenset[str] = frozenset()
    #: Ids of the scanned databases that have one (``nexura_database_identity``, Postgres).
    database_ids: frozenset[str] = frozenset()
    #: The newest run row ``updated_at`` or event ``timestamp`` seen (``None``: no runs).
    newest_activity: float | None = None


def scan_values(values: Iterable[Any]) -> tuple[set[str], set[str]]:
    """``(keys, store ids)`` of the markers among ``values``."""
    keys: set[str] = set()
    stores: set[str] = set()
    for value in values:
        parsed = parse_marker_with_store(value)
        if parsed is not None:
            keys.add(parsed[0].key)
            if parsed[2] is not None:
                stores.add(parsed[2])
    return keys, stores


def keys_from_values(values: Iterable[Any]) -> set[str]:
    return scan_values(values)[0]


def sqlite_referenced_keys(path: str | Path) -> ReferenceScan:
    """Scan a service SQLite file (read-only; it must exist)."""
    db = Path(path)
    if not db.is_file():
        raise FileNotFoundError(f"SQLite database {db} does not exist")
    keys: set[str] = set()
    stores: set[str] = set()
    protected: set[str] = set()
    tables: set[str] = set()
    marker = (len(MARKER_PREFIX), MARKER_PREFIX)
    with closing(sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)) as conn:
        conn.execute("BEGIN")  # one read snapshot for every table
        present = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        bound = read_sqlite_binding(conn)
        for table, column in REFERENCE_COLUMNS:
            if table not in present:
                continue
            tables.add(table)
            rows = conn.execute(
                f'SELECT "{column}" FROM "{table}" WHERE substr("{column}", 1, ?) = ?', marker
            )
            found_keys, found_stores = scan_values(row[0] for row in rows)
            keys |= found_keys
            stores |= found_stores
        newest: float | None = None
        for table, column in (
            ("nexura_studio_runs", "updated_at"),
            ("nexura_studio_run_events", "timestamp"),
        ):
            if table not in present or column not in {
                row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')
            }:
                continue
            value = conn.execute(f'SELECT MAX("{column}") FROM "{table}"').fetchone()[0]
            if value is not None and (newest is None or float(value) > newest):
                newest = float(value)
        if {"nexura_studio_run_events", "nexura_studio_runs"} <= present:
            placeholders = ",".join("?" for _ in FINISHED_RUN_STATUSES)
            rows = conn.execute(
                "SELECT e.data_json FROM nexura_studio_run_events e "
                "JOIN nexura_studio_runs r ON r.run_id = e.run_id "
                f"WHERE r.status NOT IN ({placeholders}) AND substr(e.data_json, 1, ?) = ?",
                (*FINISHED_RUN_STATUSES, *marker),
            )
            protected |= scan_values(row[0] for row in rows)[0]
        conn.rollback()
    return ReferenceScan(
        frozenset(keys),
        frozenset(tables),
        frozenset(stores),
        frozenset(protected),
        frozenset({bound}) if bound else frozenset(),
        frozenset(),
        float(newest) if newest is not None else None,
    )


def merge_reference_scans(scans: Iterable[ReferenceScan]) -> ReferenceScan:
    """The union of several scans; refuses when a reference table is in no source.

    A missing table means the scan cannot see some references (a wrong path, or a store that
    lives elsewhere), and every blob it holds would look unreferenced.
    """
    scans = list(scans)
    if not scans:
        raise BlobRetentionRefused(
            "Blob retention needs at least one database to scan for references"
        )
    found = frozenset().union(*(scan.tables for scan in scans))
    missing = sorted(REFERENCE_TABLES - found)
    if missing:
        raise BlobRetentionRefused(
            "Refusing blob retention: reference table(s) "
            + ", ".join(missing)
            + " were not found in any scanned database, so their references cannot be seen"
        )
    return ReferenceScan(
        frozenset().union(*(s.keys for s in scans)),
        found,
        frozenset().union(*(s.stores for s in scans)),
        frozenset().union(*(s.protected for s in scans)),
        frozenset().union(*(s.bindings for s in scans)),
        frozenset().union(*(s.database_ids for s in scans)),
        max(
            (s.newest_activity for s in scans if s.newest_activity is not None),
            default=None,
        ),
    )


def require_all_reference_tables(scans: Iterable[ReferenceScan]) -> frozenset[str]:
    """The referenced keys of ``merge_reference_scans`` (kept for callers that need only keys)."""
    return merge_reference_scans(scans).keys


@dataclass
class BlobRetentionReport:
    started_at: float
    grace_seconds: float
    payload_max_age_seconds: float | None
    dry_run: bool
    store_id: str | None = None
    bound: bool = False
    #: The database that owns the store (two-way binding), and whether it was scanned.
    owner: str | None = None
    owner_scanned: bool = False
    #: When the owner claimed the store; unreferenced blobs before it are kept (review H1).
    owner_claimed_at: float | None = None
    kept_before_claim: int = 0
    listed: int = 0
    referenced: int = 0
    deleted_unreferenced: int = 0
    deleted_expired: int = 0
    kept_unfinished_runs: int = 0
    already_gone: int = 0
    bytes_deleted: int = 0
    kept_recent_unreferenced: int = 0
    deleted_keys: list[str] = field(default_factory=list)

    def to_dict(self, *, include_keys: bool = False) -> dict[str, Any]:
        out = asdict(self)
        if not include_keys:
            out.pop("deleted_keys")
        return out


def _check_age(name: str, value: float | None) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be a finite, non-negative number of seconds")


def collect_blob_garbage(
    store: BlobStore,
    referenced_keys: Callable[[], ReferenceScan | Iterable[str]],
    *,
    grace_seconds: float = DEFAULT_GRACE_SECONDS,
    payload_max_age_seconds: float | None = None,
    dry_run: bool = False,
    force: bool = False,
    clock: Callable[[], float] = time.time,
) -> BlobRetentionReport:
    """Delete unreferenced blobs older than the grace period, and expired payload blobs.

    ``referenced_keys`` is called once, *after* the start time is taken (see the module doc). It
    returns a ``ReferenceScan`` (database scans: store binding and unfinished runs are checked)
    or plain keys. ``force`` skips the wrong-database checks; it never shortens the grace.
    """
    _check_age("grace_seconds", grace_seconds)
    _check_age("payload_max_age_seconds", payload_max_age_seconds)
    started = clock()
    report = BlobRetentionReport(
        started_at=started,
        grace_seconds=grace_seconds,
        payload_max_age_seconds=payload_max_age_seconds,
        dry_run=dry_run,
        store_id=store.identity(create=False),
    )
    result = referenced_keys()
    scan = (
        result
        if isinstance(result, ReferenceScan)
        else ReferenceScan(frozenset(result), frozenset())
    )
    listed = list(store.list(""))
    report.listed = len(listed)
    in_store = sum(1 for info in listed if info.key in scan.keys)
    report.referenced = in_store
    bound = bool(scan.bindings) and scan.bindings == {report.store_id}
    report.bound = bound
    read_record = getattr(store, "owner_record", None)
    record = read_record() if callable(read_record) else None
    if record is not None:
        report.owner, report.owner_claimed_at = record
    report.owner_scanned = report.owner is not None and report.owner in scan.database_ids
    if listed and not force:
        if report.owner is not None and not report.owner_scanned:
            raise BlobRetentionRefused(
                f"Refusing blob retention: blob store {store.describe()} belongs to database "
                f"{report.owner}, which is not among the scanned database(s) (ids: "
                f"{sorted(scan.database_ids) or 'none'}). Scan the database that owns the store; "
                "pass --force only if you are sure"
            )
        if scan.bindings and not bound:
            raise BlobRetentionRefused(
                f"Refusing blob retention: the scanned database(s) are bound to blob store "
                f"{sorted(scan.bindings)}, not to {store.describe()} (id "
                f"{report.store_id or 'unknown'}). This is a different database than the one "
                "that store belongs to; pass --force only if you are sure"
            )
        if (
            isinstance(result, ReferenceScan)
            and not bound
            and (report.store_id is None or report.store_id not in scan.stores)
        ):
            raise BlobRetentionRefused(
                f"Refusing blob retention: the scanned database(s) are not bound to blob store "
                f"{store.describe()} and hold no reference to it (id "
                f"{report.store_id or 'unknown'}; the references name "
                f"{sorted(scan.stores) or 'no store'}). This is a different database than the one "
                "that store belongs to; pass --force only if you are sure"
            )
        # With a matching binding, deleting every orphan is legitimate (after run retention).
        if not bound and in_store == 0:
            raise BlobRetentionRefused(
                f"Refusing blob retention: none of the {len(listed)} blobs in "
                f"{store.describe()} is referenced by the scanned database(s); pass --force only "
                "if that is expected"
            )
    unreferenced_cutoff = started - grace_seconds
    if listed and not force and scan.newest_activity is not None:
        # Review M2: a copy, restore or branch of the writing database sees none of the newer
        # blobs the real one keeps writing; deleting them would lose that deployment's data.
        newer = [
            info
            for info in listed
            if info.key not in scan.keys
            and info.updated_at < unreferenced_cutoff
            and info.updated_at > scan.newest_activity + STALE_COPY_SLACK_SECONDS
        ]
        if newer:
            raise BlobRetentionRefused(
                f"Refusing blob retention: {len(newer)} unreferenced blob(s) in "
                f"{store.describe()} are newer than the newest run activity of the scanned "
                "database(s), so they look like a stale copy, restore or branch of the database "
                "that writes into this store. Scan the live database; a copy used as a separate "
                "deployment needs `nexura-runtime db reset-identity` and its own bucket prefix. "
                "Pass --force only if you are sure"
            )
    expiry_cutoff = (
        started - payload_max_age_seconds if payload_max_age_seconds is not None else None
    )
    for info in listed:
        expired = (
            expiry_cutoff is not None
            and key_namespace(info.key) in PAYLOAD_NAMESPACES
            and info.updated_at < expiry_cutoff
        )
        if (
            not force
            and info.key not in scan.keys
            and report.owner_claimed_at is not None
            and info.updated_at < report.owner_claimed_at
        ):
            # Written before the owner claimed the store: it may be another deployment's.
            report.kept_before_claim += 1
            continue
        if expired and info.key in scan.protected:
            report.kept_unfinished_runs += 1
            expired = False
            if info.key in scan.keys:
                continue
        unreferenced = info.key not in scan.keys
        if not expired and not unreferenced:
            continue
        if not expired and info.updated_at >= unreferenced_cutoff:
            report.kept_recent_unreferenced += 1
            continue
        if not dry_run and not store.delete(info.key):
            report.already_gone += 1
            continue
        if expired:
            report.deleted_expired += 1
        else:
            report.deleted_unreferenced += 1
        report.bytes_deleted += info.size
        report.deleted_keys.append(info.key)
    return report
