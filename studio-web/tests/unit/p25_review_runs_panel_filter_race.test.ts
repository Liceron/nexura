// P25 review regression (promoted from .autopilot/p25/repros): WorkflowRunsPanel.load() has no stale-response guard (TracesView and
// WaitingForInputList both have one). Changing the status filter while the previous page is in
// flight lets the older, unfiltered response overwrite the filtered one: the select says "Failed"
// but the table lists running/completed runs. Ordering is controlled with deferred promises.
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { createElement } from 'react';
import { render, screen, act, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

const runs = { list: vi.fn() };
const api = {
  manager: { client: { runs } },
  connection: { status: 'connected', workspace: 'w', principal: 'p' },
};
vi.mock('../../src/api/context', () => ({ useApi: () => api }));

import { I18nProvider } from '../../src/i18n';
import { WorkflowRunsPanel } from '../../src/features/runs/WorkflowRunsPanel';

function run(id: string, status: string) {
  return {
    run_id: id, workflow_id: 'wf-1', spec_hash: 'h', status, step_count: 1,
    input_state: {}, current_state: {}, created_at: 1_700_000_000, updated_at: 1_700_000_001,
  };
}
function page(items: ReturnType<typeof run>[]) {
  return { items, total: items.length, limit: 20, offset: 0, has_more: false };
}

beforeEach(() => {
  runs.list.mockReset();
});

describe('P25 review: Runs tab filter', () => {
  it('a late unfiltered response does not replace the filtered list', async () => {
    const user = userEvent.setup();
    let resolveFirst!: (v: unknown) => void;
    runs.list
      .mockImplementationOnce(() => new Promise((r) => (resolveFirst = r)))
      .mockImplementationOnce(async () => page([run('run-f', 'failed')]));

    render(createElement(I18nProvider, null, createElement(WorkflowRunsPanel, { workflowId: 'wf-1' })));
    await waitFor(() => expect(runs.list).toHaveBeenCalledTimes(1));

    await user.selectOptions(screen.getByTestId('workflow-runs-status'), 'failed');
    await waitFor(() => expect(runs.list).toHaveBeenCalledTimes(2));
    expect(runs.list.mock.calls[1][0]).toMatchObject({ status: 'failed', offset: 0 });
    await waitFor(() =>
      expect(screen.getAllByTestId('workflow-run-row').map((r) => r.getAttribute('data-status'))).toEqual(['failed']),
    );

    // The first (status = all) request finishes last.
    await act(async () => {
      resolveFirst(page([run('run-a', 'running'), run('run-b', 'completed')]));
    });

    expect(screen.getByTestId('workflow-runs-status')).toHaveValue('failed');
    const rows = screen.getAllByTestId('workflow-run-row');
    expect(rows.map((r) => r.getAttribute('data-status'))).toEqual(['failed']);
  });
});
