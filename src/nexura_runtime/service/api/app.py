# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""FastAPI application factory for Nexura Service API."""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import shutil
import tempfile
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
from nexura_agents.session import SQLiteSessionStore

from nexura_runtime import __version__
from nexura_runtime.integrations.channels.outbox import ChannelStore
from nexura_runtime.knowledge.embeddings.config import configured_embedder
from nexura_runtime.knowledge.ingest.pipeline import IngestionPipeline
from nexura_runtime.knowledge.retrieval.pipeline import RagPipeline
from nexura_runtime.knowledge.vector.sqlite import SqliteVectorStore
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
from nexura_runtime.service.api.integrations import router as integrations_router
from nexura_runtime.service.api.knowledge import router as knowledge_router
from nexura_runtime.service.api.local_secrets import install_local_credential_store
from nexura_runtime.service.api.local_session import (
    LocalSessionMiddleware,
    LocalSessionPolicy,
    build_session_token_router,
    resolve_session_token,
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
from nexura_runtime.service.extensions import (
    AuthProvider,
    ExtensionHost,
    ServiceExtension,
    check_extensions,
    load_entry_point_extensions,
)
from nexura_runtime.service.repro_capture import FailedRunCapture, capture_failed_runs_enabled
from nexura_runtime.service.triggers.store import TriggerStore
from nexura_runtime.service.workflow_state import (
    open_workflow_state_store,
    resolve_workflow_state_url,
)
from nexura_runtime.service.workspaces.repository import WorkspaceRepository, ensure_private_file
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
            health = checker.assess_readiness()
            if not admin:
                cache["health"], cache["at"] = health, now
        code = 200 if health.is_healthy else 503
        if not admin:
            return JSONResponse(content={"status": health.state}, status_code=code)
        content: dict[str, Any] = {
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


def _local_profile(app: Any) -> bool:
    """True while the app runs the unauthenticated ``local-owner`` profile."""
    pm: ProfileManager | None = getattr(getattr(app, "state", None), "profile_manager", None)
    return pm is None or pm.profile == "local-owner"


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
    embeddings: str | None = None,
    embeddings_dimensions: int | None = None,
    capture_failed_runs: bool | None = None,
    execution_store: ExecutionStore | None = None,
    workflow_state_url: str | None = None,
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

    Authentication: the open-source edition has one principal, the local owner (role ``admin``),
    and no logins, tokens or API keys; ``X-Principal-*`` headers are ignored. The
    ``authenticated-shared-service`` profile needs an authenticating ``auth_provider``; the
    closed edition supplies one (its app factory also takes the
    ``token_validator``, ``api_key_manager`` and ``operator_principal_ids`` arguments this
    function had before EDITIONS_PLAN step 7).

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

    Durable execution:
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
            Studio run records, triggers and sessions stay in SQLite. Applies to the store
            this function creates: passing it together with ``store`` raises ``ValueError``.
            The tables are created (migrated) when the app starts.
    """
    requested_extensions = check_extensions(
        [*extensions, *load_entry_point_extensions(enabled_extensions)],
        taken=BUILTIN_EXTENSION_NAMES,
    )
    if store is not None and workflow_state_url:
        raise ValueError(
            "workflow_state_url applies to the store create_api_app creates; give the "
            "StudioStore its checkpoint_store instead."
        )
    # Checked before any file is created: an unusable URL fails fast.
    workflow_state_store: Any = None
    if store is None:
        state_url = resolve_workflow_state_url(workflow_state_url)
        if state_url is not None:
            workflow_state_store = open_workflow_state_store(state_url)
    effective_registry = registry if registry is not None else get_default_registry()
    if store is None and str(db_path) == ":memory:":
        # StudioStore shares one file across threads, so ":memory:" would otherwise
        # create a literal ":memory:" file in the working directory.
        ephemeral_dir = tempfile.mkdtemp(prefix="nexura-ephemeral-")
        atexit.register(shutil.rmtree, ephemeral_dir, ignore_errors=True)
        db_path = Path(ephemeral_dir) / "service.sqlite3"
    if store is None:
        # Create the database owner-only (0600): it holds webhook secrets, runs and chat history.
        # SQLite gives the -wal/-shm sidecar files the same mode as the main file.
        ensure_private_file(db_path or "./nexura_service.sqlite3")
    effective_store = store or StudioStore(
        db_path or "./nexura_service.sqlite3", checkpoint_store=workflow_state_store
    )

    def default_execution_store() -> ExecutionStore:
        # Workflow runs journal next to their checkpoints: the effects API reads that journal.
        if workflow_state_store is not None:
            journal: ExecutionStore = workflow_state_store.execution_store
            return journal
        return SQLiteExecutionStore(effective_store.db_path)

    effective_ws_repo = workspace_repo or WorkspaceRepository()

    # Bind database to workspace repository for durable ownership and workspaces
    if effective_ws_repo.db_path is None and hasattr(effective_store, "db_path"):
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

    assert_safe_bind(
        # When the caller names only ``bound_host`` it is the best evidence of the real bind.
        listen_host if listen_host is not None else bound_host,
        authenticated=effective_auth_provider.is_authenticating,
        allow_unauthenticated_network=allow_unauthenticated_network,
        component="Nexura service (local-owner profile)",
    )

    # Configure Trigger, Outbox, and Evaluation stores bound to sqlite database
    db_file = getattr(effective_store, "db_path", None)
    app_trigger_store: TriggerStore
    app_channel_store: ChannelStore
    if db_file and db_file != ":memory:":
        from nexura_runtime.evaluation.store import SqliteEvaluationStore
        from nexura_runtime.integrations.channels.outbox import SqliteChannelStore
        from nexura_runtime.service.triggers.store import SqliteTriggerStore

        app_trigger_store = SqliteTriggerStore(db_file)
        app_channel_store = SqliteChannelStore(db_file)
        app_eval_store = SqliteEvaluationStore(db_file)
    else:
        from nexura_runtime.evaluation.store import SqliteEvaluationStore
        from nexura_runtime.integrations.channels.outbox import MemoryChannelStore
        from nexura_runtime.service.triggers.store import MemoryTriggerStore

        app_trigger_store = MemoryTriggerStore()
        app_channel_store = MemoryChannelStore()
        app_eval_store = SqliteEvaluationStore(":memory:")

    from nexura_runtime.service.triggers.manager import TriggerManager

    app_trigger_manager = TriggerManager(store=app_trigger_store)
    with contextlib.suppress(Exception):
        app_trigger_manager.reconcile_missed_runs()

    # Restore persisted integration tools into registries
    if db_file and db_file != ":memory:" and Path(db_file).exists():
        import sqlite3

        try:
            with sqlite3.connect(str(db_file), timeout=5.0) as conn:
                cur = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND "
                    "name='nexura_service_integration_registrations'"
                )
                if cur.fetchone():
                    rows = conn.execute(
                        "SELECT workspace_id, module_id, kind, fixture_id, server_name_or_prefix "
                        "FROM nexura_service_integration_registrations"
                    ).fetchall()
                    from nexura_agents.mcp.client import McpClientSession
                    from nexura_agents.mcp.module import McpToolModule
                    from nexura_agents.mcp.transports import McpInMemoryTransport
                    from nexura_agents.mcp.types import McpToolDefinition

                    from nexura_runtime.service.api.integrations import (
                        McpSessionCleanup,
                        McpSessionFactory,
                        _create_mcp_fixture_server,
                        tag_module_workspace,
                    )

                    for ws_id, mod_id, kind, fid, srv_pfx in rows:
                        effective_ws_repo.record_resource_ownership(
                            "integration_tool", mod_id, ws_id, "local-owner"
                        )
                        if kind == "mcp":

                            def make_restored_factory(fixture_id: str) -> McpSessionFactory:
                                async def _factory() -> tuple[McpClientSession, McpSessionCleanup]:
                                    c_pair, s_pair = McpInMemoryTransport.create_pair()
                                    srv = _create_mcp_fixture_server(fixture_id, s_pair)
                                    srv_loop = asyncio.create_task(srv.serve_loop())
                                    cli = McpClientSession(c_pair)
                                    await cli.initialize()

                                    async def _cleanup() -> None:
                                        await cli.close()
                                        await srv.close()
                                        srv_loop.cancel()

                                    return cli, _cleanup

                                return _factory

                            t_name = mod_id.split(".")[-1]
                            t_def = McpToolDefinition(
                                name=t_name,
                                description=f"Restored {t_name}",
                                inputSchema={"type": "object"},
                            )
                            mod = McpToolModule(
                                client=None,
                                tool_def=t_def,
                                server_name=srv_pfx,
                                session_factory=make_restored_factory(fid or "math_fixture"),
                            )
                            tag_module_workspace(mod, ws_id)
                            effective_registry.register(mod_id, mod)
                            if effective_registry.versioned_registry:
                                with contextlib.suppress(Exception):
                                    effective_registry.versioned_registry.register(mod)
        except Exception:
            pass

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if execution_store is None and app.state.execution_store_closed:
            # The app is started again after a shutdown closed its own journal store.
            app.state.execution_store = default_execution_store()
            app.state.execution_store_closed = False
        if workflow_state_store is not None:
            # Migrate the Postgres workflow tables and their journal before serving.
            await workflow_state_store.init_db()
        if (
            workflow_state_store is None
            or app.state.execution_store is not workflow_state_store.execution_store
        ):
            await app.state.execution_store.init_db()
        # Startup phase: reconcile missed schedule triggers after restart
        with contextlib.suppress(Exception):
            app_trigger_manager.reconcile_missed_runs()

        worker_stop: asyncio.Event | None = None
        worker_task: asyncio.Task[None] | None = None
        if run_trigger_worker:
            from nexura_runtime.service.triggers.worker import TriggerWorker

            worker = TriggerWorker(app.state, poll_interval=trigger_poll_interval)
            app.state.trigger_worker = worker
            worker_stop = asyncio.Event()
            worker_task = asyncio.create_task(worker.run_forever(worker_stop))

        yield

        # 0. Stop the trigger worker before cancelling run tasks it may have started
        if worker_stop is not None and worker_task is not None:
            worker_stop.set()
            with suppress(Exception):
                await asyncio.wait_for(worker_task, timeout=30.0)
            if not worker_task.done():
                worker_task.cancel()
                await asyncio.gather(worker_task, return_exceptions=True)
        # 0b. Let pending revocations (B40) finish cancelling the runs of revoked keys
        revocations = list(getattr(app.state, "run_revocation_tasks", ()))
        if revocations:
            await asyncio.wait(revocations, timeout=10.0)
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
                run_snapshot = await effective_store.get_run(run_id)
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
        if execution_store is None:
            await app.state.execution_store.close()
            app.state.execution_store_closed = True

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
    app.state.store = effective_store
    app.state.execution_store = execution_store or default_execution_store()
    # Where workflow checkpoints live (SQLite on db_path, or Postgres, A6 S8).
    app.state.workflow_state_store = getattr(effective_store, "checkpoint_store", None)
    app.state.execution_store_closed = False
    app.state.session_store = SQLiteSessionStore(effective_store.db_path)
    app.state.chat_models = ChatModelRegistry(chat_models)
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
    app.state.vector_store = SqliteVectorStore(effective_store.db_path)
    app.state.rag_pipeline = RagPipeline(vector_store=app.state.vector_store)
    app.state.knowledge_collections = {"default"}
    # Env-var references plus the local encrypted secrets file (NEXURA_MASTER_KEY); the shared
    # profile refuses to start without a master key.
    install_local_credential_store(
        app.state,
        profile=effective_profile_mgr.profile,
        db_path=getattr(effective_store, "db_path", None),
    )
    app.state.workspace_repo = effective_ws_repo
    app.state.profile_manager = effective_profile_mgr
    token, token_configured = resolve_session_token(session_token)
    app.state.session_token = token
    app.state.local_session = LocalSessionPolicy(
        token=token,
        configured=token_configured,
        allow_network=allow_unauthenticated_network,
        require_loopback_client=True,
        require_json=True,
    )
    app.state.auth_provider = effective_auth_provider
    app.state.active_run_tasks = {}
    app.state.trigger_store = app_trigger_store
    app.state.trigger_manager = app_trigger_manager
    app.state.channel_store = app_channel_store
    app.state.eval_store = app_eval_store
    app.state.approval_required_handlers = (
        frozenset({"synthetic_ticket_creator"})
        if approval_required_handlers is None
        else frozenset(approval_required_handlers)
    )

    # Middleware: SecurityScopingMiddleware runs inside RequestIdMiddleware
    # Order (outermost first): body limit -> security headers -> host/origin -> local session
    # (loopback client, session token, JSON only) -> request id -> auth
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
        _create_service_health_router(HealthChecker(db_path=effective_store.db_path), lambda: app)
    )
    # Built-in routes match first, so no extension route pattern can shadow one.
    extension_host.place_extension_routes_last()
    # Identity check: whatever ran above, requests are authenticated by the installed provider.
    extension_host.check_auth_provider()
    # Declared public routes (inbound webhooks of other services) are not checked for the token.
    app.state.local_session.add_exempt_paths(extension_host.public_paths)

    return app
