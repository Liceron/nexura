// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import type { RunRecord } from '@liceron/nexura-sdk';
import { ApprovalDetailsPanel } from '../../src/features/approvals/ApprovalDetailsPanel';
import { ApprovalDecisionModal } from '../../src/features/approvals/ApprovalDecisionModal';
import { payloadJson } from '../../src/features/approvals/ExactAction';
import type { ExactApprovalRequest, PendingApprovalItem } from '../../src/features/approvals/types';
import { I18nProvider } from '../../src/i18n';

const digest = '0123456789abcdef'.repeat(4);
const request: ExactApprovalRequest = {
  approval_id: 'approval-original', run_id: 'run-exact', node_id: 'node-exact',
  action_kind: 'tool_call', action_name: 'refund', spec_digest: 'spec-'.repeat(15),
  action_payload: { '*args': [1, '<script>alert(1)</script>'], amount: 12, nested: { z: 2, a: null } },
  canonical_payload_json: '{"*args":[1,"<script>alert(1)</script>"],"amount":12,"nested":{"a":null,"z":2}}',
  payload_digest: digest, expires_at: 2_000_000_000, input_state: { amount: 12 },
};

function item(pending = request): PendingApprovalItem {
  return {
    approvalId: pending.approval_id!, runId: 'run-exact', nodeId: 'node-exact', workflowId: 'workflow',
    createdAt: 1, currentState: { amount: 12 },
    runRecord: {
      run_id: 'run-exact', status: 'pending_approval', spec_hash: 'h', step_count: 1,
      input_state: {}, current_state: { amount: 12 }, created_at: 1, updated_at: 1,
      pending_approval: pending,
    } as RunRecord,
  };
}

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
  localStorage.removeItem('nexura_locale');
});

describe('P32c exact action', () => {
  it.each([
    [undefined, 'refund_gate'],
    ['support/refund', 'refund_gate (support/refund)'],
  ])('describes workflow nodes without action metadata (handler: %s)', (handler, name) => {
    render(<ApprovalDetailsPanel pendingApproval={{
      approval_id: 'workflow-approval', workflow_run_id: 'workflow-run', node_id: 'refund_gate', handler,
      payload_digest: digest,
    }} />);
    expect(screen.getByTestId('exact-action')).toHaveTextContent('Action kind: Workflow step');
    expect(screen.getByTestId('exact-action')).toHaveTextContent(`Action name: ${name}`);
    expect(screen.getByTitle(digest)).toBeInTheDocument();
    expect(screen.getByTestId('exact-action')).toHaveTextContent('Agent spec digest: not recorded');
    expect(screen.getByTestId('exact-action')).toHaveTextContent('Arguments (positional and named)not recorded');
    expect(screen.queryByRole('button', { name: 'Copy Agent spec digest' })).not.toBeInTheDocument();
  });

  it('uses a workflow handler from details when supplied and preserves absent digests', () => {
    render(<ApprovalDetailsPanel pendingApproval={{
      workflow_run_id: 'workflow-run', node_id: 'gate', details: { handler: 'review' },
    }} />);
    expect(screen.getByTestId('exact-action')).toHaveTextContent('Action name: gate (review)');
    expect(screen.getByTestId('exact-action')).toHaveTextContent('Payload digest: not recorded');
    expect(screen.queryByRole('button', { name: /Copy/ })).not.toBeInTheDocument();
  });

  it('includes a handler from the captured workflow spec in the approval dialog', () => {
    const pendingItem = item({ workflow_run_id: 'workflow-run', node_id: 'node-exact', approval_id: 'workflow-approval' });
    pendingItem.spec = { name: 'Workflow', entry_node: 'node-exact', nodes: {
      'node-exact': { id: 'node-exact', handler: 'support/refund' },
    } };
    render(<ApprovalDecisionModal open item={pendingItem} onClose={vi.fn()} onDecide={vi.fn()} />);
    expect(screen.getByTestId('exact-action')).toHaveTextContent('Action name: node-exact (support/refund)');
    expect(screen.getByTestId('exact-action')).toHaveTextContent('Payload digest: not recorded');
    expect(pendingItem.runRecord.pending_approval).not.toHaveProperty('handler');
  });

  it('preserves explicit action metadata even when workflow identifiers are present', () => {
    render(<ApprovalDetailsPanel pendingApproval={{ ...request, workflow_run_id: 'workflow-run', handler: 'other' }} />);
    expect(screen.getByTestId('exact-action')).toHaveTextContent('Action kind: tool_call');
    expect(screen.getByTestId('exact-action')).toHaveTextContent('Action name: refund');
    expect(screen.getByTestId('exact-action')).not.toHaveTextContent('Workflow step');
    expect(screen.getByTestId('exact-action')).not.toHaveTextContent('(other)');
  });

  it('does not infer workflow identity from an incomplete or non-workflow request', () => {
    const { rerender } = render(<ApprovalDetailsPanel pendingApproval={{ node_id: 'node-only' }} />);
    expect(screen.getByTestId('exact-action')).toHaveTextContent('Action kind: not recorded');
    rerender(<ApprovalDetailsPanel pendingApproval={{ workflow_run_id: 'run-only' }} />);
    expect(screen.getByTestId('exact-action')).toHaveTextContent('Action name: not recorded');
  });

  it('localizes workflow step identity in Hindi', () => {
    localStorage.setItem('nexura_locale', 'hi');
    render(<I18nProvider><ApprovalDetailsPanel pendingApproval={{ workflow_run_id: 'run', node_id: 'gate' }} /></I18nProvider>);
    expect(screen.getByTestId('exact-action')).toHaveTextContent('वर्कफ़्लो चरण');
    expect(screen.getByTestId('exact-action')).toHaveTextContent('कार्रवाई का नाम: gate');
  });

  it('wraps long paused-state lines and uses an SVG for the pending badge', () => {
    const pendingItem = item();
    const long = 'अस्वीकृत-'.repeat(150);
    pendingItem.currentState = { classification: long };
    render(<ApprovalDecisionModal open item={pendingItem} onClose={vi.fn()} onDecide={vi.fn()} />);
    expect(screen.getByTestId('approval-paused-state').textContent).toBe(JSON.stringify(pendingItem.currentState, null, 2));
    expect(screen.getByTestId('approval-paused-state')).toHaveStyle({ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' });
    const badge = screen.getByText('Pending Approval');
    expect(badge.querySelector('svg')).toHaveAttribute('aria-hidden', 'true');
    expect(badge.textContent).not.toMatch(/\p{Extended_Pictographic}/u);
  });

  it('shows identity, all canonical arguments, digests and absolute local expiry as text', () => {
    const { container } = render(<ApprovalDetailsPanel pendingApproval={request} />);
    expect(screen.getByTestId('exact-action')).toHaveTextContent('tool_call');
    for (const value of ['refund', 'run-exact', 'node-exact']) expect(screen.getByText(value)).toBeInTheDocument();
    expect(container.querySelector('pre')?.textContent).toBe(request.canonical_payload_json);
    expect(container.querySelector('script')).toBeNull();
    expect(screen.getByTitle(digest)).toHaveTextContent('0123456789ab…cdef');
    expect(screen.getByTitle(request.spec_digest!)).toBeInTheDocument();
    expect(container.querySelector('time')?.textContent).toBe(new Date(request.expires_at! * 1000).toLocaleString('en-IN'));
  });

  it('copies the full digest rather than the shortened display', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });
    render(<ApprovalDetailsPanel pendingApproval={request} />);
    expect(screen.getByRole('button', { name: 'Copy Payload digest' })).toHaveStyle({
      color: 'var(--text-primary)', backgroundColor: 'var(--bg-surface-elevated)', border: '1px solid var(--border-subtle)',
    });
    fireEvent.click(screen.getByRole('button', { name: 'Copy Payload digest' }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(digest));
    expect(await screen.findByText('Copied')).toBeInTheDocument();
  });

  it('reports clipboard failures without throwing', async () => {
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: vi.fn().mockRejectedValue(new Error('denied')) } });
    render(<ApprovalDetailsPanel pendingApproval={request} />);
    fireEvent.click(screen.getByRole('button', { name: 'Copy Payload digest' }));
    expect(await screen.findByText('Copy failed')).toBeInTheDocument();
  });

  it('shows not recorded for missing identity, arguments, digests and expiry without using workflow state', () => {
    render(<ApprovalDecisionModal open item={item({ approval_id: 'legacy' })} onClose={vi.fn()} onDecide={vi.fn()} />);
    expect(screen.getByTestId('exact-action').querySelectorAll('code')).toHaveLength(4);
    for (const code of screen.getByTestId('exact-action').querySelectorAll('code')) expect(code).toHaveTextContent('not recorded');
    expect(screen.getByTestId('exact-action').textContent?.match(/not recorded/g)?.length).toBeGreaterThanOrEqual(8);
    expect(screen.getByTestId('exact-action')).not.toHaveTextContent('"amount":12');
  });

  it('disables Approve for an expired request and explains why', () => {
    const decide = vi.fn();
    render(<ApprovalDecisionModal open item={item({ ...request, expires_at: 1 })} onClose={vi.fn()} onDecide={decide} />);
    expect(screen.getByRole('button', { name: 'Approve & Resume' })).toBeDisabled();
    expect(screen.getByRole('alert')).toHaveTextContent('This request has expired');
    fireEvent.click(screen.getByRole('button', { name: 'Approve & Resume' }));
    expect(decide).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: 'Reject Execution' })).toBeEnabled();
  });

  it('ticks the countdown and disables approval on expiry, then clears timers on unmount', () => {
    vi.useFakeTimers();
    vi.setSystemTime(1_900_000_000_000);
    const { unmount } = render(<ApprovalDecisionModal open item={item({ ...request, expires_at: 1_900_000_002 })} onClose={vi.fn()} onDecide={vi.fn()} />);
    expect(screen.getByTestId('exact-action')).toHaveTextContent('0:00:02');
    act(() => vi.advanceTimersByTime(1000));
    expect(screen.getByTestId('exact-action')).toHaveTextContent('0:00:01');
    act(() => vi.advanceTimersByTime(1000));
    expect(screen.getByRole('button', { name: 'Approve & Resume' })).toBeDisabled();
    unmount();
    expect(vi.getTimerCount()).toBe(0);
  });

  it('honors an explicitly expired server status even without a timestamp', () => {
    render(<ApprovalDecisionModal open item={item({ ...request, expires_at: undefined, status: 'expired' })} onClose={vi.fn()} onDecide={vi.fn()} />);
    expect(screen.getByRole('button', { name: 'Approve & Resume' })).toBeDisabled();
  });

  it('keeps a re-gated request open with previous and new API inputs and submits its new id', async () => {
    const close = vi.fn();
    const fresh = { ...request, approval_id: 'approval-fresh', input_state: { amount: 4 }, action_payload: { amount: 4 }, canonical_payload_json: '{"amount":4}' };
    const decide = vi.fn().mockResolvedValueOnce({ success: true, run: item(fresh).runRecord }).mockResolvedValueOnce({ success: true });
    render(<ApprovalDecisionModal open item={item()} onClose={close} onDecide={decide} />);
    fireEvent.change(screen.getByLabelText('Optional State Adjustments (Applied on Approval)'), { target: { value: '{"amount":4}' } });
    fireEvent.click(screen.getByRole('button', { name: 'Approve & Resume' }));
    expect(await screen.findByTestId('approval-edited-inputs')).toHaveTextContent('new request for edited inputs');
    expect(close).not.toHaveBeenCalled();
    expect(screen.getByText('{"amount":12}')).toBeInTheDocument();
    expect(screen.getAllByText('{"amount":4}')).toHaveLength(2);
    expect(screen.getByLabelText('Optional State Adjustments (Applied on Approval)')).toHaveValue('');
    await waitFor(() => expect(screen.getByRole('button', { name: 'Approve & Resume' })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: 'Approve & Resume' }));
    await waitFor(() => expect(decide).toHaveBeenLastCalledWith('run-exact', 'approval-fresh', 'approve', undefined));
  });

  it('shows the edited-input note with missing historical values without inventing them', () => {
    render(<ApprovalDetailsPanel pendingApproval={{ approval_id: 'fresh' }} previousApproval={{ approval_id: 'old' }} />);
    expect(screen.getByTestId('approval-edited-inputs')).toHaveTextContent('new request for edited inputs');
    expect(screen.getByTestId('approval-edited-inputs').querySelectorAll('pre')).toHaveLength(0);
  });

  it('makes long argument values collapsible without truncating their text', () => {
    const full = JSON.stringify({ text: 'long'.repeat(150) });
    const { container } = render(<ApprovalDetailsPanel pendingApproval={{ canonical_payload_json: full }} />);
    const details = container.querySelector('details')!;
    expect(details.open).toBe(false);
    expect(details.querySelector('pre')?.textContent).toBe(full);
    fireEvent.click(screen.getByText(/Expand JSON/));
    expect(details.open).toBe(true);
  });

  it('sorts available JSON without losing integer-like or prototype keys and flags missing server canonical text', () => {
    expect(payloadJson(JSON.parse('{"2":2,"10":10,"__proto__":{"b":2,"a":1}}'))).toBe('{"10":10,"2":2,"__proto__":{"a":1,"b":2}}');
    render(<ApprovalDetailsPanel pendingApproval={{ action_payload: { a: null, b: [] } }} />);
    expect(screen.getByText('{"a":null,"b":[]}')).toBeInTheDocument();
    expect(screen.getByText(/Server canonical JSON: not recorded/)).toBeInTheDocument();
  });

  it('renders the new review strings through Hindi i18n', () => {
    localStorage.setItem('nexura_locale', 'hi');
    render(<I18nProvider><ApprovalDecisionModal open item={item({ approval_id: 'old', expires_at: 1 })} onClose={vi.fn()} onDecide={vi.fn()} /></I18nProvider>);
    expect(screen.getByText('सटीक कार्रवाई')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'अनुमोदन करें और फिर शुरू करें' })).toBeDisabled();
    expect(screen.getByRole('alert')).toHaveTextContent('समय सीमा समाप्त');
  });
});

describe('P32c server-recorded edit provenance', () => {
  it('shows the replaced request and both inputs from the server, without a session snapshot', () => {
    const fresh: ExactApprovalRequest = {
      ...request,
      approval_id: 'approval-fresh',
      input_state: { amount: 999 },
      supersedes_approval_id: 'approval-original',
      regate_reason: 'inputs_edited',
      previous_input_state: { amount: 12 },
    };
    render(<I18nProvider><ApprovalDetailsPanel pendingApproval={fresh} /></I18nProvider>);
    const note = screen.getByTestId('approval-edited-inputs');
    expect(note.textContent).toContain('approval-original');
    expect(note.textContent).toContain('"amount":12');
    expect(note.textContent).toContain('"amount":999');
  });

  it('shows no edit note when the server records none', () => {
    render(<I18nProvider><ApprovalDetailsPanel pendingApproval={request} /></I18nProvider>);
    expect(screen.queryByTestId('approval-edited-inputs')).toBeNull();
  });
});
