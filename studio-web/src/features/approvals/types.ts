// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import type { PendingApproval, RunRecord, WorkflowSpec } from '@liceron/nexura-sdk';

/** Optional wire fields: old Studio servers do not expose the B43 request identity. */
export interface ExactApprovalRequest extends Partial<PendingApproval> {
  run_id?: string;
  workflow_run_id?: string;
  handler?: string;
  action_kind?: string;
  action_name?: string;
  action_payload?: unknown;
  canonical_payload_json?: string;
  payload_digest?: string;
  spec_digest?: string;
  expires_at?: number | null;
  status?: string;
  input_state?: Record<string, unknown>;
  /** Set by the server when this request replaces one whose inputs were edited on resume. */
  supersedes_approval_id?: string | null;
  regate_reason?: string | null;
  previous_input_state?: Record<string, unknown> | null;
}

export interface PendingApprovalItem {
  approvalId: string;
  runId: string;
  workflowId: string;
  nodeId: string;
  createdAt: number;
  currentState: Record<string, unknown>;
  spec?: WorkflowSpec;
  runRecord: RunRecord;
  previousApproval?: ExactApprovalRequest;
}

export type DecisionType = 'approve' | 'reject';

export interface DecisionResult {
  success: boolean;
  run?: RunRecord;
  error?: string;
  conflict?: boolean;
}
