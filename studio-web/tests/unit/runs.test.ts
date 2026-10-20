import { describe, it, expect } from 'vitest';
import {
  deduplicateEvents,
  extractNodeExecutionStatus,
  computeStateReplaySteps,
  formatEventTitle,
} from '../../src/features/runs/events';
import type { RunEvent } from '@liceron/nexura-sdk';

describe('Run Stream & Event Processing (F07)', () => {
  it('monotonically sorts and deduplicates events across reconnects and replay', () => {
    const existingEvents: RunEvent[] = [
      { seq: 1, type: 'RUN_STARTED', data: { workflow_id: 'wf-1' }, timestamp: 1001 },
      { seq: 2, type: 'NODE_STARTED', data: { node_id: 'step_1' }, timestamp: 1002 },
    ];

    const incomingEvents: RunEvent[] = [
      { seq: 2, type: 'NODE_STARTED', data: { node_id: 'step_1' }, timestamp: 1002 }, // Duplicate seq across reconnect
      { seq: 3, type: 'NODE_COMPLETED', data: { node_id: 'step_1' }, timestamp: 1003 },
      { seq: 4, type: 'RUN_COMPLETED', data: { status: 'completed' }, timestamp: 1004 },
    ];

    const deduplicated = deduplicateEvents(existingEvents, incomingEvents);

    expect(deduplicated).toHaveLength(4);
    expect(deduplicated.map((e) => e.seq)).toEqual([1, 2, 3, 4]);
    expect(deduplicated[0].type).toBe('RUN_STARTED');
    expect(deduplicated[1].type).toBe('NODE_STARTED');
    expect(deduplicated[2].type).toBe('NODE_COMPLETED');
    expect(deduplicated[3].type).toBe('RUN_COMPLETED');
  });

  it('correctly maps run events to node execution statuses', () => {
    const events: RunEvent[] = [
      { seq: 1, type: 'RUN_STARTED', data: {}, timestamp: 1001 },
      { seq: 2, type: 'NODE_STARTED', data: { node_id: 'node_a' }, timestamp: 1002 },
      { seq: 3, type: 'NODE_COMPLETED', data: { node_id: 'node_a' }, timestamp: 1003 },
      { seq: 4, type: 'NODE_STARTED', data: { node_id: 'node_b' }, timestamp: 1004 },
      { seq: 5, type: 'RUN_PENDING_APPROVAL', data: { node_id: 'node_c' }, timestamp: 1005 },
      { seq: 6, type: 'NODE_FAILED', data: { node_id: 'node_d', error: 'boom' }, timestamp: 1006 },
    ];

    const statuses = extractNodeExecutionStatus(events);

    expect(statuses.get('node_a')).toBe('completed');
    expect(statuses.get('node_b')).toBe('running');
    expect(statuses.get('node_c')).toBe('paused');
    expect(statuses.get('node_d')).toBe('failed');
    expect(statuses.get('node_unknown')).toBeUndefined();
  });

  it('computes step-by-step state snapshots and transition deltas for replay scrubber', () => {
    const initialState = { order_id: 'ORD-100', status: 'new' };

    const events: RunEvent[] = [
      {
        seq: 1,
        type: 'RUN_STARTED',
        data: {},
        timestamp: 1001,
      },
      {
        seq: 2,
        type: 'NODE_STARTED',
        data: { node_id: 'validate_node' },
        timestamp: 1002,
      },
      {
        seq: 3,
        type: 'NODE_COMPLETED',
        data: {
          node_id: 'validate_node',
          state_update: { verified: true },
        },
        timestamp: 1003,
      },
      {
        seq: 4,
        type: 'NODE_STARTED',
        data: { node_id: 'score_node' },
        timestamp: 1004,
      },
      {
        seq: 5,
        type: 'NODE_COMPLETED',
        data: {
          node_id: 'score_node',
          state_update: { score: 98, status: 'delivered' },
        },
        timestamp: 1005,
      },
    ];

    const steps = computeStateReplaySteps(initialState, events);

    // Initial State (Step 0) + 2 NODE_COMPLETED events
    expect(steps.length).toBe(3);
    expect(steps[0].eventType).toBe('RUN_INITIALIZED');
    expect(steps[0].state).toEqual({ order_id: 'ORD-100', status: 'new' });

    // Step 1: after validate_node completes
    expect(steps[1].nodeId).toBe('validate_node');
    expect(steps[1].delta).toEqual({ verified: true });
    expect(steps[1].state).toEqual({
      order_id: 'ORD-100',
      status: 'new',
      verified: true,
    });

    // Step 2: after score_node completes
    expect(steps[2].nodeId).toBe('score_node');
    expect(steps[2].delta).toEqual({ score: 98, status: 'delivered' });
    expect(steps[2].state).toEqual({
      order_id: 'ORD-100',
      status: 'delivered',
      verified: true,
      score: 98,
    });
  });

  it('formats human-readable event titles with node identifiers', () => {
    expect(formatEventTitle('RUN_STARTED')).toBe('RUN STARTED');
    expect(formatEventTitle('NODE_STARTED')).toBe('Node Started');
    expect(formatEventTitle('NODE_COMPLETED')).toBe('Node Completed');
    expect(formatEventTitle('RUN_PENDING_APPROVAL')).toBe('Approval Gate Paused');
    expect(formatEventTitle('RUN_CANCELLED')).toBe('Workflow Run Cancelled');
  });
});
