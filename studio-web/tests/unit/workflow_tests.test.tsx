import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { CaseResult, StudioRun } from '../../src/features/evaluations/studioEvalsTypes';

type Handler = (method: string, path: string, options?: { params?: Record<string, unknown>; body?: unknown }) => unknown;
const calls: Array<{ method: string; path: string; options?: { params?: Record<string, unknown>; body?: unknown } }> = [];
const state: { role: string | Error; handle: Handler } = { role: 'admin', handle: () => ({}) };
const client = {
  request: async <T,>(method: string, path: string, options?: { params?: Record<string, unknown>; body?: unknown }): Promise<T> => {
    // EDITIONS_PLAN step 7: Studio reads the role through request(); answered by the fake below.
    if (path.endsWith('/my-membership')) return (await client.members.getMyMembership()) as T;
    calls.push({ method, path, options });
    const out = state.handle(method, path, options);
    if (out instanceof Error) throw out;
    return out as T;
  },
  members: {
    getMyMembership: async () => {
      if (state.role instanceof Error) throw state.role;
      return { role: state.role };
    },
  },
};
const api = { manager: { client }, connection: { workspace: 'w' } };
vi.mock('../../src/api/context', () => ({ useApi: () => api }));

import { I18nProvider } from '../../src/i18n';
import { SaveAsTestButton } from '../../src/features/runs/SaveAsTestButton';
import { WorkflowTestsPanel } from '../../src/features/evaluations/WorkflowTestsPanel';
import { installRequestMembershipSource } from './membershipSource';

// PKG-03: open-source Studio has no member client; the role comes from an extension's source.
installRequestMembershipSource();
import {
  caseRows,
  datasetOutputKey,
  isSafeDatasetId,
  runWorkflowTests,
  saveRunAsTest,
  suggestDatasetId,
} from '../../src/features/evaluations/workflowTests';

const BASE = '/evaluations/studio';

function result(name: string, status: CaseResult['status'], reason = '', error: CaseResult['error'] = null): CaseResult {
  return {
    name,
    repeat: 0,
    status,
    scores: [{ name: 'exact_match', value: status === 'passed' ? 1 : 0, passed: status === 'passed', reason }],
    output_text: '',
    error,
    latency_seconds: 0,
    agent_path: [],
    metadata: {},
  };
}

function evalRun(id: string, workflowId: string, results: CaseResult[], version = 'rev_2'): StudioRun {
  const passed = results.filter((r) => r.status === 'passed').length;
  return {
    run_id: id,
    dataset_id: 'wf-tests',
    dataset_revision: 1,
    label: 'workflow-test',
    target: { kind: 'workflow', id: workflowId, version, output_key: 'route' },
    scorers: [{ type: 'exact_match' }],
    summary: {
      total: results.length,
      passed,
      failed: results.length - passed,
      errors: 0,
      paused: 0,
      pass_rate: results.length ? passed / results.length : 0,
      mean_score: 0,
      latency_seconds: 0,
    },
    created_at: 1,
    report: { target: workflowId, repeats: 1, scorer_names: ['exact_match'], results },
  };
}

const DATASET = {
  dataset_id: 'wf-tests',
  name: 'WF tests',
  description: '',
  revision: 1,
  content_hash: 'h',
  case_count: 2,
  updated_at: 1,
  workflow_id: 'wf',
  cases: [
    { name: 'manual', input: 'x', metadata: {} },
    { name: 'run-1', input: { order_id: 'A' }, expected_output: 'refund', metadata: { source_run_id: 'run-1', output_field: 'route' } },
  ],
};

function wrap(ui: React.ReactElement) {
  return render(<I18nProvider>{ui}</I18nProvider>);
}

beforeEach(() => {
  calls.length = 0;
  state.role = 'admin';
  state.handle = () => ({});
  localStorage.setItem('nexura_locale', 'en');
});

describe('workflow test helpers', () => {
  it('suggests valid dataset ids', () => {
    expect(suggestDatasetId('wf-1')).toBe('wf-1-tests');
    expect(suggestDatasetId('my flow/α')).toBe('my-flow-tests');
    expect(suggestDatasetId('__')).toBe('workflow-tests');
    expect(isSafeDatasetId(suggestDatasetId('x'.repeat(200)))).toBe(true);
    expect(isSafeDatasetId('bad id')).toBe(false);
    expect(isSafeDatasetId('-lead')).toBe(false);
  });

  it('reads the output key from run-saved cases only', () => {
    expect(datasetOutputKey(DATASET)).toBe('route');
    expect(datasetOutputKey({ cases: [{ name: 'a', input: 'x', metadata: {} }] })).toBeNull();
    expect(datasetOutputKey({ cases: [{ name: 'a', input: 'x', metadata: { source_run_id: 'r', output_field: null } }] })).toBeNull();
  });

  it('builds case rows with failing reasons and errors', () => {
    const rows = caseRows(
      evalRun('er-1', 'wf', [
        result('ok', 'passed'),
        result('bad', 'failed', 'output differs from expected'),
        result('boom', 'error', '', { type: 'WorkflowFailed', message: 'handler raised' }),
      ]),
    );
    expect(rows.map((r) => [r.name, r.status])).toEqual([
      ['ok', 'passed'],
      ['bad', 'failed'],
      ['boom', 'error'],
    ]);
    expect(rows[1].reasons).toEqual(['exact_match: output differs from expected']);
    expect(rows[2].reasons).toContain('handler raised');
    expect(caseRows({ ...evalRun('er-2', 'wf', []), report: undefined })).toEqual([]);
  });

  it('saves a run with trimmed fields and omits empty ones', async () => {
    state.handle = () => ({ ok: true });
    await saveRunAsTest(client, 'my ds', { run_id: 'run-1', case_name: '  ', output_key: ' route ', dataset_name: 'T' });
    expect(calls[0].method).toBe('POST');
    expect(calls[0].path).toBe(`${BASE}/datasets/my%20ds/cases/from-run`);
    expect(calls[0].options?.body).toEqual({ run_id: 'run-1', output_key: 'route', dataset_name: 'T' });
  });

  it('runs against the workflow and compares with the previous run of that workflow', async () => {
    const previous = evalRun('er-prev', 'wf', [result('run-1', 'passed')], 'rev_1');
    const fresh = evalRun('er-new', 'wf', [result('run-1', 'failed', 'differs')]);
    state.handle = (method, path) => {
      if (method === 'GET' && path === `${BASE}/datasets/wf-tests`) return DATASET;
      if (method === 'GET' && path === `${BASE}/runs`) {
        return { runs: [evalRun('er-other', 'wf-other', []), previous] };
      }
      if (method === 'POST' && path === `${BASE}/runs`) return fresh;
      if (method === 'POST' && path === `${BASE}/compare`) return { gate: { passed: false }, regressions: [], improvements: [] };
      return new Error(`unexpected ${method} ${path}`);
    };
    const out = await runWorkflowTests(client, 'wf', 'wf-tests');
    expect(out.previous?.run_id).toBe('er-prev');
    expect(out.outputKey).toBe('route');
    const runCall = calls.find((c) => c.method === 'POST' && c.path === `${BASE}/runs`);
    expect(runCall?.options?.body).toEqual({
      dataset_id: 'wf-tests',
      target: { kind: 'workflow', id: 'wf', output_key: 'route' },
      label: 'workflow-test',
    });
    expect(calls.find((c) => c.path === `${BASE}/runs` && c.method === 'GET')?.options?.params).toEqual({
      dataset_id: 'wf-tests',
      limit: 50,
    });
    const cmp = calls.find((c) => c.path === `${BASE}/compare`);
    expect(cmp?.options?.body).toEqual({ baseline_run_id: 'er-prev', candidate_run_id: 'er-new' });
  });

  it('skips the comparison when there is no previous run', async () => {
    state.handle = (method, path) => {
      if (method === 'GET' && path === `${BASE}/datasets/wf-tests`) return { ...DATASET, cases: [] };
      if (method === 'GET') return { runs: [evalRun('er-other', 'wf-other', [])] };
      return evalRun('er-new', 'wf', []);
    };
    const out = await runWorkflowTests(client, 'wf', 'wf-tests');
    expect(out.comparison).toBeNull();
    expect(out.outputKey).toBeNull();
    expect(calls.some((c) => c.path === `${BASE}/compare`)).toBe(false);
    const runCall = calls.find((c) => c.method === 'POST');
    expect((runCall?.options?.body as { target: Record<string, unknown> }).target).toEqual({ kind: 'workflow', id: 'wf' });
  });
});

describe('SaveAsTestButton', () => {
  it('is not shown for runs that are not completed or have no workflow', () => {
    const { container } = wrap(
      <>
        <SaveAsTestButton runId="run-1" workflowId="wf" status="failed" />
        <SaveAsTestButton runId="run-1" workflowId={null} status="completed" />
      </>,
    );
    expect(container.querySelector('[data-testid="save-as-test"]')).toBeNull();
  });

  it('is disabled for viewers', async () => {
    state.role = 'viewer';
    wrap(<SaveAsTestButton runId="run-1" workflowId="wf" status="completed" />);
    const button = await screen.findByTestId('save-as-test');
    await waitFor(() => expect(button).toBeDisabled());
    expect(button.getAttribute('title')).toMatch(/Viewers/);
  });

  it('creates a dataset when the workflow has none and shows the redaction count', async () => {
    const user = userEvent.setup();
    state.role = 'editor';
    state.handle = (method, path) => {
      if (method === 'GET') return { datasets: [] };
      if (path.endsWith('/cases/from-run')) {
        return { ...DATASET, case_count: 1, dataset_id: 'wf-tests', case: { name: 'first', input: {} }, created: true, redactions: 2 };
      }
      return new Error('unexpected');
    };
    wrap(<SaveAsTestButton runId="run-1" workflowId="wf" status="completed" />);
    const button = await screen.findByTestId('save-as-test');
    await waitFor(() => expect(button).not.toBeDisabled());
    await user.click(button);
    expect(await screen.findByTestId('save-as-test-new-id')).toHaveValue('wf-tests');
    expect(calls[0].options?.params).toEqual({ workflow_id: 'wf' });
    const idBox = screen.getByTestId('save-as-test-new-id');
    await user.clear(idBox);
    await user.type(idBox, 'bad id');
    expect(screen.getByTestId('save-as-test-submit')).toBeDisabled();
    await user.clear(idBox);
    await user.type(idBox, 'wf-tests');
    await user.type(screen.getByTestId('save-as-test-case-name'), 'first');
    await user.click(screen.getByTestId('save-as-test-submit'));
    expect(await screen.findByTestId('save-as-test-redactions')).toHaveTextContent('2');
    const post = calls.find((c) => c.method === 'POST');
    expect(post?.path).toBe(`${BASE}/datasets/wf-tests/cases/from-run`);
    expect(post?.options?.body).toEqual({ run_id: 'run-1', case_name: 'first', dataset_name: 'wf tests' });
  });

  it('appends to an existing dataset with its output key and shows server errors as text', async () => {
    const user = userEvent.setup();
    state.handle = (method, path) => {
      if (method === 'GET' && path === `${BASE}/datasets`) return { datasets: [DATASET] };
      if (method === 'GET') return DATASET;
      return new Error('[409] CASE_NAME_TAKEN: <b>already</b> has a case');
    };
    wrap(<SaveAsTestButton runId="run-1" workflowId="wf" status="completed" />);
    const button = await screen.findByTestId('save-as-test');
    await waitFor(() => expect(button).not.toBeDisabled());
    await user.click(button);
    await waitFor(() => expect(screen.getByTestId('save-as-test-output-key')).toHaveValue('route'));
    expect(screen.getByTestId('save-as-test-dataset')).toHaveValue('wf-tests');
    expect(screen.queryByTestId('save-as-test-new-id')).toBeNull();
    await user.click(screen.getByTestId('save-as-test-submit'));
    const err = await screen.findByTestId('save-as-test-error');
    expect(err.textContent).toContain('<b>already</b>');
    expect(err.querySelector('b')).toBeNull();
    expect(calls.find((c) => c.method === 'POST')?.options?.body).toEqual({ run_id: 'run-1', output_key: 'route' });
  });

  it('sends record_path only when "check the path" is ticked', async () => {
    const user = userEvent.setup();
    state.handle = (method, path) => {
      if (method === 'GET' && path === `${BASE}/datasets`) return { datasets: [DATASET] };
      if (method === 'GET') return DATASET;
      return { ...DATASET, case: { name: 'run-1', input: {} }, created: false, redactions: 0 };
    };
    wrap(<SaveAsTestButton runId="run-1" workflowId="wf" status="completed" />);
    const button = await screen.findByTestId('save-as-test');
    await waitFor(() => expect(button).not.toBeDisabled());
    await user.click(button);
    const box = await screen.findByTestId('save-as-test-record-path');
    expect(box).not.toBeChecked();
    await user.click(box);
    await user.click(screen.getByTestId('save-as-test-submit'));
    await waitFor(() => expect(calls.some((c) => c.method === 'POST')).toBe(true));
    expect(calls.find((c) => c.method === 'POST')?.options?.body).toEqual({
      run_id: 'run-1',
      output_key: 'route',
      record_path: true,
    });
  });
});

describe('WorkflowTestsPanel', () => {
  it('lists the workflow datasets and shows an empty state', async () => {
    state.handle = () => ({ datasets: [] });
    wrap(<WorkflowTestsPanel workflowId="wf" />);
    expect(await screen.findByTestId('workflow-tests-empty')).toBeInTheDocument();
    expect(calls[0].options?.params).toEqual({ workflow_id: 'wf' });
  });

  it('runs tests, shows per-case status as text and the gate verdict', async () => {
    const user = userEvent.setup();
    const previous = evalRun('er-prev', 'wf', [result('<i>x</i>', 'passed'), result('b', 'passed')], 'rev_1');
    const fresh = evalRun('er-new', 'wf', [result('<i>x</i>', 'failed', 'output differs'), result('b', 'passed')]);
    state.handle = (method, path) => {
      if (method === 'GET' && path === `${BASE}/datasets`) return { datasets: [DATASET] };
      if (method === 'GET' && path === `${BASE}/datasets/wf-tests`) return DATASET;
      if (method === 'GET' && path === `${BASE}/runs`) return { runs: [previous] };
      if (method === 'POST' && path === `${BASE}/runs`) return fresh;
      return {
        regressions: [{ case: '<i>x</i>', scorer: 'exact_match', baseline: 1, candidate: 0, detail: '' }],
        improvements: [],
        gate: { passed: false, reasons: ['1 regression(s)'], tolerance: 0, min_pass_rate: null, semantics: '', summary: '' },
      };
    };
    const { container } = wrap(<WorkflowTestsPanel workflowId="wf" />);
    expect(await screen.findByTestId('workflow-tests-count-wf-tests')).toHaveTextContent('2');
    await user.click(screen.getByTestId('workflow-tests-run-wf-tests'));
    const gate = await screen.findByTestId('workflow-tests-gate');
    expect(gate).toHaveAttribute('data-passed', 'false');
    expect(gate).toHaveTextContent('er-prev (rev_1)');
    expect(screen.getByTestId('workflow-tests-changes')).toHaveTextContent('Regressions: 1');
    expect(screen.getByTestId('workflow-tests-case-<i>x</i>')).toHaveAttribute('data-status', 'failed');
    expect(screen.getByTestId('workflow-tests-case-b')).toHaveAttribute('data-status', 'passed');
    expect(screen.getByTestId('workflow-tests-pass-rate')).toHaveTextContent('50.0% (1/2)');
    expect(container.querySelector('i')).toBeNull();
    expect(screen.getByTestId('workflow-tests-results')).toHaveTextContent('exact_match: output differs');
  });

  it('says when there is no baseline yet', async () => {
    const user = userEvent.setup();
    state.handle = (method, path) => {
      if (method === 'GET' && path === `${BASE}/datasets`) return { datasets: [DATASET] };
      if (method === 'GET' && path === `${BASE}/datasets/wf-tests`) return DATASET;
      if (method === 'GET') return { runs: [] };
      return evalRun('er-new', 'wf', [result('b', 'passed')]);
    };
    wrap(<WorkflowTestsPanel workflowId="wf" />);
    const run = await screen.findByTestId('workflow-tests-run-wf-tests');
    await waitFor(() => expect(run).not.toBeDisabled());
    await user.click(run);
    expect(await screen.findByTestId('workflow-tests-no-baseline')).toBeInTheDocument();
    expect(calls.some((c) => c.path === `${BASE}/compare`)).toBe(false);
  });

  it('disables running for viewers', async () => {
    state.role = 'viewer';
    state.handle = () => ({ datasets: [DATASET] });
    wrap(<WorkflowTestsPanel workflowId="wf" />);
    const run = await screen.findByTestId('workflow-tests-run-wf-tests');
    await waitFor(() => expect(screen.getByTestId('workflow-tests-viewer-note')).toBeInTheDocument());
    expect(run).toBeDisabled();
  });
});
