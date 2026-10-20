# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Usage ledger maintaining reservations, transactions, and concurrency-safe spend accounting."""

from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterator
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal
from typing import Any

from nexura_agents.usage.errors import (
    BudgetExceededError,
    CrossWorkspaceAccessError,
    ReservationNotFoundError,
)
from nexura_agents.usage.models import (
    BudgetConfig,
    BudgetStatus,
    LedgerEntry,
    Reservation,
    TokenUsage,
    UsageState,
)

_ZERO = Decimal("0")
_MICRO_CENT = Decimal("0.00000001")


class UsageLedger:
    """Thread-safe and asyncio-safe usage ledger.

    Supports in-memory tracking or persistent SQLite storage with WAL mode.
    Guarantees atomic reservations preventing overspend under concurrent execution.
    """

    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path
        self._transaction: sqlite3.Connection | None = None
        self._thread_lock = threading.Lock()
        self._entries: list[LedgerEntry] = []
        self._reservations: dict[str, Reservation] = {}

        if self.db_path:
            self._init_sqlite()
            self._load_from_sqlite()

    @contextlib.contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        if self._transaction is not None:
            yield self._transaction
        else:
            assert self.db_path is not None
            with contextlib.closing(sqlite3.connect(self.db_path, timeout=30)) as conn, conn:
                yield conn

    @contextlib.contextmanager
    def _atomic(self) -> Iterator[None]:
        """Serialize read/check/write across threads and independent SQLite handles."""
        with self._thread_lock:
            if not self.db_path:
                yield
                return
            with self._connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                self._transaction = conn
                try:
                    self._load_from_sqlite()
                    yield
                except BaseException:
                    conn.rollback()
                    self._load_from_sqlite()
                    raise
                finally:
                    self._transaction = None

    def _init_sqlite(self) -> None:
        """Create tables in SQLite if database path is configured."""
        if not self.db_path:
            return
        with self._connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS usage_entries (
                    entry_id TEXT PRIMARY KEY,
                    timestamp REAL NOT NULL,
                    workspace_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    parent_run_id TEXT,
                    node_id TEXT,
                    entry_type TEXT NOT NULL,
                    entry_category TEXT NOT NULL DEFAULT 'provider_call',
                    amount_usd TEXT NOT NULL,
                    tokens INTEGER,
                    prompt_tokens INTEGER,
                    completion_tokens INTEGER,
                    usage_state TEXT NOT NULL,
                    model_id TEXT,
                    turns INTEGER NOT NULL DEFAULT 0,
                    tool_calls INTEGER NOT NULL DEFAULT 0,
                    reservation_id TEXT,
                    provider_call_id TEXT,
                    pricing_version TEXT,
                    description TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS reservations (
                    reservation_id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    parent_run_id TEXT,
                    node_id TEXT,
                    model_id TEXT,
                    estimated_tokens INTEGER NOT NULL DEFAULT 0,
                    estimated_cost_usd TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    timeout_seconds REAL NOT NULL DEFAULT 300.0,
                    provider_call_id TEXT,
                    pricing_version TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );
                """
            )
            # Add missing columns if upgrading an existing SQLite DB
            for col_sql in [
                (
                    "ALTER TABLE usage_entries ADD COLUMN "
                    "entry_category TEXT NOT NULL DEFAULT 'provider_call';"
                ),
                "ALTER TABLE usage_entries ADD COLUMN provider_call_id TEXT;",
                "ALTER TABLE usage_entries ADD COLUMN pricing_version TEXT;",
                "ALTER TABLE reservations ADD COLUMN provider_call_id TEXT;",
                "ALTER TABLE reservations ADD COLUMN pricing_version TEXT;",
            ]:
                with contextlib.suppress(sqlite3.OperationalError):
                    conn.execute(col_sql)

            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_entries_ws_run "
                "ON usage_entries(workspace_id, run_id);"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_res_ws_status "
                "ON reservations(workspace_id, status);"
            )

    def _load_from_sqlite(self) -> None:
        """Hydrate entries and reservations from SQLite on reopen."""
        if not self.db_path:
            return
        with self._connection() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM usage_entries ORDER BY timestamp ASC").fetchall()
            self._entries = []
            for row in rows:
                r = dict(row)
                self._entries.append(
                    LedgerEntry(
                        entry_id=r["entry_id"],
                        timestamp=r["timestamp"],
                        workspace_id=r["workspace_id"],
                        run_id=r["run_id"],
                        parent_run_id=r["parent_run_id"],
                        node_id=r["node_id"],
                        entry_type=r["entry_type"],
                        entry_category=r.get("entry_category", "provider_call"),
                        amount_usd=Decimal(r["amount_usd"]),
                        tokens=r["tokens"],
                        prompt_tokens=r["prompt_tokens"],
                        completion_tokens=r["completion_tokens"],
                        usage_state=r["usage_state"],
                        model_id=r["model_id"],
                        turns=r["turns"],
                        tool_calls=r["tool_calls"],
                        reservation_id=r["reservation_id"],
                        provider_call_id=r.get("provider_call_id"),
                        pricing_version=r.get("pricing_version"),
                        description=r["description"],
                        metadata=json.loads(r["metadata_json"]),
                    )
                )

            res_rows = conn.execute("SELECT * FROM reservations").fetchall()
            self._reservations = {}
            for row in res_rows:
                r = dict(row)
                self._reservations[r["reservation_id"]] = Reservation(
                    reservation_id=r["reservation_id"],
                    workspace_id=r["workspace_id"],
                    run_id=r["run_id"],
                    parent_run_id=r["parent_run_id"],
                    node_id=r["node_id"],
                    model_id=r["model_id"],
                    estimated_tokens=r["estimated_tokens"],
                    estimated_cost_usd=Decimal(r["estimated_cost_usd"]),
                    status=r["status"],
                    created_at=r["created_at"],
                    timeout_seconds=r["timeout_seconds"],
                    provider_call_id=r.get("provider_call_id"),
                    pricing_version=r.get("pricing_version"),
                    metadata=json.loads(r["metadata_json"]),
                )

    def _save_entry_to_sqlite(self, entry: LedgerEntry) -> None:
        if not self.db_path:
            return
        with self._connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO usage_entries (
                    entry_id, timestamp, workspace_id, run_id, parent_run_id, node_id,
                    entry_type, entry_category, amount_usd, tokens,
                    prompt_tokens, completion_tokens,
                    usage_state, model_id, turns, tool_calls, reservation_id,
                    provider_call_id, pricing_version, description, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    entry.entry_id,
                    entry.timestamp,
                    entry.workspace_id,
                    entry.run_id,
                    entry.parent_run_id,
                    entry.node_id,
                    entry.entry_type,
                    entry.entry_category,
                    str(entry.amount_usd),
                    entry.tokens,
                    entry.prompt_tokens,
                    entry.completion_tokens,
                    entry.usage_state,
                    entry.model_id,
                    entry.turns,
                    entry.tool_calls,
                    entry.reservation_id,
                    entry.provider_call_id,
                    entry.pricing_version,
                    entry.description,
                    json.dumps(entry.metadata),
                ),
            )

    def _save_reservation_to_sqlite(self, res: Reservation) -> None:
        if not self.db_path:
            return
        with self._connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO reservations (
                    reservation_id, workspace_id, run_id, parent_run_id, node_id,
                    model_id, estimated_tokens, estimated_cost_usd, status,
                    created_at, timeout_seconds, provider_call_id, pricing_version, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    res.reservation_id,
                    res.workspace_id,
                    res.run_id,
                    res.parent_run_id,
                    res.node_id,
                    res.model_id,
                    res.estimated_tokens,
                    str(res.estimated_cost_usd),
                    res.status,
                    res.created_at,
                    res.timeout_seconds,
                    res.provider_call_id,
                    res.pricing_version,
                    json.dumps(res.metadata),
                ),
            )

    def get_status(
        self,
        workspace_id: str,
        run_id: str | None = None,
        *,
        config: BudgetConfig | None = None,
    ) -> BudgetStatus:
        """Compute live financial and invocation totals."""
        with self._atomic():
            return self._compute_status_unlocked(workspace_id, run_id, config=config)

    def _compute_status_unlocked(
        self,
        workspace_id: str,
        run_id: str | None = None,
        *,
        config: BudgetConfig | None = None,
    ) -> BudgetStatus:
        total_spent = _ZERO
        committed_cost = _ZERO
        reported_cost = _ZERO
        estimated_cost = _ZERO
        uncertain_cost = _ZERO
        provider_calls_count = 0
        total_tokens = 0
        total_turns = 0
        total_tool_calls = 0
        unknown_usage_count = 0

        # Sum entries
        for entry in self._entries:
            if entry.workspace_id != workspace_id:
                continue
            if (
                run_id is not None
                and entry.run_id != run_id
                and entry.parent_run_id != run_id
                and run_id not in entry.metadata.get("budget_ancestors", [])
            ):
                continue

            # Nested aggregate separation: module_aggregate entries do not
            # double charge financial spend
            if entry.entry_category == "module_aggregate":
                continue

            if entry.entry_type in ("commit", "reconcile", "adjustment"):
                total_spent += entry.amount_usd
                committed_cost += entry.amount_usd
                if entry.usage_state == "reported":
                    reported_cost += entry.amount_usd
                elif entry.usage_state == "estimated":
                    estimated_cost += entry.amount_usd
                elif entry.usage_state == "unknown":
                    uncertain_cost += entry.amount_usd

                if entry.tokens is not None:
                    total_tokens += entry.tokens
                total_turns += entry.turns
                total_tool_calls += entry.tool_calls
                if entry.usage_state == "unknown":
                    unknown_usage_count += 1
                if entry.entry_category == "provider_call" and entry.entry_type in (
                    "reconcile",
                    "commit",
                ):
                    provider_calls_count += 1

        # Sum active reservations
        total_reserved = _ZERO
        reserved_tokens = 0
        for res in self._reservations.values():
            if res.workspace_id != workspace_id:
                continue
            if (
                run_id is not None
                and res.run_id != run_id
                and res.parent_run_id != run_id
                and run_id not in res.metadata.get("budget_ancestors", [])
            ):
                continue
            # Time alone does not prove an in-flight request was unbilled.
            if res.status in ("active", "expired"):
                total_reserved += res.estimated_cost_usd
                reserved_tokens += res.estimated_tokens

        effective_cost = total_spent + total_reserved

        rem_cost = None
        rem_tokens = None
        rem_turns = None
        rem_tool_calls = None
        is_exhausted = False
        soft_limit_reached = False

        if config:
            if config.max_cost_usd is not None:
                rem_cost = max(_ZERO, config.max_cost_usd - effective_cost)
                if effective_cost >= config.max_cost_usd:
                    is_exhausted = True
                elif effective_cost >= (config.max_cost_usd * config.soft_limit_percent):
                    soft_limit_reached = True

            if config.max_tokens is not None:
                rem_tokens = max(0, config.max_tokens - total_tokens - reserved_tokens)
                if total_tokens + reserved_tokens >= config.max_tokens:
                    is_exhausted = True

            if config.max_turns is not None:
                rem_turns = max(0, config.max_turns - total_turns)
                if total_turns >= config.max_turns:
                    is_exhausted = True

            if config.max_tool_calls is not None:
                rem_tool_calls = max(0, config.max_tool_calls - total_tool_calls)
                if total_tool_calls >= config.max_tool_calls:
                    is_exhausted = True

        return BudgetStatus(
            workspace_id=workspace_id,
            run_id=run_id,
            total_spent_usd=total_spent.quantize(_MICRO_CENT, rounding=ROUND_HALF_UP),
            total_reserved_usd=total_reserved.quantize(_MICRO_CENT, rounding=ROUND_HALF_UP),
            effective_cost_usd=effective_cost.quantize(_MICRO_CENT, rounding=ROUND_HALF_UP),
            committed_cost_usd=committed_cost.quantize(_MICRO_CENT, rounding=ROUND_HALF_UP),
            reported_cost_usd=reported_cost.quantize(_MICRO_CENT, rounding=ROUND_HALF_UP),
            estimated_cost_usd=estimated_cost.quantize(_MICRO_CENT, rounding=ROUND_HALF_UP),
            uncertain_cost_usd=uncertain_cost.quantize(_MICRO_CENT, rounding=ROUND_HALF_UP),
            total_tokens=total_tokens,
            total_reserved_tokens=reserved_tokens,
            total_turns=total_turns,
            total_tool_calls=total_tool_calls,
            provider_calls_count=provider_calls_count,
            pricing_version=config.pricing_version if config else None,
            remaining_cost_usd=(
                rem_cost.quantize(_MICRO_CENT, rounding=ROUND_HALF_UP)
                if rem_cost is not None
                else None
            ),
            remaining_tokens=rem_tokens,
            remaining_turns=rem_turns,
            remaining_tool_calls=rem_tool_calls,
            is_exhausted=is_exhausted,
            soft_limit_reached=soft_limit_reached,
            unknown_usage_count=unknown_usage_count,
        )

    def reserve(
        self,
        *,
        workspace_id: str,
        run_id: str,
        parent_run_id: str | None = None,
        node_id: str | None = None,
        model_id: str | None = None,
        estimated_tokens: int = 0,
        estimated_cost_usd: Decimal = _ZERO,
        timeout_seconds: float = 300.0,
        provider_call_id: str | None = None,
        pricing_version: str | None = None,
        config: BudgetConfig | None = None,
        parent_config: BudgetConfig | None = None,
        ancestor_configs: dict[str, BudgetConfig] | None = None,
        workspace_config: BudgetConfig | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Reservation:
        """Atomically reserve cost and tokens, preventing concurrent overspend."""
        with self._atomic():
            if estimated_tokens < 0 or estimated_cost_usd < 0:
                raise ValueError("Reservation estimates must be nonnegative")
            # Never round a positive admission commitment down to zero.
            estimated_cost_usd = estimated_cost_usd.quantize(_MICRO_CENT, rounding=ROUND_CEILING)
            scopes = [(None, workspace_config), (parent_run_id, parent_config), (run_id, config)]
            scopes.extend((ancestor_configs or {}).items())
            for scope_id, limits in scopes:
                if limits is None:
                    continue
                status = self._compute_status_unlocked(workspace_id, scope_id, config=limits)
                if limits.max_cost_usd is not None:
                    projected = status.effective_cost_usd + estimated_cost_usd
                    if projected > limits.max_cost_usd:
                        raise BudgetExceededError(
                            "cost",
                            projected,
                            limits.max_cost_usd,
                            workspace_id=workspace_id,
                            run_id=scope_id,
                            message=(
                                f"Parent run '{scope_id}' cost limit exceeded"
                                if scope_id == parent_run_id and parent_run_id
                                else f"Workspace '{workspace_id}' cost limit exceeded: "
                                f"{projected} > {limits.max_cost_usd}"
                                if scope_id is None
                                else f"Run '{scope_id}' cost limit exceeded: "
                                f"{projected} > {limits.max_cost_usd}"
                            ),
                        )
                if limits.max_tokens is not None:
                    projected_tokens = (
                        status.total_tokens + status.total_reserved_tokens + estimated_tokens
                    )
                    if projected_tokens > limits.max_tokens:
                        raise BudgetExceededError(
                            "tokens",
                            projected_tokens,
                            limits.max_tokens,
                            workspace_id=workspace_id,
                            run_id=scope_id,
                        )
            metadata = {**(metadata or {}), "budget_ancestors": list(ancestor_configs or {})}

            actual_call_id = provider_call_id or f"pcall_{uuid.uuid4().hex[:12]}"
            # Create reservation
            res = Reservation(
                workspace_id=workspace_id,
                run_id=run_id,
                parent_run_id=parent_run_id,
                node_id=node_id,
                model_id=model_id,
                estimated_tokens=estimated_tokens,
                estimated_cost_usd=estimated_cost_usd.quantize(_MICRO_CENT, rounding=ROUND_HALF_UP),
                status="active",
                created_at=time.time(),
                timeout_seconds=timeout_seconds,
                provider_call_id=actual_call_id,
                pricing_version=pricing_version,
                metadata=metadata or {},
            )
            self._reservations[res.reservation_id] = res
            self._save_reservation_to_sqlite(res)

            # Add reservation ledger entry
            entry = LedgerEntry(
                workspace_id=workspace_id,
                run_id=run_id,
                parent_run_id=parent_run_id,
                node_id=node_id,
                entry_type="reservation",
                entry_category="provider_call",
                amount_usd=estimated_cost_usd,
                tokens=estimated_tokens,
                usage_state="estimated",
                model_id=model_id,
                reservation_id=res.reservation_id,
                provider_call_id=actual_call_id,
                pricing_version=pricing_version,
                description=f"Pre-dispatch reservation for model '{model_id}'",
                metadata=metadata or {},
            )
            self._entries.append(entry)
            self._save_entry_to_sqlite(entry)

            return res

    async def reserve_async(
        self,
        *,
        workspace_id: str,
        run_id: str,
        parent_run_id: str | None = None,
        node_id: str | None = None,
        model_id: str | None = None,
        estimated_tokens: int = 0,
        estimated_cost_usd: Decimal = _ZERO,
        timeout_seconds: float = 300.0,
        provider_call_id: str | None = None,
        pricing_version: str | None = None,
        config: BudgetConfig | None = None,
        parent_config: BudgetConfig | None = None,
        ancestor_configs: dict[str, BudgetConfig] | None = None,
        workspace_config: BudgetConfig | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Reservation:
        """Async reservation wrapper ensuring coroutine concurrency safety."""
        # reserve has no suspension points; its threading lock also covers other loops.
        return self.reserve(
            workspace_id=workspace_id,
            run_id=run_id,
            parent_run_id=parent_run_id,
            node_id=node_id,
            model_id=model_id,
            estimated_tokens=estimated_tokens,
            estimated_cost_usd=estimated_cost_usd,
            timeout_seconds=timeout_seconds,
            provider_call_id=provider_call_id,
            pricing_version=pricing_version,
            config=config,
            parent_config=parent_config,
            ancestor_configs=ancestor_configs,
            workspace_config=workspace_config,
            metadata=metadata,
        )

    def get_reservation(self, reservation_id: str, workspace_id: str) -> Reservation:
        """Read persisted reservation identity without exposing mutable ledger state."""
        with self._atomic():
            res = self._reservations.get(reservation_id)
            if res is None:
                raise ReservationNotFoundError(reservation_id)
            if res.workspace_id != workspace_id:
                raise CrossWorkspaceAccessError(reservation_id, res.workspace_id, workspace_id)
            return res.model_copy(deep=True)

    def reconcile(
        self,
        reservation_id: str,
        *,
        workspace_id: str | None = None,
        actual_cost_usd: Decimal | None = None,
        usage: TokenUsage | None = None,
        is_cancelled: bool = False,
        is_pre_dispatch: bool = False,
        conservative_fallback_cost: Decimal | None = None,
        conservative_fallback_tokens: int = 0,
        description: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> LedgerEntry:
        """Reconcile an active reservation post-response or post-cancellation.

        Conservative reconciliation guarantee:
        - If cancelled before dispatch, reservation is cleanly released without charge.
        - If cancelled during/after dispatch (in flight), conservative cost is charged
          (retains estimated cost or conservative fallback, never zeroed out).
        - If usage is partial or unknown, it is explicitly accounted as estimated/unknown, not zero.
        - Duplicate settlement is harmless (idempotent).
        """
        with self._atomic():
            res = self._reservations.get(reservation_id)
            if res is None:
                raise ReservationNotFoundError(reservation_id)

            if workspace_id is not None and res.workspace_id != workspace_id:
                raise CrossWorkspaceAccessError(reservation_id, res.workspace_id, workspace_id)

            # Idempotent settlement: harmless if already committed or released
            if res.status in ("committed", "released"):
                for e in reversed(self._entries):
                    if e.reservation_id == reservation_id and e.entry_type in (
                        "reconcile",
                        "release",
                    ):
                        return e
                raise ReservationNotFoundError(
                    f"Reservation '{reservation_id}' is already {res.status}"
                )

            if is_cancelled:
                if is_pre_dispatch:
                    # Clean release without charge
                    res.status = "released"
                    self._save_reservation_to_sqlite(res)
                    rel_entry = LedgerEntry(
                        workspace_id=res.workspace_id,
                        run_id=res.run_id,
                        parent_run_id=res.parent_run_id,
                        node_id=res.node_id,
                        entry_type="release",
                        entry_category="provider_call",
                        amount_usd=_ZERO,
                        tokens=0,
                        usage_state="reported",
                        model_id=res.model_id,
                        reservation_id=res.reservation_id,
                        provider_call_id=res.provider_call_id,
                        pricing_version=res.pricing_version,
                        description=f"Cancelled pre-dispatch reservation released: {description}",
                        metadata=metadata or {},
                    )
                    self._entries.append(rel_entry)
                    self._save_entry_to_sqlite(rel_entry)
                    return rel_entry
                else:
                    # Cancelled in-flight / post-dispatch: conservative reconciliation
                    conservative_cost = max(
                        actual_cost_usd or _ZERO,
                        conservative_fallback_cost or _ZERO,
                        res.estimated_cost_usd,
                    )
                    res.status = "committed"
                    self._save_reservation_to_sqlite(res)

                    cancelled_tokens = max(
                        usage.total_tokens or 0 if usage else 0,
                        ((usage.prompt_tokens or 0) + (usage.completion_tokens or 0))
                        if usage
                        else 0,
                        res.estimated_tokens,
                        conservative_fallback_tokens,
                    )
                    rec_entry = LedgerEntry(
                        workspace_id=res.workspace_id,
                        run_id=res.run_id,
                        parent_run_id=res.parent_run_id,
                        node_id=res.node_id,
                        entry_type="reconcile",
                        entry_category="provider_call",
                        amount_usd=conservative_cost.quantize(_MICRO_CENT, rounding=ROUND_CEILING),
                        tokens=cancelled_tokens,
                        prompt_tokens=usage.prompt_tokens if usage else None,
                        completion_tokens=usage.completion_tokens if usage else None,
                        usage_state="estimated",
                        model_id=res.model_id,
                        reservation_id=res.reservation_id,
                        provider_call_id=res.provider_call_id,
                        pricing_version=res.pricing_version,
                        description=(
                            f"Conservative reconciliation for cancelled request: {description}"
                        ),
                        metadata={
                            **(metadata or {}),
                            **res.metadata,
                            "reconciliation_policy": "conservative_cancellation",
                        },
                    )
                    self._entries.append(rec_entry)
                    self._save_entry_to_sqlite(rec_entry)
                    return rec_entry

            # Normal completion
            res.status = "committed"
            self._save_reservation_to_sqlite(res)

            final_cost = actual_cost_usd if actual_cost_usd is not None else res.estimated_cost_usd
            usage_state: UsageState = usage.state if usage else "unknown"
            complete = (
                usage is not None
                and usage.is_complete
                and usage.state == "reported"
                and usage.prompt_tokens is not None
                and usage.completion_tokens is not None
                and (usage.prompt_tokens + usage.completion_tokens > 0)
                and (usage.total_tokens is None or usage.total_tokens > 0)
                and (
                    usage.total_tokens is None
                    or usage.total_tokens >= usage.prompt_tokens + usage.completion_tokens
                )
            )
            final_tokens = (
                (
                    usage.total_tokens
                    if usage.total_tokens is not None
                    else (usage.prompt_tokens or 0) + (usage.completion_tokens or 0)
                )
                if complete and usage is not None
                else max(
                    res.estimated_tokens,
                    conservative_fallback_tokens,
                    (usage.total_tokens or 0) if usage else 0,
                    ((usage.prompt_tokens or 0) + (usage.completion_tokens or 0)) if usage else 0,
                )
            )
            if not complete:
                final_cost = max(final_cost, res.estimated_cost_usd)
                if usage_state == "reported":
                    usage_state = "estimated"

            rec_entry = LedgerEntry(
                workspace_id=res.workspace_id,
                run_id=res.run_id,
                parent_run_id=res.parent_run_id,
                node_id=res.node_id,
                entry_type="reconcile",
                entry_category="provider_call",
                amount_usd=final_cost.quantize(_MICRO_CENT, rounding=ROUND_CEILING),
                tokens=final_tokens,
                prompt_tokens=usage.prompt_tokens if usage else None,
                completion_tokens=usage.completion_tokens if usage else None,
                usage_state=usage_state,
                model_id=res.model_id,
                reservation_id=res.reservation_id,
                provider_call_id=res.provider_call_id,
                pricing_version=res.pricing_version,
                description=description or f"Reconciled reservation for model '{res.model_id}'",
                metadata={**(metadata or {}), **res.metadata},
            )
            self._entries.append(rec_entry)
            self._save_entry_to_sqlite(rec_entry)
            return rec_entry

    def release(
        self,
        reservation_id: str,
        description: str = "",
        *,
        workspace_id: str | None = None,
    ) -> LedgerEntry:
        """Explicitly release an unused reservation."""
        with self._atomic():
            res = self._reservations.get(reservation_id)
            if res is None:
                raise ReservationNotFoundError(reservation_id)

            if workspace_id is not None and res.workspace_id != workspace_id:
                raise CrossWorkspaceAccessError(reservation_id, res.workspace_id, workspace_id)

            if res.status == "released":
                for e in reversed(self._entries):
                    if e.reservation_id == reservation_id and e.entry_type == "release":
                        return e
                raise ReservationNotFoundError(
                    f"Reservation '{reservation_id}' is already {res.status}"
                )

            if res.status != "active":
                raise ReservationNotFoundError(
                    f"Reservation '{reservation_id}' is already {res.status}"
                )

            res.status = "released"
            self._save_reservation_to_sqlite(res)

            entry = LedgerEntry(
                workspace_id=res.workspace_id,
                run_id=res.run_id,
                parent_run_id=res.parent_run_id,
                node_id=res.node_id,
                entry_type="release",
                entry_category="provider_call",
                amount_usd=_ZERO,
                tokens=0,
                usage_state="reported",
                model_id=res.model_id,
                reservation_id=res.reservation_id,
                provider_call_id=res.provider_call_id,
                pricing_version=res.pricing_version,
                description=f"Released reservation: {description}",
            )
            self._entries.append(entry)
            self._save_entry_to_sqlite(entry)
            return entry

    def record_commit(
        self,
        *,
        workspace_id: str,
        run_id: str,
        amount_usd: Decimal,
        parent_run_id: str | None = None,
        node_id: str | None = None,
        tokens: int | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        usage_state: UsageState = "reported",
        model_id: str | None = None,
        turns: int = 0,
        tool_calls: int = 0,
        entry_category: str = "direct",
        provider_call_id: str | None = None,
        pricing_version: str | None = None,
        description: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> LedgerEntry:
        """Direct commit of usage/cost to the ledger."""
        if amount_usd < 0 or any(
            value is not None and value < 0 for value in (tokens, prompt_tokens, completion_tokens)
        ):
            raise ValueError("Usage counts and costs must be nonnegative")
        with self._atomic():
            entry = LedgerEntry(
                workspace_id=workspace_id,
                run_id=run_id,
                parent_run_id=parent_run_id,
                node_id=node_id,
                entry_type="commit",
                entry_category=entry_category,
                amount_usd=amount_usd.quantize(_MICRO_CENT, rounding=ROUND_CEILING),
                tokens=tokens,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                usage_state=usage_state,
                model_id=model_id,
                turns=turns,
                tool_calls=tool_calls,
                provider_call_id=provider_call_id,
                pricing_version=pricing_version,
                description=description,
                metadata=metadata or {},
            )
            self._entries.append(entry)
            self._save_entry_to_sqlite(entry)
            return entry

    def get_entries(
        self,
        workspace_id: str | None = None,
        run_id: str | None = None,
    ) -> list[LedgerEntry]:
        """Fetch entries filtered by workspace or run."""
        with self._atomic():
            results = self._entries
            if workspace_id is not None:
                results = [e for e in results if e.workspace_id == workspace_id]
            if run_id is not None:
                results = [e for e in results if e.run_id == run_id or e.parent_run_id == run_id]
            return list(results)
