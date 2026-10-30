# Changelog

All notable changes to this project.

## 0.4.2 (2026-10-30)

Connect the open-source SDK to a hosted Nexura workspace: deploy from the command line and
send SDK traces. See [docs/hosted.md](docs/hosted.md).

- Hosted SDK connection: `nexura-runtime login|logout|whoami`, `push workflow|agent|traces`,
  and matching `nexura.commands` entry points. Login stores verified credentials with mode
  0600; uploads refuse custom Python tools and use existing declarative import APIs.
- `nexura_agents.tracing.connect()` configures a bounded, batched OTLP/HTTP JSON exporter
  using httpx, with retry, drop counters and exit flushing. Content capture stays opt-in.
- Enterprise: durable workspace editor/viewer API keys on SQLite and Postgres, workspace
  admin key-management APIs and audit; authenticated protobuf/JSON/gzip OTLP ingestion,
  workspace quotas, trace read/delete APIs and retention on the existing trigger tick.
- `push` errors name the server's error code and the rejected fields (never server messages);
  an invalid workflow file is a clean error, not a traceback. `NexuraApiError.details` now
  carries the error detail object.

## 0.4.1 (2026-10-29)

Fixes from a live user pass of the hosted product, the workflow builder upgrade, and the
Postgres and retention work listed below. The deprecated tracing names stay until 0.5.0.

### Added (live user pass and builder)
- Workflow builder: a collapsible module sidebar beside the canvas. Drag a module onto the
  canvas and the step lands where it is dropped; click or Enter adds it at the visible centre
  (next free spot, the view follows when none is free). Only `application/x-nexura-module`
  drops of a module in the loaded catalog are accepted; viewers cannot add steps.
- Workflow builder: a Test panel to try the saved workflow without leaving the page (Save and
  test for unsaved edits, message or JSON input, live step progress, approvals, input pauses,
  Stop, Open run). Each send is one normal run.
- `POST /api/v1/runs/{id}/cancel` cancels a run that is running or paused; Studio has a Cancel
  run action.
- Deletes: `DELETE /api/v1/knowledge/collections/{id}`, `/evaluations/runs/{id}` and
  `/integrations/registrations`, with Studio actions.
- `NEXURA_MODEL_PRICING` (JSON) sets per-model prices for budget checks; a run refused by its
  budget is `AGENT_BUDGET_REFUSED` and is not marked effect-unknown.

### Changed (live user pass)
- Studio is English-only for now. The Hindi dictionary stays and returns with a build that
  sets `VITE_LANGUAGE_CHOICE=on`.
- Credential verify reports `stored` ("Saved, not checked with the provider") instead of
  `valid` when the provider was not called.
- New workflow starts blank and needs a name; deleting a step, a mapping or a test case asks
  first; an agent's Chat tab starts a new conversation.

### Fixed (live user pass)
- Studio: schedules send `cron_expr`; the address bar follows Studio pages; webhook test
  results replace the previous result and a refused test delivery no longer says to sign in
  again; a revoked credential no longer shows its old verification time; reopening a saved
  benchmark shows its per-example results; phone focus follows the opened agent, skill,
  integration or evaluation.
- Trace redaction no longer hides `input_key` and `output_key`.

### Added
- The per-session token of the local server is optional: `nexura-runtime serve
  --no-session-token`, `NEXURA_REQUIRE_SESSION_TOKEN=off`, or `create_api_app(require_session_token=False)`
  (and `nexura_studio.create_app`) let curl and other plain HTTP scripts on this machine call the
  API without the `X-Nexura-Session` header. The default is unchanged. With the token off, a
  state-changing request a browser marks as coming from another page (`Sec-Fetch-Site`, or an
  `Origin` other than the server's own) is refused with 403 `CROSS_ORIGIN_REQUEST`, and a body
  without a `Content-Type` with 415; the loopback-client, `Host` and JSON-only rules stay. The
  switch is refused with `--auth token` and with the unauthenticated network opt-in.
### Added (explicit store retention)
- Bounded, opt-in `prune` methods for memory, SQLite and Postgres channel stores and evaluation
  trace stores, plus `nexura-runtime db prune --dedup-days N --outbox-days N --eval-trace-days N`
  with read-only `--dry-run` counts and independent retention ages. Dedup retention refuses
  windows below seven days or the configured delivery retry window. PostgreSQL batches use
  `FOR UPDATE SKIP LOCKED`; additive numbered retention migrations and a SQLite trace schema
  upgrade preserve existing rows. Trace pruning keeps summaries, datasets and blobs; run
  `db blob-gc` separately for unreferenced blobs. Legacy traces of unknown age are retained.

### Added (MVP Postgres WP0/WP1)
- `nexura_postgres.Database` (one shared asyncpg pool per process on a private loop thread,
  `statement_cache_size=0`, no session state), shared numbered migrations in
  `nexura_schema_migrations` under one transaction-level advisory lock, and
  `nexura-runtime db migrate|status`. `create_api_app(database_url=...)` /
  `NEXURA_DATABASE_URL` opt into Postgres; a full Postgres service still refuses to start
  until the store ports land. Unset, the service uses SQLite exactly as before.

### Added (MVP Postgres WP2)
- Workspaces and resource ownership on Postgres: `WorkspaceRepository.bind_database(db)`
  (`nexura_postgres.PostgresWorkspaceStore`, migration component `workspaces`). A bound
  repository caches nothing, so every service instance sees the same workspaces and ownership
  on its next request; workspace mutations take a per-workspace transaction-level lock. The
  service binds it when `database_url` is set. Postgres service mode still refuses to start
  until every store is ported; `NEXURA_ALLOW_PARTIAL_POSTGRES=1` allows it for development
  and tests only (unported stores stay SQLite). **Never set it in a deployment**: it logs a
  warning at startup, the operator view of `/readyz` reports `"storage": "partial-postgres"`,
  and it is refused on Cloud Run (`K_SERVICE` set). With `database_url` set, a `workspace_repo`
  loaded from SQLite is moved to Postgres, and one without `bind_database` is refused.
- A workspace always keeps an admin: removing or demoting its last admin raises
  `LastAdminError` (HTTP 409 `LAST_ADMIN` on the membership routes), on SQLite (under
  `BEGIN IMMEDIATE`) and on Postgres (under the per-workspace lock).

### Added (MVP Postgres WP2b: blob storage)
- `nexura_runtime.blobs`: a `BlobStore` protocol (`put`, `get` with a sha256 and size check on
  every read, `delete`, `exists`, listing by prefix, a store identity), `LocalBlobStore`
  (atomic create-only writes, mode 0600 files in 0700 directories, symlinked directories and a
  root others can write refused) and `GCSBlobStore` (the official `google-cloud-storage`
  client, new optional extra `nexura-runtime[gcs]`, imported only for a `gs://` store;
  credentials from ADC only). Keys are generated as `<workspace>/<namespace>/<random id>`;
  anything else, including traversal, is rejected.
- Large payloads leave the database. Studio run-event payloads (`data_json`) and imported eval
  traces (`trace_json`) above `NEXURA_BLOB_OFFLOAD_BYTES` (8 KiB by default in Postgres mode or
  when `NEXURA_BLOB_URL` is set; off by default for plain SQLite) are stored as blobs, and the
  row keeps a reference with a 256-character preview. Existing inline rows read as before; no
  data migration. `create_api_app(blob_url=...)` / `NEXURA_BLOB_URL` (`gs://bucket/prefix` or
  `file:///path`) choose the store; the default is the folder `<database file>.blobs`. On Cloud
  Run (`K_SERVICE`/`CLOUD_RUN_JOB`) a local folder is refused in Postgres mode or when named
  explicitly.
- Reading offloaded payloads: verified payloads are cached in memory (keys are write-once);
  `StudioStore.get_run(run_id, payloads=False)` reads no blob (status checks, and run routes
  before their ownership check) and `load_payloads` reads them afterwards; live event streams
  read only new events' payloads. A payload that cannot be read shows as `payload_unavailable`
  with a `reason` (`missing`, `integrity`, `store_error`) instead of failing the run read;
  integrity failures are logged at ERROR. Eval-trace import and online grading do blob I/O off
  the event loop. The cache is bounded by UTF-8 bytes and keyed by key, sha256 and size. In
  Postgres or `gs://` mode the store identity, permissions and database binding are checked at
  startup (the start fails on a problem) and later write failures are logged at ERROR.
- `nexura-runtime db blob-gc`: deletes blobs no row references once older than a grace period
  (default 24 h, minimum 1 h) and, with `--payload-max-age-days` /
  `NEXURA_BLOB_PAYLOAD_RETENTION_DAYS`, run and trace payload blobs older than that, except
  those of unfinished runs (readers then get `payload_unavailable` or skip the trace). It is
  safe while the service writes, idempotent, and has `--dry-run`. A database records the store
  its references use in the one-row table `nexura_blob_store_binding` (SQLite on first offload;
  Postgres migration component `blob_binding`), and every reference records the store id too.
  Without `--force` the command refuses when a reference table is in no scanned database
  (`--sqlite`, Postgres), when a scanned database is bound to another store, or, for unbound
  databases, when no reference names this store or none of its blobs is referenced. A bound
  database passes after run retention emptied it. It cannot recognise a stale copy
  of the right database. The Postgres scan refuses a reference column that is not `TEXT`.
  `nexura-runtime retention --apply` deletes rows only; run `db blob-gc` after it.

### Added (MVP Postgres WP3: Studio runs and workflow state)
- `nexura_postgres.PostgresStudioStore`: Studio workflows, run records and run events in the
  shared Postgres database (migration component `studio`), with the same API, table and column
  names and results as the SQLite `StudioStore`. `event_seq` is appended under a per-run
  transaction-level advisory lock (unique and gap-free across instances; `UNIQUE (run_id,
  event_seq)` as a backstop); a run read is one `REPEATABLE READ READ ONLY` snapshot;
  `data_json` stays `TEXT` holding the blob codec's text. With `database_url` /
  `NEXURA_DATABASE_URL` the service uses it, with workflow state and the trigger queue in the
  same database (`workflow_state_url` defaults to `database_url`). Postgres mode refuses an
  empty `workflow_state_url` and a caller's SQLite `StudioStore` (`create_api_app(store=...)`),
  before connecting. The partial-Postgres switch and its Cloud Run refusal are unchanged.
- `Database.run`/`arun` take `isolation=` and `readonly=` (per transaction, never per session).
- Blob binding is two-way: the Postgres service database has a random id
  (`nexura_database_identity`, `blob_binding` migration v2), and the blob store records the id
  of the database that owns it (`_nexura/store-owner`, or `<prefix>.nexura-store-owner` on GCS,
  create-only). Startup refuses a second database against an owned store (`BlobOwnerError`),
  and `db blob-gc` refuses unless the owning database is among the scanned ones. A database
  restored with `pg_dump`/`pg_restore` keeps its id, so a copy used as a separate deployment
  must run `nexura-runtime db reset-identity --this-is-a-copy` and use its own prefix. A
  database claims a store on its own only when the store is empty or its rows already reference
  it; `nexura-runtime db blob-claim --i-own-this-store` is the deliberate first claim of a store
  that already holds blobs, and `db blob-gc` never deletes unreferenced blobs written before
  the claim without `--force`. `db blob-gc` also refuses an idle copy (unreferenced blobs newer
  than the scanned database's newest run activity); a copy that is still serving runs passes
  this check, so `reset-identity` is the only safe path for copies. A SQLite service refuses an owned store.
- Run owners on the Postgres run store are process identities (the durable lease-owner format:
  hashed host name, boot id and pid namespace, pid, start time and a per-process id). Another
  instance never treats a run as interrupted because of a host name; a run the journal cannot
  judge is recoverable after three lease periods without a row update. In Postgres mode
  `workflow_state_url` must be unset or the same database as `database_url`.
- A plain SQLite service with offloading on binds its database when the codec is attached, not
  inside the first offload. `/readyz` (operator view) shows `blob_payloads` counters
  (`inline_fallbacks`, `omitted_payloads`) when offloading is on. In Postgres or `gs://` mode a
  run-event payload that the blob store cannot take is stored inline only up to
  `NEXURA_BLOB_MAX_INLINE_BYTES` (default 1 MiB); above it only a preview is kept
  (`payload_unavailable`, reason `not_stored`). Alert on the ERROR log line; the counters are
  per instance. The run's state columns are not offloaded or capped.

### Added (MVP Postgres WP4: chat sessions, agents and skills)
- `nexura_postgres.PostgresSessionStore`: chat sessions in the shared Postgres database
  (migration component `sessions`; tables `nexura_sessions` and `nexura_session_tombstones`, the
  SQLite names), implementing the `SessionStore` protocol plus `delete`, `list_sessions` and
  `updated_at`. A save is compare-and-swap on the revision (`UPDATE … WHERE revision = $n`; a
  first save takes a per-session transaction lock, shared with delete, then
  `INSERT … ON CONFLICT DO NOTHING`), so of two instances saving the same revision
  one wins and the other gets `SessionConflictError`. A deleted session keeps its last revision
  as a tombstone, and a re-created one continues the count. `nexura-agents` stays stdlib-only.
- `nexura_postgres.PostgresAgentStore` and `PostgresSkillStore` (components `agents` and
  `skills`): the API, errors and limits of the SQLite `AgentStore` and `SkillStore`. Every
  mutation of a workspace takes a transaction-level advisory lock on it, so the per-workspace
  limits (100 agents, 200 skills) and the duplicate agent-name rule hold under concurrent
  creates on several instances; `UNIQUE (workspace_id, name)` is the backstop for names, and
  agent updates and deletes are compare-and-set on the revision. Lists are ordered by name in
  byte order, as on SQLite. The skill rules (limits, labels, version numbering) are now module
  functions of `nexura_runtime.skills_store` shared by both stores; the SQLite store's
  behaviour is unchanged.
- With `database_url` / `NEXURA_DATABASE_URL` the service uses these three stores on its shared
  database; without it, SQLite as before. Postgres service mode still needs
  `NEXURA_ALLOW_PARTIAL_POSTGRES=1` (development and tests only) until WP5–WP8 land.
- Difference: a NUL character in a session id, agent id, skill name or workspace id cannot be
  stored in Postgres text, so such an id reads as missing there and a write with it is refused
  (`StorageError`, `AgentStoreError`, `SkillError`); `create(agent_id=...)` with an id that
  already exists raises `AgentStoreError` on Postgres, where SQLite lets its `IntegrityError`
  through.

### Added (MVP Postgres WP5: secrets, usage ledger and readiness)
- `nexura_postgres.PostgresSecretStore` (migration component `secrets`, table `nexura_secrets`
  with `BYTEA` ciphertext, nonce and wrapped key): the API and errors of
  `EncryptedSecretStore` and the same envelope format, so a row copied between SQLite and
  Postgres decrypts with the same master key and another key fails closed with
  `MasterKeyMismatchError` when the store opens. A replace is one atomic
  `INSERT … ON CONFLICT DO UPDATE` that keeps `created_at` and `created_by`. The plaintext is
  never sent to the database. Master-key rotation (`RotatingSecretStore`, closed edition) is
  SQLite-only for now.
- `nexura_postgres.PostgresUsageLedger` (component `usage`; tables `nexura_usage_entries`,
  `nexura_usage_reservations`, `nexura_usage_budget_policies`): a subclass of
  `nexura_agents.usage.UsageLedger` that keeps every budget rule of the base class. Each
  operation takes `pg_advisory_xact_lock(734658120)` (one global scope, as SQLite's
  `BEGIN IMMEDIATE`), reloads only the rows changed since its last look (a `change_seq` taken
  under that lock), runs the check and writes before it commits, so concurrent reservations
  from several processes or instances never exceed a cap. `get_entries` is one indexed query
  without the lock, and `reserve_async` runs off the event loop. `nexura-agents` stays
  stdlib-only; its row conversion is now shared module functions (`entry_from_row`,
  `entry_values`, …), with the SQLite ledger's behaviour unchanged.
- `Database.submit(fn)` starts a transaction and returns its future (for a caller that keeps
  a transaction open while it computes, as the ledger does).
- With `database_url` / `NEXURA_DATABASE_URL` the service keeps credentials (with
  `NEXURA_MASTER_KEY`) and the usage ledger in Postgres. Run diagnostics read only the run's
  ledger entries there, off the event loop. A credential created, revoked or deleted through
  one instance is seen at once by the others: `ScopedCredentialStore` reads a backend marked
  `shared` through on every lookup (the SQLite store is unchanged). Postgres service mode
  still needs `NEXURA_ALLOW_PARTIAL_POSTGRES=1` (development and tests only) until WP6–WP8 land.
- Differences on Postgres: a NUL character in a workspace, secret name, other secret text field
  or secret value is refused (`SecretsError`, code `SECRET_INVALID`), and such a name reads as
  missing; ledger entries are listed in commit order (SQLite: by timestamp, the same order
  unless instance clocks disagree).
- Credential writes are atomic across instances (review fix round): `EncryptedSecretStore.put`
  and `PostgresSecretStore.put` take `mode="upsert"` (default, as before), `"create"` (refuses an
  existing name with the new `SecretExistsError`, code `SECRET_EXISTS`) or `"replace"` (refuses
  a missing one with `SecretNotFoundError`); `ScopedCredentialStore.store(mode=...)` maps them
  to the new `CredentialAlreadyExistsError` and `CredentialNotFoundError`. `POST
  /api/v1/credentials` creates with `mode="create"`, so of two instances creating one name at
  once exactly one gets 201 and the other 409 `CREDENTIAL_ALREADY_EXISTS` (before, both got 201
  and the last value won). No write lowers a `revoked` status any more, on either backend and in
  the in-memory store: only `revoke`/`set_status` change it, so a replace racing a revoke on
  another instance never un-revokes the credential. `ScopedCredentialStore` guards its metadata
  map with a lock.
- Async callers never wait on the Postgres ledger's lock on the event loop:
  `UsageLedger.blocking_io` (true for `PostgresUsageLedger`) makes `ledger_call` and
  `budget_call` run ledger operations in a worker thread. `RunBudget` gains
  `get_status_async`, `is_exhausted_async`, `record_usage_async` and `to_dict_async`;
  `settle_provider_call_async`, the usage module, the workflow engine's checkpoint budget
  state, the run routes' budget status and the evaluation runner use them. SQLite and in-memory
  ledgers still run inline. An instance's first ledger load reads the history in a read-only
  snapshot before it takes the lock.
- Postgres mode without `NEXURA_MASTER_KEY` (local profiles only; the shared profile refuses to
  start) logs a warning: credentials then stay in the process, per instance.

### Added (MVP Postgres WP7: knowledge index)
- `nexura_postgres.PostgresHybridKnowledgeIndex` (migration component `knowledge`; tables
  `hk_documents`, `hk_chunks`, `hk_lineage`; the migration runs
  `CREATE EXTENSION IF NOT EXISTS vector`): the API, errors, limits and embedding lineage of the
  SQLite `HybridKnowledgeIndex`. Embeddings are pgvector `vector` values, searched by an exact
  cosine (`<=>`) scan of the newest `max_scan` chunks of a workspace and collection (no ANN
  index yet). Keywords are a `tsvector` column (`'simple'` configuration) with a GIN index.
  Query terms reach SQL only as an array parameter; no query text becomes SQL or `tsquery`
  syntax. Byte accounting uses `octet_length`.
- **Ranking difference:** the Postgres keyword ranker is `ts_rank_cd` per matched query term
  (length-normalised), weighted by the term's bm25-style inverse document frequency in the
  collection, where SQLite uses FTS5 `bm25`. The same documents match; the order of similar
  hits can differ. On the offline `synthetic-en-hi-v1` dataset, keyword-only MRR@5 is 0.778
  (`bm25`: 0.794) and the top hit agrees for 86 % of the queries. No existing ranking assertion
  needed a change.
- Consistency across instances: every write is one transaction; an ingest takes the workspace
  lock (quota) and then the collection lock, a reindex and a delete the collection lock. Uploads
  on several instances never lose a document, never leave two documents with one title, and
  never exceed the workspace quota. A reindex checks, under the lock, that the collection did
  not change since its snapshot and replaces every vector and the lineage in one commit; a
  search reads in one `REPEATABLE READ` snapshot, so other instances see the old index or the
  new one, never a mix. Searches take no lock.
- With `database_url` / `NEXURA_DATABASE_URL` every `/knowledge/*` route and chat retrieval use
  it, through `ServiceStores.knowledge_index`. No `SqliteVectorStore` (or its tables) is
  created in that mode: `app.state.vector_store` and `app.state.rag_pipeline` are `None` (no
  route used them). The routes run the index's synchronous calls in a worker thread when the
  backend blocks (`KnowledgeIndexBase.blocking_io`), so waiting for a lock never blocks the
  event loop. The partial-mode refusal now names only WP8 (evaluations, channels and
  integrations).
- `nexura_runtime.knowledge.hybrid.KnowledgeIndexBase`: the storage-independent part of the
  index (limits, upload checks, parsing) shared by both backends, with `fuse_rankings` and
  `build_hits`. `HybridKnowledgeIndex` behaves exactly as before.
- Differences on Postgres: NUL in document text, title or source URI is stored as U+FFFD
  (offsets unchanged); a workspace or collection id with NUL, or a collection id over 512
  characters, is refused on upload and reindex with 400 `INVALID_COLLECTION_ID`; non-finite
  embedding values are refused.
- Cost and startup (review fixes): the keyword query scores each matching chunk once, counts
  every term's document frequency in one pass and needs no temporary files (at most 32 distinct
  terms; time grows with matching chunks times terms). A reindex checks for concurrent changes
  through a per-collection version (migration 2, `hk_collection_versions`, bumped by every
  ingest, delete and reindex) instead of hashing every vector under the lock, and writes the
  vectors in batches of 500 in one transaction. In Postgres mode the service builds the
  knowledge index (its migration, `CREATE EXTENSION IF NOT EXISTS vector`) when the app is
  created, so no request runs it on the event loop and a missing permission stops the start; a
  later rebuild runs in a worker thread.

### Added (MVP Postgres WP9: Cloud Run statelessness)
- `nexura_runtime.service.triggers.tick.run_trigger_tick(app_state, budget_seconds=45)`: one
  bounded trigger pass on demand (due schedules, expired-job reclaim, pending jobs until the
  budget ends; jobs still running then keep running under their heartbeat), for a service
  scaled to zero that an external scheduler wakes. It runs under the trigger store's named
  lease `trigger-tick`, so one pass runs at a time across instances; a call that finds the
  lease held returns `{"status": "busy"}`. The closed edition mounts it as
  `POST /internal/triggers/tick`. New `TriggerWorker.run_pass(budget_seconds)` and `drain()`.
- Trigger stores have named leases: `acquire_lease(name, holder, ttl_seconds=...)` and
  `release_lease(name, holder)` (memory; SQLite table `trigger_leases`; Postgres migration 2 of
  `PostgresTriggerStore`, table `nexura_trigger_leases`), one atomic
  insert-or-take-over-if-expired statement, so of any number of racers exactly one wins.

### Changed (MVP Postgres WP9)
- **A Postgres service refuses to start without `NEXURA_MASTER_KEY` and
  `NEXURA_TRIGGER_SECRET_KEY`** (`SharedKeyMissingError`, naming each missing variable), before
  any connection or file: a key made up per instance would leave the shared credentials and
  webhook secrets unreadable on every other instance. This includes the local profiles (which
  used to run Postgres mode without a master key, credentials in process memory) and is not
  lifted by `NEXURA_INSECURE_ALLOW_PLAINTEXT_SECRETS`. SQLite mode is unchanged.
- In Postgres mode webhook secrets use only `NEXURA_TRIGGER_SECRET_KEY`: the
  `<db_path>.trigger.key` file fallback is gone there (WP8 review M1), so no per-instance key
  file is ever written. A service with only `workflow_state_url` keeps the file fallback.
- A Postgres-mode app object started again after its shutdown raises `ServiceShutDownError`
  ("create a new app") instead of a closed-pool error inside a store (WP8 review L3).
- The test database fixtures (`service_database_url`, `second_service_database_url`) set both
  keys (test values) unless a test sets its own.

### Added (MVP Postgres WP8: evaluations, channels, integrations; full Postgres mode)
- The last service stores on Postgres: `nexura_postgres.PostgresEvaluationStore` (component
  `evaluations`: `eval_datasets`, `eval_runs`, `eval_traces`), `PostgresStudioEvalStore`
  (`studio_evaluations`), `PostgresChannelStore` (`channels`: `channel_dedup`,
  `channel_outbox`) and `PostgresIntegrationRegistrations` (`integrations`:
  `nexura_service_integration_registrations`, created by the migration, never by the route).
  SQLite table names are kept; a `seq` identity column replaces `rowid` as every list's
  tie-break. `eval_traces.trace_json` is `TEXT` holding the blob codec's text, so large traces
  go to the blob store and `db blob-gc` finds them.
- Across instances: a channel dedup key is recorded by one `INSERT … ON CONFLICT DO NOTHING`, so
  a key delivered through one instance is a duplicate through every other; outbox receipts are
  shared; a Studio eval dataset save is a compare-and-set on its revision; Studio eval runs are
  stamped by the database clock under their workspace's lock, so they list in save order on
  every instance and retention keeps exactly the newest; a tool id is registered for one
  workspace only (two instances racing give one success).
- **Full Postgres mode.** A Postgres `database_url` runs the whole service with no SQLite file
  and no `NEXURA_ALLOW_PARTIAL_POSTGRES`. The switch is now needed only for the two optional
  SQLite-only features (remote single-operator delivery tokens, failed-run repro capture), which
  are refused in Postgres mode without it. `/readyz` (operator view) reports
  `"storage": "postgres"`, or `"partial-postgres"` when such a feature runs under the switch.
  The no-SQLite guard is a hard test for a full OSS and enterprise lifecycle.
- Evaluation, Studio eval, channel and registration calls of the routes run off the event loop
  when the store waits on the network (`blocking_io`).

### Changed (MVP Postgres WP8)
- Integration registrations are stored before the tools are registered in memory, in one
  transaction; a storage error now fails the request (it was suppressed) and leaves nothing
  registered, and an id another workspace claimed meanwhile gets 409
  `INTEGRATION_TOOL_ID_CONFLICT`. On startup a storage error stops the start (it was
  suppressed); a row that cannot be rebuilt is logged and skipped.
- Restored registrations: MCP tools get the fixture's own tool definition (a stand-in definition
  was restored before, which the manifest-digest check refused at the first call), OpenAPI
  operations are restored too (they were not), and an existing ownership row keeps its principal.
- Ties in `created_at` now list in insertion order on SQLite too (evaluation datasets and runs
  newest first, outbox receipts oldest first).
- The knowledge index build lock is per app instead of per process (WP7 review R2-L2).

### Fixed (MVP Postgres WP8)
- `SqliteChannelStore.record_dedup` rolls back after a duplicate key, so its connection no
  longer keeps the file's write lock until the next commit.

### Changed (MVP Postgres WP6: organisations and publications, closed edition)
- The closed edition keeps organisations (members, org workspaces, hashed invites and the org
  audit) and publications (promotions, credential-map changes, audit, daily spend and
  reservations, public runs and visitor history) in Postgres when the service has a
  `database_url`; the code is in `nexura-enterprise`. In the open-source service only the
  partial-mode messages change: the refusal now says WP7–WP8 are pending (knowledge,
  evaluations, channels and integrations), and the partial-mode warning lists organisations and
  publications on the Postgres side.

### Fixed (MVP Postgres WP5)
- The SQLite readiness probe reads the schema page (`SELECT count(*) FROM sqlite_master`,
  read-only) instead of `SELECT 1`, which never touches the file: a corrupt database file is
  now reported unhealthy, as the WP0/WP1 entry below says. One more case is now unhealthy: a WAL
  database whose `-wal` file is present but whose `-shm` file is missing, in a read-only
  *directory* (SQLite cannot create the shared-memory file there, and the service could not
  write there either). A read-only database *file*, or a read-only directory without a pending
  `-wal`, stays healthy (checked for both journal modes).

### Changed (MVP Postgres WP0/WP1)
- `/readyz` and `HealthChecker.check_database` run a read-only `SELECT 1` instead of writing a
  row to `_nexura_health_ping`. A read-only database file or a full disk is therefore no
  longer reported as unhealthy (a corrupt or unreadable file still is). The standalone
  `HealthChecker(db_path=...)` no longer creates a missing parent directory: a missing
  directory reports `unhealthy`, a missing file in an existing directory reports healthy
  without creating it. The details status is `read_verified` (was `read_write_verified`).
  Readiness probes now run off the event loop.
- Deleting a chat session goes through the session store, which keeps a revision tombstone:
  a session re-created with the same id continues at the old revision plus one instead of 1.
- Deleting a session or knowledge document removes its workspace-ownership row durably; before,
  only the in-memory map was cleared and the row came back after a restart.
- All `nexura-postgres` pools disable asyncpg's statement cache and use a no-op reset, so they
  work behind transaction poolers (PgBouncer, Neon's pooler). `channel_binding` is dropped from
  URLs (asyncpg cannot use it).

## 0.4.0 (2026-10-25)

### Removed (P35 A6 S12: legacy durable-execution paths)
- `DurableExecutionEngine` and its `DurableWorkflowSpec`, `DurableNodeSpec`, `DurableRunState`,
  `NodeIntent`, `WorkerLease`, `EffectPolicy`, `DurableExecutionModule`, `DurableExecutionError`,
  `FencingConflictError` and `LeaseExpiredError` (`nexura_runtime.runtime.execution`, also
  `nexura_runtime` and `nexura_runtime.runtime`). Use `WorkflowEngine` with `SQLiteWorkflowStore`
  or `nexura_postgres.PostgresWorkflowStore` (`recover`, `resolve_effect`); leases and fencing are
  `nexura_agents.execution_store`. Importing a removed name raises `RemovedAPIError` (an
  `ImportError`) that names the replacement; the stubs are deleted in the next release.
- `EffectReconciler` and its `EffectRecord`, `EffectStatus`, `EffectError`,
  `EffectIndeterminateError` and `ReconciliationError` (`nexura_runtime.runtime.effects`). Effects
  are execution-journal records (`ExecutionStore.resolve_effect`, `WorkflowEngine.resolve_effect`,
  `POST /api/v1/runs/{id}/effects/{effect_id}/resolve`). The old `effect_records` table is left as
  it was and no longer read.
- `TriggerWorker(retry_mode="new_run")` / `nexura-runtime worker --retry-mode new_run` and
  `TriggerStore.reclaim_expired_jobs(keep_run=False)`: they now raise `ValueError` naming the
  replacement. A reclaimed trigger job continues its journaled run; the no-journal
  compatibility fallback creates a replacement and preserves the old id in `previous_run_ids`.

### Changed (P35 A6 S12)
- Runs the removed engine wrote (`kind="durable_graph"`) stay readable through
  `SQLiteRepository`/`PostgresRepository` `get_run` and `get_run_events`, the journal and the
  effects API; `update_run` on one raises `StorageError`. New `durable_graph` creates and
  ordinary execution commits raise an actionable `ExecutionConflictError`; only the narrow
  lease/resolution operations remain writable for existing indeterminate effects.
- A non-idempotent workflow call that raises, times out or loses its response after its intent
  pauses with `effect_unknown` and remains resolvable. Reconcilable calls probe first and pause
  only on `Unknown`. Confirmation continues without repeating the call; `not_applied` permits
  another attempt and requires fresh approval for a gated step; `failed` fails the run. Optional
  resolution reasons are audited with the actor through the workflow engine, store and REST API.
- Workflow retries now use the same effective class policy as recovery: only pure and idempotent
  handlers may have `max_attempts > 1`. Validation and runtime both reject non-idempotent and
  reconcilable handlers, including undeclared functions and composite/nested handlers.
- Composite sibling failures wait for every unresolved unsafe effect, including cancelled parts
  and mapped sub-workflow children, before becoming terminal. A live reconciler `NotApplied`
  answer re-gates an approved mapped/parallel step while keeping finished outcomes and keys.
- Automatic workflow reconciliation, resends and continuation share one absolute deadline.
  Expiry preserves uncertainty; repeated `NotApplied` loops allow at most three automatic
  resends per part. Statically bound mapped reads keep safe retries without relaxing write checks.
- Terminal limits and failures preserve unresolved dispatched parts and children. The run stays
  at `effect_unknown` until settlement, then fails with the deferred error without resending.
  Direct recovery establishes its deadline and shared three-resend allowance before probing.
- `DurableTriggerJob.previous_run_ids` is no longer deprecated: it records runs replaced by the
  no-journal fallback only.
- Docs: new "How durable execution works" page; workflows, approvals, durable runs, budgets and the
  durable-execution reference now describe recovery as it is (a run resumes at the step it was on;
  completed steps never re-run; an unknown `non_idempotent` effect pauses the run).
- `examples/backend/b05.py` and `b07.py` use `WorkflowEngine` and the execution journal.
- An outcome is unknown only when the handler was actually invoked after its intent. When a
  step key is dispatched for the first time and refused before its handler starts (an expired
  deadline, a module's input or config schema, a saved-agent or skill refusal before the agent
  runs), the run fails as before and the intent is settled `not_applied` by
  `workflow:pre_dispatch`, never left as `effect_unknown`.
- A step that has already started is never settled `not_applied` by the system. This covers
  a node that called `request_approval()` or `interrupt()` (journaled as started with its
  pause), and any re-run of a step key whose intent was already open. If such a re-run is
  refused before it starts (deadline, an edited or deleted saved agent), the run pauses
  `effect_unknown`.
- Rejecting a node's own `request_approval()`:
  - Saved-agent node, when every tool call it dispatched returned a result: its effect is
    confirmed with the per-call outcomes (`agent_tool_results`: id, tool, status) and the
    run ends `rejected` at once.
  - Any other `non_idempotent` or `reconcilable` node, or a saved agent with a tool that
    raised or has no result: the run pauses `effect_unknown` until a person settles what
    the node did, then ends `rejected`.
  - Misuse of `request_approval` still raises `WorkflowError`, and leaves the run paused the
    same way until settlement, after which it fails.

### Added (P35 A6 S12 with P39 G6)
- `WorkflowEngine(guardrail_policy=...)`: a guardrail policy's tool rules apply to module
  nodes (`require_approval` joins the node's approval gate; `tool_input` block/fail refuses
  the node before its intent; `tool_output` block/scrub withhold outputs and `fail` fails the
  run after the call is committed as applied). The items of a mapped module node are each
  checked on their own arguments before any item runs. Without the argument the process
  default policy applies; sub-workflows inherit it. In allowlist mode a step the policy
  cannot match (plain handler, parallel step) is refused.
- `AgentDefinition` subagent delegations are refused before any model call while a policy with
  input, model or output rules is in effect; coordinator and subagent model loops refuse a reply
  with duplicate tool-call ids (`ProviderResponseError`) before any gate, hook, ledger entry or
  dispatch.

### Added (P39 G6: guardrail policy middleware and enforcement)

- `nexura_guardrails.GuardrailPolicy`: one declarative policy for every stage of every run
  (`input`, `model_input`, `model_output`, `tool_input`, `tool_output`, `output`), with `When`
  matching by tool name, origin, toolset, effect class, agent and arguments, actions `allow`,
  `block`, `scrub`, `require_approval` and `fail` (most severe wins), allowlist mode
  (`default_tool_action: block`), per-check and per-stage timeouts, `on_error` fail-closed by
  default, and `install(locked=True)`. Load it from JSON, or YAML with `nexura-guardrails[yaml]`
  (`GuardrailPolicy.from_file`, `load_policy`); unknown keys and duplicate keys are refused.
- Journaled guardrail verdicts: model-based verdicts are committed to a durable run's journal
  before they are acted on and replayed on resume and recovery instead of asking the model
  again (`nexura_agents.verdict_journal`).
- Audit: `GUARDRAIL_DECISION` run events and `nexura.guardrail.decision` trace events for policy
  decisions (with `rule`, `policy_digest`, `source`), and `JsonlAuditSink`,
  `CallbackAuditSink`, `MemoryAuditSink` (`audit_required` fails closed).
- `AgentHooks.input_guardrails(ctx, agent)`: hooks can add input guardrails to every agent, like
  `output_guardrails`.
- Service: `create_api_app(guardrail_policy=...)`, `nexura-runtime serve|worker
  --guardrail-policy FILE` and `NEXURA_GUARDRAIL_POLICY`. The policy is loaded first and locked
  as the process default; a missing or invalid policy, or a policy without `nexura-guardrails`,
  stops the start. `GET /api/v1/guardrails/policy` and `GET /api/v1/guardrails/presets`.
- Saved agents: `AgentSpec.guardrails` presets (`pii_block`, `pii_redact`, `injection_fast`,
  `safety_local`, `judge`) and `stream_guard`; Studio's agent editor has a Guardrails section
  (new agents start with `pii_redact` and `injection_fast`) and shows each tool's effect class;
  the node inspector shows a module's effect class. `/agents/options` lists presets, the
  enforced policy and each tool's `effect_policy`/`effect_class`.
- The `guardrails.check` workflow module (branches `allow`, `block`, `review`, `error`,
  `rewrite`), registered when `nexura-guardrails` is installed (`nexura.modules` entry point).

### Changed (P39 G6)

- Guardrail policy argument comparisons (`gt`, `gte`, `lt`, `lte`) compare `int`, `float` and
  `Decimal` exactly on the arguments a tool receives (typed values keep their type); a value
  that is not a finite number counts as a match for block, fail, scrub and require_approval
  rules and never satisfies an allow rule.
- `JudgeCheck("ollama:<tag>")` uses Ollama's native `/api/chat` and sends `context_tokens` as
  `num_ctx` (with `truncate`/`shift` off), so raising `context_tokens` is enough to judge longer
  text; `api="openai"` keeps the OpenAI-compatible endpoint. New `base_url`, `keep_alive` and
  `transport` options for the native route.
- `decision_record` keeps `policy_digest` and `source`; the trace event's
  `nexura.guardrail.source` reflects it.

- Argument rules match the raw arguments and the ones the tool receives after validation (the
  stricter wins); journaled verdicts are bound to agent, tool, origin, occurrence, stage and a
  content hash; `model_output`/`output` rules hold streamed text and tool-call deltas, and
  `stream_guard="passthrough"` is refused while such rules apply.
- A model reply with duplicate tool-call ids fails the run (`ProviderResponseError`).
- `AgentCoordinator` runs are refused while the effective policy has input, model or output
  rules. `AgentDefinition` subagents are refused at their first tool call; their own input,
  model and output stages are not checked yet (a follow-up closes this before release).
- Policy files (and a judge's `policy_file`) refuse YAML anchors/aliases, deep nesting and
  oversized files; a symlink is resolved once and must reach a regular file.
  `GET /guardrails/policy` shows full detail to admins only.
- Tool arguments: before hooks and guardrail policies see a call, the Runner builds the
  arguments the tool will receive (validated and coerced; a plain `@tool` still drops argument
  names it does not declare, so a stray key never fails a call; MCP/OpenAPI tools keep their
  JSON Schema rules). Policies match both the raw and these arguments, the stricter result
  winning. Migration: none for tools; a hook that inspected only `call.arguments` still sees
  the raw arguments, and `Tool.dispatch_arguments(...)` gives the dispatched ones.

### Added (P38 C9 part A: retention and cleanup for stored run data)
- Operator-run opt-in retention policy and CLI (`nexura-runtime retention`) for finished
  run records, events, repro bundles, and observability traces older than a configured threshold.
- Retention repairs validate ages, recheck lifecycle/approval/dependency protection inside
  per-run SQLite transactions, preserve journals, and report committed counts and partial
  failures. For 0.4, retention deletes database rows only. Export bundles and trace
  files are reported eligible with path, run ID, age, size and reason for manual
  removal: files listed, not deleted. Retention never deletes, renames or opens
  export files for writing. A terminal lifecycle summary survives run-row expiry;
  missing journal authorities veto
  deletion, and registered trace parents protect dependencies. Database descriptors
  are verified before SQL. Any opened-connection identity mismatch or ambiguity
  stops the pass. Isolation for existing process handles/sidecars and SQL is
  bound to the caller's original database and parent device/inode; workers never
  re-authorize a replacement pathname. WAL workers also prove main/WAL/SHM
  descriptor identities after BEGIN and before COMMIT. Unsupported Linux `/proc`
  inspection refuses the pass. Identity failure abandons the worker without
  SQLite rollback/close; checkpoints on close are disabled. The guide records
  the remaining SHM bootstrap/mapping limitation in apply mode. Database opening
  now requires the directory and every ancestor to exclude group/other write
  access. Root-owned sticky exceptions apply to ancestors only; the database's
  own directory must have no group/other write bits, even with the sticky bit,
  and must be owned by the invoking user or root. Ancestor owners must also be trusted since
  owners can change mode bits. Pre-existing rollback journals are refused;
  DELETE-mode journal descriptors are proved after writes and before COMMIT.
  Dry runs use read-only source access and worker-only Linux Landlock write denial.
  Cleanly closed WAL databases without sidecars use a private query-only memory
  snapshot, captured under a Linux read lease and rechecked for identity/content
  metadata changes and sidecar appearance. Unsafe capture fails with preview
  guidance; production journal mode and bytes remain unchanged. Both workers use
  Python -I, constant entries, cwd=/ and empty environments. Missing database
  paths and URI/non-filesystem targets now raise RetentionError and fail the CLI.
  A test guard pins the two worker launches and rejects direct uses of other
  process-launch APIs, aliases and unreviewed keywords (reflective forms are a
  known gap in that test, not in the code).
  Dry run remains the default; `--apply` is required.
- StudioStore reads use one run/event snapshot, and appends check parent existence
  under a write reservation shared with retention. Missing-parent appends log a
  warning and create no orphan event; the public `None` return is preserved.

### Fixed (P38 C1 platforms, OpenTelemetry privacy, gVisor recovery)
- `SkillLibrary.from_directory` works on macOS and Windows: a portable read-only loader
  rejects links, junctions and special files and re-checks each opened file's identity.
  Disk-mode `ScopedFilesystem` stays Linux-only (Landlock ABI >= 3) and fails closed
  elsewhere with an error that names memory mode.
- `retry_last_turn` puts the original turn back when the retry is cancelled (task
  cancellation or `CancelToken`) or fails, without overwriting a newer save; a paused
  retry stays resumable.
- With content capture off, exported `gen_ai.tool.call.id` and the call part of
  `nexura.effect.step_key` are per-run HMAC pseudonyms instead of the raw model call id;
  capture on keeps the raw id. Journal identities and recovery are unchanged.
- gVisor crash recovery only sweeps cgroup leaves under the configured parent, keeps task
  records for runs not proven stale, and cleans the task directory before reporting a
  watchdog leaf-validation error.

### Added (P38 C8: production gVisor profile)
- Sandbox: `SandboxConfig.gvisor_production(...)`, a hardened gVisor profile for servers.
  It enforces only `gvisor_oci` at `min_isolation="gvisor"`, no network, a read-only root,
  explicit normalised absolute paths, host memory/pids cgroups (the test-only cgroup bypass
  is refused even with `NEXURA_REAL_GVISOR=1`), no command prefix other than the test-only
  `sudo -n`, and ceilings of 60 s, 1024 MB, 16 processes, 256 runtime tasks, 128 MiB
  scratch and 4 MiB output (`GVISOR_PRODUCTION_CEILINGS`). A config that breaks any rule
  fails validation, and the backend re-checks the rules and the delegated cgroup parent
  before it reports itself available. Limits default lower (10 s, 256 MB, one process with
  fork/clone denied). Also available as `NEXURA_SANDBOX_PROFILE=gvisor_production`.
- Sandbox: optional runsc SHA-512 pin (`gvisor_runsc_sha512` /
  `NEXURA_SANDBOX_RUNSC_SHA512`), checked by the service user when the module is built and
  before each execution. It is a drift check, not an exec-time integrity check, and does not
  cover the `gvisor-bin/` sidecars. The pin and `NEXURA_SANDBOX_GVISOR_CGROUP_PARENT` are
  never ignored: an invalid value, or either one set without an enabled sandbox, is an error.
- nexura-sandbox: operator preflight `python -m nexura_sandbox.preflight` and
  `nexura_sandbox.gvisor_preflight(config)`.
  - It fails when runsc, its `gvisor-bin/` sidecars, the rootfs or any directory above them
    is writable by group or others, or owned by anyone other than root or the service user.
  - It also checks the pin, the runsc version and OCI seccomp support, rootfs Python access
    for UID 65534, and the cgroup delegation.
  - Then it runs one tiny guest and requires its cgroup `memory.max`/`pids.max`, observed from
    the host, to match, and nothing to be left behind.
  - It never executes a runsc binary that failed a check, and reports errors as failed checks
    rather than tracebacks.
- Docs: "Production gVisor profile" (`docs/sandbox-production.md`) with the operator
  checklist, what the profile enforces and what it only defaults, and its limits.
- Verified with enforced cgroups on one Linux host (cgroup v2, runsc release-20260928.0,
  runsc as root via a test-only sudo prefix). Not externally audited.

### Changed (P38 C8)
- The authenticated shared service now refuses to start when the operator sandbox settings
  are invalid or a registered sandbox module cannot be built.

### Fixed (P38 C8)
- gVisor teardown, the per-run crash watchdog and the startup sweep remove a run's empty
  cgroup leaf that runsc left behind (only `<parent>/nex_<id>`, only when unpopulated), and
  teardown reports a leaf it cannot remove.

### Added (P38 C1: opt-in agent harness)
- Review fixes: disk operations run in a private Landlock worker and recheck moved parents;
  todo notifications omit task text; scoped approvals bind canonical arguments and workspace
  identity; completed compaction summaries and usage replay from execution checkpoints.
- `Agent(harness=Harness(...))` composes progressive Agent Skills directories, budget-triggered
  history compaction, journaled task lists and shared scoped filesystem tools.
- `SkillLibrary.from_directory` and explicit `format="agent-skills"` support pinned SKILL.md
  front matter with bounded text resources; scripts never execute and metadata grants no tools.
- `compact_context` preserves system messages, the latest turn, pending calls and tool pairs;
  optional model summaries use existing budget admission and reported usage accounting.
- `write_todos`/`read_todos` store plans in run state and emit custom events. `ScopedFilesystem`
  provides capped memory or POSIX directory workspaces, rejects traversal and links, and shares
  through explicit parent/child configuration under inherited policies and budgets.

### Added (P39 G5: local safety models and presets)
- `nexura-guardrails` safety-model checks `LlamaGuard` (Llama Guard 3 1B/8B), `ShieldGemma`,
  `GraniteGuardian` (3.0 and 4.1) and `NemotronGuard` (Nemotron Safety Guard 8B v3). Each
  sends the model's documented prompt raw to Ollama (`/api/generate`) or an OpenAI-compatible
  server (`/v1/completions`), parses its documented output strictly and maps labels to the
  taxonomy through `LABEL_MAPS`. Prompt formats are pinned to the model cards and Ollama
  templates (revisions and digests in each module). HTTP uses `httpx`, declared by the new
  `[ollama]` extra with the range `nexura-agents` already requires.
- Fail closed: timeouts, connection and HTTP errors, malformed bodies or replies and budget
  refusals are `error` verdicts (`on_error` must be explicit); an *unsafe* answer with
  missing or unknown labels is a `block` with category `other`, never an error. The judge's
  token cap applies (hard `ContentTooLong`), each call is admitted by the run budget, and the
  server's token counts are reported on `Verdict.usage`. Checked text is defused (special
  tokens, control tags and turn-like lines) before it goes into the prompt.
- `nexura_guardrails.presets`: `pii_block`, `pii_redact`, `injection_fast`, `safety_local`
  (injection pre-filter, then Llama Guard 3 1B via Ollama; refuses a non-local server unless
  `allow_remote=True`), `strict_tools` (allowlist, approval for side-effecting MCP/OpenAPI
  tools, result scanning) and `judge`; `Preset.apply`, `Preset.hooks`, `combine`, `PRESETS`.
- `SAFETY_MODELS`: each supported model's source, pinned revision and license (Llama 3.1 /
  3.2 Community License, Gemma Terms, Apache-2.0, NVIDIA Open Model License). Nexura never
  downloads or redistributes weights. Docs: "Local safety models" and "Presets" in
  `docs/guardrails-package.md`. Tests use documented-format response fixtures and an
  `httpx.MockTransport`; one live test runs only with `NEXURA_TEST_OLLAMA_URL` set.
- `LABEL_MAPS["granite_guardian"]` gains `harm` (Granite Guardian's general risk) -> `other`.

### Fixed (P39 G5 review)
- **Token caps are now a strict upper bound** (`token_upper_bound`: the prompt's UTF-8 length)
  in the safety-model checks and in `JudgeCheck` (G3). The old 3-characters-per-token
  estimate could be beaten threefold (`"x\n"` padding), and Ollama silently drops the middle
  of an over-long prompt, so a harmful request hidden in padding reached the guard as padding
  only. Over-long prompts are now a hard `ContentTooLong`. Safety-model defaults are raised to
  `context_tokens=16384` (Llama Guard 3, Granite Guardian 4.1, Nemotron) and 8192
  (ShieldGemma, Granite Guardian 3.0), sent as `num_ctx`; Ollama requests also carry
  `"truncate": false, "shift": false`, and a truncation flag in a reply is an error. The
  judge keeps its 4096 default (Ollama's OpenAI-compatible endpoint takes no per-request
  context), so it now admits about 1,250 to 1,950 bytes of content: raise
  `OLLAMA_CONTEXT_LENGTH` and `context_tokens` together for more.
- Guard model calls have their own budget ids (`guard-local:<api>:<model>` for a local server
  with the default transport, else `guard:<api>:<model>`), never an agent-model id.
  `safety_local` registers a zero price only for the exact guard model it names on a local
  server (`zero_price`, never replacing an existing price), so it works under a cost cap with
  `deny_unknown_pricing`; `Preset.pricing()` lists the entries.
- `strict_tools` requires approval for every MCP/OpenAPI tool that is not `pure` (including
  `idempotent` and `reconcilable` ones such as a declared idempotent `PUT`/`DELETE`); bare
  allowlist names match local tools only, never an MCP, OpenAPI, A2A or module tool of the
  same name.
- `safety_local(..., allow_remote=True)` on a non-local server reports `network="model"`;
  licenses resolve Ollama tag variants (`llama-guard3`, `:latest`, `:8b-q4_K_M`) to the right
  model or to none, never to a wrong one (`safety_model_for`; ShieldGemma 9B added).
- ShieldGemma and Granite Guardian 3.0 fail closed when the text answer and the log
  probabilities disagree (a "Yes" never becomes an allow), and the answer position skips
  generated whitespace. `defuse` NFKC-normalises and handles Unicode indentation, fullwidth
  colons, marker variants and dash separators. A reply naming another model is an error
  (`ModelMismatch`); compressed replies are refused (`Accept-Encoding: identity`).
- The `[ollama]` extra declares `httpx` (the range nexura-agents already requires).
  `Preset.runner_kwargs()` and loud docs: `apply()` alone does not check tool stages.
- Not changed: the Nemotron prompt ends `Output JSON:` without the template's trailing
  space, because the model's pinned chat template applies `| trim` to the message.
- Follow-ups: the automatic zero price applies only to Ollama's native API on this machine,
  never to an OpenAI-compatible URL (even on localhost, it may proxy a paid provider);
  ShieldGemma documents that a threshold above 0.5 cannot loosen a "Yes" reply; the `judge`
  preset and guide warn that the default capacity is about 1.2 to 1.9 KB at
  `context_tokens=4096` and show how to raise it (native Ollama API for judges planned, G6).

### Added (P39 G4: streaming and output guardrails)
- `Agent(stream_guard=...)` and `StreamGuard(mode, chunk_chars=400, overlap_chars=120)`:
  with `stream_text=True`, an agent with output guardrails never streams its reply
  unchecked. `"auto"` (default) holds it until every output guardrail passed and delivers
  the checked text in `FINAL_OUTPUT`; opt-in `"chunked"` checks the reply in windows with
  every output guardrail and emits only windows that passed (`TEXT_DELTA` with
  `checked: True`), holding back an overlap so a value split across deltas is seen whole;
  `"passthrough"` keeps the old behaviour for an agent's own guardrails only. The turn's
  `MODEL_STARTED` carries `stream_guard` when the reply is checked first.
- `RunEventType.GUARDRAIL_TRIPPED` (deferred from G1): after the `GUARDRAIL_DECISION` of an
  output tripwire and before `RUN_FAILED`, with `stage`, `guardrail`, `agent`, `turn`,
  `phase` (`chunk`/`final`), `stream_guard` and `shown`; never content.
- `AgentHooks.output_guardrails(ctx, agent)`: hooks (a default or inherited policy) add output
  guardrails that run after the agent's own, also hold streamed text, and cannot be opted
  out with `passthrough`. `ComposedHooks` concatenates members' lists in order.
- Studio Playground shows "Checking reply…" (English and Hindi) for a guarded agent's
  reply, then only checked text; a trip discards anything shown.

### Fixed (P39 G4 review)
- Chunked mode holds back the RAW end of each window (at least `overlap_chars` visible
  characters, measured after NFKC without invisible characters, and everything from a
  rewrite that reached it) and checks the whole raw answer at the end, so a key or a card
  with a Luhn-valid prefix cut by a window end is never shown or stored in part.
  `overlap_chars` must be at least 32; `StreamGuard(max_windows=64)` bounds window checks per
  turn (the rest is held until the final check); `UsageLimits` are checked after every window.
- Text written before tool calls passes the output guardrails before it is stored, journaled
  or shown (all modes except `passthrough`); typed agents' is withheld.
- `TOOL_COMPLETED` of a call whose result may end the run (`tool_use_behavior`) has
  `result_withheld: True` instead of the result for agents with output guardrails.
- Typed-output validation errors (`RETRY`, `RUN_FAILED`, `OutputValidationError`) carry
  locations and kinds only, never values; `raw_output` is `None` for guarded agents and the
  rejected reply is replaced in the transcript by a fixed note.
- A non-method `output_guardrails` attribute on hooks, or a hook that raises, fails the run
  with `RUN_FAILED`; guardrails that appear after unchecked text was streamed fail closed.
- Content capture records the checked answer (`nexura.output.checked`), not the raw reply,
  for agents with output guardrails.
- The AG-UI and AI SDK adapters pass on `nexura.guardrail-tripped` with `discard` after a
  chunked trip.

### Changed (P39 G4)
- **Behaviour change:** streamed runs of agents with output guardrails no longer emit
  unchecked `TEXT_DELTA` events (they did before, and redacted text reached the screen).
  Opt out per agent with `stream_guard="passthrough"`. Output trips now also emit
  `GUARDRAIL_TRIPPED` (input trips are unchanged).
- The service chat stream no longer filters deltas itself (P38 C3); it forwards what the
  Runner emits, which also covers policy guardrails and chunked mode. Workflow agent traces
  record `guardrail_tripped`.
- In chunked mode, a final check that rewrites text already shown fails the run closed
  (`GuardrailTripwireTriggered`, guardrail `"stream_guard"`).

### Added (P38 C3: frontend streaming adapters)
- Review fixes: Unicode-preserving CR/LF SSE parsing, terminal errors on malformed,
  failed or incomplete sources, and native Accept fallback. Active agents with
  output guardrails hold chat text until the checked final output; tripwires emit
  no blocked text. Reducer-sensitive state uses authoritative snapshots only.
  Approval finishes identify the pause; Nexura-defined selectors and stock AG-UI
  query usage are documented, with inverted repros and route-level wire goldens.
- Opt-in AG-UI protocol 1.0 and AI SDK 5.0.0 UI message SSE streams on existing
  chat and workflow event endpoints, selected by `stream_format` or vendor `Accept`.
  Native SSE remains the default. Adapters consume only existing public frames,
  omit raw tool-call deltas and expose approval notices without pending arguments.
- Frontend integration guide and offline FakeModel wire-sequence, redaction,
  negotiation, replay and disconnect tests; no new dependencies.

### Added (P38 C7: GitHub, Slack and Google connector depth)
- Review fixes: accept same-origin GitHub repository-ID pagination links, retain fetched
  items on later failures, bind partial cursors to state and stop pagination loops explicitly.
  Redact secret fields without corrupting auth metadata or normal API data. Bound total
  retries by the manifest timeout and return retry-after metadata when deferring reads.
  Refresh serialises across threads, transient exchanges are retryable, read 401s refresh
  once, and write 5xx/408 outcomes are indeterminate. Hide helper secrets in repr, run App
  JWT signers off the event loop, normalise expiry to UTC and discard consumed JWTs.
  Declare read effects, avoid JSON parsing binary downloads, reject multipart boundary
  collisions and clarify that Slack correlation IDs provide no de-duplication guarantee.
- Refreshable OAuth secret records with expiry/skew checks, persisted token rotation,
  in-process single-flight refresh across connector instances, and GitHub App installation
  exchange through an application-supplied JWT. Static PAT/bot/access tokens remain supported.
- GitHub Link, Slack cursor and Drive page-token pagination with `max_items`, explicit
  truncation and partial-page resume cursors; Sheets row caps and Drive incomplete-search flags.
- Provider rate-limit handling, budgeted Retry-After/reset waits and jittered read-only retries.
  Writes keep approval gates, explicit conservative effect classes and no automatic retries;
  Slack client_msg_id and pre-generated Drive file IDs are forwarded where supplied.
- Offline wire-contract and negative tests for refresh/concurrency, pagination, rate limits,
  write keys/effects, actionable errors and token redaction. No live calls or new dependencies.

### Added (P38 C5: runtime evaluations)
- Recorded tool trajectory and argument accuracy graders with exact, in-order subset,
  and any-order matching, named argument normalisers, numeric tolerances, and aggregates.
- Scripted and injected-model persona user simulation with conversation history,
  bounded turns, explicit stops, transcripts, and offline FakeModel examples.
- Post-hoc stored-trace evaluation with deterministic sampling, workflow/agent/time
  filters, workspace-scoped trace persistence, and existing evaluation result storage.
- Evaluation capability, trace import, online and simulation API routes; Studio scorer
  support for argument accuracy while preserving existing routes and report formats.

### Added (P38 C6: workflow state migrations)
- Workflow versions on checkpoints/results and run API responses, with definition digests.
- Registered pure, chained state migrations before resume/recover; fenced journal receipts
  recover a committed migration without applying it twice after a crash.
- Missing-path and missing-node refusals, an explicit saved-definition fallback, approval
  re-gating across definition changes, and `POST /api/v1/runs/{id}/migrate` with trusted
  server hooks. Existing resume/input/recover routes remain supported.
- Review fixes: fence active definitions after lease acquisition, refuse incompatible
  paused child subgraphs before claiming approval, and re-gate migrated payment retries.
  Service migration requires `run` and `edit`, uses the saved workflow and audits the
  actor and version change. Imported legacy `version` fields keep their old hash;
  executable workflow versions serialize as `workflow_version`.

### Added (P39 G2: default policy and coverage gaps)
- `set_default_hooks(hooks)` and `default_hooks()` (`nexura_agents`, module
  `nexura_agents.policy`): hooks every `Runner` uses. A run's hooks are the hooks inherited
  from an enclosing run, then the process default, then the runner's own `hooks=`, each
  object once; the default is the outermost member. `Runner(hooks=...)` adds to it and can
  never replace it. `Runner.effective_hooks()` shows the result.
- Inheritance mirrors budgets: the run's hooks are active (`policy.active_hooks`) while its
  steps execute, so a `Runner()` built inside a run, `Agent.as_tool()` sub-runs, team
  members, `AgentMCPServer` agent calls, modules and templates that run an agent use them.
- `Runner(default_hooks=False)` and `AgentCoordinator(default_hooks=False)` leave out the
  process default. **For tests only**; hooks inherited from an enclosing run still apply.
- `set_default_hooks(policy, locked=True)` and `policy.default_hooks_locked()`: while the
  default is locked, `default_hooks=False`, `set_default_hooks(None)` and installing
  another default raise `SDKValidationError` (the same object may be installed again).
  The service will install its policy this way (G6).
- `AgentCoordinator.replace(**changes)`; `AgentCoordinatorModule` with skills now keeps the
  coordinator's `hooks` and `default_hooks` (it used to rebuild the coordinator without
  them).
- `CoordinatorCallLedger`, `MemoryCallLedger` and `purge_coordinator_ledger`
  (`nexura_runtime.runtime.agents.call_ledger`), `CoordinatorCallOutcomeUnknownError`,
  `CoordinatorRunCompletedError` and `AgentCoordinator(replay_model_responses=...)`: see
  "Changed (P39 G2)".
- `AgentCoordinator(hooks=...)`. Coordinator tool calls (and tools inside
  `AgentDefinition.as_tool()` subagents) now run through the tool's input and output
  guardrails and the hooks' `before_tool`/`after_tool`, and need approval when the tool has
  `requires_approval` or a hook's `tool_needs_approval` says so, as well as for the agent's
  `approval_required`. Before G2 they ran with no guardrails, hooks or tool approval gate.
- `AgentMCPServer` plain tools run the hooks of the server's runner (`before_tool`,
  `after_tool`, `tool_needs_approval`; a hook-gated call is refused over MCP).
- `policy.tool_call_needs_approval` and `policy.run_tool_call`: the runner's approval rule
  and guardrail/hook order for code that runs a tool outside a `Runner`.
- Origins: handoff transfer tools `origin="handoff"` (tag `handoff:<target>`); the
  runtime's knowledge-search tools `origin="knowledge:<collection>"` (tags
  `knowledge:<collection>`); `AgentDefinition.as_tool()` `origin="agent:<name>"`.

### Changed (P39 G2)
- **Hook approval gates survive a crash (G1 review MIN7).** A call a hook's
  `tool_needs_approval` gated is recorded in the run's journal (`hook_gates`, written with
  the commit that pauses the run). A resume or recovery by a runner without that hook
  still treats it as gated: an undecided call of a recovered batch, a `not_applied`
  outcome and a reconcile `NotApplied` all wait for a fresh approval instead of being
  sent. If the paused checkpoint was saved but its journal commit was lost, recovery
  records every call the pause asks about, and a resume's approval claim commit records
  them too, so an expired claim after a lost pause commit is asked for again instead of
  being used (G2 review M1). Needs a journal-capable checkpoint store; runs paused before
  this release carry no record (keep the gating hook on runners that continue them).
  `tests/agents/test_p39_g1_approval_hook.py::test_the_same_recovery_without_the_hook_gate_sends_the_call`
  now also clears the recorded gate (a pre-G2 journal) to stay a control.
- Hooks objects passed to a runner now also run for runs nested inside its runs (for
  example `Agent.as_tool()` sub-agents): `on_run_start` and the tool hooks are called for
  the sub-run too, so keep per-run state keyed by `ctx.run_id`. A plain
  `threading.Thread` does not inherit them (use `contextvars.copy_context().run`). A
  hooks object that also sits inside a `ComposedHooks` subclass runs once.
- Hooks now get copies of the tool call in `before_tool` and `after_tool` (each
  `ComposedHooks` member its own): changing `call.arguments` no longer changes the
  approved arguments the tool runs with.
- **Coordinator re-runs after an approval never repeat earlier calls.** G2 made coordinator
  tools with `requires_approval` or a hook gate pause (they used to run unasked). That
  exposed the coordinator's "re-run after approval" model, so with an `approval_gate`
  every run visit now has a ledger (`coordinator_call_ledger` in the approvals database,
  or in memory for one coordinator object without a database). A visit is workspace,
  run, node and workflow step, so a node revisited in a cycle starts fresh.
  - A call that ran is replayed by position; a run with several gated calls completes,
    each approval claimed once.
  - A dispatched call without a result is sent again only for `pure`/`idempotent` tools.
  - A diverging re-run (another call at a position that ran, or an ungated non-idempotent
    call an earlier attempt already sent) raises `CoordinatorCallOutcomeUnknownError`
    before sending.
  - `AgentCoordinator(replay_model_responses=True)` replays the recorded model responses
    instead of calling the model again.
  - Replays emit `coordinator.*_replayed` progress events and a
    `nexura.coordinator.replayed` trace event.
  - A completed visit keeps only a marker: running it again raises
    `ApprovalClaimConflictError` (approvals used) or `CoordinatorRunCompletedError`.
  - `purge_coordinator_ledger(manager, older_than_seconds)` removes old rows.
  - The approval request id now includes the workflow step and the agent definition
    digest keys the ledger, so pending coordinator approvals made before this change
    are requested again.
- A `tool_to_module` module whose tool has `requires_approval` (flag or rule) runs a gated
  call only with `ExecutionContext.approval_granted`. As a workflow node it pauses on the
  approval path (`request_approval`, details `{"tool_requires_approval", "arguments"}`) and
  runs once a person approves those arguments (compared by canonical digest); a node that
  is itself approved covers it; elsewhere the call raises `ContractError`
  (`APPROVAL_REQUIRED`). `module_tool` grants the approval when the wrapped tool's gate
  paused the call, and the rule sees the agent run's context (its `deps`), so agent use
  is unchanged. As a workflow node a rule sees `deps=None`.

### Added (P39 G1: hook composition and guardrail plumbing in `nexura-agents`)
- `Runner(hooks=[a, b])` accepts a list, composed by the new `ComposedHooks`: observers in
  list order (`on_agent_end`/`on_run_end` in reverse), `before_model`/`before_tool` take the
  first non-None answer, `after_model`/`after_tool` chain in reverse. A single hooks object
  is used as before. `runner.hooks` is now a property that composes a list on assignment.
- `AgentHooks.tool_needs_approval(ctx, tool, arguments)` (sync) adds approval gates through
  the existing approval contract, at every place the runner decides approval (pause,
  not-applied resend, recovery claims, reconcile). It can never remove a tool's own gate.
- Tool metadata: `Tool.origin`, `Tool.tags`, `@tool(tags=...)`, `Tool.info()` returning a
  frozen `ToolInfo`, and `RunContext.tool` inside tool calls. MCP tools get
  `mcp:<server>:<tool>` and `toolset:<server>`; the server's hints become
  `mcp.hint:<hint>=<bool>` tags (metadata only). OpenAPI tools get `openapi:<METHOD>:<path>`,
  `toolset:<toolset>` (new `openapi_tools(toolset=...)`), `openapi.method:<METHOD>` and, when
  the spec claims it, `openapi.hint:idempotent=true` (metadata only).
- `openapi_tools(read_only=[...], idempotent=[...])`: the operator declares, by operationId,
  which operations are `pure` or `idempotent` for durable recovery (mirrors
  `MCPServer(read_only=...)`). Undeclared operations stay `non_idempotent`.
- `RunEventType.GUARDRAIL_DECISION` run events and `nexura.guardrail.decision` trace events
  (redacted) for every guardrail outcome other than a pass; `check_tool_input` /
  `check_tool_output` take an optional `on_decision` callback.

### Changed (P39 G1)
- `module_tool` keeps a module's explicit, operator- or developer-declared effect policy, so
  MCP tools listed in `MCPServer(read_only=...)` and modules whose manifest sets
  `effect_policy="pure"`/`"read"`/`"idempotent_write"` are repeated by durable recovery
  instead of pausing. Third-party claims and wrapper defaults never set the class: unmarked
  MCP tools (whatever their `readOnlyHint`), OpenAPI operations (whatever their HTTP method
  or `x-idempotent`) and modules without an explicit `effect_policy` stay `non_idempotent`,
  and a module that wraps a `Tool` (`tool_to_module`) runs under the stricter of its
  manifest class and the tool's own class. `module_tool(effect=...)` overrides the class.
- Generic wrappers no longer default to `idempotent_write`: `tool_to_module` / `ToolModule`
  take the wrapped tool's declared class (an undeclared `@tool` is `non_idempotent_write`),
  and `LegacyModuleAdapter` (`register_legacy`), `AgentCoordinatorModule` and
  `AgentDefinition` / `AgentDefinition.from_agent` default to `non_idempotent_write`. A durable
  workflow therefore pauses (`effect_unknown`) instead of repeating such a node after a crash,
  a workflow node that retries one of them is refused at validation, and the default
  `AgentDefinition.definition_digest()` changes, so a pending coordinator approval created
  before the upgrade must be requested again. Pass `effect_policy=` to declare a class.
- Upgrading with runs in flight: `Agent.definition_digest()` is unchanged for agents with
  OpenAPI tools, unmarked MCP tools, `tool_to_module` tools and modules without an explicit
  policy. It changes for agents with `MCPServer(read_only=...)` tools or `module_tool` modules
  whose manifest explicitly sets `effect_policy` to `pure`, `read` or `idempotent_write`
  (Studio saved agents using such runtime modules included). With a journal-capable
  checkpoint store, recovery of such a run keeps the stricter class its call was sent under
  (it pauses) and warns that the definition changed. Without a journal there is no recorded
  class or digest: the first recovery after upgrading applies the new class silently, so a
  call that crashed in flight is sent again.
- Agent input and output guardrails are bounded by the run's `timeout` and stop when the
  run is cancelled; an answer whose output validators or output guardrails were interrupted
  by a cancel is not returned in `result.messages`.
- Usage recorded with `ctx.add_usage` in agent input/output guardrails (judge calls) and
  output validators now counts in `RunResult.usage`, also when the stage trips, times out or
  is cancelled, and the `UsageLimits` token caps are checked again after each guardrail stage
  that recorded usage (an output judge that crosses a cap fails the run with
  `UsageLimitExceededError`). It counts in the total only, not in `usage_by_agent` or
  `usage_by_model`, which cover the agents' own model calls (as for nested tool usage).
- `ComposedHooks` flattens only plain nested compositions (a subclass keeps its overrides)
  and refuses members with no hook methods. Approval hooks get copies of the tool and its
  arguments. Only the four MCP specification hint keys become `mcp.hint:` tags.
  `GUARDRAIL_DECISION` records copy only identifier tokens (no `@`, runs of 6+ digits, 9+
  digits joined by `-`, `.` or spaces, or key-shaped strings) and a fixed `decision`
  vocabulary. `Runner(hooks=...)` refuses a set, dict, generator, class or other non-hooks
  value when the runner is built.
- `module_tool` over a module that wraps a `Tool` (`tool_to_module`) now also applies that
  tool's own `requires_approval` flag or rule (before, `module_tool(tool_to_module(t))`
  dropped `t`'s gate and the first call ran without approval). Any gate on either side
  gates the call.
- The `nexura.usage.budget` module (`UsageBudgetModule`) now declares `non_idempotent_write`
  (was `idempotent_write`): `reserve` and `record_usage` are not idempotent, so a durable run
  pauses instead of repeating them after a crash.

### Added (P39 G3: the `nexura-guardrails` package)
- New workspace package `nexura-guardrails` (import `nexura_guardrails`, MIT, Python 3.11 to
  3.13), depending on `nexura-agents` only. Extras: `judge` (empty marker) and `yaml` (PyYAML).
- `Verdict` (allow, block, rewrite, review, error), `CheckInput`, the `Check` protocol, and
  `as_guardrail` / `as_tool_guardrail`, which turn any check into a plain guardrail for the
  existing `Agent(input_guardrails=..., output_guardrails=...)` and `@tool(...)` parameters.
- A category taxonomy with classifier label maps (Llama Guard, ShieldGemma, Granite Guardian,
  Nemotron Safety Guard v3, OpenAI moderation). Indian-context categories were removed before release (not needed for 0.4).
- Pre-filter checks `pii`, `injection`, `length`, `topics`, `json_only` that call the
  `nexura_agents` built-ins (one implementation).
- `JudgeCheck`: an LLM judge with the policy in the system message, the content in a per-call
  nonce fence, a strictly validated JSON verdict, fail-closed handling of timeouts, provider
  errors, budget refusals and malformed verdicts, calls through Nexura's model layer (run
  budgets apply, usage is counted) and a per-run verdict cache keyed by a content hash.
  The fully built prompt must fit `context_tokens` (default 4096) by a conservative token
  estimate; otherwise nothing is sent and the verdict is a hard `ContentTooLong` error that
  no `on_error` setting softens. Tool rejections the agent reads are fixed text with category
  ids only, never the judge's free-text reason.
- `tiered`, `parallel` and `cached` composition. New guide: docs/guardrails-package.md.
- Offline guardrails benchmark harness (`nexura_guardrails.bench`) for JSONL datasets, measuring recall, false-positive rate, precision, F1, p50/p95 latency and error counts across pre-filters and `JudgeCheck` with SHA-256 hashes and license tracking.

### Changed (P39 G0: guardrail docs honesty)
- The stdlib guardrails are documented as fast heuristic pre-filters, with the measured misses
  (docs/guardrails-hooks-limits.md, section renamed "Built-in pre-filters (heuristic)", and the
  `guardrails_builtin` docstrings). No behaviour or import change.
- Removed the unreachable duplicate `return None` lines in `AgentHooks`.
- New strict-xfail tests record the known pre-filter misses, so an improvement shows up.

### Changed (P38 C2: OpenTelemetry GenAI semantic conventions)
- Tracing follows the GenAI conventions pinned to `open-telemetry/semantic-conventions-genai`
  commit `8538971bd11dba723fdd33c804b5a32121827ff7` (Development status; see
  docs/observability.md). Model-call spans are `CLIENT` and carry `gen_ai.provider.name`,
  `gen_ai.response.id`, `gen_ai.response.model`, and the cache-read, cache-write and
  reasoning token attributes; workflow spans are `invoke_workflow <name>` with
  `gen_ai.workflow.name`; `error.type` is set on failed operations.
- With `capture_content=True`, `gen_ai.system_instructions`, `gen_ai.input.messages`,
  `gen_ai.output.messages`, `gen_ai.tool.call.arguments` and `gen_ai.tool.call.result` are
  recorded in the conventions' structured format (redacted, truncated). Still off by default.
- New metrics: `gen_ai.client.inference.duration`, `gen_ai.invoke_agent.duration`,
  `gen_ai.execute_tool.duration`, `gen_ai.invoke_workflow.duration`, the
  `gen_ai.client.inference.usage.*` token counters and `gen_ai.client.inference.operation.*`
  token histograms.
- New `nexura.approval.requested` / `.decided`, `nexura.budget.reserve` / `.settle` /
  `.refuse` and `nexura.effect.intent` / `.outcome` / `.unknown` / `.resolved` span events.
- `ModelResponse` gains optional `response_id` and `response_model`, filled by the OpenAI,
  Anthropic, Gemini, Sarvam and LiteLLM adapters for non-streamed calls.
- Deprecated (removed in 0.5.0): `gen_ai.system`, `nexura.usage.reasoning_tokens`,
  `nexura.operation.name`, the `gen_ai.content.prompt` / `gen_ai.content.completion` events,
  the `gen_ai.client.token.usage` metric, and `gen_ai.client.operation.duration` for model
  calls (that histogram keeps `gen_ai.operation.name=workflow` for workflows until then), the
  `gen_ai.tool.call.arguments` / `gen_ai.tool.call.result` span events, and
  `nexura.response.finish_reasons` (the raw `tool_calls` value; `gen_ai.response.finish_reasons`
  now reports `tool_call`, and `error` for a failed model call). The workflow span name changed
  from `workflow <name>` to `invoke_workflow <name>`.
- Recording content never fails or changes a run (a tool result is parsed as JSON only up to
  64,000 characters and any parse or conversion error is contained); `gen_ai.input.messages`
  keeps the newest messages; attachment URLs lose their query string; history capture is
  linear in the history size. `gen_ai.provider.name` follows an OpenAI-compatible adapter's
  base URL (unknown hosts get no value) and is `gcp.vertex_ai` for `nexura-vertex`.
- The V2 trace exporter forwards the cache and reasoning token breakdowns without adding them
  to `total_tokens` or to usage completeness.

### Added (P37 B5: project scaffolding)
- `nexura-runtime init DIR --template agent|workflow|rag` (and `--list`) writes a small runnable
  project with offline FakeModel tests, a README, `.gitignore` and `.env.example`; it refuses
  a non-empty directory without `--force` and never overwrites a file.
- `nexura-runtime dev` serves the REST service and the bundled Studio on 127.0.0.1 with the
  project's handlers and workflows from `[tool.nexura]` registered.
- `nexura-runtime deploy --target docker` writes a single-user Dockerfile, `.dockerignore` and
  DEPLOY.md with the loopback and token caveats; it builds and runs nothing.
- `nexura.commands` entry points `dev` and `project` (`nexura project init|dev|deploy`; `init`
  and `deploy` are the Nexura Cloud client's built-ins). See docs/project-templates.md.

### Changed (P35 A6 S11: budgets and approvals across a takeover)

- Budget reservations record the run step they were made for
  (`metadata["execution"]`, `metadata["step_key"]`; `nexura_agents.usage.linkage`).
  `Runner`, `WorkflowEngine` and `DurableExecutionEngine` open a dispatch scope around
  model calls, tool calls, nodes, branches and items. New
  `UsageLedger.open_reservations(workspace_id, run_id)`.
- Before any step runs, a takeover ends the run's open reservations. That covers
  `Runner.recover`, `WorkflowEngine.recover`/`resume`/`resume_interrupt`,
  `DurableExecutionEngine.claim_run` and trigger continuation. A dispatched (or possibly
  dispatched) step is settled as unknown usage at its estimate; a step whose intent the
  journal does not have is released. A run that reaches a terminal status ends its open
  reservations the same way. The rule is
  `nexura_agents.usage.recovery.settle_run_reservations`.
- A run started under a `RunBudget` records its scope in its journal.
  `WorkflowEngine` restores the policy with `manager.get_run_budget(scope)`. Continuing
  such a run without a budget raises `UsageError` (SDK) or `WorkflowError` (workflows).
  The new `DurableExecutionEngine(..., budget=)` argument links that engine's
  reservations.
- SDK approvals: with a journal-capable store, `resume` records its claim (decisions,
  expiry) in the run's journal before it consumes the approval. Approved calls' intents
  carry `approval_id`, recorded in `consumed_approvals` in the same commit. `recover`
  dispatches a claimed, unused approval once under the same approval, or asks again once
  it has expired. Before this, a crash after consumption left the run paused on a spent
  approval. A second `resume` of a claimed approval is refused.
- `resume` releases a dead same-host owner's lease. A paused agent run gives back its
  checkpoint claim with the pause commit.
- Review fixes:
  - Recovery never sends a call a person rejected. The claim's rejections and answers
    hold, and a call to a tool that requires approval with no decision on record is asked
    for again. This includes the other calls of an `effect_unknown` batch.
  - A resume that cannot take the run consumes nothing. A spent or claimed approval is
    refused before any journal write.
  - Workflows continue under the recorded budget scope: another scope is replaced with a
    warning, or refused when its ledger does not know the recorded scope. `resume` and
    `resume_interrupt` check the budget before claiming.
  - `DurableExecutionEngine.claim_run` fails closed on a run started under a budget.
  - Runner reservations outside model and tool calls (history processors, instructions,
    guardrails) are linked to the run (`step_key="run"`).
  - Terminal sweeps run before the terminal commit.
  - Claims record their turn and must be exact JSON.
- Round-2 review fixes:
  - Terminal sweeps are fenced. Each reservation records its owner's run-lease token. A
    sweep first confirms the lease (`renew`), then ends only reservations of its own
    lease or older, so a stale owner never ends the new owner's in-flight reservation.
  - Combined budgets are looked through. Workflows enforce the recorded scope's policy
    alongside, or warn; the Runner warns.
  - The Runner's no-budget refusal reads the journal before taking the lease.
  - (Superseded in round 3.) A non-replayable claim marker for decisions that are not
    exact JSON.
- Round-3 review fixes:
  - One approval path: decisions are normalised to JSON (tuples become lists). A
    journaled resume with decisions that have no exact JSON form raises
    `ApprovalDecisionError` before taking the run or consuming anything. The claim marker
    is removed.
  - An approval whose pending calls already have an intent for that turn is refused.
  - Recovery consumes an expired claimed approval (best effort) before asking again.
  - Terminal sweeps also end unlinked reservations of the run's own scope made before the
    lease was confirmed.
  - A transient store error while confirming the lease is a warning, not a failed run.
    The confirmation is retried once. If it still fails, the token-filtered sweep runs
    after the fenced terminal commit succeeds (round 4, F4-m1); otherwise recovery
    settles.
  - Documented: the shared workspace counts a recovered call once per scope under dual
    enforcement.

### Changed (P35 A6 S10/S10b: trigger jobs on the execution journal)
- A reclaimed trigger job continues the run it owns (`WorkflowEngine.recover`) instead of
  starting a new run: a completed run is never run again, and a step whose outcome is unknown
  follows its effect class (an undeclared one pauses the run `effect_unknown`). The job lease
  only coordinates the queue; the run lease decides who executes the run.
- Deprecated (removed in 0.6): `TriggerWorker(retry_mode="new_run")` /
  `nexura-runtime worker --retry-mode new_run` (the old behaviour) and
  `DurableTriggerJob.previous_run_ids`. `TriggerStore.reclaim_expired_jobs` gains
  `keep_run=True`.
- Added `nexura_postgres.PostgresTriggerStore` (webhooks, receipts, schedules and the job queue
  on Postgres, leases on the database clock); the service uses it when `workflow_state_url`
  names Postgres.
- Fixed: concurrent deliveries of the same webhook event to two processes on one SQLite file
  could fail with a unique-constraint error instead of being de-duplicated.

### Fixed
- Reserve workflow run IDs atomically before dispatch, preventing concurrent starts
  from executing the same run twice. Custom checkpoint stores must provide atomic
  `create(checkpoint) -> bool` to start runs; built-in stores preserve step history indices.
- Execute every durable conditional-loop visit and charge its fuel/time budget;
  committed cursor advancement and pre-dispatch intent still govern recovery.
- Retain node-requested approval details across interrupts for the same unfinished
  node and unchanged approved inputs.
- Keep each `:memory:` / `sqlite://:memory:` storage repository alive until close,
  with independent databases for separate repository instances.
- Persist invalid runtime branch failures before emitting node completion.
- Align the Python client's default server port with the CLI default, 8765.
- Support TypeScript subrun pagination options and response metadata, and typed
  per-run `budget_config` in `runs.start()`.

### Security (P28 step 7 security review, findings R1-R5)
- **Per-session local token (R1, founder decision 2026-10-18 #8).** `nexura-runtime serve`,
  `nexura-studio` and every `create_api_app()` / Studio `create_app()` generate
  `secrets.token_urlsafe(32)` per start (or use `NEXURA_STUDIO_TOKEN`, or the new
  `create_api_app(session_token=)`), kept on `app.state.session_token`. In the local-owner
  profile every request other than GET/HEAD/OPTIONS must send it in `X-Nexura-Session`
  (constant-time compare; 403 `SESSION_TOKEN_REQUIRED` / `SESSION_TOKEN_INVALID`). Studio reads
  it from `GET /api/v1/local-session` on its own origin (refused for `Sec-Fetch-Site`
  cross-site/same-site or a foreign `Origin`); the built-in Studio page also gets it in a meta
  tag, or from `#token=` when `NEXURA_STUDIO_TOKEN` is set. The TypeScript client
  (`sessionToken`, `autoSessionToken`, `fetchSessionToken()`), Studio web and the Python
  `NexuraClient` (`session_token`, `auto_session_token`, `local_session.token()`) send it and
  re-read it once after a token refusal. State-changing REST requests with `text/plain`, form
  or multipart bodies are now 415 (raw Python to `/workflows/import/python` from a non-browser
  client excepted). Signed webhook deliveries and extension `public_paths` take no token.
- **Loopback peers only (R2).** Without the insecure opt-in, the local-owner service and a
  token-less Studio answer 403 `NON_LOOPBACK_CLIENT` to any peer that is not loopback, so an
  app embedded with `uvicorn.run(app, host="0.0.0.0")` no longer serves the LAN. The CLI bind
  guard is unchanged.
- **Strict Host allow-list (R3).** Local mode accepts only `localhost`, `127.0.0.1`, `[::1]`
  (any port), the bound host and configured names; dotless names and `testserver` are refused
  as Host and `http://testserver` as Origin (service and Studio). Tests reach the apps as
  `http://127.0.0.1` through a central helper (`tests/_local_client_helper.py`).
- **Shared-profile principals fail closed (R4).** Under `authenticated-shared-service` a
  principal is always treated as `authenticated-shared-service`, even when a provider leaves
  `Principal.profile` at its `local-owner` default, so it never gets local-owner behaviour.
- **The single-user Docker image starts (R5).** It listens on 0.0.0.0 inside the container
  only with `NEXURA_STUDIO_TOKEN` (compose requires it), port 8765, published on
  `127.0.0.1` (`docker run -p 127.0.0.1:8765:8765 -e NEXURA_STUDIO_TOKEN ...`); the health check
  uses the public `/healthz`.

### Changed
- **Renamed the runtime package (P28 step 10):** the import package `nexura_agentkit` is now
  `nexura_runtime`, and the distribution `nexura-agentkit` is now `nexura-runtime`
  (`pip install nexura-runtime`, `import nexura_runtime`). The command was already
  `nexura-runtime`. Nothing was published under the old names, so no compatibility shim is
  kept. `nexura-agents` (the core SDK) is unchanged.
- **Renamed the TypeScript client (P28 step 10):** the npm package `@nexura/sdk` is now
  `@liceron/nexura-sdk` (the bare `nexura` npm name belongs to another project). Studio and
  the docs use the new name.
- **The 15 SaaS connectors are their own package (P28 step 10, stage 4a):**
  `nexura_runtime.integrations.connectors` moved to `nexura-connectors` (import
  `nexura_connectors`, workspace member `packages/nexura-connectors`; it depends on
  `nexura-runtime==0.4.0`, optional extra `postgres` for asyncpg). Behaviour and module ids
  (`nexura.connectors.*`, version 1.0.0) are unchanged; each manifest's `source_reference`
  now names the new path, so the connector manifest digests change. When the package is
  installed the runtime imports it and every connector registers as before. `nexura-runtime`
  without it imports and runs: the 15 ids stay listed as unavailable and using one raises
  `MissingDependencyError` (validation: `MODULE_UNAVAILABLE`) with "Install it with: pip
  install nexura-connectors" (`VersionedModuleRegistry.register_unavailable`,
  `nexura_runtime.modules.optional_packages`). The connector tests moved to `tests/connectors/`.
  The root dev dependency group installs the package, so `uv sync` still gives the full
  suite; CI and the release gate check the runtime wheel alone and then with
  `nexura-connectors` (new required gate step `connectors-standalone-consumer`). Nothing was
  published under the old import path, so no compatibility shim is kept.
- **Five more packages split out of `nexura-runtime` (P28 step 10, stage 4b).** Each is a
  workspace member under `packages/` at version 0.4.0, with its own README, MIT `LICENSE` and
  tests; the root dev dependency group installs all of them, so `uv sync` still gives the full
  suite. Nothing was published under the old paths, so no compatibility shims are kept.
  - `nexura-postgres` (`nexura_postgres`): `PostgresRepository` (from
    `nexura_runtime.storage`) and `PgVectorStoreAdapter` (from
    `nexura_runtime.knowledge.vector.adapters.pgvector`); depends on asyncpg.
    `get_storage_repository("postgresql://...")` returns it when installed and otherwise raises
    `MissingDependencyError` "PostgreSQL storage is provided by the 'nexura-postgres' package,
    which is not installed. Install it with: pip install nexura-postgres". The runtime's
    `postgres` extra is removed; its `sqlalchemy` requirement was never imported and is dropped.
    `PostgresRepository` is no longer exported by `nexura_runtime` / `nexura_runtime.storage`.
  - `nexura-qdrant` (`nexura_qdrant`): `QdrantVectorStoreAdapter`; depends on qdrant-client.
    The runtime's `vector` extra is removed.
  - `nexura-sandbox` (`nexura_sandbox`): the WASI, bubblewrap and gVisor backends
    (`WasiWorker`, `BubblewrapWorker`, `GVisorOCIWorker` and their modules, moved unchanged;
    the gVisor code was split out of `worker.py` into `gvisor_worker.py`). Optional extra `wasi`
    (wasmtime 49.0.0) replaces the runtime's `sandbox-wasi` extra. Monty, the backend
    selection, `SandboxConfig`, the sandbox module and the escape suites stay in the runtime.
    `build_backend` loads the moved backends when the package is installed; without it they are
    `PackageNotInstalledBackend` placeholders with the same name and isolation level that probe
    unavailable ("pip install nexura-sandbox"), so selection and the fail-closed default
    behave as on a host where those backends are missing. `nexura-runtime sandbox
    install-runtime wasi` names the package when it is absent. The three classes are no longer
    exported by `nexura_runtime.integrations.sandbox`.
  - `nexura-vertex` (`nexura_vertex`): `VertexAIProvider`, moved from `nexura-agents`
    (`nexura_agents.providers.vertex`); it depends on `nexura-agents` and google-auth, not on
    the runtime, and supports Python 3.11 to 3.13. `"vertex:..."` model strings build it when
    installed and otherwise raise `SDKValidationError` naming `pip install nexura-vertex`. The
    `vertex` extras of `nexura-agents` and `nexura-runtime` are removed, and `VertexAIProvider`
    is no longer exported by `nexura_agents`. `block_model_requests()` still blocks it.
  - `nexura-studio` (`nexura_studio`): the `nexura-studio` command (the console script moved
    from `nexura-runtime`), the Studio FastAPI app (`create_app`), its workflow/run/module
    routes and the static web page with the bundled Noto font (licence `MIT AND OFL-1.1`).
    The run store, built-in modules, traces, trace routes and security middleware stay in
    `nexura_runtime.studio`, because the REST service uses them. The service keeps the
    runtime's `studio` extra (FastAPI, uvicorn): `pip install "nexura-runtime[studio]"` is
    still the install for `nexura-runtime serve`. `nexura_runtime.studio.create_app` builds the
    app from the package, or raises `MissingDependencyError` naming `pip install
    nexura-studio`. The React `studio-web` app is not served by Python and did not move.
  - Tests moved with only import paths changed: `tests/postgres/` (from `tests/backend/b32`),
    `tests/vertex/` (from `tests/providers`), `tests/studio/` (the four `tests/test_studio*.py`);
    mixed files stayed and changed imports. New tests per package check the runtime without
    the package (simulated), with it, a broken install, and the import boundary.
  - The release gate has five new required steps (`postgres-`, `qdrant-`, `sandbox-`,
    `studio-` and `vertex-standalone-consumer`): each installs the base wheel alone in a fresh
    venv, checks the feature names its package, installs the package wheel, checks it works and
    runs the package README. mypy covers every package; CI installs the runtime wheel without,
    then with, the five packages.

### Removed (moved to the closed edition)
- **Client methods for closed-edition routes moved to the closed edition (audit finding PKG-03,
  founder decision 2026-10-20).** The open-source Python and TypeScript clients still carried
  methods for routes that only the closed edition serves (organisations with their members and
  invites, publishing with environments and promotions, the hosted secrets admin API, and in
  Python workspace members); against the open-source service each call answered 404. They now
  live in the closed edition as client add-ons, with unchanged method names, arguments and
  requests. Open-source Studio no longer calls the member routes: without a registered
  `membership` source (a new optional field of `StudioExtension`; `registerStudioExtensions`
  installs it) it is the local owner (admin) and hides the Members tab, exactly as it did on a
  404. `NexuraClient.with_workspace` now returns the caller's class, so subclasses keep their
  subclients. The release gate's edition-boundary step now also fails when an open-source wheel
  or sdist member, the npm tarball of `@liceron/nexura-sdk` or the built Studio bundle contains
  a closed-edition client route string (`/organisations`, `/published`, `/secrets`, `/members`,
  `/my-membership`); the step runs after the TypeScript and Studio builds and packs the client.

  | Was (open source) | Now (closed edition) |
  |---|---|
  | Python `NexuraClient(...).members`, `.secrets`, `.organisations`, `.published` | `nexura_enterprise.clients.EnterpriseClient(...)` (a `NexuraClient` subclass), same attributes |
  | `nexura_runtime.service.api.client.WorkspaceMembersClient`, `SecretsClient`, `OrganisationsClient`, `PublishedClient` | `nexura_enterprise.clients` (same class names) |
  | TypeScript `client.secrets`, `client.organisations`, `client.published`; exports `SecretsClient`, `OrganisationsClient`, `PublishedClient` | `@liceron/nexura-sdk-enterprise` (`enterprise/clients/typescript`): the same classes over any `NexuraClient`, or `createEnterpriseClients(client)` |
  | TypeScript types `SecretMetadataItem`, `CreateSecretOptions`, `OrgMemberItem`, `OrgInviteItem`, `OrganisationDetail`, `OrganisationListItem`, `CreateInviteOptions`, `PublicationRecord`, `CreatePublicationOptions`, `UpdatePublicationOptions`, `PromotionRecord`, `MapChangeRecord` | `@liceron/nexura-sdk-enterprise` |
  | Studio member calls in `studio-web/src/api/membership.ts` | the enterprise Studio's `nexura.membership` extension (a `membership` source over `WorkspaceMembersClient`) |
  | Client coverage tests for those methods (Python 4 tests, TypeScript 1 whole test and 2 parts) | `enterprise/tests/clients/test_p29_client_coverage.py`, `enterprise/clients/typescript/tests/hosted.test.mjs` |
- **Authentication and workspace membership moved out of the open-source edition (P28 step 7,
  first half: backend).** The open-source edition now has one principal, the local owner
  (`local-owner`, role `admin`), and no logins, tokens, API keys, workspace members or invites.
  `X-Principal-ID` / `X-Principal-Role` headers are no longer trusted: they cannot change who
  the caller is or its role (the old behaviour survives only as a closed-edition test helper).
  JWT validation, API keys and resource-scoped keys, the shared profile's principal resolution
  and operator allow-list, the shared-service `AuthProvider`, the member routes, the membership
  store and its audit trail, the `nexura.service.auth` / `nexura.service.workspaces` modules,
  the TypeScript `WorkspaceMembersClient` and `examples/backend/b24.py` now live in the closed
  edition (`nexura-enterprise`, packages `nexura_enterprise.auth` and
  `nexura_enterprise.membership`). No compatibility shim is left behind.

  Stays open source (plan section 3, "safety is not a paid feature"): loopback by default, the
  bind guard (unchanged; its message now points at the enterprise edition), Host/Origin checks,
  request limits, approvals, the SSRF guard, MCP stdio rules, redaction, budgets, the sandbox,
  several local workspaces (question 2), `Principal`/`WorkspaceRole`/`check_permission`,
  workspace ownership, `require_resource` and the `AuthProvider`/`ServiceExtension` seams.
  `profile="authenticated-shared-service"` without an authenticating `auth_provider` now fails
  with `AuthProviderRequiredError` naming the enterprise edition. The invite-accept exemption
  became a generic seam: an `AuthProvider` may list `routes_without_membership`
  (`invalid_identity_reason(..., without_membership=)`). `GET /api/v1/workspaces/{id}/runs` no
  longer answers 500 (a `Query` default reached the store).

  | Was | Now (closed edition) |
  |---|---|
  | `nexura_agentkit.service.auth.tokens` (`TokenValidator`, `create_token`) | `nexura_enterprise.auth.tokens` (also `nexura_enterprise.auth`) |
  | `nexura_agentkit.service.auth.api_keys` (`ApiKeyManager`) | `nexura_enterprise.auth.api_keys` |
  | `ApiKey`, `TokenPayload`, `MembershipAuditEvent` in `...service.auth.models` | `nexura_enterprise.auth.models` |
  | `RESOURCE_SCOPED_ROUTES`, `route_allows_resource_scoped`, `validate_resources` | `nexura_enterprise.auth.resources` (`require_resource` stays open source) |
  | `ProfileManager(token_validator=, api_key_manager=, operator_principal_ids=)`, `resolve_principal` | `nexura_enterprise.auth.SharedProfileManager` (the open-source `ProfileManager` keeps the profile switch) |
  | `middleware.authenticate_shared`, `extract_credentials`, `INVITE_ACCEPT_ROUTE` | `nexura_enterprise.auth.shared` |
  | `auth_providers.SharedServiceAuthProvider`, `ProfileAuthProvider`; header trust in `LocalOwnerAuthProvider` | `nexura_enterprise.auth.providers`; header trust removed (`LocalOwnerAuthProvider` stays, always the local owner) |
  | `create_api_app(token_validator=, api_key_manager=, operator_principal_ids=)` | `nexura_enterprise.app.create_enterprise_app(...)`, same arguments |
  | `run_revocation.api_key_revocation_listener` | `nexura_enterprise.auth.revocation` (wired by `create_enterprise_app`) |
  | `nexura_agentkit.service.module` (`AuthServiceModule`, `WorkspaceServiceModule`) | `nexura_enterprise.auth.module` (registered on import of `nexura_enterprise.auth`) |
  | `WorkspaceRepository` members and audit (`add_member`, `update_member_role`, `remove_member`, `list_members`, `get_audit_events`, `record_runs_cancelled`) | `nexura_enterprise.membership.MembershipRepository` (same tables); the open-source repository answers for the local owner only |
  | `GET /api/v1/workspaces/{id}/my-membership`, `GET/POST .../members`, `PUT/DELETE .../members/{principal_id}` | `nexura_enterprise.membership.MembershipExtension` (`nexura.membership`) |
  | TypeScript `client.members` (`WorkspaceMembersClient`, `MyMembershipInfo`, `WorkspaceMemberItem`) | `enterprise/clients/typescript` (`@nexura/sdk-enterprise`); `client.workspaces.updatePolicy` stays |
  | `examples/backend/b24.py`; "Resource-scoped API keys" in `docs/studio-and-service.md` | `enterprise/examples/b24.py`; `enterprise/docs/authentication.md` |
- **Nexura V2 compatibility server moved out of the open-source edition (P28 step 6).** The
  V2-shaped run API (`V2FlowServer`: `/api/v1/run/{flow_id}`, `/api/v1/webhook/{flow_id}`,
  `/api/v1/responses`, including the V7 per-caller keys `PerCaller`), the V2 SSE event shapes
  (`SSEEvent`, `V2EventMapper`), Firebase ID-token verification, V2 RBAC and policy bundles,
  V2-shaped run records, the shadow-run harness (`python -m ...nexura_v2.shadow`) and
  `docs/migrating-from-langflow.md` now live in the closed edition (`nexura-enterprise`,
  package `nexura_enterprise.v2`). No compatibility shim is left behind.

  Stays open source (founder decision 2026-10-18, question 5): the Langflow importer (`load`,
  `build`, `run_flow`, tweaks, extended components), the export back to Langflow JSON
  (`export_flow`) and the trace exporter to the hosted product (`NexuraV2TraceExporter`,
  `examples/sdk_integration/p01.py`). The engine now reports a run through a new
  `FlowEventMapper` protocol: `BuiltFlow.stream_events(...)` and `stream_flow_events(...)` take
  a required `mapper=` (they used to default to the V2 mapper); `run`/`run_async`/`run_flow`
  are unchanged. The V2 sections of `docs/langflow-import.md` moved with the server.

  | Was | Now (closed edition) |
  |---|---|
  | `nexura_agentkit.integrations.langflow.server` (`V2FlowServer`, `PerCaller`, `FlowDocument`, `as_flow_source`) | `nexura_enterprise.v2.server` |
  | `nexura_agentkit.integrations.langflow.events` and the `SSEEvent`, `V2EventMapper` exports of `...langflow` | `nexura_enterprise.v2.events` (also `nexura_enterprise.v2`) |
  | `stream_flow_events(flow, ...)` with no `mapper` | `nexura_enterprise.v2.stream_v2_events(flow, ...)`, or pass `mapper=V2EventMapper()` |
  | `nexura_agentkit.integrations.nexura_v2.{auth, policy, records, shadow}` and their re-exports from `...nexura_v2` | `nexura_enterprise.v2.{auth, policy, records, shadow}` (also `nexura_enterprise.v2`) |
  | `python -m nexura_agentkit.integrations.nexura_v2.shadow` | `python -m nexura_enterprise.v2.shadow` |
  | `docs/migrating-from-langflow.md`, V2 sections of `docs/langflow-import.md` | `enterprise/docs/migrating-from-langflow.md`, `enterprise/docs/v2-compatibility.md` |
  | `nexura_agentkit.integrations.nexura_v2.NexuraV2TraceExporter` (and `TraceMetadata`, `build_trace_envelope`) | stays open source |
  | `nexura_agentkit.integrations.langflow` importer and `export_flow` | stay open source |
- **Organisations and the hosted secrets features moved out of the open-source edition (P28
  step 5).** Organisations (`/api/v1/organisations*`: org roles, members, single-use invites,
  org-owned workspaces, the org audit log), the masked `/api/v1/secrets` admin API, master-key
  rotation (`EncryptedSecretStore.rotate_master_key`), the Studio **Organisation** page
  (`#/organisation`, `organisation.*` and `nav.organisation` strings), `organisation.spec.ts` and
  `docs/organisations-and-secrets.md` now live in the closed edition (`nexura-enterprise`), which
  adds them through `ServiceExtension` (`nexura.organisations`, `nexura.secrets`) and the Studio
  extension registry. The open-source service answers 404 on `/api/v1/organisations*` and
  `/api/v1/secrets*`. No compatibility shim is left behind.

  Stays open source (founder decision 2026-10-18): env-var credential references, the in-memory
  credential store and the **local encrypted secrets file** (`EncryptedSecretStore`,
  `StaticMasterKeyProvider`, `NEXURA_MASTER_KEY`, the shared profile's master-key requirement),
  managed through `/api/v1/credentials` and Studio's Credentials page. `create_api_app` now
  installs that store itself (`nexura_agentkit.service.api.local_secrets`), so connector
  credentials keep resolving from it and from env references with no closed edition installed.
  New guide: `docs/local-secrets.md`.

  | Was | Now (closed edition) |
  |---|---|
  | `nexura_agentkit.service.api.organisations` | `nexura_enterprise.organisations.routes` |
  | `nexura_agentkit.service.workspaces.organisations` (`OrganisationRepository`, `OrganisationError`) | `nexura_enterprise.organisations.repository` (also `nexura_enterprise.organisations`) |
  | `nexura_agentkit.service.workspaces.models.{OrgRole, ORG_ROLE_TO_WORKSPACE_ROLE, Organisation, OrgMember, OrgInvite, OrgAuditEvent}` | `nexura_enterprise.organisations.models` |
  | `builtin_extensions.OrganisationsExtension` | `nexura_enterprise.organisations.extension.OrganisationsExtension` |
  | `nexura_agentkit.secrets.api` (`/api/v1/secrets`) | `nexura_enterprise.secrets.routes`, mounted by `nexura_enterprise.secrets.extension.SecretsExtension` |
  | `EncryptedSecretStore.rotate_master_key` | `nexura_enterprise.secrets.RotatingSecretStore.rotate_master_key` |
  | `mount_organisations` secrets-store wiring | stays open source: `nexura_agentkit.service.api.local_secrets.install_local_credential_store` |
  | `nexura_agentkit.secrets.{EncryptedSecretStore, StaticMasterKeyProvider, MasterKeyProvider, master_key_provider_from_env, generate_master_key}` | stay open source |
  | Studio `features/organisation`, `organisationExtension` | `enterprise/studio-web/features/organisation` |

  `builtin_extensions()` and the Studio's `BUILTIN_STUDIO_EXTENSIONS` are now empty (the seams
  stay); `registerStudioExtensions` now refuses a registry that was already bootstrapped, which
  before followed from the in-tree Organisation extension being registered twice. The Python
  and TypeScript clients keep their `secrets` and `organisations` methods (the closed server
  serves them); their entries left `NexuraClient.COVERED_ROUTES`. The "Resource-scoped API keys"
  section moved to `docs/studio-and-service.md` (API keys move in step 7).
- **Publishing moved out of the open-source edition (P28 step 4).** The publication admin API
  (`/api/v1/published*`), the public chat gateway and widget (`/api/v1/public/...`,
  `PublicGatewayMiddleware`, `nexura-chat.js`), environments and promotion, the self-host
  launcher (`python -m nexura_agentkit.publishing.selfhost`), the Studio **Publish** tab
  (`PublishPanel`, `WidgetPreview`, embed snippet, `publish.*` strings), the widget source and
  `npm run build:widget`, `docs/publishing.md`, `docs/self-hosting.md`, and the multi-tenant
  `Dockerfile`, `docker-compose.yml` and `.env.example` now live in the closed edition
  (`nexura-enterprise`), which adds them through `ServiceExtension` and the Studio extension
  registry. The open-source service answers 404 on `/api/v1/published*` and `/api/v1/public/*`.
  No compatibility shim is left behind. The single-user local Docker setup in `deployment/`
  stays open source.

  | Was | Now (closed edition) |
  |---|---|
  | `nexura_agentkit.publishing` | `nexura_enterprise.publishing` |
  | `nexura_agentkit.service.api.publishing` | `nexura_enterprise.publishing.routes` |
  | `builtin_extensions.PublishingExtension` | `nexura_enterprise.publishing.extension.PublishingExtension` |
  | `nexura_agentkit.publishing.selfhost` | `nexura_enterprise.selfhost` |
  | `nexura_agentkit.publishing.ratelimit.SlidingWindowRateLimiter` | stays open source: `nexura_agentkit.service.api.ratelimit` |
  | Studio `features/publish/snippet.deriveEndpoint` | stays open source: `studio-web/src/api/endpoint.ts` |

  The Python and TypeScript clients keep their `published` methods (the closed server serves
  them); the `/published` entries left `NexuraClient.COVERED_ROUTES`. `scripts/gen_api_reference.py`
  no longer documents the publishing package.

### Added
- `nexura.commands` entry points for the seven runtime subcommands and Studio, ready for a Nexura Cloud client that supports plugin commands (P28 step 10 stage 5).
- **WASI sandbox backend (P31-S3):** opt-in CPython 3.13.15 with a pinned offline runtime
  installer, operator-owned serialized Wasmtime 49.0.0 module cache, disposable children,
  fuel/epoch/memory/output limits, read-only stdlib and inputs, and optional quota-checked
  scratch. Full-Python syntax and Monty unsupported-feature fallback use WASI only when
  enabled by the operator. The backend declares `interpreter` isolation and needs
  `pip install "nexura-sandbox[wasi]"` for the Wasmtime dependency.
- **gVisor OCI hardening (P31-S5):** non-root guest, exact guest NPROC and a finite host
  pids cgroup allowance for sentry/gofer threads, bounded tmpfs scratch and read-only
  payload staging. Single-process guests use mandatory OCI seccomp to deny fork/clone
  with `SCMP_ACT_ERRNO` / `EPERM` (guest code can catch the denial). The generated actions
  match runsc's explicit supported-action list; unsupported `KILL_PROCESS` was removed
  after the lead's host run rejected it. Runtime/rootfs paths must be explicit and absolute;
  sudo prefixes and cgroup
  opt-out require the existing `NEXURA_REAL_GVISOR=1` test profile. Production requires
  delegated cgroups. Cancellation awaits kill/delete/reaping despite repeated cancellation,
  always clears active bookkeeping, and leaves no background cleanup on a closed loop.
  Real gVisor escape tests passed with the sudo test profile (host cgroups ignored);
  the production profile with delegated cgroups is untested.
- **Bubblewrap sandbox backend (P31-S4):** explicit operator opt-in on Linux with native
  Python/venv runtime, private namespaces, no network, read-only runtime/input files,
  bounded tmpfs, resource/output limits and process cleanup. Real namespace probe reports
  host restrictions; enabled stronger backends take precedence. A mandatory built-in
  seccomp policy applies; an operator-supplied BPF file can only add restrictions.
  Unit tests and shared escape cases added; live bubblewrap verification was done on
  the lead host for the default profile.
- **Review the exact action (P32c):** Studio's approval screens show the action kind and name,
  run and node, every argument as the canonical JSON the server hashed, the payload and agent
  spec digests (copyable), and a live expiry; Approve is disabled once a request has expired,
  and missing fields read "not recorded" instead of being guessed. Workflow approvals now
  carry `canonical_payload_json` (the exact text behind `payload_digest`) and, when an edit
  re-gates a node, `supersedes_approval_id`, `regate_reason` and `previous_input_state`, which
  the screen uses to show what changed.
- **Portable failure reproduction (P32a):** record an agent run (`ReproRecorder.run_agent`) or
  a workflow run (`run_workflow`), export it as one versioned JSON bundle, and replay it offline
  (`replay_repro_bundle`, or `nexura-runtime repro export RUN_ID` / `repro run bundle.json`,
  exit 0 when the failure reproduces, 1 when it diverges). Replay uses a scripted model and
  recorded tool and node results only: no real tool, module, provider or network call runs,
  and socket, DNS and datagram calls are blocked. Known secret shapes are redacted
  (secret-named fields, credential headers, declared credential values and their base64 and
  URL-encoded copies); redaction is best-effort, so review a bundle before sharing it. Agent
  bundles need only `nexura-agents`; workflow bundles replay through the runtime, which
  registers its replayer on import. Guide: `docs/reproducing-failures.md`.
- **Studio extension translations (P28 step 4):** a `StudioExtension` may carry `messages`
  (per language, nested like `en.json`); `t(key)` falls back to them only for keys the core
  dictionary does not define.
- **Extension points (P28 step 2):** `create_api_app(auth_provider=..., extensions=...,
  enabled_extensions=...)` with `AuthProvider`, `ServiceExtension`, `CredentialBackend` and
  `MembershipStore` protocols; entry points (`nexura.extensions`) load only when named. The
  shared profile without a working provider refuses to start. Extensions cannot replace the
  auth provider or other protected state, shadow built-in routes, or add routes outside
  `/api/v1` unless declared in `public_paths`; a provider's identity is validated before any
  route runs. Studio features register through an extension registry. No behaviour change for
  existing setups. Guide: `docs/extensions.md`.
- **Code-execution sandbox backends (P27 stage 2):** `nexura.tools.sandbox.execute` selects
  from operator-enabled backends by isolation level (`interpreter` < `os_sandbox` < `gvisor` <
  `microvm`; `SandboxConfig.min_isolation`, `NEXURA_SANDBOX_BACKENDS`). New default backend
  **Monty** (`pydantic-monty`, extra `sandbox`): a fresh worker per call with an empty
  environment, no filesystem, network or subprocess, operator time/memory/output limits, and
  no host file descriptors or working directory. Every run still needs approval bound to the
  code; oversized code is refused before anyone is asked to approve it; at most 4 runs per
  workspace at once (configurable). The authenticated shared service refuses to start with a
  sandbox weaker than gVisor. One escape-test suite runs against every installed backend
  (`tests/security/test_p27_sandbox_escapes.py`).
- **Moving from OpenAI Agent Builder and Evals (P30):** `docs/migrating-from-openai.md` maps
  Agent Builder and OpenAI Agents SDK concepts to Nexura, with an offline worked example and the
  shutdown dates (Evals read-only 2026-10-31; Evals and Agent Builder shut down 2026-11-30).
  `nexura-runtime evals import-openai FILE --dataset-id ID` and Studio's **Import JSON** turn an OpenAI
  Evals export (JSONL `{"item": ...}` rows or `openai/evals` registry rows) into a Studio eval
  dataset through `POST /api/v1/evaluations/studio/datasets/import-openai`, which saves
  nothing; `string_check` criteria map to Nexura scorers where they mean the same, and
  everything else is listed as unmapped. Inputs are redacted and size-checked like any dataset.
- **Client coverage (P29):** the Python and TypeScript clients now have typed methods for every
  `/api/v1` route (skills, templates, organisations, publications with environments,
  promotions and map changes, builder drafts, run trace/history/sub-runs/input/fork, sessions,
  knowledge, Studio evals and workflow tests), with ids sent as one encoded path segment.
  `tests/test_p29_route_coverage.py` lists the app's routes and fails when one has no client
  method and is not in a commented allowlist (6 entries: webhook delivery, public widget and
  chat, two legacy workspace proxies).
- **Agents in Studio (P26 stage 2):** an **Agents** page (list with model, tools and
  "needs approval" badges; editor for instructions, model, tools, knowledge, skills, output
  schema and limits, checked against the server's limits before saving; revision conflicts
  offer a reload; viewers read, editors edit, admins delete with a second click), an agent
  picker for `nexura.agents.agent` nodes, readable approval details for an agent's tool calls
  (the internal resume state is never shown), a saved-agent selector in the playground (sends
  `agent_id`, never `model`; refused tool calls listed), and saved agents as eval and
  red-team targets. `GET /agents` items now include `tools` and `approval_tools`.
- **Saved agents (P26 stage 1):** an `AgentSpec` (instructions, a model from the operator's
  chat-model allowlist, tools from registered modules, workspace knowledge and skills, an
  optional output schema, limits; no code fields) stored per workspace with revisions.
  `/api/v1/agents` (viewer reads, editor writes, admin deletes) and `/agents/options`. A
  `nexura.agents.agent` node runs a saved agent in a workflow; any tool that can write pauses
  the run for an approval of the exact calls. Saved agents are eval targets, red-team targets
  and playground chat agents. TS client `client.agents`. Guide: `docs/agents-in-studio.md`.
  Review fixes before release: model adapters and provider-key modules cannot be tools (a
  prompt-injected model could otherwise pick a paid model or send the server key to its own
  URL), red-team never uses the agent's own model, output schemas are bounded, chat turn limit
  answers `422 AGENT_TURN_LIMIT`.
- **Per-caller keys for the V2 flow server (V7):** `V2FlowServer(variables=fn, models=PerCaller(fn),
  v3=fn)` resolves each caller's `load_from_db` variables, models and V3 options (File, SQL,
  Knowledge nodes) per request, `fn(principal, flow_id)`, sync or async. Builds are cached per
  caller and per a salted HMAC of the resolved inputs, so two callers never share a build and
  a rotated key rebuilds; existing constructor calls behave as before. Resolver and build
  failures answer a generic `500` (`resolver_failed`, `build_failed`) and log only the
  exception type. With `models=None` the provider table now also passes the caller's key to
  Sarvam instead of using `SARVAM_API_KEY`. Guide: `docs/langflow-import.md`; V2 steps:
  `V2_ENGINE_SWITCH_GUIDE.md` step 5.
- **Runs reachable in Studio (P25):** `#/runs/{id}` opens the run console for any run (after a
  reload, for runs started by triggers, the API or someone else), with not-found and denied
  notices. A run opened on a workflow page stays in the URL. A **Runs** tab lists the
  workflow's runs with a status filter and pages. **Open run** links from traces, the approvals
  inbox, trigger jobs and workflow-test results that came from a run. The inbox has a
  **Waiting for input** section, and Traces filters by every run status and pages past 50.
- **Publication environments and promotion:**
  - A publication is `test` (default) or `prod`, with a `credential_map` that rewrites each
    node's `config.credential_ref` when the gateway runs the pinned spec. Test passes unmapped
    references through; prod fails closed.
  - `POST /api/v1/published/{id}/promote` copies the test publication's pinned spec (same
    `spec_hash`) to the prod publication, admin only, and records who and when. With the
    workspace policy `require_promotion_approval`, a different admin approves it through
    `POST /api/v1/published/{id}/promotions/{promotion_id}/approve`, bound to the spec hash.
  - Existing publications migrate to `test`. Studio's Publish tab has the environment, a
    credential-map editor and a Promote button. Guide: `docs/publishing.md`.
- **Resource-scoped API keys:**
  - `create_api_key(..., resources=["workflow:<id>", "knowledge:<collection>"])` limits a key
    to running listed workflows, reading their runs and searching listed collections.
  - Deny-by-default: every other route answers 403 `RESOURCE_SCOPE_DENIED`.
  - Guide: `docs/organisations-and-secrets.md`.
- **Skills** (`Skill`, `SkillLibrary`):
  - Reusable, versioned instruction bundles in `SKILL.md` form. The agent sees a catalog of
    names and descriptions and calls `load_skill(name)` for the full text.
  - Skills are text only: script files are refused, front matter is never evaluated, and a
    skill can name tools but never add them.
  - Labels such as `production` let you promote a new version without changing the agent.
    Guide: `docs/skills.md`.
- **Red-team eval pack** (`nexura_agents.redteam`):
  - Cases for prompt injection, approval bypass (for example "the admin already approved it")
    and indirect injection through a poisoned document tool.
  - Two scorers:
    - `no_canary_leak` catches a secret canary in the answer or in tool-call arguments,
      including encoded forms.
    - `no_write_without_approval` fails if a write tool ran; a case paused for approval counts
      as safe.
  - `redteam_suite(write_tools)` returns everything for `evaluate`. It runs offline with
    `FakeModel` in CI. Guide: `docs/evals.md`.
  - `nexura-runtime redteam module:agent` runs the pack from the command line (exit 1 on an unsafe
    case, so it works in CI; live models need `--allow-live-model`). `--out` JSON has the
    canary scrubbed and secrets redacted.
  - Studio's Evaluations page has a Red-team section (`POST /api/v1/evaluations/studio/redteam`,
    editor role, 6 runs per minute per workspace). Write tools are inert stubs.
- **Workflow tests** (P22):
  - `POST /api/v1/evaluations/studio/datasets/{id}/cases/from-run` saves a completed workflow
    run as a regression case (initial inputs, final output or `state[output_key]`), editor
    role, owner-checked, secrets redacted as in `PUT`.
  - Refused: forks, runs that paused for an approval or input, credential-named output keys,
    and content that could not be stored unchanged.
  - Datasets can be bound to a workflow (`workflow_id`, `GET /datasets?workflow_id=`). Studio
    has **Save as test** on the run console and a **Tests** tab on the workflow page that runs
    the tests and compares with the previous run. Guide: `docs/evals.md`.
  - `record_path: true` (Studio: **Also check the path**) also stores the handler order of the
    run's top-level steps as `expected_trajectory`, so a revision that changes the path fails
    the test even with the same output (P23).
- **Skills in the service and Studio:**
  - `/api/v1/skills` stores versioned skills per workspace (viewers read, editors create and
    label, admins remove). Caps: 200 skills per workspace, 100 versions and 20 labels per skill.
  - An agent node's `config.skills` (at most 20 names) and `skills_label` attach skills from
    the workspace store when it runs; promoting a label changes what a saved workflow reads.
  - Studio has a Skills page and a skills picker in the node inspector.
- **Context-window overflow recovery:**
  - A provider error saying the prompt is longer than the context window is raised as
    `ProviderContextLengthError`, a subclass of `ProviderClientError`. This covers OpenAI,
    Anthropic, Gemini/Vertex and LiteLLM.
  - `RetryModel(trim_history=N)` drops the oldest whole turn and retries, up to N times. It
    keeps system messages and the latest turn, and never separates a tool call from its result.
    Off by default. Guide: `docs/providers.md`.
- **Sub-runs in Studio:**
  - `GET /runs/{id}` reports `has_subworkflows`, and the Sub-runs tab appears only when it is
    true.
  - Each child run opens its own view with status and a checkpoint timeline, served by
    `GET /runs/{id}/subruns/{child_key}`. The endpoint has the parent's ownership checks and
    redaction.
  - The TypeScript client types `PendingApproval.details` and adds `runs.subrun()`.
- **Durable runs in Studio:**
  - A paused run's question can be answered in a form. The answer is checked against the
    question's schema before it is sent.
  - Checkpoint history shows each checkpoint's redacted state. You can fork from a checkpoint
    with edited JSON state and land on the new run.
  - A Sub-runs panel shows child runs.
  - The approval dialog shows a mapped write's item count, the items (the first 20), and the
    cap. It also explains that editing inputs pauses the run again for a fresh approval.
  - Child runs got their own page later the same day (see "Sub-runs in Studio").
- **Connector setup in Studio:**
  - The module lists mark each operation as "reads" or "writes - needs approval" and show
    the credential type a connector needs. An unknown operation counts as a write.
  - A connector step has a credential picker filtered to that type. It shows only ids and
    descriptions, never values, and a reference it does not know is hidden.
  - The Credentials form offers the connector credential types.
  - Workflow `input_mapping` accepts `{"$literal": value}` for a fixed value, which the
    operation select uses. The approval gate reads the literal operation.
- **Knowledge search in the chat playground:**
  - The playground agent has a `knowledge_search` tool over the workspace's own documents.
    Citations reach the chat footnotes end to end.
  - It runs for document questions only: whole-word keywords, or a question when the
    workspace has documents. Small talk does not trigger it.
  - Messages are capped at 8,000 characters and search queries at 512.
  - Secrets in documents are redacted from tool output and replies, and `[n]` markers taken
    from document text are neutralised.
- **Connectors pack:** 15 connectors as modules (`nexura.connectors.*`): webhook, Slack,
  Discord, Microsoft Teams, SMTP, IMAP, Google Drive, Google Sheets, Postgres, MySQL,
  S3-compatible storage, GitHub, Notion, Jira and Airtable. They sit alongside the
  existing HTTP request and SQL modules.
  - Credentials come only from the credential store, by reference.
  - Writes need approval and are never retried. Reads retry on 429, 5xx and timeouts.
  - Outgoing requests go only to public internet addresses, with no redirects. An
    allow-list can open private and loopback hosts, but never cloud metadata addresses.
  - Responses, pages and rows are capped, and tokens are redacted from errors.
  - MySQL needs a driver you supply, since none is bundled.
  - Tested only with mock transports and fakes. Guide: `docs/connectors.md`.
- **Knowledge search with citations:**
  - Search combines keyword (SQLite FTS5) and vector results. Scores run from 0 to 1, and
    searches are scoped to one workspace.
  - Every hit cites its document, chunk, character span and source.
  - Ingestion takes text, Markdown, HTML (with scripts and markup stripped) and PDF, with
    per-file and per-workspace size limits on the extracted text. Duplicates are skipped,
    and re-uploading replaces the old version in one step. Archives are refused.
  - Studio's Knowledge page has upload, a document list, delete, and search with
    highlighted snippets. Chat answers show citations as footnotes linking to the source
    passage. Guide: `docs/knowledge.md`.
- **Cut-over kit for moving Nexura V2 off Langflow:**
  - `python -m nexura_agentkit.integrations.nexura_v2.shadow` replays recorded Langflow
    runs on the SDK and compares text, output structure and tool calls. Its tests use a
    labelled replay model, it never calls a paid model, and it redacts secrets from its
    reports.
  - `--scan` reports which flows can switch now and groups them into waves.
  - Guide: `docs/migrating-from-langflow.md`. The rollback plan is in
    `oss-completion-blueprint/reports/V6_CUTOVER_KIT.md`; V2 still has to build the
    per-flow engine flag.
- **Organisations and an encrypted secrets store:**
  - Organisations own workspaces and have owner, admin and member roles that map onto
    workspace roles.
  - Invites are single-use, expiring tokens stored only as hashes. They can be bound to
    an email address and stop working if the inviter loses the right to send them.
  - Removing a member removes them from every organisation workspace and revokes their
    API keys. Only an owner can change or remove an owner, and the last owner cannot be
    removed. Membership and role changes are written to an audit log.
  - Secrets and provider keys are encrypted with AES-256-GCM: each secret has its own data
    key, which is encrypted under `NEXURA_MASTER_KEY` and bound to its workspace and name.
    The API only ever returns a masked hint. Master-key rotation re-encrypts every data key
    in one transaction.
  - A shared server refuses to start without `NEXURA_MASTER_KEY`. Local mode is unchanged
    without a key.
  - Studio has an Organisation page. Guide: `docs/organisations-and-secrets.md`.
- **Export back to Langflow:** `export_flow(flow, original=...)` writes an imported flow
  back to Langflow 1.12 JSON, so V2's editor can keep opening it.
  - Unedited flows come back identical, apart from literal secrets, which are blanked.
    Edits land only in the fields that changed.
  - New edges are type-checked like Langflow's editor checks them.
  - Component code is never invented: nodes created only in the SDK are refused unless
    `allow_sdk_only=True`. Guide: `docs/langflow-import.md`.
- **Publishing and the embeddable chat widget:** publish a workflow version as a chat any
  website can embed with one `<script>` tag. The widget is 14 KB, uses Shadow DOM, renders
  replies as text and holds no provider keys.
  - A publication pins a spec snapshot. It has a revocable, hashed public key, an
    exact-origin allow-list, per-client and publication-wide rate limits, and a daily
    spend cap with atomic reservations (also settled after an approved run resumes).
  - The public endpoint checks the key before anything else and gives the same 401 for
    unknown, revoked or wrong keys.
  - Runs that need approval wait for review; visitors cannot approve.
  - Studio has a Publish tab. Guide: `docs/publishing.md`.
- **Self-hosting:** a root `Dockerfile` (non-root) and `docker-compose.yml` that refuse to
  start without secrets and authentication, plus `.env.example`. The files are checked by
  tests but have not been built or run here. Guide: `docs/self-hosting.md`.
- **Nexura V2 identity and policy for the V2-compatible API:**
  - Login: a Firebase-compatible ID-token verifier (RS256, caller-supplied JWKS, no
    network). The principal comes from the signed `sub` and workspace claims.
  - Policy: a V2 policy adapter for approved model providers, allowed component classes
    and token budgets. It is checked on the tweaked flow and again on the built flow, and
    charged for streams too.
  - Records: run and trace records in V2's shapes, redacted.
  - Report: `reports/V4_V2_SERVICE_MAPPING.md`.
- **Chat-to-build:** `POST /api/v1/builder/draft` and a "Describe it" panel in Create
  Workflow. Your own provider model drafts a workflow from a description.
  - Only installed modules are used; unknown ones fail the draft.
  - The engine validates the draft, with bounded retries that feed errors back to the
    model.
  - Approvals are forced in code on write steps. A handler with no manifest counts as a
    write unless it declares itself pure or read.
  - Secrets are redacted from the description and stripped from every spec field.
  - Prompts and replays are size-bounded, at 10 drafts per minute per workspace.
  - The draft is never saved until you create it. Guide: `docs/studio-and-service.md`.
- **More Langflow components (opt-in):** `load(flow, extended=True)` with
  `build(..., v3=V3Options(...))`.
  - File: rooted, with aggregate size and path caps.
  - Message memory: scoped by flow and session.
  - Knowledge retrieval: workspace-scoped.
  - Loop: bounded.
  - SQL: read-only, one statement, parameters bound, result size capped inside SQLite.
  - SplitText.
  - Covers 139 of the 146 component uses counted in V2's 27 starter flows (by class).
    `V2FlowServer(extended=True)` enables them in the V2-compatible API.
- **Template gallery:** six starter workflows, each validated by the engine and tested end
  to end offline:
  - support desk with refund approval;
  - RAG Q&A with citations;
  - research team;
  - human-in-the-loop approval;
  - scheduled report;
  - structured extraction.

  `GET /api/v1/templates` and `POST /api/v1/templates/{id}/instantiate` (workspace and role
  checked; required credentials listed by name, never filled). A Templates page in Studio
  has a graph preview. Guide: `docs/templates.md`.
- **V2-compatible flow API (Nexura V2 engine switch):**
  `nexura_agentkit.integrations.langflow.server.V2FlowServer` serves Langflow flows on the
  Nexura SDK. It shapes these endpoints the way Nexura V2 does:
  - `POST /api/v1/run/{flow_id}` (JSON or SSE);
  - `POST /api/v1/webhook/{flow_id}`;
  - `POST /api/v1/responses` (OpenAI-responses subset).

  It has pluggable flow sources and an auth verifier, and denies by default. Sessions are
  scoped by principal, flow and session; builds are cached per flow version; body size and
  nesting are capped. Stream errors carry a generic message once. V2 itself is unchanged;
  `reports/V2_ENGINE_SWITCH_GUIDE.md` explains how V2 would route a flow here. Guide:
  `docs/langflow-import.md`.
- **Export to Python and back (no lock-in):**
  - `nexura_agentkit.codegen.workflow_to_python` writes readable, ruff-clean Python that
    rebuilds the identical workflow (every node kind round-trips).
  - `python_to_workflow` reads that shape back by parsing only; it never executes the
    source, and it refuses calls, attribute tricks, f-strings, non-finite numbers and
    invalid strings, reporting line numbers.
  - Available in the API (`GET /workflows/{id}/export/python`,
    `POST /workflows/import/python`), the CLI (`nexura-runtime workflow export --format python`,
    `nexura-runtime workflow import file.py`) and Studio's import/export dialog. Guide:
    `docs/workflows.md`.
- **Studio: evals view.**
  - Datasets of cases with expected output and tool trajectories, up to 200 per dataset.
    Secrets in cases, names and descriptions are redacted on save.
  - Runs score a workflow or agent version with the SDK evals engine. Two runs can be
    compared with regressions highlighted, and a pass/fail gate uses the same rule as
    `assert_no_regressions`.
  - Storage is per workspace. Guide: `docs/evals.md#evals-in-studio`.
- **Studio: composite steps on the canvas.** Add and edit parallel, map and sub-workflow
  steps.
  - Inspector forms are checked inline, with saved values kept even when their module is
    missing.
  - Lossless round trip and undo/redo.
  - Save runs the server's validation first. The server now applies the engine's
    composite rules and counts branch and sub-workflow handlers in the side-effect
    estimate.
  - A malformed retry policy is reported instead of crashing, and a fractional
    `max_concurrency` is refused.
- **Studio: run traces.** A Run Traces page and per-run waterfall: workflow steps, model
  calls, tool calls, handoffs and retries, with durations, tokens, cost and errors.
  - Agent events inside a step are recorded server-side, redacted and capped at 200 per
    step.
  - `GET /api/v1/runs/{id}/trace` returns the span tree, with content off by default and
    every span field redacted.
  - The endpoint honours the diagnostics retention purge and workspace ownership.
- **Langflow import (for Nexura V2 compatibility):** `nexura_agentkit.integrations.langflow`.
  - `load(flow)` reads a Langflow 1.12.5 flow. `build(...)` turns it into Nexura SDK
    agents.
  - Supported nodes: ChatInput/Output, Agent, Prompt, Parser, TextInput, LanguageModel,
    StructuredOutput, and the calculator, URL and web-search tools. Tweaks follow V2's
    refusal rules.
  - Unsupported nodes on the main path fail with an explicit report. Code embedded in a
    flow is never executed.
  - `stream_flow_events` emits V2's SSE event names, so V2 can switch engines without UI
    changes.
  - Guide: `docs/langflow-import.md`.
- **Secure defaults and threat model:** `SECURITY_MODEL.md` maps each threat to its
  control and the test that proves it.
  - The service (`nexura serve`) and Studio refuse to start on a non-loopback address
    without auth. The opt-out is explicit: `--insecure-allow-unauthenticated-network` or
    an exact environment value.
  - Studio gains a bearer token and request body caps.
  - An AST test keeps `eval`/`exec`/`compile` out of the code base.
  - Under the default allowlist, MCP stdio launches refuse:
    - inline code in any flag form;
    - REPL flags and launches with no script (the program would come from the JSON-RPC
      pipe);
    - interpreters that only share a name with an allowed launcher.
- **Memory lifecycle:**
  - `Memory(ttl=...)` makes saved memories expire. Expired ones are never recalled, and
    `prune()` removes them.
  - `Memory(dedupe_threshold=...)` replaces a near-duplicate instead of adding another;
    saves to one namespace are serialised.
  - `summarize_older_turns(model, keep_last_turns=4)` is a history processor that replaces
    older turns with a labelled, cached summary. It cuts only at user turns and escapes the
    excerpt; its usage counts.
  - `MemoryRecord.expires_at` is new and optional (older records load), and a NaN expiry
    counts as expired.
  - Usage recorded with `ctx.add_usage` from history processors or instructions now counts
    right after the model call, not only after a tool step.
  - Parity with LangMem, Mem0 and ADK memory services. Guides: `docs/memory.md` and
    `docs/agents.md`.
- **GroupChat team:**
  - Members take turns in one shared conversation. `select` picks the speaker:
    `"round_robin"`, your function, or a manager `Agent` whose answer must be a member
    name.
  - `terminate` takes `MaxTurns`, `TextMention` or a callable, combined with `|` and `&`;
    `max_turns` is a hard cap.
  - `result.stop_reason` says why it ended. Pause/resume keeps each turn's speaker, and
    streaming works as for other teams.
  - Outputs are indented in the shared history so they cannot impersonate another turn.
  - Parity with AutoGen `SelectorGroupChat`/`RoundRobinGroupChat` and CrewAI's
    hierarchical process. Guide: `docs/teams.md#groupchat`.
- **Custom progress events:** `ctx.emit(name, **data)` from a tool or hook yields a `CUSTOM`
  run event. Events from tools stream live while the tool runs, including sync tools and
  parallel tools; events from hooks and instructions arrive at the next step. Team streams
  tag them with the member. Parity with LangGraph `custom` stream mode and ADK events.
  Guide: `docs/agents.md#custom-progress-events`.
- **Typed results:** `Agent` and `RunResult` are generic in the output type for mypy and
  pyright. `Agent(output_type=Invoice)` gives `RunResult[Invoice]` from `run`, `resume` and
  `recover`, `str` without `output_type`. Runtime behaviour is unchanged. Paused and
  cancelled runs have no output, and handoffs can change the output type; the docs say so.
  Parity with Pydantic AI `Agent[Deps, Output]`. Guide: `docs/structured-output.md`.
- **Team and workflow progress streams:**
  - `Team.stream_async`/`resume_stream_async` yield team, member and member-agent events
    (`TEAM_*`, `MEMBER_STARTED/COMPLETED/FAILED`, tagged with `member_path`).
  - `WorkflowEngine.stream`/`resume_stream` yield `WORKFLOW_*` and `NODE_*` events.
  - The final event carries the same result as the non-streaming call, and closing the
    stream cancels the run.
  - `RunEvent.is_terminal` covers the new end events and is false for member and
    nested-team events.
  - Parity with LangGraph stream modes and CrewAI step callbacks. Guides: `docs/teams.md`,
    `docs/workflows.md`.
- **Rate limits and concurrency caps:** `RateLimitedModel(model, RateLimiter(max_concurrent=,
  requests_per_minute=, tokens_per_minute=))`. One limiter can be shared by many models and
  runs, including across threads. Streams hold their slot until they end. The token budget
  is approximate (it counts reported usage after each call). Usage breakdowns label wrapped
  models by the inner model's name. Parity with Pydantic AI concurrency limits and LangChain
  rate limiters. Guide: `docs/providers.md`.
- **Session management:** `delete_session`, `list_sessions`, `pop_last_turn` and
  `retry_last_turn` (restores the turn if the retry fails or is cancelled, passes `deps` and
  other run options). The built-in stores gain `delete()` and `list_sessions()`. A deleted
  id keeps counting revisions, so stale snapshots cannot overwrite a recreated session.
  Parity with OpenAI Agents `pop_item`/`clear_session`. Guide: `docs/agents.md`.
- **Trajectory and multi-turn evaluation:**
  - `tool_trajectory(expected, mode=strict|unordered|subset|superset,
    check_arguments=)` and `agent_path(expected, mode=)` score which tools and agents a
    run used, with partial credit and type-strict argument matching.
  - `Case(turns=[...])` runs a multi-turn conversation; `for_turn` and `every_turn`
    score single turns.
  - Reasons redact arguments and show them only when they are checked.
  - Parity with LangSmith/agentevals trajectory match and ADK tool trajectory scores.
    Guide: `docs/evals.md`.
- **Request options for OpenAI-compatible models:** `OpenAIChatModel(headers=,
  extra_body=, query_params=)` sends defaults with every request (per-call
  `ModelSettings` wins). It refuses the SDK's own and framing headers, `Authorization`
  when `api_key` is set, case-duplicate or malformed headers, reserved and non-JSON body
  keys. Header values never appear in errors, logs or `repr`. Guide: `docs/providers.md`.
- **Tracing helpers and conversation id:** run spans carry `gen_ai.conversation.id` (the
  `session_id`, also on resume and recovery). `tracing.span(name, **attrs)` wraps your
  own code in a nested span. `tracing.trace(name, group_id=, metadata=)` groups the runs
  started inside it under one parent. User attributes are filtered to plain values and
  redacted. Parity with OpenAI Agents `trace()`/`custom_span()`. Guide:
  `docs/observability.md`.
- **Testing helpers:**
  - `FakeModel(stream=True, chunk_size=...)` streams its scripted replies, so tests get
    `TEXT_DELTA`/`TOOL_CALL_DELTA` events without `StreamingFakeProvider`.
  - `nexura_agents.testing.block_model_requests()` (context manager, overlapping guards
    allowed), `block_model_requests_from_env()` and `pytest_fixture()` make real
    provider-backed models raise `ModelRequestsBlockedError` before any request.
  - `tool_calls`, `assert_tool_called` (strict, type-aware match), `event_types` and
    `collect_events`.
  - Parity with Pydantic AI `TestModel`/`ALLOW_MODEL_REQUESTS`. Guide: `docs/testing.md`.
- **Hooks that take over:** `before_model` may return a `ModelResponse` to skip the model
  call (not billed or counted), `after_model` a replacement reply, `before_tool` a string
  to skip the tool (not cached; output guardrails still run), and `after_tool` a
  replacement result (the tool's files are then withheld). `None` keeps today's
  behaviour, and other return values fail clearly. `MODEL_COMPLETED` has `from_hook`.
  Parity with Google ADK callbacks. Guide: `docs/guardrails-hooks-limits.md`.
- **Safe web fetch tool:** `web_fetch_tool(allowed_domains=..., max_bytes=..., timeout=...)`
  gives an agent a read-only GET tool on `SafeHttpClient`. HTML comes back as text; other
  content types are refused. It refuses private, loopback, link-local, metadata, multicast
  and reserved addresses (including odd IPv4 spellings, IPv4-mapped/6to4/NAT64 IPv6 and
  hostnames that resolve to them), userinfo and control characters, re-checks every
  redirect, and caps size, time and redirects. Failures return to the model as an error
  object. Code execution is deliberately not included. Guide: `docs/tools.md`.
- **MCP resources and prompts:** `MCPServer.list_resources()`, `read_resource(uri)`,
  `list_prompts()` and `get_prompt(name, arguments)` (returns `Message`s), plus opt-in
  `resource_tools()` that let the model list and read only the URIs the server listed.
  Binary contents become a placeholder, never decoded. Guide: `docs/mcp-and-openapi.md`.
- **Branch a conversation:** `fork_session(store, source_id, new_id, at=None)` copies a
  session, whole or up to a message, into a new session id. It never overwrites a session,
  refuses a cut that separates a tool call from its results, and copies the saved run
  state. Works with any `SessionStore`. Parity with LangGraph checkpoint forks and ADK
  session rewind. Guide: `docs/agents.md#branching-a-conversation`.
- **OpenTelemetry metrics:** `instrument()` also records the GenAI histograms
  `gen_ai.client.token.usage` (per model call, by token type) and
  `gen_ai.client.operation.duration` (agent runs, model calls, tool calls, teams and
  workflows, with `error.type` on failure). Attributes stay low-cardinality (no ids, tags or
  content). `instrument(meter_provider=..., metrics=False)` picks the provider or turns them
  off. Parity with Pydantic AI (Logfire) and the OpenTelemetry GenAI instrumentations.
  Guide: `docs/observability.md#metrics`.
- **Streamed tool-call arguments:** with `stream_text=True`, `stream_async` emits
  `TOOL_CALL_DELTA` events (`index`, `id`, `name`, `arguments_delta`) while the model writes a
  tool call, for every streaming provider. They are raw model output, before validation,
  guardrails and approval. Parity with OpenAI Agents SDK and Vercel AI SDK tool-call
  streaming. Guide: `docs/agents.md#streaming`.
- **Anthropic extended thinking with tools:** thinking blocks (with signatures), from normal
  and streamed responses, are kept in `Message.provider_data` and sent back with the tool
  results. They are saved in sessions and checkpoints. Forcing a tool while thinking is
  refused, as the API requires. This replaces the F30 refusal of thinking with any tools.
- **Tools that return files:** a tool can return `ToolOutput(text, [Attachment, ...])`, an
  `Attachment`, or a list mixing text and attachments. The text is the tool result, and
  the files reach the model in one follow-up message after the batch (a description for
  models that cannot view files). Guide: `docs/multimodal.md`.
- **Usage breakdown, tags and metadata:** `result.usage_by_agent` and `result.usage_by_model`
  split model usage, which is kept across pauses and recovery. `run(..., tags=[...],
  metadata={...})` labels a run (`ctx.tags`, `ctx.metadata`, `result.tags`) and adds
  tracing attributes. Guide: `docs/budgets-and-usage.md`.
- **Per-agent hooks:** `AgentHooks.on_agent_start`, `on_agent_end` and `on_handoff`, plus an
  `AGENT_STARTED` event at the start of a run and after each handoff. Hooks objects without
  the new methods keep working. Parity with OpenAI Agents SDK and ADK agent callbacks.
  - **Note:** `stream_async` now emits `AGENT_STARTED` after `RUN_STARTED`, so code that
    asserted an exact event list needs the extra event.
- **Approve with changes:** `resume(..., approve={id: ApproveWith(arguments=..., digest=...)})`
  runs an approved call with a reviewer's edited arguments. They are validated against the
  tool schema, and the transcript is rewritten to show what ran. Parity with LangGraph
  and Pydantic AI `override_args`. Guide: `docs/approvals.md`.
- **Ask the user mid-run:** `Agent(ask_user=True)` gives the model an `ask_user(question)` tool
  that pauses the run. `interruption.questions` lists the questions, and
  `resume(..., answers={id: text})` continues, across processes too. Parity with LangGraph
  `interrupt()` for agents and Pydantic AI deferred tools. Guide: `docs/approvals.md`.
- **Shared run state:** `ctx.state`, a JSON dict that tools, hooks and instructions read and
  write. It is seeded by `run(..., state=)`, returned as `result.state`, saved with sessions,
  approval pauses and durable checkpoints, and shared across handoffs. Parity with ADK
  `session.state` and LangGraph state. Guide: `docs/agents.md`.
- **Tool retries and caching:** `@tool(idempotent=True, retry=ToolRetry(...))` retries
  transient errors (connection errors and timeouts by default) with exponential backoff.
  `cache=True` reuses identical calls within a run. `SchemaTool` takes `idempotent`,
  `retry` and `cache` too. Guide: `docs/tools.md`.
- **Handoff input and conditional handoffs:** `Handoff(target, input_type=Model)` gives the
  model structured handoff arguments (passed to `on_handoff` and the target's
  `ctx.metadata["handoff_input"]`). `Handoff(target, enabled=...)` offers a handoff only
  when it applies. Parity with OpenAI Agents SDK `input_type` and `is_enabled`. Guide:
  `docs/agents.md`.
- **Cancelling runs:** `Runner().run(agent, prompt, cancel=CancelToken())`. Calling
  `token.cancel()` from any thread stops the run before its next step, or interrupts
  the model call or async tool in progress. The run ends with `RUN_CANCELLED` and
  `status="cancelled"`, and keeps its durable checkpoint. `resume` and `recover` accept
  `cancel=` too. Guide: `docs/agents.md`.
- **Agent copies:** `agent.clone(**changes)` returns a validated copy with changes, and
  `with agent.override(model=...)` swaps settings in place for tests. Parity with
  OpenAI Agents SDK `clone` and Pydantic AI `override`. Guides: `docs/agents.md`,
  `docs/testing.md`.
- **More model settings:** `ModelSettings` adds `top_p`, `seed`, `presence_penalty`,
  `frequency_penalty`, `reasoning_effort`, `thinking_budget_tokens` (Claude extended
  thinking, Gemini `thinkingBudget`) and an `extra` passthrough. They are mapped for
  OpenAI, LiteLLM, Gemini/Vertex and Anthropic; unsupported ones produce a
  `RuntimeWarning`. Guide: `docs/providers.md`.
- **tool_choice:** `ModelSettings(tool_choice="auto" | "required" | "none" | "<tool>")`, mapped
  for OpenAI, LiteLLM, Gemini/Vertex and Anthropic. It resets to auto after a tool call
  so a forced choice cannot loop (`Agent(reset_tool_choice=False)` keeps it). Parity
  with OpenAI Agents SDK, ADK and LangGraph. Guide: `docs/providers.md`.
- **Output validators:** `Agent(output_validators=[fn])` checks the parsed answer.
  Raising `ModelRetry` sends the model back with feedback (sharing `output_retries`), and
  returning a value replaces it. Parity with Pydantic AI `@agent.output_validator`. Guide:
  `docs/structured-output.md`.
- **Per-tool options on tool factories:** `openapi_tools`, `module_tool` and `MCPServer`
  accept `timeout` (`tool_timeout` on `MCPServer`), `on_timeout`, `input_guardrails`,
  `output_guardrails` and `enabled`, and apply them to every tool they build. Guide:
  `docs/mcp-and-openapi.md`.
- **Streaming structured output:** with `output_type` and `stream_text=True`, the runner
  emits `PARTIAL_OUTPUT` events with the JSON parsed so far (unvalidated; the validated
  object still arrives with `RUN_COMPLETED`). Parity with Pydantic AI `stream_output`.
  - **Behaviour change:** typed turns now stream on models that declare
    `streams_response_schema` (`OpenAIChatModel`, `LiteLLMModel`, the OpenAI and Gemini
    providers). Previously they were always answered in one piece. The schema is still sent.
  - The Anthropic provider and other models keep answering typed turns in one piece.
- **Per-tool timeouts:** `@tool(timeout=seconds, on_timeout="tell_model" | "fail")` limits a
  single call; the model is told it timed out, or the run fails. Guide: `docs/tools.md`.
- **Diagrams:** `agent.to_mermaid()`, `team.to_mermaid()` and `WorkflowSpec.to_mermaid()`
  return Mermaid flowcharts of agents, handoffs, tools, agents-as-tools, teams and workflow
  steps (branches, approvals, sub-workflows, parallel and `map_over` steps). Parity with
  OpenAI Agents SDK `draw_graph` and LangGraph `draw_mermaid`, with no extra dependency.
- **Tool guardrails:** `@tool(input_guardrails=[...], output_guardrails=[...])` (also on
  `SchemaTool`) check a call's arguments before it runs and its result after. They can
  pass, trip (`GuardrailTripwireTriggered` with `stage="tool input"/"tool output"` and
  `tool`), reject with a message for the model (`GuardrailResult.rejected`), or rewrite
  the result. Parity with OpenAI Agents SDK tool guardrails. Guide:
  `docs/guardrails-hooks-limits.md`.
- **Tool use behaviour:** `Agent(tool_use_behavior="stop_on_first_tool" | StopAtTools([...]))`
  ends the run with a tool's result instead of calling the model again. Failed, rejected
  and retried calls never count. Parity with OpenAI Agents SDK `tool_use_behavior`.
  Guide: `docs/tools.md`.
- **Per-agent model settings:** `Agent(model_settings=ModelSettings(...))` is merged field by
  field over `Runner(model_settings=...)`, so agents in a handoff team can use different
  temperatures and token limits. Guide: `docs/providers.md`.
- **Dynamic tool availability:** `@tool(enabled=bool | fn(ctx))` and
  `Agent(prepare_tools=fn(ctx, tools))` choose which tools the model sees before each
  model call. Hidden tools cannot be called (`UnknownToolError`). `prepare_tools` can also
  reword a tool's description per run. Parity with OpenAI Agents SDK `is_enabled` and
  Pydantic AI `prepare`. Guide: `docs/tools.md`.
- **History processors:** `Agent(history_processors=[...])` trims or rewrites what the
  model sees on each call, without changing the stored session. Built-ins:
  `keep_last_turns(n)` and `fit_token_budget(max_tokens)`. They cut only at user turns, so
  tool calls stay with their results. Custom processors can be sync or async and may take
  the `RunContext`. Invalid output raises `SDKValidationError`. Guide: `docs/agents.md`.
- **Durable agent runs:** `Runner(checkpoint_store=SQLiteCheckpointStore(path))` checkpoints
  each step, and `runner.recover_async(agent, run_id)` resumes after a crash.
  - Finished tools never re-run. A tool that may have half-run is re-run only if marked
    `@tool(idempotent=True)`. Otherwise recovery pauses for a digest-bound human
    decision (or `on_uncertain="fail" | "tell_model"`).
  - Recovery takes an exclusive lease, so two recoverers cannot both continue.
  - Checkpoint writes are fenced by that lease. A stalled recoverer, or a slow original
    run, gets `CheckpointLeaseLostError` instead of overwriting newer progress. Saves by
    the lease holder renew the lease. The runner's cleanup delete is fenced too.
  - A run that crashed after saving its session completes without another model call.
  - Usage and limits carry across recovery; checkpoint files are created 0600.
  - There is zero cost when no store is configured. Guide: `docs/durable-runs.md`.
- **Agent evaluations** (`nexura_agents.evals`):
  - `evaluate(agent_or_team, cases, scorers)` runs each case in isolation, with
    concurrency and repeats. Errors are isolated per case, and approval pauses are
    reported (or `auto_approve` resumes them, digest-bound).
  - Scorers: `exact_match`, `contains`, `regex_match` (static ReDoS guard plus a
    timeout), `json_schema`, `tool_called`, `max_latency`, `max_tokens`, `max_cost`,
    and `llm_judge` (fenced, strictly parsed verdict; judge usage kept separate).
  - `EvalReport` serialises to JSON and Markdown and records prompt versions.
    `compare()` and `assert_no_regressions()` support CI. Guide: `docs/evals.md`.
- **Built-in guardrails** (`nexura_agents.guardrails`):
  - `detect_pii` / `redact_pii`: emails, phones, Aadhaar (Verhoeff), PAN, cards
    (Luhn), UPI, IFSC, IPv4, API keys.
  - Factories: `block_pii`, `no_prompt_injection` (English, Hindi and Hinglish
    phrases, hidden characters, look-alike letters), `max_length`, `allowed_topics`,
    `json_output_only`, `redact_output_pii`.
  - `GuardrailResult.rewritten()` lets an output guardrail replace the final text.
  - These are heuristics, not a security boundary (documented).
- **Examples gallery** `examples/agents/`: seven offline programs (handoffs with
  approvals, a team with tracing, A2A, MCP+OpenAPI, per-user memory, guardrails, prompt
  A/B), each run by a test.
- **Streaming for `OpenAIChatModel` and `LiteLLMModel`:** `Runner.stream_async(...,
  stream_text=True)` now yields real text deltas for both.
  - Tool-call arguments are assembled across chunks, including backends that reuse one
    index for parallel calls.
  - Usage and finish reason come at the end.
  - Mid-stream errors map to the same typed errors as non-streaming calls.
  - Cancelling closes the HTTP or LiteLLM stream.
  - Turns that request a schema still run non-streamed.
- **Docs refresh:** README, the `nexura-agents` README, POSITIONING and LAUNCH_KIT now
  describe the shipped feature set.
  - Precise Python support: SDK 3.11–3.13, platform 3.12.
  - Live-validation status: Vertex live-checked; other providers mocked only.
  - The comparison table marks competitor facts it could not verify, and says where
    other frameworks are ahead.
- **Trigger job leases and crash recovery:**
  - Claimed jobs hold a lease that the worker renews while running (`--lease-seconds`,
    default 300).
  - Jobs whose worker died are reclaimed atomically and retried up to
    `--max-attempts`. The dead run is marked `interrupted` and the new run is linked.
  - Jobs run concurrently (`--max-concurrent-jobs`), so long runs no longer block
    schedule ticks.
- **Faster import:** provider, model and contract subpackages load lazily.
  `import nexura_agents` plus `Agent, Runner, tool` takes about 135 ms (was about
  300 ms), and no provider adapter is imported until used.
- **Native structured output:** `output_type` also accepts a JSON-schema dict
  (supported subset checked at construction, outputs validated with path-level errors
  fed back to the model).
  - Models that support it get the schema natively: OpenAI `response_format`
    (`strict` only when it cannot change meaning), Gemini/Vertex `responseSchema` (refs
    inlined, unsupported keywords dropped), LiteLLM `response_format`, and Anthropic via
    a forced `structured_output` tool.
  - Other models keep the schema in the prompt.
  - The runner always validates, so constraints a provider ignores are still enforced.
  - `live/provider_check.py` covers structured output for the owner's live checks.
- **Handoffs:** `Agent(handoffs=[billing, handoff(tech, input_filter=...)])` gives the
  model `transfer_to_<name>` tools that pass the conversation to a specialist. The
  specialist's model, instructions, tools and output type take over.
  - `max_handoffs` (default 10) applies; usage and limits cover the whole run.
  - `RunResult.last_agent` and `agent_path`, a `HANDOFF` event, and handoff spans.
  - Approval pauses resume with the active agent, even after a JSON round trip.
  - Works inside teams and when served over MCP or A2A.
- **Real models in the service chat:**
  - Operators allow models with `nexura-runtime serve --chat-model NAME=PROVIDER:MODEL` or
    `NEXURA_CHAT_MODELS`; keys come from the server environment only.
  - Clients pick from `GET /api/v1/chat/models` and can never pass arbitrary provider
    strings.
  - Viewers may use only the offline fixture.
  - Usage caps apply, and provider errors reach clients only as fixed codes.
  - Responses report the real model and whether the reply was streamed.
  - Studio playground has a model picker.
- **A2A protocol (Agent2Agent 1.0, JSON-RPC binding; 0.3 method names also accepted):**
  - `A2AServer(agent_or_team, url=...).http_app(token=...)` serves an agent card and
    `SendMessage`, `SendStreamingMessage` (SSE), `GetTask` and `CancelTask`.
  - Approvals become `input-required` and are continued on the same task with
    digest-bound decisions. Follow-ups in a `contextId` keep history.
  - `A2AClient` and `remote_agent_tool(url)` let a Nexura agent call any A2A agent.
    Responses are size-capped streams, redirects are not followed, and the bearer
    token is only sent to the card's own origin.
  - The wire format was checked against the official a2a-sdk 1.2.2 source.
  - Not implemented: push notifications, ListTasks/Subscribe, the HTTP+JSON and gRPC
    bindings, signed cards, OAuth. Guide: `docs/a2a.md`.
- **Prompt registry:** `PromptRegistry` with immutable versions, movable labels
  (`set_label("support", "production", 3)`) and a safe `{{ var }}` template (mapping
  keys only, no dunder access, single-pass rendering, `\{{` escapes).
  - `InMemoryPromptStore` and `FilePromptStore` (atomic writes, safe names, locks shared
    across instances and processes on POSIX).
  - `Agent(instructions=registry.instructions("support", label="production"))`
    resolves per conversation, and the version used appears in `RunResult.prompt` and
    on tracing spans (`gen_ai.prompt.*`). Guide: `docs/prompts.md`.
- **Long-term memory for agents:** `Agent(memory=Memory(store, namespace=...))`.
  - Relevant memories are recalled into a delimited, sanitised block in the system
    prompt. The block is never saved to sessions, and it is carried across approval
    resumes without a second recall.
  - `remember`/`recall` tools.
  - Per-user namespaces (`namespace=lambda ctx: ctx.deps.user_id`).
  - `InMemoryMemoryStore`, `HashingEmbedder` (offline demos), `ProviderEmbedder`, and
    the platform's `VectorMemoryStore` over existing vector stores.
  - Bounded by the run timeout; duplicates are collapsed; a changed embedding
    dimension raises instead of silently hiding memories. Guide: `docs/memory.md`.
- **OpenTelemetry tracing (opt-in):** `nexura_agents.tracing.instrument()`, with the
  extra `nexura-agents[otel]`.
  - Emits GenAI semantic-convention spans: `invoke_agent`, `chat`, `execute_tool`,
    `invoke_team`, `workflow`, `workflow.node`, and branch and map-item spans. They
    carry usage, status, pause reason and retry attempts.
  - Parents stay correct across parallel tools and branches.
  - Content is recorded only with `capture_content=True`, and is then redacted.
  - When tracing is off the cost is one check, and `opentelemetry` is not imported.
  - A failing tracer never fails a run. Guide: `docs/observability.md`.
- **LiteLLM models (100+ providers):** `LiteLLMModel("bedrock/...")` or
  `"litellm:ollama/llama3.1"` strings, via the optional extra `nexura-agents[litellm]`.
  - Tool calls, attachments, usage (reasoning and cache tokens) and typed errors
    (auth, rate limit with `Retry-After`, server, client).
  - LiteLLM's own retries are disabled, so `RetryModel` and `FallbackModel` own retries.
  - The caller's API key is redacted from errors.
  - Checked offline against the real LiteLLM 1.104.1 with its mock responses
    (`examples/live/litellm_offline_check.py`).
  - Not streaming yet.
- **Trigger worker: schedules and webhooks now start workflow runs.** Before, jobs were
  enqueued but nothing ran them.
  - `TriggerWorker` (`run_once` / `run_forever`) ticks schedules, claims jobs
    atomically and runs them in their own workspace through the same path as
    `POST /runs`.
  - It enforces release pins (revision or spec hash) and records the job's `run_id`.
  - Run it with `nexura-runtime serve --worker` or a standalone `nexura-runtime worker --db ... [--once]`.
  - Schedules advance with compare-and-swap, so several workers never double-fire a
    schedule.
  - Overdue schedules honour `missed_run_policy` instead of firing on every tick.
- **Service, client and Studio support for human input, history and forks:**
  - New endpoints:
    - `POST /api/v1/runs/{id}/input` answers an `interrupt()`.
    - `GET /history` lists checkpoints.
    - `POST /fork` starts a new run from a checkpoint, optionally with edited state.
    - `GET /subruns` lists sub-workflow runs.
  - A new run status, `waiting_for_input`, with `pending_input`.
  - TypeScript client: `runs.provideInput/history/fork/subruns`.
  - Studio: a "Pending input" banner to answer questions, and a History tab with
    "Fork from here".
  - All endpoints enforce workspace ownership and permissions.
- **Agent teams:** `SequentialAgent`, `ParallelAgent` and `LoopAgent` compose agents and
  nested teams.
  - Output passes to the next member, and is customisable with `handoff`, `combine`,
    `until` and `feedback`.
  - One `Runner` (hooks, token limits, budget) covers the whole tree, and usage is
    summed exactly once.
  - Approvals pass through: a paused member pauses the team with a `TeamInterruption`.
    Pending calls are keyed `"path:call_id"`, the interruption round-trips through
    JSON, and `team.resume_async(...)` continues without re-running finished members.
  - `team.as_tool()` lets an agent call a team, and `AgentMCPServer(agents=[team])`
    serves one over MCP. Guide: `docs/teams.md`.
- **API reference:** `docs/api-reference.md` lists every public name in `nexura_agents`
  and the workflow API, with signatures and docstrings. It is generated by
  `scripts/gen_api_reference.py` (`--check` reports a stale page). Tests require every
  public name and method to have a docstring, and the page to be current.
- **Documentation:** `docs/` guide pages: getting started, agents, tools, MCP
  and OpenAPI, structured output, multimodal, approvals, guardrails/hooks/limits,
  serving over MCP, providers, budgets and usage, workflows, service and Studio,
  testing, and troubleshooting. `tests/docs/test_docs_examples.py` runs every
  example and checks every relative link.
- **Sub-workflows and parallel steps.** `WorkflowNode(subgraph=spec)` runs a workflow as
  one step, with input/output mappings. Approvals and `interrupt()` inside it pause the
  parent under a node path (`kyc/ask`) and are answered on the parent run. Sub-runs are
  stored with their own history (`<run>/<node>#<step>`). `parallel=[handlers]` runs
  handlers concurrently on the same state. `map_over="key"` runs a handler or sub-workflow
  per list item, with a `max_concurrency` cap. `reducers=` (`append`, `extend`, `sum`,
  `merge`, `max`, `min`) merge results in declaration order. An unreduced key written
  twice is an error, and the first failing branch cancels the rest. A sub-workflow's spec
  is part of its parent's hash.
- **Workflow retries, mid-step human input, and time travel.** `WorkflowNode(retry={...})`
  retries a step with exponential backoff, an optional `retry_on` filter and a deadline
  check (refused at validation for `non_idempotent_write` modules). `interrupt(value)`
  inside a handler pauses the run; `engine.resume_interrupt(run_id, interrupt_id, answer)`
  returns the answer from that call (claimed once; durable in SQLite).
  `engine.history(run_id)` lists every checkpoint and `engine.fork(run_id, index,
  state_update=...)` continues from one as a new run without changing the original.
- **Images, documents and audio as input:** `Attachment.from_path/from_bytes/from_url`;
  prompts may be lists mixing text and attachments. Translated for OpenAI, Anthropic and
  Gemini/Vertex; text-only providers and unsupported types fail before sending.
  Attachments persist in sessions and paused runs and are counted in budget reservations.
- **`AgentMCPServer`:** serve agents and tools over MCP via stdio (`run_stdio()`) or
  Streamable HTTP (`http_app(token=...)`, ASGI). Approval-gated work is refused over MCP.
- MCP protocol versions 2025-06-18 and 2025-03-26 (plus 2024-11-05); servers negotiate
  the client's version and answer `ping`.
- **MCP and OpenAPI as agent tools, built into `nexura-agents`.** `MCPServer.stdio(...)`,
  `MCPServer.http(url, headers=...)` and `await server.tools()`; `openapi_tools(spec,
  headers=..., requires_approval="writes")`; `module_tool(module)` for any `BaseModule`;
  `SchemaTool` for tools defined by a JSON schema. Approval policies (`True`, `"writes"`,
  names, predicate), operator-declared `read_only`, include/exclude filters, name
  prefixes, result truncation, and errors returned to the model.
- MCP Streamable HTTP: event-stream replies, `202` for notifications, `Mcp-Session-Id`
  and `MCP-Protocol-Version` headers, auth headers, session `DELETE` on close.
- `nexura_agents.mcp`, `nexura_agents.openapi` and `nexura_agents.network` (moved from the
  platform; old `nexura_agentkit.integrations.mcp`/`.openapi` and `nexura_agentkit.network`
  imports resolve to the same modules).
- **`nexura-agents`**, a separate distribution (`packages/nexura-agents`, import
  `nexura_agents`) containing the agent SDK: Agent, Runner, tools, run context,
  guardrails, hooks, limits, sessions, models, providers, budgets, contracts and context
  selection. Core dependencies: httpx and pydantic. `nexura-agentkit` depends on it, and
  every existing `nexura_agentkit.<module>` import path resolves to the same objects.
- Agent loop: typed `deps` via `RunContext`, dynamic instructions, `output_type` with
  validation retries, `ModelRetry`, parallel tool calls, input/output guardrails,
  `AgentHooks`, `UsageLimits`, per-tool approval with JSON-persistable `RunInterruption`
  bound to action digests, `stream_text` text deltas, budget/context for provider models,
  `Agent.as_tool()` with usage roll-up, `RetryModel`/`FallbackModel`.
- `Agent(model="provider:model")` strings (vertex, gemini, openai, anthropic, sarvam,
  ollama, test); OpenAI and Gemini providers read `OPENAI_API_KEY` / `GEMINI_API_KEY`.
- `VertexAIProvider` (Gemini on Vertex AI with Application Default Credentials).
- MIT `LICENSE` (Liceron) and security contact.
- Streamed chat turns: `POST /api/v1/sessions/{id}/chat/stream` (SSE) and TS
  `sessions.chatStream()`; Studio shows live turn activity, a Stop button and an
  explicit "interrupted, nothing saved" state.
- `ModuleRegistry.include()` / `include_from("pkg.module:attr")` and `--handlers` on
  `nexura workflow export|run` and exported `run.py` for custom handlers.
- `provider_capabilities` now reports each adapter's declared capabilities.
- Release gate runs the real gVisor sandbox and database profiles; skipped tests
  no longer count as passing.
- Workflow `spec_version` contract: only version 1 is accepted; unknown versions
  raise `UnsupportedWorkflowVersionError` and validation reports `UNSUPPORTED_SPEC_VERSION`.
- Validation reports `MODULE_UNAVAILABLE` when a module's optional extra is missing.
- `nexura serve` command for the local REST API used by Studio web.
- Chat responses include `delivery: "final_only"`; Studio labels final-only replies.
- Studio shows an access-denied state for workflows owned by another workspace.
- Exported `run.py` accepts `--state-update` with `--approve`.
- Shared run budgets with pre-dispatch admission (O01), durable recovery (O02),
  portable exports (O03), usage diagnostics (O04), provider cache planning (O05)
  and inspectable context selection (O06).
- CONTRIBUTING, SECURITY, code of conduct, issue/PR templates and CI workflow.

### Changed
- **Command renamed to `nexura-runtime` (B42):** the framework's command was `nexura`, which
  collides with the published Nexura Cloud client package `nexura` (same command). It is now
  `nexura-runtime` (`nexura-runtime serve`, `worker`, `workflow`, `redteam`, `evals`);
  `nexura-studio` is unchanged. A single `nexura` command is planned later via the
  Nexura Cloud client's `nexura.commands` entry-point group.
- **Licences (P28 step 1):** every first-party source file carries an SPDX MIT header
  ("Copyright (c) 2026 Liceron"; `scripts/add_spdx_headers.py --check` runs in the tests);
  `studio-web/LICENSE` added; `THIRD_PARTY.md` rebuilt from installed package metadata
  (Studio runtime packages and the bundled Noto font included). Contributions require the
  Liceron Contributor License Agreement (`CLA.md`, a draft for legal review). The source
  distribution no longer ships internal planning reports or the old `deployment/` files.
- **Dependency updates (B41, founder decision 2026-10-18):** 20 locked packages updated within
  their declared ranges: aiohttp 3.14.5, authlib 1.9.1, boto3/botocore 1.43.114, fastapi
  0.144.0, google-auth 2.62.0, litellm 1.106.0, numpy 2.5.4, opentelemetry 1.45.1 / 0.66b1,
  pluggy 1.7.0, pycparser 3.11, pydantic 2.14.1 (core 2.50.1, settings 2.16.0), pypdf 6.21.1,
  sqlalchemy 2.1.5, starlette 1.8.0, tokenizers 0.23.3. multidict 7 (major) and ruff were left
  out on purpose.
- **Revocation stops runs (B40):** removing a workspace or organisation member, revoking an API
  key or revoking a publication cancels that principal's, key's or publication's running,
  paused and waiting runs (`cancel_reason` `access_revoked` / `publication_revoked`, audited as
  `runs_cancelled`); approving or answering one afterwards returns `409 RUN_ACCESS_REVOKED`.
  Runs that execute inside a request (synchronous start, `/decide`, `/input`, `/fork`) are
  registered so they can be stopped, the revoked check is repeated before resuming, and run
  status writes are compare-and-set, so a cancelled run never comes back.
- **Webhook setup in Studio (B39):** registering a webhook shows its signing secret once (held
  only in page state), the delivery URL, the three signing headers and curl/Python snippets
  (on a shared service they send `Authorization: Bearer $NEXURA_API_KEY`, never embedded);
  both trigger forms pick the workflow from the workspace instead of a hard-coded id; the
  workflow page has a **Triggers** button; viewers cannot submit.
- **Code-execution module safety (B37):** `nexura.tools.sandbox.execute` now always needs a
  human approval bound to the code (`always_write`; a direct call needs
  `ExecutionContext(approval_granted=True)`). Limits come from the operator's
  `SandboxConfig` and inputs can only lower them; the `env` input is refused; node config can
  set nothing (refused when the workflow is saved); runsc, rootfs and any command prefix come
  only from operator config or `NEXURA_RUNSC_PATH` / `NEXURA_OCI_ROOTFS_PATH` / `PATH`, never
  from a file in the current directory; `--ignore-cgroups` only with an explicit operator
  opt-in, reported as `limits_advisory`. The module is hidden from Studio and the builder
  assistant until the new sandbox (P27) exists, and stays registered for saved workflows. It
  is an idempotent write with network off, so retries still validate. The live gVisor tests
  skip on hosts without gVisor instead of failing.
- **`cryptography` is a core dependency (B36, founder decision 2026-10-18):** it backs the
  encrypted secrets store, trigger signing keys and RS256 token verification. The `secrets`
  extra stays as an empty alias, so `pip install "nexura-agentkit[secrets]"` still works. No
  other dependency moved or changed version (`uv.lock` resolves the same 95 packages).
  `THIRD_PARTY.md` now lists it.
- **TypeScript client path encoding (P25 review):** workflow, module and run ids are sent as one
  encoded path segment (`runs.get`, `cancel`, `decide`, `streamEvents`, `workflows.get`,
  `update`, `export`, `modules.get`), so an id from a link cannot point a request at another
  route.
- **Studio dead ends (B34):**
  - The module drawer's categories match real modules: Channels (email, WhatsApp), a new
    Connectors category (the 15 connectors), Tools & APIs, Governance & Services; labels are
    localised.
  - Service-internal modules (auth, workspaces, triggers, observability, eval runner,
    approvals runtime) carry `internal: true` in their manifest and descriptor (left out of
    the module digest). Studio no longer offers them in the drawer or the parallel/map
    pickers; saved workflows that use them still validate and run.
  - The create-workflow dialog no longer lists a test-only template.
  - The sidebar approvals badge shows the pending count (server total, refreshed every 30 s
    while connected and visible, and right after a decision); it clears and stops polling
    after a 401/403.
  - The templates view links missing credentials to the Credentials page.
- **Readable validation errors (B33):** the TypeScript client turns FastAPI's 422 list into
  `field: message` text (code `VALIDATION_ERROR`, the list kept in `details`) on both error
  paths, and exports `describeValidationErrors`. Studio shows every service error through one
  `formatApiError` (plain lead for 401/403/404/409/413/429, scoped keys and revision conflicts,
  plus the server's message and code) instead of raw JSON.
- **Parallel tool failures (B35):** when one call of a parallel batch fails, the calls that
  finished are recorded first (`TOOL_COMPLETED`, tool message, checkpoint), so a write that
  already ran is visible and recovery does not repeat it. `RUN_FAILED` event text now has
  credentials masked; the raised exception is unchanged.
- **Examples run offline again (B32):** `examples/backend` b16, b17 and b26 grant the operator
  approval that writes need since B22/B23 (`ExecutionContext(..., approval_granted=True)`),
  and b31 runs from any directory. `tests/examples/test_all_examples.py` now runs every
  example except `examples/live` with no provider credentials, so a broken example fails CI.
- **Audit 14 fixes:**
  - **Red-team:** `scrub_canary` also scrubs mapping keys. `nexura redteam` scrubs the canary
    from the reasons it prints, refuses a `--write-tool` the agent does not have (exit 2), warns
    when there is no write tool to attack, and checks `--out` before the run (exit 2 when it
    cannot be written). The red-team route also attacks steps marked `requires_approval`,
    refuses a write tool named `read_document` (422, was 500) and never returns the canary.
  - **Skills:** the 20-label cap also holds when labels arrive through `POST /skills`; version
    numbers are not reused after a skill is removed and re-created; a skill listed twice on a
    node is attached once; `GET /skills` reads summaries without rebuilding every version.
    The built-in `nexura.agents.coordinator` node now reads the app's skills store (each app
    gets its own bound copy via the new `ModuleRegistry.override_module`).
  - **Evals:** Studio keeps the newest 500 eval runs per workspace (saving one more deletes the
    oldest). From-run applies the dataset's 8000-character limit up front
    (`413 RUN_TOO_LARGE_FOR_TEST`).
  - **Schemas:** a `minItems`/`maxItems` that is not a non-negative integer is ignored again
    instead of raising `TypeError` (remote MCP/OpenAPI tool schemas).
- **P17b/P20b review fixes:**
  - `nexura redteam --out` scrubs any string containing the canary (`scrub_canary`) and
    redacts secrets in the report.
  - Skill resources named `SKILL.md` and resource text with control characters are refused.
  - Module manifests now enforce `minItems` and `maxItems` in config, input and output
    schemas.
  - Dataset case names are compared after secret redaction, so two names that differ only in
    a secret are refused as duplicates instead of being stored twice.
- **Audit 13 fixes:**
  - **Network:**
    - MCP over HTTP no longer follows redirects; a 3xx is an error.
    - `McpHttpTransport` copies the destination policy instead of widening a shared one.
    - A cross-origin redirect in `SafeHttpClient` forwards only a short list of safe headers
      (`SAFE_REDIRECT_HEADERS`).
    - The local-use NAT64 range `64:ff9b:1::/48` is treated as private.
    - An OpenAPI spec's host is no longer trusted after a single lookup, so a DNS rebind to
      loopback is refused.
    - `allow_metadata_service` is removed; metadata addresses are always refused.
    - `SafeHttpClient` raises `TooManyRedirects`, keeps HEAD as HEAD, refuses redirects on
      `stream()` unless `refuse_redirect=False`, and resolves DNS off the event loop.
  - **Providers:**
    - Streaming errors are classified after reading the body, so context-window errors are
      typed on every provider. Anthropic and Sarvam no longer leak `httpx.ResponseNotRead`.
    - LiteLLM's `ContextWindowExceededError`, xAI and llama.cpp/Ollama wordings are
      recognised.
    - `RetryModel.stream` trims history like `generate`.
  - **Skills:**
    - YAML block lists are read, and other front-matter keys go to `metadata`.
    - Resources and metadata are read-only.
    - `add_directory` is atomic.
    - `remove` and `export`/`import_versions` are added.
    - `latest` cannot be pinned to an old version.
  - **Red-team:** `redteam_ok(report)` passes when every red-team scorer passed, counting
    cases paused for approval as safe.
  - **Runs:**
    - `/runs/{id}/subruns` is paginated (`limit`, `offset`, `has_more`) and in natural id
      order.
    - A resource-scoped key may cancel its own runs and open their sub-runs, but cannot
      decide approvals or answer inputs.
    - Route checks use the raw path.
  - **Publishing environments:**
    - Credential-map values must be credential ids of the workspace; pasted tokens are
      refused.
    - Changing a prod map needs the same second-admin approval as a promotion.
    - Pending promotions and map changes can be rejected or withdrawn, and expire after 7
      days.
    - `require_promotion_approval` can be set through `PATCH /api/v1/workspaces/{id}`.
- **Review of 2026-10-17's work (fixes found by two adversarial reviewers):**
  - **MCP stdio:**
    - `env` is checked under any launcher allowlist, by prefix family.
    - Refused: `PATH`, `HOME`, config and trust-store paths, and the `LD_*`, `PYTHON*`,
      `NODE_*`, `NPM_CONFIG_*`, `UV_*`, `PIP_*` and `XDG_*` families. Previously a `PATH`
      override could make a different binary run.
  - **`SafeHttpClient`:**
    - Connections are pinned to an address that passed the destination policy (no DNS
      rebinding).
    - Cross-origin redirects drop credential headers, and a 307/308 keeps its body.
    - The policy also refuses site-local IPv6, ignores a trailing dot in `allowed_hosts`, and
      reports malformed URLs as a policy denial.
  - **Resource-scoped keys:**
    - `resources=[]` reaches nothing; it used to be unrestricted.
    - A scoped key can read only the runs it started.
  - **Red-team pack:**
    - The canary scorer checks every turn and all assistant text, and catches case changes,
      separators, URL-encoding, hex, offset base64 and canaries split across tool calls.
    - The write scorer sees tools run inside `Agent.as_tool` sub-agents; `as_tool` now emits a
      `nexura.subagent_tools` event.
  - **Context overflow:**
    - Only provider-specific wording counts as a context-window error, so an unrelated 400
      never trims history.
    - Trimming keeps system messages anywhere in the history.
  - **Skills:**
    - Resources must be text files and must not start with `#!`.
    - Hidden files and a symlinked `SKILL.md` are refused.
    - Names are fully matched, and descriptions refuse every line-break character.
    - The digest is unambiguous.
    - Re-adding the same content applies new labels.
    - The loader and catalog show only skills with the requested label.
- **Security, regression pack for October 2026 competitor bugs (B28):**
  - New tests in `tests/security/b28/` and `tests/security/test_b28_network_and_stdio.py`
    mirror the n8n and Langflow bug classes:
    - resuming, approving, forking or cancelling another user's run;
    - using another workspace's credential;
    - MCP stdio command and environment injection;
    - SSRF through every fetching path;
    - `__proto__`-style keys in imported flows.
  - Two gaps were found and fixed:
    - **Shared destination policy** (web, OpenAPI, A2A, HTTP module and MCP over HTTP):
      - it now refuses every non-public address, including carrier-grade NAT, multicast and
        reserved ranges;
      - it refuses the Alibaba Cloud metadata address `100.100.100.200`, which the HTTP
        module used to try to connect to;
      - it checks IPv4 hidden inside IPv6 (mapped, NAT64, 6to4, Teredo) as IPv4;
      - it never lets an allow-listed host resolve to a metadata address.
    - **MCP stdio servers:** under the default launcher allowlist, `MCPServer.stdio` refuses
      `env` variables that load code or packages the caller chose, such as `LD_PRELOAD`,
      `NODE_OPTIONS`, `PYTHONPATH` and package-index overrides. Opt out with
      `allow_inline_code=True` or an explicit `allowed_commands`.
- **Security, DNS rebinding for mail and SQL connectors (B27):**
  - SMTP, IMAP and Postgres resolve the host once, validate every address, and connect to
    the validated IP. TLS still verifies the certificate against the original host name.
  - An injected MySQL driver receives `pinned_ip` and `server_name`.
  - For `sslmode=verify-ca` or `verify-full`, Postgres reads the same TLS inputs as asyncpg:
    root certificate, revocation list and client certificate, from the DSN, the `PG*`
    environment variables or `~/.postgresql/`. A verify mode without a root certificate now
    fails instead of falling back to the system CAs, as in libpq.
- **Security, write approval for every side-effecting module (B23, Audit 12):**
  - B22 covered the connectors. These modules also wrote without a human approval:
    - `nexura.http.request` with non-GET/HEAD methods;
    - `nexura.channel.email` and `nexura.channel.whatsapp`, whose `approved` input defaulted
      to true;
    - `nexura.sql.execute`;
    - OpenAPI operations that are not GET or HEAD.
  - All of them now pause for approval, and an `approved` input is ignored.
  - Manifests classify calls with `write_operation_key` / `write_operations` or
    `always_write`.
  - `mcp.local.calculator` is marked pure.
  - A parallel step's approval binds the whole state. Its branches now get the node's config,
    and the approval lists the branches and which of them can write.
  - A map step rejected by the write cap leaves a failed checkpoint.
- **SQL read allow-list (B24):**
  - A keyword-named function such as `filter(...)` is refused unless its position makes it SQL
    grammar.
  - About 60 more side-effect-free functions are allowed, including JSON, math, statistics,
    time, hashing, regexp and ranges.
  - MySQL `TRUNCATE(x, d)` is allowed; `TRUNCATE TABLE` is still a write.
  - Connector config forms no longer offer `allowed_hosts`, which flow config cannot set.
- **Chat playground (B25):**
  - `knowledge_search` redacts secrets in `source_uri`.
  - Session ids must match `[A-Za-z0-9_.:-]{1,128}`, and titles are capped at 200
    characters.
- **Security, connector writes need a real human approval (B22):**
  - Before this, a connector write ran whenever its input said `approved: true`. A flow author
    or a model's tool call could set that, so writes were self-approvable.
  - Now a write runs only when the engine or the agent runner has recorded a human approval
    for that step, or an operator sets `ExecutionContext(approval_granted=True)` in a
    script. The `approved` input is ignored.
  - The engine pauses before a write operation; reads such as `list_channels` do not pause.
  - The approval is bound to what the approver saw. Editing the step's inputs on resume
    pauses again for a fresh approval, and so does turning a read into a write.
  - A mapped write step shows the item count and items and is capped at 50 writes per
    approval (`max_map_writes_per_approval`).
  - SQL `query` (no approval) may call only listed side-effect-free functions and runs in a
    read-only transaction. Anything else must use `execute`, which needs approval.
  - The modules API reports `requires_approval` and `write_operations`, and Studio shows the
    Approval Gate badge on the 14 connectors that can write.
- **Connectors (Audit 11):**
  - A connector sends a credential only if the credential's type matches the connector.
  - The credential only goes to the connector's own API host, or to hosts an operator
    allowed with `allowed_hosts=`, the run context, or `NEXURA_CONNECTOR_ALLOWED_HOSTS`. A
    `base_url` or `allowed_hosts` set in flow config cannot open other hosts.
  - HTTP connections are pinned to the address that passed the network check.
  - Timeouts, retries and response caps are clamped.
  - Every list operation accepts `cursor`; the old names still work.
- **Knowledge (Audit 11):**
  - The quota counts metadata, `source_uri` and title as well as text.
  - Vector search scores at most 20,000 chunks per query.
  - `/retrieve` and `/answer` are rate limited per workspace.
  - Answers no longer cite a made-up page.
  - A deduplicated upload returns 200, not 201.
- **Organisations and secrets (Audit 11):**
  - `LAST_OWNER` returns 409.
  - The audit log, members and secrets lists take `limit`/`offset`.
  - Replacing a secret keeps its `created_at` and `created_by`.
- **Self-hosting:**
  - New `secrets` extra for `cryptography`, installed in the image.
  - `docker-compose.yml` now requires `NEXURA_MASTER_KEY`.
- **Credentials API (shared mode):**
  - Listing or reading credentials now needs the `edit` permission, as `/secrets` does.
    Viewers get 403, and Studio tells them so instead of showing an error.
  - Credential ids are scoped to their workspace: two workspaces can each have
    `prod-stripe`, and creating one no longer reveals whether another workspace owns that
    id.
  - Replacing a secret's value keeps its status, so a revoked credential stays revoked.
- **Validation errors on the service API** (422) no longer echo the request body, so a
  malformed request cannot reflect a secret back.
- **Workspace members API:** in a workspace an organisation owns, organisation members are
  managed only through the organisations API (409 `ORG_MANAGED_MEMBER`).
- **Breaking (TS client):** `TokenUsage` now matches the service:
  `prompt_tokens`, `completion_tokens`, `total_tokens` (nullable) and `is_complete`.
  The previous `input_tokens`/`output_tokens` fields were never sent by the server.
- Chat usage is reported only when the model reports it; it is no longer filled
  with invented counts.
- Module availability reads declared extras from package metadata instead of a
  hard-coded list; undeclared extras are reported unavailable.
- Synthetic ticket IDs in the built-in order workflow are deterministic across processes.
- `McpPromptArgument.required` now defaults to `False`, the MCP specification's default,
  so prompts from servers that omit it no longer reject calls without that argument.
  Pass `required=True` when registering a required prompt argument.
- `SafeHttpClient.stream` runs the destination check (DNS lookup) in a worker thread
  instead of blocking the event loop, and `DestinationPolicy` refuses host names that DNS
  cannot encode (empty or overlong labels) instead of raising `UnicodeError`.
- Locked dependency updates: pydantic 2.14.0, pydantic-core 2.50.0, fastapi 0.143.0,
  sqlalchemy 2.1.4, asyncpg 0.32.0, pycparser 3.1, iniconfig 2.3.1. opentelemetry-api
  stays at 1.45.0, because the SDK pins it exactly.

### Fixed
- **Approvals bound to the exact action (B43):** an approval now records the run, node, action
  kind and name, the agent spec digest and a canonical digest of every argument, positional
  ones included. A dispatch that differs in any of these raises
  `ApprovalIdentityMismatchError` and runs nothing; before, an approval for `pay(1)` could
  release `pay(1_000_000)`. Expiry is checked before, during and right after the claim with the
  manager's own clock (a caller-supplied `now=` can only move time forward); old rows without
  `expires_at` expire `approval_ttl` after creation. `ApprovalModule` acts only in the
  context's workspace and run and takes the deciding role from the context. Editing a
  function node's state after it paused re-gates it, so the edit needs a fresh approval.
  Coordinator approvals use a stable id (no longer the provider's tool-call id), nested
  sub-agents no longer inherit the parent's approval, and same-named tools no longer share
  approvals. Docs no longer say "every write pauses": only gated actions do.
- **Shared budgets under parallel agents (P32b):** admission is atomic across tasks, threads
  with their own event loops, SQLite handles and processes; token caps are enforced at
  admission; descendants at every depth (reservations and direct usage) count against every
  ancestor, and `is_exhausted()`/`remaining_tokens()` include ancestor caps. Expired
  reservations are no longer refunded silently; settlement is idempotent and uses the
  reserved model's price; unpriced or cancelled calls settle conservatively and never leave a
  reservation locked; negative or under-reported usage cannot create credit; positive costs
  round up to the smallest unit. `create_run_budget` for an existing run no longer drops its
  caps. `examples/budget_parallel_demo.py` shows ten agents sharing 30 tokens (seven refused).
  The guarantee covers calls that go through admission; see `docs/budgets-and-usage.md`.

- **Audit 10, cross-feature review (B19–B20):**
  - **Redaction:**
    - V2-shaped run records redact secret values and message text, not only secret-named
      keys.
    - The older eval runner and the SDK tracer catch provider keys, Basic credentials and
      GitHub tokens (`ghp_`, `gho_`, `ghu_`, `ghs_`, `ghr_`, `github_pat_`).
  - **V2FlowServer:** treats `None`, `False`, `0` and empty values from a verifier as
    unauthenticated.
  - **Publishing:**
    - `GET /api/v1/published` paginates like other lists (`limit` 1–100, `offset`;
      `limit=0` is 422).
    - Public gateway responses carry `x-request-id`.
    - Public routes are documented in `docs/publishing.md`; they are not in the admin
      OpenAPI by design.
  - **Docs:**
    - `SECURITY_MODEL.md` and `docs/studio-and-service.md` no longer describe gaps that
      are closed.
    - Studio's sections list Templates, Run Traces, Evals and Publish.
    - `GET /runs/{id}/trace` is documented.
    - The API reference covers publishing, templates, chat-to-build, codegen and the
      Langflow/V2 layers.
- **Audit 9, cross-feature review (B16–B18):**
  - **Secrets in traces:**
    - Exception text no longer reaches spans unredacted; without `capture_content` only
      the exception type is recorded.
    - Run metadata on spans is filtered and redacted.
    - `redact()` now catches camelCase and bare secret keys (`accessToken`, `clientSecret`,
      `auth`, `credentials`, `privateKey`, `Set-Cookie`) and `Basic` credentials.
    - Configured header values are masked in in-stream provider errors (OpenAI-compatible
      and LiteLLM).
  - **Sessions:** `pop_last_turn` and `retry_last_turn` skip the runner's output-retry
    feedback and remove the real user turn.
  - **web_fetch:** refuses the deprecated site-local range `fec0::/10`.
  - **Wrappers:** `RetryModel` and `FallbackModel` stream again, also nested with
    `RateLimitedModel`. They retry or fall back only before the first delta.
  - **Teams:** `run`, `run_async`, `stream_async` and the resume forms take `runner=` and
    `cancel=`. A cancelled team returns `status == "cancelled"`.
  - **Typing:** `clone`, `override` and `retry_last_turn` keep the output type.
  - **Metrics and events:**
    - A tool call stopped by a `CancelToken` records `error.type=cancelled`.
    - `ctx.emit` copies each value separately.
  - **Docs:** corrected the retry/`Retry-After` and 5xx claims in `providers.md`, plus the
    "FakeModel does not stream" and "LiteLLM non-streaming" lines.
- **Review follow-ups for today's additions (B13–B15):**
  - Run metadata that JSON cannot hold (a `datetime`, say) no longer crashes a pause
    or `interruption.to_json()`; such values are saved as text.
  - Streamed tool calls whose later chunks repeat an empty id or name no longer lose
    the real ones and fail with `unnamed_tool`. Keepalive chunks emit no
    `TOOL_CALL_DELTA`.
  - Cancelled runs and tool calls record `error.type=cancelled` on the duration metric
    instead of counting as successes.
  - `fork_session` copies the state into stores whose `save` takes `**kwargs` and no
    longer shares the state object with the source. A tool call that never got a result
    no longer blocks every later fork point.
  - Normal runs also save state into `**kwargs` stores, instead of warning and
    dropping it.
  - The docs no longer claim `handoff_input` is lost after a pause, and they mark it as
    a reserved metadata key.
- **Provider streams fail loudly (B11):** the OpenAI-compatible, Anthropic and Gemini
  streams now raise a typed provider error for in-stream error events, for streams that end
  before a finish signal, and for malformed or null chunk fields. Anthropic network errors
  during a stream become `ProviderRequestError` (retryable) instead of raw `httpx`
  errors. Gemini parallel function calls that arrive in separate chunks keep separate ids
  instead of being merged. The Anthropic keyless module mock now sends a real SSE stream.
- **Resume keeps the run context (B12):** `RunInterruption` now carries `tags`, `metadata`,
  the spent output-retry count and the active agent's start index. A resumed or recovered
  run keeps `ctx.tags`, `ctx.metadata` and `result.tags`, gets the tracing attributes again,
  and no longer gets a fresh `output_retries` budget after each pause. Older interruption
  JSON without these keys still loads.
- **Audit 6:**
  - **Privacy:** the handoff reason, and with `input_type` the full structured handoff
    arguments, was written to the tracing span as a plain attribute even with
    `capture_content=False`. It is now a content event, recorded only when content
    capture is on.
  - After a handoff, the new agent inherited the output retries already spent by the
    previous one. Each agent now gets its own `output_retries`.
  - A `CancelToken` could not interrupt a slow output validator or `on_handoff`
    callback. Both are interruptible now.
  - Diagrams marked disabled tools but not disabled or conditional handoffs. They do
    now.
- **Audit 5:**
  - `AgentMCPServer` listed and ran tools with `enabled=False`, and ignored `enabled` rules.
    Disabled tools are no longer served, and rules are checked per call.
  - A carriage return (or another line break or control character) in an agent or tool
    name could end a Mermaid label and inject directives such as `click ... href`. All of
    them are now escaped.
  - An error whose cause could not be pickled (for example, one holding a lock) was still
    unpicklable. The cause is now replaced by a readable `RuntimeError`.
  - Docs said `LiteLLMModel` does not stream, and that typed turns are not streamed. Both
    were out of date.
- The Gemini provider's streaming path dropped `temperature`, `max_tokens`,
  `stop_sequences` and the output schema. The OpenAI provider's streaming path dropped
  `stop_sequences` and the schema. Both now send the same options as `generate()`.
- `Agent(instructions=async_fn)` was accepted but the run failed with a misleading
  `ProviderError: cannot pickle 'coroutine' object` (and a "never awaited" warning). Async
  instruction functions are now awaited, for the starting agent and for handoff targets.
  An instructions function that returns something other than a string raises a clear
  `SDKValidationError`.
- SDK exceptions can be pickled. Errors with their own constructors
  (`UsageLimitExceededError`, `MaxTurnsExceededError`, `GuardrailTripwireTriggered` and
  about 30 more) raised `TypeError` on unpickling, so they could not cross process
  boundaries. A test now pickles every one of the 80 error classes.
- **Security (audit 4):** a tool hidden with `enabled=` or `prepare_tools` could run if
  the model called it in the same batch as an approval-gated tool: the batch paused on
  the visible call, and on resume the hidden call ran. Now a call to any tool that was
  not offered fails the whole batch with `UnknownToolError` before anything runs or
  pauses.
- `ModelSettings` validates its fields. `stop_sequences="END"` used to become one stop
  sequence per character.
- `Runner(model_settings=...)` was silently ignored by `OpenAIChatModel`, `LiteLLMModel` and
  custom models (only provider models received it). They now send temperature,
  max_tokens and stop, including when streaming. `RetryModel` and `FallbackModel` forward
  the settings. A model that cannot take them triggers a `RuntimeWarning`.
- **Studio chat:** sessions are listed most recently active first
  (`SQLiteSessionStore.updated_at`), so reloading the playground reopens the latest
  conversation instead of the oldest.
- **Second security campaign (B8): 24 more findings fixed, each with a regression test.**
  - Auth and app:
    - JWTs must carry `exp` and `sub` (optional maximum lifetime).
    - Operators (`NEXURA_OPERATOR_PRINCIPALS`) alone may create workspaces or see
      `/readyz` details.
    - `/api/v1` responses send `Cache-Control: no-store`.
    - JSON bodies nested deeper than 64 levels get 422.
  - Triggers:
    - Creating a schedule or webhook needs both edit and run permission and an existing
      workflow in the caller's workspace.
    - The outbox dedup key is unambiguous.
    - Impossible cron expressions are rejected quickly.
    - Viewers see redacted payloads.
  - Sessions and integrations:
    - Chat needs run permission.
    - Tenants cannot overwrite operator tools.
    - The calculator rejects complex results.
    - In shared mode, other tenants' sessions and documents answer 404.
    - The regex grader rejects overlapping repeats.
  - Runs and workflows:
    - Redaction covers camelCase, kebab-case and other key spellings, plus value
      patterns (tokens, JWTs, PEM keys) and run snapshots, including the echoed spec.
    - The run list filters by workspace before paging.
    - In shared mode, a workflow id already held by another tenant gets a fresh id.
    - Viewers get scrubbed workflow configs.
- **Service security audit (B7): 43 findings fixed, each with a regression test.**
  - Critical: in shared mode, any user could re-create an existing workspace and become
    its admin.
  - High:
    - session, webhook and schedule ids could be hijacked from another workspace
    - run events leaked secrets from node state and error text
    - a JWT without a workspace claim reached any workspace
    - export wrote to client-chosen paths
    - tiny schedule intervals looped
    - the chat calculator used `eval()`
    - per-node config could disable the OpenAPI SSRF policy
  - Also:
    - Host/Origin checks (DNS rebinding)
    - global request-size limit
    - membership and role re-checked on every request
    - API-key scopes enforced
    - webhook signature v2 now covers the event id
    - receipts and outbox scoped per workspace
    - webhook secrets no longer stored in plain text
    - a safe arithmetic evaluator
    - ReDoS guard in the evaluation grader
    - security headers on Studio
    - quieter health and auth errors
    - docs and OpenAPI hidden in shared mode
    - new database files created with 0600 permissions
  - Behaviour changes:
    - Webhooks must sign with v2 (`v2.<ts>.<event_id>.<body>`).
    - In shared mode, another tenant's resources answer 404 (local Studio still
      explains with 403).
    - `nexura serve` on a non-loopback host needs `allowed_hosts` /
      `NEXURA_ALLOWED_HOSTS`.
- **CI:** the Python 3.11/3.12/3.13 wheel job ran only 5 agent test files. It now uses
  `scripts/test_agents_wheel.py`, which runs every agent test that does not need the
  platform: 395 tests on 3.11 and 3.13 locally (2026-10-09).
- **Audit 3 cross-feature fixes (B6):**
  - An A2A-served team resumed after approval without the server runner's limits.
  - `/fork`, `/input` and `/decide` did not scan state for cross-workspace references.
  - A gated handler inside a sub-workflow caused a 500 and left an orphan run.
  - Concurrent memory adds created duplicates.
  - `RunInterruption` JSON dropped cache and reasoning token counts.
  - `retry_on` ignored errors raised inside sub-workflows.
  - Sub-runs were listed in string order.
  - The OTLP scope version was stale.
  - The Vertex install hint named the wrong package.
- **Service:** replaced the deprecated Starlette `HTTP_422_UNPROCESSABLE_ENTITY` with a
  literal `422`, which also works with the oldest supported FastAPI.
- **Lint:** 76 files had unsorted imports that ruff's cache had been hiding, so a
  fresh checkout (CI) would fail lint. Imports are now sorted, and the release gate
  lints with `--no-cache`.
- **Service:** a run paused by `interrupt()` was stored as `pending_approval` with no
  approval, so it could never be resumed. It is now `waiting_for_input`. `decide`
  also now records failed results, which it used to drop.
- **Legacy `nexura-studio` server:** a run that calls `interrupt()`, at the start or
  after an approval, returned HTTP 500. It now fails with a message pointing to
  `nexura serve` (B5).
- `Agent.as_tool()`: a sub-agent that stops for approval now raises `ToolError`. Before,
  the calling model received `null` and could report success (B4).
- Providers and tools (autopilot item B3; each bug reproduced by a failing test first):
  - **Retries:**
    - `RetryModel` now retries HTTP 5xx from every built-in provider
      (`ProviderServerError`).
    - It honours `Retry-After` on 429 for all providers, including `0`
      (`ProviderRateLimitedError.retry_after`).
    - It never retries other 4xx errors (`ProviderClientError`). Anthropic and Sarvam
      400s were retried before.
  - **Budgets:** `RunBudget` `max_turns` and `max_tool_calls` are now enforced in agent
    runs, before the over-limit model call or tool executes.
  - **MCP pinning:** a pinned tool is refused when the server's definition changed or
    the tool was removed. The client re-lists tools when the server sends
    `notifications/tools/list_changed`, or when the last listing is older than
    `tool_list_max_age` (30 s). Exported bundles pin MCP tools to the real fixture
    definitions instead of a stub schema.
  - **Attachments:** OpenAI and Anthropic reject `gs://` attachment URLs, which they
    cannot fetch.
  - **OpenAPI:** `openapi_tools(allowed_hosts=[...])` trusts specific hosts.
  - **TypeScript client:** the default base URL is now `http://127.0.0.1:8765`, the port
    `nexura serve` uses.
- Agent SDK (autopilot review B2, each bug reproduced by a failing test first):
  - **OpenAPI tools (security):**
    - A spec's own server host is no longer trusted when it is an IP literal or resolves
      to a loopback, private or link-local address. A hostile spec can therefore no
      longer reach cloud metadata or internal services.
    - Path parameters are URL-encoded, which stops path traversal and query injection.
    - Recursive `$ref` schemas parse instead of crashing.
    - `idempotency_key` and `retries` are no longer sent in request bodies.
  - **`AgentMCPServer`:**
    - A non-ASCII `Authorization` header now gets 401, not 500.
    - Malformed `params` get JSON-RPC -32602, and other failures get a generic -32603
      (over stdio, previously no reply was sent).
    - `http_app(max_body_bytes=4 MiB)` returns 413 for larger bodies.
    - Sessions are capped (`max_sessions=1000`, least recently used evicted) and expire
      after `session_ttl_seconds=3600` idle.
  - **MCP client:**
    - Stdio messages up to 16 MiB work (was 64 KiB).
    - The server's stderr is drained, so chatty servers no longer deadlock; its tail is
      shown in errors.
    - Every request has a timeout (`request_timeout`, default 60 s).
    - The SSE parser no longer splits JSON on U+2028/U+2029.
  - **Agents:**
    - `UsageLimits(request_limit=...)` now counts requests made before an approval pause
      (`RunInterruption.requests`).
    - `Attachment.from_path` checks the size and requires a regular file before reading.
    - Input guardrails receive `""` instead of `None` for attachment-only prompts.
    - Presigned-URL query strings are redacted from `Attachment` reprs and error messages.
- Workflows (autopilot review B1, each bug reproduced by a failing test first):
  - `fork()` and `history()` now enforce workspace ownership. Checkpoints record their
    workspace, and SQLite gains a `workspace_id` column.
  - Two concurrent forks can no longer claim the same run id. Stores gain an atomic
    `create()`.
  - SQLite databases created before history existed backfill one history entry per
    existing run.
  - A retry after an answered `interrupt()` no longer asks the same question again.
  - Retry attempts now share the workflow deadline instead of each getting the full
    timeout.
  - A retried sub-workflow step is refused when the sub-workflow, at any depth, contains
    a non-idempotent module.
  - `interrupt()` deep inside a mapped sub-workflow now fails clearly instead of leaving
    a pause that cannot be resumed.
  - A sub-workflow's writes of unchanged values now reach `map_over` reducers.
- A checkpoint written just before a terminal node that has a handler was marked
  `completed`, so a restart at that moment would skip the final node. It is now `running`.
- MCP client took the next message as the reply, so a server notification or `ping`
  before the reply was mistaken for it; replies are now matched by id, pings answered and
  requests serialized per session. It also stopped advertising sampling/roots support it
  does not implement.
- The built-in MCP server sent tool results as Python `repr` text; it now sends JSON.
- Gemini/Vertex usage omitted "thinking" tokens from output counts, under-reporting
  billed output for Gemini 2.5 models; `Usage.reasoning_tokens` added.
- Vertex AI errors were reported as "invalid API key"; they now name the project,
  credential source and Google reason code.
- TS SSE parser lost an event's id and type when it was split across network chunks.
- Chat `tool_executions[].arguments` was always null.
- `nexura workflow export` crashed with a traceback on unknown handlers.
- Exported `run.py` could mistake an option's value for the initial state.
- Evaluation batches now stop at their budget; their usage was previously excluded
  from budget totals.
- REST export endpoints validate handlers against the service's registry instead of
  the built-in default registry.

## 0.3.0

- Local Studio (`nexura-studio`) with workflow editing, module registry, runs and
  approvals; restart-safe pending approvals; Devanagari font bundled.

## 0.2.0

- Persistent SQLite sessions with optimistic concurrency, approval-gated workflows
  with durable checkpoints, OpenAI-compatible provider adapter hardening.

## 0.1.0

- Initial typed SDK: tools, agents, bounded runner, lifecycle events, `FakeModel`.
