// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import type { RunEvent } from '@liceron/nexura-sdk';
import type { NodeExecutionStatus, ReplayStep } from './types';

/**
 * Merges and deduplicates run lifecycle events, guaranteeing strict monotonic sequence ordering.
 * Enforces zero duplicate counting across reconnects, replay cursors, and concurrent event sources.
 */
export function deduplicateEvents(
  existing: RunEvent[],
  incoming: RunEvent[],
  maxRetained: number = 1000
): RunEvent[] {
  const eventMap = new Map<number, RunEvent>();

  // Add existing
  for (const ev of existing) {
    if (ev && typeof ev.seq === 'number') {
      eventMap.set(ev.seq, ev);
    }
  }

  // Add incoming (incoming with same seq overwrites/refreshes cleanly)
  for (const ev of incoming) {
    if (ev && typeof ev.seq === 'number') {
      eventMap.set(ev.seq, ev);
    }
  }

  // Return sorted ascending by seq, bounded by maxRetained to prevent memory leaks
  const sorted = Array.from(eventMap.values()).sort((a, b) => a.seq - b.seq);
  if (maxRetained > 0 && sorted.length > maxRetained) {
    return sorted.slice(-maxRetained);
  }
  return sorted;
}

/**
 * Extracts latest execution status for each workflow node from ordered event sequence.
 */
export function extractNodeExecutionStatus(
  events: RunEvent[],
  currentNodeId?: string | null
): Map<string, NodeExecutionStatus> {
  const statuses = new Map<string, NodeExecutionStatus>();

  for (const ev of events) {
    const nodeId = ev.node_id || (ev.data && (ev.data.node_id as string));
    if (!nodeId) continue;

    switch (ev.type) {
      case 'NODE_STARTED':
        statuses.set(nodeId, 'running');
        break;
      case 'NODE_COMPLETED':
        statuses.set(nodeId, 'completed');
        break;
      case 'NODE_FAILED':
        statuses.set(nodeId, 'failed');
        break;
      case 'RUN_PENDING_APPROVAL':
        statuses.set(nodeId, 'paused');
        break;
    }
  }

  // If a current_node_id is specified and currently running without terminal event
  if (currentNodeId && !statuses.has(currentNodeId)) {
    statuses.set(currentNodeId, 'running');
  }

  return statuses;
}

/**
 * Reconstructs state evolution across time from sequential events.
 * Computes step-by-step state snapshot and differential delta for replay.
 */
export function computeStateReplaySteps(
  initialState: Record<string, unknown>,
  events: RunEvent[]
): ReplayStep[] {
  const steps: ReplayStep[] = [];
  let currentState: Record<string, unknown> = { ...initialState };

  // Step 0: Initial State
  steps.push({
    stepIndex: 0,
    seq: 0,
    nodeId: null,
    eventType: 'RUN_INITIALIZED',
    state: { ...currentState },
    timestamp: events.length > 0 ? events[0].timestamp : Date.now() / 1000,
  });

  let stepCounter = 1;
  for (const ev of events) {
    const nodeId = ev.node_id || (ev.data && (ev.data.node_id as string)) || null;

    if (ev.type === 'NODE_COMPLETED') {
      const update = (ev.data && (ev.data.state_update as Record<string, unknown>)) || {};
      currentState = { ...currentState, ...update };
      steps.push({
        stepIndex: stepCounter++,
        seq: ev.seq,
        nodeId,
        eventType: ev.type,
        state: { ...currentState },
        delta: update,
        timestamp: ev.timestamp,
      });
    } else if (ev.type === 'APPROVAL_GRANTED') {
      const update = (ev.data && (ev.data.state_update as Record<string, unknown>)) || {};
      currentState = { ...currentState, ...update };
      steps.push({
        stepIndex: stepCounter++,
        seq: ev.seq,
        nodeId,
        eventType: ev.type,
        state: { ...currentState },
        delta: update,
        timestamp: ev.timestamp,
      });
    } else if (ev.type === 'NODE_FAILED') {
      const errorMsg = (ev.data && (ev.data.error as string)) || 'Node execution failed';
      steps.push({
        stepIndex: stepCounter++,
        seq: ev.seq,
        nodeId,
        eventType: ev.type,
        state: { ...currentState },
        error: errorMsg,
        timestamp: ev.timestamp,
      });
    } else if (ev.type === 'RUN_COMPLETED') {
      const finalState = (ev.data && (ev.data.final_state as Record<string, unknown>)) || currentState;
      currentState = { ...currentState, ...finalState };
      steps.push({
        stepIndex: stepCounter++,
        seq: ev.seq,
        nodeId,
        eventType: ev.type,
        state: { ...currentState },
        timestamp: ev.timestamp,
      });
    } else if (ev.type === 'RUN_CANCELLED' || ev.type === 'RUN_FAILED') {
      steps.push({
        stepIndex: stepCounter++,
        seq: ev.seq,
        nodeId,
        eventType: ev.type,
        state: { ...currentState },
        error: (ev.data && (ev.data.reason || ev.data.error)) as string,
        timestamp: ev.timestamp,
      });
    }
  }

  return steps;
}

/**
 * Returns human-readable label and description for run events.
 */
export function formatEventTitle(type: string): string {
  switch (type) {
    case 'NODE_STARTED':
      return 'Node Started';
    case 'NODE_COMPLETED':
      return 'Node Completed';
    case 'NODE_FAILED':
      return 'Node Execution Failed';
    case 'RUN_PENDING_APPROVAL':
      return 'Approval Gate Paused';
    case 'APPROVAL_GRANTED':
      return 'Approval Decision Granted';
    case 'APPROVAL_REJECTED':
      return 'Approval Decision Rejected';
    case 'RUN_COMPLETED':
      return 'Workflow Run Completed';
    case 'RUN_FAILED':
      return 'Workflow Run Failed';
    case 'RUN_CANCELLED':
      return 'Workflow Run Cancelled';
    case 'RUN_INTERRUPTED':
      return 'Workflow Run Interrupted';
    case 'AGENT_EVENT':
      return 'Agent Event';
    default:
      return type.replace(/_/g, ' ');
  }
}
