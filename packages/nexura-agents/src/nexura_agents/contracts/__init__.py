# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Typed module contracts for Nexura AgentKit."""

from __future__ import annotations

import importlib
import importlib.util
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexura_agents.contracts.base import BaseModule
    from nexura_agents.contracts.canonical import canonical_digest, canonical_json
    from nexura_agents.contracts.context import (
        ArtifactStore,
        BudgetInterface,
        CredentialReference,
        CredentialResolver,
        EventSink,
        ExecutionContext,
    )
    from nexura_agents.contracts.errors import (
        BudgetExhaustedError,
        CapabilityUnsupportedError,
        ConfigValidationError,
        ContractError,
        ContractErrorCode,
        DeadlineExceededError,
        IndeterminateEffectError,
        MissingDependencyError,
        PolicyDeniedError,
        SchemaValidationError,
    )
    from nexura_agents.contracts.events import ModuleEvent, ModuleEventType
    from nexura_agents.contracts.execution import (
        Applied,
        DefinitionMismatchError,
        EffectClass,
        EffectRecord,
        EffectResolution,
        EffectStatus,
        ExecutionId,
        LeaseLostError,
        NotApplied,
        ReconcileResult,
        StepIntent,
        StepKey,
        StepOutcome,
        Unknown,
        default_idempotency_key,
        derive_effect_id,
        effect_class_from_idempotent,
        effect_class_from_policy,
    )
    from nexura_agents.contracts.manifest import (
        DisplayMetadata,
        EffectPolicy,
        ModuleManifest,
        PortDefinition,
        VerificationStatus,
    )
    from nexura_agents.contracts.result import ArtifactReference, ModuleResult, ResultStatus

_EXPORTS: dict[str, str] = {
    "Applied": "nexura_agents.contracts.execution",
    "ArtifactReference": "nexura_agents.contracts.result",
    "ArtifactStore": "nexura_agents.contracts.context",
    "BaseModule": "nexura_agents.contracts.base",
    "BudgetExhaustedError": "nexura_agents.contracts.errors",
    "BudgetInterface": "nexura_agents.contracts.context",
    "CapabilityUnsupportedError": "nexura_agents.contracts.errors",
    "ConfigValidationError": "nexura_agents.contracts.errors",
    "ContractError": "nexura_agents.contracts.errors",
    "ContractErrorCode": "nexura_agents.contracts.errors",
    "CredentialReference": "nexura_agents.contracts.context",
    "CredentialResolver": "nexura_agents.contracts.context",
    "DeadlineExceededError": "nexura_agents.contracts.errors",
    "DefinitionMismatchError": "nexura_agents.contracts.execution",
    "DisplayMetadata": "nexura_agents.contracts.manifest",
    "EffectClass": "nexura_agents.contracts.execution",
    "EffectPolicy": "nexura_agents.contracts.manifest",
    "EffectRecord": "nexura_agents.contracts.execution",
    "EffectResolution": "nexura_agents.contracts.execution",
    "EffectStatus": "nexura_agents.contracts.execution",
    "EventSink": "nexura_agents.contracts.context",
    "ExecutionContext": "nexura_agents.contracts.context",
    "ExecutionId": "nexura_agents.contracts.execution",
    "IndeterminateEffectError": "nexura_agents.contracts.errors",
    "LeaseLostError": "nexura_agents.contracts.execution",
    "MissingDependencyError": "nexura_agents.contracts.errors",
    "ModuleEvent": "nexura_agents.contracts.events",
    "ModuleEventType": "nexura_agents.contracts.events",
    "ModuleManifest": "nexura_agents.contracts.manifest",
    "ModuleResult": "nexura_agents.contracts.result",
    "NotApplied": "nexura_agents.contracts.execution",
    "PolicyDeniedError": "nexura_agents.contracts.errors",
    "PortDefinition": "nexura_agents.contracts.manifest",
    "ReconcileResult": "nexura_agents.contracts.execution",
    "ResultStatus": "nexura_agents.contracts.result",
    "SchemaValidationError": "nexura_agents.contracts.errors",
    "StepIntent": "nexura_agents.contracts.execution",
    "StepKey": "nexura_agents.contracts.execution",
    "StepOutcome": "nexura_agents.contracts.execution",
    "Unknown": "nexura_agents.contracts.execution",
    "VerificationStatus": "nexura_agents.contracts.manifest",
    "canonical_digest": "nexura_agents.contracts.canonical",
    "canonical_json": "nexura_agents.contracts.canonical",
    "default_idempotency_key": "nexura_agents.contracts.execution",
    "derive_effect_id": "nexura_agents.contracts.execution",
    "effect_class_from_idempotent": "nexura_agents.contracts.execution",
    "effect_class_from_policy": "nexura_agents.contracts.execution",
}

__all__ = [
    # Base
    "BaseModule",
    # Canonical
    "canonical_json",
    "canonical_digest",
    # Context
    "ExecutionContext",
    "CredentialReference",
    "CredentialResolver",
    "ArtifactStore",
    "BudgetInterface",
    "EventSink",
    # Errors
    "ContractError",
    "ContractErrorCode",
    "SchemaValidationError",
    "ConfigValidationError",
    "MissingDependencyError",
    "PolicyDeniedError",
    "DeadlineExceededError",
    "BudgetExhaustedError",
    "IndeterminateEffectError",
    "CapabilityUnsupportedError",
    # Events
    "ModuleEvent",
    "ModuleEventType",
    # Manifest
    "ModuleManifest",
    "PortDefinition",
    "DisplayMetadata",
    "EffectPolicy",
    "VerificationStatus",
    # Result
    "ModuleResult",
    "ArtifactReference",
    "ResultStatus",
    # Execution Contract v1 (additive; executor adapters are being implemented)
    "EffectClass",
    "ExecutionId",
    "StepKey",
    "StepIntent",
    "StepOutcome",
    "EffectRecord",
    "EffectStatus",
    "EffectResolution",
    "ReconcileResult",
    "Applied",
    "NotApplied",
    "Unknown",
    "LeaseLostError",
    "DefinitionMismatchError",
    "derive_effect_id",
    "default_idempotency_key",
    "effect_class_from_policy",
    "effect_class_from_idempotent",
]


def __getattr__(name: str) -> Any:
    module_path = _EXPORTS.get(name)
    if module_path is None:
        if not name.startswith("_") and importlib.util.find_spec(f"{__name__}.{name}") is not None:
            return importlib.import_module(f"{__name__}.{name}")
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(module_path), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
