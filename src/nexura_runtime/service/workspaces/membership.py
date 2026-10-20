# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""``MembershipStore``: the read side of workspace membership, as a protocol.

Workspace ownership of resources stays with ``WorkspaceRepository``. This protocol is what the
open-source service asks about membership: whether a principal belongs to a workspace and with
which role, whether its membership was removed, and which workspaces it belongs to. In the
open-source edition the answers are fixed by ``WorkspaceRepository`` (the local owner is the
``admin`` member of every workspace; nobody else is a member).

Managing members (adding, changing roles, removing) and the membership audit trail moved to the
closed edition in EDITIONS_PLAN step 7 (a protocol that extends this one, and a
``WorkspaceRepository`` subclass that implements it).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from nexura_runtime.service.workspaces.models import Workspace, WorkspaceMember


@runtime_checkable
class MembershipStore(Protocol):
    """Who belongs to a workspace, with which role (read only)."""

    def get_member(self, workspace_id: str, principal_id: str) -> WorkspaceMember | None:
        """The member record of ``principal_id`` in ``workspace_id``, or ``None``."""
        ...

    def is_removed_member(self, workspace_id: str, principal_id: str) -> bool:
        """True when the principal's latest membership event in the workspace is a removal."""
        ...

    def list_workspaces_for_principal(self, principal_id: str) -> list[Workspace]:
        """Workspaces in which ``principal_id`` currently has a membership record."""
        ...


__all__ = ["MembershipStore"]
