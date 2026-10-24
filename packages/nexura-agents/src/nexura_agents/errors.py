# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Structured exception hierarchy for Nexura AgentKit.

All SDK exceptions inherit from NexuraError. Causes and diagnostic details
are preserved without leaking sensitive credentials or payloads.
"""

from typing import Any


class NexuraError(Exception):
    """Base exception for all errors in Nexura AgentKit."""

    def __init__(self, message: str, *, original_error: BaseException | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.original_error = original_error
        if original_error is not None:
            self.__cause__ = original_error

    def __reduce__(self) -> tuple[Any, ...]:
        # Subclasses take their own constructor arguments, so the default pickling (which
        # calls ``cls(*args)``) fails for them. Rebuild without calling ``__init__``
        # instead, so errors can cross process boundaries (multiprocessing, task queues).
        # A value that cannot be pickled (a cause holding a lock or a client) is replaced
        # by a readable stand-in rather than making the whole error unpicklable.
        state = {key: _picklable(value) for key, value in self.__dict__.items()}
        return (_restore_error, (type(self), _picklable(self.args), state))


def _picklable(value: Any) -> Any:
    import pickle

    try:
        pickle.dumps(value)
    except Exception:
        if isinstance(value, BaseException):
            return RuntimeError(f"{type(value).__name__}: {value}")
        if isinstance(value, tuple):
            return tuple(_picklable(v) for v in value)
        return repr(value)
    return value


def _restore_error(cls: type[NexuraError], args: tuple[Any, ...], state: dict[str, Any]) -> Any:
    err = cls.__new__(cls, *args)
    err.args = args
    err.__dict__.update(state)
    if err.__dict__.get("original_error") is not None:
        err.__cause__ = err.__dict__["original_error"]
    return err


class SDKValidationError(NexuraError):
    """Raised when SDK configurations, parameters, or limits are invalid."""


class ToolError(NexuraError):
    """Base exception for tool-related failures."""


class UnknownToolError(ToolError):
    """Raised when the model requests execution of an unregistered tool."""

    def __init__(
        self,
        tool_name: str,
        *,
        permitted_tools: list[str] | None = None,
        original_error: BaseException | None = None,
    ) -> None:
        message = f"Unknown tool requested: '{tool_name}'."
        if permitted_tools:
            message += f" Permitted tools: {permitted_tools}."
        super().__init__(message, original_error=original_error)
        self.tool_name = tool_name
        self.permitted_tools = permitted_tools or []


class ToolValidationError(ToolError):
    """Raised when tool arguments fail schema validation."""

    def __init__(
        self,
        tool_name: str,
        message: str,
        *,
        arguments: dict[str, Any] | None = None,
        original_error: BaseException | None = None,
    ) -> None:
        full_msg = f"Validation failed for tool '{tool_name}': {message}"
        super().__init__(full_msg, original_error=original_error)
        self.tool_name = tool_name
        self.arguments = arguments


class ToolExecutionError(ToolError):
    """Raised when a tool raises an unhandled exception during execution."""

    def __init__(
        self,
        tool_name: str,
        *,
        original_error: BaseException | None = None,
    ) -> None:
        cause_str = (
            f" ({type(original_error).__name__}: {original_error})" if original_error else ""
        )
        super().__init__(
            f"Execution failed for tool '{tool_name}'{cause_str}",
            original_error=original_error,
        )
        self.tool_name = tool_name


class ToolResultSerializationError(ToolError):
    """Raised when a tool return value cannot be JSON-serialized."""

    def __init__(
        self,
        tool_name: str,
        *,
        original_error: BaseException | None = None,
    ) -> None:
        super().__init__(
            f"Result from tool '{tool_name}' is not JSON serializable",
            original_error=original_error,
        )
        self.tool_name = tool_name


class ProviderError(NexuraError):
    """Base exception for LLM provider errors.

    Ensures API keys and sensitive tokens are masked from messages and representations.
    """


class ProviderAuthError(ProviderError):
    """Raised on authentication failures (e.g., HTTP 401)."""


class ProviderRateLimitError(ProviderError):
    """Raised when rate limits or quotas are exceeded (e.g., HTTP 429)."""


class ProviderRequestError(ProviderError):
    """Raised when the provider cannot be reached (e.g., network timeout, DNS failure)."""


class ProviderResponseError(ProviderError):
    """Raised when provider returns an unparseable or schema-violating response."""


class RunnerError(NexuraError):
    """Base exception for runner loop execution errors."""


class MaxTurnsExceededError(RunnerError):
    """Raised when the model/tool execution loop exceeds configured maximum turns."""

    def __init__(self, turns_count: int, max_turns: int) -> None:
        super().__init__(
            f"Execution loop exceeded maximum allowed turns ({turns_count} > {max_turns})"
        )
        self.turns_count = turns_count
        self.max_turns = max_turns


class MaxToolCallsExceededError(RunnerError):
    """Raised when cumulative tool calls exceed configured maximum limit."""

    def __init__(self, calls_count: int, max_tool_calls: int) -> None:
        super().__init__(
            f"Execution loop exceeded maximum allowed tool calls ({calls_count} > {max_tool_calls})"
        )
        self.calls_count = calls_count
        self.max_tool_calls = max_tool_calls


class MaxHandoffsExceededError(RunnerError):
    """Raised when a run hands off between agents more often than ``max_handoffs`` allows."""

    def __init__(self, handoffs_count: int, max_handoffs: int) -> None:
        super().__init__(
            f"Run exceeded maximum allowed handoffs ({handoffs_count} > {max_handoffs})"
        )
        self.handoffs_count = handoffs_count
        self.max_handoffs = max_handoffs


class ModelTimeoutError(RunnerError):
    """Raised when execution duration exceeds configured timeout limit."""

    def __init__(self, timeout_seconds: float, elapsed_seconds: float) -> None:
        super().__init__(
            f"Execution exceeded timeout limit ({elapsed_seconds:.2f}s >= {timeout_seconds:.2f}s)"
        )
        self.timeout_seconds = timeout_seconds
        self.elapsed_seconds = elapsed_seconds


class ExecutionCancelledError(RunnerError):
    """Raised when an execution run is explicitly cancelled."""


class EventLoopError(RunnerError):
    """Raised when synchronous run() is invoked inside an active asyncio event loop."""


class ModelRetry(ToolError):
    """Raise inside a tool (or output validator) to send ``message`` back to the model
    and let it try again, instead of failing the run."""

    def __init__(self, message: str) -> None:
        super().__init__(message)


class OutputValidationError(RunnerError):
    """Raised when the model's final output still fails ``output_type`` after all retries."""

    def __init__(self, message: str, *, raw_output: str | None, attempts: int) -> None:
        super().__init__(f"Output failed validation after {attempts} attempt(s): {message}")
        # The rejected reply; None when the agent has output guardrails (the reply never
        # passed them, so it is not kept, P39 G4).
        self.raw_output = raw_output
        self.attempts = attempts


class GuardrailTripwireTriggered(RunnerError):
    """Raised when an input or output guardrail stops the run."""

    def __init__(
        self, guardrail: str, stage: str, info: Any = None, *, tool: str | None = None
    ) -> None:
        where = f" on tool '{tool}'" if tool else ""
        super().__init__(f"{stage.capitalize()} guardrail '{guardrail}' tripped{where}.")
        self.guardrail = guardrail
        self.stage = stage  # "input", "output", "tool input" or "tool output"
        self.info = info
        self.tool = tool


class UsageLimitExceededError(RunnerError):
    """Raised when a run exceeds a configured ``UsageLimits`` value."""

    def __init__(self, limit: str, value: int, maximum: int) -> None:
        super().__init__(f"Usage limit '{limit}' exceeded: {value} > {maximum}.")
        self.limit = limit
        self.value = value
        self.maximum = maximum


class ApprovalDecisionError(RunnerError):
    """Raised when resume decisions do not match the paused run's pending tool calls."""


class RunRecoveryError(RunnerError):
    """Raised when a durable run cannot be recovered (unknown, corrupt or mismatched
    checkpoint, or an uncertain tool call with ``on_uncertain="fail"``)."""


class CheckpointLeaseLostError(RunRecoveryError):
    """A checkpoint write was refused because another caller now holds the run's
    recovery lease; this caller must stop so it cannot overwrite newer progress."""


class StorageError(NexuraError):
    """Base exception for persistence and storage errors."""


class SessionConflictError(StorageError):
    """Raised when a compare-and-swap update detects a revision conflict."""

    def __init__(
        self,
        session_id: str,
        expected_revision: int,
        actual_revision: int,
    ) -> None:
        super().__init__(
            f"Session conflict on '{session_id}': expected revision {expected_revision}, "
            f"found {actual_revision}."
        )
        self.session_id = session_id
        self.expected_revision = expected_revision
        self.actual_revision = actual_revision


class StorageSerializationError(StorageError):
    """Raised when session or checkpoint data cannot be serialized or deserialized."""


class WorkflowError(NexuraError):
    """Base exception for workflow specification and execution errors."""


class WorkflowValidationError(WorkflowError):
    """Raised when a workflow specification is malformed or invalid."""


class UnsupportedWorkflowVersionError(WorkflowValidationError):
    """Raised when a workflow specification declares an unsupported or unknown version."""

    def __init__(self, version: Any, supported_versions: Any) -> None:
        sorted_supported = (
            sorted(supported_versions)
            if hasattr(supported_versions, "__iter__")
            else [supported_versions]
        )
        super().__init__(
            f"Unsupported workflow spec_version '{version}'. "
            f"Supported versions: {sorted_supported}."
        )
        self.version = version
        self.supported_versions = list(sorted_supported)


class WorkflowConflictError(WorkflowError):
    """Raised when a workflow update detects a revision conflict."""

    def __init__(self, workflow_id: str, expected_revision: int, actual_revision: int) -> None:
        super().__init__(
            f"Workflow conflict on '{workflow_id}': expected revision {expected_revision}, "
            f"found {actual_revision}."
        )
        self.workflow_id = workflow_id
        self.expected_revision = expected_revision
        self.actual_revision = actual_revision


class WorkflowSpecMismatchError(WorkflowError):
    """Raised when attempting to resume a checkpoint against a different workflow spec."""

    def __init__(self, expected_hash: str, actual_hash: str) -> None:
        super().__init__(
            f"Workflow spec mismatch: checkpoint was created with spec hash {expected_hash!r}, "
            f"current spec hash is {actual_hash!r}."
        )
        self.expected_hash = expected_hash
        self.actual_hash = actual_hash


class WorkflowExecutionError(WorkflowError):
    """Raised when a workflow node handler raises an unhandled error."""

    def __init__(
        self, node_id: str, message: str, *, original_error: BaseException | None = None
    ) -> None:
        super().__init__(
            f"Execution failed at node '{node_id}': {message}",
            original_error=original_error,
        )
        self.node_id = node_id


class WorkflowMaxStepsError(WorkflowError):
    """Raised when workflow execution exceeds configured max_steps."""

    def __init__(self, step_count: int, max_steps: int) -> None:
        super().__init__(f"Workflow exceeded maximum allowed steps ({step_count} >= {max_steps}).")
        self.step_count = step_count
        self.max_steps = max_steps


class WorkflowTimeoutError(WorkflowError):
    """Raised when workflow execution exceeds configured timeout."""

    def __init__(self, timeout_seconds: float, elapsed_seconds: float) -> None:
        super().__init__(
            f"Workflow exceeded timeout limit ({elapsed_seconds:.2f}s >= {timeout_seconds:.2f}s)."
        )
        self.timeout_seconds = timeout_seconds
        self.elapsed_seconds = elapsed_seconds


class WorkflowTerminalError(WorkflowError):
    """Raised when attempting to execute or resume a workflow already in a terminal state."""


class ApprovalError(WorkflowError):
    """Base exception for approval lifecycle errors."""


class ApprovalAlreadyClaimedError(ApprovalError):
    """Raised when an approval request was already claimed or resolved."""
