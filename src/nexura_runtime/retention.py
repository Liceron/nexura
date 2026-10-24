# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Conservative, opt-in retention of Nexura's historical projections.

The execution journal and checkpoints are protection authorities, never deletion
targets. Every run is re-read under a SQLite write reservation. For 0.4,
filesystem exports are inspected read-only and listed for manual removal only.
"""

from __future__ import annotations

import base64
import ctypes
import fcntl
import hashlib
import json
import math
import os
import select
import signal
import sqlite3
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Collection, Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

from nexura_agents.repro import ReproBundle

TERMINAL_STATUSES = ("completed", "failed", "cancelled")
SUPPORTED_KINDS = ("runs", "bundles", "traces")
SECONDS_PER_DAY = 86400.0
MAX_AGE_DAYS = 365250.0  # 1,000 years; reject typos and overflowing arithmetic.
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_SNAPSHOT_BYTES = 256 * 1024 * 1024
ARTIFACT_TABLE = "nexura_retention_artifacts"
LIFECYCLE_TABLE = "nexura_retention_lifecycles"
_REQUIRED_COLUMNS = {
    LIFECYCLE_TABLE: {"run_id", "status", "finished_json", "edges_json", "schema_version"},
    "runs": {"workspace_id", "run_id", "status", "updated_at", "state_json", "metadata_json"},
    "run_events": {"workspace_id", "run_id", "timestamp"},
    "workflow_checkpoints": {
        "workflow_run_id",
        "status",
        "updated_at",
        "pending_approval_json",
    },
    "effect_records": {"workspace_id", "run_id", "status", "updated_at"},
    "nexura_studio_runs": {
        "run_id",
        "status",
        "created_at",
        "updated_at",
        "approval_id",
        "approval_json",
        "pending_effect_json",
        "pending_input_json",
        "journal_workspace",
        "forked_from_json",
    },
    "nexura_studio_run_events": {"id", "run_id", "event_type", "data_json", "timestamp"},
    "nexura_service_repro_bundles": {"run_id", "workspace_id", "created_at", "bundle_json"},
    "observability_events": {
        "event_id",
        "run_id",
        "parent_run_id",
        "timestamp",
        "payload_json",
        "metadata_json",
        "usage_json",
        "error_message",
    },
    "approval_requests": {
        "workspace_id",
        "approval_id",
        "run_id",
        "status",
        "claimed_by",
        "claimed_at",
    },
    "nexura_sdk_approvals": {"workspace_id", "approval_id", "payload", "expires_at", "consumed_at"},
    "nexura_exec_runs": {
        "workspace_id",
        "run_id",
        "status",
        "run_json",
        "journal_schema_version",
        "state_version",
        "lease_owner",
        "lease_expires_at",
    },
    "nexura_exec_steps": {"workspace_id", "run_id", "intent_json", "outcome_json"},
    "nexura_exec_effects": {"workspace_id", "run_id", "status", "record_json", "resolution"},
    "nexura_exec_events": {"workspace_id", "run_id", "timestamp"},
    "nexura_workflow_checkpoints": {
        "workflow_run_id",
        "status",
        "updated_at",
        "approval_id",
        "approval_json",
        "effect_json",
        "interrupt_json",
        "meta_json",
    },
    "nexura_workflow_checkpoint_history": {"workflow_run_id", "checkpoint_json", "created_at"},
    "nexura_run_checkpoints": {"workspace_id", "run_id", "data", "updated_at"},
    "nexura_run_claims": {"workspace_id", "run_id", "owner", "expires_at"},
    ARTIFACT_TABLE: {
        "schema_version",
        "data_dir",
        "relative_path",
        "run_id",
        "kind",
        "device",
        "inode",
        "size",
        "mtime_ns",
        "ctime_ns",
        "sha256",
    },
}
_OWNED_KEYS = {
    LIFECYCLE_TABLE: ("run_id",),
    "nexura_studio_runs": ("run_id",),
    "nexura_studio_run_events": ("id",),
    "nexura_service_repro_bundles": ("run_id",),
    "observability_events": ("event_id",),
    ARTIFACT_TABLE: ("data_dir", "relative_path"),
}


def _age(value: float | int | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Retention age must be a number of days")
    try:
        days = float(value)
    except OverflowError as exc:
        raise ValueError("Retention age is too large") from exc
    if not math.isfinite(days) or not 0 <= days <= MAX_AGE_DAYS:
        raise ValueError(f"Retention age must be non-negative, finite and <= {MAX_AGE_DAYS:g} days")
    return days


@dataclass
class RetentionPolicy:
    """Days to retain each kind; None disables it. Validated again before use."""

    runs_days: float | None = None
    bundles_days: float | None = None
    traces_days: float | None = None

    def __init__(
        self,
        older_than_days: float | int | dict[str, float | int | None] | None = None,
        *,
        runs_days: float | int | None = None,
        bundles_days: float | int | None = None,
        traces_days: float | int | None = None,
        kinds: Collection[str] | None = None,
        max_age_days: dict[str, float | int | None] | None = None,
    ) -> None:
        enabled = set(SUPPORTED_KINDS if kinds is None else (k.strip().lower() for k in kinds))
        if not enabled or enabled - set(SUPPORTED_KINDS):
            raise ValueError("Unknown or empty retention kinds")
        if isinstance(older_than_days, dict):
            if max_age_days is not None:
                raise ValueError("Specify only one retention age mapping")
            max_age_days, older_than_days = older_than_days, None
        if max_age_days is not None and set(max_age_days) - set(SUPPORTED_KINDS):
            raise ValueError("Unknown retention kinds in max_age_days")
        default = _age(older_than_days)
        for kind, override in zip(
            SUPPORTED_KINDS, (runs_days, bundles_days, traces_days), strict=True
        ):
            value = max_age_days.get(kind) if max_age_days is not None else default
            value = _age(value)
            if override is not None:
                value = _age(override)
            setattr(self, f"{kind}_days", value if kind in enabled else None)

    def cutoff(self, kind: str, now: float | None = None) -> float | None:
        if kind not in SUPPORTED_KINDS:
            raise ValueError(f"Unknown retention kind: {kind}")
        days = _age(getattr(self, f"{kind}_days"))
        ref = time.time() if now is None else now
        if not math.isfinite(ref):
            raise ValueError("Retention clock must be finite")
        return None if days is None else ref - days * SECONDS_PER_DAY

    def is_active(self, kind: str) -> bool:
        return kind in SUPPORTED_KINDS and getattr(self, f"{kind}_days") is not None

    @property
    def active_kinds(self) -> tuple[str, ...]:
        return tuple(k for k in SUPPORTED_KINDS if self.is_active(k))


@dataclass(frozen=True)
class EligibleFile:
    """Read-only observation for manual removal, never a deletion promise."""

    path: str
    run_id: str
    kind: str
    age_days: float
    size_bytes: int
    reason: str = "older than cutoff; terminal, unprotected run; manual removal only"


@dataclass
class RetentionReport:
    """Committed counts (eligible counts in dry run) and logical UTF-8 bytes.

    bytes_freed and its legacy aliases measure payload, not reclaimed SQLite pages.
    Failures carry this same report, including earlier committed runs.
    """

    runs_deleted: int = 0
    events_deleted: int = 0
    bundles_deleted: int = 0
    traces_deleted: int = 0
    runs_bytes: int = 0
    events_bytes: int = 0
    bundles_bytes: int = 0
    traces_bytes: int = 0
    bytes_freed: int = 0
    journal_rows_skipped: int = 0
    dry_run: bool = True
    files_deleted: int = 0
    files_skipped: int = 0
    runs_skipped: int = 0
    errors: list[str] = field(default_factory=list)
    eligible_files: list[EligibleFile] = field(default_factory=list)
    _now: float = field(default_factory=time.time, repr=False)
    _observed: list[dict[str, Any]] = field(default_factory=list, repr=False)
    candidates: dict[str, int] = field(
        default_factory=lambda: dict.fromkeys((*SUPPORTED_KINDS, "events", "files"), 0)
    )

    @property
    def runs_count(self) -> int:
        return self.runs_deleted

    @property
    def events_count(self) -> int:
        return self.events_deleted

    @property
    def bundles_count(self) -> int:
        return self.bundles_deleted

    @property
    def traces_count(self) -> int:
        return self.traces_deleted

    @property
    def total_bytes(self) -> int:
        return self.bytes_freed

    @property
    def bytes_reclaimed(self) -> int:
        return self.bytes_freed

    @property
    def left_for_journal_compaction(self) -> int:
        return self.journal_rows_skipped

    def summary(self) -> str:
        mode = "DRY RUN" if self.dry_run else "PARTIAL FAILURE" if self.errors else "APPLIED"
        count = self.runs_deleted + self.events_deleted + self.bundles_deleted + self.traces_deleted
        lines = [
            f"Retention pass [{mode}]:",
            f"- Runs: {self.runs_deleted} run records, {self.events_deleted} events "
            f"({self.runs_bytes + self.events_bytes:,} logical bytes)",
            f"- Repro bundles: {self.bundles_deleted} bundles "
            f"({self.bundles_bytes:,} logical bytes)",
            f"- Observability traces: {self.traces_deleted} records "
            f"({self.traces_bytes:,} logical bytes)",
            f"- Files listed, not deleted: {len(self.eligible_files)} eligible exports",
            f"Total: {count} records (including run events), {self.bytes_freed:,} logical bytes",
            f"Skipped: {self.runs_skipped} run identities, {self.files_skipped} files",
        ]
        lines.extend(
            f"  {item.path} | run={item.run_id} | {item.kind} | "
            f"age={item.age_days:.6f} days | size={item.size_bytes} bytes | {item.reason}"
            for item in self.eligible_files
        )
        if self.journal_rows_skipped:
            lines.append(f"Left for journal compaction: {self.journal_rows_skipped} rows")
        lines.extend(f"Error: {error}" for error in self.errors)
        if self.dry_run:
            lines.append("(Dry run: no data was deleted. Use --apply to delete.)")
            lines.append("--apply deletes database rows only; files listed, not deleted.")
        return "\n".join(lines)


class RetentionError(sqlite3.DatabaseError):
    """A failed pass, with committed partial results attached in report."""

    def __init__(self, message: str, report: RetentionReport) -> None:
        super().__init__(message)
        self.report = report


def _timestamp(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return None
            result = parsed.timestamp()
        except (TypeError, ValueError, AttributeError, OverflowError):
            return None
    return result if math.isfinite(result) and result >= 0 else None


def _old(value: Any, cutoff: float) -> bool:
    stamp = _timestamp(value)
    return stamp is not None and stamp < cutoff


def _object(raw: Any) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        parsed: dict[str, Any] = {}
        for key, item in pairs:
            if key in parsed:
                raise ValueError(f"Ambiguous duplicate JSON field: {key}")
            parsed[key] = item
        return parsed

    value = json.loads(raw, object_pairs_hook=unique)
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    return value


def _rows(conn: sqlite3.Connection, table: str) -> list[sqlite3.Row]:
    # Callers supply only module constants, never artifact or operator identifiers.
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone():
        return []
    return conn.execute(f'SELECT * FROM "{table}"').fetchall()


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        is not None
    )


def _validate_schemas(conn: sqlite3.Connection) -> None:
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table, required in _REQUIRED_COLUMNS.items():
        if table not in tables:
            continue
        info = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        if not required <= {r[1] for r in info}:
            raise ValueError(f"Unknown retention authority schema: {table}")
        if table in _OWNED_KEYS:
            key = tuple(r[1] for r in sorted(info, key=lambda r: r[5]) if r[5])
            if key != _OWNED_KEYS[table]:
                raise ValueError(f"Unknown ownership key: {table}")
            if conn.execute(f'PRAGMA foreign_key_list("{table}")').fetchone():
                raise ValueError(f"Unknown foreign-key behavior on owned data: {table}")
    for row in conn.execute("SELECT name, tbl_name FROM sqlite_master WHERE type='trigger'"):
        if row[1] in _OWNED_KEYS:
            raise ValueError(f"Unknown trigger on retention-owned data: {row[0]}")


def _payload_bytes(row: sqlite3.Row, columns: Sequence[str]) -> int:
    present = set(row.keys())
    return sum(
        len(row[column].encode("utf-8"))
        if isinstance(row[column], str)
        else len(row[column])
        if isinstance(row[column], bytes)
        else 0
        for column in columns
        if column in present
    )


@dataclass
class _Protection:
    known: set[str] = field(default_factory=set)
    blocked: set[str] = field(default_factory=set)
    finished: dict[str, list[Any]] = field(default_factory=dict)
    edges: set[tuple[str, str]] = field(default_factory=set)
    statuses: dict[str, set[str]] = field(default_factory=dict)

    def terminal(self, run_id: str, status: Any, timestamp: Any) -> None:
        self.known.add(run_id)
        self.statuses.setdefault(run_id, set()).add(status)
        if len(self.statuses[run_id]) > 1:
            self.blocked.add(run_id)
        if status not in TERMINAL_STATUSES or _timestamp(timestamp) is None:
            self.blocked.add(run_id)
        else:
            self.finished.setdefault(run_id, []).append(timestamp)

    def eligible(self, run_id: str, cutoff: float) -> bool:
        return (
            run_id in self.known
            and run_id not in self.blocked
            and bool(self.statuses.get(run_id))
            and bool(self.finished.get(run_id))
            and all(_old(stamp, cutoff) for stamp in self.finished.get(run_id, []))
        )


def _get_unresolved_approval_run_ids(conn: sqlite3.Connection) -> set[str]:
    """Read shipped approval schemas. Unidentifiable envelopes veto the pass."""
    blocked: set[str] = set()
    for row in _rows(conn, "approval_requests"):
        if (
            row["status"] not in ("approved", "rejected", "expired")
            or row["claimed_by"] is not None
            or row["claimed_at"] is not None
            or (
                row["status"] == "approved"
                and "identity_digest" in set(row.keys())
                and row["identity_digest"]
            )
        ):
            if not row["run_id"]:
                raise ValueError("Unresolved approval has no run identity")
            blocked.add(row["run_id"])
    for row in _rows(conn, "nexura_sdk_approvals"):
        # Expiry alone does not establish that recovery/effects have settled.
        envelope = _object(row["payload"])
        run_id = envelope.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("SDK approval has no validated run identity")
        consumed = _timestamp(row["consumed_at"])
        if consumed is None or consumed > time.time():
            blocked.add(run_id)
    return blocked


def _protection(
    conn: sqlite3.Connection, file_edges: Collection[tuple[str, str]] = ()
) -> _Protection:
    """One transaction's authoritative protection and undirected dependency closure.

    Studio IDs are globally keyed. A veto in any workspace protects that ID in
    every workspace, avoiding unsafe assumptions for legacy unscoped projections.
    """
    _validate_schemas(conn)
    result = _Protection(blocked=_get_unresolved_approval_run_ids(conn))
    for row in _rows(conn, "runs"):
        rid = row["run_id"]
        result.terminal(rid, row["status"], row["updated_at"])
        for column in ("state_json", "metadata_json"):
            data = _object(row[column])
            if any(
                data.get(k) is not None for k in ("pending_effect", "pending_approval", "interrupt")
            ):
                result.blocked.add(rid)
        parent = _object(row["metadata_json"]).get("parent_run_id")
        if parent is not None:
            if not isinstance(parent, str) or not parent:
                result.blocked.add(rid)
            else:
                result.edges.add((rid, parent))
    for row in _rows(conn, "workflow_checkpoints"):
        rid = row["workflow_run_id"]
        result.terminal(rid, row["status"], row["updated_at"])
        if row["pending_approval_json"] is not None:
            result.blocked.add(rid)
    for row in _rows(conn, "effect_records"):
        rid = row["run_id"]
        if row["status"] not in ("confirmed", "reconciled"):
            result.blocked.add(rid)
        if _timestamp(row["updated_at"]) is None:
            result.blocked.add(rid)
        else:
            result.finished.setdefault(rid, []).append(row["updated_at"])
    for row in _rows(conn, "nexura_studio_runs"):
        rid = row["run_id"]
        result.terminal(rid, row["status"], row["updated_at"])
        if _timestamp(row["created_at"]) is None:
            result.blocked.add(rid)
        else:
            result.finished.setdefault(rid, []).append(row["created_at"])
        for column in ("pending_effect_json", "pending_input_json", "approval_json"):
            if row[column] is not None:
                result.blocked.add(rid)
        if row["forked_from_json"] is not None:
            origin = _object(row["forked_from_json"])
            parent = origin.get("workflow_run_id", origin.get("run_id"))
            if not isinstance(parent, str) or not parent:
                result.blocked.add(rid)
            else:
                result.edges.add((rid, parent))
        if row["approval_id"]:
            matches = [
                r
                for r in _rows(conn, "approval_requests")
                if r["approval_id"] == row["approval_id"] and r["run_id"] == rid
            ]
            if not matches:
                result.blocked.add(rid)
        if row["journal_workspace"]:
            matches = [
                r
                for r in _rows(conn, "nexura_exec_runs")
                if r["run_id"] == rid and r["workspace_id"] == row["journal_workspace"]
            ]
            if not matches:
                result.blocked.add(rid)

    for row in _rows(conn, "nexura_exec_runs"):
        rid = row["run_id"]
        result.terminal(rid, row["status"], _object(row["run_json"]).get("updated_at"))
        data = _object(row["run_json"])
        if (
            data.get("status") != row["status"]
            or data.get("execution_id") != {"workspace_id": row["workspace_id"], "run_id": rid}
            or row["journal_schema_version"] != 1
            or row["state_version"] != 2
            or row["lease_owner"] is not None
            or row["lease_expires_at"] is not None
            or data.get("journal_schema_version") != 1
            or data.get("state_version") != 2
        ):
            result.blocked.add(rid)
        state = data.get("state")
        if not isinstance(state, dict) or any(
            state.get(k) is not None
            for k in ("pending_effect", "pending_approval", "interrupt", "pause_reason")
        ):
            result.blocked.add(rid)
        parent = data.get("parent_run_id")
        if parent is not None:
            if not isinstance(parent, str) or not parent:
                result.blocked.add(rid)
            else:
                result.edges.add((rid, parent))
        execution_origin = data.get("origin")
        if not isinstance(execution_origin, dict):
            result.blocked.add(rid)
        elif execution_origin.get("legacy") is not None:
            legacy = execution_origin["legacy"]
            parent = legacy.get("run_id") if isinstance(legacy, dict) else None
            if not isinstance(parent, str) or not parent:
                result.blocked.add(rid)
            else:
                # The shipped effect reconciler journals an alias execution;
                # its origin, rather than its name, identifies the owning run.
                result.edges.add((rid, parent))

    for row in _rows(conn, "nexura_exec_effects"):
        record = _object(row["record_json"])
        if (
            row["status"] not in ("confirmed", "resolved")
            or record.get("status") != row["status"]
            or (
                row["status"] == "resolved"
                and row["resolution"] not in ("confirmed", "not_applied", "failed")
            )
        ):
            result.blocked.add(row["run_id"])
    for row in _rows(conn, "nexura_exec_steps"):
        if row["intent_json"] is not None and row["outcome_json"] is None:
            result.blocked.add(row["run_id"])

    for row in _rows(conn, "nexura_workflow_checkpoints"):
        rid = row["workflow_run_id"]
        result.terminal(rid, row["status"], row["updated_at"])
        if any(
            row[c] is not None
            for c in ("approval_id", "approval_json", "effect_json", "interrupt_json")
        ):
            result.blocked.add(rid)
        if row["meta_json"] is not None:
            origin = _object(row["meta_json"])
            parent = origin.get("workflow_run_id")
            if not isinstance(parent, str) or not parent:
                result.blocked.add(rid)
            else:
                result.edges.add((rid, parent))
    for row in _rows(conn, "nexura_run_checkpoints"):
        # An SDK checkpoint represents resumable execution, including completed
        # checkpoints retained for recovery. Part A does not compact these.
        result.known.add(row["run_id"])
        result.blocked.add(row["run_id"])
    for row in _rows(conn, "nexura_run_claims"):
        result.blocked.add(row["run_id"])

    checkpoint_ids = {r["workflow_run_id"] for r in _rows(conn, "nexura_workflow_checkpoints")}
    for row in _rows(conn, "nexura_workflow_checkpoint_history"):
        if row["workflow_run_id"] not in checkpoint_ids:
            result.blocked.add(row["workflow_run_id"])

    for row in _rows(conn, "nexura_studio_run_events"):
        rid = row["run_id"]
        if _timestamp(row["timestamp"]) is None:
            result.blocked.add(rid)
        else:
            result.finished.setdefault(rid, []).append(row["timestamp"])
    for table in ("nexura_exec_events", "run_events"):
        for row in _rows(conn, table):
            stamp = _timestamp(row["timestamp"])
            if stamp is None:
                result.blocked.add(row["run_id"])
            else:
                result.finished.setdefault(row["run_id"], []).append(stamp)
    for row in _rows(conn, "observability_events"):
        if row["parent_run_id"]:
            result.edges.add((row["run_id"], row["parent_run_id"]))

    for row in _rows(conn, LIFECYCLE_TABLE):
        rid = row["run_id"]
        stamps = json.loads(row["finished_json"])
        edges = json.loads(row["edges_json"])
        if (
            row["schema_version"] != 1
            or row["status"] not in TERMINAL_STATUSES
            or not isinstance(stamps, list)
            or not stamps
            or any(_timestamp(stamp) is None for stamp in stamps)
            or not isinstance(edges, list)
            or any(
                not isinstance(edge, list)
                or len(edge) != 2
                or any(not isinstance(part, str) or not part for part in edge)
                for edge in edges
            )
        ):
            raise ValueError("Invalid retained lifecycle authority")
        # Live authorities always supersede a previous terminal snapshot.
        if rid not in result.statuses:
            for stamp in stamps:
                result.terminal(rid, row["status"], stamp)
        result.edges.update((child, parent) for child, parent in edges)

    for row in _rows(conn, ARTIFACT_TABLE):
        columns = set(row.keys())
        if "parent_run_ids_json" not in columns or row["parent_run_ids_json"] is None:
            # The old schema never recorded dependencies. Do not inspect an
            # untrusted path to reconstruct them during deletion: an unknown
            # parent could otherwise be deleted earlier in this pass.
            raise ValueError("Registered export dependencies unavailable; retain and audit")
        try:
            parents = json.loads(row["parent_run_ids_json"])
            if not isinstance(parents, list) or any(
                not isinstance(parent, str) or not parent for parent in parents
            ):
                raise ValueError("Invalid registered dependency identities")
            result.edges.update((row["run_id"], parent) for parent in parents)
        except (OSError, ValueError, UnicodeError):
            raise ValueError("Invalid registered export dependencies; retain and audit") from None

    for row in _rows(conn, "nexura_service_repro_bundles"):
        # FailedRunCapture owns this schema and keys completed failure captures by
        # run ID, including captures whose Studio projection has already expired.
        result.known.add(row["run_id"])
        data = _object(row["bundle_json"])
        if data.get("run_id", row["run_id"]) != row["run_id"]:
            result.blocked.add(row["run_id"])

    result.edges.update(file_edges)
    identities = result.known | result.blocked
    for rid in identities:
        parts = rid.split("/")
        for i in range(1, len(parts)):
            result.edges.add((rid, "/".join(parts[:i])))
    for child, parent in result.edges:
        if parent not in result.known or child not in result.known:
            result.blocked.update((child, parent))
    changed = True
    while changed:
        before = len(result.blocked)
        for child, parent in result.edges:
            if child in result.blocked or parent in result.blocked:
                result.blocked.update((child, parent))
        changed = len(result.blocked) != before
    return result


def _journal_count(conn: sqlite3.Connection, run_ids: Sequence[str]) -> int:
    wanted = set(run_ids)
    return sum(
        row[key] in wanted
        for table, key in (
            ("nexura_exec_runs", "run_id"),
            ("nexura_exec_steps", "run_id"),
            ("nexura_exec_effects", "run_id"),
            ("nexura_exec_events", "run_id"),
            ("nexura_workflow_checkpoints", "workflow_run_id"),
            ("nexura_workflow_checkpoint_history", "workflow_run_id"),
        )
        for row in _rows(conn, table)
    )


def _count_journal_rows_for_runs(conn: sqlite3.Connection, run_ids: Sequence[str]) -> int:
    """Diagnostic count before reservations; never used to authorize deletion."""
    return _journal_count(conn, run_ids)


def _process_db_bundles_retention(conn: sqlite3.Connection) -> set[str]:
    """Discover identities only. Authorization and mutation occur in the transaction."""
    return {row["run_id"] for row in _rows(conn, "nexura_service_repro_bundles")}


class _RetentionConnection(sqlite3.Connection):
    verify_identity: Callable[[], None]
    prepare_write: Callable[[], None]
    finish_write: Callable[[], None]
    journal_written = False


class _DatabaseIdentityError(ValueError):
    """The worker must terminate without further SQLite I/O."""


def _abandon_database_worker(exc: BaseException) -> None:
    print(json.dumps({"error": str(exc), "type": "ValueError", "terminal": True}), flush=True)
    os._exit(74)


@contextmanager
def _transaction(conn: sqlite3.Connection, *, dry_run: bool) -> Iterator[None]:
    if isinstance(conn, _RetentionConnection):
        conn.verify_identity()
    conn.execute("BEGIN" if dry_run else "BEGIN IMMEDIATE")
    try:
        if isinstance(conn, _RetentionConnection):
            conn.verify_identity()
        yield
        if isinstance(conn, _RetentionConnection):
            conn.verify_identity()
        conn.rollback() if dry_run else conn.commit()
    except BaseException:
        conn.rollback()
        raise


def _file_descriptors(*, read_only: bool = False) -> dict[int, tuple[int, int]]:
    """Inspect Linux descriptors without opening/closing any database inode.

    Closing an extra ordinary database descriptor releases *other* connections'
    POSIX locks. fstat and proc fdinfo reads do not have that side effect. Refuse
    platforms without this inspection capability rather than guess an identity.
    """
    result = {}
    try:
        names = os.listdir("/proc/self/fd")
    except OSError as exc:
        raise _DatabaseIdentityError(
            "SQLite/WAL descriptor identity requires Linux /proc/self/fd and fdinfo; "
            "retention refused (including WAL-mode databases)"
        ) from exc
    for name in names:
        fd = int(name)
        try:
            info = os.fstat(fd)
        except OSError:
            continue  # The descriptor used by listdir has already closed.
        if not stat.S_ISREG(info.st_mode) or not info.st_nlink:
            continue
        try:
            fields = dict(
                line.split(":", 1)
                for line in Path(f"/proc/self/fdinfo/{fd}").read_text().splitlines()
            )
        except OSError as exc:
            raise _DatabaseIdentityError(
                "Cannot prove SQLite/WAL descriptor identity using Linux /proc/self/fdinfo"
            ) from exc
        access = int(fields["flags"].strip(), 8) & os.O_ACCMODE
        if access == (os.O_RDONLY if read_only else os.O_RDWR):
            result[fd] = (info.st_dev, info.st_ino)
    return result


def _writable_file_descriptors() -> dict[int, tuple[int, int]]:
    return _file_descriptors()


def _deny_filesystem_writes() -> None:
    """Worker-only Landlock policy; SQLite's C I/O cannot bypass it.

    No write-capable regular descriptor may predate this policy. ABI 3 is
    required to deny truncate as well as write/create/remove/rename operations.
    No permissions are granted for the handled actions, anywhere on disk.
    """
    if sys.platform != "linux" or os.uname().machine not in ("x86_64", "aarch64"):
        raise ValueError("Filesystem-neutral dry run requires Linux Landlock ABI >= 3")
    libc = ctypes.CDLL(None, use_errno=True)
    abi = libc.syscall(444, None, 0, 1)  # landlock_create_ruleset VERSION
    if abi < 3:
        raise ValueError("Filesystem-neutral dry run requires Linux Landlock ABI >= 3")
    # WRITE_FILE, REMOVE_*, MAKE_*, REFER and TRUNCATE (Linux UAPI bits 1, 4..14).
    access = ctypes.c_uint64((1 << 1) | sum(1 << bit for bit in range(4, 15)))
    fd = libc.syscall(444, ctypes.byref(access), ctypes.sizeof(access), 0)
    if fd < 0:
        raise ValueError("Cannot establish filesystem-neutral dry-run Landlock policy")
    try:
        if libc.prctl(38, 1, 0, 0, 0) or libc.syscall(446, fd, 0):
            raise ValueError("Cannot enforce filesystem-neutral dry-run Landlock policy")
    finally:
        os.close(fd)


def _wire_value(value: Any) -> Any:
    return {"bytes": base64.b64encode(value).decode("ascii")} if isinstance(value, bytes) else value


def _unwire_value(value: Any) -> Any:
    return base64.b64decode(value["bytes"], validate=True) if isinstance(value, dict) else value


class _RemoteRow:
    def __init__(self, columns: list[str], values: list[Any]) -> None:
        self.columns, self.values = columns, [_unwire_value(value) for value in values]

    def keys(self) -> list[str]:
        return self.columns

    def __getitem__(self, key: str | int) -> Any:
        return self.values[self.columns.index(key) if isinstance(key, str) else key]


class _RemoteCursor:
    def __init__(self, response: dict[str, Any]) -> None:
        self.rows = [_RemoteRow(response["columns"], row) for row in response["rows"]]
        self.rowcount = response["rowcount"]

    def fetchone(self) -> _RemoteRow | None:
        return self.rows.pop(0) if self.rows else None

    def fetchall(self) -> list[_RemoteRow]:
        rows, self.rows = self.rows, []
        return rows

    def __iter__(self) -> Iterator[_RemoteRow]:
        return iter(self.fetchall())


class _RemoteConnection:
    """One private pipe to one real SQLite connection, never a snapshot copy."""

    def __init__(self, process: subprocess.Popen[str], request: dict[str, Any]) -> None:
        self.process = process
        self.failed = False
        assert process.stdin is not None
        process.stdin.write(json.dumps(request) + "\n")
        process.stdin.flush()
        self._receive()

    def _receive(self) -> dict[str, Any]:
        assert self.process.stdout is not None
        readable = select.poll()
        readable.register(self.process.stdout, select.POLLIN | select.POLLHUP | select.POLLERR)
        if not readable.poll(35000):
            raise sqlite3.OperationalError("Isolated SQLite worker timed out; outcome unknown")
        line = self.process.stdout.readline()
        if not line:
            raise sqlite3.OperationalError("Isolated SQLite worker closed; outcome unknown")
        result = json.loads(line)
        if "error" in result:
            self.failed = bool(result.get("terminal"))
            exception = {
                "IntegrityError": sqlite3.IntegrityError,
                "OperationalError": sqlite3.OperationalError,
                "DatabaseError": sqlite3.DatabaseError,
                "ValueError": ValueError,
            }.get(result["type"], sqlite3.DatabaseError)
            raise exception(result["error"])
        return cast(dict[str, Any], result)

    def _call(self, operation: str, **arguments: Any) -> dict[str, Any]:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps({"operation": operation, **arguments}) + "\n")
        self.process.stdin.flush()
        return self._receive()

    def execute(self, statement: str, parameters: Sequence[Any] = ()) -> _RemoteCursor:
        return _RemoteCursor(
            self._call(
                "execute",
                statement=statement,
                parameters=[_wire_value(value) for value in parameters],
            )
        )

    def commit(self) -> None:
        self._call("commit")

    def rollback(self) -> None:
        if self.failed:
            # A terminal identity failure abandons the isolated connection.
            # Do not ask SQLite to touch an unverified sidecar during rollback.
            return
        self._call("rollback")

    def set_authorizer(self, callback: Callable[..., int]) -> None:
        if callback is not _retention_authorizer:
            raise ValueError("Unknown retention authorizer")
        self._call("authorize")


@contextmanager
def _isolated_database(
    path: Path, authorised: tuple[int, int, int, int], *, read_only: bool = False
) -> Iterator[sqlite3.Connection]:
    """Use exec, not forked SQLite state or a shared service connection.

    Only anonymous inherited pipes are used. The child must match the caller's
    original database and parent identities, with recursion disabled. SQL and
    results are JSON, never pickle. A pathname cannot grant fresh authorization.
    Existing planning hooks stay in the caller; all actual SQL uses this child.
    """
    process = subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            "from nexura_runtime.retention import _serve_database; _serve_database()",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        close_fds=True,
        cwd="/",
        env={},
    )
    try:
        yield cast(
            sqlite3.Connection,
            _RemoteConnection(
                process, {"path": str(path), "authorised": authorised, "read_only": read_only}
            ),
        )
    finally:
        if process.stdin is not None:
            with suppress(BrokenPipeError):
                process.stdin.close()
        try:
            process.wait(timeout=35)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        if process.stdout is not None:
            process.stdout.close()


def _serve_database() -> None:
    def reply(result: dict[str, Any]) -> None:
        print(json.dumps(result), flush=True)

    try:
        request = json.loads(sys.stdin.readline())
        authorised = request["authorised"]
        if (
            not isinstance(authorised, list)
            or len(authorised) != 4
            or any(type(part) is not int or part < 0 for part in authorised)
        ):
            raise ValueError("Missing or invalid caller-authorised database identity")
        with _database(
            Path(request["path"]),
            isolated=True,
            authorised=cast(tuple[int, int, int, int], tuple(authorised)),
            read_only=request["read_only"],
        ) as conn:
            assert isinstance(conn, _RetentionConnection)
            reply({"ready": True})
            for line in sys.stdin:
                try:
                    request = json.loads(line)
                    operation = request["operation"]
                    if isinstance(conn, _RetentionConnection):
                        conn.verify_identity()
                    if operation == "execute":
                        statement = request["statement"].lstrip().upper()
                        writes = statement.startswith(
                            ("CREATE ", "ALTER ", "INSERT ", "UPDATE ", "DELETE ")
                        )
                        if writes:
                            conn.prepare_write()
                        cursor = conn.execute(
                            request["statement"],
                            [_unwire_value(value) for value in request["parameters"]],
                        )
                        if isinstance(conn, _RetentionConnection):
                            if writes:
                                conn.journal_written = True
                            conn.verify_identity()
                        columns = [column[0] for column in cursor.description or ()]
                        reply(
                            {
                                "columns": columns,
                                "rowcount": cursor.rowcount,
                                "rows": [[_wire_value(value) for value in row] for row in cursor],
                            }
                        )
                    elif operation == "commit":
                        conn.commit()
                        conn.finish_write()
                        reply({})
                    elif operation == "rollback":
                        conn.rollback()
                        conn.finish_write()
                        reply({})
                    elif operation == "authorize":
                        conn.set_authorizer(_retention_authorizer)
                        reply({})
                    else:
                        raise ValueError("Unknown isolated SQLite operation")
                except _DatabaseIdentityError as exc:
                    # Do not unwind _database or invoke SQLite destructors:
                    # rollback/close may write to the wrong WAL-index mapping.
                    _abandon_database_worker(exc)
                except Exception as exc:
                    reply({"error": str(exc), "type": type(exc).__name__})
    except _DatabaseIdentityError as exc:
        _abandon_database_worker(exc)
    except Exception as exc:
        reply({"error": str(exc), "type": type(exc).__name__})


def _validate_sidecar(
    parent: int, name: str, suffix: str, expected: os.stat_result, *, wal_validated: bool
) -> None:
    """Inspect only in a fresh worker before it owns any SQLite locks.

    Opening/closing an arbitrary regular file in the service process could close
    an aliased database descriptor and release that process's SQLite locks.
    """
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        before = os.fstat(fd)
        if _file_identity(before) != _file_identity(expected):
            raise ValueError("Database sidecar changed before inspection")
        header = os.read(fd, 128)
        if suffix == "-wal":
            valid = len(header) >= 32 and header[:4] in (b"7\x7f\x06\x82", b"7\x7f\x06\x83")
            valid = valid and int.from_bytes(header[4:8], "big") == 3007000
            # SQLite creates an empty WAL before its first write. It carries no
            # pages to replay; this is a shipped initialization state.
            valid = valid or before.st_size == 0
        elif suffix == "-shm":
            valid = len(header) >= 96 and int.from_bytes(header[:4], sys.byteorder) == 3007000
            # The WAL index is initially zero-filled and populated on first read.
            valid = valid or (wal_validated and before.st_size >= 32768 and header == bytes(128))
        else:
            valid = len(header) >= 28 and header[:8] == bytes.fromhex("d9d505f920a163d7")
        if not valid:
            raise ValueError("Unknown database sidecar format; retained")
        after = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if _file_identity(os.fstat(fd)) != _file_identity(before) or _file_identity(after) != (
            _file_identity(before)
        ):
            raise ValueError("Database sidecar changed during inspection")
    finally:
        os.close(fd)


def _wal_header(fd: int) -> bool:
    """Borrow a verified main descriptor; never open/close one in the service."""
    return os.pread(fd, 100, 0)[18:20] == b"\x02\x02"


def _worker_wal_mode(parent: int, name: str, expected: tuple[int, int]) -> bool:
    # This is only called before a fresh worker has opened SQLite or owns locks.
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) != expected:
            raise _DatabaseIdentityError("Main database changed before WAL inspection")
        return _wal_header(fd)
    finally:
        os.close(fd)


def _create_transaction_sidecar(parent: int, name: str, size: int) -> os.stat_result:
    """Own new SQLite transaction files before SQLite opens them by name.

    O_EXCL is essential: an appeared file is ambiguity, never fresh ownership.
    Only a fresh worker uses this, before acquiring any SQLite/POSIX locks.
    Export files are never passed here.
    """
    try:
        fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
    except FileExistsError as exc:
        raise _DatabaseIdentityError("SQLite sidecar appeared during preparation; refused") from exc
    try:
        remaining = memoryview(bytes(size))
        while remaining:
            written = os.write(fd, remaining)
            if written <= 0:
                raise OSError("Cannot initialize owned SQLite transaction sidecar")
            remaining = remaining[written:]
        return os.fstat(fd)
    finally:
        os.close(fd)


def _check_sticky_entry_owner(path: Path, parent: os.stat_result, entry: os.stat_result) -> None:
    # A root-owned sticky directory protects entries owned by us/root. Another
    # user's own sidecar could still be replaced by that user in the same directory.
    if parent.st_mode & 0o022 and entry.st_uid not in (0, os.geteuid()):
        raise _DatabaseIdentityError(
            f"Untrusted entry owner in sticky database directory: {str(path)!r}; "
            "database and sidecars there must be owned by the invoking user or root"
        )


def _closed_wal_image(
    path: Path, parent: int, before: os.stat_result, verify_namespace: Callable[[], None]
) -> tuple[bytes, Callable[[], None]]:
    """Worker-only read lease: copy a quiescent, sidecar-free WAL into memory.

    The lease rejects pre-existing writers and blocks new write opens/truncates
    until capture finishes. A break request invalidates capture. Never expose a
    mutable source through SQLite's immutable flag or close its fd in the service.
    """
    guidance = (
        "Cannot prove a private read-only preview of the closed WAL database. "
        "Nothing was changed. Stop writers and retry, or run the preview while "
        "the service is running with a valid existing WAL index."
    )
    expected = _file_identity(before)
    broken = False

    def lease_broken(signum: int, frame: Any) -> None:
        nonlocal broken
        broken = True

    def verify_snapshot_source() -> None:
        verify_namespace()
        current = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        if broken or _file_identity(current) != expected:
            raise _DatabaseIdentityError("Closed WAL snapshot source changed; " + guidance)
        for suffix in ("-wal", "-shm", "-journal"):
            try:
                os.stat(path.name + suffix, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                continue
            raise _DatabaseIdentityError("Closed WAL snapshot sidecar appeared; " + guidance)

    # Single-purpose worker owns this signal handler until exit; it must also
    # notice a queued break notification after releasing the capture lease.
    signal.signal(signal.SIGIO, lease_broken)
    fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        if _file_identity(os.fstat(fd)) != expected:
            raise _DatabaseIdentityError("Database changed before snapshot; " + guidance)
        if before.st_size > MAX_SNAPSHOT_BYTES:
            raise ValueError("Closed WAL snapshot exceeds the 256 MiB memory limit. " + guidance)
        try:
            fcntl.fcntl(fd, fcntl.F_SETOWN, os.getpid())
            fcntl.fcntl(fd, fcntl.F_SETLEASE, fcntl.F_RDLCK)
        except OSError as exc:
            raise ValueError(guidance + f" Read lease unavailable: {exc.strerror}") from exc
        verify_snapshot_source()
        chunks = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(fd, min(remaining, 1024 * 1024))
            if not chunk:
                raise _DatabaseIdentityError("Short database snapshot read; " + guidance)
            chunks.append(chunk)
            remaining -= len(chunk)
            if broken:
                raise _DatabaseIdentityError("Writer requested a lease break; " + guidance)
        verify_snapshot_source()
        if (
            _file_identity(os.fstat(fd)) != expected
            or fcntl.fcntl(fd, fcntl.F_GETLEASE) != fcntl.F_RDLCK
        ):
            raise _DatabaseIdentityError("Database snapshot lease or identity changed; " + guidance)
        raw = b"".join(chunks)
        if raw[:16] != b"SQLite format 3\x00" or raw[18:20] != b"\x02\x02":
            raise ValueError("Unrecognised closed WAL header; " + guidance)
        # sqlite3_deserialize cannot read WAL-mode images. SQLite documents this
        # adjustment of the PRIVATE image's format bytes. Source bytes are untouched.
        return raw[:18] + b"\x01\x01" + raw[20:], verify_snapshot_source
    finally:
        # Closing releases any lease, even when acquisition or capture failed.
        os.close(fd)


@contextmanager
def _database(
    path: Path,
    *,
    isolated: bool = False,
    authorised: tuple[int, int, int, int] | None = None,
    read_only: bool = False,
) -> Iterator[sqlite3.Connection]:
    """Verify SQLite's actual writable descriptor before its first SQL statement.

    Ambient writable files/sidecars select isolation before opening SQLite. Every
    worker is bound to the caller's original main/parent device and inode. An
    opened connection with ambiguous or mismatched identity is refused, never
    retried or handed off. No transition authorizes a replacement pathname.
    No raw database descriptor is opened or closed in the service process.
    """
    if isolated and authorised is None:
        raise ValueError("SQLite worker requires the caller-authorised database identity")
    with _directory(path.parent, database=True) as parent:
        conn: sqlite3.Connection | None = None
        try:
            # Do not separately open/close the database with os.open: POSIX
            # close() would release SQLite locks held by other local threads.
            before = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise ValueError("Retention database must be a regular, non-hard-linked file")
            pinned = os.fstat(parent)
            _check_sticky_entry_owner(path, pinned, before)
            original = (before.st_dev, before.st_ino, pinned.st_dev, pinned.st_ino)
            if authorised is not None and original != authorised:
                raise ValueError("Database or parent differs from caller-authorised identity")
            authorised = original
            expected = (before.st_dev, before.st_ino)

            def verify_namespace() -> None:
                try:
                    with _directory(path.parent, database=True) as current:
                        named = os.fstat(current)
                        entry = os.stat(path.name, dir_fd=current, follow_symlinks=False)
                        _check_sticky_entry_owner(path, named, entry)
                        if (
                            (entry.st_dev, entry.st_ino, named.st_dev, named.st_ino) != original
                            or not stat.S_ISREG(entry.st_mode)
                            or entry.st_nlink != 1
                        ):
                            raise _DatabaseIdentityError(
                                "Database ancestor or identity changed; retention refused"
                            )
                except OSError as exc:
                    raise _DatabaseIdentityError(
                        "Cannot prove database namespace identity"
                    ) from exc

            allowed = {(before.st_dev, before.st_ino)}
            sidecars = []
            for suffix in ("-wal", "-shm", "-journal"):
                try:
                    info = os.stat(path.name + suffix, dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if suffix == "-journal":
                    raise _DatabaseIdentityError(
                        f"Ambiguous pre-existing rollback journal: {path}{suffix}; "
                        "retention refused"
                    )
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError("Database sidecars must be regular, non-hard-linked files")
                _check_sticky_entry_owner(Path(str(path) + suffix), pinned, info)
                sidecars.append((suffix, info))
            descriptors = _writable_file_descriptors()
            ambient = any(identity not in allowed for identity in descriptors.values())
            wal_mode = False
            if isolated:
                if ambient:
                    raise ValueError("Unexpected writable descriptor in SQLite worker")
                wal_mode = _worker_wal_mode(parent, path.name, expected)
                if wal_mode and not read_only:
                    present = {suffix for suffix, _ in sidecars}
                    for suffix, size in (("-wal", 0), ("-shm", 32768)):
                        if suffix not in present:
                            sidecars.append(
                                (
                                    suffix,
                                    _create_transaction_sidecar(parent, path.name + suffix, size),
                                )
                            )
                sidecars.sort(key=lambda item: ("-wal", "-shm", "-journal").index(item[0]))
                wal_validated = False
                for suffix, info in sidecars:
                    _validate_sidecar(
                        parent, path.name + suffix, suffix, info, wal_validated=wal_validated
                    )
                    if suffix == "-wal":
                        wal_validated = True
            verify_namespace()
            if not isolated and (ambient or sidecars or read_only):
                # Isolation is an opening strategy for the already-authorised
                # inode, never recovery from an unverified SQLite connection.
                worker = (
                    _isolated_database(path, authorised, read_only=True)
                    if read_only
                    else _isolated_database(path, authorised)
                )
                with worker as remote:
                    yield remote
                return
            # Check every validated sidecar again before SQLite can initialize SHM.
            # The post-connect descriptor proof still covers the remaining window.
            for suffix, info in sidecars:
                current = os.stat(path.name + suffix, dir_fd=parent, follow_symlinks=False)
                if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                    raise _DatabaseIdentityError(
                        "Validated SQLite sidecar name changed before open"
                    )
            if read_only:
                _deny_filesystem_writes()
            if isolated and read_only and wal_mode and not sidecars:
                image, verify_source = _closed_wal_image(path, parent, before, verify_namespace)
                verify_source()
                conn = sqlite3.connect(":memory:", factory=_RetentionConnection)
                assert isinstance(conn, _RetentionConnection)
                conn.verify_identity = verify_source
                conn.finish_write = lambda: None

                def refuse_snapshot_write() -> None:
                    raise _DatabaseIdentityError("Private preview snapshot is read-only")

                conn.prepare_write = refuse_snapshot_write
                conn.deserialize(image)
                conn.execute("PRAGMA query_only=ON")
                conn.row_factory = sqlite3.Row
                verify_source()
                yield conn
                verify_source()
                return
            conn = sqlite3.connect(
                path.as_uri() + ("?mode=ro" if read_only else "?mode=rw"),
                uri=True,
                timeout=30,
                factory=_RetentionConnection,
            )
            # Even a read-only failure/normal close must not checkpoint or unlink
            # a sidecar name after another process has replaced that name.
            if not hasattr(conn, "setconfig") or not hasattr(
                sqlite3, "SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE"
            ):
                raise ValueError(
                    "WAL safety requires SQLite SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE support"
                )
            conn.setconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE, True)
            probe: Callable[[], dict[int, tuple[int, int]]] = (
                (lambda: _file_descriptors(read_only=True))
                if read_only
                else _writable_file_descriptors
            )
            opened = probe()
            verify_namespace()
            main_fds = [fd for fd, identity in opened.items() if identity == expected]
            # SQLite may reuse an idle descriptor from its process-wide cache.
            # Attribution requires *every* writable regular inode in the process
            # before and after opening to be the selected main database. Existing
            # sidecar handles also make attribution ambiguous: an aliased foreign
            # database must never be mistaken for an authorized sidecar.
            database_fds = set(main_fds)

            validated = {suffix: (info.st_dev, info.st_ino) for suffix, info in sidecars}
            wal_opened = False
            connection = conn

            def verify() -> None:
                nonlocal wal_opened
                try:
                    verify_namespace()
                    current_fds = probe()
                    if not any(current_fds.get(fd) == expected for fd in database_fds):
                        raise _DatabaseIdentityError("SQLite database descriptor changed")
                    if not wal_mode and not set(current_fds.values()) <= {
                        expected,
                        *validated.values(),
                    }:
                        raise _DatabaseIdentityError(
                            "Unvalidated rollback journal descriptor; refused"
                        )
                    if wal_mode:
                        sidecar_ids = {
                            validated[suffix] for suffix in ("-wal", "-shm") if suffix in validated
                        }
                        actual = set(current_fds.values())
                        if not actual <= {expected, *sidecar_ids}:
                            raise _DatabaseIdentityError(
                                "SQLite WAL/SHM descriptor identity differs from validated sidecars"
                            )
                        wal_opened = wal_opened or bool(actual & sidecar_ids)
                        if connection.in_transaction or wal_opened:
                            if len(sidecar_ids) != 2:
                                raise _DatabaseIdentityError(
                                    "Cannot prove main/WAL/SHM descriptors; "
                                    "read-only WAL requires existing sidecars"
                                )
                            if not {expected, *sidecar_ids} <= actual:
                                raise _DatabaseIdentityError(
                                    "Cannot prove main/WAL/SHM descriptors after transaction start"
                                )
                            for suffix in ("-wal", "-shm"):
                                info = os.stat(
                                    path.name + suffix, dir_fd=parent, follow_symlinks=False
                                )
                                if (info.st_dev, info.st_ino) != validated[suffix]:
                                    raise _DatabaseIdentityError(
                                        "Validated SQLite sidecar name changed"
                                    )
                                if info.st_nlink != 1 or not stat.S_ISREG(info.st_mode):
                                    raise _DatabaseIdentityError(
                                        "SQLite sidecar is no longer a single-link regular file"
                                    )
                                _check_sticky_entry_owner(Path(str(path) + suffix), pinned, info)
                    journal = validated.get("-journal")
                    try:
                        named = os.stat(
                            path.name + "-journal", dir_fd=parent, follow_symlinks=False
                        )
                    except FileNotFoundError:
                        if journal is not None:
                            raise _DatabaseIdentityError(
                                "Reserved rollback journal disappeared"
                            ) from None
                    else:
                        if (
                            journal is None
                            or (named.st_dev, named.st_ino) != journal
                            or named.st_nlink != 1
                        ):
                            raise _DatabaseIdentityError(
                                "Unvalidated rollback journal identity; retention refused"
                            )
                    if (
                        journal is not None
                        and connection.journal_written
                        and journal not in current_fds.values()
                    ):
                        raise _DatabaseIdentityError(
                            "Cannot prove rollback journal descriptor after write"
                        )
                except (OSError, ValueError) as exc:
                    if isinstance(exc, _DatabaseIdentityError):
                        raise
                    raise _DatabaseIdentityError(
                        f"Cannot prove SQLite descriptor identity: {exc}"
                    ) from exc

            opening_ids = allowed | (set(validated.values()) if wal_mode else set())
            if not main_fds or any(identity not in opening_ids for identity in opened.values()):
                if isolated:
                    raise _DatabaseIdentityError(
                        "SQLite opened an unverified database/sidecar descriptor"
                    )
                # Close through SQLite's VFS, preserving other local instances'
                # locks. Never execute SQL on an unattributable connection.
                conn.close()
                conn = None
                raise ValueError(
                    "SQLite database descriptor identity is ambiguous or changed; refused"
                )

            assert isinstance(conn, _RetentionConnection)
            conn.verify_identity = verify

            def prepare_write() -> None:
                verify()
                if not connection.in_transaction:
                    raise _DatabaseIdentityError("Retention write requires an explicit transaction")
                if read_only:
                    raise _DatabaseIdentityError("Read-only dry run refused a write statement")
                if not wal_mode and "-journal" not in validated:
                    info = _create_transaction_sidecar(parent, path.name + "-journal", 0)
                    validated["-journal"] = (info.st_dev, info.st_ino)

            def finish_write() -> None:
                validated.pop("-journal", None)
                connection.journal_written = False

            conn.prepare_write = prepare_write
            conn.finish_write = finish_write
            verify()
            if not isolated:
                # The main-only connection was verified and has executed no SQL.
                # WAL sidecar creation/validation and all WAL SQL need a worker.
                conn.close()
                conn = None
                with _isolated_database(path, authorised) as remote:
                    yield remote
                return
            after = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISREG(after.st_mode) or (after.st_dev, after.st_ino) != (
                before.st_dev,
                before.st_ino,
            ):
                raise ValueError("Database replaced while opening retention connection")
            conn.row_factory = sqlite3.Row
            # Force SQLite to open its sidecars here, not at a later caller RPC.
            # Read-only workers already deny all filesystem writes in the kernel.
            verify()
            try:
                conn.execute("SELECT name FROM sqlite_schema LIMIT 1").fetchall()
            except sqlite3.OperationalError as exc:
                if read_only and wal_mode:
                    raise ValueError(
                        "Read-only preview needs a valid existing WAL index; nothing was changed. "
                        "Run the preview with the service running, or after a clean shutdown "
                        "with no sidecars and no writers for a private snapshot."
                    ) from exc
                raise
            verify()
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            if mode not in ("wal", "delete"):
                raise ValueError(
                    f"Unsupported SQLite journal mode {mode!r}; use WAL or DELETE mode"
                )
            if (mode == "wal") != wal_mode:
                raise _DatabaseIdentityError("SQLite journal mode changed while opening; refused")
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA wal_autocheckpoint=0")
            yield conn
        finally:
            if isolated:
                if sys.exc_info()[1] is not None:
                    _abandon_database_worker(cast(BaseException, sys.exc_info()[1]))
                if isinstance(conn, _RetentionConnection):
                    try:
                        conn.verify_identity()
                    except Exception as exc:
                        _abandon_database_worker(exc)
            if conn is not None:
                conn.close()


def _run_retention(
    conn: sqlite3.Connection,
    rid: str,
    cutoffs: dict[str, float | None],
    *,
    dry_run: bool,
    data_dir: Path | None,
    report: RetentionReport,
) -> RetentionReport:
    delta = RetentionReport(dry_run=dry_run)
    with _transaction(conn, dry_run=dry_run):
        protection = _protection(
            conn,
            [
                (row["run_id"], parent)
                for row in report._observed
                for parent in json.loads(row["parent_run_ids_json"])
            ],
        )
        registered = [row for row in _rows(conn, ARTIFACT_TABLE) if row["run_id"] == rid]
        observed: list[sqlite3.Row | dict[str, Any]] = [*registered, *report._observed]
        artifacts = [
            row
            for row in observed
            if data_dir is not None
            and row["run_id"] == rid
            and row["data_dir"] == str(data_dir)
            and cutoffs[row["kind"]] is not None
        ]
        if rid in protection.blocked:
            delta.runs_skipped = 1
            report.files_skipped += len(artifacts)
            return delta
        for kind, table, id_column, time_column in (
            (
                "bundles",
                "nexura_service_repro_bundles",
                "run_id",
                "created_at",
            ),
            (
                "traces",
                "observability_events",
                "event_id",
                "timestamp",
            ),
        ):
            cutoff = cutoffs[kind]
            if cutoff is None or not protection.eligible(rid, cutoff):
                continue
            for row in _rows(conn, table):
                if row["run_id"] != rid or not _old(row[time_column], cutoff):
                    continue
                if kind == "bundles":
                    data = _object(row["bundle_json"])
                    if "created_at" in data and not _old(data["created_at"], cutoff):
                        continue
                report.candidates[kind] += 1
                if not dry_run:
                    cursor = conn.execute(
                        f'DELETE FROM "{table}" WHERE "{id_column}"=?', (row[id_column],)
                    )
                    if cursor.rowcount != 1:
                        raise sqlite3.IntegrityError("Retention row count changed")
                setattr(delta, f"{kind}_deleted", getattr(delta, f"{kind}_deleted") + 1)
                setattr(
                    delta,
                    f"{kind}_bytes",
                    getattr(delta, f"{kind}_bytes") + _payload_bytes(row, tuple(row.keys())),
                )
        cutoff = cutoffs["runs"]
        if cutoff is not None and protection.eligible(rid, cutoff):
            runs = [r for r in _rows(conn, "nexura_studio_runs") if r["run_id"] == rid]
            events = [r for r in _rows(conn, "nexura_studio_run_events") if r["run_id"] == rid]
            if runs and all(_old(e["timestamp"], cutoff) for e in events):
                report.candidates["runs"] += 1
                report.candidates["events"] += len(events)
                delta.runs_bytes = _payload_bytes(runs[0], tuple(runs[0].keys()))
                delta.events_bytes = sum(_payload_bytes(e, tuple(e.keys())) for e in events)
                delta.journal_rows_skipped = _journal_count(conn, [rid])
                if not dry_run:
                    conn.execute(
                        f"INSERT INTO {LIFECYCLE_TABLE} "
                        "(run_id,status,finished_json,edges_json,schema_version) "
                        "VALUES (?,?,?,?,1) "
                        "ON CONFLICT(run_id) DO UPDATE SET status=excluded.status, "
                        "finished_json=excluded.finished_json,edges_json=excluded.edges_json",
                        (
                            rid,
                            runs[0]["status"],
                            json.dumps(protection.finished[rid]),
                            json.dumps(sorted(edge for edge in protection.edges if rid in edge)),
                        ),
                    )
                    conn.execute("DELETE FROM nexura_studio_run_events WHERE run_id=?", (rid,))
                    cursor = conn.execute("DELETE FROM nexura_studio_runs WHERE run_id=?", (rid,))
                    if cursor.rowcount != 1:
                        raise sqlite3.IntegrityError("Retention run count changed")
                delta.runs_deleted, delta.events_deleted = 1, len(events)
        if data_dir is not None:
            for artifact in artifacts:
                cutoff = cutoffs[artifact["kind"]]
                if cutoff is not None and protection.eligible(rid, cutoff):
                    _report_artifact(artifact, data_dir, cutoff, report, protection)
                else:
                    report.files_skipped += 1
        if not (
            delta.runs_deleted
            or delta.events_deleted
            or delta.bundles_deleted
            or delta.traces_deleted
            or any(item.run_id == rid for item in report.eligible_files)
        ):
            delta.runs_skipped = 1
    return delta


def _merge(report: RetentionReport, delta: RetentionReport) -> None:
    for key in (
        "runs_deleted",
        "events_deleted",
        "bundles_deleted",
        "traces_deleted",
        "runs_bytes",
        "events_bytes",
        "bundles_bytes",
        "traces_bytes",
        "journal_rows_skipped",
        "runs_skipped",
    ):
        setattr(report, key, getattr(report, key) + getattr(delta, key))
    report.bytes_freed = sum(
        (report.runs_bytes, report.events_bytes, report.bundles_bytes, report.traces_bytes)
    )


def _inspect_loose_files(data_dir: Path, report: RetentionReport, owned: set[Path]) -> None:
    """Identify actual shipped schemas read-only; never infer ownership by name."""
    root = data_dir.absolute()
    if not root.exists() and not root.is_symlink():
        return
    try:
        with _directory(root) as fd:
            loose = _inventory(fd, root, root.resolve(strict=True), report, owned)
        if not report.dry_run and loose:
            report.errors.append(
                "Unrecognised or unsafe files retained; no manual removal recommendation"
            )
    except (OSError, ValueError) as exc:
        report.errors.append(f"Cannot inspect data_dir safely: {exc}")


def _check_directory_permissions(path: Path, info: os.stat_result, *, final: bool) -> None:
    sticky_root = info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX)
    if info.st_mode & 0o022 and (final or not sticky_root):
        raise _DatabaseIdentityError(
            f"Unsafe database directory permissions at {str(path)!r}: "
            "group/other write access is forbidden; root-owned sticky exceptions "
            "apply to ancestors only, never the database's own directory; "
            f"fix: chmod go-w {str(path)!r}"
        )
    # An untrusted owner can chmod/rename an ancestor despite mode 0755, so
    # checking only group/other bits would not establish namespace trust.
    if info.st_uid not in (0, os.geteuid()):
        role = "database directory" if final else "database ancestor"
        raise _DatabaseIdentityError(
            f"Unsafe {role} owner at {str(path)!r}: must be owned by "
            f"uid {os.geteuid()} or root; use an owned directory with chmod 700"
        )


@contextmanager
def _directory(path: Path, *, database: bool = False) -> Iterator[int]:
    """Open every ancestor without following symlinks, including the root path."""
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        current = Path(path.anchor)
        if database:
            _check_directory_permissions(current, os.fstat(fd), final=current == path)
        for part in path.parts[1:]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
            current /= part
            if database:
                _check_directory_permissions(current, os.fstat(fd), final=current == path)
        yield fd
    finally:
        os.close(fd)


def _inventory(fd: int, path: Path, root: Path, report: RetentionReport, owned: set[Path]) -> int:
    loose = 0
    for name in os.listdir(fd):
        candidate = path / name
        before = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if stat.S_ISLNK(before.st_mode):
            report.files_skipped += 1
            loose += 1
            continue
        if not candidate.resolve(strict=True).is_relative_to(root):
            raise ValueError("Candidate escapes data_dir")
        if stat.S_ISDIR(before.st_mode):
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            try:
                if os.fstat(child).st_ino != before.st_ino:
                    raise ValueError("Directory changed during inventory")
                loose += _inventory(child, candidate, root, report, owned)
            finally:
                os.close(child)
        elif candidate not in owned:
            try:
                inspected = _inspect_export(root, str(candidate.relative_to(root)), "", "")
                identity = inspected["identity"]
                report._observed.append(
                    {
                        "data_dir": str(root),
                        "relative_path": str(candidate.relative_to(root)),
                        "kind": inspected["kind"],
                        "run_id": inspected["run_id"],
                        **dict(
                            zip(
                                ("device", "inode", "size", "mtime_ns", "ctime_ns"),
                                identity,
                                strict=True,
                            )
                        ),
                        "sha256": inspected["sha256"],
                        "parent_run_ids_json": json.dumps(inspected["parents"]),
                    }
                )
            except (OSError, ValueError, subprocess.SubprocessError):
                report.files_skipped += 1
                loose += 1
    return loose


def apply_retention(
    policy: RetentionPolicy,
    *,
    db_path: str | Path | None = None,
    data_dir: str | Path | None = None,
    dry_run: bool = True,
) -> RetentionReport:
    """Expire owned SQLite projections; report eligible files for manual removal.

    A run and its events are committed atomically with its selected bundle/trace
    rows. Any later failure raises RetentionError with the committed partial
    report. Files are never deleted, renamed, or opened for writing. A file
    listing is a read-only observation, not a promise about its future identity.
    """
    now = time.time()
    cutoffs = {kind: policy.cutoff(kind, now) for kind in SUPPORTED_KINDS}
    report = RetentionReport(dry_run=dry_run)
    report._now = now
    disk = Path(data_dir).absolute() if data_dir is not None else None
    owned: set[Path] = set()
    if db_path is None and disk is not None:
        if disk is not None:
            _inspect_loose_files(disk, report, owned)
        return report
    try:
        if db_path is None:
            raise ValueError("A local SQLite database path is required")
        raw_path = os.fspath(db_path)
        if not raw_path.strip() or raw_path == ":memory:" or urlsplit(raw_path).scheme:
            raise ValueError(
                f"Retention requires a filesystem SQLite path, not a URI: {raw_path!r}"
            )
        path = Path(db_path).absolute()
        with _database(path, read_only=True) if dry_run else _database(path) as conn:
            if all(cutoff is None for cutoff in cutoffs.values()):
                return report
            _validate_schemas(conn)
            if not dry_run:
                with _transaction(conn, dry_run=False):
                    if not _table_exists(conn, LIFECYCLE_TABLE):
                        conn.execute(
                            f"CREATE TABLE {LIFECYCLE_TABLE} ("
                            "run_id TEXT PRIMARY KEY, status TEXT NOT NULL, "
                            "finished_json TEXT NOT NULL, edges_json TEXT NOT NULL, "
                            "schema_version INTEGER NOT NULL CHECK(schema_version=1))"
                        )
                _validate_schemas(conn)
            conn.set_authorizer(_retention_authorizer)
            identities = {r["run_id"] for r in _rows(conn, "nexura_studio_runs")}
            identities.update(_process_db_bundles_retention(conn))
            identities.update(r["run_id"] for r in _rows(conn, "observability_events"))
            artifacts = _rows(conn, ARTIFACT_TABLE)
            identities.update(r["run_id"] for r in artifacts)
            if disk is not None:
                owned = {disk / r["relative_path"] for r in artifacts if r["data_dir"] == str(disk)}
                if any(cutoffs[k] is not None for k in ("bundles", "traces")):
                    _inspect_loose_files(disk, report, owned)
                    identities.update(row["run_id"] for row in report._observed)
            _count_journal_rows_for_runs(conn, sorted(identities))
            for rid in sorted(identities):
                delta = _run_retention(
                    conn, rid, cutoffs, dry_run=dry_run, data_dir=disk, report=report
                )
                _merge(report, delta)
    except Exception as exc:
        report.errors.append(f"Retention stopped: {exc}")
        raise RetentionError(str(exc), report) from exc
    return report


def _retention_authorizer(
    action: int, table: str | None, column: str | None, database: str | None, source: str | None
) -> int:
    """Never permit triggers/cascades to mutate data outside explicit DELETE targets."""
    deletable = {
        "nexura_studio_runs",
        "nexura_studio_run_events",
        "nexura_service_repro_bundles",
        "observability_events",
    }
    if action in (sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_INSERT):
        return (
            sqlite3.SQLITE_OK
            if table == LIFECYCLE_TABLE and source is None
            else sqlite3.SQLITE_DENY
        )
    if action == sqlite3.SQLITE_DELETE and (table not in deletable or source is not None):
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def _file_identity(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _artifact_content(kind: str, run_id: str, content: str) -> tuple[int, list[float]]:
    """Validate the actual shipped formats and every contained record's age."""
    if kind == "bundles":
        data = _object(content)
        ReproBundle(data)
        if data.get("run_id") != run_id:
            raise ValueError("Bundle metadata does not identify the registered run")
        stamp = _timestamp(data.get("created_at"))
        if stamp is None:
            raise ValueError("Bundle has no unambiguous created_at")
        return 1, [stamp]
    if kind != "traces":
        raise ValueError("Only bundles and traces can be registered as artifacts")
    from nexura_runtime.observability.models import EventRecord

    stamps = []
    for line in content.splitlines():
        if not line.strip():
            continue
        data = _object(line)
        if "timestamp" not in data:
            raise ValueError("Trace has no explicit timestamp")
        record = EventRecord.model_validate(data)
        stamp = _timestamp(record.timestamp)
        if record.run_id != run_id or stamp is None:
            raise ValueError("Trace contains an unknown run or timestamp")
        stamps.append(stamp)
    return len(stamps), stamps


def _artifact_parents(kind: str, run_id: str, content: str) -> list[str]:
    _artifact_content(kind, run_id, content)
    if kind != "traces":
        return []
    parents = set()
    for line in content.splitlines():
        if line.strip():
            parent = _object(line).get("parent_run_id")
            if parent is not None:
                if not isinstance(parent, str) or not parent:
                    raise ValueError("Invalid registered trace parent")
                parents.add(parent)
    return sorted(parents)


def write_retention_artifact(
    content: str,
    *,
    db_path: str | Path,
    data_dir: str | Path,
    relative_path: str,
    run_id: str,
    kind: str,
) -> Path:
    """Register an existing export, without creating or writing any file.

    Historical name retained for import compatibility. The exporter must create
    the file first. Registration verifies its content using read-only inspection;
    retention will only report it for manual removal, including with --apply.
    """
    parents = _artifact_parents(kind, run_id, content)
    raw = content.encode("utf-8")
    if len(raw) > MAX_FILE_BYTES:
        raise ValueError("Artifact exceeds retention's inspection limit")
    root = Path(data_dir).absolute()
    relative = Path(relative_path)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise ValueError("Artifact path must be relative and inside data_dir")
    path = root / relative
    with _directory(path.parent):
        if not path.parent.resolve(strict=True).is_relative_to(root.resolve(strict=True)):
            raise ValueError("Artifact escapes data_dir")
        with _database(Path(db_path).absolute()) as conn, _transaction(conn, dry_run=False):
            if run_id not in _protection(conn).known:
                raise ValueError("Artifact requires a known run identity")
            if not _table_exists(conn, ARTIFACT_TABLE):
                conn.execute(
                    f"CREATE TABLE {ARTIFACT_TABLE} ("
                    "data_dir TEXT NOT NULL, relative_path TEXT NOT NULL, "
                    "run_id TEXT NOT NULL, kind TEXT NOT NULL, device INTEGER NOT NULL, "
                    "inode INTEGER NOT NULL, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, "
                    "ctime_ns INTEGER NOT NULL, sha256 TEXT NOT NULL, "
                    "schema_version INTEGER NOT NULL CHECK(schema_version=1), "
                    "parent_run_ids_json TEXT, PRIMARY KEY(data_dir, relative_path))"
                )
            if "parent_run_ids_json" not in {
                column[1] for column in conn.execute(f"PRAGMA table_info({ARTIFACT_TABLE})")
            }:
                conn.execute(f"ALTER TABLE {ARTIFACT_TABLE} ADD COLUMN parent_run_ids_json TEXT")
            inspected = _inspect_export(root, relative.as_posix(), kind, run_id)
            identity = inspected["identity"]
            if inspected["sha256"] != hashlib.sha256(raw).hexdigest():
                raise ValueError("Existing export differs from registration content")
            conn.execute(
                f"INSERT INTO {ARTIFACT_TABLE} "
                "(data_dir,relative_path,run_id,kind,device,inode,size,mtime_ns,ctime_ns,"
                "sha256,schema_version,parent_run_ids_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    str(root),
                    relative.as_posix(),
                    run_id,
                    kind,
                    *identity,
                    hashlib.sha256(raw).hexdigest(),
                    1,
                    json.dumps(parents),
                ),
            )
    return path


def _inspect_export(root: Path, relative: str, kind: str, rid: str) -> dict[str, Any]:
    # A separate process owns no service SQLite locks. Closing an untrusted inode
    # here therefore cannot accidentally release the service's POSIX DB locks.
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            "from nexura_runtime.retention import _inspect_export_worker; _inspect_export_worker()",
        ],
        input=json.dumps([str(root), relative, kind, rid]),
        capture_output=True,
        text=True,
        timeout=35,
        check=False,
        close_fds=True,
        cwd="/",
        env={},
    )
    if result.returncode:
        raise ValueError("Cannot inspect export safely: " + result.stderr.strip())
    return cast(dict[str, Any], json.loads(result.stdout))


def _inspect_export_worker() -> None:
    root_raw, relative_raw, kind, rid = json.load(sys.stdin)
    root, relative = Path(root_raw), Path(relative_raw)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise ValueError("Export path escapes data_dir")
    path = root / relative
    with _directory(root), _directory(path.parent) as parent:
        resolved_root = root.resolve(strict=True)
        if not path.resolve(strict=True).is_relative_to(resolved_root):
            raise ValueError("Export escapes data_dir")
        before = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ValueError("Export is not a single-link regular file")
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(fd, "rb") as source:
            opened = os.fstat(source.fileno())
            if _file_identity(before) != _file_identity(opened):
                raise ValueError("Export changed before inspection")
            raw = source.read(MAX_FILE_BYTES + 1)
            if len(raw) > MAX_FILE_BYTES:
                raise ValueError("Export exceeds inspection limit")
            after = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            if _file_identity(before) != _file_identity(after):
                raise ValueError("Export modified during inspection")
            if _file_identity(before) != _file_identity(path.lstat()):
                raise ValueError("Export directory changed during inspection")
            if not path.resolve(strict=True).is_relative_to(resolved_root):
                raise ValueError("Export directory moved outside data_dir")
        content = raw.decode("utf-8")
        if not kind:
            # Positively identify content, including required run identity. JSONL
            # must be nonempty; only registered empty traces have an identity.
            try:
                data = _object(content)
                ReproBundle(data)
                kind, rid = "bundles", data.get("run_id")
            except (ValueError, TypeError):
                lines = [line for line in content.splitlines() if line.strip()]
                if not lines:
                    raise ValueError("Unregistered empty file has no run identity") from None
                kind, rid = "traces", _object(lines[0]).get("run_id")
            if not isinstance(rid, str) or not rid:
                raise ValueError("File has no explicit run identity")
        count, stamps = _artifact_content(kind, rid, content)
        print(
            json.dumps(
                {
                    "identity": _file_identity(before),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "count": count,
                    "stamps": stamps,
                    "parents": _artifact_parents(kind, rid, content),
                    "kind": kind,
                    "run_id": rid,
                }
            )
        )


def _report_artifact(
    row: sqlite3.Row | dict[str, Any],
    data_dir: Path,
    cutoff: float,
    report: RetentionReport,
    protection: _Protection,
) -> None:
    try:
        inspected = _inspect_export(data_dir, row["relative_path"], row["kind"], row["run_id"])
        expected = [row[key] for key in ("device", "inode", "size", "mtime_ns", "ctime_ns")]
        if inspected["identity"] != expected or inspected["sha256"] != row["sha256"]:
            raise ValueError("Export identity/content changed since registration")
        if inspected["parents"] != sorted(json.loads(row["parent_run_ids_json"])):
            raise ValueError("Export dependencies changed")
        stamps = inspected["stamps"] + [
            _timestamp(stamp) for stamp in protection.finished[row["run_id"]]
        ]
        if not stamps or any(stamp is None or stamp >= cutoff for stamp in stamps):
            report.files_skipped += 1
            return
        newest = max(stamps)
        # cutoff is computed from a single pass-wide clock reading.
        now = report._now
        report.eligible_files.append(
            EligibleFile(
                path=str(data_dir / row["relative_path"]),
                run_id=row["run_id"],
                kind=row["kind"],
                age_days=(now - newest) / SECONDS_PER_DAY,
                size_bytes=row["size"],
            )
        )
        report.candidates["files"] += 1
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        report.files_skipped += 1
        report.errors.append(f"Export retained ({row['relative_path']}): {exc}")
