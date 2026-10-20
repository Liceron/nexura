"""P28 step 2 review findings 1-6 (auth and extension seams), promoted from the review repros
in .autopilot/p28/repros. Each test asserts the fixed behaviour."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient
from starlette.requests import Request

from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.service.api.middleware import AuthFailure
from nexura_runtime.service.auth.models import Principal
from nexura_runtime.service.extensions import ExtensionContext, ExtensionError, Identity

SHARED = "authenticated-shared-service"


class StrictProvider:
    """Authenticating provider: only ``Authorization: Bearer good`` passes; no operators."""

    name = "strict"
    is_authenticating = True

    def authenticate(self, request: Request, target_workspace_id: str | None) -> Identity:
        if request.headers.get("authorization") != "Bearer good":
            raise AuthFailure(401, "UNAUTHENTICATED", "Authentication required.")
        return Identity(
            Principal(principal_id="alice", profile=SHARED),
            target_workspace_id or "default",
            "admin",
        )

    def is_operator(self, request: Request) -> bool:
        return False


class AllowAll:
    name = "allow-all"
    is_authenticating = True  # claims to authenticate, does not

    def authenticate(self, request: Request, target_workspace_id: str | None) -> Identity:
        return Identity(Principal(principal_id="anyone", profile=SHARED), "default", "admin")

    def is_operator(self, request: Request) -> bool:
        return True


def _db(tmp_path: Path, name: str = "svc.sqlite3") -> str:
    return str(tmp_path / name)


# --- Finding 1: an extension can silently replace the installed AuthProvider -------------


class HijackAuthExtension:
    name = "hijack"

    def configure(self, context: ExtensionContext) -> None:
        # The docs allow setting app.state attributes; nothing re-checks auth_provider.
        context.app.state.auth_provider = AllowAll()


def test_extension_cannot_silently_replace_the_auth_provider(tmp_path: Path) -> None:
    provider = StrictProvider()
    try:
        app = create_api_app(
            db_path=_db(tmp_path),
            profile=SHARED,
            auth_provider=provider,
            extensions=[HijackAuthExtension()],
        )
    except ExtensionError:
        return  # correct: startup refused because an extension swapped auth
    assert app.state.auth_provider is provider, "extension replaced app.state.auth_provider"
    with TestClient(app) as client:
        assert client.get("/api/v1/workflows").status_code == 401


# --- Finding 2: extension routes on context.app shadow built-in /api/v1 routes and /readyz --


class ShadowExtension:
    name = "shadow"

    def configure(self, context: ExtensionContext) -> None:
        router = APIRouter()

        @router.get("/api/v1/workflows")
        async def fake_workflows() -> dict[str, Any]:
            return {"shadowed": True}

        @router.get("/readyz")
        async def fake_ready() -> dict[str, Any]:
            return {"status": "healthy", "shadowed": True}

        context.app.include_router(router)


def test_extension_cannot_shadow_builtin_routes(tmp_path: Path) -> None:
    try:
        app = create_api_app(db_path=_db(tmp_path), extensions=[ShadowExtension()])
    except ExtensionError:
        return  # correct: refusing to override an existing route
    with TestClient(app) as client:
        workflows = client.get("/api/v1/workflows").json()
        ready = client.get("/readyz").json()
    assert "shadowed" not in workflows, "extension route replaced built-in GET /api/v1/workflows"
    assert "shadowed" not in ready, "extension route replaced built-in /readyz"


# --- Finding 3: a broken provider returning principal=None gets local-owner privileges ------


class NonePrincipalProvider:
    name = "none-principal"
    is_authenticating = True

    def authenticate(self, request: Request, target_workspace_id: str | None) -> Identity:
        return Identity(None, target_workspace_id or "default", "viewer")  # type: ignore[arg-type]

    def is_operator(self, request: Request) -> bool:
        return False


def test_identity_without_principal_fails_closed(tmp_path: Path) -> None:
    app = create_api_app(
        db_path=_db(tmp_path), profile=SHARED, auth_provider=NonePrincipalProvider()
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post("/api/v1/workspaces", json={"workspace_id": "pwned", "name": "Pwned"})
    # A viewer with no principal must never create a tenant (routes treat None as local-owner).
    assert resp.status_code in (401, 403, 500, 503), resp.status_code


# --- Finding 4: operator decision is split between AuthProvider and ProfileManager ----------


# --- Finding 5: a broken is_operator crashes the public /readyz ----------------------------


class BrokenOperatorProvider(StrictProvider):
    def is_operator(self, request: Request) -> bool:
        raise RuntimeError("operator directory unreachable")


def test_broken_is_operator_keeps_public_readyz_minimal(tmp_path: Path) -> None:
    app = create_api_app(
        db_path=_db(tmp_path), profile=SHARED, auth_provider=BrokenOperatorProvider()
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/readyz")
    assert resp.status_code in (200, 503), resp.status_code
    assert set(resp.json()) == {"status"}


# --- Finding 6: an explicitly supplied falsy membership store must be honoured ------------


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])


class AdminNonOperatorProvider(StrictProvider):
    """alice is a tenant admin; the provider says nobody is an operator."""


def test_provider_is_operator_governs_workspace_creation(tmp_path: Path) -> None:
    app = create_api_app(
        db_path=_db(tmp_path),
        profile=SHARED,
        auth_provider=AdminNonOperatorProvider(),
    )
    with TestClient(app) as client:
        assert set(client.get("/readyz", headers={"authorization": "Bearer good"}).json()) == {
            "status"
        }  # provider: alice is not an operator
        resp = client.post(
            "/api/v1/workspaces",
            json={"workspace_id": "tenant-x", "name": "X"},
            headers={"authorization": "Bearer good"},
        )
    # docs/extensions.md: the AuthProvider decides "who is an operator".
    assert resp.status_code == 403, resp.text


def test_explicit_membership_store_is_used_even_when_falsy() -> None:
    from test_p28_credentials_and_membership import MembershipReader

    from nexura_runtime.service.workspaces.repository import WorkspaceRepository

    class EmptySizedMembership(WorkspaceRepository):
        def __len__(self) -> int:
            return 0

    mine = EmptySizedMembership()
    mine.create_workspace("ws", "Test local project", "local-owner")
    provider = MembershipReader(mine)
    assert provider.membership is mine
    assert provider.role("ws", "local-owner") == "admin"
