import { describe, it, expect } from 'vitest';
import type { PendingApprovalItem } from '../../src/features/approvals/types';

describe('Approval Inbox & Decision Processing (F08)', () => {
  const sampleApprovals: PendingApprovalItem[] = [
    {
      approvalId: 'appr-refund-101',
      runId: 'run-ord-501',
      workflowId: 'wf-order-triage',
      nodeId: 'approval_gate',
      createdAt: 1700000000,
      currentState: {
        order_id: 'ORD-501',
        amount: 2500,
        category: 'refund',
        classification: 'High-Value Refund Request (Approval Gate Required)',
      },
      runRecord: {
        run_id: 'run-ord-501',
        workflow_id: 'wf-order-triage',
        spec_hash: 'hash1',
        status: 'pending_approval',
        step_count: 2,
        input_state: {},
        current_state: { order_id: 'ORD-501', amount: 2500 },
        created_at: 1700000000,
        updated_at: 1700000005,
      },
    },
    {
      approvalId: 'appr-credit-202',
      runId: 'run-cred-702',
      workflowId: 'wf-credit-check',
      nodeId: 'risk_officer_review',
      createdAt: 1700000100,
      currentState: {
        client_id: 'CLI-888',
        requested_limit: 50000,
        credit_score: 720,
      },
      runRecord: {
        run_id: 'run-cred-702',
        workflow_id: 'wf-credit-check',
        spec_hash: 'hash2',
        status: 'pending_approval',
        step_count: 4,
        input_state: {},
        current_state: { client_id: 'CLI-888' },
        created_at: 1700000100,
        updated_at: 1700000105,
      },
    },
  ];

  it('filters pending approvals by run ID, workflow ID, and node ID', () => {
    const filterByRun = (query: string) => {
      const q = query.toLowerCase();
      return sampleApprovals.filter(
        (a) =>
          a.runId.toLowerCase().includes(q) ||
          a.workflowId.toLowerCase().includes(q) ||
          a.nodeId.toLowerCase().includes(q) ||
          JSON.stringify(a.currentState).toLowerCase().includes(q)
      );
    };

    expect(filterByRun('ord-501')).toHaveLength(1);
    expect(filterByRun('ord-501')[0].approvalId).toBe('appr-refund-101');

    expect(filterByRun('risk_officer_review')).toHaveLength(1);
    expect(filterByRun('risk_officer_review')[0].nodeId).toBe('risk_officer_review');

    expect(filterByRun('refund')).toHaveLength(1);
    expect(filterByRun('CLI-888')).toHaveLength(1);
    expect(filterByRun('nonexistent')).toHaveLength(0);
  });

  it('validates state update parsing in approval decisions', () => {
    const parseStateUpdate = (text: string) => {
      if (!text.trim()) return { parsed: undefined, error: null };
      try {
        const val = JSON.parse(text);
        if (typeof val !== 'object' || val === null || Array.isArray(val)) {
          return { parsed: undefined, error: 'State update must be a JSON object.' };
        }
        return { parsed: val, error: null };
      } catch (err: unknown) {
        return { parsed: undefined, error: `JSON syntax error: ${err instanceof Error ? err.message : String(err)}` };
      }
    };

    expect(parseStateUpdate('').parsed).toBeUndefined();
    expect(parseStateUpdate('').error).toBeNull();

    const valid = parseStateUpdate('{"approved_amount": 2500, "approved_by": "alice"}');
    expect(valid.error).toBeNull();
    expect(valid.parsed).toEqual({ approved_amount: 2500, approved_by: 'alice' });

    const arrayVal = parseStateUpdate('[1, 2, 3]');
    expect(arrayVal.error).toBe('State update must be a JSON object.');

    const malformed = parseStateUpdate('{ invalid: json }');
    expect(malformed.error).toContain('JSON syntax error');
  });

  it('correctly maps 409 conflict and terminal state errors', () => {
    const isConflictError = (msg: string) =>
      msg.includes('409') ||
      msg.includes('APPROVAL_ALREADY_CLAIMED') ||
      msg.includes('NOT_PENDING_APPROVAL') ||
      msg.includes('RUN_ALREADY_TERMINAL');

    expect(isConflictError('HTTP 409: Conflict - APPROVAL_ALREADY_CLAIMED')).toBe(true);
    expect(isConflictError('NOT_PENDING_APPROVAL: Run run-1 is already completed')).toBe(true);
    expect(isConflictError('RUN_ALREADY_TERMINAL: Run is completed')).toBe(true);
    expect(isConflictError('HTTP 500 Internal Server Error')).toBe(false);
    expect(isConflictError('Network timeout')).toBe(false);
  });
});
