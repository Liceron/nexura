# Extending the service and Studio

The Nexura service (`create_api_app`) and Studio have a few extension points. Extensions use
them to add routes, modules, capability flags and Studio pages without editing this package.
This page describes exactly what exists today.

## The four Python interfaces

| Interface | Module | What it decides |
|---|---|---|
| `AuthProvider` | `nexura_runtime.service.extensions` | Who a `/api/v1` request comes from, and who is an operator |
| `ServiceExtension` | `nexura_runtime.service.extensions` | Extra routes, modules, capability flags and health fields |
| `CredentialBackend` | `nexura_runtime.credentials.backends` | Where `ScopedCredentialStore` keeps secret values |
| `MembershipStore` | `nexura_runtime.service.workspaces.membership` | Read-only workspace membership lookups; OSS always has the local owner only |

All four are `typing.Protocol` classes marked `runtime_checkable`. Any object with the right
attributes and methods qualifies; you do not subclass anything.

## Service extensions

A service extension has a `name` and a `configure(context)` method. `create_api_app` calls
`configure` once, while it builds the app. The `ExtensionContext` gives you:

- `context.app`: the FastAPI app under construction. See "What an extension may change" below.
- `context.api_v1`: the `/api/v1` router. Routers you include here sit behind the installed
  `AuthProvider`, like every built-in route.
- `context.registry`: the module registry, for registering workflow modules.
- `context.auth_provider`: the installed `AuthProvider`, for reading only. The context is
  frozen, and an extension has no supported way to change which provider authenticates
  requests.
- `context.add_capabilities({...})`: boolean flags added to `GET /api/v1/capabilities` under
  `capabilities`. A flag may not replace a built-in flag or another extension's flag.
- `context.add_health_fields({...})`: JSON fields added to `/readyz` under
  `extensions.<name>`. They appear only to callers for whom `AuthProvider.is_operator` is true.

```python
from fastapi import APIRouter
from fastapi.testclient import TestClient

from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.service.extensions import ExtensionContext


class HelloExtension:
    name = "hello"

    def configure(self, context: ExtensionContext) -> None:
        router = APIRouter(prefix="/hello", tags=["hello"])

        @router.get("")
        async def hello() -> dict[str, str]:
            return {"message": "hello"}

        context.api_v1.include_router(router)
        context.add_capabilities({"hello": True})
        context.add_health_fields({"greetings_ready": True})


app = create_api_app(db_path=":memory:", extensions=[HelloExtension()])
# The local service accepts loopback hosts and peers only.
with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as client:
    assert client.get("/api/v1/hello").json() == {"message": "hello"}
    assert client.get("/api/v1/capabilities").json()["capabilities"]["hello"] is True
    # The local owner is the operator of a local service, so it sees the health fields.
    assert client.get("/readyz").json()["extensions"] == {"hello": {"greetings_ready": True}}
```

### What an extension may change

`create_api_app` checks each extension right after its `configure` returns. An extension may:

- include routers on `context.api_v1` (authenticated by the installed `AuthProvider`);
- register modules on `context.registry`;
- call `context.add_capabilities` and `context.add_health_fields`;
- add **new** `context.app.state` attributes for its own objects (use names that start with
  the extension name, for example `app.state.hello_store`);
- add routes on `context.app` under `/api/v1` (also authenticated), or under a path it declares
  in `public_paths` (see below).

Anything else fails startup with `ExtensionError` naming the extension:

- replacing or deleting any existing `app.state` attribute. This includes
  `auth_provider`, `profile_manager`, `workspace_repo` and `extensions`
  (`nexura_runtime.service.extensions.PROTECTED_STATE`), which not even the in-tree
  extensions may replace. After every extension has been configured, `create_api_app` also
  checks that `app.state.auth_provider` is still the installed provider;
- removing a route, or adding a route whose method and path are already used by a built-in
  route or by an earlier extension (a mount may not cover such a path either);
- using `/healthz`, `/readyz` or the API documentation paths;
- adding a route on `context.app` outside `/api/v1` whose path is not declared in
  `public_paths`.

Routes that extensions add on `context.app` are moved after every built-in route, so a
built-in route always answers first even when an extension route's pattern overlaps it.

These checks catch mistakes; they are not a sandbox. An extension is Python code running in the
service process. Middleware it adds with `context.app.add_middleware` runs outside
authentication, and objects it mutates in place are not tracked. Enable only extensions you
trust.

#### Public (unauthenticated) routes

Routes outside `/api/v1` are **not authenticated**: `SecurityScopingMiddleware` only
authenticates `/api/v1`. An extension that needs such a route, for example a webhook receiver
that checks its own signature, must say so with a `public_paths` attribute. Each entry is a
literal path prefix such as `/hooks/hello`; it may not overlap `/api`, `/healthz`, `/readyz` or
the documentation paths. The extension is responsible for any authentication on those routes.

```python
from fastapi import APIRouter
from fastapi.testclient import TestClient

from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.service.extensions import ExtensionContext


class HookExtension:
    name = "hook"
    public_paths = ("/hooks/hook",)  # unauthenticated: the route checks its own signature

    def configure(self, context: ExtensionContext) -> None:
        router = APIRouter()

        @router.post("/hooks/hook")
        async def receive() -> dict[str, bool]:
            return {"received": True}

        context.app.include_router(router)


app = create_api_app(db_path=":memory:", extensions=[HookExtension()])
# A declared public path takes no session token (its sender authenticates itself).
with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as client:
    assert client.post("/hooks/hook").json() == {"received": True}
```

### Order and failures

- The open-source service has no in-tree extension. Workspace members
  (`/api/v1/workspaces/{id}/members`, `/my-membership`), organisations (`/api/v1/organisations`),
  the hosted secrets API (`/api/v1/secrets`) and publishing (`/api/v1/published` and the public
  chat gateway) are not in the open-source edition: the closed edition adds them as ordinary
  extensions passed in `extensions=` (`nexura.membership`, `nexura.organisations`,
  `nexura.secrets`, `nexura.publishing`), and the open-source service answers 404 on those
  paths. The credential
  store (env-var references and the local encrypted file, see [local secrets](local-secrets.md))
  is installed by `create_api_app` itself, before any extension runs.
- Next come the objects passed in `extensions=`, in that order, after every built-in router.
- Last come the entry points named in `enabled_extensions=`, in that order.
- Names must be unique. A duplicate name, an empty name or an object that is not a
  `ServiceExtension` fails startup with `ExtensionError` before the database is created. An
  invalid capability flag or health field raises `ExtensionError` from `configure`.
- An exception raised by `configure` stops `create_api_app` and propagates with its own type.
  A note on the exception names the extension. Extensions after it are not configured.

### Entry points (`nexura.extensions`)

A package can publish an extension under the `nexura.extensions` entry-point group:

```toml
[project.entry-points."nexura.extensions"]
hello = "my_package.extension:HelloExtension"
```

The entry point may name a class, a zero-argument factory, or an extension instance.

Installing the package does **not** load the extension. `create_api_app` reads the group
only for the names you list:

```python notest
app = create_api_app(enabled_extensions=["hello"])
```

With no names, package metadata is never read. A name with no matching entry point, a name
used by two entry points, a name listed twice, an import error, or an object that is not a
`ServiceExtension` raises `ExtensionLoadError` before anything else is created. `nexura-runtime
serve` does not enable entry-point extensions.

## Auth providers

An `AuthProvider` has:

- `name`: a short identifier used in error messages.
- `is_authenticating`: true when the provider verifies credentials.
- `authenticate(request, target_workspace_id) -> Identity`: returns
  `Identity(principal, workspace_id, role)`. `target_workspace_id` comes from the
  `/api/v1/workspaces/{id}` path, else the `X-Workspace-ID` header, else `None`. To refuse a
  request, raise `nexura_runtime.service.api.middleware.AuthFailure(status, code, message)`;
  the client receives `{"detail": {"code": ..., "message": ...}}` with that status.
  The middleware checks the result before any route runs. `principal` must be a `Principal`
  with a non-empty `principal_id` (never `None`), `workspace_id` a non-empty string and `role`
  one of `admin`, `editor` or `viewer`. A provider may declare an optional
  `routes_without_membership` attribute: exact `(method, path)` pairs on which an authenticated
  principal with no membership is admitted. Only there may `workspace_id` and `role` both be
  `None` (the closed edition lists its organisation invite acceptance).
  Anything else is a provider bug: the request is refused with
  `500 AUTH_PROVIDER_INVALID_IDENTITY` and the reason is logged.
- `is_operator(request) -> bool`: whether the caller is a platform operator. Every operator
  decision of the service asks this method: full `/readyz` probe details and extension health
  fields, and creating workspaces (`POST /api/v1/workspaces`, which also needs the `admin`
  role). Only a return value of `True` counts. If the method raises, the caller is treated as
  not an operator, so the public `/readyz` stays minimal. The open-source
  `LocalOwnerAuthProvider` treats the local owner as the operator of a local service.

Pass one with `create_api_app(auth_provider=...)`. The rules are:

- With no provider, `create_api_app` installs `LocalOwnerAuthProvider`: every request is the
  local owner (`local-owner`, role `admin`) in the workspace named by the path or the
  `X-Workspace-ID` header (else `default`). No header can name another principal or role; the
  `X-Principal-ID` / `X-Principal-Role` trust was removed in EDITIONS_PLAN step 7. The
  open-source edition has no logins, tokens or API keys; the closed edition's authenticating
  provider (JWT, API keys, memberships) is passed here by its own app factory.
- `profile="authenticated-shared-service"` with nothing that can authenticate raises
  `AuthProviderRequiredError` (a subclass of `UnauthenticatedNetworkError`). It also raises
  when the provider you pass has `is_authenticating = False`.
- A provider with `is_authenticating = True` needs `profile="authenticated-shared-service"`;
  otherwise `create_api_app` raises `ValueError`.
- The bind guard is unchanged. A non-loopback `listen_host` needs an authenticating provider
  or the explicit insecure opt-in.
- If the shared profile is active and `app.state.auth_provider` is missing or does not
  authenticate, every `/api/v1` request is refused with `503 AUTH_PROVIDER_REQUIRED`.

```python
from fastapi.testclient import TestClient
from starlette.requests import Request

from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.service.api.middleware import AuthFailure
from nexura_runtime.service.auth.models import Principal
from nexura_runtime.service.extensions import Identity

TOKENS = {"token-alice": "alice"}  # a real provider verifies signed credentials


class StaticTokenProvider:
    name = "static-token"
    is_authenticating = True

    def authenticate(self, request: Request, target_workspace_id: str | None) -> Identity:
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        user = TOKENS.get(token)
        if user is None:
            raise AuthFailure(401, "UNAUTHENTICATED", "Authentication required.")
        principal = Principal(principal_id=user, profile="authenticated-shared-service")
        return Identity(principal, target_workspace_id or "default", "editor")

    def is_operator(self, request: Request) -> bool:
        return False


app = create_api_app(
    db_path=":memory:",
    profile="authenticated-shared-service",
    auth_provider=StaticTokenProvider(),
)
with TestClient(app) as client:
    assert client.get("/api/v1/workflows").status_code == 401
    ok = client.get("/api/v1/workflows", headers={"Authorization": "Bearer token-alice"})
    assert ok.status_code == 200
    assert set(client.get("/readyz").json()) == {"status"}  # not an operator
```

## Credential backends

`ScopedCredentialStore(secret_store=None, *, environment=None)` enforces workspace isolation,
revocation and redaction itself. The backend only stores values.

- With no backend, secret values are held in process memory.
- `environment` is an `EnvironmentCredentialSource`. It resolves references that name an
  `environment_variable`, from `os.environ` by default or from a mapping you pass.
- `encrypted_credential_backend(master_key_provider, db_path)` returns an
  `EncryptedSecretStore`, which encrypts every value at rest.

A backend implements `list_all_info()`, `put(...)`, `resolve(workspace_id, name)`,
`set_status(...)`, `mark_verified(...)` and `delete(...)`. The records it returns carry only
non-secret fields (`CredentialRecord`). See the protocol docstrings for the exact signatures.

## Membership stores

`MembershipStore` is the read side of workspace membership: `get_member`, `is_removed_member`
and `list_workspaces_for_principal`. `WorkspaceRepository` implements it for the open-source
edition's single principal: the local owner is the `admin` member of every workspace and nobody
else is a member. Managing members (`add_member`, `update_member_role`, `remove_member`,
`list_members`) and the membership audit trail moved to the closed edition in EDITIONS_PLAN
step 7, which extends this protocol and keeps the same database tables.

## Studio extensions

Studio has a typed registry in `studio-web/src/app/extensions.ts`. A `StudioExtension` has an
`id` and any of:

- `navItems`: sidebar entries (`id`, `labelKey`, `icon`, optional `after`, the id of the entry
  it follows).
- `routes`: hash routes (`hash`, for example `#/reports`) that open a page tab (`tab`) and
  render `component`.
- `panels`: workflow page tabs (`slot: 'workflow-detail'`, `id`, `labelKey`, `testId`,
  optional `after`, and a `component` that receives `workflowId` and `workflowName`).
- `messages`: translations for the extension's own keys, per language code (`{ en: {...},
  hi: {...} }`, nested like `src/i18n/en.json`). `t(key)` uses them only when the core
  dictionary has no such key, so an extension cannot change a core string; a key missing in
  the current language falls back to `en`.

`studio-web/src/app/bootstrap.tsx` exports `mountStudio(element, extensions)`. It creates a
registry, registers the built-in features (none at the moment) and then the extensions you
pass, and renders Studio with that registry. `studio-web/src/main.tsx` calls it with no extra
extensions. A duplicate id, nav item, route or panel throws when it is registered. The sidebar,
the hash router and the workflow page read the registry; they do not import any feature
directly. The Organisation page and the Publish tab are not part of the open-source Studio: the
closed edition's Studio entry registers them as extensions (moved in EDITIONS_PLAN steps 5
and 4).

Components rendered without `mountStudio` (for example `App` or `Shell` embedded directly)
read the default registry `studioExtensions`. It holds the built-in extensions (none since
the Organisation page moved to the closed edition), so such a Studio has the same pages as
`mountStudio()`. `createDefaultStudioExtensions()` returns a new registry with the same
contents.

## Extending

- **Custom handlers**: `ModuleRegistry.register(name, fn)` or `@registry.register_handler`.
- **Custom modules**: subclass `nexura_runtime.contracts.BaseModule`, give it a
  `ModuleManifest` and implement `invoke()`; register with
  `nexura_runtime.modules.registry.VersionedModuleRegistry`. Plugins can be
  discovered through Python entry points (`discover_entry_points`).
- **Providers**: subclass `nexura_runtime.providers.base.BaseProvider` and declare
  `ProviderCapabilities` honestly (`can_stream=False` for final-only models).

None of this requires Studio.
