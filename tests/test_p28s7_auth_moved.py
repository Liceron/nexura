# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""P28 step 7: authentication and membership left the open-source edition.

EDITIONS_PLAN section 3: the open-source edition has one principal (``local-owner``, role
``admin``) and no logins, tokens, API keys, members or invites; ``X-Principal-*`` headers are not
trusted; the bind guard still refuses a network bind without the explicit opt-in, and its
message points at the enterprise edition. These tests run with no closed edition installed.
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import re
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi import APIRouter, Request
from fastapi.testclient import TestClient

from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.service.api.bind_guard import INSECURE_ENV_VAR, assert_safe_bind
from nexura_runtime.service.auth.errors import UnauthenticatedNetworkError
from nexura_runtime.service.auth.profile import ProfileManager
from nexura_runtime.service.extensions import AuthProviderRequiredError, ExtensionContext
from nexura_runtime.service.workspaces.repository import WorkspaceRepository

ROOT = Path(__file__).resolve().parents[1]
SHARED = "authenticated-shared-service"
SPOOF = {"x-principal-id": "mallory", "x-principal-role": "viewer"}


@pytest.fixture(autouse=True)
def _no_closed_edition() -> None:
    assert importlib.util.find_spec("nexura_enterprise") is None
    assert "nexura_enterprise" not in sys.modules


class _WhoAmI:
    """A test extension that reports what the middleware derived for the request."""

    name = "test.whoami"

    def configure(self, context: ExtensionContext) -> None:
        router = APIRouter()

        @router.get("/whoami")
        async def whoami(request: Request) -> dict[str, Any]:
            principal = request.state.principal
            return {
                "principal_id": principal.principal_id,
                "profile": principal.profile,
                "workspace_id": request.state.workspace_id,
                "role": request.state.role,
            }

        context.api_v1.include_router(router)


def _app(tmp_path: Path, **kwargs: Any) -> Any:
    return create_api_app(db_path=str(tmp_path / "svc.sqlite3"), **kwargs)


def test_moved_modules_and_symbols_are_gone() -> None:
    for module in (
        "nexura_runtime.service.auth.tokens",
        "nexura_runtime.service.auth.api_keys",
        "nexura_runtime.service.module",
    ):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(module)
    gone = {
        "nexura_runtime.service.auth": ("ApiKeyManager", "TokenValidator", "create_token"),
        "nexura_runtime.service.auth.models": ("ApiKey", "TokenPayload", "MembershipAuditEvent"),
        "nexura_runtime.service.auth.resources": (
            "RESOURCE_SCOPED_ROUTES",
            "route_allows_resource_scoped",
            "validate_resources",
        ),
        "nexura_runtime.service.api.middleware": (
            "authenticate_shared",
            "extract_credentials",
            "INVITE_ACCEPT_ROUTE",
        ),
        "nexura_runtime.service.api.auth_providers": (
            "SharedServiceAuthProvider",
            "ProfileAuthProvider",
        ),
        "nexura_runtime.service.api.run_revocation": ("api_key_revocation_listener",),
        "nexura_runtime.service": ("AuthServiceModule", "WorkspaceServiceModule", "ApiKeyManager"),
    }
    for module, names in gone.items():
        mod = importlib.import_module(module)
        for name in names:
            assert not hasattr(mod, name), f"{module}.{name}"
    # Kept SHARED-CORE pieces.
    from nexura_runtime.service.auth.resources import require_resource

    assert callable(require_resource)
    params = inspect.signature(create_api_app).parameters
    for name in ("token_validator", "api_key_manager", "operator_principal_ids"):
        assert name not in params
    for name in ("profile", "profile_manager", "auth_provider", "extensions"):
        assert name in params
    for attr in ("resolve_principal", "token_validator", "api_key_manager"):
        assert not hasattr(ProfileManager(), attr)
    for attr in ("add_member", "update_member_role", "remove_member", "get_audit_events"):
        assert not hasattr(WorkspaceRepository(), attr), attr
    assert not (ROOT / "examples" / "backend" / "b24.py").exists()


def test_the_local_owner_is_the_only_member_of_every_workspace() -> None:
    repo = WorkspaceRepository()
    repo.create_workspace("proj", "Project", "local-owner")
    member = repo.get_member("proj", "local-owner")
    assert member is not None and member.role == "admin"
    assert repo.get_member("proj", "bob") is None
    assert repo.get_member("missing", "local-owner") is None
    assert not repo.is_removed_member("proj", "local-owner")
    assert {w.workspace_id for w in repo.list_workspaces_for_principal("local-owner")} == {
        "default",
        "proj",
    }
    assert repo.list_workspaces_for_principal("bob") == []


def test_x_principal_headers_cannot_change_identity_or_role(tmp_path: Path) -> None:
    app = _app(tmp_path, extensions=[_WhoAmI()])
    with TestClient(app) as client:
        plain = client.get("/api/v1/whoami").json()
        assert plain == {
            "principal_id": "local-owner",
            "profile": "local-owner",
            "workspace_id": "default",
            "role": "admin",
        }
        for headers in (
            SPOOF,
            {"x-principal-id": "root", "x-principal-role": "admin"},
            {"X-Principal-ID": "mallory"},
            {"x-principal-role": "viewer"},
        ):
            assert client.get("/api/v1/whoami", headers=headers).json() == plain
        # A viewer header does not take away the local owner's rights either.
        made = client.post(
            "/api/v1/workspaces",
            headers=SPOOF,
            json={"workspace_id": "proj", "name": "Project"},
        )
        assert made.status_code == 201, made.text
        scoped = client.get("/api/v1/workspaces/proj/workflows", headers=SPOOF)
        assert scoped.status_code == 200
        assert client.get("/api/v1/whoami", headers={**SPOOF, "x-workspace-id": "proj"}).json() == {
            **plain,
            "workspace_id": "proj",
        }


def test_member_token_and_api_key_routes_are_404(tmp_path: Path) -> None:
    app = _app(tmp_path)
    with TestClient(app) as client:
        assert client.get("/api/v1/workspaces/default/members").status_code == 404
        assert client.get("/api/v1/workspaces/default/my-membership").status_code == 404
        added = client.post(
            "/api/v1/workspaces/default/members", json={"principal_id": "bob", "role": "admin"}
        )
        assert added.status_code in (404, 405)
        for method in ("PUT", "DELETE"):
            r = client.request(method, "/api/v1/workspaces/default/members/bob", json={})
            assert r.status_code in (404, 405)
        for path in ("/api/v1/api-keys", "/api/v1/tokens", "/api/v1/auth/token"):
            assert client.post(path, json={}).status_code in (404, 405)
        paths = set(client.get("/openapi.json").json()["paths"])
    assert not [p for p in paths if re.search(r"members|membership|api[-_]?key|token", p)]


def test_the_single_local_principal_reaches_every_route(tmp_path: Path) -> None:
    """No /api/v1 route refuses the local owner for authentication or authorisation reasons."""
    app = _app(tmp_path)
    auth_codes = {
        "UNAUTHENTICATED",
        "AUTHENTICATION_FAILED",
        "PERMISSION_DENIED",
        "AUTH_PROVIDER_REQUIRED",
        "CROSS_WORKSPACE_ACCESS_DENIED",
        "RESOURCE_SCOPE_DENIED",
    }
    checked = 0
    with TestClient(app) as client:
        routes = client.get("/openapi.json").json()["paths"]
        for path, ops in sorted(routes.items()):
            if not path.startswith("/api/v1"):
                continue
            concrete = re.sub(r"\{[^}]+\}", "p28s7-missing", path)
            for method in ops:
                if method.upper() not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                    continue
                if concrete.endswith(("/events", "/stream")) or "stream" in concrete:
                    continue  # server-sent streams; they authenticate like any other route
                r = client.request(method.upper(), concrete, json={})
                checked += 1
                assert r.status_code != 503, (method, path, r.status_code, r.text)
                body = r.json() if "json" in r.headers.get("content-type", "") else {}
                detail = body.get("detail") if isinstance(body, dict) else None
                code = detail.get("code") if isinstance(detail, dict) else None
                assert code not in auth_codes, (method, path, r.status_code, r.text)
                if r.status_code == 401:
                    # Only a webhook delivery may answer 401: its HMAC signature is missing.
                    assert code == "MISSING_SIGNATURE_HEADERS", (method, path, r.text)
    assert checked > 100


def test_shared_profile_needs_the_enterprise_edition(tmp_path: Path) -> None:
    with pytest.raises(AuthProviderRequiredError, match="enterprise edition"):
        _app(tmp_path, profile=SHARED)
    # Switched at runtime with nothing that authenticates: fails closed, never the local owner.
    app = _app(tmp_path)
    app.state.profile_manager.set_profile(SHARED)
    with TestClient(app) as client:
        r = client.get("/api/v1/workflows")
        assert r.status_code == 503
        assert r.json()["detail"]["code"] == "AUTH_PROVIDER_REQUIRED"


def test_non_loopback_bind_is_refused_with_the_new_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(INSECURE_ENV_VAR, raising=False)
    with pytest.raises(UnauthenticatedNetworkError) as exc:
        _app(tmp_path, listen_host="0.0.0.0")
    message = str(exc.value)
    assert "refuses to listen" in message
    assert "Nexura enterprise edition" in message
    assert "token_validator" not in message and "api_key_manager" not in message
    with pytest.raises(UnauthenticatedNetworkError, match="enterprise edition"):
        assert_safe_bind("192.168.1.10", authenticated=False)
    # Loopback needs nothing; the loud opt-in still works.
    assert_safe_bind("127.0.0.1", authenticated=False)
    assert _app(tmp_path, listen_host="0.0.0.0", allow_unauthenticated_network=True)


def test_no_oss_source_references_the_moved_code() -> None:
    pattern = re.compile(
        r"service\.auth\.(tokens|api_keys)\b|service\.module\b|client\.members\b"
        r"|authenticate_shared|SharedServiceAuthProvider"
    )
    roots = ("src", "packages", "examples", "scripts", "clients/typescript/src", "studio-web/src")
    hits = []
    for base in roots:
        for path in (ROOT / base).rglob("*"):
            if path.suffix not in {".py", ".ts", ".tsx"} or "node_modules" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if pattern.search(text):
                hits.append(path.relative_to(ROOT).as_posix())
    # PKG-03: no OSS source names client.members any more (the Studio helper has no member
    # client; the closed edition registers a membership source).
    assert hits == []
    # Neither client has a members client any more: the TypeScript one since step 7, the Python
    # one since PKG-03 (both moved to the closed edition with organisations, secrets, published).
    ts = "".join(
        p.read_text(encoding="utf-8") for p in (ROOT / "clients/typescript/src").glob("*.ts")
    )
    assert "WorkspaceMembersClient" not in ts and "my-membership" not in ts
    py = (ROOT / "src/nexura_runtime/service/api/client.py").read_text(encoding="utf-8")
    assert "WorkspaceMembersClient" not in py and "my-membership" not in py
