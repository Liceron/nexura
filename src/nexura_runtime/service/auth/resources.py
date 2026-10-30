# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""The per-route resource check for restricted principals (resource-scoped API keys).

A principal whose ``resources`` is a list (for example ``["workflow:wf-1",
"knowledge:default"]``) may only touch the resources it lists. The routes that accept such a
principal (starting and reading runs, knowledge retrieval) call :func:`require_resource`.
The local owner has ``resources=None`` and is never restricted.

Resource-scoped API keys themselves (creating them, the list of routes such a key may call
and its enforcement) moved to the closed edition in EDITIONS_PLAN
step 7.
"""

from __future__ import annotations

from nexura_runtime.service.auth.errors import PermissionDeniedError
from nexura_runtime.service.auth.models import Principal

RESOURCE_KINDS = ("workflow", "knowledge")


def require_resource(principal: Principal | None, kind: str, resource_id: str | None) -> None:
    """Raise ``PermissionDeniedError`` unless an unrestricted principal or ``kind:id`` is listed."""
    if principal is None or principal.resources is None:
        return
    wanted = f"{kind}:{resource_id}" if resource_id else None
    if wanted is None or wanted not in principal.resources:
        raise PermissionDeniedError(
            f"This API key is limited to {sorted(principal.resources)}; "
            f"{wanted or 'an unnamed ' + kind} is not among them.",
            code="RESOURCE_SCOPE_DENIED",
        )


__all__ = ["RESOURCE_KINDS", "require_resource"]
