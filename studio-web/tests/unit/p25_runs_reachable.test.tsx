// P25: runs reachable after reload. Route parsing, the standalone run page, the workflow Runs tab,
// every "Open run" link, the Approval Inbox "Waiting for input" section and the traces list
// filter/pagination. The API is a local fake (no network); RunVisualizer is stubbed because its
// event stream is covered by runs.test.ts / durable_runs_components.test.tsx.
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ReactNode } from 'react';
import { NexuraApiError } from '@liceron/nexura-sdk';
import type { RunRecord } from '@liceron/nexura-sdk';

const runs = { list: vi.fn(), get: vi.fn(), decide: vi.fn() };
const request = vi.fn();
const triggers = {
  listSchedules: vi.fn(async () => ({ schedules: [] })),
  listWebhooks: vi.fn(async () => ({ webhooks: [] })),
  listJobs: vi.fn(),
  listOutbox: vi.fn(async () => ({ outbox: [], guarantee: 'DRY_RUN_ONLY' })),
};
const workflows = { get: vi.fn() };
const api = {
  manager: { client: { runs, request, triggers, workflows } },
  connection: { status: 'connected', workspace: 'w', principal: 'p' },
};
vi.mock('../../src/api/context', () => ({ useApi: () => api }));

interface StubProps {
  runId: string;
  onClose: () => void;
  onSelectRun?: (id: string) => void;
}
vi.mock('../../src/features/runs/RunVisualizer', () => ({
  RunVisualizer: ({ runId, onClose, onSelectRun }: StubProps) => (
    <div data-testid="run-visualizer-stub">
      <span data-testid="stub-run-id">{runId}</span>
      <button onClick={() => onSelectRun?.('run-child')}>select child</button>
      <button onClick={onClose}>close console</button>
    </div>
  ),
}));

import { I18nProvider } from '../../src/i18n';
import { parseHash, runHref, workflowRunHref } from '../../src/app/routes';
import { RunPage } from '../../src/features/runs/RunPage';
import { WorkflowRunsPanel, WORKFLOW_RUNS_PAGE_SIZE } from '../../src/features/runs/WorkflowRunsPanel';
import { RUN_STATUSES } from '../../src/features/runs/runStatus';
import { WorkflowDetail } from '../../src/features/workflows/WorkflowDetail';
import { RunTraceView } from '../../src/features/traces/RunTraceView';
import { TracesView, TRACES_PAGE_SIZE } from '../../src/features/traces/TracesView';
import { ApprovalInbox } from '../../src/features/approvals/ApprovalInbox';
import { TriggersView } from '../../src/features/triggers/TriggersView';
import { WorkflowTestsPanel } from '../../src/features/evaluations/WorkflowTestsPanel';
import { caseRows } from '../../src/features/evaluations/workflowTests';

const wrap = (ui: ReactNode) => render(<I18nProvider>{ui}</I18nProvider>);

function run(id: string, status: RunRecord['status'], extra: Partial<RunRecord> = {}): RunRecord {
  return {
    run_id: id,
    workflow_id: 'wf-1',
    spec_hash: 'h',
    status,
    step_count: 2,
    input_state: {},
    current_state: {},
    created_at: 1_700_000_000,
    updated_at: 1_700_000_001,
    ...extra,
  };
}

function page(items: RunRecord[], total: number, offset = 0, limit = 20) {
  return { items, total, limit, offset, has_more: offset + items.length < total };
}

beforeEach(() => {
  vi.clearAllMocks();
  window.location.hash = '';
});

describe('parseHash', () => {
  it('parses the standalone run route and decodes the id', () => {
    expect(parseHash('#/runs/run-1')).toMatchObject({ tab: 'workflows', runId: 'run-1', workflowId: null });
    expect(parseHash('#/runs/a%2Fb')).toMatchObject({ runId: 'a/b' });
    // A malformed escape is kept as typed instead of throwing.
    expect(parseHash('#/runs/%E0%A4')).toMatchObject({ runId: '%E0%A4' });
    expect(parseHash(runHref('wf/child#[2]')).runId).toBe('wf/child#[2]');
  });

  it('parses a workflow page with a run open in its Run Console', () => {
    expect(parseHash('#/workflows/wf-1/runs/run-9')).toMatchObject({
      tab: 'workflows',
      workflowId: 'wf-1',
      workflowRunId: 'run-9',
      runId: null,
    });
    expect(parseHash(workflowRunHref('wf-1', 'r 1'))).toMatchObject({ workflowId: 'wf-1', workflowRunId: 'r 1' });
  });

  it('keeps the existing routes', () => {
    expect(parseHash('')).toMatchObject({ tab: 'workflows', workflowId: null, runId: null });
    expect(parseHash('#/workflows/wf-1')).toMatchObject({ tab: 'workflows', workflowId: 'wf-1', workflowRunId: null });
    expect(parseHash('#/traces')).toMatchObject({ tab: 'traces', traceRunId: null });
    expect(parseHash('#/traces/run%201')).toMatchObject({ tab: 'traces', traceRunId: 'run 1' });
    expect(parseHash('#/chat').tab).toBe('playground');
    expect(parseHash('#/integrations').tab).toBe('tools');
    expect(parseHash('#/approvals').tab).toBe('approvals');
    // Deliberate (P25 review): a bare run path opens the run list (traces) instead of workflows.
    expect(parseHash('#/runs/')).toMatchObject({ tab: 'traces', runId: null });
    expect(parseHash('#/runs')).toMatchObject({ tab: 'traces', runId: null });
    expect(parseHash('#/workflows//runs/x')).toMatchObject({ tab: 'workflows', workflowId: null, workflowRunId: null });
    expect(parseHash('#/nope')).toMatchObject({ tab: 'workflows', runId: null, workflowId: null });
  });
});

describe('RunPage (#/runs/{id})', () => {
  it('renders the run console with a link back to its workflow', async () => {
    const user = userEvent.setup();
    runs.get.mockResolvedValue(run('run-1', 'waiting_for_input'));
    wrap(<RunPage runId="run-1" />);
    expect(await screen.findByTestId('stub-run-id')).toHaveTextContent('run-1');
    expect(runs.get).toHaveBeenCalledWith('run-1');
    expect(screen.getByTestId('run-page-workflow-link')).toHaveAttribute('href', '#/workflows/wf-1');
    await user.click(screen.getByRole('button', { name: 'select child' }));
    expect(window.location.hash).toBe('#/runs/run-child');
    await user.click(screen.getByRole('button', { name: 'close console' }));
    expect(window.location.hash).toBe('#/workflows/wf-1');
  });

  it('a run without a workflow links back to the run list', async () => {
    runs.get.mockResolvedValue(run('run-adhoc', 'completed', { workflow_id: null }));
    wrap(<RunPage runId="run-adhoc" />);
    expect(await screen.findByTestId('run-page-runs-link')).toHaveAttribute('href', '#/traces');
  });

  it('shows a not-found notice for an unknown run', async () => {
    runs.get.mockRejectedValue(new NexuraApiError(404, 'RUN_NOT_FOUND', "Run 'nope' not found."));
    wrap(<RunPage runId="nope" />);
    const notice = await screen.findByTestId('run-page-notFound');
    expect(notice).toHaveTextContent('Run not found');
    expect(notice).toHaveTextContent('nope');
    expect(screen.queryByTestId('run-visualizer-stub')).toBeNull();
  });

  it('shows a denied notice for a run of another workspace', async () => {
    runs.get.mockRejectedValue(new NexuraApiError(403, 'CROSS_WORKSPACE_ACCESS_DENIED', 'Cross-workspace access denied'));
    wrap(<RunPage runId="foreign" />);
    expect(await screen.findByTestId('run-page-denied')).toHaveTextContent('Run access denied');
    expect(screen.queryByTestId('run-visualizer-stub')).toBeNull();
  });

  it('shows other errors through formatApiError with a retry', async () => {
    const user = userEvent.setup();
    runs.get.mockRejectedValueOnce(new NexuraApiError(500, 'INTERNAL', 'boom'));
    wrap(<RunPage runId="run-1" />);
    expect(await screen.findByTestId('run-page-error')).toHaveTextContent('boom (INTERNAL)');
    runs.get.mockResolvedValueOnce(run('run-1', 'completed'));
    await user.click(screen.getByRole('button', { name: 'Try again' })); // was the connection label
    expect(await screen.findByTestId('stub-run-id')).toHaveTextContent('run-1');
  });
});

describe('WorkflowRunsPanel (workflow Runs tab)', () => {
  it('lists the workflow runs newest first with status badges and run links', async () => {
    runs.list.mockResolvedValue(
      page(
        [
          run('r-new', 'waiting_for_input'),
          run('r-int', 'interrupted'),
          run('r-rej', 'rejected'),
          run('r-can', 'cancelled'),
          run('r-old', 'completed'),
        ],
        5,
      ),
    );
    wrap(<WorkflowRunsPanel workflowId="wf-1" />);
    const rows = await screen.findAllByTestId('workflow-run-row');
    expect(runs.list).toHaveBeenCalledWith({ workflowId: 'wf-1', status: undefined, limit: WORKFLOW_RUNS_PAGE_SIZE, offset: 0 });
    expect(rows.map((r) => r.getAttribute('data-status'))).toEqual([
      'waiting_for_input',
      'interrupted',
      'rejected',
      'cancelled',
      'completed',
    ]);
    expect(rows[0]).toHaveTextContent('Waiting for input');
    expect(rows[1]).toHaveTextContent('Interrupted');
    expect(rows[2]).toHaveTextContent('Rejected');
    expect(rows[3]).toHaveTextContent('Cancelled');
    expect(within(rows[0]).getByTestId('workflow-run-open')).toHaveAttribute('href', '#/runs/r-new');
    // No pager when everything fits on one page.
    expect(screen.queryByTestId('workflow-runs-range')).toBeNull();
  });

  it('filters by status (every backend status offered) and resets to the first page', async () => {
    const user = userEvent.setup();
    runs.list.mockResolvedValue(page([run('r-1', 'completed')], 1));
    wrap(<WorkflowRunsPanel workflowId="wf-1" />);
    await screen.findAllByTestId('workflow-run-row');
    const select = screen.getByTestId('workflow-runs-status') as HTMLSelectElement;
    expect(Array.from(select.options).map((o) => o.value)).toEqual(['', ...RUN_STATUSES]);
    await user.selectOptions(select, 'waiting_for_input');
    await waitFor(() =>
      expect(runs.list).toHaveBeenLastCalledWith({ workflowId: 'wf-1', status: 'waiting_for_input', limit: 20, offset: 0 }),
    );
  });

  it('pages with offset and shows the range', async () => {
    const user = userEvent.setup();
    const first = Array.from({ length: 20 }, (_, i) => run(`r-${i}`, 'completed'));
    runs.list.mockImplementation(async ({ offset }: { offset: number }) =>
      offset === 0 ? page(first, 21, 0) : page([run('r-20', 'failed')], 21, 20),
    );
    wrap(<WorkflowRunsPanel workflowId="wf-1" />);
    await waitFor(() => expect(screen.getAllByTestId('workflow-run-row')).toHaveLength(20));
    expect(screen.getByTestId('workflow-runs-range')).toHaveTextContent('1-20 of 21');
    expect(screen.getByRole('button', { name: 'Previous' })).toBeDisabled();
    await user.click(screen.getByRole('button', { name: 'Next' }));
    await waitFor(() => expect(screen.getAllByTestId('workflow-run-row')).toHaveLength(1));
    expect(runs.list).toHaveBeenLastCalledWith({ workflowId: 'wf-1', status: undefined, limit: 20, offset: 20 });
    expect(screen.getByTestId('workflow-runs-range')).toHaveTextContent('21-21 of 21');
    expect(screen.getByRole('button', { name: 'Next' })).toBeDisabled();
    await user.click(screen.getByRole('button', { name: 'Previous' }));
    await waitFor(() => expect(screen.getAllByTestId('workflow-run-row')).toHaveLength(20));
  });

  it('shows a list error through formatApiError', async () => {
    runs.list.mockRejectedValue(new NexuraApiError(404, 'WORKFLOW_NOT_FOUND', "Workflow 'wf-x' not found."));
    wrap(<WorkflowRunsPanel workflowId="wf-x" />);
    expect(await screen.findByTestId('workflow-runs-error')).toHaveTextContent("Workflow 'wf-x' not found. (WORKFLOW_NOT_FOUND)");
  });
});

describe('WorkflowDetail keeps the open run in the URL', () => {
  const record = {
    workflow_id: 'wf-1',
    name: 'Flow',
    revision: 1,
    spec: { name: 'Flow', entry_node: 'a', nodes: { a: { id: 'a', handler: 'h' } } },
  };

  it('opens the Run Console for a run id from the URL and reports run switches', async () => {
    const user = userEvent.setup();
    workflows.get.mockResolvedValue(record);
    const onRunChange = vi.fn();
    wrap(
      <WorkflowDetail workflowId="wf-1" runId="run-9" onRunChange={onRunChange} onBack={() => {}} onOpenWorkflow={() => {}} />,
    );
    expect(await screen.findByTestId('stub-run-id')).toHaveTextContent('run-9');
    expect(screen.getByTestId('tab-run-console')).toHaveTextContent('run-9');
    await user.click(screen.getByRole('button', { name: 'select child' }));
    expect(onRunChange).toHaveBeenCalledWith('run-child');
    expect(screen.getByTestId('stub-run-id')).toHaveTextContent('run-child');
  });

  it('has a Runs tab listing this workflow runs', async () => {
    const user = userEvent.setup();
    workflows.get.mockResolvedValue(record);
    runs.list.mockResolvedValue(page([run('r-1', 'pending_approval')], 1));
    wrap(<WorkflowDetail workflowId="wf-1" runId="run-9" onBack={() => {}} onOpenWorkflow={() => {}} />);
    await user.click(await screen.findByTestId('tab-runs'));
    const row = await screen.findByTestId('workflow-run-row');
    expect(row).toHaveTextContent('Pending approval');
    expect(runs.list).toHaveBeenCalledWith(expect.objectContaining({ workflowId: 'wf-1' }));
  });
});

const TRACE = {
  run_id: 'run-1',
  status: 'completed',
  include_content: false,
  truncated: false,
  span_count: 1,
  duration_ms: 5,
  spans: [
    {
      span_id: 's1',
      parent_id: null,
      kind: 'run',
      name: 'run',
      status: 'ok',
      start_ms: 0,
      duration_ms: 5,
      input_tokens: null,
      output_tokens: null,
      total_tokens: null,
      cost_usd: null,
      error: null,
      input: null,
      output: null,
      has_content: false,
    },
  ],
};

describe('Open run links', () => {
  it('trace view links to the run page only when asked', async () => {
    request.mockResolvedValue(TRACE);
    const { unmount } = wrap(<RunTraceView runId="run-1" showOpenRun />);
    expect(await screen.findByTestId('trace-open-run')).toHaveAttribute('href', '#/runs/run-1');
    unmount();
    wrap(<RunTraceView runId="run-1" />);
    await screen.findByTestId('trace-waterfall');
    expect(screen.queryByTestId('trace-open-run')).toBeNull();
  });

  it('a trace page (#/traces/{id}) links to the run page', async () => {
    request.mockResolvedValue(TRACE);
    wrap(<TracesView runId="run-1" />);
    expect(await screen.findByTestId('trace-open-run')).toHaveAttribute('href', '#/runs/run-1');
  });

  it('approval inbox items link to their run', async () => {
    runs.list.mockImplementation(async ({ status }: { status?: string }) =>
      status === 'pending_approval'
        ? page([run('run-gate', 'pending_approval', { pending_approval: { approval_id: 'ap-1', node_id: 'gate' } as RunRecord['pending_approval'] })], 1)
        : page([], 0),
    );
    wrap(<ApprovalInbox onOpenWorkflow={() => {}} />);
    const link = await screen.findByTestId('approval-open-run');
    expect(link).toHaveAttribute('href', '#/runs/run-gate');
    expect(screen.getByRole('button', { name: 'View Workflow' })).toBeInTheDocument();
  });

  it('trigger jobs show their run id and link only when the job has one', async () => {
    triggers.listJobs.mockResolvedValue({
      jobs: [
        { job_id: 'job-ran', workflow_id: 'wf-1', trigger_type: 'schedule', trigger_source_id: 's1', status: 'claimed', run_id: 'run-trig' },
        { job_id: 'job-new', workflow_id: 'wf-1', trigger_type: 'webhook', trigger_source_id: 'h1', status: 'pending', run_id: null },
      ],
      total: 2,
    });
    wrap(<TriggersView />);
    const ran = await screen.findByTestId('trigger-job-run-job-ran');
    expect(ran).toHaveTextContent('run-trig');
    expect(within(ran).getByTestId('trigger-job-open-run')).toHaveAttribute('href', '#/runs/run-trig');
    const fresh = screen.getByTestId('trigger-job-run-job-new');
    expect(fresh).toHaveTextContent('Not started yet');
    expect(within(fresh).queryByTestId('trigger-job-open-run')).toBeNull();
  });

  it('workflow test cases link to their source run only when one is recorded', async () => {
    const result = (name: string, metadata: Record<string, unknown>) => ({
      name,
      repeat: 0,
      status: 'passed' as const,
      scores: [],
      output_text: '',
      error: null,
      latency_seconds: 0,
      agent_path: [],
      metadata,
    });
    const evalRun = {
      run_id: 'eval-run-1', // an eval run id, not a workflow run: never linked
      dataset_id: 'ds',
      dataset_revision: 1,
      label: 'workflow-test',
      target: { kind: 'workflow' as const, id: 'wf-1', version: 'rev_1' },
      scorers: [],
      summary: { total: 2, passed: 2, failed: 0, errors: 0, paused: 0, pass_rate: 1, mean_score: 1, latency_seconds: 0 },
      created_at: 1,
      report: {
        target: 'wf-1',
        repeats: 1,
        scorer_names: [],
        results: [result('saved', { source_run_id: 'run-src' }), result('typed', {})],
      },
    };
    expect(caseRows(evalRun).map((r) => r.sourceRunId)).toEqual(['run-src', undefined]);

    const user = userEvent.setup();
    request.mockImplementation(async (method: string, path: string) => {
      if (path.endsWith('/datasets') && method === 'GET') {
        return { datasets: [{ dataset_id: 'ds', name: 'DS', description: '', revision: 1, content_hash: 'x', case_count: 2, updated_at: 1, workflow_id: 'wf-1' }] };
      }
      if (path.endsWith('/datasets/ds')) return { dataset_id: 'ds', name: 'DS', description: '', revision: 1, cases: [] };
      if (method === 'POST' && path.endsWith('/runs')) return evalRun;
      if (path.endsWith('/runs')) return { runs: [] };
      return {};
    });
    // No members API in this fake: useCanEdit falls back to admin, like the local owner.
    wrap(<WorkflowTestsPanel workflowId="wf-1" />);
    const runBtn = await screen.findByTestId('workflow-tests-run-ds');
    await waitFor(() => expect(runBtn).not.toBeDisabled());
    await user.click(runBtn);
    const saved = await screen.findByTestId('workflow-tests-case-saved');
    expect(within(saved).getByTestId('workflow-tests-source-run')).toHaveAttribute('href', '#/runs/run-src');
    expect(within(screen.getByTestId('workflow-tests-case-typed')).queryByTestId('workflow-tests-source-run')).toBeNull();
    expect(screen.queryByText('eval-run-1', { selector: 'a' })).toBeNull();
  });
});

describe('Approval Inbox: Waiting for input', () => {
  it('lists runs waiting for input, each linking to its run page', async () => {
    runs.list.mockImplementation(async ({ status }: { status?: string }) =>
      status === 'waiting_for_input'
        ? page(
            [
              run('run-ask', 'waiting_for_input', {
                pending_input: { interrupt_id: 'i-1', node_id: 'ask', value: { question: '<b>Which region?</b>' } },
              }),
            ],
            1,
          )
        : page([], 0),
    );
    wrap(<ApprovalInbox />);
    const row = await screen.findByTestId('waiting-input-row');
    expect(runs.list).toHaveBeenCalledWith({ status: 'waiting_for_input', limit: 50 });
    expect(within(row).getByTestId('waiting-input-open')).toHaveAttribute('href', '#/runs/run-ask');
    // Server text is rendered as text, not markup.
    expect(within(row).getByTestId('waiting-input-question').textContent).toBe('<b>Which region?</b>');
    expect(row.querySelector('b')).toBeNull();
    expect(row).toHaveTextContent('ask');
    expect(screen.getByTestId('waiting-input-count')).toHaveTextContent('1');
    // The approvals counter is unchanged by waiting runs.
    expect(screen.getByLabelText('Pending approvals counter')).toHaveTextContent('0 Pending');
  });

  it('shows an empty note and list errors', async () => {
    runs.list.mockImplementation(async ({ status }: { status?: string }) => {
      if (status === 'waiting_for_input') throw new NexuraApiError(403, 'PERMISSION_DENIED', 'nope');
      return page([], 0);
    });
    wrap(<ApprovalInbox />);
    expect(await screen.findByTestId('waiting-input-error')).toHaveTextContent('Your role does not allow this. nope (PERMISSION_DENIED)');
  });

  it('shows the empty note when nothing waits', async () => {
    runs.list.mockResolvedValue(page([], 0));
    wrap(<ApprovalInbox />);
    expect(await screen.findByTestId('waiting-input-empty')).toHaveTextContent('No runs are waiting for input.');
  });
});

describe('TracesView list: statuses and pagination', () => {
  it('offers every backend status in the filter', async () => {
    const user = userEvent.setup();
    runs.list.mockResolvedValue(page([run('r-1', 'interrupted')], 1));
    wrap(<TracesView />);
    await screen.findByTestId('trace-run-row');
    const select = screen.getByLabelText('Run status') as HTMLSelectElement;
    const values = Array.from(select.options).map((o) => o.value);
    for (const s of ['waiting_for_input', 'interrupted', 'rejected', 'cancelled']) expect(values).toContain(s);
    expect(Array.from(select.options).map((o) => o.textContent)).toContain('Waiting for input');
    await user.selectOptions(select, 'rejected');
    await waitFor(() => expect(runs.list).toHaveBeenLastCalledWith({ status: 'rejected', limit: TRACES_PAGE_SIZE, offset: 0 }));
    expect(screen.getByTestId('trace-run-open')).toHaveAttribute('href', '#/runs/r-1');
  });

  it('pages past the first page', async () => {
    const user = userEvent.setup();
    const first = Array.from({ length: TRACES_PAGE_SIZE }, (_, i) => run(`r-${i}`, 'completed'));
    runs.list.mockImplementation(async ({ offset }: { offset: number }) =>
      offset === 0 ? page(first, 51, 0, 50) : page([run('r-50', 'cancelled')], 51, 50, 50),
    );
    wrap(<TracesView />);
    await waitFor(() => expect(screen.getAllByTestId('trace-run-row')).toHaveLength(50));
    expect(screen.getByTestId('trace-runs-range')).toHaveTextContent('1-50 of 51');
    await user.click(screen.getByRole('button', { name: 'Next' }));
    await waitFor(() => expect(screen.getAllByTestId('trace-run-row')).toHaveLength(1));
    expect(runs.list).toHaveBeenLastCalledWith({ status: undefined, limit: 50, offset: 50 });
    expect(screen.getByTestId('trace-run-row')).toHaveAttribute('data-status', 'cancelled');
    expect(screen.getByRole('button', { name: 'Next' })).toBeDisabled();
  });
});
