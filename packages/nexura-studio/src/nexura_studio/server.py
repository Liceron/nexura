# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""FastAPI application factory for Nexura Studio local development server."""

from __future__ import annotations

import html
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from nexura_runtime import __version__
from nexura_runtime.release.health import HealthChecker, create_health_router
from nexura_runtime.service.api.bind_guard import assert_safe_bind
from nexura_runtime.service.api.local_session import (
    LocalSessionMiddleware,
    LocalSessionPolicy,
    build_session_token_router,
    is_loopback_client,
    resolve_session_token,
    studio_error_body,
    token_read_refusal,
)
from nexura_runtime.service.api.middleware import (
    DEFAULT_MAX_BODY_BYTES,
    DEFAULT_MAX_IMPORT_BODY_BYTES,
    BodySizeLimitMiddleware,
    SecurityHeadersMiddleware,
)
from nexura_runtime.studio.builtin_modules import get_default_registry
from nexura_runtime.studio.security import (
    STUDIO_TOKEN_ENV,
    StudioAuthMiddleware,
    StudioSecurityMiddleware,
    register_error_handlers,
)
from nexura_runtime.studio.store import StudioStore
from nexura_runtime.workflow import ModuleRegistry
from nexura_studio.api.router import api_v1_router

STUDIO_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; font-src 'self'; connect-src 'self'; object-src 'none'; "
    "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
)


def create_app(
    registry: ModuleRegistry | None = None,
    db_path: str | Path | None = None,
    allowed_hosts: set[str] | list[str] | None = None,
    listen_host: str | None = None,
    api_token: str | None = None,
    allow_unauthenticated_network: bool = False,
    max_request_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
    max_import_body_bytes: int | None = None,
) -> FastAPI:
    """Create a configured FastAPI application for Nexura Studio.

    Args:
        registry: Custom ModuleRegistry with developer-provided handlers.
                  Defaults to built-in synthetic order support handlers.
        db_path: Path to SQLite database file for workflow and run persistence.
                 Defaults to './nexura_studio.sqlite3'.
        allowed_hosts: Optional set of allowed Host header values.
        listen_host: Interface the server will bind. A non-loopback value without an API token
            raises ``UnauthenticatedNetworkError`` unless ``allow_unauthenticated_network``.
        api_token: Bearer token required on every API request when set (default: the
            ``NEXURA_STUDIO_TOKEN`` environment variable). Loopback-only use needs none.
            It is also the session token; without it a random one is generated per start
            (``app.state.session_token``). Every state-changing request must send the session
            token in ``X-Nexura-Session``. The page served at ``/`` carries it to loopback
            browsers (a ``nexura-session`` meta tag); ``GET /api/v1/local-session`` returns it
            to the same origin. Without ``api_token``, requests from a non-loopback peer are
            refused (403 ``NON_LOOPBACK_CLIENT``) unless ``allow_unauthenticated_network``.
        allow_unauthenticated_network: loudly named opt-out for throwaway local development.
        max_request_body_bytes: request-body cap (default 10 MiB) answered with 413.
        max_import_body_bytes: cap for workflow import. Default: 40 MiB when the general cap is
            left at its default, otherwise the smaller of the general cap and 40 MiB.

    Returns:
        Configured FastAPI application instance.
    """
    raw_token = api_token if api_token is not None else os.environ.get(STUDIO_TOKEN_ENV)
    # An empty or whitespace-only token is "no token", never a credential.
    token = raw_token if raw_token is not None and raw_token.strip() else None
    assert_safe_bind(
        listen_host,
        authenticated=token is not None,
        allow_unauthenticated_network=allow_unauthenticated_network,
        component="Nexura Studio",
    )
    effective_registry = registry if registry is not None else get_default_registry()
    effective_db_path = Path(db_path) if db_path is not None else Path("./nexura_studio.sqlite3")
    store = StudioStore(effective_db_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        await store.close()

    app = FastAPI(
        title="Nexura Studio",
        description=(
            "Local standalone developer Studio for Nexura AgentKit workflows, runs, "
            "and approval gates. Operates strictly over local loopback with synthetic data."
        ),
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
        openapi_url="/openapi.json",
    )

    # Attach shared instances to application state (no global singletons)
    app.state.registry = effective_registry
    app.state.store = store
    session_token, _ = resolve_session_token(token)
    app.state.session_token = session_token
    app.state.local_session = LocalSessionPolicy(
        token=session_token,
        configured=token is not None,
        allow_network=allow_unauthenticated_network,
        # With an API token every API request is authenticated, so remote browsers may connect.
        require_loopback_client=token is None,
        require_json=False,  # StudioSecurityMiddleware already answers 415 for non-JSON bodies
    )

    # Security middleware & safe error handlers
    allowed_set = set(allowed_hosts) if allowed_hosts else None
    app.add_middleware(LocalSessionMiddleware, error_body=studio_error_body)
    if token is not None:
        app.add_middleware(StudioAuthMiddleware, token=token)
    app.add_middleware(StudioSecurityMiddleware, allowed_hosts=allowed_set)
    app.add_middleware(SecurityHeadersMiddleware, content_security_policy=STUDIO_CSP)
    app.add_middleware(
        BodySizeLimitMiddleware,
        max_body_bytes=max_request_body_bytes,
        large_body_bytes=(
            max_import_body_bytes
            if max_import_body_bytes is not None
            else (
                DEFAULT_MAX_IMPORT_BODY_BYTES
                if max_request_body_bytes == DEFAULT_MAX_BODY_BYTES
                else min(max_request_body_bytes, DEFAULT_MAX_IMPORT_BODY_BYTES)
            )
        ),
    )
    register_error_handlers(app)

    # Include REST API
    app.include_router(api_v1_router)
    app.include_router(build_session_token_router(), prefix="/api/v1")
    app.include_router(create_health_router(HealthChecker(db_path=effective_db_path)))

    # Static Web UI assets
    static_dir = Path(__file__).parent / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

        @app.get("/", include_in_schema=False)
        async def serve_index(request: Request) -> HTMLResponse:
            page = (static_dir / "index.html").read_text(encoding="utf-8")
            # The session token goes only to a browser on this machine loading the page from
            # this origin; the page is not framable and has no CORS, so other sites cannot
            # read it. Other browsers (Docker, an API token) get it from the #token= fragment.
            if (
                is_loopback_client(request.scope)
                and token_read_refusal(request.headers, request.url.scheme) is None
            ):
                meta = (
                    '<meta name="nexura-session" '
                    f'content="{html.escape(session_token, quote=True)}">\n</head>'
                )
                page = page.replace("</head>", meta, 1)
            return HTMLResponse(page)

    return app
