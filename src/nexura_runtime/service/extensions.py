# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Extension seams of the Nexura service: ``AuthProvider`` and ``ServiceExtension``.

This module only defines interfaces and the extension loader. It never imports an extension.

* ``AuthProvider`` decides who a request comes from. ``create_api_app`` installs exactly one
  provider; ``SecurityScopingMiddleware`` calls it for every ``/api/v1`` request and the health
  router calls ``is_operator`` before showing probe details. The in-tree providers live in
  ``nexura_runtime.service.api.auth_providers``.
* ``ServiceExtension`` adds routes, modules, capability flags and health fields while
  ``create_api_app`` builds the app. Extensions are passed explicitly with ``extensions=`` or
  named in ``enabled_extensions=``, which loads the matching entry points of the
  ``nexura.extensions`` group. Installing a package never loads an extension on its own.

``ExtensionHost`` checks what each extension did. No extension may replace the installed
``AuthProvider`` or the other security state (``PROTECTED_STATE``). An extension that is not
in-tree may also not replace or delete any ``app.state`` attribute, remove a route, add a route
whose method and path a built-in route or an earlier extension already uses, or add a route on
``app`` outside ``/api/v1`` that it has not declared in ``public_paths``. Each violation raises
``ExtensionError`` and stops startup.
"""

from __future__ import annotations

import importlib.metadata
import inspect
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from nexura_runtime.service.auth.errors import UnauthenticatedNetworkError
from nexura_runtime.service.auth.models import Principal, WorkspaceRole

if TYPE_CHECKING:
    from fastapi import APIRouter, FastAPI
    from starlette.requests import Request
    from starlette.routing import BaseRoute

    from nexura_runtime.workflow import ModuleRegistry

    #: One route as the router matches it: the original route, its full path, its methods.
    _FlatRoute = tuple[BaseRoute, str, frozenset[str] | None]

#: Entry-point group read by ``load_entry_point_extensions``.
EXTENSION_ENTRY_POINT_GROUP = "nexura.extensions"

#: ``app.state`` attributes no extension (in-tree or not) may replace or delete.
PROTECTED_STATE = (
    "auth_provider",
    "profile_manager",
    "workspace_repo",
    "extensions",
    "local_session",
    "session_token",
    "delivery_tokens",
)

#: Prefix of the routes ``SecurityScopingMiddleware`` authenticates.
AUTHENTICATED_PREFIX = "/api/v1"

#: Public paths of the service itself (health probes and API docs). No extension may use them.
RESERVED_PATHS = frozenset(
    {"/healthz", "/readyz", "/docs", "/openapi.json", "/redoc", "/docs/oauth2-redirect"}
)

_MISSING = object()


class ExtensionError(RuntimeError):
    """An extension or one of its contributions is invalid (type, name, capability, health)."""


class ExtensionLoadError(ExtensionError):
    """An enabled entry-point extension could not be found, imported or instantiated."""


class AuthProviderRequiredError(UnauthenticatedNetworkError):
    """The authenticated profile was requested without anything that can authenticate."""


@dataclass(frozen=True)
class Identity:
    """Result of ``AuthProvider.authenticate``.

    ``workspace_id`` and ``role`` may both be ``None`` only on a route the provider lists in
    ``routes_without_membership`` (for example the closed edition's organisation invite
    acceptance, where an authenticated person has no membership yet).
    """

    principal: Principal
    workspace_id: str | None
    role: WorkspaceRole | None


@runtime_checkable
class AuthProvider(Protocol):
    """Authenticates requests for ``/api/v1`` and decides who is a platform operator."""

    @property
    def name(self) -> str:
        """Short identifier, used in error messages."""
        ...

    @property
    def is_authenticating(self) -> bool:
        """True when the provider verifies credentials.

        Only an authenticating provider may serve the ``authenticated-shared-service`` profile
        or bind a non-loopback address without the insecure opt-in.
        """
        ...

    def authenticate(self, request: Request, target_workspace_id: str | None) -> Identity:
        """Return the identity for ``request``.

        ``target_workspace_id`` is the workspace from the ``/api/v1/workspaces/{id}`` path,
        else from the ``X-Workspace-ID`` header, else ``None``. Raise
        ``nexura_runtime.service.api.middleware.AuthFailure`` to refuse the request; its
        status, code and message are sent to the client as is.
        """
        ...

    def is_operator(self, request: Request) -> bool:
        """True when the caller may see host internals (full ``/readyz`` probe details)."""
        ...

    # Optional, not part of the runtime check: ``routes_without_membership``, a collection of
    # exact ``(method, path)`` pairs on which ``authenticate`` may return an ``Identity`` with
    # ``workspace_id`` and ``role`` both ``None``. Absent means no such route.


@dataclass
class _Contributions:
    capabilities: dict[str, bool] = field(default_factory=dict)
    health: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass(frozen=True)
class ExtensionContext:
    """What one extension receives in ``configure``.

    ``app`` and ``api_v1`` are the application under construction and its ``/api/v1`` router;
    ``api_v1`` is included into ``app`` after every extension has been configured. The context
    is read-only: ``auth_provider`` is the installed provider, for reading only, and nothing an
    extension does here can change which provider authenticates requests (``ExtensionHost``
    refuses startup if ``app.state.auth_provider`` is replaced).
    """

    extension_name: str
    app: FastAPI
    api_v1: APIRouter
    registry: ModuleRegistry
    auth_provider: AuthProvider
    reserved_capabilities: frozenset[str]
    _contributions: _Contributions

    def add_capabilities(self, flags: Mapping[str, bool]) -> None:
        """Add boolean flags to ``GET /api/v1/capabilities`` under ``capabilities``.

        A flag may not replace a built-in flag or a flag added by another extension.
        """
        for key, value in flags.items():
            if not isinstance(key, str) or not key:
                raise ExtensionError(
                    f"Extension '{self.extension_name}' added a capability with an invalid name."
                )
            if not isinstance(value, bool):
                raise ExtensionError(
                    f"Extension '{self.extension_name}' capability '{key}' must be a bool."
                )
            if key in self.reserved_capabilities or key in self._contributions.capabilities:
                raise ExtensionError(
                    f"Extension '{self.extension_name}' cannot redefine capability '{key}'."
                )
        self._contributions.capabilities.update(flags)

    def add_health_fields(self, fields: Mapping[str, Any]) -> None:
        """Add JSON fields to the operator view of ``/readyz`` under ``extensions.<name>``.

        Callers who are not operators never see them.
        """
        merged = {**self._contributions.health.get(self.extension_name, {}), **fields}
        try:
            json.dumps(merged)
        except (TypeError, ValueError) as exc:
            raise ExtensionError(
                f"Extension '{self.extension_name}' health fields must be JSON serialisable."
            ) from exc
        self._contributions.health[self.extension_name] = merged


@runtime_checkable
class ServiceExtension(Protocol):
    """Something that adds routes, modules, capabilities or health fields to the service."""

    @property
    def name(self) -> str:
        """Unique, non-empty extension name."""
        ...

    def configure(self, context: ExtensionContext) -> None:
        """Mount routers on ``context.api_v1``, register modules on ``context.registry``,
        add new ``context.app.state`` attributes of its own and call
        ``add_capabilities``/``add_health_fields``. Routes on ``context.app`` itself are allowed
        only under ``/api/v1`` or under a path the extension lists in an optional
        ``public_paths`` attribute (those routes are not authenticated). Any exception aborts
        ``create_api_app`` and propagates with its own type.
        """
        ...


def _instantiate(entry_name: str, value: str, obj: Any) -> ServiceExtension:
    """An entry point names a ``ServiceExtension`` instance, a class or a zero-argument factory."""
    if inspect.isclass(obj) or (not isinstance(obj, ServiceExtension) and callable(obj)):
        try:
            obj = obj()
        except Exception as exc:
            raise ExtensionLoadError(
                f"Extension entry point '{entry_name}' ({value}) failed to instantiate: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
    if not isinstance(obj, ServiceExtension):
        raise ExtensionLoadError(
            f"Extension entry point '{entry_name}' ({value}) is not a ServiceExtension "
            "(it needs a 'name' and a 'configure(context)' method)."
        )
    return obj


def load_entry_point_extensions(
    names: Iterable[str], *, group: str = EXTENSION_ENTRY_POINT_GROUP
) -> list[ServiceExtension]:
    """Load the named entry points of ``group``, in the order given.

    Nothing is read from package metadata when ``names`` is empty. Every name must match
    exactly one installed entry point; an unknown, duplicated or ambiguous name, an import
    error or an object that is not a ``ServiceExtension`` raises ``ExtensionLoadError``.
    """
    wanted = list(names)
    if not wanted:
        return []
    seen: set[str] = set()
    for name in wanted:
        if not isinstance(name, str) or not name.strip():
            raise ExtensionLoadError("Enabled extension names must be non-empty strings.")
        if name in seen:
            raise ExtensionLoadError(f"Extension '{name}' is enabled more than once.")
        seen.add(name)

    by_name: dict[str, list[importlib.metadata.EntryPoint]] = {}
    for ep in importlib.metadata.entry_points(group=group):
        by_name.setdefault(ep.name, []).append(ep)

    loaded: list[ServiceExtension] = []
    for name in wanted:
        matches = by_name.get(name, [])
        if not matches:
            raise ExtensionLoadError(
                f"Extension '{name}' is enabled but no installed package provides an entry "
                f"point named '{name}' in group '{group}'."
            )
        if len(matches) > 1:
            values = ", ".join(sorted(ep.value for ep in matches))
            raise ExtensionLoadError(
                f"Extension '{name}' is ambiguous: several entry points in group '{group}' "
                f"use that name ({values})."
            )
        ep = matches[0]
        try:
            obj = ep.load()
        except Exception as exc:
            raise ExtensionLoadError(
                f"Extension '{name}' ({ep.value}) failed to import: {type(exc).__name__}: {exc}"
            ) from exc
        loaded.append(_instantiate(name, ep.value, obj))
    return loaded


def check_extensions(
    extensions: Iterable[Any], *, taken: Iterable[str] = ()
) -> list[ServiceExtension]:
    """Validate extensions before any is configured: type, non-empty and unique names."""
    names = set(taken)
    checked: list[ServiceExtension] = []
    for ext in extensions:
        if not isinstance(ext, ServiceExtension):
            raise ExtensionError(
                f"{type(ext).__name__} is not a ServiceExtension "
                "(it needs a 'name' and a 'configure(context)' method)."
            )
        name = ext.name
        if not isinstance(name, str) or not name.strip():
            raise ExtensionError("A ServiceExtension needs a non-empty string name.")
        if name in names:
            raise ExtensionError(f"Two extensions use the name '{name}'.")
        names.add(name)
        checked.append(ext)
    return checked


def _methods(methods: Any) -> frozenset[str] | None:
    return frozenset(str(m).upper() for m in methods) if methods else None


def _flatten(routes: Sequence[BaseRoute]) -> list[_FlatRoute]:
    """Every route reachable from ``routes``, with its effective path.

    FastAPI 0.144+ includes a router lazily (one ``_IncludedRouter`` entry that resolves the
    sub-router's routes on demand, so routes added later still appear); older versions copy the
    routes at include time. Both are handled.
    """
    from fastapi import routing as fastapi_routing

    iter_contexts = getattr(fastapi_routing, "iter_route_contexts", None)
    if iter_contexts is None:
        return [
            (r, str(getattr(r, "path", "")), _methods(getattr(r, "methods", None))) for r in routes
        ]
    return [
        (ctx.original_route, str(ctx.path or ""), _methods(ctx.methods))
        for ctx in iter_contexts(list(routes))
    ]


def _route_keys(flat: _FlatRoute) -> set[tuple[str, str]]:
    """``(method, path)`` pairs a route answers; ``"*"`` for a mount (every method)."""
    from starlette.routing import Mount, WebSocketRoute

    route, path, methods = flat
    if isinstance(route, Mount):
        return {("*", path)}
    if isinstance(route, WebSocketRoute):
        return {("WEBSOCKET", path)}
    return {(m, path) for m in (methods or {"*"})}


def _flat_id(flat: _FlatRoute) -> tuple[int, str]:
    return id(flat[0]), flat[1]


def _under(path: str, prefix: str) -> bool:
    prefix = prefix.rstrip("/")
    return path == prefix or path.startswith(prefix + "/")


def _public_paths(ext: ServiceExtension) -> frozenset[str]:
    """The validated ``public_paths`` declaration of ``ext`` (empty when it has none)."""
    declared = getattr(ext, "public_paths", ())
    if isinstance(declared, str) or not isinstance(declared, Iterable):
        raise ExtensionError(
            f"Extension '{ext.name}' public_paths must be a collection of path strings."
        )
    paths: set[str] = set()
    for raw in declared:
        if (
            not isinstance(raw, str)
            or not raw.startswith("/")
            or raw.rstrip("/") == ""
            or any(ch in raw for ch in "{}*?#")
        ):
            raise ExtensionError(
                f"Extension '{ext.name}' public path {raw!r} must be a literal path such as "
                "'/hooks/my-extension'."
            )
        path = raw.rstrip("/")
        if _under(path, "/api") or any(_under(r, path) or _under(path, r) for r in RESERVED_PATHS):
            raise ExtensionError(
                f"Extension '{ext.name}' cannot declare the public path '{raw}': it overlaps "
                "/api or the service's health and documentation paths."
            )
        paths.add(path)
    return frozenset(paths)


@dataclass
class ExtensionHost:
    """Configures extensions in order, checks what each one changed, keeps contributions."""

    app: FastAPI
    api_v1: APIRouter
    registry: ModuleRegistry
    auth_provider: AuthProvider
    reserved_capabilities: frozenset[str] = frozenset()
    configured: list[str] = field(default_factory=list)
    _contributions: _Contributions = field(default_factory=_Contributions)
    #: Routes extensions added on ``app`` itself (moved after the built-in routes at the end).
    _app_routes: list[BaseRoute] = field(default_factory=list)
    #: ``public_paths`` declared by configured extensions (not checked for the session token).
    public_paths: set[str] = field(default_factory=set)

    @property
    def capabilities(self) -> dict[str, bool]:
        """Capability flags added by extensions."""
        return dict(self._contributions.capabilities)

    @property
    def health_fields(self) -> dict[str, dict[str, Any]]:
        """Health fields added by extensions, keyed by extension name."""
        return {name: dict(fields) for name, fields in self._contributions.health.items()}

    def check_auth_provider(self) -> None:
        """Raise ``ExtensionError`` unless ``app.state.auth_provider`` is the installed one."""
        if getattr(self.app.state, "auth_provider", _MISSING) is not self.auth_provider:
            raise ExtensionError(
                "app.state.auth_provider is no longer the installed AuthProvider "
                f"'{self.auth_provider.name}'; extensions cannot replace it."
            )

    def place_extension_routes_last(self) -> None:
        """Move the routes extensions added on ``app`` after every other route.

        Starlette uses the first matching route, so a built-in route always wins over an
        extension route whose pattern overlaps it.
        """
        ext_ids = {id(r) for r in self._app_routes}
        routes = self.app.router.routes
        routes[:] = [r for r in routes if id(r) not in ext_ids] + [
            r for r in routes if id(r) in ext_ids
        ]

    def _check_state(self, name: str, before: dict[str, Any], *, in_tree: bool) -> None:
        state = self.app.state._state
        for attr in PROTECTED_STATE:
            if state.get(attr, _MISSING) is not before.get(attr, _MISSING):
                raise ExtensionError(
                    f"Extension '{name}' replaced or deleted app.state.{attr}; extensions "
                    "cannot change authentication or workspace membership state."
                )
        self.check_auth_provider()
        if in_tree:
            return
        for attr, value in before.items():
            if state.get(attr, _MISSING) is not value:
                raise ExtensionError(
                    f"Extension '{name}' replaced or deleted app.state.{attr}; an extension may "
                    "only add new app.state attributes."
                )

    def _check_routes(
        self,
        ext: ServiceExtension,
        before_app: list[BaseRoute],
        before_flat: list[_FlatRoute],
    ) -> None:
        name = ext.name
        public = _public_paths(ext)
        self.public_paths |= set(public)
        app_flat = _flatten(self.app.router.routes)
        after_flat = [*app_flat, *_flatten(self.api_v1.routes)]
        after_ids = {_flat_id(f) for f in after_flat}
        if any(_flat_id(f) not in after_ids for f in before_flat):
            raise ExtensionError(f"Extension '{name}' removed an existing route.")
        before_ids = {_flat_id(f) for f in before_flat}
        taken: set[tuple[str, str]] = set()
        for flat in before_flat:
            taken |= _route_keys(flat)
        taken_paths = {p for _, p in taken} | RESERVED_PATHS
        added = [f for f in after_flat if _flat_id(f) not in before_ids]
        for flat in added:
            for method, path in _route_keys(flat):
                clash = (
                    (method, path) in taken
                    or ("*", path) in taken
                    or path in RESERVED_PATHS
                    or (method != "*" and any(m == "*" and _under(path, p) for m, p in taken))
                    or (method == "*" and any(_under(p, path) for p in taken_paths))
                )
                if clash or path.rstrip("/") == "":
                    raise ExtensionError(
                        f"Extension '{name}' route {method} {path or '/'} collides with a "
                        "built-in route or a route of an earlier extension."
                    )
                taken.add((method, path))
        app_ids = {_flat_id(f) for f in app_flat}
        for flat in added:
            path = flat[1]
            if _flat_id(flat) not in app_ids or _under(path, AUTHENTICATED_PREFIX):
                continue  # on api_v1, or under /api/v1: behind the AuthProvider
            if not any(_under(path, p) for p in public):
                raise ExtensionError(
                    f"Extension '{name}' added the route {path or '/'} on app outside "
                    f"{AUTHENTICATED_PREFIX}. Routes there are not authenticated: mount the route "
                    "on context.api_v1, or list its path in the extension's public_paths."
                )
        before_app_ids = {id(r) for r in before_app}
        self._app_routes.extend(r for r in self.app.router.routes if id(r) not in before_app_ids)

    def configure(self, extensions: Sequence[ServiceExtension], *, in_tree: bool = False) -> None:
        """Call ``configure`` on each extension in order; the first failure aborts startup.

        After each extension the installed ``AuthProvider`` and ``PROTECTED_STATE`` are
        re-checked. Unless ``in_tree`` is true (an extension that ships in this package; there
        is none at the moment), the extension's ``app.state`` changes and routes are checked too.
        """
        for ext in check_extensions(extensions, taken=self.configured):
            state_before = dict(self.app.state._state)
            app_before = list(self.app.router.routes)
            flat_before = (
                []
                if in_tree
                else [*_flatten(self.app.router.routes), *_flatten(self.api_v1.routes)]
            )
            context = ExtensionContext(
                extension_name=ext.name,
                app=self.app,
                api_v1=self.api_v1,
                registry=self.registry,
                auth_provider=self.auth_provider,
                reserved_capabilities=self.reserved_capabilities,
                _contributions=self._contributions,
            )
            try:
                ext.configure(context)
            except Exception as exc:
                # The original exception propagates unchanged (callers may rely on its type);
                # the note names the extension in the traceback.
                exc.add_note(f"while configuring Nexura service extension '{ext.name}'")
                raise
            self._check_state(ext.name, state_before, in_tree=in_tree)
            if not in_tree:
                self._check_routes(ext, app_before, flat_before)
            self.configured.append(ext.name)


__all__ = [
    "AUTHENTICATED_PREFIX",
    "EXTENSION_ENTRY_POINT_GROUP",
    "PROTECTED_STATE",
    "RESERVED_PATHS",
    "AuthProvider",
    "AuthProviderRequiredError",
    "ExtensionContext",
    "ExtensionError",
    "ExtensionHost",
    "ExtensionLoadError",
    "Identity",
    "ServiceExtension",
    "check_extensions",
    "load_entry_point_extensions",
]
