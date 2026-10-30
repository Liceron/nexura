# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Module execution result and artifact reference models."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from nexura_agents.types import Usage

ResultStatus = Literal["success", "failure", "indeterminate"]


class ArtifactReference(BaseModel):
    """Reference to an artifact stored outside checkpoints."""

    artifact_id: str
    uri: str | None = None
    mime_type: str = "application/octet-stream"
    size_bytes: int = 0
    digest: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class ModuleResult(BaseModel):
    """Outcome of a module invocation."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    status: ResultStatus = "success"
    outputs: dict[str, Any] = Field(default_factory=dict)
    artifact_refs: list[ArtifactReference] = Field(default_factory=list)
    branch: str | None = None
    usage: Usage | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None

    @classmethod
    def success(
        cls,
        outputs: dict[str, Any],
        *,
        branch: str | None = None,
        artifact_refs: list[ArtifactReference] | None = None,
        usage: Usage | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ModuleResult:
        """Create a successful result."""
        return cls(
            status="success",
            outputs=outputs,
            branch=branch,
            artifact_refs=artifact_refs or [],
            usage=usage,
            metadata=metadata or {},
        )

    @classmethod
    def failure(
        cls,
        error_code: str,
        error_message: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> ModuleResult:
        """Create a failed result."""
        return cls(
            status="failure",
            error_code=error_code,
            error_message=error_message,
            metadata=metadata or {},
        )

    @classmethod
    def indeterminate(
        cls,
        error_message: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> ModuleResult:
        """Create an indeterminate result where the external side effect state is uncertain."""
        return cls(
            status="indeterminate",
            error_code="INDETERMINATE_EFFECT",
            error_message=error_message,
            metadata=metadata or {},
        )
