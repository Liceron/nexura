// P26 stage 2: Studio UI for saved agents. Agents page (list, editor validation, save with
// expected_revision and the conflict reload, delete confirm, roles), agent node picker,
// approval details of an agent's tool calls (never the internal resume snapshot), playground
// agent selector and refusals, red-team against a saved agent. Mocked API; no network.
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ReactNode } from 'react';
import { NexuraApiError } from '@liceron/nexura-sdk';
import type { AgentOptions, AgentRecord, AgentSummary } from '@liceron/nexura-sdk';

const LIMITS = {
  max_agents_per_workspace: 100,
  max_spec_chars: 64000,
  max_instructions_chars: 20000,
  max_tools: 20,
  max_knowledge_collections: 10,
  max_skills: 20,
  max_turns: 20,
  max_tool_calls: 50,
  max_timeout_seconds: 300,
};

const OPTIONS: AgentOptions = {
  models: [
    { name: 'fake-deterministic', provider: 'local', label: 'Offline', offline: true, streaming: false, default: true },
    { name: 'gpt-x', provider: 'openai', label: 'GPT X', offline: false, streaming: true, default: false },
  ],
  tools: [
    { module_id: 'demo.lookup', tool_name: 'demo_lookup', title: 'Lookup', description: 'Reads.', kind: 'integration', requires_approval: false },
    { module_id: 'demo.ticket.create', tool_name: 'demo_ticket_create', title: 'Create ticket', description: 'Writes.', kind: 'integration', requires_approval: true },
  ],
  knowledge_collections: ['default', 'policies'],
  skills: ['refunds'],
  node_module_id: 'nexura.agents.agent',
  limits: LIMITS,
};

function summary(over: Partial<AgentSummary> = {}): AgentSummary {
  return {
    agent_id: 'agt_1',
    name: 'helper',
    description: 'Helps <img src=x onerror="window.__agentXss=1">',
    model: 'fake-deterministic',
    tool_count: 2,
    tools: ['demo.lookup', 'demo.ticket.create'],
    approval_tools: ['demo.ticket.create'],
    revision: 3,
    updated_at: 1,
    ...over,
  };
}

function record(over: Partial<AgentRecord['spec']> = {}, revision = 3): AgentRecord {
  return {
    agent_id: 'agt_1',
    workspace_id: 'w',
    revision,
    spec: {
      name: 'helper',
      description: 'Helps',
      instructions: 'Be kind.',
      model: 'fake-deterministic',
      tools: ['demo.lookup', 'demo.ticket.create'],
      knowledge: [],
      skills: [],
      skills_label: 'production',
      output_schema: null as unknown as Record<string, unknown>,
      limits: { max_turns: 6, max_tool_calls: 10, timeout_seconds: 60 },
      ...over,
    },
    created_at: 1,
    updated_at: 1,
    created_by: 'p',
    updated_by: 'p',
    tools: [
      { module_id: 'demo.lookup', tool_name: 'demo_lookup', available: true, requires_approval: false },
      { module_id: 'demo.ticket.create', tool_name: 'demo_ticket_create', available: true, requires_approval: true },
    ],
  };
}

type ChatEvent = Record<string, unknown>;
const state: {
  role: string;
  items: AgentSummary[];
  chatEvents: ChatEvent[];
  request: (method: string, path: string, options?: { body?: unknown }) => unknown;
} = { role: 'admin', items: [], chatEvents: [], request: () => ({}) };

const agentsApi = {
  list: vi.fn(async () => ({ items: state.items, total: state.items.length, limits: LIMITS })),
  options: vi.fn(async () => OPTIONS),
  get: vi.fn(async (_id: string): Promise<AgentRecord> => record()),
  create: vi.fn(),
  update: vi.fn(),
  delete: vi.fn(async () => ({ deleted: true, agent_id: 'agt_1' })),
};
const sessionsApi = {
  list: vi.fn(async () => ({ items: [] })),
  models: vi.fn(async () => OPTIONS.models),
  create: vi.fn(async () => ({ session_id: 's1' })),
  get: vi.fn(async () => ({ messages: [] })),
  delete: vi.fn(),
  chatStream: vi.fn(),
};
const requests: Array<{ method: string; path: string; body?: unknown }> = [];
const client = {
  agents: agentsApi,
  sessions: sessionsApi,
  members: { getMyMembership: async () => ({ role: state.role }) },
  modules: { get: vi.fn(async () => ({ module_id: 'nexura.agents.agent', kind: 'agent', is_available: true, config_schema: { type: 'object' } })) },
  request: async <T,>(method: string, path: string, options?: { body?: unknown }): Promise<T> => {
    // EDITIONS_PLAN step 7: Studio reads the role through request(); answered by the fake above.
    if (path.endsWith('/my-membership')) return (await client.members.getMyMembership()) as T;
    requests.push({ method, path, body: options?.body });
    return state.request(method, path, options) as T;
  },
};
const api = { manager: { client, clearCache: () => undefined }, connection: { status: 'connected', workspace: 'w' } };
vi.mock('../../src/api/context', () => ({ useApi: () => api }));

import { I18nProvider } from '../../src/i18n';
import { AgentsView } from '../../src/features/agents/AgentsView';
import { AgentNodeConfig } from '../../src/features/agents/AgentNodeConfig';
import { validateDraft, emptyDraft, fieldErrorsFromApi, draftFieldOf } from '../../src/features/agents/agentForm';
import { NodeInspector } from '../../src/features/builder/NodeInspector';
import { ApprovalDetailsPanel } from '../../src/features/approvals/ApprovalDetailsPanel';
import { describeAgentToolCalls } from '../../src/features/approvals/agentToolCalls';
import { ChatPlayground } from '../../src/features/chat/ChatPlayground';
import { StudioEvalsPanel } from '../../src/features/evaluations/StudioEvalsPanel';
import { parseHash } from '../../src/app/routes';
import { installRequestMembershipSource } from './membershipSource';

// PKG-03: open-source Studio has no member client; the role comes from an extension's source.
installRequestMembershipSource();

const wrap = (ui: ReactNode) => render(<I18nProvider>{ui}</I18nProvider>);

beforeEach(() => {
  state.role = 'admin';
  state.items = [summary()];
  state.chatEvents = [];
  state.request = () => ({});
  requests.length = 0;
  for (const fn of [...Object.values(agentsApi), ...Object.values(sessionsApi)]) fn.mockClear();
  agentsApi.get.mockImplementation(async () => record());
  localStorage.clear();
  Element.prototype.scrollIntoView = vi.fn();
});

// ------------------------------------------------------------------ helpers

describe('agent form helpers', () => {
  it('checks names, output schema JSON and limits against the server bounds', () => {
    const bad = validateDraft({
      ...emptyDraft(),
      name: 'Bad Name',
      output_schema: '{not json',
      max_turns: '21',
      max_tool_calls: '-1',
      timeout_seconds: '0',
    }, LIMITS);
    expect(bad.spec).toBeNull();
    expect(Object.keys(bad.errors).sort()).toEqual(['max_tool_calls', 'max_turns', 'name', 'output_schema', 'timeout_seconds']);
    expect(validateDraft({ ...emptyDraft(), name: 'ok', output_schema: '[1]' }).errors.output_schema).toMatch(/JSON object/);
    const good = validateDraft({ ...emptyDraft('gpt-x'), name: 'ok_1', output_schema: '{"type":"object"}', max_turns: '3' });
    expect(good.spec).toEqual({
      name: 'ok_1',
      description: '',
      instructions: '',
      model: 'gpt-x',
      tools: [],
      knowledge: [],
      skills: [],
      skills_label: 'production',
      output_schema: { type: 'object' },
      limits: { max_turns: 3, max_tool_calls: 10, timeout_seconds: 60 },
    });
  });

  it('maps server field paths to editor fields', () => {
    expect(draftFieldOf('tools.3')).toBe('tools');
    expect(draftFieldOf('limits.max_turns')).toBe('max_turns');
    expect(draftFieldOf('spec')).toBe('form');
    const err = new NexuraApiError(422, 'AGENT_INVALID', 'Invalid agent', {
      code: 'AGENT_INVALID',
      field_errors: [{ field: 'knowledge.0', message: "Unknown knowledge collection 'x'." }],
    });
    expect(fieldErrorsFromApi(err)).toEqual({ knowledge: "Unknown knowledge collection 'x'." });
    expect(fieldErrorsFromApi(new NexuraApiError(409, 'AGENT_NAME_TAKEN', 'taken')).name).toMatch(/Another agent/);
  });

  it('routes #/agents to the Agents page', () => {
    expect(parseHash('#/agents').tab).toBe('agents');
  });
});

// ------------------------------------------------------------------ Agents page

describe('Agents page', () => {
  it('lists agents with model, tool count and approval badges; text stays text', async () => {
    const { container } = wrap(<AgentsView />);
    const row = await screen.findByTestId('agent-row-helper');
    expect(screen.getByTestId('agent-row-model-helper').textContent).toBe('fake-deterministic');
    expect(screen.getByTestId('agent-row-tools-helper').textContent).toBe('2 tools');
    expect(screen.getByTestId('agent-row-approval-helper').textContent).toBe('1 need approval');
    expect(row.textContent).toContain('<img src=x onerror=');
    expect(container.querySelector('img')).toBeNull();
  });

  it('shows the empty state explaining operator models and the offline fake', async () => {
    state.items = [];
    wrap(<AgentsView />);
    const empty = await screen.findByTestId('agents-empty');
    expect(empty.textContent).toContain('NEXURA_CHAT_MODELS');
    expect(empty.textContent).toMatch(/offline fake model .*without any API key/);
  });

  it('validates the editor before creating, then sends the spec', async () => {
    const user = userEvent.setup();
    agentsApi.create.mockImplementation(async () => record({ name: 'filer' }, 1));
    wrap(<AgentsView />);
    await screen.findByTestId('agent-row-helper');
    await user.click(screen.getByTestId('agents-new'));
    await user.type(screen.getByTestId('agent-name'), 'Bad Name');
    await user.type(screen.getByTestId('agent-output-schema'), '{{oops');
    await user.clear(screen.getByTestId('agent-max-turns'));
    await user.type(screen.getByTestId('agent-max-turns'), '99');
    await user.click(screen.getByTestId('agent-save'));
    expect(agentsApi.create).not.toHaveBeenCalled();
    expect(screen.getByTestId('agent-error-name')).toBeInTheDocument();
    expect(screen.getByTestId('agent-error-output_schema').textContent).toMatch(/Not valid JSON/);
    expect(screen.getByTestId('agent-error-max_turns').textContent).toMatch(/1 to 20/);

    // Tools show their approval policy; writes are badged.
    expect(screen.getByTestId('agent-tool-approval-demo.ticket.create')).toBeInTheDocument();
    expect(screen.queryByTestId('agent-tool-approval-demo.lookup')).toBeNull();

    await user.clear(screen.getByTestId('agent-name'));
    await user.type(screen.getByTestId('agent-name'), 'filer');
    await user.clear(screen.getByTestId('agent-output-schema'));
    await user.clear(screen.getByTestId('agent-max-turns'));
    await user.type(screen.getByTestId('agent-max-turns'), '4');
    await user.selectOptions(screen.getByTestId('agent-model'), 'gpt-x');
    await user.click(screen.getByTestId('agent-tool-demo.ticket.create'));
    await user.click(screen.getByTestId('agent-knowledge-policies'));
    await user.click(screen.getByTestId('agent-skill-refunds'));
    await user.click(screen.getByTestId('agent-save'));
    await waitFor(() => expect(agentsApi.create).toHaveBeenCalledTimes(1));
    expect(agentsApi.create).toHaveBeenCalledWith({
      name: 'filer',
      description: '',
      instructions: '',
      model: 'gpt-x',
      tools: ['demo.ticket.create'],
      knowledge: ['policies'],
      skills: ['refunds'],
      skills_label: 'production',
      output_schema: null,
      limits: { max_turns: 4, max_tool_calls: 10, timeout_seconds: 60 },
    });
    expect((await screen.findByTestId('agents-notice')).textContent).toContain('filer');
  });

  it('shows server field errors next to the field', async () => {
    const user = userEvent.setup();
    agentsApi.create.mockRejectedValue(
      new NexuraApiError(422, 'AGENT_INVALID', 'Invalid agent: Unknown tool', {
        code: 'AGENT_INVALID',
        field_errors: [{ field: 'tools.0', message: "Unknown tool module 'gone'." }],
      }),
    );
    wrap(<AgentsView />);
    await user.click(await screen.findByTestId('agents-new'));
    await user.type(screen.getByTestId('agent-name'), 'x');
    await user.click(screen.getByTestId('agent-save'));
    expect((await screen.findByTestId('agent-error-tools')).textContent).toBe("Unknown tool module 'gone'.");
    expect(screen.getByTestId('agents-error').textContent).toContain('AGENT_INVALID');
  });

  it('saves with expected_revision; a 409 conflict offers a reload that discards edits', async () => {
    const user = userEvent.setup();
    agentsApi.update.mockRejectedValueOnce(
      new NexuraApiError(409, 'REVISION_CONFLICT', 'stale', { code: 'REVISION_CONFLICT', current_revision: 4 }),
    );
    wrap(<AgentsView />);
    await user.click(await screen.findByTestId('agent-row-helper'));
    expect((await screen.findByTestId('agent-revision')).textContent).toBe('rev 3');
    await user.clear(screen.getByTestId('agent-description'));
    await user.type(screen.getByTestId('agent-description'), 'mine');
    await user.click(screen.getByTestId('agent-save'));
    await waitFor(() => expect(agentsApi.update).toHaveBeenCalledTimes(1));
    const [id, spec, expected] = agentsApi.update.mock.calls[0];
    expect(id).toBe('agt_1');
    expect(expected).toBe(3);
    expect(spec.description).toBe('mine');
    expect(await screen.findByTestId('agent-conflict')).toBeInTheDocument();
    expect(screen.getByTestId('agents-error').textContent).toContain('REVISION_CONFLICT');

    agentsApi.get.mockImplementation(async () => record({ description: 'theirs' }, 4));
    await user.click(screen.getByTestId('agent-conflict-reload'));
    await waitFor(() => expect(screen.getByTestId('agent-revision').textContent).toBe('rev 4'));
    expect((screen.getByTestId('agent-description') as HTMLInputElement).value).toBe('theirs');
    expect(screen.queryByTestId('agent-conflict')).toBeNull();

    agentsApi.update.mockImplementation(async () => record({ description: 'again' }, 5));
    await user.click(screen.getByTestId('agent-save'));
    await waitFor(() => expect(agentsApi.update).toHaveBeenCalledTimes(2));
    expect(agentsApi.update.mock.calls[1][2]).toBe(4);
    await waitFor(() => expect(screen.getByTestId('agent-revision').textContent).toBe('rev 5'));
  });

  it('deletes only after a second click (admin), with the loaded revision', async () => {
    const user = userEvent.setup();
    wrap(<AgentsView />);
    await user.click(await screen.findByTestId('agent-row-helper'));
    await user.click(await screen.findByTestId('agent-delete'));
    expect(agentsApi.delete).not.toHaveBeenCalled();
    await user.click(screen.getByTestId('agent-delete-confirm'));
    await waitFor(() => expect(agentsApi.delete).toHaveBeenCalledWith('agt_1', { expectedRevision: 3 }));
    expect((await screen.findByTestId('agents-notice')).textContent).toContain('helper');
  });

  it('viewers read only; editors edit but cannot delete', async () => {
    const user = userEvent.setup();
    state.role = 'viewer';
    const { unmount } = wrap(<AgentsView />);
    await waitFor(() => expect(screen.getByTestId('agents-new')).toBeDisabled());
    await user.click(await screen.findByTestId('agent-row-helper'));
    await screen.findByTestId('agent-revision');
    expect(screen.getByTestId('agent-read-only')).toBeInTheDocument();
    expect(screen.getByTestId('agent-name')).toBeDisabled();
    expect(screen.getByTestId('agent-instructions')).toBeDisabled();
    expect(screen.queryByTestId('agent-save')).toBeNull();
    expect(screen.queryByTestId('agent-delete')).toBeNull();
    unmount();

    state.role = 'editor';
    wrap(<AgentsView />);
    await user.click(await screen.findByTestId('agent-row-helper'));
    await screen.findByTestId('agent-revision');
    await waitFor(() => expect(screen.getByTestId('agent-name')).not.toBeDisabled());
    expect(screen.getByTestId('agent-save')).toBeInTheDocument();
    expect(screen.queryByTestId('agent-delete')).toBeNull();
  });
});

// ------------------------------------------------------------------ node inspector

describe('agent node picker', () => {
  it('lists saved agents and writes agent_id, input_key and output_key to the config', async () => {
    const user = userEvent.setup();
    state.items = [summary(), summary({ agent_id: 'agt_2', name: 'second', approval_tools: [], revision: 1 })];
    const onChange = vi.fn();
    wrap(<AgentNodeConfig value={{ agent_id: 'agt_1', input_key: 'q' }} onChange={onChange} />);
    const picker = await screen.findByTestId('agent-node-picker');
    await waitFor(() => expect(within(picker).getAllByRole('option')).toHaveLength(3));
    expect(within(picker).getByRole('option', { name: 'helper (rev 3)' })).toBeInTheDocument();
    expect(screen.getByTestId('agent-node-summary').textContent).toContain('needs approval');
    await user.selectOptions(picker, 'agt_2');
    expect(onChange).toHaveBeenLastCalledWith({ agent_id: 'agt_2', input_key: 'q' });
    await user.type(screen.getByTestId('agent-node-output-key'), 'a');
    expect(onChange).toHaveBeenLastCalledWith({ agent_id: 'agt_1', input_key: 'q', output_key: 'a' });
    expect(screen.queryByTestId('agent-node-missing')).toBeNull();
  });

  it('shows an unknown or deleted agent as missing', async () => {
    wrap(<AgentNodeConfig value={{ agent_id: 'agt_gone' }} onChange={() => undefined} />);
    expect((await screen.findByTestId('agent-node-missing')).textContent).toContain('agt_gone');
    const picker = screen.getByTestId('agent-node-picker') as HTMLSelectElement;
    expect(picker.value).toBe('agt_gone');
    expect(within(picker).getByRole('option', { name: 'agt_gone (missing)' })).toBeInTheDocument();
  });

  it('replaces the raw config form for nexura.agents.agent nodes in the inspector', async () => {
    const node = {
      id: 'ask',
      position: { x: 0, y: 0 },
      data: {
        nodeId: 'ask',
        label: 'ask',
        handler: 'nexura.agents.agent',
        module_id: 'nexura.agents.agent',
        requires_approval: false,
        config: { agent_id: 'agt_1' },
        input_mapping: {},
        output_mapping: {},
        isEntry: true,
      },
    };
    const noop = () => undefined;
    wrap(
      <NodeInspector
        selectedNode={node}
        allNodes={[node]}
        allEdges={[]}
        entryNode="ask"
        onUpdateNodeData={noop}
        onRenameNode={noop}
        onSetEntryNode={noop}
        onDeleteNode={noop}
        onSetDefaultTransition={noop}
        onSetBranchTransition={noop}
        onClose={noop}
      />,
    );
    expect(await screen.findByTestId('agent-node-config')).toBeInTheDocument();
    await waitFor(() => expect((screen.getByTestId('agent-node-picker') as HTMLSelectElement).value).toBe('agt_1'));
    expect(screen.queryByText('Module Parameters')).toBeNull();
  });
});

// ------------------------------------------------------------------ approvals

describe('approval details of an agent tool call', () => {
  const pending = {
    approval_id: 'ap-1',
    node_id: 'ask',
    details: {
      kind: 'agent_tool_calls',
      message: "Agent 'filer' wants to run demo_ticket_create",
      agent_id: 'agt_9',
      agent_name: 'filer',
      agent_revision: 2,
      node_id: 'ask',
      calls: [
        { id: 'call_1', tool: 'demo_ticket_create', arguments: { title: '<b>Printer</b>', priority: 2 }, digest: 'DIGEST_abc' },
      ],
      resume: '{"INTERNAL_RESUME_SNAPSHOT": "messages and secrets"}',
    },
  };

  it('shows agent name, revision and each call as formatted text', () => {
    const { container } = render(<ApprovalDetailsPanel pendingApproval={pending} />);
    expect(screen.getByTestId('approval-agent-name').textContent).toContain('filer');
    expect(screen.getByTestId('approval-agent-revision').textContent).toContain('2');
    expect(screen.getByTestId('approval-agent-call-tool').textContent).toBe('demo_ticket_create');
    expect(screen.getByTestId('approval-agent-call-arguments').textContent).toBe(
      JSON.stringify({ title: '<b>Printer</b>', priority: 2 }, null, 2),
    );
    expect(container.querySelector('b')).toBeNull();
    // Not the mapped-items view.
    expect(screen.queryByTestId('approval-item-count')).toBeNull();
  });

  it('never shows the internal resume field or call digests', () => {
    const { container } = render(<ApprovalDetailsPanel pendingApproval={pending} />);
    expect(container.textContent).not.toContain('INTERNAL_RESUME_SNAPSHOT');
    expect(container.textContent).not.toContain('resume');
    expect(container.textContent).not.toContain('DIGEST_abc');
    expect(JSON.stringify(describeAgentToolCalls(pending))).not.toContain('INTERNAL_RESUME_SNAPSHOT');
  });
});

// ------------------------------------------------------------------ playground

async function* events(list: ChatEvent[]) {
  for (const e of list) yield e;
}

describe('playground agent selector', () => {
  it('sends agent_id without a model, hides the model select and lists refused tool calls', async () => {
    const user = userEvent.setup();
    sessionsApi.chatStream.mockImplementation(() =>
      events([
        {
          kind: 'chat_completed',
          seq: 1,
          response: {
            session_id: 's1',
            revision: 1,
            messages: [
              { role: 'user', content: 'file it' },
              { role: 'assistant', content: 'I need approval.' },
            ],
            reply: 'I need approval.',
            model: 'fake-deterministic',
            delivery: 'final_only',
            tool_executions: [],
            usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 },
            latency_ms: 1,
            agent_id: 'agt_1',
            refused_tool_calls: [{ id: 'call_1', tool: 'demo_ticket_create' }],
          },
        },
      ]),
    );
    wrap(<ChatPlayground />);
    const agentSelect = await screen.findByTestId('agent-select');
    await waitFor(() => expect(within(agentSelect).getAllByRole('option')).toHaveLength(2));
    expect(screen.getByTestId('model-select')).toBeInTheDocument();
    await user.selectOptions(agentSelect, 'agt_1');
    expect(screen.queryByTestId('model-select')).toBeNull();
    expect(screen.getByTestId('model-badge').textContent).toContain('helper');

    await user.type(screen.getByTestId('chat-input-textarea'), 'file it');
    await user.click(screen.getByTestId('send-message-button'));
    await waitFor(() => expect(sessionsApi.chatStream).toHaveBeenCalledTimes(1));
    const body = sessionsApi.chatStream.mock.calls[0][1];
    expect(body).toEqual({ message: 'file it', language: 'en', agent_id: 'agt_1' });
    expect(body).not.toHaveProperty('model');
    const refused = await screen.findByTestId('refused-tool-calls');
    expect(within(refused).getByTestId('refused-tool-call').textContent).toBe('demo_ticket_create');
  });

  it('explains AGENT_TURN_LIMIT', async () => {
    const user = userEvent.setup();
    sessionsApi.chatStream.mockImplementation(() =>
      events([{ kind: 'chat_failed', seq: 1, code: 'AGENT_TURN_LIMIT', message: 'turn limit', persisted: false }]),
    );
    wrap(<ChatPlayground />);
    const agentSelect = await screen.findByTestId('agent-select');
    await waitFor(() => expect(within(agentSelect).getAllByRole('option')).toHaveLength(2));
    await user.selectOptions(agentSelect, 'agt_1');
    await user.type(screen.getByTestId('chat-input-textarea'), 'loop');
    await user.click(screen.getByTestId('send-message-button'));
    expect((await screen.findByTestId('agent-turn-limit')).textContent).toMatch(/turn limit/);
    expect(screen.getByTestId('turn-interrupted').textContent).toContain('AGENT_TURN_LIMIT');
  });

  it('keeps the demo (model, no agent_id) when no agent is chosen', async () => {
    const user = userEvent.setup();
    sessionsApi.chatStream.mockImplementation(() => events([]));
    wrap(<ChatPlayground />);
    await screen.findByTestId('agent-select');
    await user.type(screen.getByTestId('chat-input-textarea'), 'hi');
    await user.click(screen.getByTestId('send-message-button'));
    await waitFor(() => expect(sessionsApi.chatStream).toHaveBeenCalledTimes(1));
    const body = sessionsApi.chatStream.mock.calls[0][1];
    expect(body).toEqual({ message: 'hi', language: 'en', model: 'fake-deterministic' });
  });
});

// ------------------------------------------------------------------ evals and red-team

describe('evals targets and red-team against a saved agent', () => {
  beforeEach(() => {
    state.request = (method, path) => {
      if (path.endsWith('/datasets')) return { datasets: [] };
      if (path.endsWith('/runs')) return { runs: [] };
      if (path.endsWith('/targets')) {
        return {
          targets: [
            { kind: 'workflow', id: 'wf-1', name: 'Support', version: 'rev_1' },
            { kind: 'agent', id: 'local-fake', name: 'Local fake agent (synthetic)' },
            { kind: 'agent', id: 'agent:agt_1', name: 'helper', version: 'rev_3', saved: true },
          ],
        };
      }
      if (method === 'POST' && path.endsWith('/redteam')) {
        return {
          agent_id: 'agt_1',
          agent_version: 'rev_3',
          model: { name: 'fake-deterministic', offline: true },
          write_tools: ['demo_ticket_create'],
          synthetic_write_tool: false,
          scorers: ['no_canary_leak', 'no_write_without_approval'],
          ok: true,
          summary: { total: 1, passed: 1, failed: 0, errors: 0, paused: 0, pass_rate: 1, mean_score: 1, latency_seconds: 0 },
          cases: [{ name: 'injection-1', attack: 'injection', status: 'passed', safe: true, scores: [], reason: '' }],
        };
      }
      return {};
    };
  });

  it('shows saved agents with their revision in the run target picker', async () => {
    const user = userEvent.setup();
    wrap(<StudioEvalsPanel />);
    await user.click(await screen.findByTestId('studio-evals-tab-run'));
    const select = await screen.findByTestId('studio-evals-run-target');
    await waitFor(() => expect(within(select).getAllByRole('option')).toHaveLength(3));
    const saved = within(select).getByRole('option', { name: /Saved agent: helper \(rev_3\)/ });
    expect((saved as HTMLOptionElement).value).toBe('agent:agent:agt_1');
  });

  it('runs the red-team pack with {agent_id} for a saved agent', async () => {
    const user = userEvent.setup();
    wrap(<StudioEvalsPanel />);
    await user.click(await screen.findByTestId('studio-evals-tab-redteam'));
    const select = await screen.findByTestId('studio-evals-redteam-target');
    await waitFor(() => expect(within(select).getAllByRole('option')).toHaveLength(2));
    // Workflows stay the default; the local fake (not saved) is not offered.
    expect((select as HTMLSelectElement).value).toBe('workflow:wf-1');
    await user.selectOptions(select, 'agent:agt_1');
    expect(screen.getByTestId('studio-evals-redteam-agent-note')).toBeInTheDocument();
    await user.click(screen.getByTestId('studio-evals-redteam-run'));
    await waitFor(() => expect(requests.some((r) => r.method === 'POST' && r.path.endsWith('/redteam'))).toBe(true));
    const post = requests.find((r) => r.method === 'POST' && r.path.endsWith('/redteam'));
    expect(post?.body).toEqual({ agent_id: 'agt_1', write_tools: [] });
    expect((await screen.findByTestId('studio-evals-redteam-target-version')).textContent).toContain('agt_1 (rev_3)');
  });
});
