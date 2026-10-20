# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Security middleware and safe error formatting for Nexura Studio.

Enforces:
1. Strict local Host header validation.
2. Origin validation rejecting cross-origin mutating requests.
3. Content-Type verification for mutating JSON routes.
4. Error masking ensuring zero traceback or credential leaks.
"""

from __future__ import annotations

import hmac
import logging
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from nexura_agents.errors import (
    ApprovalAlreadyClaimedError,
    ApprovalError,
    NexuraError,
    SessionConflictError,
    WorkflowConflictError,
    WorkflowSpecMismatchError,
    WorkflowTerminalError,
    WorkflowValidationError,
)
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response
from starlette.types import ASGIApp

logger = logging.getLogger("nexura.studio.security")

#: Loopback names only. A dotless name such as ``testserver`` could be answered by another
#: machine on the LAN (search domain, LLMNR, NetBIOS) and rebound to 127.0.0.1.
DEFAULT_ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}


STUDIO_TOKEN_ENV = "NEXURA_STUDIO_TOKEN"
_PUBLIC_PATHS = ("/healthz", "/")
_PUBLIC_PREFIXES = ("/static/",)


class StudioAuthMiddleware(BaseHTTPMiddleware):
    """Require ``Authorization: Bearer <token>`` (or ``X-Nexura-Token``) on every API request.

    Only installed when a token is configured. Static UI assets, the index page and ``/healthz``
    stay public; every other path, including ``/docs`` and ``/openapi.json``, needs the token.
    Comparison is constant-time and the failure response never echoes the supplied value.
    """

    def __init__(self, app: ASGIApp, token: str) -> None:
        super().__init__(app)
        if not token.strip():
            raise ValueError("StudioAuthMiddleware needs a non-empty token.")
        self._token = token.encode()

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path
        if (
            request.method == "OPTIONS"
            or path in _PUBLIC_PATHS
            or path.startswith(_PUBLIC_PREFIXES)
        ):
            return await call_next(request)
        supplied = request.headers.get("x-nexura-token", "")
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            supplied = auth[7:].strip()
        if not supplied or not hmac.compare_digest(supplied.encode(), self._token):
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={
                    "error": {
                        "code": "UNAUTHENTICATED",
                        "message": "A valid Studio API token is required.",
                    }
                },
                headers={"WWW-Authenticate": "Bearer"},
            )
        return await call_next(request)


class StudioSecurityMiddleware(BaseHTTPMiddleware):
    """Protects local loopback developer endpoints against DNS rebinding and CSRF."""

    def __init__(self, app: ASGIApp, allowed_hosts: set[str] | None = None) -> None:
        super().__init__(app)
        self.allowed_hosts = set(allowed_hosts) if allowed_hosts else set(DEFAULT_ALLOWED_HOSTS)

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        # 1. Host Header Validation
        host_header = request.headers.get("host", "")
        if not host_header:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"error": {"code": "INVALID_HOST", "message": "Missing Host header."}},
            )

        # Host may include port: e.g. "127.0.0.1:8000" or "[::1]:8000"
        raw_host = host_header
        if raw_host.startswith("[") and "]" in raw_host:
            hostname = raw_host[1 : raw_host.index("]")]
        else:
            hostname = raw_host.split(":")[0]

        if hostname.lower() not in self.allowed_hosts:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={
                    "error": {
                        "code": "INVALID_HOST",
                        "message": f"Host '{hostname}' is not authorized for local Studio access.",
                    }
                },
            )

        # 2. Mutating Route Origin Validation
        if request.method in ("POST", "PUT", "DELETE", "PATCH"):
            origin = request.headers.get("origin")
            if origin:
                parsed_origin = urlparse(origin)
                origin_host = parsed_origin.hostname or ""
                if origin_host.lower() not in self.allowed_hosts:
                    return JSONResponse(
                        status_code=status.HTTP_403_FORBIDDEN,
                        content={
                            "error": {
                                "code": "CROSS_ORIGIN_FORBIDDEN",
                                "message": (
                                    f"Cross-origin request from origin '{origin}' is forbidden."
                                ),
                            }
                        },
                    )

            # Enforce JSON Content-Type on mutating API calls when a body is sent
            content_type = request.headers.get("content-type", "")
            path = request.url.path
            if path.startswith("/api/"):
                content_length = request.headers.get("content-length")
                try:
                    has_body = bool(content_length) and int(content_length or 0) > 0
                except ValueError:
                    has_body = True  # malformed length: treat as a body, never a 500
                if has_body and "application/json" not in content_type:
                    return JSONResponse(
                        status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                        content={
                            "error": {
                                "code": "UNSUPPORTED_MEDIA_TYPE",
                                "message": "Content-Type must be 'application/json'.",
                            }
                        },
                    )

        return await call_next(request)


def register_error_handlers(app: FastAPI) -> None:
    """Register safe exception handlers that omit internal stack traces and secrets."""

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
        code = "HTTP_ERROR"
        message = str(exc.detail)
        if isinstance(exc.detail, dict):
            code = exc.detail.get("code", code)
            message = exc.detail.get("message", message)
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": code, "message": message}},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Simplify validation error without exposing internal schemas
        errors = exc.errors()
        first_msg = (
            errors[0].get("msg", "Invalid request body") if errors else "Invalid request body"
        )
        first_loc = " -> ".join(str(loc) for loc in errors[0].get("loc", [])) if errors else ""
        return JSONResponse(
            status_code=422,  # literal: the constant was renamed in newer Starlette
            content={
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": f"{first_msg} at {first_loc}" if first_loc else first_msg,
                }
            },
        )

    @app.exception_handler(SessionConflictError)
    async def session_conflict_handler(request: Request, exc: SessionConflictError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"error": {"code": "SESSION_CONFLICT", "message": str(exc)}},
        )

    @app.exception_handler(WorkflowConflictError)
    async def workflow_conflict_handler(
        request: Request, exc: WorkflowConflictError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"error": {"code": "WORKFLOW_CONFLICT", "message": str(exc)}},
        )

    @app.exception_handler(ApprovalAlreadyClaimedError)
    async def approval_claimed_handler(
        request: Request, exc: ApprovalAlreadyClaimedError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"error": {"code": "APPROVAL_ALREADY_CLAIMED", "message": str(exc)}},
        )

    @app.exception_handler(WorkflowTerminalError)
    async def workflow_terminal_handler(
        request: Request, exc: WorkflowTerminalError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"error": {"code": "WORKFLOW_TERMINAL", "message": str(exc)}},
        )

    @app.exception_handler(WorkflowSpecMismatchError)
    async def workflow_mismatch_handler(
        request: Request, exc: WorkflowSpecMismatchError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"error": {"code": "SPEC_MISMATCH", "message": str(exc)}},
        )

    @app.exception_handler(WorkflowValidationError)
    async def workflow_validation_handler(
        request: Request, exc: WorkflowValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"error": {"code": "VALIDATION_ERROR", "message": str(exc)}},
        )

    @app.exception_handler(ApprovalError)
    async def approval_error_handler(request: Request, exc: ApprovalError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"error": {"code": "APPROVAL_ERROR", "message": str(exc)}},
        )

    @app.exception_handler(NexuraError)
    async def nexura_error_handler(request: Request, exc: NexuraError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"error": {"code": "SDK_ERROR", "message": str(exc)}},
        )

    @app.exception_handler(Exception)
    async def generic_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.error("Unhandled Studio server error on %s: %s", request.url.path, exc)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "error": {
                    "code": "INTERNAL_SERVER_ERROR",
                    "message": "An internal server error occurred while processing the request.",
                }
            },
        )
