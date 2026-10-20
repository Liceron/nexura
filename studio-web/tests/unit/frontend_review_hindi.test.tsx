// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
import type { ReactNode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { I18nProvider, useI18n } from '../../src/i18n';
import en from '../../src/i18n/en.json';
import hi from '../../src/i18n/hi.json';
import { CredentialsView } from '../../src/features/credentials/CredentialsView';
import { TriggersView } from '../../src/features/triggers/TriggersView';
import { EvaluationsView } from '../../src/features/evaluations/EvaluationsView';
import { StudioEvalsPanel } from '../../src/features/evaluations/StudioEvalsPanel';
import { IntegrationsView } from '../../src/features/integrations/IntegrationsView';
import { KnowledgeView } from '../../src/features/knowledge/KnowledgeView';
import { WorkflowList } from '../../src/features/workflows/WorkflowList';
import { WorkflowDetail } from '../../src/features/workflows/WorkflowDetail';
import { ImportExportModal } from '../../src/features/workflows/ImportExportModal';
import { ConflictModal } from '../../src/features/workflows/ConflictModal';
import { RunStartModal } from '../../src/features/runs/RunStartModal';
import { RunVisualizer } from '../../src/features/runs/RunVisualizer';
import { RunDiagnostics } from '../../src/features/runs/RunDiagnostics';
import { PendingInputBanner } from '../../src/features/runs/PendingInputBanner';
import { NodeInspector } from '../../src/features/builder/NodeInspector';
import { WorkflowCanvas } from '../../src/features/builder/WorkflowCanvas';
import { WorkspaceSelector } from '../../src/app/WorkspaceSelector';
import { SystemView } from '../../src/features/system/SystemView';
import { InlineConfirm } from '../../src/components/InlineConfirm';
import { Button } from '../../src/components/Button';
import { ChatPlayground } from '../../src/features/chat/ChatPlayground';
import { ModuleDrawer } from '../../src/features/builder/ModuleDrawer';
import { removeMember } from '../../src/api/membership';
import type { RunRecord, WorkflowSpec } from '@liceron/nexura-sdk';

// Explicit local mock API: no service, provider, model or network calls.
const fixture = vi.hoisted(() => {
  const spec = { name: 'नाम', entry_node: 'नोड', nodes: { नोड: { id: 'नोड', handler: 'मॉड्यूल', transitions: {} } } };
  const workflow = { workflow_id: 'वर्कफ़्लो', name: 'नाम', revision: 1, node_count: 1, entry_node: 'नोड', spec, created_at: 1, updated_at: 1 };
  const dataset = { dataset_id: 'डेटासेट', version: '1.0.0', description: '', is_builtin: false, example_count: 1, examples: [{ example_id: 'उदाहरण', input_data: { question: 'User supplied question' }, expected_output: 'User supplied answer', tags: [] }] };
  const run = { run_id: 'रन', workflow_id: 'वर्कफ़्लो', workflow_revision: 1, status: 'completed', revision: 1, current_state: {}, current_node: null, step_count: 1, created_at: 1, updated_at: 1 };
  const credentials = { credential_id: 'कुंजी', provider: 'openai', credential_type: 'api_key', description: '', status: 'active', created_at: 1 };
  const client = {
    modules: { list: vi.fn(async () => ({ items: [] })), get: vi.fn(async () => ({ module_id: 'मॉड्यूल', version: '1.0.0', kind: 'tool', is_available: true, config_schema: { type: 'object', properties: {} }, input_schema: { type: 'object', properties: {} }, output_schema: { type: 'object', properties: {} } })) },
    workflows: { list: vi.fn(async () => ({ items: [workflow], total: 1, has_more: false })), get: vi.fn(async () => workflow), validate: vi.fn(async () => ({ valid: true, issues: [] })) },
    credentials: { list: vi.fn(async () => ({ items: [credentials] })), delete: vi.fn(async () => ({})), revoke: vi.fn(async () => ({})) },
    sessions: { list: vi.fn(async () => ({ items: [] })), models: vi.fn(async () => []) },
    agents: { list: vi.fn(async () => ({ items: [] })) },
    evaluations: { listDatasets: vi.fn(async () => ({ datasets: [dataset] })), getDataset: vi.fn(async () => dataset), listRuns: vi.fn(async () => ({ runs: [] })), deleteDataset: vi.fn(async () => ({})) },
    integrations: { getStatus: vi.fn(async () => ({ mcp: { available: true }, openapi: { available: true } })), discoverOpenAPI: vi.fn(async () => ({ operations: [] })) },
    knowledge: { listCollections: vi.fn(async () => ({ collections: ['संग्रह'] })), listDocuments: vi.fn(async () => ({ items: [{ document_id: 'दस्तावेज़', filename: 'User supplied filename', content_length: 10, chunk_count: 1, content_hash: '123', metadata: {}, indexed: true }] })), deleteDocument: vi.fn(async () => ({})) },
    triggers: {
      listSchedules: vi.fn(async () => ({ schedules: [{ schedule_id: 'शेड्यूल', workflow_id: 'वर्कफ़्लो', schedule_type: 'interval', interval_seconds: 60, timezone: 'UTC', missed_run_policy: 'skip', is_paused: false, next_fire_at: 1 }] })),
      listWebhooks: vi.fn(async () => ({ webhooks: [{ endpoint_id: 'एंडपॉइंट', workflow_id: 'वर्कफ़्लो', replay_window_seconds: 300 }], total: 1 })),
      listJobs: vi.fn(async () => ({ jobs: [] })), listOutbox: vi.fn(async () => ({ outbox: [], guarantee: 'DRY_RUN_ONLY' })),
      deleteSchedule: vi.fn(async () => ({})), deleteWebhook: vi.fn(async () => ({})),
    },
    runs: { diagnostics: vi.fn(async () => ({ usage: { known_input_tokens: 1, known_output_tokens: 2, reported_attempts: 1, complete: false }, attempt_count: 1, truncated_attempts: 0, attempts: [{ id: 'प्रयास', node_id: 'नोड', module_id: 'मॉड्यूल', status: 'completed', duration_ms: 2, input_tokens: 1, output_tokens: 2 }] })), history: vi.fn(async () => ({ items: [] })), children: vi.fn(async () => ({ items: [] })) },
    request: vi.fn(async (): Promise<Record<string, unknown>> => ({ datasets: [], targets: [], runs: [], items: [], max_file_bytes: 5000000, max_workspace_bytes: 50000000, workspace_used_bytes: 0, max_top_k: 50 })),
  };
  const api = { manager: { client, clearCache: vi.fn(), abortSignal: new AbortController().signal, baseUrl: '/api' }, connection: { status: 'connected', workspace: 'कार्यस्थान', principal: 'स्वामी', profile: 'local-owner', baseUrl: '/api' }, changeWorkspace: vi.fn(), refreshConnection: vi.fn() };
  return { api, client, spec, workflow, dataset, run };
});
vi.mock('../../src/api/context', () => ({ useApi: () => fixture.api }));
vi.mock('../../src/api/membership', () => ({ getMyMembership: async () => ({ workspace_id: 'कार्यस्थान', principal_id: 'स्वामी', role: 'admin', permissions: [], profile: 'local-owner' }), listMembers: async () => [{ principal_id: 'सदस्य', role: 'editor', joined_at: 1 }], removeMember: vi.fn() }));
vi.mock('../../src/features/runs/useRunStream', () => ({ useRunStream: () => ({ run: fixture.run, events: [], streamStatus: 'closed', loading: false, error: null, nodeStatuses: new Map(), replaySteps: [], cancelRun: vi.fn(), decideApproval: vi.fn(), provideInput: vi.fn(), refetchSnapshot: vi.fn() }) }));
// Render the real canvas toolbar/inspector. Only browser-specific graph primitives are replaced.
vi.mock('@xyflow/react', () => ({
  ReactFlowProvider: ({ children }: { children: ReactNode }) => <>{children}</>,
  ReactFlow: ({ children }: { children: ReactNode }) => <div>{children}</div>, Background: () => null, Controls: () => null, MiniMap: () => null,
  useNodesState: (nodes: unknown[]) => [nodes, vi.fn(), vi.fn()], useEdgesState: (edges: unknown[]) => [edges, vi.fn(), vi.fn()],
  addEdge: vi.fn(), Handle: () => null, Position: { Left: 'left', Right: 'right' },
}));

// Only product names, protocols, keyboard names and literal API/schema identifiers.
// Ordinary English UI words (Delete, Status, Dataset, Refresh, etc.) are NEVER allowed.
const ALLOWED = new Set([
  'Nexura', 'Studio', 'JSON', 'JSONL', 'API', 'URL', 'HTTP', 'MCP', 'RAG', 'SDK', 'SSE', 'IANA', 'UTC', 'UUID', 'HMAC', 'SHA',
  'OpenAPI', 'OpenAI', 'Anthropic', 'Claude', 'Google', 'Gemini', 'Sarvam', 'WhatsApp', 'SMTP', 'SendGrid', 'PostgreSQL', 'OAuth', 'JWT',
  'Python', 'TypeScript', 'YAML', 'HTML', 'PDF', 'CAS', 'Ctrl', 'Del', 'Enter', 'localStorage', 'DOM', 'zip', 'curl', 'Authorization', 'Bearer',
  'run', 'edit', 'approve', 'admin', 'fire_once', 'skip', 'burst', 'math_add', 'math_multiply', 'echo', 'reverse_text', 'getPetById', 'addPet', 'deletePet', 'getCurrentWeather',
]);
// Exact fixture values model user-owned names/content; never exempt a whole view or row.
const USER_DATA = ['User supplied question', 'User supplied answer', 'User supplied filename'];
// Literal sample IDs/paths in form hints stay usable as code. Match complete values,
// never generic English words. Editable textarea contents are code or user-owned data;
// their labels, placeholders and surrounding help are still checked below.
const TECHNICAL_EXAMPLES = [
  'tenant-alpha', 'default', 'sandbox', 'synthetic_order_lookup', 'dev_sarah', 'srv_indexer',
  'openai_prod', 'whatsapp_api', 'OPENAI_API_KEY', 'support_qa_benchmark', 'sched_order_daily', 'orders_ingress',
  'port_name', 'state_key', 'branch_route', 'calc', 'Asia/Kolkata', 'America/New_York',
  'document.md', 'guide.txt', 'user@example.com',
  'local-eval-fake', 'fake-deterministic', 'NEXURA_WEBHOOK_SECRET', 'NEXURA_API_KEY', 'authenticated-shared-service', 'timestamp', 'event id', 'DRY_RUN_ONLY', 'dedup_order_123',
  'calculate', 'ORD-DELHI-501',
];
const allowedLower = new Set([...ALLOWED].map((word) => word.toLowerCase()));
function englishLeaks(root: HTMLElement): string[] {
  const leaks: string[] = [];
  const check = (text: string) => {
    for (const value of USER_DATA) text = text.replaceAll(value, '');
    for (const value of TECHNICAL_EXAMPLES) {
      const escaped = value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
      text = text.replace(new RegExp(`(?<![A-Za-z0-9_])${escaped}(?![A-Za-z0-9_])`, 'g'), '');
    }
    for (const word of text.match(/[A-Za-z][A-Za-z_]{2,}/g) ?? []) if (!allowedLower.has(word.toLowerCase())) leaks.push(word);
  };
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  while (walker.nextNode()) {
    const node = walker.currentNode;
    const parent = node.parentElement;
    if (!parent || parent.closest('code, pre, kbd, textarea, script, style, [hidden], [aria-hidden="true"], [data-testid^="identifier-"]')) continue;
    check(node.textContent ?? '');
  }
  // Accessible names and hints count too; editable values and JSON source do not.
  for (const element of root.querySelectorAll('[aria-label], [placeholder], [title]')) {
    if (element.closest('code, pre, kbd, [data-testid^="identifier-"]')) continue;
    for (const attr of ['aria-label', 'placeholder', 'title']) check(element.getAttribute(attr) ?? '');
  }
  return [...new Set(leaks)];
}
const show = async (view: ReactNode) => {
  const result = render(<I18nProvider>{view}</I18nProvider>);
  await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
  return result;
};
const assertHindi = () => expect(englishLeaks(document.body)).toEqual([]);
const click = async (id: string) => { fireEvent.click(screen.getByTestId(id)); await act(async () => { await Promise.resolve(); }); assertHindi(); };
const noop = () => {};

beforeEach(() => {
  localStorage.clear(); localStorage.setItem('nexura_locale', 'hi'); vi.clearAllMocks();
  Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', { configurable: true, value: vi.fn() });
});
afterEach(() => { cleanup(); localStorage.clear(); });

describe('Hindi UI coverage (local mock API)', () => {
  it('detects an ordinary untranslated UI word and exempts only identifiers, code and exact user data', () => {
    const { container } = render(<div><span>Untranslated</span><code>print example</code><pre>raw payload</pre><kbd>Escape</kbd><span data-testid="identifier-example">opaque_identifier</span><span>User supplied question</span></div>);
    expect(englishLeaks(container)).toEqual(['Untranslated']);
  });
  it('keeps dictionary keys and placeholders in sync, including durable controls', () => {
    const flatten = (obj: object, prefix = ''): Record<string, string> => Object.fromEntries(Object.entries(obj).flatMap(([k, v]) => typeof v === 'object' ? Object.entries(flatten(v, prefix + k + '.')) : [[prefix + k, v]]));
    const a = flatten(en), b = flatten(hi);
    expect(Object.keys(a).sort()).toEqual(Object.keys(b).sort());
    for (const key of Object.keys(a)) expect(b[key].match(/\{\w+\}/g)?.sort() ?? [], key).toEqual(a[key].match(/\{\w+\}/g)?.sort() ?? []);
    for (const key of ['durable.input.sent', 'durable.subruns.back', 'durable.subruns.open']) expect(b[key]).toMatch(/[\u0900-\u097f]/);
  });
  it('credentials, member management, permissions and credential form', async () => {
    await show(<CredentialsView />); assertHindi();
    await click('register-credential-button'); assertHindi(); fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'रद्द करें' }));
    await click('tab-members'); await click('tab-matrix');
  });
  it('schedules, webhooks, outbox and both trigger forms', async () => {
    await show(<TriggersView />); assertHindi();
    await click('btn-open-create-schedule'); fireEvent.click(screen.getByRole('button', { name: 'रद्द करें' }));
    await click('tab-webhooks'); await click('btn-open-register-webhook'); fireEvent.click(screen.getByRole('button', { name: 'रद्द करें' }));
    await click('tab-outbox');
  });
  it('evaluation datasets, benchmark, comparisons and upload', async () => {
    await show(<EvaluationsView />); assertHindi();
    await click('btn-open-create-dataset'); fireEvent.click(screen.getByRole('button', { name: 'रद्द करें' }));
    await click('tab-run'); await click('tab-compare');
  });
  it('Studio evaluation editor and remaining tabs', async () => {
    await show(<StudioEvalsPanel />); assertHindi();
    const tabs = within(screen.getByRole('tablist')).getAllByRole('tab');
    expect(tabs).toHaveLength(5);
    for (const tab of tabs) {
      fireEvent.click(tab); assertHindi();
      expect(tab).toHaveAttribute('aria-selected', 'true');
      expect(tabs.filter((item) => item.getAttribute('aria-selected') === 'true')).toHaveLength(1);
    }
  });
  it('both integration discovery views', async () => { await show(<IntegrationsView />); assertHindi(); await click('tab-openapi'); });
  it('knowledge ingestion, retrieval and grounded agent', async () => {
    await show(<KnowledgeView />); assertHindi();
    const tabs = within(screen.getByRole('tablist')).getAllByRole('tab');
    expect(tabs).toHaveLength(2);
    expect(screen.getByTestId('tab-retrieval')).toHaveAttribute('aria-selected', 'true');
    fireEvent.click(screen.getByTestId('tab-agent')); assertHindi();
    expect(screen.getByTestId('tab-agent')).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByTestId('tab-retrieval')).toHaveAttribute('aria-selected', 'false');
  });
  it('workflow list, built-in examples and create dialog', async () => {
    await show(<WorkflowList onSelectWorkflow={noop} />); assertHindi();
    fireEvent.click(screen.getByRole('button', { name: /नया वर्कफ़्लो/ })); assertHindi();
  });
  it('workflow detail tabs and real canvas toolbar', async () => { await show(<WorkflowDetail workflowId="वर्कफ़्लो" onBack={noop} onOpenWorkflow={noop} />); assertHindi(); });
  it.each(['export', 'bundle', 'import', 'pyexport', 'pyimport'] as const)('import/export mode %s', async (initialMode) => {
    await show(<ImportExportModal open initialMode={initialMode} workflowId="वर्कफ़्लो" workflowName="नाम" currentSpec={fixture.spec as WorkflowSpec} onClose={noop} />); assertHindi();
    const tabs = within(screen.getByRole('tablist')).getAllByRole('tab');
    expect(tabs).toHaveLength(5);
    expect(tabs[['export', 'bundle', 'import', 'pyexport', 'pyimport'].indexOf(initialMode)])
      .toHaveAttribute('aria-selected', 'true');
    const next = tabs.find((tab) => tab.getAttribute('aria-selected') === 'false')!;
    fireEvent.click(next); assertHindi();
    expect(next).toHaveAttribute('aria-selected', 'true');
    expect(tabs.filter((tab) => tab.getAttribute('aria-selected') === 'true')).toHaveLength(1);
  });
  it('conflict resolution and copy form', async () => { await show(<ConflictModal open workflowName="नाम" expectedRevision={1} localSpec={fixture.spec as WorkflowSpec} onClose={noop} onReloadServer={noop} onSaveAsCopy={async () => {}} />); assertHindi(); fireEvent.click(screen.getByRole('button', { name: 'कॉपी के रूप में सहेजें' })); assertHindi(); });
  it('run-start form and local validation', async () => { await show(<RunStartModal open workflowId="वर्कफ़्लो" workflowName="नाम" serverRevision={1} currentSpec={fixture.spec as WorkflowSpec} hasUnsavedDraft onClose={noop} onLaunchRun={async () => {}} />); assertHindi(); });
  it('translates local JSON-object validation in the run-start dialog', async () => {
    await show(<RunStartModal open workflowId="वर्कफ़्लो" workflowName="नाम" serverRevision={1} currentSpec={fixture.spec as WorkflowSpec} hasUnsavedDraft={false} onClose={noop} onLaunchRun={async () => {}} />);
    fireEvent.change(screen.getByRole('textbox'), { target: { value: '[]' } });
    expect(screen.getByText(hi.runStart.objectError)).toBeInTheDocument(); assertHindi();
  });
  it('run console, status, stream, replay and diagnostics', async () => {
    await show(<RunVisualizer runId="रन" onClose={noop} />); assertHindi();
    const tabs = within(screen.getByRole('tablist')).getAllByRole('tab');
    expect(tabs).toHaveLength(6);
    expect(tabs[2]).toHaveAttribute('aria-selected', 'true');
    fireEvent.click(tabs[0]);
    await act(async () => { await Promise.resolve(); });
    assertHindi();
    expect(tabs[0]).toHaveAttribute('aria-selected', 'true');
    expect(tabs[2]).toHaveAttribute('aria-selected', 'false');
  });
  it('persisted run diagnostics with recorded usage', async () => { await show(<RunDiagnostics runId="रन" revision={1} />); assertHindi(); });
  it('node inspector tabs, module schema and properties', async () => {
    await show(<NodeInspector selectedNode={{ id: 'नोड', type: 'workflowNode', position: { x: 0, y: 0 }, data: { nodeId: 'नोड', label: 'नाम', handler: 'मॉड्यूल', config: {}, input_mapping: {}, output_mapping: {}, requires_approval: false, isEntry: true } }} allNodes={[]} allEdges={[]} entryNode="नोड" onUpdateNodeData={noop} onRenameNode={noop} onSetEntryNode={noop} onDeleteNode={noop} onSetDefaultTransition={noop} onSetBranchTransition={noop} onClose={noop} />); assertHindi();
    for (const tab of screen.getAllByRole('tab')) {
      expect(tab).not.toHaveAttribute('aria-pressed');
      fireEvent.click(tab); assertHindi();
      expect(tab).toHaveAttribute('aria-selected', 'true');
    }
  });
  it('standalone canvas toolbar', async () => { await show(<WorkflowCanvas workflowId="वर्कफ़्लो" workflowName="नाम" initialSpec={fixture.spec as WorkflowSpec} revision={1} onSaveSpec={async () => {}} />); assertHindi(); });
  it('workspace dialog and helper text', async () => { await show(<WorkspaceSelector />); fireEvent.click(screen.getByRole('button')); assertHindi(); });
  it('system feature descriptions', async () => { await show(<SystemView />); assertHindi(); });
  it('chat model labels, quick prompts and empty conversation', async () => { await show(<ChatPlayground />); assertHindi(); });
  it('built-in module drawer labels', async () => { await show(<ModuleDrawer open onClose={noop} onAddNode={noop} />); assertHindi(); });
  it('send-answer action and sent state', async () => {
    const onSubmit = vi.fn(async () => {});
    await show(<PendingInputBanner run={{ ...fixture.run, status: 'waiting_for_input', pending_input: { interrupt_id: 'इनपुट', node_id: 'नोड', value: { question: 'प्रश्न?' } } } as unknown as RunRecord} onSubmit={onSubmit} />);
    assertHindi(); fireEvent.click(screen.getByRole('button', { name: 'उत्तर भेजें' })); await waitFor(() => expect(onSubmit).toHaveBeenCalledOnce()); assertHindi();
    expect(screen.getByText(hi.durable.input.sent)).toBeInTheDocument();
  });
});

describe('shared inline confirmation', () => {
  const confirmAction = async (id: string, action: ReturnType<typeof vi.fn>) => {
    fireEvent.click(screen.getByTestId(id));
    expect(action).not.toHaveBeenCalled(); assertHindi();
    const controls = screen.getByTestId(`${id}-confirm`).parentElement!;
    fireEvent.click(within(controls).getByRole('button', { name: 'रद्द करें' }));
    expect(action).not.toHaveBeenCalled();
    fireEvent.click(screen.getByTestId(id));
    fireEvent.click(screen.getByTestId(`${id}-confirm`));
    await waitFor(() => expect(action).toHaveBeenCalledOnce());
    await act(async () => { await Promise.resolve(); });
  };
  it.each(['delete', 'revoke'] as const)('guards credential %s', async (action) => {
    await show(<CredentialsView />);
    await confirmAction(`${action}-cred-कुंजी`, fixture.client.credentials[action]);
  });
  it('guards member removal', async () => {
    await show(<CredentialsView />); await click('tab-members');
    await confirmAction('remove-member-सदस्य', vi.mocked(removeMember));
  });
  it('guards schedule deletion', async () => {
    await show(<TriggersView />); await confirmAction('btn-delete-schedule-शेड्यूल', fixture.client.triggers.deleteSchedule);
  });
  it('guards webhook deletion', async () => {
    await show(<TriggersView />); await click('tab-webhooks');
    await confirmAction('btn-delete-webhook-एंडपॉइंट', fixture.client.triggers.deleteWebhook);
  });
  it('guards benchmark dataset deletion', async () => {
    await show(<EvaluationsView />); await confirmAction('btn-delete-dataset-डेटासेट', fixture.client.evaluations.deleteDataset);
  });
  it('guards knowledge document deletion', async () => {
    await show(<KnowledgeView />); await confirmAction('delete-doc-दस्तावेज़', fixture.client.knowledge.deleteDocument);
  });
  it('uses the current language for async errors after switching, without restarting data loads', async () => {
    function SwitchLanguage() {
      const { setLanguage } = useI18n();
      return <button onClick={() => setLanguage('hi')}>हिंदी</button>;
    }
    localStorage.setItem('nexura_locale', 'en');
    await show(<><SwitchLanguage /><CredentialsView /></>);
    const loads = fixture.client.credentials.list.mock.calls.length;
    fireEvent.click(screen.getByRole('button', { name: 'हिंदी' }));
    expect(fixture.client.credentials.list).toHaveBeenCalledTimes(loads);
    fixture.client.credentials.delete.mockRejectedValueOnce({});
    fireEvent.click(screen.getByTestId('delete-cred-कुंजी'));
    fireEvent.click(screen.getByTestId('delete-cred-कुंजी-confirm'));
    expect(await screen.findByText(hi.validationMessages.credentialDelete)).toBeInTheDocument();
    assertHindi();
  });
  it('guards Studio evaluation dataset deletion', async () => {
    const dataset = { dataset_id: 'डेटासेट', name: 'नाम', description: '', revision: 1, cases: [] };
    const response = { datasets: [dataset], targets: [], runs: [], items: [], ...dataset, max_file_bytes: 5000000, max_workspace_bytes: 50000000, workspace_used_bytes: 0, max_top_k: 50 };
    const request = vi.spyOn(fixture.client, 'request').mockResolvedValue(response);
    try {
      await show(<StudioEvalsPanel />);
      await click('studio-evals-dataset-डेटासेट');
      const deletions = () => request.mock.calls.filter((args: unknown[]) => args[0] === 'DELETE');
      fireEvent.click(screen.getByTestId('studio-evals-delete-dataset')); expect(deletions()).toHaveLength(0); assertHindi();
      fireEvent.click(screen.getByTestId('studio-evals-delete-dataset-confirm'));
      await waitFor(() => expect(deletions()).toHaveLength(1));
    } finally { cleanup(); request.mockRestore(); }
  });
  it('requires Confirm, supports Cancel, and blocks duplicate submissions while pending', async () => {
    let finish!: () => void;
    const action = vi.fn(() => new Promise<void>((resolve) => { finish = resolve; }));
    await show(<InlineConfirm consequence="यह मान हट जाएगा।"><Button onClick={action}>हटाएँ</Button></InlineConfirm>);
    fireEvent.click(screen.getByRole('button', { name: 'हटाएँ' })); expect(action).not.toHaveBeenCalled(); assertHindi();
    fireEvent.click(screen.getByRole('button', { name: 'रद्द करें' })); expect(action).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'हटाएँ' })); fireEvent.click(screen.getByRole('button', { name: 'पुष्टि करें' })); expect(action).toHaveBeenCalledOnce();
    expect(screen.getByRole('button', { name: /पुष्टि करें/ })).toBeDisabled();
    await act(async () => finish()); expect(screen.getByRole('button', { name: 'हटाएँ' })).toBeInTheDocument();
  });
  it('does not bypass the original disabled permission gate', async () => {
    const action = vi.fn(); await show(<InlineConfirm consequence="हट जाएगा।"><Button disabled onClick={action}>हटाएँ</Button></InlineConfirm>);
    fireEvent.click(screen.getByRole('button', { name: 'हटाएँ' })); expect(screen.queryByRole('button', { name: 'पुष्टि करें' })).toBeNull(); expect(action).not.toHaveBeenCalled();
  });
});
