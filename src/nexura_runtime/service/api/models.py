# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""API request, response, and pagination domain models."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class PaginationParams(BaseModel):
    """Query parameters for paginated list endpoints."""

    limit: int = Field(default=20, ge=1, le=100, description="Maximum number of items to return")
    offset: int = Field(default=0, ge=0, description="Offset index for pagination")


class PaginatedResponse[T](BaseModel):
    """Standard envelopes for paginated list responses."""

    items: list[T] = Field(description="Page items")
    total: int = Field(description="Total count across all pages")
    limit: int = Field(description="Requested page size limit")
    offset: int = Field(description="Requested page offset")
    has_more: bool = Field(description="True if more items exist past this page")


class CreateWorkflowRequest(BaseModel):
    """Payload to create and persist a new workflow specification."""

    workflow_id: str | None = Field(default=None, description="Optional custom workflow identifier")
    name: str | None = Field(default=None, description="Optional human-readable name")
    spec: dict[str, Any] = Field(description="WorkflowSpec dictionary definition")


class UpdateWorkflowRequest(BaseModel):
    """Payload to update an existing workflow with optimistic concurrency control."""

    expected_revision: int = Field(
        description="Expected current revision number for CAS optimistic concurrency check"
    )
    spec: dict[str, Any] = Field(description="Updated WorkflowSpec dictionary definition")


class StartRunRequest(BaseModel):
    """Payload to initiate an execution run for a workflow."""

    workflow_id: str | None = Field(default=None, description="Persisted workflow ID to execute")
    spec: dict[str, Any] | None = Field(default=None, description="Ad-hoc WorkflowSpec definition")
    initial_state: dict[str, Any] = Field(
        default_factory=dict, description="Initial state parameters for entry node"
    )
    async_execution: bool = Field(
        default=False,
        description="If True, dispatches run in the background and returns 202 Accepted",
    )
    budget_config: dict[str, Any] | None = Field(
        default=None,
        description="Optional budget limits and safety bounds for the run",
    )


class DecisionRequest(BaseModel):
    """Payload to decide a pending approval gate."""

    approval_id: str = Field(description="Pending approval ID to claim and decide")
    decision: Literal["approve", "reject"] = Field(
        default="approve", description="Decision: 'approve' to resume or 'reject' to abort"
    )
    state_update: dict[str, Any] | None = Field(
        default=None, description="Optional state updates applied on approval"
    )


class ProvideInputRequest(BaseModel):
    """Payload answering a run paused by ``interrupt()``."""

    interrupt_id: str = Field(description="Pending interrupt ID being answered")
    value: Any = Field(default=None, description="JSON answer returned by interrupt()")


class ForkRunRequest(BaseModel):
    """Payload to start a new run from a past checkpoint of an existing run."""

    checkpoint_index: int = Field(description="Index into the run history (negative from end)")
    state_update: dict[str, Any] | None = Field(
        default=None, description="Optional state changes applied before the fork runs"
    )


class CancelRunRequest(BaseModel):
    """Payload to explicitly request cancellation of an active run."""

    reason: str | None = Field(default=None, description="Optional cancellation explanation")


class ExportWorkflowRequest(BaseModel):
    """Payload to request standalone portable bundle export."""

    workflow_id: str | None = Field(default=None, description="Persisted workflow ID to export")
    spec: dict[str, Any] | None = Field(default=None, description="Ad-hoc spec to export")
    target_dir: str | None = Field(
        default=None, description="Target filesystem directory for bundle files"
    )


class ExportBundleResponse(BaseModel):
    """Response containing exported bundle files and metadata."""

    workflow_id: str = Field(description="Workflow identifier")
    spec_hash: str = Field(description="Canonical SHA-256 hash of the exported spec")
    bundle_path: str = Field(description="Absolute path to exported bundle directory")
    files: list[str] = Field(description="List of files generated in bundle")


class ImportWorkflowRequest(BaseModel):
    """Payload to request re-import of an exported workflow bundle or spec."""

    spec: dict[str, Any] | None = Field(
        default=None, description="Workflow specification dictionary"
    )
    spec_json: str | None = Field(default=None, description="Workflow specification JSON string")
    archive_base64: str | None = Field(
        default=None, description="Base64-encoded zip archive of exported bundle"
    )
    name: str | None = Field(default=None, description="Optional imported workflow name override")
    workflow_id: str | None = Field(default=None, description="Optional workflow ID override")


class RunEventModel(BaseModel):
    """Typed execution lifecycle event record."""

    seq: int = Field(description="Monotonically increasing sequence number")
    type: str = Field(description="Event type name (e.g., NODE_STARTED, NODE_COMPLETED)")
    node_id: str | None = Field(default=None, description="Node associated with event")
    data: dict[str, Any] = Field(default_factory=dict, description="Structured event payload")
    timestamp: float = Field(description="Unix timestamp in seconds")


class RecoverRunRequest(BaseModel):
    """Options of ``POST /runs/{run_id}/recover`` (A6 S9)."""

    model_config = ConfigDict(extra="forbid")
    on_uncertain: Literal["pause", "fail"] = Field(
        default="pause",
        description=(
            "What to do when a non_idempotent step dispatched but its outcome is unknown: "
            "pause the run (pause_reason effect_unknown) or fail it"
        ),
    )


class MigrateRunRequest(RecoverRunRequest):
    """Migrate to the run's saved workflow using trusted server registrations."""

    expected_definition_digest: str | None = None
    resume: bool = True


class RunResponse(BaseModel):
    """Execution run snapshot representation."""

    run_id: str = Field(description="Unique run identifier")
    workflow_id: str | None = Field(default=None, description="Associated workflow ID")
    spec_hash: str = Field(description="Canonical spec hash")
    workflow_version: str | int | None = None
    definition_digest: str = ""
    started_workflow_version: str | int | None = None
    origin_definition_digest: str = ""
    status: str = Field(
        description="Run status: running, pending_approval, waiting_for_input, completed, etc."
    )
    current_node_id: str | None = Field(default=None, description="Current executing node ID")
    step_count: int = Field(default=0, description="Number of executed steps")
    input_state: dict[str, Any] = Field(default_factory=dict, description="Initial inputs")
    current_state: dict[str, Any] = Field(default_factory=dict, description="Current state values")
    pending_approval: dict[str, Any] | None = Field(
        default=None, description="Pending approval details if gated"
    )
    pending_input: dict[str, Any] | None = Field(
        default=None,
        description="Pending interrupt (interrupt_id, node_id, value) while waiting_for_input",
    )
    forked_from: dict[str, Any] | None = Field(
        default=None, description="Source run and checkpoint if this run was forked"
    )
    error: str | None = Field(default=None, description="Error message if failed")
    cancel_reason: str | None = Field(
        default=None,
        description=(
            "Why the run was cancelled when access ended: access_revoked (member removed, "
            "API key revoked) or publication_revoked; null otherwise"
        ),
    )
    created_at: float = Field(description="Creation timestamp")
    updated_at: float = Field(description="Last update timestamp")
    events: list[RunEventModel] = Field(default_factory=list, description="Ordered event log")
    budget_status: dict[str, Any] | None = Field(
        default=None, description="Current budget expenditure and reservation status"
    )
    has_subworkflows: bool = Field(
        default=False, description="True when the run's spec has a sub-workflow node"
    )
    pause_reason: str | None = Field(
        default=None,
        description=(
            "effect_unknown while status is paused: a step's outcome is unknown after a crash "
            "(resolve pending_effect, then POST /runs/{run_id}/recover); null otherwise"
        ),
    )
    pending_effect: dict[str, Any] | None = Field(
        default=None, description="The effect a paused run waits on (effect_id, node_id, ...)"
    )


# ---------------------------------------------------------------------------
# F13: Evaluation, Triggers & Outbox Domain Models
# ---------------------------------------------------------------------------


class CreateDatasetRequest(BaseModel):
    """Payload to create or upload a new evaluation dataset."""

    dataset_id: str = Field(description="Unique dataset identifier")
    version: str = Field(default="1.0.0", description="Dataset semantic version")
    description: str = Field(default="", description="Dataset description")
    examples: list[dict[str, Any]] = Field(description="List of DatasetExample dictionaries")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Custom metadata tags")


class RunEvaluationRequest(BaseModel):
    """Payload to launch an evaluation run against a dataset."""

    dataset_id: str = Field(description="Evaluation dataset ID to benchmark against")
    dataset_version: str | None = Field(default=None, description="Optional dataset version")
    target_workflow_id: str | None = Field(default=None, description="Workflow ID to evaluate")
    target_module_id: str | None = Field(default=None, description="Module ID to evaluate")
    graders: list[str] = Field(
        default_factory=lambda: ["exact_match", "substring"],
        description="Grader names to apply: exact_match, substring, citation_overlap",
    )
    budget_max_usd: float | None = Field(default=None, description="Optional cost budget ceiling")


class CompareEvaluationsRequest(BaseModel):
    """Payload to compare two evaluation runs side by side."""

    run_id_a: str = Field(description="Baseline evaluation run ID")
    run_id_b: str = Field(description="Candidate evaluation run ID")


class CreateScheduleRequest(BaseModel):
    """Payload to register a new timezone-aware workflow execution schedule."""

    schedule_id: str = Field(description="Unique schedule identifier")
    workflow_id: str = Field(description="Target workflow ID to execute on trigger")
    release_id: str = Field(default="current", description="Pinned release or current revision")
    cron_expr: str | None = Field(default=None, description="Standard 5-field cron expression")
    interval_seconds: float | None = Field(default=None, description="Repeat interval in seconds")
    timezone: str = Field(default="UTC", description="IANA timezone (e.g. Asia/Kolkata, UTC)")
    missed_run_policy: Literal["fire_once", "fire_all", "skip"] = Field(
        default="fire_once", description="Policy for handling missed fires upon restart"
    )
    payload: dict[str, Any] = Field(default_factory=dict, description="Initial state payload")


class RegisterWebhookRequest(BaseModel):
    """Payload to configure a signed webhook endpoint for workflow triggering."""

    endpoint_id: str = Field(description="Unique webhook endpoint slug")
    workflow_id: str = Field(description="Target workflow ID to trigger")
    release_id: str = Field(default="current", description="Pinned release or current revision")
    secret: str | None = Field(
        default=None, description="Shared HMAC secret (auto-generated if empty)"
    )
    replay_window_seconds: int = Field(
        default=300, ge=10, le=86400, description="Allowed clock skew window"
    )


class TestWebhookRequest(BaseModel):
    """Payload to simulate a signed webhook delivery event."""

    payload: dict[str, Any] = Field(default_factory=dict, description="Event body dictionary")
    event_id: str | None = Field(
        default=None, description="Custom event ID (for deduplication tests)"
    )
    timestamp: float | None = Field(default=None, description="Custom timestamp override")
    invalid_signature: bool = Field(default=False, description="Simulate corrupted signature")
    expired_timestamp: bool = Field(default=False, description="Simulate replay window expiry")


class TestDispatchOutboxRequest(BaseModel):
    """Payload to test an outbound channel action into the observable dry-run outbox."""

    channel: Literal["email", "whatsapp"] = Field(description="Destination channel")
    recipient: str = Field(description="Target email address or E.164 phone number")
    subject_or_template: str = Field(description="Subject line or WhatsApp template name")
    body: str = Field(description="Message body text or template parameters")
    dedup_key: str | None = Field(
        default=None, description="Optional idempotency key for deduplication"
    )
