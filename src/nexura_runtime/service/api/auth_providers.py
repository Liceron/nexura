# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""The open-source ``AuthProvider``s: one principal, the local owner.

* ``LocalOwnerAuthProvider``: every request is the local owner (``local-owner``) with the
  ``admin`` role, in the workspace named by the path or the ``X-Workspace-ID`` header (else
  ``default``). No header can name another principal or role: the ``X-Principal-ID`` /
  ``X-Principal-Role`` trust was removed in EDITIONS_PLAN step 7.
* ``OperatorTokenAuthProvider`` (``operator_token``, P38 C4): the same single owner, but every
  request must carry the operator token. It is the only authenticating provider the
  ``local-owner`` profile accepts (remote single-operator mode); it adds no accounts or roles.
* ``resolve_auth_provider``: what ``create_api_app`` installs. The ``authenticated-shared-service``
  profile needs an authenticating provider passed explicitly; the closed edition supplies one.
* ``caller_is_operator``: every operator decision asks the installed provider.
"""

from __future__ import annotations

import logging

from starlette.requests import Request

from nexura_runtime.service.auth.models import Principal
from nexura_runtime.service.auth.profile import ProfileManager
from nexura_runtime.service.extensions import AuthProvider, AuthProviderRequiredError, Identity

SHARED_PROFILE = "authenticated-shared-service"

logger = logging.getLogger("nexura.service.security")


def _local_owner() -> Principal:
    return Principal(
        principal_id="local-owner",
        email="owner@local.nexura",
        name="Local Developer",
        profile="local-owner",
    )


class LocalOwnerAuthProvider:
    """No authentication: the single local owner, role ``admin``."""

    name = "local-owner"
    is_authenticating = False

    def authenticate(self, request: Request, target_workspace_id: str | None) -> Identity:
        """Return the local owner in the target workspace (``default`` when none is named)."""
        return Identity(_local_owner(), target_workspace_id or "default", "admin")

    def is_operator(self, request: Request) -> bool:
        """The local owner is the operator of a local service."""
        return True


def caller_is_operator(request: Request) -> bool:
    """Ask the installed ``AuthProvider`` whether the caller is a platform operator.

    Every operator decision of the service goes through here (full ``/readyz`` details,
    creating workspaces). It fails closed: a provider that raises, or returns anything but
    ``True``, means "not an operator". With no provider installed, the local-owner profile
    keeps the local owner as operator and the shared profile has no operator.
    """
    state = request.app.state
    provider: AuthProvider | None = getattr(state, "auth_provider", None)
    if provider is None:
        pm: ProfileManager | None = getattr(state, "profile_manager", None)
        return pm is None or pm.profile != SHARED_PROFILE
    try:
        return provider.is_operator(request) is True
    except Exception as exc:
        logger.warning(
            "auth provider %r is_operator failed (%s); treating the caller as not an operator",
            getattr(provider, "name", type(provider).__name__),
            type(exc).__name__,
        )
        return False


def resolve_auth_provider(
    auth_provider: AuthProvider | None, profile_manager: ProfileManager
) -> AuthProvider:
    """Pick the provider ``create_api_app`` installs, failing closed.

    * An explicit provider must match the profile: the shared profile needs an authenticating
      provider, and an authenticating provider needs the shared profile, except the open-source
      ``OperatorTokenAuthProvider``, which authenticates the single local owner (remote
      single-operator mode) and therefore needs the ``local-owner`` profile.
    * Without one, the ``local-owner`` profile gets ``LocalOwnerAuthProvider`` and the shared
      profile raises ``AuthProviderRequiredError``: the open-source edition has nothing that
      authenticates requests (authentication is in the Nexura enterprise edition).
    """
    shared = profile_manager.profile == SHARED_PROFILE
    if auth_provider is not None:
        if not isinstance(auth_provider, AuthProvider):
            raise TypeError(
                f"auth_provider must implement AuthProvider, got {type(auth_provider).__name__}."
            )
        if shared and not auth_provider.is_authenticating:
            raise AuthProviderRequiredError(
                "Service profile 'authenticated-shared-service' refuses startup with the "
                f"non-authenticating auth provider '{auth_provider.name}'."
            )
        from nexura_runtime.service.api.operator_token import OperatorTokenAuthProvider

        if isinstance(auth_provider, OperatorTokenAuthProvider):
            if shared:
                raise ValueError(
                    "The operator token provider authenticates the single local owner; it "
                    "cannot serve the 'authenticated-shared-service' profile (multi-user "
                    "authentication is in the Nexura enterprise edition)."
                )
            return auth_provider
        if auth_provider.is_authenticating and not shared:
            raise ValueError(
                f"Auth provider '{auth_provider.name}' authenticates requests; use "
                "profile='authenticated-shared-service' with it."
            )
        return auth_provider
    if shared:
        raise AuthProviderRequiredError(
            "Service profile 'authenticated-shared-service' refuses startup without an "
            "authenticating auth_provider. The open-source edition has a single local owner "
            "(optionally behind one operator token, see docs/remote-single-operator.md) and no "
            "logins, user accounts or API keys; a shared multi-user service requires the Nexura "
            "enterprise edition."
        )
    return LocalOwnerAuthProvider()


__all__ = [
    "SHARED_PROFILE",
    "LocalOwnerAuthProvider",
    "caller_is_operator",
    "resolve_auth_provider",
]
