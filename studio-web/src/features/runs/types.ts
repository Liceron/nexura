// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Liceron
import type { RunRecord, RunEvent, WorkflowSpec } from '@liceron/nexura-sdk';

export type NodeExecutionStatus = 'idle' | 'running' | 'completed' | 'failed' | 'paused';

export type StreamStatus = 'connecting' | 'connected' | 'reconnecting' | 'closed' | 'error';

export interface ReplayStep {
  stepIndex: number;
  seq: number;
  nodeId?: string | null;
  eventType: string;
  state: Record<string, unknown>;
  delta?: Record<string, unknown>;
  timestamp: number;
  error?: string;
}

export interface RunStateSummary {
  run: RunRecord | null;
  events: RunEvent[];
  streamStatus: StreamStatus;
  maxSeq: number;
  loading: boolean;
  error: string | null;
  nodeStatuses: Map<string, NodeExecutionStatus>;
  replaySteps: ReplayStep[];
  activeNodeId?: string | null;
}

export interface RunStartConfig {
  initialState: Record<string, unknown>;
  asyncExecution: boolean;
  spec?: WorkflowSpec;
}
