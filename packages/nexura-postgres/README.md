# Nexura Postgres

PostgreSQL run/session storage and a pgvector vector store for nexura-runtime.

## Install

```bash
pip install nexura-postgres
```

Alpha (0.4): `pip install nexura-postgres`. See [package installation](https://github.com/Liceron/nexura/blob/main/docs/packages.md).

## Minimal example

This example constructs a repository without connecting. Call `await repository.init_db()` against a running PostgreSQL server to create the run/session/event tables. pgvector needs the `vector` extension. SQLite needs no add-on; without this package the runtime factory raises `MissingDependencyError` naming `nexura-postgres`.

```python
from nexura_runtime.storage import StorageRepository, get_storage_repository

from nexura_postgres import PostgresRepository

repository = get_storage_repository("postgresql://nexura:secret@localhost:5432/nexura")
assert isinstance(repository, PostgresRepository)
assert isinstance(repository, StorageRepository)  # same protocol as SQLiteRepository
print(type(repository).__name__, repository.dsn)
```

## Durable workflows and agents

`PostgresWorkflowStore(dsn)` is the workflow checkpoint store for `WorkflowEngine` on several
hosts (revisioned checkpoints, history, single-use approvals and the execution journal on the
same pool); `nexura-runtime serve --workflow-state-url postgresql://...` uses it for the
service, together with `PostgresTriggerStore(dsn)`, the trigger job queue (webhooks, receipts,
schedules and job leases on the database clock). `PostgresCheckpointStore(dsn)` does the same
for SDK agent runs. See
[Durable execution](https://github.com/Liceron/nexura/blob/main/docs/durable-execution.md).

## Status and limits

The MVP service foundations provide `Database.from_url(url)`, a shared pool on a private
event-loop thread. Synchronous stores use `db.run(async_callback)`; async callers use
`await db.arun(async_callback)`. A callback receives a connection inside a transaction;
it must not retain the connection or use session state. Each app obtains one database
reference and releases it with `close()` / `await aclose()` when finished.

All pools disable statement caching and use a no-op reset hook for transaction poolers.
The shared pool defaults to 0–5 connections (`NEXURA_DATABASE_POOL_MAX_SIZE` overrides the
maximum), a 30-second command timeout and 60-second inactive lifetime. URL normalization
preserves `sslmode` and removes asyncpg's unsupported `channel_binding` option without
logging credentials. Use direct PostgreSQL URLs for schema-isolated tests; `search_path`
startup parameters are unsuitable for transaction poolers.

New service components use numbered migrations and checksums in
`nexura_schema_migrations`, under transaction advisory lock 734658110. The existing
workflow, trigger and execution ledgers remain independent. Operator commands
`nexura-runtime db migrate --database-url ...` and `db status --database-url ...` operate
on the shared ledger; status exits 1 for pending, newer or mismatched migrations. WP0
registers no new store schemas yet.

`create_api_app(database_url=...)` and `create_enterprise_app(database_url=...)` accept
the opt-in URL; an omitted argument reads `NEXURA_DATABASE_URL`. The library ignores
generic `DATABASE_URL`; the enterprise shared-service launcher maps it explicitly.
Since MVP WP8 every service store is ported, so **a Postgres `database_url` runs the whole
service without any SQLite file** (no switch needed). The default SQLite service and the
existing Postgres workflow-state option remain supported.
Since MVP WP9 such a service refuses to start without `NEXURA_MASTER_KEY` and
`NEXURA_TRIGGER_SECRET_KEY` (the same values on every instance); webhook secrets then use only
the variable, never a key file.

`PostgresTriggerStore` migration 2 adds `nexura_trigger_leases`: named leases
(`acquire_lease(name, holder, ttl_seconds=...)`, `release_lease(name, holder)`) taken by one
insert-or-take-over-if-expired statement on the database clock, so exactly one of any number
of racers wins. The trigger tick (`nexura_runtime.service.triggers.tick`) runs one bounded pass
under the lease `trigger-tick`, one at a time across instances.

Service workspaces and resource ownership (MVP WP2): `WorkspaceRepository.bind_database(db)`
stores them through `PostgresWorkspaceStore` (migration component `workspaces`), read-through
with no in-process cache, so several instances never disagree. The closed edition binds its
members and membership audit the same way. Two optional features still keep rows in SQLite
(the remote single-operator mode's delivery tokens and failed-run repro capture); with a
Postgres `database_url` they are refused, and `NEXURA_ALLOW_PARTIAL_POSTGRES=1` allows them for
development and tests only. **Never set it in a deployment**: the service then logs a warning,
the operator view of `/readyz` reports `"storage": "partial-postgres"` (`"postgres"` otherwise),
and it refuses to start on Cloud Run (`K_SERVICE`). Other installed
packages register migration components through the `nexura.migrations` entry point group, so
`db migrate|status` covers them. Run `db status` from the image that serves the database (the
enterprise image for an enterprise deployment): without `nexura-enterprise` installed, its
`membership` component is unknown and reported as newer than the application (exit 1).

Studio runs (MVP WP3): `PostgresStudioStore(database, checkpoint_store=..., db_path=...)` keeps
Studio workflows, run records and run events in the shared database (migration component
`studio`; tables `nexura_studio_workflows`, `nexura_studio_runs`, `nexura_studio_run_events`, the
SQLite names). The service uses it whenever `database_url` is set, with `PostgresWorkflowStore`
and `PostgresTriggerStore` on the same URL. Event numbers are appended under a per-run
transaction-level advisory lock, so two instances writing one run get unique, gap-free
`event_seq` values; a run read (row plus events) is one `REPEATABLE READ READ ONLY`
transaction (`Database.run(..., isolation="repeatable_read", readonly=True)`). Opening the store
sweeps only legacy, non-journaled runs whose owner process on this host is gone; journaled
runs are decided by their run lease. Run owners are process identities (host name, boot id
and pid namespace hashed, plus a per-process id), never bare host names, so an instance never
marks another instance's live run interrupted; a run the journal cannot judge is recoverable
once its row has been idle for three lease periods. With a blob codec the store registers
`PostgresBlobBinding`, which also gives the database a random id that the blob store records as
its owner. Another database is refused against an owned store, and a fresh database never
claims a store that already holds blobs (`nexura-runtime db blob-claim --i-own-this-store` is
the deliberate first claim). A copy or branch keeps the id: run
`nexura-runtime db reset-identity --this-is-a-copy` before using it as a separate deployment.

Chat sessions, agents and skills (MVP WP4): `PostgresSessionStore(database)` implements the
`SessionStore` protocol (plus `delete`, `list_sessions`, `updated_at`) on `nexura_sessions` and
`nexura_session_tombstones` (component `sessions`). Saves are compare-and-swap on the revision,
across instances: `UPDATE … WHERE session_id = $1 AND revision = $2 RETURNING`, and a first save
is `INSERT … ON CONFLICT DO NOTHING RETURNING` under a per-session transaction lock, so a deleted
session's revision is never reused. `PostgresAgentStore(database)` and
`PostgresSkillStore(database)` (components `agents` and `skills`) have the API, errors and
limits of the SQLite `AgentStore` and `SkillStore`; every mutation of a workspace runs under a
transaction-level advisory lock on that workspace, so the per-workspace limits and the
duplicate agent-name rule hold when instances create at once (`UNIQUE (workspace_id, name)` is
the backstop). The service uses all three whenever `database_url` is set. None of them closes
the shared `Database`.

Secrets and the usage ledger (MVP WP5): `PostgresSecretStore(provider, database)` (component
`secrets`) has the API and errors of `EncryptedSecretStore` and its envelope format byte for
byte (`BYTEA` columns), so a row copied between the backends decrypts with the same master key
and another key fails closed with `MasterKeyMismatchError`; a replace is one atomic upsert that
keeps `created_at` and `created_by`. It is marked `shared`, so `ScopedCredentialStore` reads it
through and a revocation on one instance holds on every other at once.
`PostgresUsageLedger(database)` (component `usage`) is the `nexura-agents` `UsageLedger` with its
rows in `nexura_usage_entries`, `nexura_usage_reservations` and `nexura_usage_budget_policies`.
Every ledger operation is one transaction under `pg_advisory_xact_lock(734658120)`: it reloads
the rows changed since its last look (`change_seq`, taken under the lock, so sequence order is
commit order), runs the base class's check in the caller's thread while the lock is held, and
writes before the commit. Concurrent reservations from any number of processes never exceed a
cap. An instance's first load of the history runs in a read-only snapshot *before* it takes the
lock, so a cold start never holds other instances' budget operations; only the delta is read
under the lock. `get_entries` (run diagnostics) is one indexed read without the lock. Async
callers run ledger operations in a worker thread (`blocking_io`), so the event loop never waits
on the lock. The service uses both whenever `database_url` is set.

Knowledge index (MVP WP7): `PostgresHybridKnowledgeIndex(database, embedder=...)` (component
`knowledge`, tables `hk_documents`, `hk_chunks`, `hk_lineage`) has the API, errors, limits and
lineage of the SQLite `HybridKnowledgeIndex`. Its migration creates the `vector` extension if it
is absent (the role needs that right; the database owner has it on Neon and Cloud SQL). Vectors
are pgvector `vector` values scanned exactly with `<=>` (no ANN index yet); keywords are a
`tsvector` (`'simple'`) column with a GIN index, ranked with IDF-weighted `ts_rank_cd` instead
of FTS5 `bm25`, so the order of similar hits can differ. An ingest takes the workspace and then
the collection advisory lock (`nexura-knowledge-ws/<ws>`, `nexura-knowledge/<ws>/<collection>`),
a reindex and a delete the collection lock; searches read one `REPEATABLE READ` snapshot and take
no lock. A reindex detects a concurrent change by a per-collection version
(`hk_collection_versions`, migration 2) that every ingest, delete and reindex bumps in its own
transaction, and writes the new vectors in batches inside one transaction. The keyword query
uses at most 32 distinct terms, scores each matching chunk once and needs no temporary files;
its time grows with matching chunks times terms. The SQL names the extension's schema (read
from `pg_extension`), so the index works whatever the search path is. Synchronous methods are
network calls (`blocking_io = True`); constructing the index runs its migrations, so the service
builds it at startup.

Evaluations, channels and integrations (MVP WP8): `PostgresEvaluationStore(database,
blob_payloads=...)` (component `evaluations`; tables `eval_datasets`, `eval_runs`, `eval_traces`),
`PostgresStudioEvalStore(database)` (`studio_evaluations`; `studio_eval_datasets`,
`studio_eval_runs`), `PostgresChannelStore(database)` (`channels`; `channel_dedup`,
`channel_outbox`) and `PostgresIntegrationRegistrations(database)` (`integrations`;
`nexura_service_integration_registrations`) have the APIs of their SQLite twins and keep the
SQLite table names. `eval_traces.trace_json` is `TEXT` holding the blob codec's text (never
`JSONB`), and setting `blob_payloads` registers `PostgresBlobBinding`, so large traces go to the
blob store and `db blob-gc` finds their references. A `seq` identity column replaces SQLite's
`rowid` as the tie-break of every list. A channel dedup key is recorded by one
`INSERT … ON CONFLICT DO NOTHING RETURNING`, so of any number of instances exactly one records a
key. A Studio dataset save is a compare-and-set on its revision under a per-dataset advisory
lock; a Studio run is inserted under its workspace's run lock with `created_at` from the
database clock, and the retention (`MAX_RUNS_PER_WORKSPACE`) runs under the same lock. A
registration takes one advisory lock per tool id (sorted), refuses ids another workspace holds
(`IntegrationToolTakenError`; a unique index on `module_id` is the backstop) and upserts in one
transaction. NUL in a stored text value is kept as U+FFFD. All four are `blocking_io`; the
service runs their calls off the event loop and builds them at startup.

**Rule for any future writer of `nexura_usage_entries` or `nexura_usage_reservations`** (a
retention job, a backfill, manual SQL): take `pg_advisory_xact_lock(734658120)` first and give
each written row a new `change_seq` (`nextval('nexura_usage_change_seq')`). A row committed
outside the lock can carry a sequence value below one a ledger has already read past, and that
ledger would never load it.

Secrets writes are one statement each, atomic across instances: a create
(`put(..., mode="create")`) is `ON CONFLICT DO NOTHING` and raises `SecretExistsError` when
another instance created the name first; a replace (`mode="replace"`) is an `UPDATE` that fails
with `SecretNotFoundError` when the secret was deleted meanwhile; and no write lowers a
`revoked` status (only `set_status` does). Do not rotate `NEXURA_MASTER_KEY` on Postgres yet:
the closed edition's `RotatingSecretStore` handles only the SQLite file.

Large payloads (MVP WP2b) are not stored in Postgres: run-event payloads and imported eval
traces above 8 KiB go to the blob store named by `NEXURA_BLOB_URL` (`gs://bucket/prefix` with
`nexura-runtime[gcs]`, or `file:///path`), and the row keeps a reference with a sha256 and a
short preview. `referenced_blob_keys` is the Postgres side of `nexura-runtime db blob-gc`: it
reads the reference columns that exist in the current schema, with the store ids they record and
the keys of unfinished runs. A reference column must be `TEXT` holding the codec's text
unchanged (`JSONB` reorders keys, so markers would be missed); any other type is refused. Setup,
IAM and retention are in the closed edition's self-hosting guide.

Operations notes:
- Pass the URL as `NEXURA_DATABASE_URL` (for example from Secret Manager), not as
  `--database-url`: a password on the command line is visible in the process list, shell
  history and job specs, and the CLI warns about it.
- Use `sslmode=verify-full` for managed Postgres such as Neon. asyncpg verifies the server
  certificate only with `verify-ca`/`verify-full`, and it cannot use SCRAM channel binding.
  A remote host without `require`/`verify-*` logs a warning.
- Migrations: every component is applied in one transaction under one advisory lock, so a
  statement that cannot run in a transaction (`CREATE INDEX CONCURRENTLY`) is not supported.
  Waiting for the lock and each statement are bounded by `MIGRATION_TIMEOUT` (300 s), not by
  the pool's 30 s `command_timeout`, so a second instance can wait out a long migration.
- `Database` is not fork-safe: create it after any pre-fork. Using an instance created before
  `fork` raises `RuntimeError` instead of hanging.
- `ping()` (readiness) is bounded to 10 s and connecting to 10 s, so an unreachable database
  fails readiness quickly.

Tests load the `nexura_runtime.testing.pytest_backend` plugin. Store contracts use the
`backend` fixture and, for Postgres, `service_database_url`. `--service-backend=postgres`
sets a disposable schema URL per test; it skips without `NEXURA_TEST_POSTGRES_URI`.
Mark SQLite file/schema tests `sqlite_only` with a reason. `isolated_postgres_database`
gives a test its own database (`tests/postgres` uses it for every test). Under
`--service-backend=postgres` or `NEXURA_REQUIRE_POSTGRES_TESTS=1` (set by the CI Postgres
jobs) a missing URI, or a run in which no test passed, fails instead of skipping. The
service CI steps run every store and service suite with `--service-backend=postgres`. WP11 adds the PgBouncer
service and the pooled smoke. `nexura_runtime.testing.pooling_rules` is the static
transaction-pooling gate; both the OSS and the enterprise suites run it.

> Status: **alpha** (0.4), Python 3.12 (the same as `nexura-runtime`). Tested against a local
> disposable PostgreSQL service with pgvector; never against a managed cloud database.

## Documentation and licence

[Main documentation](https://github.com/Liceron/nexura/blob/main/docs/README.md) · [Full guide](https://github.com/Liceron/nexura/blob/main/docs/knowledge.md). MIT, Copyright (c) 2026 Liceron. See [LICENSE](https://github.com/Liceron/nexura/blob/main/packages/nexura-postgres/LICENSE).
