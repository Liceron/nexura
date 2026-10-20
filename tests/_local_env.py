"""OSS fixtures: one local owner, independent project workspaces, no credentials.

The principal headers are deliberately forged. Only X-Workspace-ID selects a local
project; these headers must never select a different identity or reduce its role.
"""

from starlette.requests import Request

from nexura_runtime.service.api.middleware import AuthFailure
from nexura_runtime.service.auth.models import Principal
from nexura_runtime.service.extensions import Identity


def local_headers(workspace_id: str, role: str = "viewer") -> dict[str, str]:
    return {
        "X-Workspace-ID": workspace_id,
        "X-Principal-ID": "forged-other-user",
        "X-Principal-Role": role,
    }


class TestAuthProvider:
    """Explicit test-only authentication seam, using no tokens or paid services."""

    __test__ = False
    name = "oss-test-auth"
    is_authenticating = True
    routes_without_membership = frozenset({("POST", "/api/v1/test/accept")})

    def authenticate(self, request: Request, target_workspace_id: str | None) -> Identity:
        if request.headers.get("x-test-user") != "local-owner":
            raise AuthFailure(401, "TEST_UNAUTHENTICATED", "Test identity required")
        return Identity(
            Principal(principal_id="local-owner", profile="authenticated-shared-service"),
            target_workspace_id or "default",
            "admin",
        )

    def is_operator(self, request: Request) -> bool:
        return request.headers.get("x-test-operator") == "yes"
