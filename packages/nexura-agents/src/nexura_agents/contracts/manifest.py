# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Module manifest schemas, capability flags, and validation logic."""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from nexura_agents.contracts.canonical import canonical_digest
from nexura_agents.contracts.errors import ConfigValidationError, SchemaValidationError
from nexura_agents.contracts.schema_validator import validate_schema

EffectPolicy = Literal["pure", "read", "idempotent_write", "non_idempotent_write"]
VerificationStatus = Literal["unverified", "mock_verified", "live_verified"]

_MODULE_ID_REGEX = re.compile(r"^[a-z0-9_-]+(\.[a-z0-9_-]+)+$")
_SEMVER_REGEX = re.compile(r"^\d+\.\d+\.\d+(-[a-zA-Z0-9.]+)?$")


class PortDefinition(BaseModel):
    """Named port metadata for module input/output wiring."""

    name: str
    type: str = "any"
    description: str = ""
    required: bool = True


class DisplayMetadata(BaseModel):
    """Optional UI/display hints for module presentation.

    Contains only display metadata; executable code or layout bindings are strictly forbidden.
    """

    label: str | None = None
    icon: str | None = None
    group: str | None = None
    examples: list[dict[str, Any]] = Field(default_factory=list)
    secret_hints: list[str] = Field(default_factory=list)


class ModuleManifest(BaseModel):
    """Declarative manifest defining a module's capabilities, schemas, and policies."""

    schema_version: int = 1
    module_id: str
    version: str
    kind: str
    title: str
    description: str
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    config_schema: dict[str, Any] = Field(default_factory=dict)
    named_ports: list[PortDefinition] = Field(default_factory=list)
    capabilities: dict[str, bool] = Field(default_factory=dict)
    dependency_extra: str | None = None
    min_sdk_version: str = "0.3.0"
    timeout_seconds: float | None = None
    output_limit_bytes: int | None = None
    effect_policy: EffectPolicy = "pure"
    #: Operations (of an operation-switched module) that write to an external system.
    write_operations: list[str] = Field(default_factory=list)
    #: Name of the input that selects the operation (``operation``, ``method``...).
    write_operation_key: str = "operation"
    #: True when every call writes to an external system, whatever its inputs.
    always_write: bool = False
    required_credential_types: list[str] = Field(default_factory=list)
    supported_locales: list[str] = Field(default_factory=list)
    supported_modalities: list[str] = Field(default_factory=lambda: ["text"])
    source_reference: str | None = None
    license: str | None = None
    verification_status: VerificationStatus = "unverified"
    display: DisplayMetadata | None = None
    #: Service-internal module (auth, workspaces, triggers, ...). It stays registered and runs
    #: in existing workflows, but builders such as Studio's module palette do not offer it as a
    #: new node. Discovery metadata only: excluded from the canonical digest.
    internal: bool = False

    @field_validator("module_id")
    @classmethod
    def validate_module_id(cls, v: str) -> str:
        if not _MODULE_ID_REGEX.match(v):
            raise ValueError(
                f"module_id '{v}' is invalid. "
                "Must be lowercase alphanumeric with namespace (e.g. 'nexura.text.normalize')."
            )
        return v

    @field_validator("version")
    @classmethod
    def validate_version(cls, v: str) -> str:
        if not _SEMVER_REGEX.match(v):
            raise ValueError(f"version '{v}' must be semantic versioning (e.g. '1.0.0').")
        return v

    def write_gate(self, operation: Any) -> bool:
        """True when running ``operation`` needs a human approval.

        Modules with no declared write operations never gate. Otherwise an operation that is
        not a string (unknown at this point) is gated conservatively, and so is every
        declared write operation. Declared reads and unknown names are not gated.
        """
        if not self.write_operations:
            return False
        if not isinstance(operation, str):
            return True
        return operation in self.write_operations

    def write_gate_inputs(self, inputs: dict[str, Any]) -> bool:
        """True when a call with ``inputs`` is a write that needs human approval."""
        if self.always_write:
            return True
        return self.write_gate(inputs.get(self.write_operation_key))

    @property
    def digest(self) -> str:
        """Compute the canonical SHA-256 hash of this manifest."""
        return self.canonical_hash()

    def canonical_hash(self) -> str:
        """Generate deterministic canonical digest.

        ``internal`` is palette metadata, not behaviour, so it is left out: flagging a module
        internal does not change its digest or the manifest hashes in compiled plans.
        """
        dumped = self.model_dump(mode="json", exclude={"internal"})
        return canonical_digest(dumped)

    def validate_inputs(self, inputs: dict[str, Any]) -> None:
        """Validate input payload against input_schema.

        Raises:
            SchemaValidationError: If inputs do not conform to schema.
        """
        validate_schema(inputs, self.input_schema, path="inputs", schema_kind="input")

    def validate_outputs(self, outputs: dict[str, Any]) -> None:
        """Validate output payload against output_schema.

        Raises:
            SchemaValidationError: If outputs do not conform to schema.
        """
        validate_schema(outputs, self.output_schema, path="outputs", schema_kind="output")

    def validate_config(self, config: dict[str, Any]) -> None:
        """Validate configuration payload against config_schema.

        Raises:
            ConfigValidationError: If configuration does not conform to schema.
        """
        try:
            validate_schema(config, self.config_schema, path="config", schema_kind="config")
        except SchemaValidationError as e:
            raise ConfigValidationError(
                f"Configuration validation failed: {e.message}",
                details=e.details,
                original_error=e,
            ) from e
