# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""FastAPI application factory for Nexura Service API."""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import logging
import shutil
import tempfile
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterable, Mapping
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from nexura_agents.execution_store import ExecutionStore, SQLiteExecutionStore
from nexura_agents.limits import UsageLimits
from nexura_agents.models.base import ModelProtocol

from nexura_runtime import __version__
from nexura_runtime.integrations.channels.outbox import ChannelStore
from nexura_runtime.knowledge.embeddings.config import configured_embedder
from nexura_runtime.knowledge.ingest.pipeline import IngestionPipeline
from nexura_runtime.knowledge.retrieval.pipeline import RagPipeline
from nexura_runtime.release.health import HealthChecker
from nexura_runtime.service.api.agents import mount_agents
from nexura_runtime.service.api.auth_providers import caller_is_operator, resolve_auth_provider
from nexura_runtime.service.api.bind_guard import assert_safe_bind
from nexura_runtime.service.api.builder import router as builder_router
from nexura_runtime.service.api.builtin_extensions import (
    BUILTIN_EXTENSION_NAMES,
    builtin_extensions,
)
from nexura_runtime.service.api.chat_models import ChatModelRegistry
from nexura_runtime.service.api.credentials import router as credentials_router
from nexura_runtime.service.api.descriptors import BUILTIN_CAPABILITIES
from nexura_runtime.service.api.descriptors import router as descriptors_router
from nexura_runtime.service.api.diagnostics import router as diagnostics_router
from nexura_runtime.service.api.effects import router as effects_router
from nexura_runtime.service.api.errors import register_validation_handler
from nexura_runtime.service.api.evaluations import router as evaluations_router
from nexura_runtime.service.api.guardrails import (
    check_guardrail_lock,
    install_guardrail_policy,
    resolve_guardrail_policy,
)
from nexura_runtime.service.api.guardrails import router as guardrails_router
from nexura_runtime.service.api.integrations import router as integrations_router
from nexura_runtime.service.api.knowledge import router as knowledge_router
from nexura_runtime.service.api.local_secrets import install_local_credential_store
from nexura_runtime.service.api.local_session import (
    LocalSessionMiddleware,
    LocalSessionPolicy,
    build_session_token_router,
    check_session_token_switch,
    resolve_session_token,
    session_token_required,
)
from nexura_runtime.service.api.middleware import (
    DEFAULT_MAX_BODY_BYTES,
    DEFAULT_MAX_IMPORT_BODY_BYTES,
    BodySizeLimitMiddleware,
    HostOriginMiddleware,
    RequestIdMiddleware,
    SecurityHeadersMiddleware,
    SecurityScopingMiddleware,
    require_shared_principal,
)
from nexura_runtime.service.api.model_pricing import build_pricing_table
from nexura_runtime.service.api.operator_token import (
    DeliveryTokenStore,
    OperatorTokenAuthProvider,
    OperatorTokenMiddleware,
    assert_remote_transport,
    single_operator_mode,
)
from nexura_runtime.service.api.runs import router as runs_router
from nexura_runtime.service.api.sessions import (
    DEFAULT_CHAT_USAGE_LIMITS,
    chat_router,
)
from nexura_runtime.service.api.sessions import router as sessions_router
from nexura_runtime.service.api.skills import mount_skills
from nexura_runtime.service.api.triggers import router as triggers_router
from nexura_runtime.service.api.workflows import router as workflows_router
from nexura_runtime.service.api.workspaces import (
    router as workspaces_router,
)
from nexura_runtime.service.api.workspaces import (
    workspace_scoped_router,
)
from nexura_runtime.service.auth import ProfileManager, ProfileType
from nexura_runtime.service.cloud import shutdown_grace as _shutdown_grace
from nexura_runtime.service.extensions import (
    AuthProvider,
    ExtensionHost,
    ServiceExtension,
    check_extensions,
    load_entry_point_extensions,
)
from nexura_runtime.service.repro_capture import FailedRunCapture, capture_failed_runs_enabled
from nexura_runtime.service.run_liveness import (
    SHUTDOWN_ERROR,
    SHUTDOWN_REASON,
    journal_decides,
    reconcile_quietly,
)
from nexura_runtime.service.triggers.secretbox import key_file_for
from nexura_runtime.service.triggers.store import TriggerStore
from nexura_runtime.service.workflow_state import (
    open_trigger_state_store,
    open_workflow_state_store,
    resolve_workflow_state_url,
)
from nexura_runtime.service.workspaces.repository import (
    WorkspaceRepository,
    ensure_private_file,
    refuse_sqlite_loaded_repository,
)
from nexura_runtime.studio.api.traces import router as traces_router
from nexura_runtime.studio.builtin_modules import get_default_registry
from nexura_runtime.studio.store import StudioStore
from nexura_runtime.templates import mount_templates
from nexura_runtime.workflow import ModuleRegistry


def _create_service_health_router(
    checker: HealthChecker, app_ref: Callable[[], FastAPI]
) -> APIRouter:
    """Health routes that never reveal paths, PIDs or raw errors to unauthenticated callers.

    ``/healthz`` is public and returns only a status. ``/readyz`` is public but also minimal
    (status only, cached for a few seconds so it is not an unauthenticated write amplifier);
    full probe details are returned to the local owner (local-owner profile) or to an
    allow-listed operator principal (shared profile). Who is an operator is decided by the
    installed ``AuthProvider.is_operator``. Health fields added by extensions appear only in
    the operator view, under ``extensions``.
    """
    router = APIRouter(tags=["health"])
    cache: dict[str, Any] = {"at": 0.0, "health": None}

    def _is_admin(request: Request) -> bool:
        # Remote single-operator mode never shows probe details on this public route: it would
        # tell a right token from a wrong one outside the failed-attempt limiter (P38 C4 M1).
        if single_operator_mode(request.app):
            return False
        # Fails closed: a provider whose is_operator raises keeps the response minimal.
        return caller_is_operator(request)

    @router.get("/healthz")
    async def liveness() -> dict[str, Any]:
        probe = checker.check_liveness()
        return {"status": probe.state}

    @router.get("/readyz")
    async def readiness(request: Request) -> Any:
        admin = _is_admin(request)
        now = time.monotonic()
        health = cache["health"]
        if admin or health is None or now - cache["at"] > 5.0:
            # The database probe may wait on the network (Postgres); keep the loop free.
            health = await asyncio.to_thread(checker.assess_readiness)
            if not admin:
                cache["health"], cache["at"] = health, now
        code = 200 if health.is_healthy else 503
        if not admin:
            return JSONResponse(content={"status": health.state}, status_code=code)
        # Partial Postgres mode is never silent to an operator (no URL, no credentials). The
        # public body stays minimal, as for every other detail.
        # MVP WP8: "postgres" (every store in the shared database) or "partial-postgres" (a
        # SQLite-only feature kept in the SQLite file under the development switch).
        stores = getattr(app_ref().state, "service_stores", None)
        mode = getattr(stores, "storage_mode", None)
        storage: dict[str, Any] = {"storage": mode} if mode else {}
        # MVP WP3 (R3-3): payloads kept inline (or as a preview only) because the blob store
        # failed, for alerting. Shown only when offloading is on, so other bodies are unchanged.
        codec = getattr(app_ref().state, "blob_payloads", None)
        if codec is not None and getattr(codec, "threshold", 0) > 0:
            storage["blob_payloads"] = codec.stats()
        content: dict[str, Any] = {
            **storage,
            "status": health.state,
            "timestamp": health.timestamp,
            "probes": [
                {
                    "name": p.name,
                    "state": p.state,
                    "latency_ms": p.latency_ms,
                    "details": p.details,
                    "error": p.error,
                }
                for p in health.probes
            ],
        }
        host: ExtensionHost | None = getattr(app_ref().state, "extensions", None)
        extension_fields = host.health_fields if host is not None else {}
        if extension_fields:
            content["extensions"] = extension_fields
        return JSONResponse(content=content, status_code=code)

    return router


def bind_workspaces_to_postgres(repo: Any, stores: Any) -> None:
    """Postgres mode: workspaces, ownership and membership must live in the shared database.

    Refused before any connection: a repository loaded from a SQLite file (a stale copy would
    undo removals on every start) and one that cannot bind to Postgres (each instance would
    keep its own memberships, so a member removed on one instance would stay a member on the
    others). In-memory repositories are bound and keep their pre-bind records.
    """
    if getattr(repo, "database_bound", False):
        return
    check_workspaces_bindable(repo)
    repo.bind_database(stores.open_database())


def check_workspaces_bindable(repo: Any) -> None:
    """The refusals of ``bind_workspaces_to_postgres``, without connecting (run first)."""
    if repo is None or getattr(repo, "database_bound", False):
        return
    refuse_sqlite_loaded_repository(repo)
    if not callable(getattr(repo, "bind_database", None)):
        raise TypeError(
            f"database_url is set but workspace_repo ({type(repo).__name__}) cannot bind to "
            "Postgres (it has no bind_database); workspaces and membership would stay per "
            "instance"
        )


def _local_profile(app: Any) -> bool:
    """True while the app runs the unauthenticated ``local-owner`` profile.

    False in the remote single-operator mode: there the operator token authenticates every
    request, so the loopback-client rule, the per-session token and the JSON-only rule of
    ``local_session`` do not apply (a bearer header is never sent by a browser on its own).
    """
    pm: ProfileManager | None = getattr(getattr(app, "state", None), "profile_manager", None)
    return (pm is None or pm.profile == "local-owner") and not single_operator_mode(app)


def create_api_app(
    registry: ModuleRegistry | None = None,
    store: StudioStore | None = None,
    workspace_repo: WorkspaceRepository | None = None,
    db_path: str | Path | None = None,
    profile: ProfileType = "local-owner",
    profile_manager: ProfileManager | None = None,
    approval_required_handlers: frozenset[str] | None = None,
    run_trigger_worker: bool = False,
    trigger_poll_interval: float = 1.0,
    chat_models: Mapping[str, ModelProtocol | str] | None = None,
    chat_usage_limits: UsageLimits | None = None,
    allowed_hosts: Iterable[str] | None = None,
    allowed_origins: Iterable[str] | None = None,
    bound_host: str | None = None,
    max_request_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
    max_import_body_bytes: int = DEFAULT_MAX_IMPORT_BODY_BYTES,
    expose_api_docs: bool | None = None,
    allow_unauthenticated_network: bool = False,
    listen_host: str | None = None,
    auth_provider: AuthProvider | None = None,
    extensions: Iterable[ServiceExtension] = (),
    enabled_extensions: Iterable[str] = (),
    session_token: str | None = None,
    require_session_token: bool | None = None,
    embeddings: str | None = None,
    embeddings_dimensions: int | None = None,
    capture_failed_runs: bool | None = None,
    execution_store: ExecutionStore | None = None,
    workflow_state_url: str | None = None,
    workflow_lease_ttl: float = 30.0,
    allow_plain_http: bool = False,
    guardrail_policy: Any = None,
    database_url: str | None = None,
    blob_url: str | None = None,
    model_pricing: str | Mapping[str, Any] | None = None,
) -> FastAPI:
    """Create a configured FastAPI application exposing the Nexura REST API.

    Knowledge options:
        embeddings: operator-owned ``provider:model`` spec, overriding ``NEXURA_EMBEDDINGS``.
            Unconfigured services use the labelled non-semantic fixture. Invalid configuration
            fails explicitly. Existing collections require explicit reindex after a change.
        embeddings_dimensions: expected native output dimension, overriding
            ``NEXURA_EMBEDDINGS_DIMENSIONS`` (needed for models without a known default).

    Security options:
        allowed_hosts: extra ``Host`` names accepted (loopback names and ``bound_host`` are
            always accepted in local mode). Also read from ``NEXURA_ALLOWED_HOSTS``.
        allowed_origins: browser origins (``scheme://host[:port]``) allowed to call the API.
            Local mode additionally accepts loopback origins on any port; shared mode accepts
            only these. Also read from ``NEXURA_ALLOWED_ORIGINS``.
        bound_host: the interface the server binds to (accepted as a Host name).
        max_request_body_bytes: global request-body cap (default 10 MiB); workflow archive
            import uses ``max_import_body_bytes`` (default 40 MiB).
        expose_api_docs: serve ``/docs``, ``/redoc`` and ``/openapi.json``. Defaults to True in
            local mode and False in shared mode; when enabled in shared mode they require
            authentication.

        listen_host: the address the server will actually bind (what you pass to uvicorn).
            ``bound_host`` only widens Host-header acceptance; it is used for the bind check
            only when ``listen_host`` is omitted. With neither, the app is assumed loopback.
        allow_unauthenticated_network: loudly named opt-out. Without it, passing a non-loopback
            ``listen_host`` to the unauthenticated ``local-owner`` profile raises
            ``UnauthenticatedNetworkError`` at startup (also settable with
            ``NEXURA_INSECURE_ALLOW_UNAUTHENTICATED_NETWORK=I_UNDERSTAND_THIS_IS_UNSAFE``).
            ``nexura-runtime serve`` must pass its ``--host`` here. Without the opt-in, the
            local-owner profile also answers 403 ``NON_LOOPBACK_CLIENT`` to any request whose
            peer address is not loopback, so embedding the app with
            ``uvicorn.run(app, host="0.0.0.0")`` does not serve the network.
        session_token: the per-session token every state-changing request of the local-owner
            profile must send in ``X-Nexura-Session`` (constant-time compare; 403
            ``SESSION_TOKEN_REQUIRED`` / ``SESSION_TOKEN_INVALID`` otherwise). Default:
            ``NEXURA_STUDIO_TOKEN`` when set, else ``secrets.token_urlsafe(32)``. Read it from
            ``app.state.session_token``; Studio reads it from ``GET /api/v1/local-session`` on
            its own origin. The local-owner profile also refuses ``text/plain``, form and
            multipart bodies on state-changing requests (415), see ``local_session``.
        require_session_token: ``False`` turns the session token check off for programs on
            this machine that send plain HTTP requests. State-changing requests from another
            page (``Sec-Fetch-Site`` cross-site/same-site, or an ``Origin`` other than this
            server) are then refused with 403 ``CROSS_ORIGIN_REQUEST``; the loopback-client,
            ``Host`` and JSON-only rules stay. Default: ``NEXURA_REQUIRE_SESSION_TOKEN``
            (``on``/``off``), else on. Refused (ValueError) with
            ``allow_unauthenticated_network``.

    Authentication: the open-source edition has one principal, the local owner (role ``admin``),
    and no logins, user accounts or API keys; ``X-Principal-*`` headers are ignored.

    Remote single-operator mode (P38 C4, ``docs/remote-single-operator.md``): pass
    ``auth_provider=OperatorTokenAuthProvider(token)`` (``nexura-runtime serve --auth token``).
    Every request except ``/healthz`` and ``/readyz`` then needs ``Authorization: Bearer
    <token>`` and the caller is the same local owner; the loopback-client rule and the
    per-session token are off, a non-loopback ``listen_host`` is allowed by the bind guard, and
    ``Origin`` may also be the request's own origin. ``allow_plain_http`` (default False) must be
    True to use a non-loopback ``listen_host`` in this mode, since the service speaks plain HTTP
    (``PlainHttpNetworkError`` otherwise); put a TLS reverse proxy in front instead. Without it,
    a request from a non-loopback peer that did not arrive over HTTPS is also refused at request
    time (403 ``PLAIN_HTTP_REFUSED``), which covers embedding without ``listen_host``.
    ``/readyz`` shows no probe details in this mode. Webhook deliveries take a per-endpoint
    delivery token instead of the operator token (``docs/remote-single-operator.md``). Combining
    the mode with ``allow_unauthenticated_network`` raises ``ValueError``. The
    ``authenticated-shared-service`` profile needs an authenticating ``auth_provider``; the
    closed edition supplies one (its app factory also takes the
    ``token_validator``, ``api_key_manager`` and ``operator_principal_ids`` arguments this
    function had before EDITIONS_PLAN step 7).

    Model prices: ``model_pricing`` (JSON text or a mapping in the ``NEXURA_MODEL_PRICING``
    format, see :mod:`nexura_runtime.service.api.model_pricing`) is merged over the built-in
    price table and used by every run budget, trace and diagnostics cost
    (``app.state.pricing_table``). A run under a monetary cap refuses a model without a price.
    The launchers read ``NEXURA_MODEL_PRICING``; an invalid value raises ``ModelPricingError``.

    Extension options (see ``docs/extensions.md``):
        auth_provider: the ``AuthProvider`` that authenticates ``/api/v1`` requests. Omitted,
            it is ``LocalOwnerAuthProvider`` (the local owner, role ``admin``).
            ``profile="authenticated-shared-service"`` fails closed with
            ``AuthProviderRequiredError`` unless an authenticating provider is given.
        extensions: ``ServiceExtension`` objects configured, in order, after every built-in
            router. Startup fails with ``ExtensionError`` if one replaces ``app.state``
            attributes (including the ``AuthProvider``), reuses a built-in route or adds a
            route on ``app`` outside ``/api/v1`` that is not declared in its ``public_paths``.
        enabled_extensions: names of ``nexura.extensions`` entry points to load and configure
            after ``extensions``. Entry points are never loaded unless named here; an unknown or
            broken one raises ``ExtensionLoadError`` before anything else is created.

    Diagnostics:
        capture_failed_runs: record service workflow runs and keep a redacted repro bundle for
            each run that ends ``failed`` (download: ``GET /api/v1/runs/{run_id}/repro``).
            Off by default; ``None`` reads ``NEXURA_CAPTURE_FAILED_RUNS`` (``1``/``true``).
            See ``docs/reproducing-failures.md``.

    Guardrails (P39 G6, see ``docs/guardrail-policy.md``):
        guardrail_policy: the operator's guardrail policy, a ``nexura_guardrails.GuardrailPolicy``
            or the path of a policy file (``.json``, or ``.yaml`` with
            ``nexura-guardrails[yaml]``). ``None`` reads ``NEXURA_GUARDRAIL_POLICY``. It is
            loaded before anything else; a missing or invalid file, or a policy without
            ``nexura-guardrails`` installed, raises ``GuardrailPolicyConfigError`` (a
            ``ValueError``) and the service does not start. The policy is installed with
            ``set_default_hooks(policy, locked=True)``, so every agent run in this process
            uses it and no code can remove it; one process enforces one policy. It is never
            settable through the API; ``GET /api/v1/guardrails/policy`` shows a summary.

    Durable execution:
        database_url: opt-in service backend URL; ``None`` reads ``NEXURA_DATABASE_URL``.
            The generic ``DATABASE_URL`` is read only by the enterprise launcher. With it
            every service store lives in Postgres (MVP WP2–WP8) and no SQLite file is opened
            or created; ``capture_failed_runs`` and the operator-token mode, which keep rows in
            SQLite, are refused unless ``NEXURA_ALLOW_PARTIAL_POSTGRES=1`` (development and
            tests only). Unset preserves the SQLite default. With it, ``workflow_state_url``
            must not be empty and a supplied ``store`` must keep its runs in Postgres.
        blob_url: where large run-event and eval-trace payloads go (MVP WP2b):
            ``gs://<bucket>[/<prefix>]`` (credentials from ADC) or ``file:///<path>``. ``None``
            reads ``NEXURA_BLOB_URL``; unset means the folder ``<db_path>.blobs``. Payloads
            above ``NEXURA_BLOB_OFFLOAD_BYTES`` (8 KiB by default in Postgres mode or with a
            blob URL; off by default for plain SQLite) are stored there and the row keeps a
            reference. On Cloud Run a local folder is refused in Postgres mode or when named
            explicitly.
        execution_store: the execution journal served by ``GET /api/v1/runs/{run_id}/effects``
            and ``POST .../effects/{effect_id}/resolve``. Default: a ``SQLiteExecutionStore``
            on ``db_path``, closed with the app; a supplied store (for example
            ``PostgresExecutionStore``) stays caller-owned. See ``docs/durable-execution.md``.
        workflow_state_url: where workflow checkpoints, their history and the execution
            journal of workflow runs live. ``None`` reads ``NEXURA_WORKFLOW_STATE_URL``;
            unset or empty keeps them in the SQLite database at ``db_path`` (the default).
            A ``postgresql://`` URI keeps them in Postgres
            (``nexura_postgres.PostgresWorkflowStore``, ``pip install nexura-postgres``);
            unless ``execution_store`` is given, the effects API then reads that journal.
            The trigger job queue, webhooks and schedules then live in Postgres too
            (``nexura_postgres.PostgresTriggerStore``, A6 S10b; webhook secrets are encrypted
            with ``NEXURA_TRIGGER_SECRET_KEY`` or, without a ``database_url``, the
            ``<db_path>.trigger.key`` file, so set the variable when several hosts share the
            queue). Without ``database_url``
            Studio run records and sessions stay in SQLite. Applies to the store
            this function creates: passing it together with ``store`` raises ``ValueError``.
            The tables are created (migrated) when the app starts.
        workflow_lease_ttl: seconds of the run lease a workflow run's process holds (renewed
            every third of it; default 30). A run whose lease expired, or whose owner process
            on this host exited, is shown ``interrupted`` and can be continued with
            ``POST /api/v1/runs/{run_id}/recover`` (A6 S9); a run another live ``serve`` or
            ``worker`` executes is never shown interrupted.
    """
    from nexura_runtime.service.storage_backend import (
        ServiceShutDownError,
        ServiceStores,
        refuse_sqlite_studio_store,
    )

    service_stores = ServiceStores(db_path or "./nexura_service.sqlite3", database_url, blob_url)
    service_stores.require_service_support()
    # MVP WP9: a Postgres service needs the keys every instance shares (master key, trigger
    # secret key) before anything is created; SQLite mode is unchanged.
    service_stores.require_shared_keys()
    # MVP WP8: every service store is on Postgres; the two optional features that still keep
    # rows in SQLite are refused with a database_url (before anything is created) unless the
    # development switch allows them.
    if isinstance(auth_provider, OperatorTokenAuthProvider):
        service_stores.require_sqlite_feature("delivery_tokens")
    if capture_failed_runs_enabled(capture_failed_runs):
        service_stores.require_sqlite_feature("repro_capture")
    # MVP WP2b: a local blob folder is refused on Cloud Run before anything is created.
    service_stores.require_blob_support()
    if service_stores.partial:
        logging.getLogger("nexura_runtime.service").warning(service_stores.partial_warning())
    # P39 G6: the operator's guardrail policy is loaded first; a bad one stops the start.
    app_chat_models = ChatModelRegistry(chat_models)
    app_pricing_table = build_pricing_table(model_pricing, aliases=app_chat_models.model_ids())
    app_guardrail_policy = resolve_guardrail_policy(guardrail_policy, app_chat_models)
    if app_guardrail_policy is not None:
        check_guardrail_lock(app_guardrail_policy)
    requested_extensions = check_extensions(
        [*extensions, *load_entry_point_extensions(enabled_extensions)],
        taken=BUILTIN_EXTENSION_NAMES,
    )
    if (
        isinstance(workflow_lease_ttl, bool)
        or not isinstance(workflow_lease_ttl, (int, float))
        or not (0 < workflow_lease_ttl < float("inf"))
    ):
        raise ValueError(
            f"workflow_lease_ttl must be a positive number of seconds, got {workflow_lease_ttl!r}."
        )
    if store is not None and workflow_state_url:
        raise ValueError(
            "workflow_state_url applies to the store create_api_app creates; give the "
            "StudioStore its checkpoint_store instead."
        )
    if service_stores.database_url:
        # Refusals come before the first connection (the Postgres run store connects to migrate).
        check_workspaces_bindable(workspace_repo)
        if store is not None:
            # MVP WP3: runs must live in the shared database, never in a caller's SQLite file.
            refuse_sqlite_studio_store(store)
    # Checked before any file is created: an unusable URL fails fast.
    workflow_state_store: Any = None
    state_url: str | None = None
    if store is None:
        state_url = resolve_workflow_state_url(
            workflow_state_url if workflow_state_url is not None else service_stores.database_url
        )
        if service_stores.database_url and state_url is None:
            raise ValueError(
                "Postgres mode keeps workflow state in the database: workflow_state_url must "
                "not be empty when database_url is set"
            )
        if service_stores.database_url and state_url is not None:
            from nexura_runtime.service.storage_url import normalize_database_url

            if normalize_database_url(state_url, strict=False) != service_stores.database_url:
                # Review L5: runs in one database and their checkpoints and journal in another
                # split one run across two databases (as the empty-URL refusal prevents).
                raise ValueError(
                    "Postgres mode keeps workflow state next to the runs: workflow_state_url "
                    "must be unset or name the same database as database_url"
                )
        if state_url is not None:
            workflow_state_store = open_workflow_state_store(state_url)
    effective_registry = registry if registry is not None else get_default_registry()
    if store is None and str(db_path) == ":memory:":
        # StudioStore shares one file across threads, so ":memory:" would otherwise
        # create a literal ":memory:" file in the working directory.
        ephemeral_dir = tempfile.mkdtemp(prefix="nexura-ephemeral-")
        atexit.register(shutil.rmtree, ephemeral_dir, ignore_errors=True)
        db_path = Path(ephemeral_dir) / "service.sqlite3"
    if store is None and (not service_stores.database_url or service_stores.partial):
        # Create the database owner-only (0600): it holds webhook secrets, runs and chat history.
        # SQLite gives the -wal/-shm sidecar files the same mode as the main file. A Postgres
        # service has no SQLite file at all (MVP WP8), unless a SQLite-only feature is allowed.
        ensure_private_file(db_path or "./nexura_service.sqlite3")
    # MVP WP3: with a Postgres database_url the run store is PostgresStudioStore on the shared
    # database.
    effective_store = store or service_stores.studio_store(
        db_path or "./nexura_service.sqlite3", workflow_state_store
    )
    service_stores.db_path = effective_store.db_path
    # Large run-event and eval-trace payloads go to the blob store (MVP WP2b).
    blob_payloads = service_stores.blob_payloads()
    if hasattr(effective_store, "blob_payloads"):
        effective_store.blob_payloads = blob_payloads

    def default_execution_store() -> ExecutionStore:
        # Workflow runs journal next to their checkpoints: the effects API reads that journal.
        if workflow_state_store is not None:
            journal: ExecutionStore = workflow_state_store.execution_store
            return journal
        supplied = getattr(
            getattr(effective_store, "checkpoint_store", None), "execution_store", None
        )
        if service_stores.database_url and supplied is not None:
            # A caller-supplied PostgresStudioStore: its runs journal next to its checkpoints.
            return supplied  # type: ignore[no-any-return]
        return SQLiteExecutionStore(effective_store.db_path)

    effective_ws_repo = workspace_repo or service_stores.create("workspaces", WorkspaceRepository)

    # Bind database to workspace repository for durable ownership and workspaces
    # (or, with a Postgres database_url, read-through to the shared database: MVP WP2).
    if service_stores.database_url:
        bind_workspaces_to_postgres(effective_ws_repo, service_stores)
    elif effective_ws_repo.db_path is None and hasattr(effective_store, "db_path"):
        effective_ws_repo.bind_db_path(effective_store.db_path)

    # Configure ProfileManager (the closed edition passes its SharedProfileManager)
    if profile_manager is not None:
        effective_profile_mgr = profile_manager
        if profile != "local-owner":
            effective_profile_mgr.set_profile(profile)
    else:
        effective_profile_mgr = ProfileManager(profile=profile)

    # The shared profile fails closed unless an authenticating provider was passed
    effective_auth_provider = resolve_auth_provider(auth_provider, effective_profile_mgr)

    # The multi-tenant service runs code only behind gVisor or a micro-VM, never Monty alone:
    # refuse to start when an enabled sandbox backend is weaker than gvisor.
    from nexura_runtime.integrations.sandbox import assert_sandbox_allowed_for_profile

    assert_sandbox_allowed_for_profile(effective_profile_mgr.profile, effective_registry)

    # Remote single-operator mode: one operator token, plain HTTP only on loopback by default.
    remote_operator = isinstance(effective_auth_provider, OperatorTokenAuthProvider)
    if remote_operator:
        if allow_unauthenticated_network:
            raise ValueError(
                "allow_unauthenticated_network cannot be combined with the operator token: "
                "remote single-operator mode authenticates every request."
            )
        assert_remote_transport(
            listen_host if listen_host is not None else bound_host,
            allow_plain_http=allow_plain_http,
        )
    elif allow_plain_http:
        raise ValueError(
            "allow_plain_http only applies to remote single-operator mode (an operator token)."
        )

    assert_safe_bind(
        # When the caller names only ``bound_host`` it is the best evidence of the real bind.
        listen_host if listen_host is not None else bound_host,
        authenticated=effective_auth_provider.is_authenticating,
        allow_unauthenticated_network=allow_unauthenticated_network,
        component="Nexura service (local-owner profile)",
    )

    # Trigger, channel and evaluation stores: on the SQLite database, or in Postgres (MVP WP3,
    # WP8) with a database_url.
    db_file = getattr(effective_store, "db_path", None)
    sqlite_file = bool(db_file and db_file != ":memory:")
    app_trigger_store: TriggerStore
    app_channel_store: ChannelStore
    # A6 S10b: with Postgres workflow state the trigger queue lives next to it, so workers on
    # several hosts share it. Webhook secrets: with only workflow_state_url (one host's SQLite
    # service) the key file of the SQLite database is the fallback; in Postgres mode (MVP WP9,
    # WP8 review M1) only NEXURA_TRIGGER_SECRET_KEY, which require_shared_keys made sure of: no
    # per-instance key file is ever written.
    postgres_trigger_store: Any = None
    if workflow_state_store is not None and state_url is not None:
        postgres_trigger_store = open_trigger_state_store(
            state_url,
            secret_key_file=None
            if service_stores.database_url
            else key_file_for(db_file or "./nexura_service.sqlite3"),
        )
    if postgres_trigger_store is not None:
        app_trigger_store = postgres_trigger_store
    elif sqlite_file:
        from nexura_runtime.service.triggers.store import SqliteTriggerStore

        app_trigger_store = service_stores.create("triggers", SqliteTriggerStore, db_file)
    else:
        from nexura_runtime.service.triggers.store import MemoryTriggerStore

        app_trigger_store = service_stores.create("triggers", MemoryTriggerStore)
    app_channel_store = service_stores.channels(db_file if sqlite_file else None)
    app_eval_store = service_stores.evaluations(db_file if sqlite_file else None)

    from nexura_runtime.service.triggers.manager import TriggerManager

    app_trigger_manager = TriggerManager(store=app_trigger_store)
    if postgres_trigger_store is None:
        # A Postgres queue is not touched before the app starts (its pool opens on first
        # use); the startup phase and the worker's schedule ticks catch up instead.
        with contextlib.suppress(Exception):
            app_trigger_manager.reconcile_missed_runs()

    integration_registrations = service_stores.integration_registrations(effective_store.db_path)
    # Persisted integration tools are restored into this instance's registry (MVP WP8: from the
    # shared database in Postgres mode, so every instance restores every registration). A
    # storage error stops the start; a row that cannot be rebuilt is logged and skipped.
    from nexura_runtime.service.api.integrations import restore_integration_registrations

    restore_integration_registrations(
        integration_registrations.list(), effective_ws_repo, effective_registry
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if service_stores.database_url and app.state.lifespan_ended:
            # MVP WP9 (WP8 review L3): the first shutdown closed the shared database pool that
            # every Postgres store of this app holds; say so instead of failing later with a
            # closed-pool error inside some store.
            raise ServiceShutDownError(
                "This Postgres-mode app has already been shut down: its shutdown closed the "
                "shared database pool its stores hold, so it cannot start again. Create a new "
                "app with create_api_app(...) (a new process does this on every start)"
            )
        if (
            isinstance(app.state.auth_provider, OperatorTokenAuthProvider)
            and app.state.delivery_tokens is None
        ):
            # The app is started again after a shutdown closed its delivery-token store.
            app.state.delivery_tokens = DeliveryTokenStore(effective_store.db_path)
        if execution_store is None and app.state.execution_store_closed:
            # The app is started again after a shutdown closed its own journal store.
            app.state.execution_store = default_execution_store()
            app.state.execution_store_closed = False
        if workflow_state_store is not None:
            # Migrate the Postgres workflow tables and their journal before serving.
            await workflow_state_store.init_db()
        if postgres_trigger_store is not None:
            await asyncio.to_thread(postgres_trigger_store.init_db)
        # MVP WP2b: in Postgres or gs:// mode an unusable blob store (identity, permissions,
        # IAM) or a database bound to another store stops the start here.
        await asyncio.to_thread(service_stores.check_blobs)
        if (
            workflow_state_store is None
            or app.state.execution_store is not workflow_state_store.execution_store
        ):
            await app.state.execution_store.init_db()
        # A6 S9: journaled runs whose lease expired (their process stopped) are interrupted;
        # runs another live process executes are left alone. Legacy rows were swept by
        # StudioStore when it opened (B46).
        await reconcile_quietly(app.state)
        # Startup phase: reconcile missed schedule triggers after restart
        with contextlib.suppress(Exception):
            app_trigger_manager.reconcile_missed_runs()

        worker_stop: asyncio.Event | None = None
        worker_task: asyncio.Task[None] | None = None
        if run_trigger_worker:
            from nexura_runtime.service.triggers.worker import TriggerWorker

            worker = TriggerWorker(
                app.state,
                poll_interval=trigger_poll_interval,
                shutdown_grace_seconds=_shutdown_grace(20.0),
            )
            app.state.trigger_worker = worker
            worker_stop = asyncio.Event()
            worker_task = asyncio.create_task(worker.run_forever(worker_stop))

        yield

        # 0. Stop the trigger worker before cancelling run tasks it may have started
        if worker_stop is not None and worker_task is not None:
            worker_stop.set()
            with suppress(Exception):
                await asyncio.wait_for(worker_task, timeout=_shutdown_grace(30.0))
            if not worker_task.done():
                worker_task.cancel()
                await asyncio.gather(worker_task, return_exceptions=True)
        # 0a. Jobs a trigger tick left running (MVP WP9) get the same grace period
        tick_worker = getattr(app.state, "trigger_tick_worker", None)
        if tick_worker is not None:
            with suppress(Exception):
                await tick_worker.drain()
        # 0b. Let pending revocations (B40) finish cancelling the runs of revoked keys
        revocations = list(getattr(app.state, "run_revocation_tasks", ()))
        if revocations:
            await asyncio.wait(revocations, timeout=_shutdown_grace(10.0))
        # 1. Shutdown phase: cancel and reap owned active run tasks
        active_tasks: dict[str, asyncio.Task[Any]] = getattr(app.state, "active_run_tasks", {})
        running_task_items = list(active_tasks.items())
        for _run_id, task in running_task_items:
            if not task.done():
                task.cancel()

        # Wait for all cancelled tasks to finish
        if running_task_items:
            tasks_to_wait = [task for _, task in running_task_items]
            await asyncio.gather(*tasks_to_wait, return_exceptions=True)

        # 2. For any run that was genuinely interrupted by shutdown, leave interrupted
        # state truthful. Do not mark a completed, paused, failed, or cancelled run as interrupted.
        for run_id, _ in running_task_items:
            with contextlib.suppress(Exception):
                if await journal_decides(app.state, run_id):
                    # A6 S9: its lease decides; this process released it when the task was
                    # cancelled, so it is interrupted unless another worker holds it now.
                    await reconcile_quietly(
                        app.state,
                        run_ids=[run_id],
                        error=SHUTDOWN_ERROR,
                        reason=SHUTDOWN_REASON,
                        stopped_here=True,
                    )
                    continue
                run_snapshot = await effective_store.get_run(run_id, payloads=False)
                if run_snapshot and run_snapshot.get("status") == "running":
                    await effective_store.record_run_event(
                        run_id,
                        "RUN_INTERRUPTED",
                        data={"reason": "Server shutdown; background task cancelled."},
                    )
                    await effective_store.update_run(
                        run_id=run_id,
                        status="interrupted",
                        current_node_id=run_snapshot.get("current_node_id"),
                        current_state=run_snapshot.get("current_state", {}),
                        step_count=run_snapshot.get("step_count", 0),
                        error="Run was interrupted by server shutdown.",
                    )

        active_tasks.clear()
        service_stores.close()

        # 3. Close store and session store if created by factory
        if hasattr(app.state, "session_store"):
            with suppress(Exception):
                await app.state.session_store.close()

        if hasattr(app.state, "vector_store") and hasattr(app.state.vector_store, "_conn"):
            with suppress(Exception):
                app.state.vector_store._conn.close()

        with suppress(Exception):
            close_embedder = getattr(app.state.knowledge_embedder.provider, "close", None)
            if close_embedder is not None:
                await close_embedder()

        if store is None:
            await effective_store.close()
        if sqlite_file:
            # Release the database file (Windows cannot delete one that is still open); these
            # stores reopen it on next use, so a restarted app keeps working. Postgres stores
            # (MVP WP8) have nothing to release: the shared pool closes with service_stores.
            sqlite_stores: list[Any] = [
                st
                for st in (app_channel_store, app_eval_store)
                if not getattr(st, "blocking_io", False)
            ]
            if postgres_trigger_store is None:
                sqlite_stores.append(app_trigger_store)
            for sqlite_store in sqlite_stores:
                with suppress(Exception):
                    sqlite_store.close()
        if postgres_trigger_store is not None:
            # Reopened on next use, so a restarted app keeps working.
            with suppress(Exception):
                await asyncio.to_thread(postgres_trigger_store.close)
        if execution_store is None:
            await app.state.execution_store.close()
            app.state.execution_store_closed = True
        delivery_tokens = app.state.delivery_tokens
        if isinstance(delivery_tokens, DeliveryTokenStore):
            # Reopened at the next start (Windows cannot delete an open file).
            with suppress(Exception):
                delivery_tokens.close()
            app.state.delivery_tokens = None
        app.state.lifespan_ended = True

    docs_enabled = (
        expose_api_docs
        if expose_api_docs is not None
        else effective_profile_mgr.profile == "local-owner"
    )
    app = FastAPI(
        title="Nexura API",
        description="Backend API for Nexura AgentKit workflows, runs, and approvals.",
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs" if docs_enabled else None,
        redoc_url="/redoc" if docs_enabled else None,
        openapi_url="/openapi.json" if docs_enabled else None,
    )

    register_validation_handler(app)

    # Attach state
    app.state.registry = effective_registry
    app.state.service_stores = service_stores
    app.state.integration_registrations = integration_registrations
    app.state.agent_store = service_stores.agents()
    app.state.skills_store = service_stores.skills()
    app.state.store = effective_store
    app.state.execution_store = execution_store or default_execution_store()
    # Where workflow checkpoints live (SQLite on db_path, or Postgres, A6 S8).
    app.state.workflow_state_store = getattr(effective_store, "checkpoint_store", None)
    app.state.execution_store_closed = False
    # Set by the first shutdown; a Postgres-mode app refuses a second start (MVP WP9).
    app.state.lifespan_ended = False
    # Run lease of the service's workflow engines (A6 S9).
    app.state.workflow_lease_ttl = float(workflow_lease_ttl)
    app.state.session_store = service_stores.sessions()
    app.state.chat_models = app_chat_models
    # Prices of every run budget, trace and diagnostics cost (live pass C2).
    app.state.pricing_table = app_pricing_table
    if app_guardrail_policy is not None:
        install_guardrail_policy(app_guardrail_policy)
    app.state.guardrail_policy = app_guardrail_policy
    app.state.repro_capture = (
        FailedRunCapture(effective_store.db_path)
        if capture_failed_runs_enabled(capture_failed_runs)
        else None
    )
    app.state.knowledge_embedder = configured_embedder(embeddings, dimensions=embeddings_dimensions)
    app.state.chat_usage_limits = (
        chat_usage_limits if chat_usage_limits is not None else DEFAULT_CHAT_USAGE_LIMITS
    )
    app.state.ingest_pipeline = IngestionPipeline()
    # Postgres mode (MVP WP7): no SQLite vector store; the knowledge index lives in Postgres and
    # no route uses the generic store or the pipeline built on it.
    app.state.vector_store = (
        service_stores.vector_store() if service_stores.database_url is None else None
    )
    app.state.rag_pipeline = (
        RagPipeline(vector_store=app.state.vector_store)
        if app.state.vector_store is not None
        else None
    )
    if service_stores.database_url is not None:
        # Built now, not by the first knowledge or chat request (MVP WP7 review M2): this runs
        # the knowledge migration (the shared migration lock, CREATE EXTENSION vector) and a
        # catalog query, so a missing permission stops the start instead of failing a request,
        # and no request ever waits for them on the event loop.
        app.state.hybrid_knowledge_index = service_stores.knowledge_index(
            None, app.state.knowledge_embedder
        )
        # The same for the Studio eval store (MVP WP8): its migration runs now, never on the
        # event loop of a first request.
        app.state.studio_eval_store = service_stores.studio_evaluations()
    # One build lock per app (MVP WP7 review R2-L2): a lazy index build of one app never makes
    # another app in the same process wait.
    app.state.knowledge_build_lock = threading.Lock()
    app.state.knowledge_collections = {"default"}
    # Env-var references plus the local encrypted secrets file (NEXURA_MASTER_KEY); the shared
    # profile refuses to start without a master key.
    install_local_credential_store(
        app.state,
        profile=effective_profile_mgr.profile,
        db_path=getattr(effective_store, "db_path", None),
        stores=service_stores,
    )
    app.state.workspace_repo = effective_ws_repo
    app.state.profile_manager = effective_profile_mgr
    token, token_configured = resolve_session_token(session_token)
    require_token = session_token_required(require_session_token)
    check_session_token_switch(require_token, allow_unauthenticated_network)
    app.state.session_token = token
    app.state.local_session = LocalSessionPolicy(
        token=token,
        configured=token_configured,
        allow_network=allow_unauthenticated_network,
        # Off in remote single-operator mode, where every request carries the operator token.
        require_loopback_client=not remote_operator,
        require_json=True,
        require_token=require_token,
    )
    app.state.auth_provider = effective_auth_provider
    # Per-endpoint webhook delivery tokens (hashes) of remote single-operator mode (P38 C4 M3),
    # kept on this app, not on the provider, so apps sharing a provider stay apart (R2-3).
    app.state.delivery_tokens = (
        DeliveryTokenStore(effective_store.db_path) if remote_operator else None
    )
    app.state.active_run_tasks = {}
    app.state.trigger_store = app_trigger_store
    app.state.trigger_manager = app_trigger_manager
    app.state.channel_store = app_channel_store
    app.state.eval_store = app_eval_store
    if hasattr(app_eval_store, "blob_payloads"):
        app_eval_store.blob_payloads = blob_payloads
    # The blob store and its offload codec (MVP WP2b); stores receive the codec directly.
    app.state.blob_payloads = blob_payloads
    app.state.blob_store = blob_payloads.store
    # R3-2: bind the databases now that every store has the codec, not inside a first offload.
    service_stores.bind_blobs_now()
    app.state.approval_required_handlers = (
        frozenset({"synthetic_ticket_creator"})
        if approval_required_handlers is None
        else frozenset(approval_required_handlers)
    )

    # Middleware: SecurityScopingMiddleware runs inside RequestIdMiddleware
    # Order (outermost first): body limit -> security headers -> [operator token, remote
    # single-operator mode only] -> host/origin -> local session (loopback client, session
    # token, JSON only; off in remote mode) -> request id -> auth
    app.state.workspace_repo.fail_closed_unowned = (
        effective_profile_mgr.profile == "authenticated-shared-service"
    )
    app.add_middleware(SecurityScopingMiddleware)
    app.add_middleware(RequestIdMiddleware)
    app.add_middleware(LocalSessionMiddleware, is_local=_local_profile)
    app.add_middleware(
        HostOriginMiddleware,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
        bound_host=bound_host,
    )
    if remote_operator:
        assert isinstance(effective_auth_provider, OperatorTokenAuthProvider)
        app.add_middleware(
            OperatorTokenMiddleware,
            provider=effective_auth_provider,
            allow_plain_http=allow_plain_http,
        )
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(
        BodySizeLimitMiddleware,
        max_body_bytes=max_request_body_bytes,
        large_body_bytes=max_import_body_bytes,
    )

    # API v1 Router
    api_v1 = APIRouter(prefix="/api/v1", dependencies=[Depends(require_shared_principal)])
    api_v1.include_router(workflows_router)
    api_v1.include_router(runs_router)
    api_v1.include_router(effects_router)
    api_v1.include_router(diagnostics_router)
    api_v1.include_router(traces_router)
    api_v1.include_router(sessions_router)
    api_v1.include_router(chat_router)
    api_v1.include_router(knowledge_router)
    api_v1.include_router(credentials_router)
    api_v1.include_router(workspaces_router)
    api_v1.include_router(workspace_scoped_router)
    api_v1.include_router(descriptors_router)
    api_v1.include_router(integrations_router)
    api_v1.include_router(evaluations_router)
    api_v1.include_router(triggers_router)
    api_v1.include_router(builder_router)
    api_v1.include_router(guardrails_router)
    api_v1.include_router(build_session_token_router(is_local=_local_profile))
    extension_host = ExtensionHost(
        app=app,
        api_v1=api_v1,
        registry=effective_registry,
        auth_provider=effective_auth_provider,
        reserved_capabilities=frozenset(BUILTIN_CAPABILITIES),
    )
    app.state.extensions = extension_host
    mount_templates(app, api_v1)
    # In-tree extensions (none since organisations moved to the closed edition in EDITIONS_PLAN
    # step 5) keep the position (route and middleware order) they had as mounts.
    extension_host.configure(builtin_extensions(), in_tree=True)
    mount_skills(app, api_v1)
    mount_agents(app, api_v1)
    # Each extension is checked after it runs: it cannot replace the AuthProvider or other
    # app.state, reuse a built-in method+path, or add unauthenticated routes it did not declare.
    extension_host.configure(requested_extensions)

    app.include_router(api_v1)
    app.include_router(
        _create_service_health_router(
            HealthChecker(db_path=effective_store.db_path, database=service_stores), lambda: app
        )
    )
    # Built-in routes match first, so no extension route pattern can shadow one.
    extension_host.place_extension_routes_last()
    # Identity check: whatever ran above, requests are authenticated by the installed provider.
    extension_host.check_auth_provider()
    # Declared public routes (inbound webhooks of other services) are not checked for the token.
    app.state.local_session.add_exempt_paths(extension_host.public_paths)

    return app
