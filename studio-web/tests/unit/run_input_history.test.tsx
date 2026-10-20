import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { RunRecord } from '@liceron/nexura-sdk';
import { PendingInputBanner } from '../../src/features/runs/PendingInputBanner';
import { RunHistory } from '../../src/features/runs/RunHistory';

const runsApi = {
  history: vi.fn(),
  fork: vi.fn(),
};

const apiValue = { manager: { client: { runs: runsApi } }, connection: { workspace: 'w' } };
vi.mock('../../src/api/context', () => ({
  useApi: () => apiValue,
}));

function makeRun(over: Partial<RunRecord> = {}): RunRecord {
  return {
    run_id: 'run-1',
    spec_hash: 'h',
    status: 'waiting_for_input',
    step_count: 1,
    input_state: {},
    current_state: {},
    pending_input: { interrupt_id: 'int-1', node_id: 'ask', value: { question: 'Which region?' } },
    created_at: 1,
    updated_at: 2,
    ...over,
  };
}

describe('PendingInputBanner', () => {
  it('renders question and full JSON, sends parsed JSON', async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    render(<PendingInputBanner run={makeRun()} onSubmit={onSubmit} />);
    expect(screen.getByTestId('pending-input-banner')).toBeTruthy();
    expect(screen.getByTestId('pending-input-question').textContent).toBe('Which region?');
    expect(screen.getByRole('region', { name: 'Pending input banner' }).querySelector('pre')?.textContent).toContain(
      '"question": "Which region?"'
    );
    await user.type(screen.getByLabelText('Your answer'), '{{"a": 1}');
    await user.click(screen.getByRole('button', { name: 'Send answer' }));
    expect(onSubmit).toHaveBeenCalledWith('int-1', { a: 1 });
  });

  it('sends raw string when not JSON', async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    render(<PendingInputBanner run={makeRun()} onSubmit={onSubmit} />);
    await user.type(screen.getByLabelText('Your answer'), 'eu-west');
    await user.click(screen.getByRole('button', { name: 'Send answer' }));
    expect(onSubmit).toHaveBeenCalledWith('int-1', 'eu-west');
  });

  it('shows API errors inline', async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn().mockRejectedValue(new Error('ALREADY_ANSWERED'));
    render(<PendingInputBanner run={makeRun()} onSubmit={onSubmit} />);
    await user.click(screen.getByRole('button', { name: 'Send answer' }));
    expect((await screen.findByRole('alert')).textContent).toContain('ALREADY_ANSWERED');
  });

  it('renders nothing for other statuses', () => {
    render(<PendingInputBanner run={makeRun({ status: 'running' })} onSubmit={vi.fn()} />);
    expect(screen.queryByTestId('pending-input-banner')).toBeNull();
  });
});

describe('RunHistory', () => {
  beforeEach(() => {
    runsApi.history.mockReset();
    runsApi.fork.mockReset();
  });

  it('lists checkpoints and forks from a row', async () => {
    const user = userEvent.setup();
    runsApi.history.mockResolvedValue({
      items: [
        { workflow_run_id: 'run-1', step_count: 1, current_node_id: 'a', status: 'running' },
        { workflow_run_id: 'run-1', step_count: 2, current_node_id: null, status: 'completed' },
      ],
    });
    runsApi.fork.mockResolvedValue(makeRun({ run_id: 'run-2', status: 'completed' }));
    const onForked = vi.fn();
    render(<RunHistory runId="run-1" onForked={onForked} />);
    const rows = await screen.findAllByTestId('run-history-row');
    expect(rows).toHaveLength(2);
    expect(rows[0].textContent).toContain('a');
    expect(rows[1].textContent).toContain('end');
    await user.click(screen.getAllByRole('button', { name: 'Fork from here' })[0]);
    await waitFor(() => expect(onForked).toHaveBeenCalled());
    expect(runsApi.fork).toHaveBeenCalledWith('run-1', { checkpointIndex: 0 });
    expect(onForked.mock.calls[0][0].run_id).toBe('run-2');
  });

  it('shows fork errors', async () => {
    const user = userEvent.setup();
    runsApi.history.mockResolvedValue({
      items: [{ workflow_run_id: 'run-1', step_count: 1, current_node_id: 'a', status: 'running' }],
    });
    runsApi.fork.mockRejectedValue(new Error('bad index'));
    render(<RunHistory runId="run-1" />);
    await user.click(await screen.findByRole('button', { name: 'Fork from here' }));
    await waitFor(() => expect(runsApi.fork).toHaveBeenCalledWith('run-1', { checkpointIndex: 0 }));
    expect((await screen.findByRole('alert')).textContent).toContain('bad index');
  });
});
