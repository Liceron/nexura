// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Liceron
// Local mock transport only. No service or model calls.
import type { ReactNode } from 'react';
import { act, cleanup, render, renderHook, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { NexuraApiError } from '@liceron/nexura-sdk';
import { I18nProvider } from '../../src/i18n';
import { useTriggers } from '../../src/features/triggers/useTriggers';
import { useIntegrations } from '../../src/features/integrations/useIntegrations';
import { useChatSession } from '../../src/features/chat/useChatSession';
import { useStudioEvals } from '../../src/features/evaluations/useStudioEvals';
import { StudioEvalsPanel } from '../../src/features/evaluations/StudioEvalsPanel';
import { CreateWorkflowModal } from '../../src/features/workflows/CreateWorkflowModal';
import { KnowledgeView } from '../../src/features/knowledge/KnowledgeView';
import { TemplatesView } from '../../src/features/templates/TemplatesView';
import userEvent from '@testing-library/user-event';

const fixture = vi.hoisted(() => {
  const client = {
    request: vi.fn(),
    workflows: { list: vi.fn(), validate: vi.fn(), create: vi.fn() },
    knowledge: { listCollections: vi.fn(), listDocuments: vi.fn(), createCollection: vi.fn() },
    triggers: { listSchedules: vi.fn(), listWebhooks: vi.fn(), listJobs: vi.fn(), listOutbox: vi.fn(), testWebhook: vi.fn() },
    integrations: { getStatus: vi.fn(), testRunMcp: vi.fn() },
    sessions: { list: vi.fn(), models: vi.fn(), get: vi.fn(), create: vi.fn(), chatStream: vi.fn() },
    agents: { list: vi.fn() },
  };
  return { client, api: { manager: { client, baseUrl: '/api' }, connection: { workspace: 'local-mock', status: 'connected' } } };
});
vi.mock('../../src/api/context', () => ({ useApi: () => fixture.api }));
vi.mock('../../src/api/membership', () => ({ getMyMembership: async () => ({ role: 'admin' }) }));
const wrapper = ({ children }: { children: ReactNode }) => <I18nProvider>{children}</I18nProvider>;

beforeEach(() => {
  vi.resetAllMocks();
  const c = fixture.client;
  c.request.mockResolvedValue({});
  c.workflows.list.mockResolvedValue({ items: [{ workflow_id: 'wf1', name: 'Local workflow' }], has_more: false });
  c.workflows.validate.mockResolvedValue({ valid: true, errors: [] });
  c.workflows.create.mockResolvedValue({ workflow_id: 'new-workflow' });
  c.knowledge.listCollections.mockResolvedValue({ collections: ['default'] });
  c.knowledge.listDocuments.mockResolvedValue({ items: [], total: 0 });
  c.triggers.listSchedules.mockResolvedValue({ schedules: [] });
  c.triggers.listWebhooks.mockResolvedValue({ webhooks: [{ endpoint_id: 'ep1', workflow_id: 'wf1' }] });
  c.triggers.listJobs.mockResolvedValue({ jobs: [] });
  c.triggers.listOutbox.mockResolvedValue({ outbox: [] });
  c.integrations.getStatus.mockResolvedValue({ mcp: { available: false }, openapi: { available: false } });
  c.sessions.list.mockResolvedValue({ items: [{ session_id: 'saved-session' }] });
  c.sessions.get.mockResolvedValue({ messages: [] });
  c.sessions.models.mockResolvedValue([]);
  c.agents.list.mockResolvedValue({ items: [] });
});
afterEach(cleanup);

it('cron sends the HTTP model fields and retains the server refusal message', async () => {
  const { result } = renderHook(() => useTriggers(), { wrapper });
  await waitFor(() => expect(result.current.loading).toBe(false));
  act(() => { result.current.setNewScheduleId('hourly'); result.current.setNewScheduleType('cron'); result.current.setNewCronExpr('0 * * * *'); result.current.setNewMissedPolicy('burst'); });
  await act(async () => { expect(await result.current.handleCreateSchedule()).toBe(true); });
  expect(fixture.client.request).toHaveBeenCalledWith('POST', '/triggers/schedules', { body: {
    schedule_id: 'hourly', workflow_id: 'wf1', timezone: 'Asia/Kolkata', interval_seconds: undefined, cron_expr: '0 * * * *', missed_run_policy: 'fire_all',
  } });
  act(() => result.current.setNewScheduleId('invalid'));
  fixture.client.request.mockRejectedValueOnce(new NexuraApiError(400, 'INVALID_CRON', 'Cron must have five fields.'));
  await act(async () => { expect(await result.current.handleCreateSchedule()).toBe(false); });
  expect(result.current.formError).toContain('Cron must have five fields.');
});

it('each webhook outcome replaces the previous result, including validation and network errors', async () => {
  const { result } = renderHook(() => useTriggers(), { wrapper });
  await waitFor(() => expect(result.current.testEndpointId).toBe('ep1'));
  const delivered = { job_id: 'j1', is_duplicate: true };
  for (const failure of [new NexuraApiError(401, 'BAD_SIGNATURE', 'Bad signature'), new NexuraApiError(400, 'REPLAY_WINDOW', 'Outside replay window'), new Error('Network unavailable')]) {
    fixture.client.triggers.testWebhook.mockResolvedValueOnce(delivered);
    await act(async () => { await result.current.handleTestWebhook(); });
    expect(result.current.webhookTestResult).toEqual(delivered);
    expect(result.current.webhookTestError).toBeNull();
    fixture.client.triggers.testWebhook.mockRejectedValueOnce(failure);
    await act(async () => { await result.current.handleTestWebhook({ invalid_signature: true }); });
    expect(result.current.webhookTestResult).toBeNull();
    expect(result.current.webhookTestError).toContain(failure.message.replace(/^\[\d+\] \w+: /, ''));
  }
  fixture.client.triggers.testWebhook.mockResolvedValueOnce(delivered);
  await act(async () => { await result.current.handleTestWebhook(); });
  act(() => result.current.setTestPayloadJson('{'));
  await act(async () => { await result.current.handleTestWebhook(); });
  expect(result.current.webhookTestResult).toBeNull();
  expect(result.current.webhookTestError).toBeTruthy();
});

it('a refused test delivery is not reported as a sign-in problem, a real 401 still is', async () => {
  const { result } = renderHook(() => useTriggers(), { wrapper });
  await waitFor(() => expect(result.current.testEndpointId).toBe('ep1'));
  fixture.client.triggers.testWebhook.mockRejectedValueOnce(new NexuraApiError(401, 'INVALID_SIGNATURE', 'Signature mismatch'));
  await act(async () => { await result.current.handleTestWebhook({ invalid_signature: true }); });
  expect(result.current.webhookTestError).toBe('Signature mismatch (INVALID_SIGNATURE)');
  fixture.client.triggers.testWebhook.mockRejectedValueOnce(new NexuraApiError(401, 'UNAUTHENTICATED', 'Missing key'));
  await act(async () => { await result.current.handleTestWebhook(); });
  expect(result.current.webhookTestError).toContain('Sign in again');
});

it('invalid MCP arguments show the parser error and clear the old result without a request', async () => {
  const { result } = renderHook(() => useIntegrations(), { wrapper });
  fixture.client.integrations.testRunMcp.mockResolvedValue({ status: 'success', output: 2 });
  await act(async () => { await result.current.testRunMcp('calculate', '{}'); });
  expect(result.current.mcpRunResult).toBeTruthy();
  await act(async () => { await result.current.testRunMcp('calculate', '{'); });
  expect(result.current.mcpRunResult).toBeNull();
  expect(result.current.error).toMatch(/JSON|property|position|Expected|Unexpected/);
  expect(fixture.client.integrations.testRunMcp).toHaveBeenCalledTimes(1);
});

it('a routed agent survives session reload and failed agent discovery without sending the demo model', async () => {
  fixture.client.agents.list.mockRejectedValue(new Error('Agent list unavailable'));
  fixture.client.sessions.chatStream.mockImplementation(async function* () { yield { kind: 'chat_completed', response: { messages: [], tool_executions: [], reply: 'Mock agent reply' } }; });
  const { result } = renderHook(() => useChatSession('agent1'), { wrapper });
  // An agent's Chat tab starts a new conversation; it does not reopen the workspace's latest session.
  await waitFor(() => expect(result.current.sessions.map(s => s.session_id)).toContain('saved-session'));
  expect(result.current.currentSessionId).toBeNull();
  expect(result.current.selectedAgentId).toBe('agent1');
  await act(async () => { await result.current.selectSession('saved-session'); });
  await act(async () => { await result.current.sendMessage('Hello'); });
  expect(fixture.client.sessions.chatStream).toHaveBeenCalledWith('saved-session', { message: 'Hello', language: 'en', agent_id: 'agent1' }, expect.anything());
});

it('the playground without an agent still reopens the latest session', async () => {
  const { result } = renderHook(() => useChatSession(''), { wrapper });
  await waitFor(() => expect(result.current.currentSessionId).toBe('saved-session'));
});

it('agent evaluations expose only that saved agent targets and runs', async () => {
  fixture.client.request.mockImplementation(async (_method: string, path: string) => {
    if (path.endsWith('/datasets')) return { datasets: [] };
    if (path.endsWith('/targets')) return { targets: [{ kind: 'agent', id: 'agent:a1' }, { kind: 'agent', id: 'agent:a2' }, { kind: 'workflow', id: 'wf1' }] };
    return { runs: [{ run_id: 'mine', target: { kind: 'agent', id: 'agent:a1' } }, { run_id: 'other', target: { kind: 'agent', id: 'agent:a2' } }] };
  });
  const { result } = renderHook(() => useStudioEvals('a1'), { wrapper });
  await waitFor(() => expect(result.current.runs).toHaveLength(1));
  expect(result.current.runs[0].run_id).toBe('mine');
  expect(result.current.targets).toEqual([{ kind: 'agent', id: 'agent:a1' }]);
});

it('an empty Studio-tests dataset explains why saving is refused', async () => {
  fixture.client.request.mockImplementation(async (_method: string, path: string) => path.endsWith('/datasets') ? { datasets: [] } : path.endsWith('/targets') ? { targets: [] } : { runs: [] });
  render(wrapper({ children: <StudioEvalsPanel /> }));
  await act(async () => { screen.getByTestId('studio-evals-save-dataset').click(); });
  expect(screen.getByText('Add at least one test case before saving the dataset.')).toBeInTheDocument();
  expect(fixture.client.request.mock.calls.every(([method]) => method === 'GET')).toBe(true);
});

it('new workflows need an explicit name and warn about duplicate names before creating', async () => {
  const user = userEvent.setup();
  const onCreated = vi.fn();
  render(wrapper({ children: <CreateWorkflowModal open onClose={vi.fn()} onCreated={onCreated} /> }));
  const name = screen.getByLabelText('Workflow Name');
  expect(name).toHaveValue('');
  expect(screen.getByDisplayValue('blank')).toBeChecked();
  await user.type(name, '   ');
  await user.click(screen.getByTestId('create-workflow-submit-btn'));
  expect(await screen.findByText('Workflow name is required.')).toBeInTheDocument();
  expect(fixture.client.workflows.create).not.toHaveBeenCalled();
  await user.clear(name); await user.type(name, 'local workflow');
  fixture.client.workflows.list.mockResolvedValueOnce({ items: [{ name: 'local workflow copy' }], has_more: true }).mockResolvedValueOnce({ items: [{ name: 'Local workflow' }], has_more: false });
  await user.click(screen.getByTestId('create-workflow-submit-btn'));
  expect(await screen.findByText(/already exists. Choose a different name/)).toBeInTheDocument();
  expect(fixture.client.workflows.validate).not.toHaveBeenCalled();
  expect(fixture.client.workflows.list).toHaveBeenCalledWith({ query: 'local workflow', limit: 100, offset: 100 });
  expect(fixture.client.workflows.create).not.toHaveBeenCalled();
  await user.clear(name); await user.type(name, '  Unique workflow  ');
  await user.click(screen.getByTestId('create-workflow-submit-btn'));
  await waitFor(() => expect(onCreated).toHaveBeenCalledWith('new-workflow'));
  expect(fixture.client.workflows.create).toHaveBeenCalledWith(expect.objectContaining({ name: 'Unique workflow' }), { name: 'Unique workflow' });
});

it('knowledge creation previews the exact normalized collection ID before the request', async () => {
  const user = userEvent.setup();
  fixture.client.request.mockResolvedValue({ max_file_bytes: 5000000, max_workspace_bytes: 50000000, max_top_k: 50, workspace_used_bytes: 0 });
  render(wrapper({ children: <KnowledgeView /> }));
  await user.click(screen.getByRole('button', { name: /New collection/i }));
  await user.type(screen.getByLabelText('Collection name'), 'QA kb bad!');
  expect(screen.getByText('qa-kb-bad-')).toBeInTheDocument();
  expect(fixture.client.knowledge.createCollection).not.toHaveBeenCalled();
  await user.click(screen.getByRole('button', { name: /Create collection/i }));
  expect(fixture.client.knowledge.createCollection).toHaveBeenCalledWith({ collection_id: 'qa-kb-bad-', name: 'QA kb bad!' });
});

it('template previews restore from the hash and clear the selection when closed', async () => {
  const user = userEvent.setup();
  const template = { id: 'support', title: 'Support template', description: 'Local fixture', category: 'support', difficulty: 'beginner', tags: [], required_modules: [], required_credentials: [], node_count: 1, spec: { name: 'Support', entry_node: 'start', nodes: { start: { id: 'start', handler: 'local-fixture', transitions: {} } } }, mermaid: '', sample_input: {} };
  fixture.client.request.mockImplementation(async (_method: string, path: string) => path === '/templates' ? { items: [template], categories: [], tags: [] } : template);
  window.history.replaceState(null, '', '#/templates?template=support');
  const view = render(wrapper({ children: <TemplatesView /> }));
  expect(await screen.findByTestId('template-preview')).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Close preview' }));
  expect(window.location.hash).toBe('#/templates');
  await user.click(await screen.findByTestId('template-preview-support'));
  expect(await screen.findByTestId('template-preview')).toBeInTheDocument();
  expect(window.location.hash).toBe('#/templates?template=support');
  view.unmount();
  render(wrapper({ children: <TemplatesView /> }));
  expect(await screen.findByTestId('template-preview')).toBeInTheDocument();
  window.history.replaceState(null, '', '#/workflows');
});

it('clicking the same template after a failed detail request retries without a hash change', async () => {
  const user = userEvent.setup();
  const template = { id: 'retry-support', title: 'Retry support', description: 'Local fixture', category: 'support', difficulty: 'beginner', tags: [], required_modules: [], required_credentials: [], node_count: 1, spec: { name: 'Support', entry_node: 'start', nodes: { start: { id: 'start', handler: 'local-fixture', transitions: {} } } }, mermaid: '', sample_input: {} };
  const detail = vi.fn().mockRejectedValueOnce(new Error('Local mock transport failed')).mockResolvedValueOnce(template);
  fixture.client.request.mockImplementation(async (_method: string, path: string) => path === '/templates' ? { items: [template], categories: [], tags: [] } : detail());
  window.history.replaceState(null, '', '#/templates');
  render(wrapper({ children: <TemplatesView /> }));
  await user.click(await screen.findByTestId('template-preview-retry-support'));
  expect(await screen.findByRole('alert')).toBeInTheDocument();
  expect(screen.queryByTestId('template-preview')).not.toBeInTheDocument();
  expect(window.location.hash).toBe('#/templates?template=retry-support');
  await user.click(screen.getByTestId('template-preview-retry-support'));
  expect(await screen.findByTestId('template-preview')).toBeInTheDocument();
  expect(detail).toHaveBeenCalledTimes(2);
  expect(window.location.hash).toBe('#/templates?template=retry-support');
  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  window.history.replaceState(null, '', '#/workflows');
});
