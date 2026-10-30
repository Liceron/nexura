# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Operator commands for the shared service migration ledger and blob retention."""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from contextlib import ExitStack, closing
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from nexura_runtime.service.storage_backend import resolve_database_url

PASSWORD_IN_ARGV_WARNING = (
    "warning: --database-url contains a password, which is visible in the process list, "
    "shell history and job specs; set NEXURA_DATABASE_URL (for example from a secret) instead"
)


MIGRATION_ENTRY_POINTS = "nexura.migrations"


def load_migration_components() -> None:
    """Import every installed package's migration module (entry point group
    ``nexura.migrations``), so ``db migrate|status`` covers, for example, the closed edition's
    membership tables. Importing a module registers its component."""
    from importlib.metadata import entry_points

    for entry in entry_points(group=MIGRATION_ENTRY_POINTS):
        entry.load()


def database_command(args: argparse.Namespace) -> int:
    if args.database_url and urlsplit(args.database_url).password:
        print(PASSWORD_IN_ARGV_WARNING, file=sys.stderr)
    url = resolve_database_url(args.database_url)
    if url is None:
        print("Set NEXURA_DATABASE_URL or --database-url", file=sys.stderr)
        return 1
    from nexura_runtime.modules.optional_packages import POSTGRES_PACKAGE, require_package

    postgres = require_package(POSTGRES_PACKAGE, "Postgres service migrations")
    load_migration_components()
    db = postgres.Database.from_url(url)
    try:
        if args.db_action == "migrate":
            db.run(postgres.migrate_all, timeout=postgres.MIGRATION_TIMEOUT + 30)
        status = db.run(postgres.migration_status)
        print(json.dumps({"components": status, "scope": "shared service ledger"}, sort_keys=True))
        return 0 if all(value == "current" for value in status.values()) else 1
    finally:
        db.close()


PAYLOAD_RETENTION_ENV = "NEXURA_BLOB_PAYLOAD_RETENTION_DAYS"
#: The smallest grace period the command accepts: writers must commit within it.
MIN_GRACE_HOURS = 1.0


def _finite(value: float | None, name: str) -> str | None:
    """An error message when ``value`` is NaN or infinite."""
    if value is not None and not math.isfinite(value):
        return f"{name} must be a finite number"
    return None


def prune_command(args: argparse.Namespace) -> int:
    """Explicit row retention, one bounded batch per category and database; JSON report.

    Dry runs open SQLite read-only and never migrate schemas or initialize blob stores.
    Exit 2 for refused inputs, 1 for pending Postgres migrations, 0 for success.
    """
    import sqlite3

    from nexura_runtime.store_retention import (
        DEFAULT_PRUNE_LIMIT,
        MIN_DEDUP_RETENTION_SECONDS,
        prune_cutoff,
        sqlite_prune_channels,
        sqlite_prune_traces,
    )

    if args.database_url and urlsplit(args.database_url).password:
        print(PASSWORD_IN_ARGV_WARNING, file=sys.stderr)
    now = time.time()
    limit = getattr(args, "limit", DEFAULT_PRUNE_LIMIT)
    retry_seconds = getattr(args, "delivery_retry_days", 7.0) * 86400.0
    try:
        prune_cutoff(retry_seconds, now, limit, minimum=MIN_DEDUP_RETENTION_SECONDS)
        ages = {
            "dedup_keys": args.dedup_days * 86400.0,
            "outbox_receipts": args.outbox_days * 86400.0,
            "eval_traces": args.eval_trace_days * 86400.0,
        }
        cutoffs = {
            key: prune_cutoff(age, now, limit, minimum=retry_seconds if key == "dedup_keys" else 0)
            for key, age in ages.items()
        }
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    paths = list(dict.fromkeys(str(Path(path).resolve()) for path in args.sqlite or []))
    url = resolve_database_url(args.database_url)
    if not paths and url is None:
        print("Give --sqlite PATH and/or NEXURA_DATABASE_URL / --database-url", file=sys.stderr)
        return 2
    reports: list[dict[str, Any]] = []
    try:
        # Open and validate every file before any deletions, without creating missing files.
        with ExitStack() as stack:
            connections = []
            for path in paths:
                conn = stack.enter_context(
                    closing(
                        sqlite3.connect(
                            Path(path).resolve().as_uri()
                            + ("?mode=ro" if args.dry_run else "?mode=rw"),
                            uri=True,
                            timeout=30,
                        )
                    )
                )
                tables = {
                    row[0]
                    for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
                }
                if not tables.intersection({"channel_dedup", "channel_outbox", "eval_traces"}):
                    raise ValueError(f"No retention tables in {path}")
                connections.append((path, conn, tables))
            postgres: Any = None
            db: Any = None
            if url is not None:
                from nexura_runtime.modules.optional_packages import (
                    POSTGRES_PACKAGE,
                    require_package,
                )

                postgres = require_package(POSTGRES_PACKAGE, "Postgres row retention")
                db = postgres.Database.from_url(url)
                stack.callback(db.close)
                status = db.run(postgres.migration_status)
                required = (
                    "channels",
                    "channel_retention",
                    "evaluations",
                    "evaluation_trace_retention",
                )
                if any(status.get(component) != "current" for component in required):
                    print("Run nexura-runtime db migrate before db prune", file=sys.stderr)
                    return 1
            for path, conn, tables in connections:
                counts = dict.fromkeys(ages, 0)
                for key, table, dedup, outbox in (
                    ("dedup_keys", "channel_dedup", True, False),
                    ("outbox_receipts", "channel_outbox", False, True),
                ):
                    if table in tables:
                        with conn:
                            counts[key] = sqlite_prune_channels(
                                conn,
                                cutoffs[key],
                                limit,
                                dry_run=args.dry_run,
                                dedup=dedup,
                                outbox=outbox,
                            )[key]
                if "eval_traces" in tables:
                    with conn:
                        counts.update(
                            sqlite_prune_traces(
                                conn, cutoffs["eval_traces"], limit, dry_run=args.dry_run
                            )
                        )
                reports.append({"database": str(path), **counts})
            if db is not None:
                channels = postgres.PostgresChannelStore(
                    db, migrate=False, delivery_retry_window_seconds=retry_seconds
                )
                evals = postgres.PostgresEvaluationStore(db, migrate=False)
                counts = {
                    "dedup_keys": channels.prune(
                        ages["dedup_keys"], now=now, limit=limit, dry_run=args.dry_run, outbox=False
                    )["dedup_keys"],
                    "outbox_receipts": channels.prune(
                        ages["outbox_receipts"],
                        now=now,
                        limit=limit,
                        dry_run=args.dry_run,
                        dedup=False,
                    )["outbox_receipts"],
                    **evals.prune(ages["eval_traces"], now=now, limit=limit, dry_run=args.dry_run),
                }
                reports.append({"database": "postgres", **counts})
    except (ValueError, OSError, sqlite3.Error) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "dry_run": args.dry_run,
                "limit_per_category": limit,
                "cutoffs": cutoffs,
                "databases": reports,
                "counts": {key: sum(report[key] for report in reports) for key in ages},
            },
            sort_keys=True,
        )
    )
    return 0


def blob_gc_command(args: argparse.Namespace) -> int:
    """``nexura-runtime db blob-gc``: delete unreferenced and expired payload blobs.

    References are read from every database given (``--sqlite`` files and the Postgres
    database of ``--database-url`` / ``NEXURA_DATABASE_URL``). The command refuses (exit 2,
    nothing deleted) when a reference table is in none of them, when they reference no blob of
    this store's identity, or when none of the store's blobs is referenced, unless ``--force``.
    Prints a JSON report; exit 0 on success.
    """
    from nexura_runtime.blobs.base import BlobStoreError
    from nexura_runtime.blobs.retention import (
        collect_blob_garbage,
        merge_reference_scans,
        sqlite_referenced_keys,
    )
    from nexura_runtime.blobs.url import (
        BLOB_URL_ENV,
        open_blob_store,
        parse_blob_url,
        resolve_blob_url,
    )
    from nexura_runtime.service.storage_backend import default_blob_dir

    if args.database_url and urlsplit(args.database_url).password:
        print(PASSWORD_IN_ARGV_WARNING, file=sys.stderr)
    error = _finite(args.grace_hours, "--grace-hours") or _finite(
        args.payload_max_age_days, "--payload-max-age-days"
    )
    if error:
        print(error, file=sys.stderr)
        return 2
    if args.grace_hours < MIN_GRACE_HOURS:
        print(f"--grace-hours must be at least {MIN_GRACE_HOURS:g}", file=sys.stderr)
        return 2
    max_age_days = args.payload_max_age_days
    if max_age_days is None and os.environ.get(PAYLOAD_RETENTION_ENV, "").strip():
        try:
            max_age_days = float(os.environ[PAYLOAD_RETENTION_ENV])
        except ValueError:
            max_age_days = math.nan
        if not math.isfinite(max_age_days):
            print(f"{PAYLOAD_RETENTION_ENV} must be a finite number of days", file=sys.stderr)
            return 2
    if max_age_days is not None and max_age_days <= 0:
        print("payload max age must be a positive number of days", file=sys.stderr)
        return 2

    sqlite_paths = list(args.sqlite or [])
    blob_url = resolve_blob_url(args.blob_url)
    source = "--blob-url" if args.blob_url else BLOB_URL_ENV
    if blob_url:
        try:
            location: Any = parse_blob_url(blob_url)
        except ValueError as exc:
            print(f"Invalid blob URL: {exc}", file=sys.stderr)
            return 2
        if not args.blob_url and len(sqlite_paths) == 1:
            default = parse_blob_url(default_blob_dir(sqlite_paths[0]).as_uri())
            if location != default:
                print(
                    f"note: using {BLOB_URL_ENV}={location.describe()} from the environment, not "
                    f"the folder next to {sqlite_paths[0]}; the store's identity is checked "
                    "against the database",
                    file=sys.stderr,
                )
    elif len(sqlite_paths) == 1:
        location = parse_blob_url(default_blob_dir(sqlite_paths[0]).as_uri())
        source = "default folder of --sqlite"
    else:
        print("Set NEXURA_BLOB_URL or --blob-url (or give exactly one --sqlite)", file=sys.stderr)
        return 2

    database_url = resolve_database_url(args.database_url)
    if not sqlite_paths and database_url is None:
        print(
            "Give the databases that reference blobs: --sqlite PATH and/or "
            "NEXURA_DATABASE_URL / --database-url",
            file=sys.stderr,
        )
        return 2

    def scan() -> Any:
        scans = [sqlite_referenced_keys(path) for path in sqlite_paths]
        if database_url is not None:
            from nexura_runtime.modules.optional_packages import POSTGRES_PACKAGE, require_package

            postgres = require_package(POSTGRES_PACKAGE, "Blob reference scan on Postgres")
            db = postgres.Database.from_url(database_url)
            try:
                scans.append(db.run(postgres.referenced_blob_keys, timeout=600))
            finally:
                db.close()
        return merge_reference_scans(scans)

    store = open_blob_store(location)
    try:
        report = collect_blob_garbage(
            store,
            scan,
            grace_seconds=args.grace_hours * 3600.0,
            payload_max_age_seconds=max_age_days * 86400.0 if max_age_days else None,
            dry_run=args.dry_run,
            force=args.force,
        )
    except (ValueError, FileNotFoundError, BlobStoreError) as exc:
        print(f"{exc} (blob store: {store.describe()}, from {source})", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "blob_store": store.describe(),
                "blob_store_source": source,
                **report.to_dict(include_keys=args.dry_run),
            },
            sort_keys=True,
        )
    )
    return 0


def _postgres_database(args: argparse.Namespace) -> tuple[Any, Any] | None:
    """``(nexura_postgres module, Database)`` for ``--database-url`` / ``NEXURA_DATABASE_URL``."""
    if args.database_url and urlsplit(args.database_url).password:
        print(PASSWORD_IN_ARGV_WARNING, file=sys.stderr)
    url = resolve_database_url(args.database_url)
    if url is None:
        print("Set NEXURA_DATABASE_URL or --database-url (a Postgres database)", file=sys.stderr)
        return None
    from nexura_runtime.modules.optional_packages import POSTGRES_PACKAGE, require_package

    postgres = require_package(POSTGRES_PACKAGE, "Blob store ownership")
    return postgres, postgres.Database.from_url(url)


def blob_claim_command(args: argparse.Namespace) -> int:
    """``nexura-runtime db blob-claim --i-own-this-store``: the deliberate first claim of a blob
    store that already holds blobs (for example one written before store owners existed, or
    by a SQLite service now moved to Postgres) by this Postgres database.

    Refused (exit 2) when the store already has another owner. Records this database as the
    owner (with the time), then binds the database to the store. Unreferenced blobs written
    before the claim are never deleted by ``db blob-gc`` without ``--force``. Prints JSON.
    """
    from nexura_runtime.blobs.base import BlobStoreError
    from nexura_runtime.blobs.url import (
        BLOB_URL_ENV,
        open_blob_store,
        parse_blob_url,
        resolve_blob_url,
    )

    if not args.i_own_this_store:
        print(
            "Refusing: pass --i-own-this-store to confirm that this database's deployment "
            "wrote the blobs in this store (never claim another deployment's store)",
            file=sys.stderr,
        )
        return 2
    blob_url = resolve_blob_url(args.blob_url)
    if not blob_url:
        print(f"Set {BLOB_URL_ENV} or --blob-url", file=sys.stderr)
        return 2
    try:
        # Both built-in stores (local, GCS) keep an owner record; typed loosely on purpose.
        store: Any = open_blob_store(parse_blob_url(blob_url))
    except ValueError as exc:
        print(f"Invalid blob URL: {exc}", file=sys.stderr)
        return 2
    opened = _postgres_database(args)
    if opened is None:
        return 2
    postgres, database = opened
    try:
        binding = postgres.PostgresBlobBinding(database)
        mine = binding.database_id()
        try:
            store_id = store.identity()
            # Refuse before the owner record is written: a claim is permanent, so a database
            # already bound to another store (a wrong database URL) must never take this one.
            bound = binding.read()
            if bound is not None and bound != str(store_id):
                print(
                    f"Refusing: this database ({mine}) is bound to blob store {bound}, not to "
                    f"{store.describe()} ({store_id}); check the database URL",
                    file=sys.stderr,
                )
                return 2
            owner = store.claim_owner(mine)
            if owner != mine:
                print(
                    f"Refusing: blob store {store.describe()} already belongs to database "
                    f"{owner}, not to this database ({mine})",
                    file=sys.stderr,
                )
                return 2
            binding.ensure(str(store_id), store.describe())
            record = store.owner_record()
        except BlobStoreError as exc:
            print(f"{exc} (blob store: {store.describe()})", file=sys.stderr)
            return 2
    finally:
        database.close()
    print(
        json.dumps(
            {
                "blob_store": store.describe(),
                "store_id": store_id,
                "owner": record[0] if record else None,
                "claimed_at": record[1] if record else None,
            },
            sort_keys=True,
        )
    )
    return 0


def reset_identity_command(args: argparse.Namespace) -> int:
    """``nexura-runtime db reset-identity --this-is-a-copy``: give a copied, restored or branched
    Postgres database a new id and clear its blob store binding, so it can be started as a
    separate deployment against its own (new) ``NEXURA_BLOB_URL`` prefix.

    Never run it on the database that owns the live store: that service would then refuse to
    start (the store belongs to the old id). Prints JSON with the new id.
    """
    if not args.this_is_a_copy:
        print(
            "Refusing: pass --this-is-a-copy to confirm that this database is a copy, restore or "
            "branch that becomes a separate deployment (never the original)",
            file=sys.stderr,
        )
        return 2
    opened = _postgres_database(args)
    if opened is None:
        return 2
    postgres, database = opened
    try:
        new_id = postgres.PostgresBlobBinding(database).reset_identity()
    finally:
        database.close()
    print(json.dumps({"database_id": new_id, "binding": None}, sort_keys=True))
    return 0
