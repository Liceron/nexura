# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Multi-tenant resource scope enforcement and negative access boundary checks."""

from __future__ import annotations

from typing import Any

from nexura_runtime.service.auth.errors import (
    CrossWorkspaceAccessError,
    PermissionDeniedError,
)
from nexura_runtime.service.auth.models import (
    ActionPermission,
    Principal,
    WorkspaceRole,
)
from nexura_runtime.service.auth.permissions import check_permission
from nexura_runtime.service.workspaces.repository import WorkspaceRepository


class ResourceScopeEnforcer:
    """Enforces strict tenant isolation across every resource repository and route.

    Guarantees:
    - Principals in Workspace A cannot access, view, or mutate resources owned by Workspace B.
    - Role permissions (admin, editor, viewer) are strictly enforced per operation.
    - Negative access raises CrossWorkspaceAccessError or PermissionDeniedError.
    """

    def __init__(self, workspace_repo: WorkspaceRepository | None = None) -> None:
        self.workspace_repo = workspace_repo or WorkspaceRepository()

    def enforce_workspace_access(
        self,
        principal: Principal,
        target_workspace_id: str,
        *,
        required_permission: ActionPermission | None = None,
    ) -> WorkspaceRole:
        """Verify principal's membership and permission within the target workspace.

        Returns:
            The member's active WorkspaceRole.

        Raises:
            CrossWorkspaceAccessError: If principal has no membership in target workspace.
            PermissionDeniedError: If member lacks required permission.
        """
        # Local-owner profile bypass for single-tenant local runs
        if principal.profile == "local-owner":
            return "admin"

        member = self.workspace_repo.get_member(target_workspace_id, principal.principal_id)
        if not member:
            raise CrossWorkspaceAccessError(
                f"Principal '{principal.principal_id}' does not belong to "
                f"workspace '{target_workspace_id}'."
            )

        if required_permission:
            check_permission(principal, member.role, required_permission)

        return member.role

    def enforce_resource_ownership(
        self,
        *,
        caller_workspace_id: str,
        resource_workspace_id: str,
        resource_type: str,
        resource_id: str,
    ) -> None:
        """Verify that a referenced resource belongs strictly to the caller's workspace."""
        if caller_workspace_id != resource_workspace_id:
            raise CrossWorkspaceAccessError(
                f"Cross-workspace access denied: {resource_type} '{resource_id}' "
                f"belongs to workspace '{resource_workspace_id}', not '{caller_workspace_id}'."
            )

    def check_workflow_access(
        self,
        caller_workspace_id: str,
        workflow_workspace_id: str,
        workflow_id: str,
    ) -> None:
        """Assert workflow specification belongs to caller's workspace."""
        self.enforce_resource_ownership(
            caller_workspace_id=caller_workspace_id,
            resource_workspace_id=workflow_workspace_id,
            resource_type="workflow",
            resource_id=workflow_id,
        )

    def check_artifact_access(
        self,
        caller_workspace_id: str,
        artifact_workspace_id: str,
        artifact_id: str,
    ) -> None:
        """Assert artifact belongs to caller's workspace."""
        self.enforce_resource_ownership(
            caller_workspace_id=caller_workspace_id,
            resource_workspace_id=artifact_workspace_id,
            resource_type="artifact",
            resource_id=artifact_id,
        )

    def check_vector_collection_access(
        self,
        caller_workspace_id: str,
        collection_workspace_id: str,
        collection_id: str,
    ) -> None:
        """Assert vector collection belongs to caller's workspace."""
        self.enforce_resource_ownership(
            caller_workspace_id=caller_workspace_id,
            resource_workspace_id=collection_workspace_id,
            resource_type="vector_collection",
            resource_id=collection_id,
        )

    def check_document_access(
        self,
        caller_workspace_id: str,
        document_workspace_id: str,
        document_id: str,
    ) -> None:
        """Assert document belongs to caller's workspace."""
        self.enforce_resource_ownership(
            caller_workspace_id=caller_workspace_id,
            resource_workspace_id=document_workspace_id,
            resource_type="document",
            resource_id=document_id,
        )

    def check_credential_access(
        self,
        caller_workspace_id: str,
        credential_workspace_id: str,
        credential_id: str,
        *,
        principal: Principal,
        role: WorkspaceRole,
    ) -> None:
        """Assert credential belongs to workspace and principal has export permission."""
        self.enforce_resource_ownership(
            caller_workspace_id=caller_workspace_id,
            resource_workspace_id=credential_workspace_id,
            resource_type="credential",
            resource_id=credential_id,
        )
        if principal.profile != "local-owner" and role != "admin":
            raise PermissionDeniedError(
                f"Principal '{principal.principal_id}' with role '{role}' "
                "cannot export credentials. Admin role required."
            )

    def check_approval_access(
        self,
        caller_workspace_id: str,
        run_workspace_id: str,
        run_id: str,
        *,
        principal: Principal,
        role: WorkspaceRole,
    ) -> None:
        """Assert workflow run belongs to workspace and principal has approval permission."""
        self.enforce_resource_ownership(
            caller_workspace_id=caller_workspace_id,
            resource_workspace_id=run_workspace_id,
            resource_type="run",
            resource_id=run_id,
        )
        if principal.profile != "local-owner" and role not in ("admin",):
            raise PermissionDeniedError(
                f"Principal '{principal.principal_id}' with role '{role}' "
                "cannot approve workflow gates. Admin role required."
            )

    def check_run_access(
        self,
        caller_workspace_id: str,
        run_workspace_id: str,
        run_id: str,
    ) -> None:
        """Assert workflow execution run belongs to caller's workspace."""
        self.enforce_resource_ownership(
            caller_workspace_id=caller_workspace_id,
            resource_workspace_id=run_workspace_id,
            resource_type="run",
            resource_id=run_id,
        )

    def check_event_access(
        self,
        caller_workspace_id: str,
        event_workspace_id: str,
        event_id: str,
    ) -> None:
        """Assert event record belongs to caller's workspace."""
        self.enforce_resource_ownership(
            caller_workspace_id=caller_workspace_id,
            resource_workspace_id=event_workspace_id,
            resource_type="event",
            resource_id=event_id,
        )

    def check_index_access(
        self,
        caller_workspace_id: str,
        index_workspace_id: str,
        index_id: str,
    ) -> None:
        """Assert index / vector collection belongs to caller's workspace."""
        self.enforce_resource_ownership(
            caller_workspace_id=caller_workspace_id,
            resource_workspace_id=index_workspace_id,
            resource_type="index",
            resource_id=index_id,
        )

    def check_evaluation_dataset_access(
        self,
        caller_workspace_id: str,
        dataset_workspace_id: str,
        dataset_id: str,
    ) -> None:
        """Assert evaluation dataset belongs to caller's workspace."""
        self.enforce_resource_ownership(
            caller_workspace_id=caller_workspace_id,
            resource_workspace_id=dataset_workspace_id,
            resource_type="evaluation_dataset",
            resource_id=dataset_id,
        )

    def check_schedule_access(
        self,
        caller_workspace_id: str,
        schedule_workspace_id: str,
        schedule_id: str,
    ) -> None:
        """Assert schedule definition belongs to caller's workspace."""
        self.enforce_resource_ownership(
            caller_workspace_id=caller_workspace_id,
            resource_workspace_id=schedule_workspace_id,
            resource_type="schedule",
            resource_id=schedule_id,
        )

    def scan_and_validate_references(
        self,
        caller_workspace_id: str,
        data: Any,
    ) -> None:
        """Recursively scan data structures for cross-resource references
        and reject foreign ownership.

        The owners of every reference are read first in one batch when the repository offers
        ``get_resource_workspaces`` (MVP WP11a: one query instead of one or two per
        reference); the references are then checked in document order, so the first
        violation found is the same as before.
        """
        keys: list[tuple[str, str]] = []
        _collect_reference_keys(data, keys)
        batch = getattr(self.workspace_repo, "get_resource_workspaces", None)
        known: dict[tuple[str, str], str | None] = batch(keys) if keys and callable(batch) else {}
        self._scan_references(caller_workspace_id, data, known)

    def _owner(
        self, known: dict[tuple[str, str], str | None], resource_type: str, ref_id: str
    ) -> str | None:
        key = (resource_type, ref_id)
        if key in known:
            return known[key]
        return self.workspace_repo.get_resource_workspace(resource_type, ref_id)

    def _scan_references(
        self,
        caller_workspace_id: str,
        data: Any,
        known: dict[tuple[str, str], str | None],
    ) -> None:
        if isinstance(data, dict):
            for key, val in data.items():
                k_lower = key.lower()
                # 1. Credential references
                if k_lower in ("credential_id", "credential_ref", "credential"):
                    ref_id = val.get("ref") if isinstance(val, dict) else val
                    if isinstance(ref_id, str):
                        owner = self._owner(known, "credential", ref_id)
                        if owner and owner != caller_workspace_id:
                            raise CrossWorkspaceAccessError(
                                f"Cross-workspace access denied: credential '{ref_id}' "
                                f"belongs to workspace '{owner}', not '{caller_workspace_id}'."
                            )

                # 2. Artifact references
                elif k_lower in ("artifact_id", "artifact_ref", "artifact"):
                    ref_id = val.get("id") if isinstance(val, dict) else val
                    if isinstance(ref_id, str):
                        if ref_id.startswith("artifact://"):
                            parts = ref_id[len("artifact://") :].split("/")
                            if parts and parts[0] != caller_workspace_id:
                                raise CrossWorkspaceAccessError(
                                    f"Cross-workspace access denied: artifact URI '{ref_id}' "
                                    f"references workspace '{parts[0]}', "
                                    f"not '{caller_workspace_id}'."
                                )
                        else:
                            owner = self._owner(known, "artifact", ref_id)
                            if owner and owner != caller_workspace_id:
                                raise CrossWorkspaceAccessError(
                                    f"Cross-workspace access denied: artifact '{ref_id}' "
                                    f"belongs to workspace '{owner}', not '{caller_workspace_id}'."
                                )

                # 3. Index / vector collection references
                elif k_lower in ("index_id", "collection_id", "collection_name", "index"):
                    ref_id = val.get("id") if isinstance(val, dict) else val
                    if isinstance(ref_id, str):
                        owner = self._owner(known, "index", ref_id) or self._owner(
                            known, "vector_collection", ref_id
                        )
                        if owner and owner != caller_workspace_id:
                            raise CrossWorkspaceAccessError(
                                f"Cross-workspace access denied: index '{ref_id}' "
                                f"belongs to workspace '{owner}', not '{caller_workspace_id}'."
                            )

                # 4. Evaluation dataset references
                elif k_lower in ("dataset_id", "evaluation_dataset_id", "dataset"):
                    ref_id = val.get("id") if isinstance(val, dict) else val
                    if isinstance(ref_id, str):
                        owner = self._owner(known, "dataset", ref_id) or self._owner(
                            known, "evaluation_dataset", ref_id
                        )
                        if owner and owner != caller_workspace_id:
                            raise CrossWorkspaceAccessError(
                                f"Cross-workspace access denied: evaluation dataset '{ref_id}' "
                                f"belongs to workspace '{owner}', not '{caller_workspace_id}'."
                            )

                # 5. Schedule references
                elif k_lower in ("schedule_id", "schedule"):
                    ref_id = val.get("id") if isinstance(val, dict) else val
                    if isinstance(ref_id, str):
                        owner = self._owner(known, "schedule", ref_id)
                        if owner and owner != caller_workspace_id:
                            raise CrossWorkspaceAccessError(
                                f"Cross-workspace access denied: schedule '{ref_id}' "
                                f"belongs to workspace '{owner}', not '{caller_workspace_id}'."
                            )

                # 6. Module / integration tool references
                elif k_lower in ("module_id", "module", "handler"):
                    ref_id = val.get("id") if isinstance(val, dict) else val
                    if isinstance(ref_id, str):
                        owner = self._owner(known, "integration_tool", ref_id) or self._owner(
                            known, "module", ref_id
                        )
                        if owner and owner != caller_workspace_id:
                            raise CrossWorkspaceAccessError(
                                f"Cross-workspace access denied: module '{ref_id}' "
                                f"belongs to workspace '{owner}', not '{caller_workspace_id}'."
                            )

                # Recurse
                self._scan_references(caller_workspace_id, val, known)
        elif isinstance(data, list):
            for item in data:
                self._scan_references(caller_workspace_id, item, known)
        elif isinstance(data, str) and data.startswith("artifact://"):
            parts = data[len("artifact://") :].split("/")
            if parts and parts[0] != caller_workspace_id:
                raise CrossWorkspaceAccessError(
                    f"Cross-workspace access denied: artifact URI '{data}' "
                    f"references workspace '{parts[0]}', not '{caller_workspace_id}'."
                )


def _collect_reference_keys(data: Any, keys: list[tuple[str, str]]) -> None:
    """The ``(resource_type, resource_id)`` lookups ``scan_and_validate_references`` may make,
    in document order (both alternatives of the ``or`` pairs). Keep in step with it: a key
    missing here is still looked up, one query at a time."""
    if isinstance(data, dict):
        for key, val in data.items():
            k_lower = key.lower()
            types: tuple[str, ...] = ()
            if k_lower in ("credential_id", "credential_ref", "credential"):
                ref_id = val.get("ref") if isinstance(val, dict) else val
                types = ("credential",)
            else:
                ref_id = val.get("id") if isinstance(val, dict) else val
                if k_lower in ("artifact_id", "artifact_ref", "artifact"):
                    is_uri = isinstance(ref_id, str) and ref_id.startswith("artifact://")
                    types = () if is_uri else ("artifact",)
                elif k_lower in ("index_id", "collection_id", "collection_name", "index"):
                    types = ("index", "vector_collection")
                elif k_lower in ("dataset_id", "evaluation_dataset_id", "dataset"):
                    types = ("dataset", "evaluation_dataset")
                elif k_lower in ("schedule_id", "schedule"):
                    types = ("schedule",)
                elif k_lower in ("module_id", "module", "handler"):
                    types = ("integration_tool", "module")
            if isinstance(ref_id, str):
                keys.extend((resource_type, ref_id) for resource_type in types)
            _collect_reference_keys(val, keys)
    elif isinstance(data, list):
        for item in data:
            _collect_reference_keys(item, keys)
