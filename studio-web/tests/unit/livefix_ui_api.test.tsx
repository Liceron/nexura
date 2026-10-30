// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Liceron
// All requests use a local mocked client; no provider or HTTP calls.
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, renderHook, screen, waitFor } from '@testing-library/react';
import { NexuraApiError } from '@liceron/nexura-sdk';
import { I18nProvider } from '../../src/i18n';
import { CancelRunButton } from '../../src/features/runs/CancelRunButton';
import { KnowledgeView } from '../../src/features/knowledge/KnowledgeView';
import { EvaluationsView } from '../../src/features/evaluations/EvaluationsView';
import { IntegrationsView } from '../../src/features/integrations/IntegrationsView';
import { CredentialsView } from '../../src/features/credentials/CredentialsView';
import { useStudioEvals } from '../../src/features/evaluations/useStudioEvals';

const mock = vi.hoisted(() => ({
  connection: { workspace: 'default', principal: 'owner', profile: 'local-owner' },
  manager: { clearCache: vi.fn(), client: {
    request: vi.fn(),
    runs: { cancel: vi.fn() },
    modules: { list: vi.fn() },
    knowledge: { listCollections: vi.fn(), listDocuments: vi.fn() },
    evaluations: { listDatasets: vi.fn(), listRuns: vi.fn(), getRun: vi.fn(), runEvaluation: vi.fn() },
    integrations: { getStatus: vi.fn() },
    credentials: { list: vi.fn(), verify: vi.fn(), revoke: vi.fn() },
  } },
  membership: vi.fn(),
}));
vi.mock('../../src/api/context', () => ({ useApi: () => mock }));
vi.mock('../../src/api/membership', async original => ({
  ...await original<typeof import('../../src/api/membership')>(),
  getMyMembership: mock.membership,
}));
const mount = (element: React.ReactNode) => render(<I18nProvider>{element}</I18nProvider>);

beforeEach(() => {
  vi.resetAllMocks(); localStorage.clear();
  mock.membership.mockResolvedValue({ role: 'admin', permissions: ['admin', 'edit', 'run'], principal_id: 'owner' });
  mock.manager.client.request.mockResolvedValue({});
  mock.manager.client.knowledge.listCollections.mockResolvedValue({ collections: ['default', 'manuals'] });
  mock.manager.client.knowledge.listDocuments.mockResolvedValue({ items: [] });
  mock.manager.client.evaluations.listDatasets.mockResolvedValue({ datasets: [] });
  mock.manager.client.evaluations.listRuns.mockResolvedValue({ runs: [] });
  mock.manager.client.credentials.list.mockResolvedValue({ items: [] });
  mock.manager.client.modules.list.mockResolvedValue({ items: [] });
  mock.manager.client.integrations.getStatus.mockResolvedValue({ mcp: { available: false }, openapi: { available: false } });
});
afterEach(cleanup);

it.each(['pending_approval', 'waiting_for_input', 'paused'])('cancels a %s run only after named confirmation with an optional reason', async status => {
  const refreshed = vi.fn();
  mount(<CancelRunButton runId="run-review" status={status} onCancelled={refreshed} />);
  fireEvent.click(screen.getByRole('button', { name: 'Cancel run' }));
  expect(screen.getByRole('dialog')).toHaveTextContent('run-review');
  expect(screen.getByRole('dialog')).toHaveTextContent('does not resolve an unknown external effect');
  expect(mock.manager.client.runs.cancel).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: 'Keep run' }));
  expect(mock.manager.client.runs.cancel).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: 'Cancel run' }));
  fireEvent.change(screen.getByLabelText('Reason (optional)'), { target: { value: '  Operator stopped  ' } });
  fireEvent.click(screen.getByRole('button', { name: 'Confirm cancellation' }));
  await waitFor(() => expect(refreshed).toHaveBeenCalledOnce());
  expect(mock.manager.client.runs.cancel).toHaveBeenCalledWith('run-review', 'Operator stopped');
});

it('hides cancellation for a known viewer and for a terminal run', async () => {
  mock.membership.mockResolvedValue({ role: 'viewer', permissions: [] });
  const view = mount(<CancelRunButton runId="run-review" status="paused" onCancelled={() => {}} />);
  await waitFor(() => expect(screen.queryByRole('button', { name: 'Cancel run' })).toBeNull());
  view.rerender(<I18nProvider><CancelRunButton runId="run-review" status="completed" onCancelled={() => {}} /></I18nProvider>);
  expect(screen.queryByRole('button', { name: 'Cancel run' })).toBeNull();
});

it.each([[403, 'ROLE_DENIED'], [409, 'RUN_ALREADY_TERMINAL']] as const)('shows cancellation refusal %s and keeps the confirmation open', async (status, code) => {
  mock.membership.mockRejectedValue(new Error('Role unknown'));
  mock.manager.client.runs.cancel.mockRejectedValue(new NexuraApiError(status, code, 'Cannot cancel this run'));
  const refreshed = vi.fn();
  mount(<CancelRunButton runId="run-review" status="paused" onCancelled={refreshed} />);
  fireEvent.click(screen.getByRole('button', { name: 'Cancel run' }));
  fireEvent.click(screen.getByRole('button', { name: 'Confirm cancellation' }));
  expect(await screen.findByRole('alert')).toHaveTextContent(code);
  expect(screen.getByRole('dialog')).toBeInTheDocument();
  expect(mock.manager.client.runs.cancel).toHaveBeenCalledWith('run-review', undefined);
  expect(refreshed).not.toHaveBeenCalled();
});

it('deletes the named collection only on confirmation and returns to a remaining collection', async () => {
  mount(<KnowledgeView />);
  await screen.findByRole('option', { name: 'manuals' });
  fireEvent.change(screen.getByTestId('collection-select'), { target: { value: 'manuals' } });
  fireEvent.click(screen.getByRole('button', { name: 'Delete collection' }));
  expect(screen.getByRole('status')).toHaveTextContent('manuals');
  expect(screen.getByRole('status')).toHaveTextContent('documents and index');
  expect(mock.manager.client.request).not.toHaveBeenCalledWith('DELETE', expect.anything());
  mock.manager.client.knowledge.listCollections.mockResolvedValue({ collections: ['default'] });
  fireEvent.click(screen.getByRole('button', { name: 'Confirm' }));
  await waitFor(() => expect(screen.getByTestId('collection-select')).toHaveValue('default'));
  expect(mock.manager.client.request).toHaveBeenCalledWith('DELETE', '/knowledge/collections/manuals');
  expect(screen.queryByRole('option', { name: 'manuals' })).toBeNull();
});

it('deletes a saved benchmark run from Compare after confirmation and clears its picker entry', async () => {
  mock.manager.client.evaluations.listRuns.mockResolvedValue({ runs: [{ run_id: 'eval_saved', dataset_id: 'test', pass_rate: 1 }] });
  mount(<EvaluationsView />);
  fireEvent.click(await screen.findByRole('tab', { name: /Compare/ }));
  fireEvent.click(screen.getByRole('button', { name: 'Delete evaluation run' }));
  expect(screen.getByRole('status')).toHaveTextContent('eval_saved');
  expect(mock.manager.client.request).not.toHaveBeenCalledWith('DELETE', expect.anything());
  fireEvent.click(screen.getByRole('button', { name: 'Confirm' }));
  await waitFor(() => expect(screen.queryByRole('button', { name: 'eval_saved' })).toBeNull());
  expect(mock.manager.client.request).toHaveBeenCalledWith('DELETE', '/evaluations/runs/eval_saved');
});

it('fetches saved benchmark details before reopening a just-executed run and restores its case breakdown', async () => {
  const summary = { run_id: 'eval_saved', dataset_id: 'test', pass_rate: 1, mean_score: 1, total_examples: 1, passed_examples: 1, mean_latency_ms: 2 };
  const detail = { ...summary, results: [{ example_id: 'case-1', passed: true, score: 1, actual_output: 'Local mock answer', expected_output: 'Local mock answer' }] };
  mock.manager.client.evaluations.listRuns.mockResolvedValue({ runs: [summary] });
  mock.manager.client.evaluations.runEvaluation.mockResolvedValue(detail);
  let finish!: (value: typeof detail) => void;
  mock.manager.client.evaluations.getRun.mockReturnValue(new Promise(resolve => { finish = resolve; }));
  mount(<EvaluationsView />);
  fireEvent.click(screen.getByRole('tab', { name: 'Benchmark' }));
  fireEvent.click(screen.getByTestId('btn-run-eval'));
  expect(await screen.findByRole('table', { name: 'Results by case' })).toHaveTextContent('Local mock answer');
  fireEvent.click(screen.getByRole('tab', { name: /Compare/ }));
  fireEvent.click(screen.getByRole('button', { name: 'eval_saved' }));
  expect(mock.manager.client.evaluations.getRun).toHaveBeenCalledWith('eval_saved');
  expect(screen.getByRole('status')).toHaveTextContent(/Loading/);
  expect(screen.queryByTestId('eval-results-container')).toBeNull();
  await act(async () => { finish(detail); });
  expect(screen.getByRole('table', { name: 'Results by case' })).toHaveTextContent('case-1');
  expect(screen.getByRole('table', { name: 'Results by case' })).toHaveTextContent('Local mock answer');
  expect(screen.queryByRole('status')).toBeNull();
});

it('shows saved benchmark detail fetch errors without presenting the list summary as a full report', async () => {
  mock.manager.client.evaluations.listRuns.mockResolvedValue({ runs: [{ run_id: 'eval_saved', dataset_id: 'test', pass_rate: 1 }] });
  mock.manager.client.evaluations.getRun.mockRejectedValue(new NexuraApiError(404, 'RUN_NOT_FOUND', 'Saved benchmark no longer exists'));
  mount(<EvaluationsView />);
  fireEvent.click(await screen.findByRole('tab', { name: /Compare/ }));
  fireEvent.click(screen.getByRole('button', { name: 'eval_saved' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('Saved benchmark no longer exists');
  expect(screen.getByRole('alert')).toHaveTextContent('RUN_NOT_FOUND');
  expect(screen.queryByTestId('eval-results-container')).toBeNull();
  expect(screen.queryByRole('status')).toBeNull();
});

it('deletes a Studio evaluation run using its separate endpoint and clears the selected report', async () => {
  const saved = { run_id: 'qa/run', label: 'QA', target: { kind: 'workflow', id: 'wf1' } };
  mock.manager.client.request.mockImplementation(async (method, path) => {
    if (method === 'DELETE') return {};
    if (path.endsWith('/datasets')) return { datasets: [] };
    if (path.endsWith('/targets')) return { targets: [] };
    if (path.endsWith('/runs')) return { runs: [saved] };
    return saved;
  });
  const { result } = renderHook(() => useStudioEvals());
  await waitFor(() => expect(result.current.runs).toHaveLength(1));
  await act(async () => { await result.current.openRun('qa/run'); });
  expect(result.current.run?.run_id).toBe('qa/run');
  await act(async () => { await result.current.deleteRun('qa/run'); });
  expect(mock.manager.client.request).toHaveBeenCalledWith('DELETE', '/evaluations/studio/runs/qa%2Frun');
  expect(result.current.run).toBeNull(); expect(result.current.runs).toEqual([]);
});

it('lists palette registration response rows and removes exactly the confirmed module', async () => {
  mock.manager.client.request.mockImplementation(async method => method === 'GET' ? { registrations: [
    { module_id: 'petstore:get/pet', kind: 'openapi', fixture_id: 'petstore', server_name_or_prefix: 'petstore' },
    { module_id: 'calc:add', kind: 'mcp', fixture_id: 'math', server_name_or_prefix: 'calc' },
  ] } : {});
  mount(<IntegrationsView />);
  await screen.findByText('petstore:get/pet');
  fireEvent.click(screen.getAllByRole('button', { name: 'Remove' })[0]);
  expect(screen.getByRole('status')).toHaveTextContent('petstore:get/pet');
  expect(mock.manager.client.request).not.toHaveBeenCalledWith('DELETE', expect.anything());
  fireEvent.click(screen.getByRole('button', { name: 'Confirm' }));
  await waitFor(() => expect(screen.queryByText('petstore:get/pet')).toBeNull());
  expect(mock.manager.client.request).toHaveBeenCalledWith('DELETE', '/integrations/registrations?module_id=petstore%3Aget%2Fpet');
  expect(screen.getByText('calc:add')).toBeInTheDocument();
});

it.each(['stored', 'valid'])('renders verify %s as saved without provider validation and clears the result on revoke', async status => {
  const credential = { credential_id: 'provider_key', provider: 'custom', status: 'active', credential_type: 'api_key', created_at: 1 };
  mock.manager.client.credentials.list.mockResolvedValue({ items: [credential] });
  mock.manager.client.credentials.verify.mockResolvedValue({ credential_id: 'provider_key', status, checked_with_provider: false, message: 'Old server says valid', checked_at: 1 });
  mount(<CredentialsView />);
  fireEvent.click(await screen.findByTestId('verify-cred-provider_key'));
  const badge = await screen.findByTestId('verification-badge-provider_key');
  expect(badge).toHaveTextContent('Saved, not checked with the provider');
  expect(badge.className).not.toContain('success');
  expect(screen.queryByText('Old server says valid')).toBeNull();
  fireEvent.click(screen.getByTestId('revoke-cred-provider_key'));
  fireEvent.click(screen.getByRole('button', { name: 'Confirm' }));
  await waitFor(() => expect(screen.queryByTestId('verification-badge-provider_key')).toBeNull());
});
