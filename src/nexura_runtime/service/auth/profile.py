# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""The service profile switch: ``local-owner`` (open source) or ``authenticated-shared-service``.

The open-source edition has one principal, the local owner, with the ``admin`` role, and no
logins, tokens or API keys. ``ProfileManager`` only records which profile the service runs; the
rest of the service reads it (Host/Origin mode, API docs default, fail-closed unowned resources,
the sandbox and master-key startup checks).

The shared profile's principal resolution, token validation, API keys and the operator
allow-list moved to the closed edition in EDITIONS_PLAN step 7
(a ``ProfileManager`` subclass there). Without it the shared profile
needs an authenticating ``AuthProvider``; ``create_api_app`` refuses to start otherwise.
"""

from __future__ import annotations

from nexura_runtime.service.auth.models import Principal, ProfileType


class ProfileManager:
    """Holds the active service profile.

    Guarantees:
    - Default profile is 'local-owner' (offline, zero-friction developer mode).
    - Only the local owner is a platform operator; nothing here authenticates anyone.
    """

    def __init__(self, profile: ProfileType = "local-owner") -> None:
        self.profile: ProfileType = profile

    def is_operator(self, principal: Principal | None) -> bool:
        """True while the profile is ``local-owner``: the local owner operates a local service."""
        return self.profile == "local-owner"

    def set_profile(self, profile: ProfileType) -> None:
        """Switch the active profile."""
        self.profile = profile

    def is_configured(self) -> bool:
        """True for the ``local-owner`` profile; nothing here can authenticate a shared one."""
        return self.profile == "local-owner"


__all__ = ["ProfileManager"]
