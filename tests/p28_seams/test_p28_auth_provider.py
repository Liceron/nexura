"""P28 step 2: the ``AuthProvider`` seam and the fail-closed shared profile."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.service.api.auth_providers import (
    LocalOwnerAuthProvider,
    resolve_auth_provider,
)
from nexura_runtime.service.api.middleware import AuthFailure
from nexura_runtime.service.auth import ProfileManager, UnauthenticatedNetworkError
from nexura_runtime.service.auth.models import Principal
from nexura_runtime.service.extensions import AuthProvider, AuthProviderRequiredError, Identity

SHARED = "authenticated-shared-service"


class HeaderAuthProvider:
    """Test provider: ``X-Test-User`` names the principal; ``ops`` is the only operator."""

    name = "header-test"
    is_authenticating = True

    def __init__(self) -> None:
        self.calls: list[str | None] = []

    def authenticate(self, request: Request, target_workspace_id: str | None) -> Identity:
        self.calls.append(target_workspace_id)
        user = request.headers.get("x-test-user")
        if not user:
            raise AuthFailure(401, "TEST_UNAUTHENTICATED", "Who are you?")
        return Identity(
            Principal(principal_id=user, profile=SHARED),
            target_workspace_id or "default",
            "admin",
        )

    def is_operator(self, request: Request) -> bool:
        return request.headers.get("x-test-user") == "ops"


class QuietLocalProvider(LocalOwnerAuthProvider):
    """Non-authenticating provider whose callers are never operators."""

    name = "quiet-local"

    def is_operator(self, request: Request) -> bool:
        return False


def _db(tmp_path: Path) -> str:
    return str(tmp_path / "svc.sqlite3")


def test_shared_profile_without_provider_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(AuthProviderRequiredError) as info:
        create_api_app(db_path=_db(tmp_path), profile=SHARED)
    # Callers that caught the pre-seam error type still catch it.
    assert isinstance(info.value, UnauthenticatedNetworkError)
    assert "auth_provider" in str(info.value)


def test_authenticating_provider_allows_a_network_bind(tmp_path: Path) -> None:
    create_api_app(
        db_path=_db(tmp_path),
        profile=SHARED,
        auth_provider=HeaderAuthProvider(),
        listen_host="0.0.0.0",
    )
    with pytest.raises(UnauthenticatedNetworkError):
        create_api_app(
            db_path=_db(tmp_path), auth_provider=QuietLocalProvider(), listen_host="0.0.0.0"
        )


def test_readyz_details_follow_is_operator(tmp_path: Path) -> None:
    app = create_api_app(db_path=_db(tmp_path), profile=SHARED, auth_provider=HeaderAuthProvider())
    with TestClient(app) as client:
        anonymous = client.get("/readyz").json()
        tenant = client.get("/readyz", headers={"x-test-user": "alice"}).json()
        operator = client.get("/readyz", headers={"x-test-user": "ops"}).json()
    assert set(anonymous) == {"status"}
    assert set(tenant) == {"status"}
    assert "probes" in operator


def test_local_readyz_details_also_ask_the_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.environ.get("NEXURA_DATABASE_URL"):
        # Both apps share this test's Postgres database, which is bound to one blob store
        # (MVP WP3), so they share one blob folder too, as two instances of one deployment do.
        monkeypatch.setenv("NEXURA_BLOB_URL", (tmp_path / "blobs").as_uri())
    app = create_api_app(db_path=_db(tmp_path), auth_provider=QuietLocalProvider())
    with TestClient(app) as client:
        assert set(client.get("/readyz").json()) == {"status"}
    default = create_api_app(db_path=str(tmp_path / "other.sqlite3"))
    with TestClient(default) as client:
        assert "probes" in client.get("/readyz").json()


def test_middleware_fails_closed_when_the_shared_provider_is_missing(tmp_path: Path) -> None:
    app = create_api_app(db_path=_db(tmp_path), profile=SHARED, auth_provider=HeaderAuthProvider())
    headers = {"x-test-user": "alice"}
    with TestClient(app) as client:
        app.state.auth_provider = None
        missing = client.get("/api/v1/workflows", headers=headers)
        assert missing.status_code == 503
        assert missing.json()["detail"]["code"] == "AUTH_PROVIDER_REQUIRED"
        app.state.auth_provider = LocalOwnerAuthProvider()
        weak = client.get("/api/v1/workflows", headers=headers)
        assert weak.status_code == 503
        assert weak.json()["detail"]["code"] == "AUTH_PROVIDER_REQUIRED"


def test_local_middleware_without_a_provider_is_the_local_owner(tmp_path: Path) -> None:
    app = create_api_app(db_path=_db(tmp_path))
    with TestClient(app) as client:
        app.state.auth_provider = None
        resp = client.get("/api/v1/workflows")
    assert resp.status_code == 200


def test_local_and_explicit_test_providers_implement_the_protocol() -> None:
    assert isinstance(LocalOwnerAuthProvider(), AuthProvider)
    assert isinstance(HeaderAuthProvider(), AuthProvider)
    assert LocalOwnerAuthProvider.is_authenticating is False
    assert HeaderAuthProvider.is_authenticating is True


def test_default_app_installs_the_local_owner_provider(tmp_path: Path) -> None:
    app = create_api_app(db_path=_db(tmp_path))
    assert isinstance(app.state.auth_provider, LocalOwnerAuthProvider)
    assert app.state.auth_provider.is_authenticating is False


@pytest.mark.parametrize("provider", [LocalOwnerAuthProvider(), QuietLocalProvider()])
def test_shared_profile_refuses_a_non_authenticating_provider(
    tmp_path: Path, provider: AuthProvider
) -> None:
    with pytest.raises(AuthProviderRequiredError):
        create_api_app(db_path=_db(tmp_path), profile=SHARED, auth_provider=provider)


def test_authenticating_provider_needs_the_shared_profile(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="authenticated-shared-service"):
        create_api_app(db_path=_db(tmp_path), auth_provider=HeaderAuthProvider())


def test_auth_provider_must_implement_the_protocol() -> None:
    with pytest.raises(TypeError, match="AuthProvider"):
        resolve_auth_provider(object(), ProfileManager())  # type: ignore[arg-type]


def test_explicit_provider_authenticates_api_requests(tmp_path: Path) -> None:
    provider = HeaderAuthProvider()
    app = create_api_app(db_path=_db(tmp_path), profile=SHARED, auth_provider=provider)
    assert app.state.auth_provider is provider
    with TestClient(app) as client:
        refused = client.get("/api/v1/workflows")
        assert refused.status_code == 401
        assert refused.json()["detail"]["code"] == "TEST_UNAUTHENTICATED"
        ok = client.get("/api/v1/workflows", headers={"x-test-user": "alice"})
        assert ok.status_code == 200
        client.get("/api/v1/workspaces/ws-1/workflows", headers={"x-test-user": "alice"})
    assert provider.calls[-1] == "ws-1"
