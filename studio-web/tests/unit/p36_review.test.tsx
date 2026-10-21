// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { useState } from 'react';
import { act, cleanup, fireEvent, render, screen, within, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { RunRecord } from '@liceron/nexura-sdk';
import { ApprovalDecisionModal } from '../../src/features/approvals/ApprovalDecisionModal';
import { I18nProvider } from '../../src/i18n';
import type { ExactApprovalRequest, PendingApprovalItem } from '../../src/features/approvals/types';

const request: ExactApprovalRequest = {
  approval_id: 'review-1', run_id: 'run-1', node_id: 'send', status: 'pending',
  action_kind: 'tool_call', action_name: 'unknown.vendor.send',
  canonical_payload_json: '{"recipients":["one@example.test","two@example.test"],"amount":1.0000000000000001}',
  action_payload: { amount: 1 }, payload_digest: 'server-digest-' + 'a'.repeat(64),
  input_state: { recipients: ['one@example.test', 'two@example.test'] }, expires_at: 2_000_000_000,
};
function item(pending: ExactApprovalRequest = request): PendingApprovalItem {
  return { approvalId: pending.approval_id!, runId: 'run-1', nodeId: 'send', workflowId: 'wf-1',
    createdAt: 1, currentState: { currency: 'INR', amount: 5000 },
    spec: { name: 'Send messages', entry_node: 'send', nodes: { send: { id: 'send', handler: 'unknown.vendor.send' } } },
    runRecord: { run_id: 'run-1', workflow_id: 'wf-1', status: 'pending_approval', current_state: {}, input_state: {},
      step_count: 1, created_at: 1, updated_at: 1, spec_hash: 'h', pending_approval: pending } as RunRecord };
}
const show = (pending = request, decide = vi.fn().mockResolvedValue({ success: true }), close = vi.fn()) =>
  render(<I18nProvider><ApprovalDecisionModal open item={item(pending)} workspace="local-review" onDecide={decide} onClose={close} /></I18nProvider>);
let style: HTMLStyleElement;
beforeEach(() => {
  localStorage.clear();
  style = document.createElement('style');
  style.textContent = readFileSync(path.resolve(__dirname, '../../src/styles/index.css'), 'utf8').replace(/^@import.*$/gm, '');
  document.head.append(style);
  vi.spyOn(HTMLElement.prototype, 'getClientRects').mockImplementation(function(this: HTMLElement) {
    const closed = this.closest('details:not([open])');
    if (this.closest('[hidden], [inert]') || (closed && !closed.querySelector('summary')?.contains(this))) return [] as unknown as DOMRectList;
    return [new DOMRect(0, 0, 100, 44)] as unknown as DOMRectList;
  });
});
afterEach(() => { cleanup(); style.remove(); vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.useRealTimers(); localStorage.clear(); document.documentElement.style.fontSize = ''; });

describe('Round 3 exact-action review', () => {
  it('shows unknown tools and all canonical arguments without guessing an amount, currency or target from paused state', () => {
    show();
    const evidence = screen.getByTestId('exact-action');
    expect(evidence).toHaveTextContent('unknown.vendor.send');
    expect(within(evidence).getByRole('region', { name: 'Arguments (positional and named)' }).querySelector('pre')?.textContent).toBe(request.canonical_payload_json);
    expect(evidence).not.toHaveTextContent('INR');
    expect(evidence).not.toHaveTextContent('5000');
    expect(screen.getByText('local-review', { exact: false })).toBeVisible();
    expect(screen.getByText('Send messages')).toBeVisible();
  });

  it('keeps long payloads fully visible in the body and copies the server bytes unchanged', async () => {
    const canonical = '{"long":"' + 'हिन्दी-recipient-'.repeat(800) + '","value":1.0000000000000001}';
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });
    show({ ...request, canonical_payload_json: canonical });
    const region = screen.getByRole('region', { name: 'Arguments (positional and named)' });
    expect(region.querySelector('pre')?.textContent).toBe(canonical);
    expect(region.closest('details')).toBeNull();
    expect(getComputedStyle(region.querySelector('pre')!).overflowWrap).toBe('anywhere');
    fireEvent.click(within(region).getByRole('button', { name: 'Copy Arguments (positional and named)' }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(canonical));
    expect(await within(region).findByRole('status')).toHaveTextContent('Copied');
  });

  it('labels missing canonical evidence and preserves full digest provenance behind an accessible disclosure', () => {
    show({ ...request, canonical_payload_json: undefined, action_payload: { z: 2, a: 1 } });
    expect(screen.getByText(/Server canonical JSON: not recorded/)).toBeVisible();
    expect(screen.getByText('{"a":1,"z":2}')).toBeVisible();
    fireEvent.click(screen.getByText('Request details'));
    expect(screen.getByTitle(request.payload_digest!)).toHaveTextContent(request.payload_digest!);
    expect(screen.getByRole('button', { name: 'Copy Payload digest' })).toBeVisible();
    expect(screen.getByText('Agent spec digest: not recorded')).toBeVisible();
  });

  it('shows server truncation and write limits without inventing unreturned items; remaining recorded items can be opened', () => {
    const items = Array.from({ length: 25 }, (_, i) => ({ target: 'recorded-' + i }));
    show({ ...request, details: { items, item_count: 40, max_writes_per_approval: 10, map_over: 'items' } });
    expect(screen.getAllByTestId('approval-item')).toHaveLength(20);
    expect(screen.getByTestId('approval-more')).toHaveTextContent('and 5 more');
    expect(screen.getByTestId('approval-server-truncated')).toHaveTextContent('Showing 20 of 40 items. The server returned 25');
    expect(screen.getByTestId('approval-max-writes')).toHaveTextContent('10');
    expect(screen.getByRole('alert')).toHaveTextContent('more items than one approval may cover');
    fireEvent.click(screen.getByText('Show remaining recorded items'));
    expect(screen.getByText('{"target":"recorded-24"}')).toBeVisible();
    expect(screen.queryByText(/recorded-25/)).not.toBeInTheDocument();
  });

  it('preserves ordered agent calls and every argument before request metadata', () => {
    show({ ...request, details: { kind: 'agent_tool_calls', agent_name: 'Local agent', agent_id: 'agent-1', agent_revision: 7,
      calls: [{ tool: 'unknown.send', arguments: { to: ['a', 'b'] } }, { tool: 'unknown.write', arguments: { key: 'अंतिम', text: 'x'.repeat(500) } }] } });
    const calls = screen.getAllByTestId('approval-agent-call');
    expect(calls).toHaveLength(2);
    expect(calls[0]).toHaveTextContent('unknown.send');
    expect(calls[1]).toHaveTextContent('अंतिम');
    expect(within(calls[1]).getByTestId('approval-agent-call-arguments')).toHaveTextContent('x'.repeat(500));
    expect(calls[1].compareDocumentPosition(screen.getByText('Request details')) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it('separates edits from approval, shows old/new inputs, and requires a second decision against the new ID', async () => {
    const next = { ...request, approval_id: 'review-2', input_state: { recipients: ['new'] }, canonical_payload_json: '{"recipients":["new"]}' };
    const decide = vi.fn().mockResolvedValueOnce({ success: true, run: item(next).runRecord }).mockResolvedValueOnce({ success: true });
    const close = vi.fn(); show(request, decide, close);
    fireEvent.click(screen.getByRole('button', { name: 'Edit inputs' }));
    fireEvent.change(screen.getByLabelText('Edited inputs (JSON object)'), { target: { value: '{"recipients":["new"]}' } });
    expect(screen.queryByRole('button', { name: 'Approve action' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Submit edited inputs' }));
    await waitFor(() => expect(decide).toHaveBeenCalledWith('run-1', 'review-1', 'approve', { recipients: ['new'] }));
    expect(await screen.findByTestId('approval-edited-inputs')).toHaveTextContent('new request for edited inputs');
    expect(screen.getByRole('region', { name: 'Previous inputs' })).toHaveTextContent('one@example.test');
    expect(screen.getByRole('region', { name: 'New inputs' })).toHaveTextContent('new');
    expect(close).not.toHaveBeenCalled();
    expect(screen.getByRole('heading', { name: 'Review action' })).toHaveFocus();
    fireEvent.click(screen.getByRole('button', { name: 'Approve action' }));
    await waitFor(() => expect(decide).toHaveBeenLastCalledWith('run-1', 'review-2', 'approve', undefined));
  });

  it('allows rejection even when the optional edit is invalid, and sends no state update', async () => {
    const decide = vi.fn().mockResolvedValue({ success: true }); show(request, decide);
    fireEvent.click(screen.getByRole('button', { name: 'Edit inputs' }));
    fireEvent.change(screen.getByLabelText('Edited inputs (JSON object)'), { target: { value: '[' } });
    expect(screen.getByRole('button', { name: 'Submit edited inputs' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Reject' }));
    await waitFor(() => expect(decide).toHaveBeenCalledWith('run-1', 'review-1', 'reject', undefined));
  });

  it.each(['approved', 'rejected'])('blocks an already %s server request', status => {
    show({ ...request, status });
    expect(screen.getByRole('button', { name: 'Approve action' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Reject' })).toBeDisabled();
    expect(screen.getByRole('alert')).toHaveTextContent('no longer current');
  });

  it('keeps conflict evidence open and cannot silently retry against a new request', async () => {
    const close = vi.fn(), decide = vi.fn().mockResolvedValue({ success: false, conflict: true, error: '409 APPROVAL_ALREADY_CLAIMED' });
    show(request, decide, close);
    fireEvent.click(screen.getByRole('button', { name: 'Approve action' }));
    expect(await screen.findByText('409 APPROVAL_ALREADY_CLAIMED')).toBeVisible();
    expect(screen.getByRole('button', { name: 'Approve action' })).toBeDisabled();
    expect(close).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Approve action' }));
    expect(decide).toHaveBeenCalledTimes(1);
  });

  it('prevents duplicate decisions and closing while waiting for the server', async () => {
    let finish!: (value: { success: boolean }) => void;
    const decide = vi.fn(() => new Promise<{ success: boolean }>(resolve => { finish = resolve; })), close = vi.fn();
    show(request, decide, close);
    fireEvent.click(screen.getByRole('button', { name: 'Approve action' }));
    fireEvent.click(screen.getByRole('button', { name: 'Approve action' }));
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(close).not.toHaveBeenCalled(); expect(decide).toHaveBeenCalledTimes(1);
    expect(screen.getByText('Submitting decision…')).toBeVisible();
    await act(async () => finish({ success: true }));
    expect(close).toHaveBeenCalledTimes(1);
  });

  it.each(['en', 'hi'])('phone review at 200%% text keeps the footer separate, keyboard focus trapped and restores its opener (%s)', async language => {
    vi.stubGlobal('innerWidth', 390);
    localStorage.setItem('nexura_locale', language);
    document.documentElement.style.fontSize = '32px';
    function Example() {
      const [open, setOpen] = useState(false);
      return <I18nProvider><button onClick={() => setOpen(true)}>Open review</button>
        <ApprovalDecisionModal open={open} item={item()} workspace="local-review" onDecide={vi.fn()} onClose={() => setOpen(false)} /></I18nProvider>;
    }
    const user = userEvent.setup(); render(<Example />);
    const opener = screen.getByRole('button', { name: 'Open review' });
    await user.click(opener);
    const dialog = screen.getByRole('dialog'), title = within(dialog).getByRole('heading');
    expect(title).toHaveFocus();
    const approve = within(dialog).getByRole('button', { name: language === 'hi' ? 'कार्रवाई अनुमोदित करें' : 'Approve action' });
    const body = dialog.querySelector('.studio-modal-body')!, footer = dialog.querySelector('.studio-modal-footer')!;
    expect(body.contains(approve)).toBe(false);
    expect(footer.contains(approve)).toBe(true);
    expect(getComputedStyle(footer).flexShrink).toBe('0');
    expect(getComputedStyle(body).overflowY).toBe('auto');
    expect(dialog.querySelector('.studio-modal-surface')).toHaveStyle({ maxWidth: '800px' });
    // JSDOM has no viewport layout: inspect the actual narrow CSS contract as well.
    const media = Array.from(style.sheet!.cssRules).filter(rule => rule.type === 4 && (rule as CSSMediaRule).conditionText === '(max-width: 767px)') as CSSMediaRule[];
    const narrow = media.flatMap(rule => Array.from(rule.cssRules)).find(rule => (rule as CSSStyleRule).selectorText === '.approval-review .studio-modal-surface') as CSSStyleRule;
    const overlay = media.flatMap(rule => Array.from(rule.cssRules)).find(rule => (rule as CSSStyleRule).selectorText === '.studio-modal-overlay.approval-review') as CSSStyleRule;
    expect(overlay.style.getPropertyValue('padding')).toBe('0');
    expect(narrow.style.getPropertyValue('max-height')).toBe('100dvh');
    expect(narrow.style.getPropertyValue('border-radius')).toBe('0');
    approve.focus(); await user.tab();
    expect(within(dialog).getByRole('button', { name: language === 'hi' ? 'संवाद बंद करें' : 'Close dialog' })).toHaveFocus();
    await user.keyboard('{Escape}'); expect(opener).toHaveFocus();
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });
});
