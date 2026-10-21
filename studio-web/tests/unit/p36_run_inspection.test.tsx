// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import { act, cleanup, fireEvent, render, renderHook, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { NexuraApiError } from '@liceron/nexura-sdk';
import { I18nProvider } from '../../src/i18n';
import { useRunStream } from '../../src/features/runs/useRunStream';
import { RunVisualizer } from '../../src/features/runs/RunVisualizer';
import { RunTraceView } from '../../src/features/traces/RunTraceView';

const fixture = vi.hoisted(() => {
  const run = { run_id: 'run-review', workflow_id: 'wf-review', status: 'pending_approval', created_at: 1, updated_at: 1,
    pending_approval: { approval_id: 'review-old', node_id: 'gate', workflow_run_id: 'run-review', canonical_payload_json: '{"exact":true}' },
    current_state: {}, input_state: {}, events: [], step_count: 1 };
  const api = { manager: { client: {
    runs: { get: vi.fn(), decide: vi.fn(), streamEvents: (_id: string, opts: { signal?: AbortSignal }) =>
      (async function* () { await new Promise<void>(resolve => opts.signal?.addEventListener('abort', () => resolve())); })() },
    request: vi.fn(),
  } }, connection: { status: 'connected', workspace: 'default', baseUrl: 'local', principal: 'local-owner' } };
  return { run, api };
});
vi.mock('../../src/api/context', () => ({ useApi: () => fixture.api }));
beforeEach(() => {
  vi.clearAllMocks(); localStorage.clear();
  window.history.replaceState({}, '', '/');
  fixture.api.manager.client.runs.get.mockResolvedValue(fixture.run);
  fixture.api.manager.client.runs.decide.mockResolvedValue({ ...fixture.run, status: 'completed', pending_approval: null });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe('Round 3 run evidence', () => {
  it('sends the reviewed request ID even when a newer snapshot arrives, and surfaces a server conflict', async () => {
    const { result, unmount } = renderHook(() => useRunStream('run-review'));
    await waitFor(() => expect(result.current.run?.pending_approval?.approval_id).toBe('review-old'));
    fixture.api.manager.client.runs.get.mockResolvedValue({ ...fixture.run, pending_approval: { ...fixture.run.pending_approval, approval_id: 'review-new' } });
    await act(async () => result.current.refetchSnapshot());
    expect(result.current.run?.pending_approval?.approval_id).toBe('review-new');
    fixture.api.manager.client.runs.decide.mockRejectedValue(new NexuraApiError(409, 'APPROVAL_ALREADY_CLAIMED', 'Request already decided'));
    await act(async () => {
      await expect(result.current.decideApproval('approve', undefined, 'review-old')).rejects.toThrow();
    });
    expect(fixture.api.manager.client.runs.decide).toHaveBeenCalledWith('run-review', { approval_id: 'review-old', decision: 'approve', state_update: undefined });
    expect(result.current.run?.pending_approval?.approval_id).toBe('review-new');
    expect(result.current.error).toBeTruthy(); unmount();
  });

  it('requires review from the console before a decision and shows canonical evidence there', async () => {
    render(<I18nProvider><RunVisualizer runId="run-review" onClose={vi.fn()} /></I18nProvider>);
    await screen.findByRole('region', { name: 'Pending approval banner' });
    expect(screen.queryByRole('button', { name: 'Approve action' })).not.toBeInTheDocument();
    screen.getByRole('button', { name: 'Review action' }).focus();
    fireEvent.click(screen.getByRole('button', { name: 'Review action' }));
    const dialog = screen.getByRole('dialog', { name: 'Review action' });
    expect(within(dialog).getByRole('region', { name: 'Arguments (positional and named)' })).toHaveTextContent('{"exact":true}');
    expect(fixture.api.manager.client.runs.decide).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Approve action' }));
    await waitFor(() => expect(fixture.api.manager.client.runs.decide).toHaveBeenCalledWith('run-review', {
      approval_id: 'review-old', decision: 'approve', state_update: undefined,
    }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(screen.getByLabelText('Run status badge')).toHaveTextContent('Completed');
    expect(screen.getByRole('heading', { name: 'Run Console' })).toHaveFocus();
  });

  it('restores selected trace evidence after remount while content remains opt-in', async () => {
    const span = { span_id: 'root', parent_id: null, kind: 'run', name: 'Recorded run', status: 'ok',
      start_ms: 0, duration_ms: 5, total_tokens: null, input_tokens: null, output_tokens: null, cost_usd: null,
      error: null, input: null, output: null, has_content: false };
    fixture.api.manager.client.request.mockResolvedValue({ run_id: 'run-trace', status: 'completed', span_count: 1, duration_ms: 5, spans: [span] });
    const { unmount } = render(<I18nProvider><RunTraceView runId="run-trace" /></I18nProvider>);
    const row = await screen.findByTestId('trace-span');
    fireEvent.click(within(row).getByRole('button'));
    const details = screen.getByRole('region', { name: 'Span details for Recorded run' });
    expect(details).toHaveFocus();
    expect(window.location.search).toContain('traceSpan=root');
    expect(details.querySelector('pre')).toBeNull();
    unmount();
    render(<I18nProvider><RunTraceView runId="run-trace" /></I18nProvider>);
    const restored = await screen.findByRole('region', { name: 'Span details for Recorded run' });
    expect(screen.getByRole('checkbox', { name: 'Include redacted inputs and outputs' })).not.toBeChecked();
    expect(restored).toHaveFocus();
    fireEvent.click(within(restored).getByRole('button', { name: 'Back to events' }));
    expect(screen.queryByRole('region', { name: 'Span details for Recorded run' })).not.toBeInTheDocument();
    expect(window.location.search).not.toContain('traceSpan');
    expect(within(screen.getByTestId('trace-span')).getByRole('button')).toHaveFocus();
  });
});
