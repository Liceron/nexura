// P26 stage 2 review regression (promoted from .autopilot/p26s2/repros). P26 stage 2 review repros (vitest): Agents page roles and stale responses, agent node picker
// when the list fails, playground refusals/turn-limit text, form bounds vs the server. Each
// test asserts the correct behaviour and fails on the code as reviewed. Mocked API; no network.
// Promises are resolved by hand (no timers), so ordering is deterministic.
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { createElement as h } from 'react';
import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

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
const OPTIONS = {
  models: [{ name: 'fake-deterministic', provider: 'local', label: 'Offline', offline: true, streaming: false, default: true }],
  tools: [],
  knowledge_collections: [],
  skills: [],
  node_module_id: 'nexura.agents.agent',
  limits: LIMITS,
};

function summary(agent_id: string, name: string) {
  return {
    agent_id,
    name,
    description: '',
    model: 'fake-deterministic',
    tool_count: 0,
    tools: [],
    approval_tools: [],
    revision: 1,
    updated_at: 1,
  };
}

function record(agent_id: string, name: string, revision = 1) {
  return {
    agent_id,
    workspace_id: 'w',
    revision,
    spec: {
      name,
      description: '',
      instructions: '',
      model: 'fake-deterministic',
      tools: [],
      knowledge: [],
      skills: [],
      skills_label: 'production',
      output_schema: null,
      limits: { max_turns: 6, max_tool_calls: 10, timeout_seconds: 60 },
    },
    created_at: 1,
    updated_at: 1,
    created_by: 'p',
    updated_by: 'p',
    tools: [],
  };
}

function deferred<T>() {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

const state: { items: ReturnType<typeof summary>[] } = { items: [] };
const agentsApi = {
  list: vi.fn(async () => ({ items: state.items, total: state.items.length, limits: LIMITS })),
  options: vi.fn(async () => OPTIONS),
  get: vi.fn(async (id: string) => record(id, 'helper')),
  create: vi.fn(),
  update: vi.fn(),
  delete: vi.fn(async () => ({ deleted: true })),
};
const sessionsApi = {
  list: vi.fn(async () => ({ items: [] as Array<{ session_id: string; title?: string }> })),
  models: vi.fn(async () => OPTIONS.models),
  create: vi.fn(async () => ({ session_id: 's1' })),
  get: vi.fn(async () => ({ messages: [] })),
  delete: vi.fn(),
  chatStream: vi.fn(),
};
const members = { getMyMembership: vi.fn(async () => ({ role: 'admin' })) };
const client = {
  agents: agentsApi,
  sessions: sessionsApi,
  members,
  // EDITIONS_PLAN step 7: Studio reads the role through request(); served by the fake above.
  request: vi.fn((_method: string, path: string) =>
    path.endsWith('/my-membership') ? members.getMyMembership() : Promise.resolve({}),
  ),
};
const api = {
  manager: { client, clearCache: () => undefined },
  connection: { status: 'connected', workspace: 'w' },
};
// vi.doMock + dynamic imports: vi.mock hoisting only recognises a bare 'vitest' import.
vi.doMock('../../src/api/context', () => ({ useApi: () => api }));

const { I18nProvider } = await import('../../src/i18n');
const { AgentsView } = await import('../../src/features/agents/AgentsView');
const { AgentNodeConfig } = await import('../../src/features/agents/AgentNodeConfig');
const { ChatPlayground } = await import('../../src/features/chat/ChatPlayground');
const { validateDraft, emptyDraft } = await import('../../src/features/agents/agentForm');
// PKG-03: open-source Studio has no member client; the role comes from an extension's source.
const { installRequestMembershipSource } = await import('./membershipSource');
installRequestMembershipSource();

const wrap = (el: ReturnType<typeof h>) => render(h(I18nProvider, null, el));

async function* events(list: Array<Record<string, unknown>>) {
  for (const e of list) yield e;
}

async function flush() {
  await act(async () => {
    for (let i = 0; i < 10; i += 1) await Promise.resolve();
  });
}

beforeEach(() => {
  state.items = [summary('agt_1', 'helper')];
  api.connection = { status: 'connected', workspace: 'w' };
  for (const fn of [...Object.values(agentsApi), ...Object.values(sessionsApi)]) fn.mockReset();
  agentsApi.list.mockImplementation(async () => ({ items: state.items, total: state.items.length, limits: LIMITS }));
  agentsApi.options.mockImplementation(async () => OPTIONS);
  agentsApi.get.mockImplementation(async (id: string) => record(id, 'helper'));
  agentsApi.delete.mockImplementation(async () => ({ deleted: true }));
  sessionsApi.list.mockImplementation(async () => ({ items: [] }));
  sessionsApi.models.mockImplementation(async () => OPTIONS.models);
  sessionsApi.create.mockImplementation(async () => ({ session_id: 's1' }));
  sessionsApi.get.mockImplementation(async () => ({ messages: [] }));
  members.getMyMembership.mockReset();
  members.getMyMembership.mockImplementation(async () => ({ role: 'admin' }));
  localStorage.clear();
  Element.prototype.scrollIntoView = vi.fn();
});

describe('Agents page roles', () => {
  it('keeps create and delete disabled until the membership is known', async () => {
    // Membership request still in flight (slow server): the role is not known yet.
    members.getMyMembership.mockImplementation(() => new Promise(() => undefined));
    const user = userEvent.setup();
    wrap(h(AgentsView));
    await screen.findByTestId('agent-row-helper');
    // Same rule as useCanEdit() in evaluations/workflowTests.ts: false until known.
    expect(screen.getByTestId('agents-new')).toBeDisabled();
    await user.click(screen.getByTestId('agent-row-helper'));
    await screen.findByTestId('agent-name');
    expect(screen.queryByTestId('agent-delete')).toBeNull();
    expect(screen.queryByTestId('agent-save')).toBeNull();
  });
});

describe('Agents page stale responses', () => {
  it('a slow GET of the first agent does not replace the agent opened after it', async () => {
    state.items = [summary('agt_a', 'alpha'), summary('agt_b', 'beta')];
    const a = deferred<ReturnType<typeof record>>();
    const b = deferred<ReturnType<typeof record>>();
    agentsApi.get.mockImplementation((id: string) => (id === 'agt_a' ? a.promise : b.promise));
    const user = userEvent.setup();
    wrap(h(AgentsView));
    await user.click(await screen.findByTestId('agent-row-alpha'));
    await user.click(screen.getByTestId('agent-row-beta'));
    await act(async () => b.resolve(record('agt_b', 'beta')));
    await waitFor(() => expect((screen.getByTestId('agent-name') as HTMLInputElement).value).toBe('beta'));
    await act(async () => a.resolve(record('agt_a', 'alpha')));
    await flush();
    // The user's last choice wins.
    expect((screen.getByTestId('agent-name') as HTMLInputElement).value).toBe('beta');
    expect(screen.getByTestId('agent-row-beta')).toHaveAttribute('aria-pressed', 'true');
  });

  it('a slow first list does not hide an agent created after it', async () => {
    const first = deferred<{ items: ReturnType<typeof summary>[]; total: number; limits: typeof LIMITS }>();
    const fresh = summary('agt_new', 'fresh');
    let calls = 0;
    agentsApi.list.mockImplementation(() => {
      calls += 1;
      return calls === 1 ? first.promise : Promise.resolve({ items: [fresh], total: 1, limits: LIMITS });
    });
    agentsApi.create.mockImplementation(async () => record('agt_new', 'fresh'));
    const user = userEvent.setup();
    wrap(h(AgentsView));
    await waitFor(() => expect(screen.getByTestId('agents-new')).not.toBeDisabled());
    await user.click(screen.getByTestId('agents-new'));
    await user.type(screen.getByTestId('agent-name'), 'fresh');
    await user.click(screen.getByTestId('agent-save'));
    await screen.findByTestId('agent-row-fresh');
    // The page's initial list (sent before the create) answers last, without the new agent.
    await act(async () => first.resolve({ items: [], total: 0, limits: LIMITS }));
    await flush();
    expect(screen.queryByTestId('agent-row-fresh')).not.toBeNull();
  });

  it('switching workspace closes the agent opened in the previous workspace', async () => {
    const user = userEvent.setup();
    const view = wrap(h(AgentsView));
    await user.click(await screen.findByTestId('agent-row-helper'));
    await screen.findByTestId('agent-name');
    // Workspace w2 has no agents; the open editor still targets agt_1 of workspace w.
    state.items = [];
    api.connection = { status: 'connected', workspace: 'w2' };
    view.rerender(h(I18nProvider, null, h(AgentsView)));
    await screen.findByTestId('agents-empty');
    await flush();
    expect(screen.queryByTestId('agent-name')).toBeNull();
    expect(screen.queryByTestId('agent-delete')).toBeNull();
  });
});

describe('agent node picker', () => {
  it('does not call an agent deleted when the agent list failed to load', async () => {
    agentsApi.list.mockImplementation(async () => {
      throw new Error('network down');
    });
    wrap(h(AgentNodeConfig, { value: { agent_id: 'agt_1' }, onChange: () => undefined }));
    await screen.findByText(/Could not load agents/);
    // The list is unknown: the agent may well exist. Claiming it is deleted (and that the
    // node fails when it runs) is false.
    expect(screen.queryByTestId('agent-node-missing')).toBeNull();
  });
});

describe('playground', () => {
  function completed(sessionId: string) {
    return {
      kind: 'chat_completed',
      seq: 1,
      response: {
        session_id: sessionId,
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
    };
  }

  it("another session's transcript does not show the previous session's refused calls", async () => {
    sessionsApi.list.mockImplementation(async () => ({
      items: [
        { session_id: 's1', title: 'one' },
        { session_id: 's2', title: 'two' },
      ],
    }));
    sessionsApi.chatStream.mockImplementation(() => events([completed('s1')]));
    const user = userEvent.setup();
    wrap(h(ChatPlayground));
    const agentSelect = await screen.findByTestId('agent-select');
    await waitFor(() => expect(within(agentSelect).getAllByRole('option')).toHaveLength(2));
    await user.selectOptions(agentSelect, 'agt_1');
    await screen.findByTestId('session-item-s1');
    await user.type(screen.getByTestId('chat-input-textarea'), 'file it');
    await user.click(screen.getByTestId('send-message-button'));
    await screen.findByTestId('refused-tool-calls');
    await user.click(screen.getByTestId('session-item-s2'));
    await waitFor(() => expect(sessionsApi.get).toHaveBeenLastCalledWith('s2'));
    await flush();
    expect(screen.queryByTestId('refused-tool-calls')).toBeNull();
  });

  it('the saved-agent turn-limit explanation is not shown for the demo (no agent chosen)', async () => {
    sessionsApi.chatStream.mockImplementation(() =>
      events([{ kind: 'chat_failed', seq: 1, code: 'AGENT_TURN_LIMIT', message: 'turn limit', persisted: false }]),
    );
    const user = userEvent.setup();
    wrap(h(ChatPlayground));
    await screen.findByTestId('agent-select');
    await user.type(screen.getByTestId('chat-input-textarea'), 'loop');
    await user.click(screen.getByTestId('send-message-button'));
    await screen.findByTestId('turn-interrupted');
    // The text is about a saved agent ("run the agent in a workflow ... raise its max turns").
    expect(screen.queryByTestId('agent-turn-limit')).toBeNull();
  });
});

describe('form bounds match the server', () => {
  it('counts instructions in characters (code points) like the server, not UTF-16 units', () => {
    // 10,001 emoji: 10,001 characters for the server (limit 20,000), 20,002 UTF-16 units.
    const draft = { ...emptyDraft(), name: 'emoji', instructions: '\u{1F600}'.repeat(10_001) };
    const { errors } = validateDraft(draft, LIMITS);
    expect(errors.instructions).toBeUndefined();
  });

  it("measures the output schema the way the server does (json.dumps, ', ' and ': ')", () => {
    // Compact JSON is under 16,000 characters; Python's json.dumps (the server's measure) is over.
    const properties: Record<string, { type: string }> = {};
    for (let i = 0; i < 600; i += 1) properties[`p${String(i).padStart(4, '0')}`] = { type: 'string' };
    const schema = { type: 'object', properties };
    const compact = JSON.stringify(schema).length;
    const pythonStyle = JSON.stringify(schema).replace(/":/g, '": ').replace(/,"/g, ', "').length;
    expect(compact).toBeLessThan(16_000);
    expect(pythonStyle).toBeGreaterThan(16_000);
    const draft = { ...emptyDraft(), name: 'big', output_schema: JSON.stringify(schema) };
    const { errors } = validateDraft(draft, LIMITS);
    expect(errors.output_schema).toBeDefined();
  });
});
