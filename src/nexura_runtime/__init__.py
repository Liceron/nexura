# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Public SDK exports, loaded lazily so optional capabilities stay optional."""

from __future__ import annotations

import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import sys
import types as _types
from typing import TYPE_CHECKING, Any

# The agent SDK core lives in the separate ``nexura_agents`` package. These names keep
# every ``nexura_runtime.<module>`` import path working and resolve to the very same
# module objects, so classes, isinstance checks and patches stay identical.
_MOVED_TO_AGENTS: dict[str, str] = {
    **{
        name: name
        for name in (
            "agent",
            "runner",
            "tools",
            "run_context",
            "guardrails",
            "hooks",
            "limits",
            "events",
            "types",
            "errors",
            "session",
            "models",
            "providers",
            "usage",
            "contracts",
            "context",
            "network",
        )
    },
    "integrations.mcp": "mcp",
    "integrations.openapi": "openapi",
}


class _AgentsAliasFinder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    _prefix = __name__ + "."

    def find_spec(
        self, fullname: str, path: Any = None, target: Any = None
    ) -> importlib.machinery.ModuleSpec | None:
        if not fullname.startswith(self._prefix):
            return None
        if self._target(fullname) is None:
            return None
        return importlib.util.spec_from_loader(fullname, self)

    def _target(self, fullname: str) -> str | None:
        rest = fullname[len(self._prefix) :]
        for old, new in _MOVED_TO_AGENTS.items():
            if rest == old or rest.startswith(old + "."):
                return "nexura_agents." + new + rest[len(old) :]
        return None

    def create_module(self, spec: importlib.machinery.ModuleSpec) -> _types.ModuleType:
        target = self._target(spec.name)
        assert target is not None
        return importlib.import_module(target)

    def exec_module(self, module: _types.ModuleType) -> None:
        return None


if not any(isinstance(f, _AgentsAliasFinder) for f in sys.meta_path):
    sys.meta_path.insert(0, _AgentsAliasFinder())

__version__ = "0.4.2"

__all__ = [
    "Agent",
    "Runner",
    "Tool",
    "tool",
    "ModelProtocol",
    "FakeModel",
    "OpenAIChatModel",
    "SessionStore",
    "SessionSnapshot",
    "MemorySessionStore",
    "SQLiteSessionStore",
    "serialize_session",
    "deserialize_session",
    "WorkflowSpec",
    "WorkflowNode",
    "ModuleRegistry",
    "WorkflowEngine",
    "WorkflowRunResult",
    "WorkflowCheckpoint",
    "PendingApproval",
    "HandlerOutput",
    "WorkflowCheckpointStore",
    "MemoryWorkflowStore",
    "SQLiteWorkflowStore",
    "WorkflowStatus",
    "NodeRetryPolicy",
    "WorkflowInterrupt",
    "interrupt",
    "RunEvent",
    "RunEventType",
    "RunResult",
    "Message",
    "Role",
    "Usage",
    "ToolCall",
    "ToolResult",
    "ModelResponse",
    "NexuraError",
    "SDKValidationError",
    "ToolError",
    "UnknownToolError",
    "ToolValidationError",
    "ToolExecutionError",
    "ToolResultSerializationError",
    "ProviderError",
    "ProviderAuthError",
    "ProviderRateLimitError",
    "ProviderRequestError",
    "ProviderResponseError",
    "RunnerError",
    "MaxTurnsExceededError",
    "MaxToolCallsExceededError",
    "ModelTimeoutError",
    "ExecutionCancelledError",
    "EventLoopError",
    "StorageError",
    "SessionConflictError",
    "StorageSerializationError",
    "WorkflowError",
    "WorkflowValidationError",
    "WorkflowConflictError",
    "WorkflowSpecMismatchError",
    "WorkflowExecutionError",
    "WorkflowMaxStepsError",
    "WorkflowTimeoutError",
    "WorkflowTerminalError",
    "ApprovalError",
    "ApprovalAlreadyClaimedError",
    "BaseModule",
    "ModuleManifest",
    "ExecutionContext",
    "ModuleResult",
    "ModuleEvent",
    "ModuleEventType",
    "ArtifactReference",
    "ContractError",
    "ContractErrorCode",
    "canonical_json",
    "canonical_digest",
    "VersionedModuleRegistry",
    "default_registry",
    "LocalArtifactStore",
    "ScopedCredentialStore",
    "SecretValue",
    "DestinationPolicy",
    "SafeHttpClient",
    "StorageRepository",
    "SQLiteRepository",
    "RunRecord",
    "RunEventRecord",
    "get_storage_repository",
    "EventRecord",
    "MemoryEventSink",
    "MetricsCollector",
    "NexuraTracer",
    "SqliteEventSink",
    "BudgetConfig",
    "BudgetStatus",
    "CostEstimate",
    "FXRate",
    "ModelPricing",
    "PricingTable",
    "CurrencyConverter",
    "Reservation",
    "LedgerEntry",
    "UsageLedger",
    "BudgetManager",
    "RunBudget",
    "UsageBudgetModule",
    "UsageError",
    "BudgetExceededError",
    "DatasetExample",
    "EvaluationDataset",
    "EvaluationSummary",
    "ExampleEvaluationResult",
    "GraderResult",
    "Grader",
    "ExactMatchGrader",
    "ContainsGrader",
    "RegexGrader",
    "JsonSchemaGrader",
    "CitationCoverageGrader",
    "ZeroSideEffectsGrader",
    "ModelJudgeGrader",
    "DatasetRegistry",
    "create_synthetic_support_dataset",
    "EvaluationRunner",
    "EvaluationModule",
    "EvaluationError",
    "DatasetNotFoundError",
    "DatasetValidationError",
    "JudgeUnauthorizedEffectError",
    "EvaluationBudgetExceededError",
    "RegressionDetectedError",
    "CapabilityUnavailableError",
    "ExecutionRequest",
    "ExecutionResult",
    "ExecutionTimeoutError",
    "IsolatedProcessWorker",
    "MockDisposableWorker",
    "OutputLimitExceededError",
    "SandboxConfig",
    "SandboxError",
    "SandboxExecutionModule",
    "SecurityViolationError",
    "create_api_app",
    "NexuraClient",
    "NexuraApiError",
    "WorkflowExporter",
    "ExportBundle",
    "BackupEngine",
    "HealthChecker",
    "ReleaseDiagnostics",
    "WorkflowMigrator",
    "get_matrix",
    "__version__",
]

if TYPE_CHECKING:
    from nexura_agents.agent import Agent as Agent
    from nexura_agents.contracts import ArtifactReference as ArtifactReference
    from nexura_agents.contracts import BaseModule as BaseModule
    from nexura_agents.contracts import ContractError as ContractError
    from nexura_agents.contracts import ContractErrorCode as ContractErrorCode
    from nexura_agents.contracts import ExecutionContext as ExecutionContext
    from nexura_agents.contracts import ModuleEvent as ModuleEvent
    from nexura_agents.contracts import ModuleEventType as ModuleEventType
    from nexura_agents.contracts import ModuleManifest as ModuleManifest
    from nexura_agents.contracts import ModuleResult as ModuleResult
    from nexura_agents.contracts import canonical_digest as canonical_digest
    from nexura_agents.contracts import canonical_json as canonical_json
    from nexura_agents.errors import ApprovalAlreadyClaimedError as ApprovalAlreadyClaimedError
    from nexura_agents.errors import ApprovalError as ApprovalError
    from nexura_agents.errors import EventLoopError as EventLoopError
    from nexura_agents.errors import ExecutionCancelledError as ExecutionCancelledError
    from nexura_agents.errors import MaxToolCallsExceededError as MaxToolCallsExceededError
    from nexura_agents.errors import MaxTurnsExceededError as MaxTurnsExceededError
    from nexura_agents.errors import ModelTimeoutError as ModelTimeoutError
    from nexura_agents.errors import NexuraError as NexuraError
    from nexura_agents.errors import ProviderAuthError as ProviderAuthError
    from nexura_agents.errors import ProviderError as ProviderError
    from nexura_agents.errors import ProviderRateLimitError as ProviderRateLimitError
    from nexura_agents.errors import ProviderRequestError as ProviderRequestError
    from nexura_agents.errors import ProviderResponseError as ProviderResponseError
    from nexura_agents.errors import RunnerError as RunnerError
    from nexura_agents.errors import SDKValidationError as SDKValidationError
    from nexura_agents.errors import SessionConflictError as SessionConflictError
    from nexura_agents.errors import StorageError as StorageError
    from nexura_agents.errors import StorageSerializationError as StorageSerializationError
    from nexura_agents.errors import ToolError as ToolError
    from nexura_agents.errors import ToolExecutionError as ToolExecutionError
    from nexura_agents.errors import ToolResultSerializationError as ToolResultSerializationError
    from nexura_agents.errors import ToolValidationError as ToolValidationError
    from nexura_agents.errors import UnknownToolError as UnknownToolError
    from nexura_agents.errors import WorkflowConflictError as WorkflowConflictError
    from nexura_agents.errors import WorkflowError as WorkflowError
    from nexura_agents.errors import WorkflowExecutionError as WorkflowExecutionError
    from nexura_agents.errors import WorkflowMaxStepsError as WorkflowMaxStepsError
    from nexura_agents.errors import WorkflowSpecMismatchError as WorkflowSpecMismatchError
    from nexura_agents.errors import WorkflowTerminalError as WorkflowTerminalError
    from nexura_agents.errors import WorkflowTimeoutError as WorkflowTimeoutError
    from nexura_agents.errors import WorkflowValidationError as WorkflowValidationError
    from nexura_agents.events import RunEvent as RunEvent
    from nexura_agents.events import RunEventType as RunEventType
    from nexura_agents.models import FakeModel as FakeModel
    from nexura_agents.models import ModelProtocol as ModelProtocol
    from nexura_agents.models import OpenAIChatModel as OpenAIChatModel
    from nexura_agents.network import DestinationPolicy as DestinationPolicy
    from nexura_agents.network import SafeHttpClient as SafeHttpClient
    from nexura_agents.runner import Runner as Runner
    from nexura_agents.session import MemorySessionStore as MemorySessionStore
    from nexura_agents.session import SessionSnapshot as SessionSnapshot
    from nexura_agents.session import SessionStore as SessionStore
    from nexura_agents.session import SQLiteSessionStore as SQLiteSessionStore
    from nexura_agents.session import deserialize_session as deserialize_session
    from nexura_agents.session import serialize_session as serialize_session
    from nexura_agents.tools import Tool as Tool
    from nexura_agents.tools import tool as tool
    from nexura_agents.types import Message as Message
    from nexura_agents.types import ModelResponse as ModelResponse
    from nexura_agents.types import Role as Role
    from nexura_agents.types import RunResult as RunResult
    from nexura_agents.types import ToolCall as ToolCall
    from nexura_agents.types import ToolResult as ToolResult
    from nexura_agents.types import Usage as Usage
    from nexura_agents.usage import BudgetConfig as BudgetConfig
    from nexura_agents.usage import BudgetExceededError as BudgetExceededError
    from nexura_agents.usage import BudgetManager as BudgetManager
    from nexura_agents.usage import BudgetStatus as BudgetStatus
    from nexura_agents.usage import CostEstimate as CostEstimate
    from nexura_agents.usage import CurrencyConverter as CurrencyConverter
    from nexura_agents.usage import FXRate as FXRate
    from nexura_agents.usage import LedgerEntry as LedgerEntry
    from nexura_agents.usage import ModelPricing as ModelPricing
    from nexura_agents.usage import PricingTable as PricingTable
    from nexura_agents.usage import Reservation as Reservation
    from nexura_agents.usage import RunBudget as RunBudget
    from nexura_agents.usage import UsageBudgetModule as UsageBudgetModule
    from nexura_agents.usage import UsageError as UsageError
    from nexura_agents.usage import UsageLedger as UsageLedger

    from nexura_runtime.artifacts import LocalArtifactStore as LocalArtifactStore
    from nexura_runtime.credentials import ScopedCredentialStore as ScopedCredentialStore
    from nexura_runtime.credentials import SecretValue as SecretValue
    from nexura_runtime.evaluation import CitationCoverageGrader as CitationCoverageGrader
    from nexura_runtime.evaluation import ContainsGrader as ContainsGrader
    from nexura_runtime.evaluation import DatasetExample as DatasetExample
    from nexura_runtime.evaluation import DatasetNotFoundError as DatasetNotFoundError
    from nexura_runtime.evaluation import DatasetRegistry as DatasetRegistry
    from nexura_runtime.evaluation import DatasetValidationError as DatasetValidationError
    from nexura_runtime.evaluation import (
        EvaluationBudgetExceededError as EvaluationBudgetExceededError,
    )
    from nexura_runtime.evaluation import EvaluationDataset as EvaluationDataset
    from nexura_runtime.evaluation import EvaluationError as EvaluationError
    from nexura_runtime.evaluation import EvaluationModule as EvaluationModule
    from nexura_runtime.evaluation import EvaluationRunner as EvaluationRunner
    from nexura_runtime.evaluation import EvaluationSummary as EvaluationSummary
    from nexura_runtime.evaluation import ExactMatchGrader as ExactMatchGrader
    from nexura_runtime.evaluation import ExampleEvaluationResult as ExampleEvaluationResult
    from nexura_runtime.evaluation import Grader as Grader
    from nexura_runtime.evaluation import GraderResult as GraderResult
    from nexura_runtime.evaluation import JsonSchemaGrader as JsonSchemaGrader
    from nexura_runtime.evaluation import (
        JudgeUnauthorizedEffectError as JudgeUnauthorizedEffectError,
    )
    from nexura_runtime.evaluation import ModelJudgeGrader as ModelJudgeGrader
    from nexura_runtime.evaluation import RegexGrader as RegexGrader
    from nexura_runtime.evaluation import RegressionDetectedError as RegressionDetectedError
    from nexura_runtime.evaluation import ZeroSideEffectsGrader as ZeroSideEffectsGrader
    from nexura_runtime.evaluation import (
        create_synthetic_support_dataset as create_synthetic_support_dataset,
    )
    from nexura_runtime.integrations.sandbox import (
        CapabilityUnavailableError as CapabilityUnavailableError,
    )
    from nexura_runtime.integrations.sandbox import ExecutionRequest as ExecutionRequest
    from nexura_runtime.integrations.sandbox import ExecutionResult as ExecutionResult
    from nexura_runtime.integrations.sandbox import ExecutionTimeoutError as ExecutionTimeoutError
    from nexura_runtime.integrations.sandbox import IsolatedProcessWorker as IsolatedProcessWorker
    from nexura_runtime.integrations.sandbox import MockDisposableWorker as MockDisposableWorker
    from nexura_runtime.integrations.sandbox import (
        OutputLimitExceededError as OutputLimitExceededError,
    )
    from nexura_runtime.integrations.sandbox import SandboxConfig as SandboxConfig
    from nexura_runtime.integrations.sandbox import SandboxError as SandboxError
    from nexura_runtime.integrations.sandbox import (
        SandboxExecutionModule as SandboxExecutionModule,
    )
    from nexura_runtime.integrations.sandbox import (
        SecurityViolationError as SecurityViolationError,
    )
    from nexura_runtime.modules import VersionedModuleRegistry as VersionedModuleRegistry
    from nexura_runtime.modules import default_registry as default_registry
    from nexura_runtime.observability import EventRecord as EventRecord
    from nexura_runtime.observability import MemoryEventSink as MemoryEventSink
    from nexura_runtime.observability import MetricsCollector as MetricsCollector
    from nexura_runtime.observability import NexuraTracer as NexuraTracer
    from nexura_runtime.observability import SqliteEventSink as SqliteEventSink
    from nexura_runtime.release import BackupEngine as BackupEngine
    from nexura_runtime.release import HealthChecker as HealthChecker
    from nexura_runtime.release import ReleaseDiagnostics as ReleaseDiagnostics
    from nexura_runtime.release import WorkflowMigrator as WorkflowMigrator
    from nexura_runtime.release import get_matrix as get_matrix
    from nexura_runtime.service.api import ExportBundle as ExportBundle
    from nexura_runtime.service.api import NexuraApiError as NexuraApiError
    from nexura_runtime.service.api import NexuraClient as NexuraClient
    from nexura_runtime.service.api import WorkflowExporter as WorkflowExporter
    from nexura_runtime.service.api import create_api_app as create_api_app
    from nexura_runtime.storage import RunEventRecord as RunEventRecord
    from nexura_runtime.storage import RunRecord as RunRecord
    from nexura_runtime.storage import SQLiteRepository as SQLiteRepository
    from nexura_runtime.storage import StorageRepository as StorageRepository
    from nexura_runtime.storage import get_storage_repository as get_storage_repository
    from nexura_runtime.workflow import HandlerOutput as HandlerOutput
    from nexura_runtime.workflow import MemoryWorkflowStore as MemoryWorkflowStore
    from nexura_runtime.workflow import ModuleRegistry as ModuleRegistry
    from nexura_runtime.workflow import PendingApproval as PendingApproval
    from nexura_runtime.workflow import SQLiteWorkflowStore as SQLiteWorkflowStore
    from nexura_runtime.workflow import WorkflowCheckpoint as WorkflowCheckpoint
    from nexura_runtime.workflow import WorkflowCheckpointStore as WorkflowCheckpointStore
    from nexura_runtime.workflow import WorkflowEngine as WorkflowEngine
    from nexura_runtime.workflow import WorkflowNode as WorkflowNode
    from nexura_runtime.workflow import WorkflowRunResult as WorkflowRunResult
    from nexura_runtime.workflow import WorkflowSpec as WorkflowSpec
    from nexura_runtime.workflow import WorkflowStatus as WorkflowStatus

_EXPORTS: dict[str, tuple[str, str]] = {
    "Agent": ("nexura_agents.agent", "Agent"),
    "ApprovalAlreadyClaimedError": ("nexura_agents.errors", "ApprovalAlreadyClaimedError"),
    "ApprovalError": ("nexura_agents.errors", "ApprovalError"),
    "ArtifactReference": ("nexura_agents.contracts", "ArtifactReference"),
    "BackupEngine": ("nexura_runtime.release", "BackupEngine"),
    "BaseModule": ("nexura_agents.contracts", "BaseModule"),
    "BudgetConfig": ("nexura_agents.usage", "BudgetConfig"),
    "BudgetExceededError": ("nexura_agents.usage", "BudgetExceededError"),
    "BudgetManager": ("nexura_agents.usage", "BudgetManager"),
    "BudgetStatus": ("nexura_agents.usage", "BudgetStatus"),
    "CapabilityUnavailableError": (
        "nexura_runtime.integrations.sandbox",
        "CapabilityUnavailableError",
    ),
    "CitationCoverageGrader": ("nexura_runtime.evaluation", "CitationCoverageGrader"),
    "ContainsGrader": ("nexura_runtime.evaluation", "ContainsGrader"),
    "ContractError": ("nexura_agents.contracts", "ContractError"),
    "ContractErrorCode": ("nexura_agents.contracts", "ContractErrorCode"),
    "CostEstimate": ("nexura_agents.usage", "CostEstimate"),
    "CurrencyConverter": ("nexura_agents.usage", "CurrencyConverter"),
    "DatasetExample": ("nexura_runtime.evaluation", "DatasetExample"),
    "DatasetNotFoundError": ("nexura_runtime.evaluation", "DatasetNotFoundError"),
    "DatasetRegistry": ("nexura_runtime.evaluation", "DatasetRegistry"),
    "DatasetValidationError": ("nexura_runtime.evaluation", "DatasetValidationError"),
    "DestinationPolicy": ("nexura_agents.network", "DestinationPolicy"),
    "EvaluationBudgetExceededError": (
        "nexura_runtime.evaluation",
        "EvaluationBudgetExceededError",
    ),
    "EvaluationDataset": ("nexura_runtime.evaluation", "EvaluationDataset"),
    "EvaluationError": ("nexura_runtime.evaluation", "EvaluationError"),
    "EvaluationModule": ("nexura_runtime.evaluation", "EvaluationModule"),
    "EvaluationRunner": ("nexura_runtime.evaluation", "EvaluationRunner"),
    "EvaluationSummary": ("nexura_runtime.evaluation", "EvaluationSummary"),
    "EventLoopError": ("nexura_agents.errors", "EventLoopError"),
    "EventRecord": ("nexura_runtime.observability", "EventRecord"),
    "ExactMatchGrader": ("nexura_runtime.evaluation", "ExactMatchGrader"),
    "ExampleEvaluationResult": ("nexura_runtime.evaluation", "ExampleEvaluationResult"),
    "ExecutionCancelledError": ("nexura_agents.errors", "ExecutionCancelledError"),
    "ExecutionContext": ("nexura_agents.contracts", "ExecutionContext"),
    "ExecutionRequest": ("nexura_runtime.integrations.sandbox", "ExecutionRequest"),
    "ExecutionResult": ("nexura_runtime.integrations.sandbox", "ExecutionResult"),
    "ExecutionTimeoutError": ("nexura_runtime.integrations.sandbox", "ExecutionTimeoutError"),
    "ExportBundle": ("nexura_runtime.service.api", "ExportBundle"),
    "FXRate": ("nexura_agents.usage", "FXRate"),
    "FakeModel": ("nexura_agents.models", "FakeModel"),
    "Grader": ("nexura_runtime.evaluation", "Grader"),
    "GraderResult": ("nexura_runtime.evaluation", "GraderResult"),
    "HandlerOutput": ("nexura_runtime.workflow", "HandlerOutput"),
    "HealthChecker": ("nexura_runtime.release", "HealthChecker"),
    "IsolatedProcessWorker": ("nexura_runtime.integrations.sandbox", "IsolatedProcessWorker"),
    "JsonSchemaGrader": ("nexura_runtime.evaluation", "JsonSchemaGrader"),
    "JudgeUnauthorizedEffectError": ("nexura_runtime.evaluation", "JudgeUnauthorizedEffectError"),
    "LedgerEntry": ("nexura_agents.usage", "LedgerEntry"),
    "LocalArtifactStore": ("nexura_runtime.artifacts", "LocalArtifactStore"),
    "MaxToolCallsExceededError": ("nexura_agents.errors", "MaxToolCallsExceededError"),
    "MaxTurnsExceededError": ("nexura_agents.errors", "MaxTurnsExceededError"),
    "MemoryEventSink": ("nexura_runtime.observability", "MemoryEventSink"),
    "MemorySessionStore": ("nexura_agents.session", "MemorySessionStore"),
    "MemoryWorkflowStore": ("nexura_runtime.workflow", "MemoryWorkflowStore"),
    "Message": ("nexura_agents.types", "Message"),
    "MetricsCollector": ("nexura_runtime.observability", "MetricsCollector"),
    "MockDisposableWorker": ("nexura_runtime.integrations.sandbox", "MockDisposableWorker"),
    "ModelJudgeGrader": ("nexura_runtime.evaluation", "ModelJudgeGrader"),
    "ModelPricing": ("nexura_agents.usage", "ModelPricing"),
    "ModelProtocol": ("nexura_agents.models", "ModelProtocol"),
    "ModelResponse": ("nexura_agents.types", "ModelResponse"),
    "ModelTimeoutError": ("nexura_agents.errors", "ModelTimeoutError"),
    "ModuleEvent": ("nexura_agents.contracts", "ModuleEvent"),
    "ModuleEventType": ("nexura_agents.contracts", "ModuleEventType"),
    "ModuleManifest": ("nexura_agents.contracts", "ModuleManifest"),
    "ModuleRegistry": ("nexura_runtime.workflow", "ModuleRegistry"),
    "ModuleResult": ("nexura_agents.contracts", "ModuleResult"),
    "NexuraApiError": ("nexura_runtime.service.api", "NexuraApiError"),
    "NexuraClient": ("nexura_runtime.service.api", "NexuraClient"),
    "NexuraError": ("nexura_agents.errors", "NexuraError"),
    "NexuraTracer": ("nexura_runtime.observability", "NexuraTracer"),
    "OpenAIChatModel": ("nexura_agents.models", "OpenAIChatModel"),
    "OutputLimitExceededError": (
        "nexura_runtime.integrations.sandbox",
        "OutputLimitExceededError",
    ),
    "PendingApproval": ("nexura_runtime.workflow", "PendingApproval"),
    "PricingTable": ("nexura_agents.usage", "PricingTable"),
    "ProviderAuthError": ("nexura_agents.errors", "ProviderAuthError"),
    "ProviderError": ("nexura_agents.errors", "ProviderError"),
    "ProviderRateLimitError": ("nexura_agents.errors", "ProviderRateLimitError"),
    "ProviderRequestError": ("nexura_agents.errors", "ProviderRequestError"),
    "ProviderResponseError": ("nexura_agents.errors", "ProviderResponseError"),
    "RegexGrader": ("nexura_runtime.evaluation", "RegexGrader"),
    "RegressionDetectedError": ("nexura_runtime.evaluation", "RegressionDetectedError"),
    "ReleaseDiagnostics": ("nexura_runtime.release", "ReleaseDiagnostics"),
    "Reservation": ("nexura_agents.usage", "Reservation"),
    "Role": ("nexura_agents.types", "Role"),
    "RunBudget": ("nexura_agents.usage", "RunBudget"),
    "RunEvent": ("nexura_agents.events", "RunEvent"),
    "RunEventRecord": ("nexura_runtime.storage", "RunEventRecord"),
    "RunEventType": ("nexura_agents.events", "RunEventType"),
    "RunRecord": ("nexura_runtime.storage", "RunRecord"),
    "RunResult": ("nexura_agents.types", "RunResult"),
    "Runner": ("nexura_agents.runner", "Runner"),
    "RunnerError": ("nexura_agents.errors", "RunnerError"),
    "SDKValidationError": ("nexura_agents.errors", "SDKValidationError"),
    "SQLiteRepository": ("nexura_runtime.storage", "SQLiteRepository"),
    "SQLiteSessionStore": ("nexura_agents.session", "SQLiteSessionStore"),
    "SQLiteWorkflowStore": ("nexura_runtime.workflow", "SQLiteWorkflowStore"),
    "SafeHttpClient": ("nexura_agents.network", "SafeHttpClient"),
    "SandboxConfig": ("nexura_runtime.integrations.sandbox", "SandboxConfig"),
    "SandboxError": ("nexura_runtime.integrations.sandbox", "SandboxError"),
    "SandboxExecutionModule": ("nexura_runtime.integrations.sandbox", "SandboxExecutionModule"),
    "ScopedCredentialStore": ("nexura_runtime.credentials", "ScopedCredentialStore"),
    "SecretValue": ("nexura_runtime.credentials", "SecretValue"),
    "SecurityViolationError": ("nexura_runtime.integrations.sandbox", "SecurityViolationError"),
    "SessionConflictError": ("nexura_agents.errors", "SessionConflictError"),
    "SessionSnapshot": ("nexura_agents.session", "SessionSnapshot"),
    "SessionStore": ("nexura_agents.session", "SessionStore"),
    "SqliteEventSink": ("nexura_runtime.observability", "SqliteEventSink"),
    "StorageError": ("nexura_agents.errors", "StorageError"),
    "StorageRepository": ("nexura_runtime.storage", "StorageRepository"),
    "StorageSerializationError": ("nexura_agents.errors", "StorageSerializationError"),
    "Tool": ("nexura_agents.tools", "Tool"),
    "ToolCall": ("nexura_agents.types", "ToolCall"),
    "ToolError": ("nexura_agents.errors", "ToolError"),
    "ToolExecutionError": ("nexura_agents.errors", "ToolExecutionError"),
    "ToolResult": ("nexura_agents.types", "ToolResult"),
    "ToolResultSerializationError": ("nexura_agents.errors", "ToolResultSerializationError"),
    "ToolValidationError": ("nexura_agents.errors", "ToolValidationError"),
    "UnknownToolError": ("nexura_agents.errors", "UnknownToolError"),
    "Usage": ("nexura_agents.types", "Usage"),
    "UsageBudgetModule": ("nexura_agents.usage", "UsageBudgetModule"),
    "UsageError": ("nexura_agents.usage", "UsageError"),
    "UsageLedger": ("nexura_agents.usage", "UsageLedger"),
    "VersionedModuleRegistry": ("nexura_runtime.modules", "VersionedModuleRegistry"),
    "WorkflowCheckpoint": ("nexura_runtime.workflow", "WorkflowCheckpoint"),
    "WorkflowCheckpointStore": ("nexura_runtime.workflow", "WorkflowCheckpointStore"),
    "WorkflowConflictError": ("nexura_agents.errors", "WorkflowConflictError"),
    "WorkflowEngine": ("nexura_runtime.workflow", "WorkflowEngine"),
    "WorkflowInterrupt": ("nexura_runtime.workflow", "WorkflowInterrupt"),
    "NodeRetryPolicy": ("nexura_runtime.workflow", "NodeRetryPolicy"),
    "interrupt": ("nexura_runtime.workflow", "interrupt"),
    "WorkflowError": ("nexura_agents.errors", "WorkflowError"),
    "WorkflowExecutionError": ("nexura_agents.errors", "WorkflowExecutionError"),
    "WorkflowExporter": ("nexura_runtime.service.api", "WorkflowExporter"),
    "WorkflowMaxStepsError": ("nexura_agents.errors", "WorkflowMaxStepsError"),
    "WorkflowMigrator": ("nexura_runtime.release", "WorkflowMigrator"),
    "WorkflowNode": ("nexura_runtime.workflow", "WorkflowNode"),
    "WorkflowRunResult": ("nexura_runtime.workflow", "WorkflowRunResult"),
    "WorkflowSpec": ("nexura_runtime.workflow", "WorkflowSpec"),
    "WorkflowSpecMismatchError": ("nexura_agents.errors", "WorkflowSpecMismatchError"),
    "WorkflowStatus": ("nexura_runtime.workflow", "WorkflowStatus"),
    "WorkflowTerminalError": ("nexura_agents.errors", "WorkflowTerminalError"),
    "WorkflowTimeoutError": ("nexura_agents.errors", "WorkflowTimeoutError"),
    "WorkflowValidationError": ("nexura_agents.errors", "WorkflowValidationError"),
    "ZeroSideEffectsGrader": ("nexura_runtime.evaluation", "ZeroSideEffectsGrader"),
    "canonical_digest": ("nexura_agents.contracts", "canonical_digest"),
    "canonical_json": ("nexura_agents.contracts", "canonical_json"),
    "create_api_app": ("nexura_runtime.service.api", "create_api_app"),
    "create_synthetic_support_dataset": (
        "nexura_runtime.evaluation",
        "create_synthetic_support_dataset",
    ),
    "default_registry": ("nexura_runtime.modules", "default_registry"),
    "deserialize_session": ("nexura_agents.session", "deserialize_session"),
    "get_matrix": ("nexura_runtime.release", "get_matrix"),
    "get_storage_repository": ("nexura_runtime.storage", "get_storage_repository"),
    "serialize_session": ("nexura_agents.session", "serialize_session"),
    "tool": ("nexura_agents.tools", "tool"),
}


# Removed by P35 A6 S12 (the legacy DurableExecutionEngine): importing one raises an
# ImportError that names the replacement, for one release.
_REMOVED = frozenset(
    {
        "DurableExecutionEngine",
        "DurableExecutionModule",
        "DurableRunState",
        "DurableWorkflowSpec",
        "DurableNodeSpec",
        "FencingConflictError",
        "LeaseExpiredError",
    }
)


def __getattr__(name: str) -> Any:
    target = _EXPORTS.get(name)
    if target is None:
        if name in _REMOVED:
            from nexura_runtime._removed import removed

            removed(__name__, name, _REMOVED)
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(target[0]), target[1])
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_EXPORTS))
