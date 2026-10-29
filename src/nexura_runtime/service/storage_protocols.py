# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Service store contracts. Existing trigger/channel/evaluation protocols are reused.

``BlobStore`` (large write-once payloads outside the database, MVP WP2b) is defined in
``nexura_runtime.blobs`` and re-exported here with the other contracts.

These contracts expose the public operations used by routes, without SQLite
connections, locks, paths or cache dictionaries. Ports may use any backend.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from nexura_agents.session import SessionStore
from nexura_agents.skills import SkillLibrary

from nexura_runtime.agents_spec import AgentSpec
from nexura_runtime.blobs.base import BlobStore
from nexura_runtime.service.workspaces.models import Workspace, WorkspaceMember

__all__ = [
    "AgentBackend",
    "BlobStore",
    "SessionBackend",
    "SkillsBackend",
    "StudioEvaluationBackend",
    "WorkspaceBackend",
]


class SessionBackend(SessionStore, Protocol):
    async def updated_at(self, session_ids: list[str]) -> dict[str, float]: ...

    async def delete(self, session_id: str) -> bool: ...


class AgentBackend(Protocol):
    def list_agents(self, workspace_id: str) -> list[dict[str, Any]]: ...

    def get(self, workspace_id: str, agent_id: str) -> dict[str, Any]: ...

    def get_spec(self, workspace_id: str, agent_id: str) -> tuple[AgentSpec, int]: ...

    def create(
        self, workspace_id: str, spec: AgentSpec, *, actor: str = "", agent_id: str | None = None
    ) -> dict[str, Any]: ...

    def update(
        self,
        workspace_id: str,
        agent_id: str,
        spec: AgentSpec,
        *,
        expected_revision: int | None,
        actor: str = "",
    ) -> dict[str, Any]: ...

    def delete(
        self, workspace_id: str, agent_id: str, *, expected_revision: int | None = None
    ) -> None: ...


class SkillsBackend(Protocol):
    def list_skills(self, workspace_id: str, label: str | None = None) -> list[dict[str, Any]]: ...

    def get(
        self, workspace_id: str, name: str, *, version: int | None = None, label: str | None = None
    ) -> dict[str, Any]: ...

    def versions(self, workspace_id: str, name: str) -> dict[str, Any]: ...

    def create(
        self,
        workspace_id: str,
        markdown: str,
        *,
        resources: Mapping[str, str] | None = None,
        labels: Sequence[str] = (),
        actor: str = "",
    ) -> dict[str, Any]: ...

    def set_label(
        self, workspace_id: str, name: str, label: str, version: int, *, actor: str = ""
    ) -> dict[str, Any]: ...

    def remove(
        self, workspace_id: str, name: str, version: int | None = None, *, actor: str = ""
    ) -> int: ...

    def library_for(self, workspace_id: str, names: Sequence[str], label: str) -> SkillLibrary: ...


class StudioEvaluationBackend(Protocol):
    def get_dataset(self, workspace_id: str, dataset_id: str) -> dict[str, Any] | None: ...

    def list_datasets(
        self, workspace_id: str, workflow_id: str | None = None
    ) -> list[dict[str, Any]]: ...

    def count_datasets(self, workspace_id: str) -> int: ...

    def save_dataset(
        self,
        workspace_id: str,
        dataset_id: str,
        *,
        name: str,
        description: str,
        cases: list[dict[str, Any]],
        content_hash: str,
        expected_revision: int | None,
        workflow_id: str | None = None,
    ) -> dict[str, Any]: ...

    def delete_dataset(self, workspace_id: str, dataset_id: str) -> bool: ...

    def save_run(
        self,
        workspace_id: str,
        *,
        run_id: str,
        dataset_id: str,
        dataset_revision: int,
        dataset_hash: str,
        label: str,
        target: dict[str, Any],
        scorers: list[dict[str, Any]],
        summary: dict[str, Any],
        report: dict[str, Any],
    ) -> dict[str, Any]: ...

    def get_run(self, workspace_id: str, run_id: str) -> dict[str, Any] | None: ...

    def list_runs(
        self, workspace_id: str, dataset_id: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]: ...

    def delete_run(self, workspace_id: str, run_id: str) -> bool: ...


class WorkspaceBackend(Protocol):
    fail_closed_unowned: bool

    def record_resource_ownership(
        self, resource_type: str, resource_id: str, workspace_id: str, owner_id: str
    ) -> None: ...

    def get_resource_ownership(
        self, resource_type: str, resource_id: str
    ) -> tuple[str, str] | None: ...

    def get_resource_workspace(self, resource_type: str, resource_id: str) -> str | None: ...

    def list_workspace_resource_ids(self, resource_type: str, workspace_id: str) -> list[str]: ...

    def list_owned_resource_ids(
        self, resource_type: str, workspace_id: str, owner_id: str
    ) -> list[str]: ...

    def delete_resource_ownership(self, resource_type: str, resource_id: str) -> bool: ...

    def create_workspace(
        self, workspace_id: str, name: str, owner_id: str, *, metadata: dict[str, Any] | None = None
    ) -> Workspace: ...

    def update_workspace_metadata(
        self, workspace_id: str, updates: dict[str, Any]
    ) -> Workspace: ...

    def get_workspace(self, workspace_id: str) -> Workspace | None: ...

    def is_removed_member(self, workspace_id: str, principal_id: str) -> bool: ...

    def list_workspaces(self) -> list[Workspace]: ...

    def list_workspaces_for_principal(self, principal_id: str) -> list[Workspace]: ...

    def get_member(self, workspace_id: str, principal_id: str) -> WorkspaceMember | None: ...
