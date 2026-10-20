// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import type { Node, Edge } from '@xyflow/react';
import type { WorkflowSpec } from '@liceron/nexura-sdk';

export interface BuilderNodeData extends Record<string, unknown> {
  nodeId: string;
  label: string;
  handler: string;
  module_id?: string;
  module_version?: string;
  requires_approval: boolean;
  config: Record<string, unknown>;
  input_mapping: Record<string, string>;
  output_mapping: Record<string, string>;
  isEntry: boolean;
  validationError?: string;
  executionStatus?: 'idle' | 'running' | 'completed' | 'failed' | 'paused';
  isActiveReplayNode?: boolean;
  metadata?: Record<string, unknown>;
  extraFields?: Record<string, unknown>;
  // Composite step fields, exactly as in the workflow spec (undefined = absent).
  /** Handler names that run concurrently on the same state, then join. */
  parallel?: string[];
  /** State key holding the list a step runs once per item of. */
  map_over?: string;
  /** State key the current item is exposed under (spec default "item"). */
  map_as?: string;
  /** Concurrent items/branches limit, 1..100 (spec default 10). */
  max_concurrency?: number;
  /** State key -> reducer name, combining branch/item results. */
  reducers?: Record<string, string>;
  /** Nested workflow run as this step (a copy of a saved workflow's spec). */
  subgraph?: WorkflowSpec;
  /** Editor-only: a sub-workflow step whose workflow is not chosen yet. Never serialized. */
  pendingKind?: 'subworkflow';
}

export interface BuilderEdgeData extends Record<string, unknown> {
  branch?: string;
  isDefault?: boolean;
}

export type BuilderNode = Node<BuilderNodeData>;
export type BuilderEdge = Edge<BuilderEdgeData>;

export interface GraphDocument {
  nodes: BuilderNode[];
  edges: BuilderEdge[];
  entryNode: string;
  viewport?: { x: number; y: number; zoom: number };
}

export interface HistorySnapshot {
  nodes: BuilderNode[];
  edges: BuilderEdge[];
  entryNode: string;
}
