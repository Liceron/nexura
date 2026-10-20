"""Test P29: Route coverage for FastAPI /api/v1 endpoints.

This test builds the API app, extracts every /api/v1 route (method + path template),
and asserts that each route is either:
1. Implemented as a typed client method on NexuraClient (or its subclients), or
2. Listed in an explicit, commented allowlist for routes that clients should not call
   (such as inbound webhook delivery).

Both maps must also be free of stale entries: every ALLOWLIST and COVERED_ROUTES key must be
a route of the open-source app (publishing routes moved to the closed edition in EDITIONS_PLAN
step 4, together with their entries).
"""

from __future__ import annotations

from typing import Any

import pytest
from starlette.routing import Mount

from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.service.api.client import NexuraClient

# Explicit allowlist of routes under /api/v1 that should NOT be called by the client SDK.
# Every entry MUST have a detailed rationale explaining why it is exempt from client coverage.
ALLOWLIST: dict[tuple[str, str], str] = {
    # 1. External webhook delivery receiver
    ("POST", "/api/v1/triggers/webhooks/{endpoint_id}/deliver"): (
        "External inbound webhook delivery receiver invoked by third-party webhook senders "
        "(GitHub, Stripe, custom services) using raw HTTP bodies and HMAC-SHA256 headers. "
        "It is not invoked by SDK client sessions."
    ),
    # 2. Internal workspace-scoped workflow list proxy
    ("GET", "/api/v1/workspaces/{workspace_id}/workflows"): (
        "Internal workspace-scoped workflow proxy route for legacy canvas components. "
        "SDK clients list workflows using client.workflows.list() with the X-Workspace-ID header "
        "or client.with_workspace(workspace_id)."
    ),
    # 3. Internal workspace-scoped run list proxy
    ("GET", "/api/v1/workspaces/{workspace_id}/runs"): (
        "Internal workspace-scoped run proxy route for legacy canvas components. "
        "SDK clients list runs using client.runs.list() with the X-Workspace-ID header "
        "or client.with_workspace(workspace_id)."
    ),
    # The public widget and visitor chat endpoints (PublicGatewayMiddleware) moved with publishing
    # to the closed edition (EDITIONS_PLAN step 4); its own route-coverage test lists them.
}


def _extract_routes(app: Any) -> list[tuple[str, str]]:
    """Recursively discover all HTTP routes with their methods and paths."""
    discovered: list[tuple[str, str]] = []

    def crawl(obj: Any, prefix: str = "") -> None:
        item_routes = []
        if hasattr(obj, "routes"):
            item_routes = obj.routes
        elif callable(getattr(obj, "effective_candidates", None)):
            item_routes = obj.effective_candidates()

        for r in item_routes:
            if callable(getattr(r, "effective_candidates", None)):
                crawl(r, prefix)
            elif isinstance(r, Mount):
                mount_prefix = f"{prefix}{r.path}".rstrip("/")
                crawl(r.app, mount_prefix)
            elif hasattr(r, "methods") and hasattr(r, "path"):
                for m in r.methods:
                    if m not in ("HEAD", "OPTIONS"):
                        discovered.append((m, f"{prefix}{r.path}"))
            elif hasattr(r, "routes"):
                crawl(r, prefix)

    crawl(app)
    # Check middleware for mounted sub-apps (an extension may serve routes from middleware)
    for m in getattr(app, "user_middleware", []):
        sub_app = getattr(m, "kwargs", {}).get("public_app")
        if sub_app:
            crawl(sub_app)

    return sorted(set(discovered))


def _resolve_client_method(client: NexuraClient, target: tuple[str, str]) -> Any:
    """Resolve subclient and method name from target tuple, e.g. ('evaluations', 'create_run')."""
    subclient_name, method_path = target
    obj = getattr(client, subclient_name)
    for part in method_path.split("."):
        obj = getattr(obj, part)
    return obj


def test_every_api_v1_route_covered_or_allowlisted() -> None:
    """Every /api/v1 route must have a client method or an explicit allowlist entry."""
    app = create_api_app(db_path=":memory:")
    all_routes = _extract_routes(app)
    api_v1_routes = [(method, path) for method, path in all_routes if path.startswith("/api/v1")]

    assert len(api_v1_routes) > 0, "No /api/v1 routes found in application"

    client = NexuraClient(base_url="http://test")
    missing_routes: list[tuple[str, str]] = []

    for method, path in api_v1_routes:
        route_key = (method, path)
        if route_key in ALLOWLIST:
            # Must have non-empty explanation
            assert len(ALLOWLIST[route_key].strip()) > 10, (
                f"Allowlist entry {route_key} lacks a descriptive rationale."
            )
            continue

        if route_key in NexuraClient.COVERED_ROUTES:
            target = NexuraClient.COVERED_ROUTES[route_key]
            try:
                method_obj = _resolve_client_method(client, target)
                assert callable(method_obj), f"Client method {target} is not callable"
            except AttributeError as exc:
                pytest.fail(
                    f"Route {route_key} mapped to non-existent client method {target}: {exc}"
                )
        else:
            missing_routes.append(route_key)

    if missing_routes:
        formatted = "\n".join(f"  - {m} {p}" for m, p in missing_routes)
        pytest.fail(
            f"The following {len(missing_routes)} /api/v1 routes lack Python client methods "
            f"and are not in ALLOWLIST:\n{formatted}"
        )


def test_covered_routes_all_exist_on_client() -> None:
    """Ensure every route in NexuraClient.COVERED_ROUTES points to an actual callable method."""
    client = NexuraClient(base_url="http://test")
    for (method, path), target in NexuraClient.COVERED_ROUTES.items():
        try:
            method_obj = _resolve_client_method(client, target)
            assert callable(method_obj), (
                f"Route ({method}, '{path}') points to target {target} which is not callable."
            )
        except AttributeError as exc:
            pytest.fail(
                f"Route ({method}, '{path}') target {target} cannot be resolved "
                f"on NexuraClient: {exc}"
            )


def test_allowlist_has_no_stale_or_empty_entries() -> None:
    """All allowlist entries must have clear justifications."""
    for (method, path), rationale in ALLOWLIST.items():
        assert method in ("GET", "POST", "PUT", "PATCH", "DELETE"), f"Invalid method {method}"
        assert path.startswith("/api/v1"), f"Path {path} does not start with /api/v1"
        assert isinstance(rationale, str) and len(rationale.strip()) > 20, (
            f"Allowlist entry ({method}, {path}) must have a detailed comment "
            "explaining why it is exempt."
        )


def test_missing_route_fails_coverage() -> None:
    """Verify that an unmapped, non-allowlisted route triggers a test failure."""
    fake_route = ("POST", "/api/v1/fake/uncovered/endpoint")
    route_in_covered = fake_route in NexuraClient.COVERED_ROUTES
    route_in_allowlist = fake_route in ALLOWLIST
    assert not route_in_covered and not route_in_allowlist


def test_allowlist_and_covered_routes_have_no_stale_entries() -> None:
    """Every ALLOWLIST and COVERED_ROUTES key is a route the open-source app serves."""
    app = create_api_app(db_path=":memory:")
    routes = set(_extract_routes(app))
    stale_allowlist = sorted(set(ALLOWLIST) - routes)
    stale_covered = sorted(set(NexuraClient.COVERED_ROUTES) - routes)
    assert not stale_allowlist, f"ALLOWLIST lists routes the app does not serve: {stale_allowlist}"
    assert not stale_covered, f"COVERED_ROUTES lists routes the app does not serve: {stale_covered}"


def test_closed_edition_routes_are_neither_served_nor_covered() -> None:
    """PKG-03: organisations, publishing, hosted secrets and workspace-member routes are closed
    edition routes. The open-source app serves none of them and ``COVERED_ROUTES`` maps none of
    them (their client methods live in ``nexura_enterprise.clients.EnterpriseClient``)."""
    closed = ("/organisations", "/published", "/secrets", "/members", "/my-membership")

    def is_closed(path: str) -> bool:
        return any(f"{seg}/" in f"{path}/" for seg in closed)

    served = [r for r in _extract_routes(create_api_app(db_path=":memory:")) if is_closed(r[1])]
    assert not served, served
    covered = [r for r in NexuraClient.COVERED_ROUTES if is_closed(r[1])]
    assert not covered, covered
    targets = {target[0] for target in NexuraClient.COVERED_ROUTES.values()}
    assert not targets & {"members", "secrets", "organisations", "published"}
