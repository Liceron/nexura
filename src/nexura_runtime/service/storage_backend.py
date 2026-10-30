# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Service store construction boundary.

WP0 supplies the shared Postgres database and factory seam; WP2–WP8 ported every service store:
workspaces, ownership and membership (WP2); Studio workflows, runs and run events, workflow state
and triggers (WP3); chat sessions, saved agents and skills (WP4); encrypted secrets, the usage
ledger and the readiness probe (WP5); the closed edition's organisations and publications (WP6);
the knowledge index (WP7); evaluations, Studio evals, channel dedup keys and outbox receipts, and
integration tool registrations (WP8). A Postgres ``database_url`` therefore runs the whole
service without any SQLite file.

Two optional features still keep their rows in SQLite and are refused with a ``database_url``
(``SQLITE_ONLY_FEATURES``): the remote single-operator mode's webhook delivery tokens
(``serve --auth token``) and failed-run repro capture (``NEXURA_CAPTURE_FAILED_RUNS``). Neither is
part of the shared deployment. ``NEXURA_ALLOW_PARTIAL_POSTGRES=1`` lets them use the SQLite file
next to Postgres for development and tests only (refused on Cloud Run).
"""

from __future__ import annotations

import asyncio
import builtins
import os
import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any, Protocol, TypeVar

from nexura_runtime.blobs.payloads import DEFAULT_OFFLOAD_BYTES, BlobPayloads
from nexura_runtime.blobs.url import (
    BLOB_URL_ENV,
    BlobLocation,
    open_blob_store,
    parse_blob_url,
    resolve_blob_url,
)
from nexura_runtime.service.cloud import on_cloud_run
from nexura_runtime.service.storage_protocols import (
    AgentBackend,
    SessionBackend,
    SkillsBackend,
    StudioEvaluationBackend,
)

T = TypeVar("T")


class DatabaseProbe(Protocol):
    def ping(self) -> bool: ...


class IntegrationToolTakenError(ValueError):
    """A registration names a tool id that another workspace already holds (nothing stored)."""

    def __init__(self, module_id: str) -> None:
        super().__init__(f"Integration tool id '{module_id}' is already in use.")
        self.module_id = module_id


class IntegrationRegistrations(Protocol):
    blocking_io: bool

    def list(self) -> list[tuple[str, str, str, str | None, str]]: ...

    def save(
        self,
        workspace_id: str,
        module_ids: builtins.list[str],
        kind: str,
        fixture_id: str | None,
        server_name_or_prefix: str,
    ) -> None: ...

    def delete(self, workspace_id: str, module_ids: builtins.list[str]) -> builtins.list[str]: ...


async def store_call[R](store: Any, fn: Callable[..., R], /, *args: Any, **kwargs: Any) -> R:
    """Call ``fn`` (a method of ``store``) from async code: in a worker thread when the store
    waits on the network or a lock (``blocking_io``: the Postgres stores), inline otherwise
    (SQLite and in-memory stores, unchanged)."""
    if getattr(store, "blocking_io", False):
        return await asyncio.to_thread(fn, *args, **kwargs)
    return fn(*args, **kwargs)


def resolve_database_url(database_url: str | None) -> str | None:
    url = database_url if database_url is not None else os.environ.get("NEXURA_DATABASE_URL")
    if not url:
        return None
    from nexura_runtime.service.storage_url import normalize_database_url

    return normalize_database_url(url)


#: The SQLite readiness query: read-only, and it reads the file (the schema page).
SQLITE_PROBE_SQL = "SELECT count(*) FROM sqlite_master"


class SQLiteProbe:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)

    def ping(self) -> bool:
        # A readiness request must never create a database, table or row.
        path = Path(self.path).resolve()
        # Standalone health checks historically accept an uninitialized SQLite path.
        # Probe the SQLite engine in memory in that case, leaving the path untouched.
        if self.path == ":memory:" or (path.parent.is_dir() and not path.exists()):
            with closing(sqlite3.connect(":memory:")) as conn:
                return bool(conn.execute("SELECT 1").fetchone() == (1,))
        uri = path.as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=3)) as conn:
            # Reads the schema page (MVP WP5): ``SELECT 1`` never touches the file, so an
            # unreadable or corrupt database would still look healthy. Read-only, no growth.
            row = conn.execute(SQLITE_PROBE_SQL).fetchone()
            return bool(row is not None and isinstance(row[0], int))


class SQLiteIntegrationRegistrations:
    """Persisted fixture tool registrations in the SQLite file (``nexura_postgres.
    PostgresIntegrationRegistrations`` in Postgres mode). Nothing is created until the first
    save; restored into the module registry when the service starts."""

    blocking_io = False

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)

    def list(self) -> list[tuple[str, str, str, str | None, str]]:
        if self.path == ":memory:" or not Path(self.path).exists():
            return []
        with closing(sqlite3.connect(self.path, timeout=5)) as conn:
            if not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND "
                "name='nexura_service_integration_registrations'"
            ).fetchone():
                return []
            return conn.execute(
                "SELECT workspace_id, module_id, kind, fixture_id, server_name_or_prefix "
                "FROM nexura_service_integration_registrations"
            ).fetchall()

    def save(
        self,
        workspace_id: str,
        module_ids: builtins.list[str],
        kind: str,
        fixture_id: str | None,
        server_name_or_prefix: str,
    ) -> None:
        """Store (or replace) the workspace's registrations of ``module_ids`` in one transaction.

        Raises ``IntegrationToolTakenError`` (nothing stored) when another workspace holds one
        of the ids.
        """
        if self.path == ":memory:" or not module_ids:
            return
        with closing(sqlite3.connect(self.path, timeout=5, isolation_level=None)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._save(conn, workspace_id, module_ids, kind, fixture_id, server_name_or_prefix)
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")

    def delete(self, workspace_id: str, module_ids: builtins.list[str]) -> builtins.list[str]:
        """Delete the workspace's registrations of ``module_ids``; returns the ids deleted."""
        if self.path == ":memory:" or not module_ids or not Path(self.path).exists():
            return []
        ids = sorted(set(module_ids))
        marks = ",".join("?" * len(ids))
        with closing(sqlite3.connect(self.path, timeout=5, isolation_level=None)) as conn:
            if not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND "
                "name='nexura_service_integration_registrations'"
            ).fetchone():
                return []
            conn.execute("BEGIN IMMEDIATE")
            try:
                found = [
                    str(r[0])
                    for r in conn.execute(
                        "SELECT module_id FROM nexura_service_integration_registrations "
                        f"WHERE workspace_id = ? AND module_id IN ({marks}) ORDER BY module_id",
                        (workspace_id, *ids),
                    ).fetchall()
                ]
                conn.execute(
                    "DELETE FROM nexura_service_integration_registrations "
                    f"WHERE workspace_id = ? AND module_id IN ({marks})",
                    (workspace_id, *ids),
                )
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        return found

    @staticmethod
    def _save(
        conn: sqlite3.Connection,
        workspace_id: str,
        module_ids: builtins.list[str],
        kind: str,
        fixture_id: str | None,
        server_name_or_prefix: str,
    ) -> None:
        import time

        conn.execute("""CREATE TABLE IF NOT EXISTS nexura_service_integration_registrations (
            workspace_id TEXT NOT NULL, module_id TEXT NOT NULL, kind TEXT NOT NULL,
            fixture_id TEXT, server_name_or_prefix TEXT NOT NULL, created_at REAL NOT NULL,
            PRIMARY KEY (workspace_id, module_id))""")
        ids = sorted(set(module_ids))
        marks = ",".join("?" * len(ids))
        taken = conn.execute(
            "SELECT module_id FROM nexura_service_integration_registrations "
            f"WHERE module_id IN ({marks}) AND workspace_id <> ? ORDER BY module_id LIMIT 1",
            (*ids, workspace_id),
        ).fetchone()
        if taken is not None:
            raise IntegrationToolTakenError(taken[0])
        conn.executemany(
            "INSERT OR REPLACE INTO nexura_service_integration_registrations "
            "(workspace_id, module_id, kind, fixture_id, server_name_or_prefix, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (workspace_id, mid, kind, fixture_id, server_name_or_prefix, time.time())
                for mid in ids
            ],
        )


#: Development/test switch: let a SQLite-only feature (``SQLITE_ONLY_FEATURES``) keep its rows
#: in the SQLite file next to a Postgres ``database_url``. Every service store is on Postgres
#: (MVP WP8), so the switch is needed only for those features, never for a deployment.
PARTIAL_POSTGRES_ENV = "NEXURA_ALLOW_PARTIAL_POSTGRES"
#: Optional features whose rows have no Postgres store (design §1 rows #20 and #21; neither is part
#: of the shared deployment). With a ``database_url`` they are refused unless the switch is set.
SQLITE_ONLY_FEATURES = {
    "delivery_tokens": (
        "remote single-operator mode (serve --auth token) keeps its webhook delivery tokens"
    ),
    "repro_capture": (
        "failed-run repro capture (NEXURA_CAPTURE_FAILED_RUNS) keeps its repro bundles"
    ),
}
#: Keys every instance of a Postgres service must share (MVP WP9): the master key that wraps the
#: encrypted credentials and the key that encrypts webhook signing secrets, both stored in the
#: shared database. A key generated per instance would make those rows unreadable elsewhere.
SHARED_KEY_VARIABLES = {
    "NEXURA_MASTER_KEY": "encrypts the stored credentials",
    "NEXURA_TRIGGER_SECRET_KEY": "encrypts the webhook signing secrets",
}


class SharedKeyMissingError(RuntimeError):
    """Postgres mode refuses to start without the keys every instance must share (MVP WP9)."""

    def __init__(self, missing: list[str]) -> None:
        self.missing = list(missing)
        what = "; ".join(f"{name} ({SHARED_KEY_VARIABLES[name]})" for name in missing)
        verb = "is" if len(missing) == 1 else "are"
        super().__init__(
            f"database_url is set, but {what} {verb} not set. A Postgres service keeps these "
            "rows in the shared database, so every instance must use the same keys: a key made "
            "up by one instance (or a key file on its disk) would leave them unreadable on every "
            "other instance and after a restart. Set the same value on every instance (for "
            "example from a secret manager) and start again"
        )


class ServiceShutDownError(RuntimeError):
    """A Postgres-mode app object was started again after its shutdown (MVP WP9, WP8 review L3).

    The shutdown closes the shared database pool every Postgres store of the app holds; build a
    new app instead (a new process does)."""


#: Payloads above this many bytes go to the blob store (see ``ServiceStores.blob_offload_bytes``).
BLOB_OFFLOAD_ENV = "NEXURA_BLOB_OFFLOAD_BYTES"
#: Strict mode: the largest run-event payload stored inline when the blob store fails (R3-3).
BLOB_MAX_INLINE_ENV = "NEXURA_BLOB_MAX_INLINE_BYTES"
#: Set only by ``nexura_runtime.testing.blob_store`` (no environment variable exists for it).
_TEST_LOCAL_BLOBS_ON_CLOUD_RUN = False
#: Set only by ``nexura_runtime.testing.sqlite_import`` (no environment variable exists for it).
_TEST_SQLITE_STUDIO_STORE = False


def refuse_sqlite_studio_store(store: Any) -> None:
    """Postgres mode: a caller-supplied run store must keep its rows in the shared database.

    A SQLite ``StudioStore`` passed to ``create_api_app(store=...)`` together with a Postgres
    ``database_url`` would keep runs, traces and approvals per instance. It is refused before
    any connection, except inside the test-only ``allow_sqlite_studio_store()`` (never on Cloud
    Run).
    """
    if getattr(store, "database", None) is not None:
        return  # a Postgres-backed store (PostgresStudioStore)
    if _TEST_SQLITE_STUDIO_STORE and on_cloud_run() is None:
        return
    raise ValueError(
        f"database_url is set but the supplied store ({type(store).__name__}) keeps its runs in "
        "the SQLite file "
        f"{getattr(store, 'db_path', '?')}; every instance would have its own runs. Pass no "
        "store (the service then uses nexura_postgres.PostgresStudioStore) or a "
        "PostgresStudioStore"
    )


def default_blob_dir(db_path: str | Path) -> Path:
    """The default local blob folder: ``<sqlite file>.blobs`` next to the service database."""
    path = Path(db_path)
    return path.with_name(path.name + ".blobs").resolve()


class ServiceStores:
    """App-owned, lazy store factory. Extensions use the same construction boundary.

    With a Postgres ``database_url`` every service store lives in Postgres (MVP WP2–WP8): no
    SQLite file is opened or created. Ported: WP2 workspaces, ownership, membership and its
    audit; WP3 Studio workflows, runs, run events, workflow state and triggers; WP4 chat
    sessions, saved agents and skills; WP5 encrypted secrets and the usage ledger; WP6 (closed
    edition) organisations and publications; WP7 the knowledge index; WP8 evaluations, Studio
    evals, channel dedup keys and outbox receipts, and integration tool registrations. The
    readiness probe is ``SELECT 1`` on the shared pool (WP1/WP5).

    The optional ``SQLITE_ONLY_FEATURES`` are refused in Postgres mode
    (``require_sqlite_feature``) unless ``NEXURA_ALLOW_PARTIAL_POSTGRES=1`` (development and
    tests only, refused on Cloud Run) lets them use the SQLite file on ``db_path``; the service
    is then in *partial* mode, which the startup warning and ``/readyz`` report.

    ``create(name, ...)`` builds each named store once per app and returns that same
    instance afterwards, ignoring later arguments. Do not use it for objects that callers
    rebuild with different arguments (the knowledge index is built directly for that reason).
    """

    def __init__(
        self, db_path: str | Path, database_url: str | None = None, blob_url: str | None = None
    ) -> None:
        self.db_path = db_path
        self.database_url = resolve_database_url(database_url)
        self._stores: dict[str, Any] = {}
        self.database: Any = None
        self.allow_partial = os.environ.get(PARTIAL_POSTGRES_ENV) == "1"
        #: SQLite-only features in use next to Postgres (partial mode), by name.
        self.sqlite_features: list[str] = []
        # Parsed now, so a bad NEXURA_BLOB_URL fails before anything is created.
        self.blob_url = resolve_blob_url(blob_url)
        self.blob_location = parse_blob_url(self.blob_url) if self.blob_url else None
        self._blob_payloads: BlobPayloads | None = None

    @property
    def partial(self) -> bool:
        """True while a Postgres URL runs with a SQLite-only feature in the SQLite file (never
        deploy). Without such a feature a Postgres service is complete, switch or not."""
        return bool(self.database_url and self.sqlite_features)

    @property
    def storage_mode(self) -> str | None:
        """``/readyz`` (operator view): ``"postgres"``, ``"partial-postgres"``, or ``None`` for
        SQLite (whose body stays as it always was)."""
        if not self.database_url:
            return None
        return "partial-postgres" if self.partial else "postgres"

    def require_service_support(self) -> None:
        """Refuse the development switch on Cloud Run (it is never meant for a deployment).

        Postgres mode itself needs nothing more: every service store is ported (MVP WP8).
        """
        cloud = on_cloud_run()
        if self.database_url and self.allow_partial and cloud:
            raise RuntimeError(
                f"{PARTIAL_POSTGRES_ENV}=1 is refused on Cloud Run ({cloud} is set): it lets "
                "SQLite-only features keep rows in the container's SQLite file, which is lost on "
                "every restart or scale-in. Never set it in a deployment"
            )

    def require_shared_keys(self) -> None:
        """Postgres mode: refuse to start without ``SHARED_KEY_VARIABLES`` (MVP WP9), before
        anything is created. SQLite mode is unchanged (a local key file or the in-process store).

        ``NEXURA_INSECURE_ALLOW_PLAINTEXT_SECRETS`` does not lift this: it keeps credentials in
        one process, which a shared database cannot serve.
        """
        if not self.database_url:
            return
        missing = [name for name in SHARED_KEY_VARIABLES if not os.environ.get(name, "").strip()]
        if missing:
            raise SharedKeyMissingError(missing)

    def require_sqlite_feature(self, feature: str) -> None:
        """Postgres mode: refuse a ``SQLITE_ONLY_FEATURES`` feature before anything is created,
        unless the development switch allows it (then the service is in partial mode)."""
        if not self.database_url:
            return
        what = SQLITE_ONLY_FEATURES[feature]
        if not self.allow_partial:
            raise ValueError(
                f"database_url is set, but {what} in a SQLite file, which every instance would "
                "keep for itself. Turn the feature off for a Postgres service; "
                f"{PARTIAL_POSTGRES_ENV}=1 allows it for development and tests only"
            )
        self.require_service_support()
        if feature not in self.sqlite_features:
            self.sqlite_features.append(feature)

    def partial_warning(self) -> str:
        """The startup warning for partial mode (names the SQLite file, never the URL)."""
        features = "; ".join(SQLITE_ONLY_FEATURES[f] for f in self.sqlite_features) or "none"
        return (
            f"PARTIAL POSTGRES MODE ({PARTIAL_POSTGRES_ENV}=1, development and tests only; never "
            "deploy): every service store (workspaces, membership, runs, workflow state, "
            "triggers, chat sessions, agents, skills, secrets, the usage ledger, the knowledge "
            "index, evaluations, channels, integrations and the closed edition's organisations "
            "and publications) uses Postgres, but these SQLite-only features keep their rows in "
            f"the SQLite file {self.db_path}, which is per instance and not shared: {features}"
        )

    def create(self, name: str, sqlite_factory: Callable[..., T], *args: Any, **kw: Any) -> T:
        self.require_service_support()
        if name not in self._stores:
            self._stores[name] = sqlite_factory(*args, **kw)
        result: T = self._stores[name]
        return result

    def open_database(self) -> Any:
        """Foundation available to future Postgres stores and migration tooling."""
        if self.database_url is None:
            raise ValueError("Postgres database_url is not configured")
        if self.database is None:
            from nexura_runtime.modules.optional_packages import POSTGRES_PACKAGE, require_package

            postgres = require_package(POSTGRES_PACKAGE, "Postgres service storage")
            self.database = postgres.Database.from_url(self.database_url)
        return self.database

    def studio_store(self, db_path: str | Path, checkpoint_store: Any) -> Any:
        """The Studio run store: ``PostgresStudioStore`` on the shared database in Postgres mode
        (``checkpoint_store`` is then required), else a SQLite ``StudioStore`` on ``db_path``."""
        if not self.database_url:
            from nexura_runtime.studio.store import StudioStore

            return self.create("studio", StudioStore, db_path, checkpoint_store=checkpoint_store)
        if checkpoint_store is None:
            raise ValueError(
                "Postgres mode keeps workflow state in the database: workflow_state_url must "
                "not be empty when database_url is set"
            )
        from nexura_runtime.modules.optional_packages import POSTGRES_PACKAGE, require_package

        postgres = require_package(POSTGRES_PACKAGE, "Postgres Studio run storage")
        return self.create(
            "studio",
            postgres.PostgresStudioStore,
            self.open_database(),
            checkpoint_store=checkpoint_store,
            db_path=db_path,
        )

    def ping(self) -> bool:
        if self.database_url:
            return bool(self.open_database().ping())
        return SQLiteProbe(self.db_path).ping()

    def close(self) -> None:
        if self.database is not None:
            self.database.close()
            self.database = None

    # -- Blob storage (MVP WP2b) ------------------------------------------------------------

    def blob_offload_bytes(self) -> int:
        """Payloads above this many bytes go to the blob store; ``0`` keeps them inline.

        ``NEXURA_BLOB_OFFLOAD_BYTES`` wins. Otherwise offloading is on (8 KiB) in Postgres mode
        or when ``NEXURA_BLOB_URL`` is set, and off for a plain SQLite service, whose database
        and the backup of it then stay one self-contained file.
        """
        raw = os.environ.get(BLOB_OFFLOAD_ENV)
        if raw is not None and raw.strip():
            try:
                value = int(raw)
            except ValueError:
                value = -1
            if value < 0:
                raise ValueError(f"{BLOB_OFFLOAD_ENV} must be a non-negative integer of bytes")
            return value
        return DEFAULT_OFFLOAD_BYTES if (self.database_url or self.blob_url) else 0

    def blob_location_or_default(self) -> BlobLocation:
        """The configured location, or the local folder next to the SQLite file."""
        if self.blob_location is not None:
            return self.blob_location
        return BlobLocation(scheme="file", path=str(default_blob_dir(self.db_path)))

    def require_blob_support(self) -> None:
        """Refuse a local blob folder on Cloud Run when the service is not itself local.

        In Postgres mode, or with an explicit ``file://`` ``NEXURA_BLOB_URL``, blobs in the
        container's folder would be lost on every restart or scale-in while the rows that
        point at them survive. A plain SQLite service keeps its default folder next to its
        (equally local) database file, exactly as the database itself. Tests that simulate Cloud
        Run use ``nexura_runtime.testing.blob_store.allow_local_blobs_on_cloud_run``.
        """
        cloud = on_cloud_run()
        if not cloud or _TEST_LOCAL_BLOBS_ON_CLOUD_RUN:
            return
        if self.blob_location_or_default().scheme != "file":
            return
        if self.database_url or self.blob_location is not None:
            raise RuntimeError(
                f"A local blob folder is refused on Cloud Run ({cloud} is set): its disk is "
                "not durable, so stored payloads would be lost while database rows still point "
                f"at them. Set {BLOB_URL_ENV}=gs://<bucket>/<prefix>"
            )

    @property
    def blob_strict(self) -> bool:
        """Postgres mode or a ``gs://`` store: the store is checked at startup (``check_blobs``)
        and later write failures are errors. A plain local SQLite service stays lenient."""
        return bool(self.database_url) or self.blob_location_or_default().scheme == "gs"

    def blob_payloads(self) -> BlobPayloads:
        """The app's blob store and offload codec, opened once (no network, no directory)."""
        if self._blob_payloads is None:
            self.require_blob_support()
            store = open_blob_store(self.blob_location_or_default())
            self._blob_payloads = BlobPayloads(
                store,
                self.blob_offload_bytes(),
                strict=self.blob_strict,
                max_inline_bytes=self.blob_max_inline_bytes(),
            )
        return self._blob_payloads

    def blob_max_inline_bytes(self) -> int | None:
        """``NEXURA_BLOB_MAX_INLINE_BYTES``, else ``None`` (the codec's default: 1 MiB in strict
        mode, no cap otherwise)."""
        raw = os.environ.get(BLOB_MAX_INLINE_ENV)
        if raw is None or not raw.strip():
            return None
        try:
            value = int(raw)
        except ValueError:
            value = -1
        if value < 0:
            raise ValueError(f"{BLOB_MAX_INLINE_ENV} must be a non-negative integer of bytes")
        return value

    def bind_blobs_now(self) -> None:
        """Bind the registered databases as soon as the codec is attached (R3-2), so the first
        offload never binds inside a database write. Strict mode binds in ``check_blobs`` at
        startup instead (it may need the network); a plain local service binds here when
        offloading is on, and only logs a failure."""
        if not self.blob_strict:
            self.blob_payloads().bind_now()

    def check_blobs(self) -> None:
        """Startup check in strict mode: read or create the store identity (a local root also
        has its permissions checked) and bind every registered database to it. Any problem stops
        the start with a ``RuntimeError`` that names the store, never the database URL."""
        if not self.blob_strict:
            return
        codec = self.blob_payloads()
        try:
            codec.check_ready()
        except Exception as exc:
            raise RuntimeError(
                f"Blob store {codec.store.describe()} is not usable: {exc}. Fix the store "
                f"(permissions, IAM, {BLOB_URL_ENV}) before starting the service"
            ) from exc

    def knowledge_index(self, vector_store: Any, embedder: Any) -> Any:
        """The hybrid knowledge index for ``embedder`` (not cached: callers rebuild it with
        another embedder). Postgres mode (MVP WP7): ``PostgresHybridKnowledgeIndex`` on the
        shared database; ``vector_store`` is not used (the service creates none). Otherwise the
        SQLite index on ``vector_store``'s connection (the borrowing stays inside this boundary).
        """
        self.require_service_support()
        if self.database_url:
            postgres = self._postgres("Postgres knowledge index")
            return postgres.PostgresHybridKnowledgeIndex(self.open_database(), embedder=embedder)
        from nexura_runtime.knowledge.hybrid import HybridKnowledgeIndex

        return HybridKnowledgeIndex(vector_store._conn, embedder=embedder)

    def vector_store(self) -> Any:
        """The generic SQLite vector store (``SqliteVectorStore``, unused by any route), or
        ``None`` in Postgres mode: no SQLite file or table is created there (MVP WP7, #13)."""
        if self.database_url:
            self.require_service_support()
            return None
        from nexura_runtime.knowledge.vector.sqlite import SqliteVectorStore

        return self.create("vectors", SqliteVectorStore, self.db_path)

    def studio_evaluations(self) -> StudioEvaluationBackend:
        """Studio eval datasets and runs: ``PostgresStudioEvalStore`` in Postgres mode (MVP WP8),
        else a ``StudioEvalStore`` on ``db_path``."""
        if self.database_url:
            postgres = self._postgres("Postgres Studio eval storage")
            studio: StudioEvaluationBackend = self.create(
                "studio_evaluations", postgres.PostgresStudioEvalStore, self.open_database()
            )
            return studio
        from nexura_runtime.evaluation.studio_store import StudioEvalStore

        return self.create("studio_evaluations", StudioEvalStore, self.db_path)

    def evaluations(self, db_file: str | Path | None) -> Any:
        """Evaluation datasets, runs and traces: ``PostgresEvaluationStore`` in Postgres mode
        (MVP WP8), else a ``SqliteEvaluationStore`` on ``db_file`` (``None``: in memory)."""
        if self.database_url:
            postgres = self._postgres("Postgres evaluation storage")
            return self.create(
                "evaluations", postgres.PostgresEvaluationStore, self.open_database()
            )
        from nexura_runtime.evaluation.store import SqliteEvaluationStore

        path = str(db_file) if db_file and str(db_file) != ":memory:" else ":memory:"
        return self.create("evaluations", SqliteEvaluationStore, path)

    def channels(self, db_file: str | Path | None) -> Any:
        """Channel dedup keys and outbox receipts: ``PostgresChannelStore`` in Postgres mode (MVP
        WP8), else a ``SqliteChannelStore`` on ``db_file`` (``None``: in process memory)."""
        if self.database_url:
            postgres = self._postgres("Postgres channel storage")
            return self.create("channels", postgres.PostgresChannelStore, self.open_database())
        if db_file and str(db_file) != ":memory:":
            from nexura_runtime.integrations.channels.outbox import SqliteChannelStore

            return self.create("channels", SqliteChannelStore, str(db_file))
        from nexura_runtime.integrations.channels.outbox import MemoryChannelStore

        return self.create("channels", MemoryChannelStore)

    def integration_registrations(self, db_file: str | Path) -> IntegrationRegistrations:
        """Persisted fixture tool registrations: ``PostgresIntegrationRegistrations`` in Postgres
        mode (MVP WP8; the table comes from the migration), else the SQLite file."""
        if self.database_url:
            postgres = self._postgres("Postgres integration registrations")
            pg: IntegrationRegistrations = self.create(
                "integration_registrations",
                postgres.PostgresIntegrationRegistrations,
                self.open_database(),
            )
            return pg
        return self.create("integration_registrations", SQLiteIntegrationRegistrations, db_file)

    def _postgres(self, purpose: str) -> Any:
        from nexura_runtime.modules.optional_packages import POSTGRES_PACKAGE, require_package

        return require_package(POSTGRES_PACKAGE, purpose)

    def sessions(self) -> SessionBackend:
        """Chat sessions: ``PostgresSessionStore`` on the shared database in Postgres mode (MVP
        WP4), else a ``SQLiteSessionStore`` on ``db_path``."""
        if self.database_url:
            postgres = self._postgres("Postgres chat session storage")
            sessions: SessionBackend = self.create(
                "sessions", postgres.PostgresSessionStore, self.open_database()
            )
            return sessions
        from nexura_agents.session import SQLiteSessionStore

        return self.create("sessions", SQLiteSessionStore, self.db_path)

    def agents(self) -> AgentBackend:
        """Saved agents: ``PostgresAgentStore`` in Postgres mode (MVP WP4), else SQLite."""
        if self.database_url:
            postgres = self._postgres("Postgres agent storage")
            agents: AgentBackend = self.create(
                "agents", postgres.PostgresAgentStore, self.open_database()
            )
            return agents
        from nexura_runtime.agents_store import AgentStore

        return self.create("agents", AgentStore, self.db_path)

    def skills(self) -> SkillsBackend:
        """Skills: ``PostgresSkillStore`` in Postgres mode (MVP WP4), else SQLite."""
        if self.database_url:
            postgres = self._postgres("Postgres skill storage")
            skills: SkillsBackend = self.create(
                "skills", postgres.PostgresSkillStore, self.open_database()
            )
            return skills
        from nexura_runtime.skills_store import SkillStore

        return self.create("skills", SkillStore, self.db_path)

    def secrets(self, provider: Any) -> Any:
        """Encrypted secrets under ``provider`` (the master key): ``PostgresSecretStore`` on the
        shared database in Postgres mode (MVP WP5), else an ``EncryptedSecretStore`` on
        ``db_path``. ``None`` is returned by the caller when no master key is configured."""
        if self.database_url:
            postgres = self._postgres("Postgres secret storage")
            return self.create(
                "secrets", postgres.PostgresSecretStore, provider, self.open_database()
            )
        from nexura_runtime.secrets.store import EncryptedSecretStore

        path = None if str(self.db_path) == ":memory:" else self.db_path
        return self.create("secrets", EncryptedSecretStore, provider, path)

    def usage_ledger(self) -> Any:
        """The usage ledger: ``PostgresUsageLedger`` on the shared database in Postgres mode (MVP
        WP5), else a ``UsageLedger`` on ``db_path`` (in memory for ``:memory:``)."""
        if self.database_url:
            postgres = self._postgres("Postgres usage ledger")
            return self.create("usage", postgres.PostgresUsageLedger, self.open_database())
        from nexura_agents.usage.ledger import UsageLedger

        path = str(self.db_path) if self.db_path and str(self.db_path) != ":memory:" else None
        return self.create("usage", UsageLedger, path)


def app_usage_ledger(state: Any) -> Any:
    """The app's one usage ledger, created on first use and shared by budgets and diagnostics.

    It is kept as ``state.app_usage_ledger``, never as ``state.usage_ledger``: the trace
    endpoint reads ``usage_ledger`` only when an operator installs one, so traces keep their
    pre-WP1 output and never take the ledger's write lock. With the app's ``service_stores`` it
    is ``ServiceStores.usage_ledger()`` (``PostgresUsageLedger`` in Postgres mode, MVP WP5). The
    budget helper has also historically accepted a state holding only ``store.db_path``; that
    SQLite seam is kept (it never reads ``NEXURA_DATABASE_URL``).
    """
    ledger = getattr(state, "app_usage_ledger", None)
    if ledger is not None:
        return ledger
    stores = getattr(state, "service_stores", None)
    if stores is not None:
        stores.require_service_support()
        if stores.database_url:
            ledger = stores.usage_ledger()
            state.app_usage_ledger = ledger
            return ledger
    from nexura_agents.usage.ledger import UsageLedger

    path = getattr(getattr(state, "store", None), "db_path", None)
    ledger = UsageLedger(str(path) if path and str(path) != ":memory:" else None)
    state.app_usage_ledger = ledger
    return ledger
