// P25 review regression (promoted from .autopilot/p25/repros): the trigger jobs "Run" column says "Not started yet" for every job without a
// run_id, including jobs the worker already failed before starting a run (refused / HTTP error:
// store.complete_job(..., run_id=None) -> status "failed"). Those jobs will never start.
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { createElement } from 'react';
import { render, screen } from '@testing-library/react';

const triggers = {
  listSchedules: vi.fn(async () => ({ schedules: [] })),
  listWebhooks: vi.fn(async () => ({ webhooks: [] })),
  listJobs: vi.fn(),
  listOutbox: vi.fn(async () => ({ outbox: [], guarantee: 'DRY_RUN_ONLY' })),
};
const api = {
  manager: { client: { triggers, runs: { list: vi.fn() }, request: vi.fn(), workflows: { get: vi.fn() } } },
  connection: { status: 'connected', workspace: 'w', principal: 'p' },
};
vi.mock('../../src/api/context', () => ({ useApi: () => api }));

import { I18nProvider } from '../../src/i18n';
import { TriggersView } from '../../src/features/triggers/TriggersView';

beforeEach(() => {
  triggers.listJobs.mockReset();
});

describe('P25 review: trigger job run column', () => {
  it('a failed job that never started a run is not shown as "Not started yet"', async () => {
    triggers.listJobs.mockResolvedValue({
      jobs: [
        {
          job_id: 'job-refused', workflow_id: 'wf-1', trigger_type: 'webhook', trigger_source_id: 'h1',
          status: 'failed', run_id: null, error_message: 'workflow not published',
        },
      ],
      total: 1,
    });
    render(createElement(I18nProvider, null, createElement(TriggersView)));
    const cell = await screen.findByTestId('trigger-job-run-job-refused');
    expect(cell).not.toHaveTextContent('Not started yet');
  });
});
