import { describe, it, expect } from 'vitest';
import type { SubRunDetail } from '@liceron/nexura-sdk';
import { describeSubRun, showSubRunsTab } from '../../src/features/runs/durable';

const DETAIL: SubRunDetail = {
  child_key: 'c3ViIzE',
  run_id: 'run-1/sub#1',
  parent_run_id: 'run-1',
  status: 'completed',
  step_count: 2,
  current_node_id: null,
  error: null,
  steps: [
    { index: 0, step_count: 0, node_id: 'c1', status: 'running', updated_at: 1700000000 },
    { index: 1, step_count: 2, node_id: null, status: 'completed', updated_at: 1700000001 },
  ],
  history: { items: [] },
};

describe('showSubRunsTab', () => {
  it('is true only when the server says so', () => {
    expect(showSubRunsTab({ has_subworkflows: true })).toBe(true);
    expect(showSubRunsTab({ has_subworkflows: false })).toBe(false);
    expect(showSubRunsTab({})).toBe(false);
    expect(showSubRunsTab(null)).toBe(false);
  });
});

describe('describeSubRun', () => {
  it('labels the child, maps steps and shows end for a finished node', () => {
    const v = describeSubRun(DETAIL);
    expect(v.label).toBe('sub#1');
    expect(v.status).toBe('completed');
    expect(v.currentNode).toBe('end');
    expect(v.error).toBeNull();
    expect(v.steps.map((s) => [s.key, s.node, s.status, s.step])).toEqual([
      [0, 'c1', 'running', 0],
      [1, 'end', 'completed', 2],
    ]);
  });
  it('carries a failure message', () => {
    expect(describeSubRun({ ...DETAIL, status: 'failed', error: 'boom' }).error).toBe('boom');
  });
});
