# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Refreshable credentials stored as secret JSON, never as flow configuration."""

from __future__ import annotations

import asyncio
import json
import math
import threading
import time
import weakref
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, ClassVar
from urllib.parse import urlencode, urlsplit

from nexura_agents.contracts.context import CredentialReference

from nexura_connectors._core import SECRET_FIELDS, Call, ConnectorHttp, HttpResult, secret_variants
from nexura_connectors._rest import RestConnector
from nexura_connectors.errors import (
    AuthFailedError,
    ConnectorError,
    ConnectorTimeoutError,
    ConnectorTransportError,
    IndeterminateEffectError,
    InvalidConfigError,
    RateLimitedError,
)

# Shared by connector instances, including Drive and Sheets. Entries live only while a
# refresh is in flight. Thread locks serialise exchanges across event loops; tasks
# deduplicate waiters within a loop without allowing cancellation to kill the exchange.
_FLIGHTS: weakref.WeakKeyDictionary[Any, dict[tuple[Any, str, str, str], asyncio.Task[str]]] = (
    weakref.WeakKeyDictionary()
)
_LOCKS: weakref.WeakKeyDictionary[Any, dict[tuple[str, str, str], Any]] = (
    weakref.WeakKeyDictionary()
)
_REGISTRY_LOCK = threading.Lock()


def _bundle(secret: str) -> dict[str, Any] | None:
    if not secret.lstrip().startswith("{"):
        return None
    try:
        data = json.loads(secret)
    except ValueError:
        raise InvalidConfigError(
            "credential JSON is invalid; store a token or an auth record"
        ) from None
    if not isinstance(data, dict):
        raise InvalidConfigError("credential auth record must be a JSON object")
    return data


def _remember(http: ConnectorHttp, data: dict[str, Any]) -> None:
    for key in SECRET_FIELDS:
        value = data.get(key)
        if isinstance(value, str) and value:
            http.secrets.extend(secret_variants(value, split=False))


def _expiry(value: Any) -> float:
    try:
        expiry = float(value)
        if math.isfinite(expiry) and expiry >= 0:
            return expiry
    except (ValueError, TypeError):
        pass
    raise InvalidConfigError("auth record expires_at must be a finite Unix timestamp")


class RefreshingRestConnector(RestConnector):
    """PAT/bot tokens or persisted OAuth/App records with single-flight refresh."""

    AUTH_FAMILY: ClassVar[str]
    TOKEN_URL: ClassVar[str]

    def build_http(self, *args: Any, **kwargs: Any) -> ConnectorHttp:
        """Enable vendor rate-limit interpretation for these four connectors only."""
        http = super().build_http(*args, **kwargs)
        http.rate_profile = self.AUTH_FAMILY
        http.indeterminate_status = True
        return http

    def _persister(self, call: Call, expected_secret: str) -> Callable[[str], None]:
        resolver = call.context.credential_resolver
        ref = CredentialReference(
            credential_id=call.cfg["credential_ref"], credential_type=self.CREDENTIAL_TYPE
        )
        # A custom resolver may implement this synchronous, atomic replacement hook.
        updater = getattr(resolver, "update_secret", None)
        if callable(updater):
            return lambda secret: updater(ref, secret)
        store = getattr(resolver, "store", None)
        workspace = getattr(resolver, "workspace_id", None)
        if store is None or workspace is None or not callable(getattr(store, "store", None)):
            raise InvalidConfigError(
                "refresh needs a writable WorkspaceCredentialResolver or a resolver with "
                "update_secret(ref, secret); a read-only resolver can use static tokens"
            )
        meta = store.get_metadata(workspace, ref.credential_id)
        if meta.environment_variable:
            raise InvalidConfigError(
                "refreshable credentials must be stored directly, without an environment fallback"
            )

        def persist(secret: str) -> None:
            # Do not resurrect a credential revoked/deleted or replace an operator's
            # newer credential while this refresh was awaiting HTTP. Store writes are
            # synchronous; distributed writers still need an external refresh owner.
            if store.resolve_ref(workspace, ref).reveal() != expected_secret:
                raise AuthFailedError("credential changed during refresh; re-resolve it")
            latest = store.get_metadata(workspace, ref.credential_id)
            store.store(
                workspace,
                ref.credential_id,
                self.CREDENTIAL_TYPE,
                secret,
                description=latest.description,
                provider=latest.provider,
                status=latest.status,
            )

        return persist

    async def _refresh(self, call: Call, rejected_token: str | None = None) -> str:
        resolver = call.context.credential_resolver
        owner = getattr(resolver, "store", None) or resolver
        key = (
            getattr(resolver, "workspace_id", call.context.workspace_id),
            call.cfg["credential_ref"],
            self.AUTH_FAMILY,
        )
        with _REGISTRY_LOCK:
            lock = _LOCKS.setdefault(owner, {}).setdefault(key, threading.Lock())
        # A threading lock serialises exchanges across event loops too. Nonblocking
        # acquisition avoids blocking the loop or leaking a to_thread acquire on cancellation.
        while not lock.acquire(blocking=False):
            if call.http.remaining_budget() <= 0:
                raise ConnectorTransportError("refresh owner busy; retry this read later")
            await asyncio.sleep(0.01)
        try:
            return await self._exchange(call, rejected_token)
        finally:
            lock.release()

    async def _exchange(self, call: Call, rejected_token: str | None) -> str:
        # Re-resolve after joining the flight: another completed invocation may already
        # have rotated the token. Never reuse a cached token across revocation/resolution.
        raw = self.resolve_secret(call.context, call.cfg)
        data = _bundle(raw)
        if data is None:
            return raw
        _remember(call.http, data)
        if (
            data.get("access_token")
            and data.get("access_token") != rejected_token
            and _expiry(data.get("expires_at", 0)) > time.time() + 30
        ):
            return raw
        persist = self._persister(call, raw)  # Fail before consuming a rotating refresh token.
        auth_type = data.get("auth_type", "oauth2")
        headers = {"Accept": "application/json"}
        if auth_type == "github_app" and self.AUTH_FAMILY == "github":
            from nexura_connectors._core import safe_id

            installation = safe_id(str(data.get("installation_id", "")), "installation id")
            provider = getattr(self, "app_jwt_provider", None)
            jwt = await asyncio.to_thread(provider) if callable(provider) else data.get("app_jwt")
            if not isinstance(jwt, str) or not jwt:
                raise InvalidConfigError("GitHub App auth needs app_jwt or app_jwt_provider")
            call.http.secrets.extend(secret_variants(jwt, split=False))
            endpoint = (
                call.base_url(self.DEFAULT_BASE_URL)
                + f"/app/installations/{installation}/access_tokens"
            )
            headers.update(self.EXTRA_HEADERS)
            headers["Authorization"] = f"Bearer {jwt}"
            body = b"{}"
            headers["Content-Type"] = "application/json"
        elif auth_type == "oauth2":
            if "refresh_expires_at" in data and _expiry(data["refresh_expires_at"]) <= time.time():
                raise AuthFailedError("refresh token expired; re-authorize this credential")
            required = ("refresh_token", "client_id", "client_secret")
            if any(not isinstance(data.get(k), str) or not data[k] for k in required):
                raise InvalidConfigError(
                    "OAuth auth record needs refresh_token, client_id and client_secret"
                )
            endpoint = data.get("token_url", self.TOKEN_URL)
            if not isinstance(endpoint, str):
                raise InvalidConfigError("auth record token_url must be an HTTPS URL")
            body = urlencode(
                {
                    "grant_type": "refresh_token",
                    **{k: data[k] for k in required},
                }
            ).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            raise InvalidConfigError("auth_type must be oauth2 (or github_app for GitHub)")

        if call.http.remaining_budget() <= 0:
            raise ConnectorTransportError(
                "operation timeout exhausted before token exchange; no API operation was sent"
            )
        token_http = replace(
            call.http,
            base_origin=endpoint,
            timeout=min(call.http.timeout, call.http.remaining_budget()),
            default_host=urlsplit(self.TOKEN_URL).hostname,
            max_retries=0,
            rate_profile=None,
            idempotency_key=None,
            refresh_auth=None,
        )
        # Nonstandard token hosts need the same operator allow-list as API overrides.
        if auth_type != "github_app":
            token_http.check_base_url(endpoint)
        parts = urlsplit(endpoint)
        if parts.username or parts.password or parts.query or parts.fragment:
            raise InvalidConfigError("token endpoint must not contain userinfo, query or fragment")
        try:
            response = await token_http.request(
                "POST",
                endpoint,
                headers=headers,
                content=body,
                write=True,
            )
            payload = response.json()
            if not isinstance(payload, dict) or payload.get("ok") is False or payload.get("error"):
                raise AuthFailedError("token exchange rejected")
            token = payload.get("token" if auth_type == "github_app" else "access_token")
            if not isinstance(token, str) or not token:
                raise AuthFailedError("token exchange returned no access token")
            _remember(call.http, payload)
            if auth_type == "github_app":
                expiry = datetime.fromisoformat(payload["expires_at"].replace("Z", "+00:00"))
                expires = (expiry if expiry.tzinfo else expiry.replace(tzinfo=UTC)).timestamp()
            else:
                expires = time.time() + _expiry(payload.get("expires_in"))
            if not math.isfinite(expires) or expires <= time.time() + 30:
                raise AuthFailedError("token exchange returned an unusable expiry")
            updated = {**data, "access_token": token, "expires_at": expires}
            if auth_type == "github_app":
                updated.pop("app_jwt", None)
            if "refresh_token" in payload:
                if not isinstance(payload["refresh_token"], str) or not payload["refresh_token"]:
                    raise AuthFailedError("token exchange returned an invalid refresh token")
                updated["refresh_token"] = payload["refresh_token"]
            if "refresh_token_expires_in" in payload:
                updated["refresh_expires_at"] = time.time() + _expiry(
                    payload["refresh_token_expires_in"]
                )
            result = json.dumps(updated)
            try:
                persist(result)
            except Exception:
                raise AuthFailedError(
                    "refreshed token could not be persisted; repair the credential store and "
                    "re-authorize if the provider rotated the refresh token"
                ) from None
            return result
        except RateLimitedError as exc:
            error = RateLimitedError(
                "token endpoint rate limited; wait for its Retry-After/reset before refreshing; "
                "no API operation was sent and the exchange was not retried"
            )
            error.retry_after_seconds = exc.retry_after_seconds
            raise error from None
        except (ConnectorTimeoutError, ConnectorTransportError, IndeterminateEffectError):
            raise ConnectorTransportError(
                "token endpoint temporarily unavailable; no API operation was sent; retry later. "
                "The exchange may have rotated the refresh token; "
                "re-authorize if it is no longer valid"
            ) from None
        except (ConnectorError, ValueError, KeyError, TypeError):
            raise AuthFailedError(
                "token refresh/installation exchange failed; check client credentials, refresh "
                "token or App JWT expiry, installation permissions and credential storage; "
                "re-authorize if revoked (no API operation was sent)"
            ) from None
        finally:
            call.http.attempts_made = token_http.attempts_made

    async def _prepare_auth(self, call: Call, rejected_token: str | None = None) -> None:
        """Re-check expiry and revocation before each page or operation."""
        call.secret = self.resolve_secret(call.context, call.cfg)
        call.http.secrets.extend(secret_variants(call.secret))
        data = _bundle(call.secret)
        if data is not None:
            _remember(call.http, data)
            if not (
                data.get("access_token")
                and data.get("access_token") != rejected_token
                and _expiry(data.get("expires_at", 0)) > time.time() + 30
            ):
                if call.http.remaining_budget() <= 0:
                    raise ConnectorTransportError(
                        "operation timeout exhausted before refresh; retry this read later"
                    )
                resolver = call.context.credential_resolver
                owner = getattr(resolver, "store", None) or resolver
                try:
                    with _REGISTRY_LOCK:
                        flights = _FLIGHTS.setdefault(owner, {})
                except TypeError:
                    raise InvalidConfigError(
                        "refresh resolver must support weak references and hashing"
                    ) from None
                key = (
                    asyncio.get_running_loop(),
                    getattr(resolver, "workspace_id", call.context.workspace_id),
                    call.cfg["credential_ref"],
                    self.AUTH_FAMILY,
                )
                task = flights.get(key)
                if task is None:
                    task = asyncio.create_task(self._refresh(call, rejected_token))
                    flights[key] = task

                    def finished(done: asyncio.Task[str]) -> None:
                        flights.pop(key, None)
                        if not done.cancelled():
                            done.exception()  # retrieve failures even if every waiter cancelled

                    task.add_done_callback(finished)
                try:
                    raw = await asyncio.wait_for(asyncio.shield(task), call.http.remaining_budget())
                except TimeoutError:
                    raise ConnectorTransportError(
                        "refresh wait exceeded the operation timeout; retry later; "
                        "the shared exchange may still complete and persist its token"
                    ) from None
                data = _bundle(raw)
                if data is None:
                    call.secret = raw
            if data is not None:
                _remember(call.http, data)
                token = data.get("access_token")
                if not isinstance(token, str) or not token:
                    raise InvalidConfigError("auth record access_token must be a nonempty string")
                call.secret = token

    async def api(self, call: Call, method: str, path: str, **kwargs: Any) -> HttpResult:
        """Resolve an unexpired access token before dispatch; never replay rejected writes."""
        await self._prepare_auth(call)

        async def refresh_headers() -> dict[str, str]:
            await self._prepare_auth(call)
            return self.auth_headers(call.secret)

        call.http.refresh_auth = refresh_headers
        try:
            try:
                return await super().api(call, method, path, **kwargs)
            except AuthFailedError as exc:
                if (
                    exc.status != 401
                    or kwargs.get("write")
                    or method.upper() not in {"GET", "HEAD"}
                    or call.auth_retried
                    or _bundle(self.resolve_secret(call.context, call.cfg)) is None
                ):
                    raise
                call.auth_retried = True
                await self._prepare_auth(call, rejected_token=call.secret)
                return await super().api(call, method, path, **kwargs)
        finally:
            call.http.refresh_auth = None
