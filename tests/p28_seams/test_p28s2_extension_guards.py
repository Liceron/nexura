"""P28 step 2 review follow-up: what ExtensionHost refuses, and identity/operator edge cases."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient
from starlette.requests import Request

from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.service.api.auth_providers import (
    LocalOwnerAuthProvider,
)
from nexura_runtime.service.api.middleware import AuthFailure, invalid_identity_reason
from nexura_runtime.service.auth import ProfileManager
from nexura_runtime.service.auth.models import Principal
from nexura_runtime.service.extensions import ExtensionContext, ExtensionError, Identity

SHARED = "authenticated-shared-service"


def _db(tmp_path: Path) -> str:
    return str(tmp_path / "svc.sqlite3")


class _Ext:
    """An extension whose configure runs ``action(context)``."""

    def __init__(self, name: str, action: Any, public_paths: Any = ()) -> None:
        self.name = name
        self._action = action
        self.public_paths = public_paths

    def configure(self, context: ExtensionContext) -> None:
        self._action(context)


def _router(path: str, method: str = "get") -> APIRouter:
    router = APIRouter()

    async def endpoint() -> dict[str, Any]:
        return {"extension": True}

    router.add_api_route(path, endpoint, methods=[method.upper()])
    return router


# --- app.state -----------------------------------------------------------------------------


@pytest.mark.parametrize("attr", ["auth_provider", "profile_manager", "workspace_repo", "store"])
def test_replacing_existing_state_fails_startup(tmp_path: Path, attr: str) -> None:
    def replace(context: ExtensionContext) -> None:
        setattr(context.app.state, attr, object())

    with pytest.raises(ExtensionError, match=f"'swap'.*app.state.{attr}"):
        create_api_app(db_path=_db(tmp_path), extensions=[_Ext("swap", replace)])


def test_deleting_state_fails_startup(tmp_path: Path) -> None:
    def delete(context: ExtensionContext) -> None:
        del context.app.state.workspace_repo

    with pytest.raises(ExtensionError, match="app.state.workspace_repo"):
        create_api_app(db_path=_db(tmp_path), extensions=[_Ext("del", delete)])


def test_new_state_attributes_are_allowed(tmp_path: Path) -> None:
    sentinel = object()

    def add(context: ExtensionContext) -> None:
        context.app.state.mine_store = sentinel

    app = create_api_app(db_path=_db(tmp_path), extensions=[_Ext("mine", add)])
    assert app.state.mine_store is sentinel


def test_extension_context_is_read_only(tmp_path: Path) -> None:
    def assign(context: ExtensionContext) -> None:
        context.auth_provider = LocalOwnerAuthProvider()  # type: ignore[misc]

    with pytest.raises(AttributeError):
        create_api_app(db_path=_db(tmp_path), extensions=[_Ext("assign", assign)])


# --- routes --------------------------------------------------------------------------------


def test_collision_on_api_v1_router_fails_startup(tmp_path: Path) -> None:
    def add(context: ExtensionContext) -> None:
        context.api_v1.include_router(_router("/workflows"))

    with pytest.raises(ExtensionError, match="GET /api/v1/workflows collides"):
        create_api_app(db_path=_db(tmp_path), extensions=[_Ext("dup", add)])


def test_collision_between_extensions_fails_startup(tmp_path: Path) -> None:
    def add(context: ExtensionContext) -> None:
        context.api_v1.include_router(_router("/shared-thing"))

    with pytest.raises(ExtensionError, match="'second'"):
        create_api_app(db_path=_db(tmp_path), extensions=[_Ext("first", add), _Ext("second", add)])


def test_a_different_method_on_a_builtin_path_is_allowed(tmp_path: Path) -> None:
    def add(context: ExtensionContext) -> None:
        context.api_v1.include_router(_router("/capabilities", "patch"))

    app = create_api_app(db_path=_db(tmp_path), extensions=[_Ext("patcher", add)])
    with TestClient(app) as client:
        assert client.patch("/api/v1/capabilities").json() == {"extension": True}
        assert "capabilities" in client.get("/api/v1/capabilities").json()


@pytest.mark.parametrize("path", ["/healthz", "/readyz", "/docs", "/openapi.json"])
def test_reserved_public_paths_fail_startup(tmp_path: Path, path: str) -> None:
    def add(context: ExtensionContext) -> None:
        context.app.include_router(_router(path))

    with pytest.raises(ExtensionError, match="collides"):
        create_api_app(
            db_path=_db(tmp_path), extensions=[_Ext("reserved", add)], expose_api_docs=False
        )


def test_undeclared_route_outside_api_v1_fails_startup(tmp_path: Path) -> None:
    def add(context: ExtensionContext) -> None:
        context.app.include_router(_router("/admin-panel"))

    with pytest.raises(ExtensionError, match="outside /api/v1.*public_paths"):
        create_api_app(db_path=_db(tmp_path), extensions=[_Ext("open", add)])


@pytest.mark.parametrize(
    "declared", ["/", "/api", "/api/v1/x", "/readyz", "relative", "/x/{id}", "/healthz/sub", 5]
)
def test_invalid_public_paths_fail_startup(tmp_path: Path, declared: Any) -> None:
    with pytest.raises(ExtensionError, match="public path"):
        create_api_app(
            db_path=_db(tmp_path), extensions=[_Ext("bad", lambda c: None, public_paths=[declared])]
        )


def test_public_paths_must_be_a_collection(tmp_path: Path) -> None:
    with pytest.raises(ExtensionError, match="public_paths must be a collection"):
        create_api_app(
            db_path=_db(tmp_path), extensions=[_Ext("bad", lambda c: None, public_paths="/hooks")]
        )


def test_root_mount_fails_startup(tmp_path: Path) -> None:
    from starlette.responses import PlainTextResponse

    async def asgi(scope: Any, receive: Any, send: Any) -> None:
        await PlainTextResponse("mounted")(scope, receive, send)

    def add(context: ExtensionContext) -> None:
        context.app.mount("/api", asgi)

    with pytest.raises(ExtensionError, match="collides"):
        create_api_app(db_path=_db(tmp_path), extensions=[_Ext("mounter", add)])


def test_overlapping_app_route_pattern_never_shadows_builtin(tmp_path: Path) -> None:
    def add(context: ExtensionContext) -> None:
        context.app.include_router(_router("/api/v1/{anything:path}"))

    app = create_api_app(db_path=_db(tmp_path), extensions=[_Ext("catchall", add)])
    with TestClient(app) as client:
        assert "items" in client.get("/api/v1/workflows").json()
        assert client.get("/api/v1/not-a-builtin").json() == {"extension": True}


def test_removing_a_route_fails_startup(tmp_path: Path) -> None:
    def remove(context: ExtensionContext) -> None:
        context.api_v1.routes.pop(0)

    with pytest.raises(ExtensionError, match="removed an existing route"):
        create_api_app(db_path=_db(tmp_path), extensions=[_Ext("rm", remove)])


# --- identity validation (finding 3) --------------------------------------------------------


def _principal(pid: str = "alice") -> Principal:
    return Principal(principal_id=pid, profile=SHARED)


# --- operator decisions (findings 4 and 5) --------------------------------------------------


class _AdminProvider:
    name = "admin"
    is_authenticating = True

    def __init__(self, operator: Any) -> None:
        self._operator = operator

    def authenticate(self, request: Request, target_workspace_id: str | None) -> Identity:
        if request.headers.get("authorization") != "Bearer good":
            raise AuthFailure(401, "UNAUTHENTICATED", "Authentication required.")
        return Identity(_principal(), target_workspace_id or "default", "admin")

    def is_operator(self, request: Request) -> Any:
        if isinstance(self._operator, Exception):
            raise self._operator
        return self._operator


@pytest.mark.parametrize(
    ("operator", "expected"),
    [(True, 201), (False, 403), (RuntimeError("down"), 403), ("yes", 403), (1, 403)],
)
def test_workspace_creation_follows_provider_is_operator(
    tmp_path: Path, operator: Any, expected: int
) -> None:
    app = create_api_app(
        db_path=_db(tmp_path), profile=SHARED, auth_provider=_AdminProvider(operator)
    )
    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/workspaces",
            json={"workspace_id": "tenant-y", "name": "Y"},
            headers={"authorization": "Bearer good"},
        )
    assert resp.status_code == expected, resp.text


@pytest.mark.parametrize("profile", ["local-owner", SHARED])
def test_declared_public_route_is_served_without_authentication(
    tmp_path: Path, profile: str
) -> None:
    from _local_env import TestAuthProvider

    def add(context: ExtensionContext) -> None:
        context.app.include_router(_router("/hooks/hook/receive", "post"))

    app = create_api_app(
        db_path=_db(tmp_path),
        profile=profile,
        auth_provider=TestAuthProvider() if profile == SHARED else None,
        extensions=[_Ext("hook", add, public_paths=["/hooks/hook"])],
    )
    with TestClient(app) as client:
        assert client.post("/hooks/hook/receive").json() == {"extension": True}
        assert client.get("/api/v1/workflows").status_code == (401 if profile == SHARED else 200)


@pytest.mark.parametrize(
    ("identity", "invite"),
    [
        ("not-an-identity", False),
        (Identity(None, "default", "admin"), False),  # type: ignore[arg-type]
        (Identity(_principal(""), "default", "admin"), False),
        (Identity(_principal(), None, "admin"), False),
        (Identity(_principal(), None, None), False),
        (Identity(_principal(), "", "admin"), False),
        (Identity(_principal(), "default", "owner"), False),  # type: ignore[arg-type]
        (Identity(_principal(), "default", None), False),
        (Identity(_principal(), None, "admin"), True),
        (Identity(None, None, None), True),  # type: ignore[arg-type]
    ],
)
def test_invalid_identities_are_rejected(identity: Any, invite: bool) -> None:
    assert invalid_identity_reason(identity, without_membership=invite) is not None


def test_valid_identities_are_accepted() -> None:
    assert (
        invalid_identity_reason(Identity(_principal(), "w", "viewer"), without_membership=False)
        is None
    )
    assert (
        invalid_identity_reason(Identity(_principal(), None, None), without_membership=True) is None
    )


class _NoWorkspaceProvider:
    name = "no-workspace"
    is_authenticating = True

    def authenticate(self, request: Request, target_workspace_id: str | None) -> Identity:
        return Identity(_principal(), None, None)

    def is_operator(self, request: Request) -> bool:
        return False


def test_workspace_none_outside_invite_accept_fails_closed(tmp_path: Path) -> None:
    app = create_api_app(
        db_path=_db(tmp_path), profile=SHARED, auth_provider=_NoWorkspaceProvider()
    )
    with TestClient(app) as client:
        resp = client.get("/api/v1/workflows")
    assert resp.status_code == 500
    assert resp.json()["detail"]["code"] == "AUTH_PROVIDER_INVALID_IDENTITY"


def test_explicit_falsy_profile_manager_is_honoured(tmp_path: Path) -> None:
    class FalsyProfileManager(ProfileManager):
        def __bool__(self) -> bool:
            return False

    mine = FalsyProfileManager()
    app = create_api_app(db_path=_db(tmp_path), profile_manager=mine)
    assert app.state.profile_manager is mine


def test_workspace_free_identity_is_allowed_only_on_provider_declared_route(tmp_path: Path) -> None:
    class AcceptProvider(_NoWorkspaceProvider):
        routes_without_membership = frozenset({("POST", "/api/v1/test/accept")})

    def configure(context: ExtensionContext) -> None:
        context.api_v1.include_router(_router("/test/accept", "post"))

    app = create_api_app(
        db_path=_db(tmp_path),
        profile=SHARED,
        auth_provider=AcceptProvider(),
        extensions=[_Ext("accept", configure)],
    )
    with TestClient(app) as client:
        assert client.post("/api/v1/test/accept").status_code == 200
        assert client.get("/api/v1/workflows").status_code == 500
