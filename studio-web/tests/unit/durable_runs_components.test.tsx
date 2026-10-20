import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { RunRecord } from '@liceron/nexura-sdk';
import { PendingInputBanner } from '../../src/features/runs/PendingInputBanner';
import { RunHistory } from '../../src/features/runs/RunHistory';
import { SubRunsPanel } from '../../src/features/runs/SubRunsPanel';
import { ApprovalDetailsPanel } from '../../src/features/approvals/ApprovalDetailsPanel';

const runsApi = { history: vi.fn(), fork: vi.fn(), subruns: vi.fn() };
vi.mock('../../src/api/context', () => ({
  useApi: () => ({ manager: { client: { runs: runsApi } }, connection: { workspace: 'w' } }),
}));

const SCHEMA = { type: 'object', properties: { region: { type: 'string', enum: ['EU', 'US'] } }, required: ['region'] };

function inputRun(value: unknown): RunRecord {
  return {
    run_id: 'run-1',
    spec_hash: 'h',
    status: 'waiting_for_input',
    step_count: 1,
    input_state: {},
    current_state: {},
    pending_input: { interrupt_id: 'int-1', node_id: 'ask', value },
    created_at: 1,
    updated_at: 2,
  };
}

describe('PendingInputBanner with a schema', () => {
  it('prefills a skeleton, blocks invalid answers and sends a valid one', async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    render(<PendingInputBanner run={inputRun({ question: 'Where?', schema: SCHEMA })} onSubmit={onSubmit} />);
    expect(screen.getByTestId('pending-input-schema').textContent).toContain('"required":["region"]');
    const box = screen.getByLabelText('Your answer') as HTMLTextAreaElement;
    expect(JSON.parse(box.value)).toEqual({ region: '' });
    await user.click(screen.getByRole('button', { name: 'Send answer' }));
    expect(onSubmit).not.toHaveBeenCalled();
    expect(screen.getByTestId('pending-input-validation').textContent).toContain('must be one of');
    await user.clear(box);
    await user.type(box, '{{"region": "EU"}');
    await user.click(screen.getByRole('button', { name: 'Send answer' }));
    expect(onSubmit).toHaveBeenCalledWith('int-1', { region: 'EU' });
  });
});

describe('RunHistory selection and fork with edited state', () => {
  beforeEach(() => {
    runsApi.history.mockReset();
    runsApi.fork.mockReset();
    runsApi.history.mockResolvedValue({
      items: [
        { workflow_run_id: 'run-1', step_count: 1, current_node_id: 'a', status: 'running', state: { x: 1 }, updated_at: 1_700_000_000 },
        { workflow_run_id: 'run-1', step_count: 2, current_node_id: null, status: 'completed', state: { x: 2 }, updated_at: 1_700_000_005 },
      ],
    });
  });

  it('shows step, node, status, time and the selected state', async () => {
    const user = userEvent.setup();
    render(<RunHistory runId="run-1" />);
    const rows = await screen.findAllByTestId('run-history-row');
    expect(rows[0].textContent).toContain('UTC');
    await user.click(screen.getByRole('button', { name: 'View state 0' }));
    expect(screen.getByTestId('run-history-state').textContent).toContain('"x": 1');
  });

  it('validates the edit and forks with stateUpdate', async () => {
    const user = userEvent.setup();
    runsApi.fork.mockResolvedValue({ run_id: 'run-2' });
    const onForked = vi.fn();
    render(<RunHistory runId="run-1" onForked={onForked} />);
    await user.click(await screen.findByRole('button', { name: 'View state 0' }));
    const box = screen.getByLabelText('Fork state update JSON');
    await user.type(box, '{{bad');
    expect(screen.getByTestId('fork-state-error').textContent).toMatch(/JSON syntax error/);
    expect(screen.getByRole('button', { name: 'Fork with edited state' })).toBeDisabled();
    await user.clear(box);
    await user.type(box, '{{"x": 9}');
    await user.click(screen.getByRole('button', { name: 'Fork with edited state' }));
    await waitFor(() => expect(onForked).toHaveBeenCalled());
    expect(runsApi.fork).toHaveBeenCalledWith('run-1', { checkpointIndex: 0, stateUpdate: { x: 9 } });
  });

  it('explains that the end checkpoint cannot be forked', async () => {
    const user = userEvent.setup();
    render(<RunHistory runId="run-1" />);
    await user.click(await screen.findByRole('button', { name: 'View state 1' }));
    expect(screen.queryByLabelText('Fork state update JSON')).toBeNull();
    expect(screen.getByTestId('run-history-detail').textContent).toContain('end of the run');
  });
});

describe('SubRunsPanel', () => {
  beforeEach(() => runsApi.subruns.mockReset());

  it('lists children with status and expands redacted state', async () => {
    const user = userEvent.setup();
    runsApi.subruns.mockResolvedValue({
      items: [{ workflow_run_id: 'run-1/s#2', step_count: 1, current_node_id: 'c2', status: 'paused', state: { a: 1 }, updated_at: 1_700_000_000 }],
    });
    render(<SubRunsPanel runId="run-1" />);
    const row = await screen.findByTestId('subrun-row');
    expect(screen.getByTestId('subrun-id').textContent).toBe('s#2');
    expect(row.textContent).toContain('paused');
    await user.click(screen.getByRole('button', { name: 'Show state' }));
    expect(screen.getByTestId('subrun-state').textContent).toContain('"a": 1');
  });

  it('says so when there are no children', async () => {
    runsApi.subruns.mockResolvedValue({ items: [] });
    render(<SubRunsPanel runId="run-1" />);
    expect(await screen.findByText(/No child runs yet/)).toBeTruthy();
  });
});

describe('ApprovalDetailsPanel', () => {
  it('records missing exact-action fields without inventing details', () => {
    const { container } = render(<ApprovalDetailsPanel pendingApproval={{ approval_id: 'a' }} />);
    expect(container.textContent).toContain('Exact action');
    expect(container.textContent).toContain('not recorded');
    expect(screen.queryByTestId('approval-details')).toBeNull();
  });

  it('shows count, first 20 items, "and N more", the cap and the re-approval note', () => {
    const items = Array.from({ length: 25 }, (_, i) => ({ params: { text: `m${i}` } }));
    render(
      <ApprovalDetailsPanel
        pendingApproval={{ details: { map_over: 'items', item_count: 25, items, max_writes_per_approval: 50 } }}
      />
    );
    expect(screen.getByTestId('approval-item-count').textContent).toContain('25');
    expect(screen.getByTestId('approval-max-writes').textContent).toContain('50');
    expect(screen.getAllByTestId('approval-item')).toHaveLength(20);
    expect(screen.getByTestId('approval-more').textContent).toBe('and 5 more');
    expect(screen.getByTestId('approval-edit-note').textContent).toMatch(/pauses again .* fresh approval/);
  });
});
