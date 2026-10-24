// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
// P39 G4: the Playground's live reply for an agent with output guardrails. Explicit local
// mock API (the round 4 fixture): no service, provider, model or network calls. The events
// below are a scripted mock of the service's native SSE, labelled as such.
import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { I18nProvider } from '../../src/i18n';
import en from '../../src/i18n/en.json';
import hi from '../../src/i18n/hi.json';

const fixture = vi.hoisted(() => {
  const skillVersion = (version: number, instructions: string) => ({
    name: 'refunds', version, labels: version === 1 ? ['production'] : ['latest'], description: 'Refund rules.',
    instructions, tools: ['lookup_order'], resources: {}, sha256: 'x',
  });
  const client = {
    sessions: {
      list: vi.fn(async () => ({ items: [] as Array<Record<string, unknown>> })),
      models: vi.fn(async () => [] as Array<Record<string, unknown>>),
      create: vi.fn(async () => ({ session_id: 's1' })),
      get: vi.fn(async (_id: string) => ({ messages: [] as Array<Record<string, unknown>> })),
      delete: vi.fn(),
      chatStream: vi.fn(),
    },
    agents: {
      list: vi.fn(async () => ({ items: [] as Array<Record<string, unknown>>, total: 0, limits: null })),
      options: vi.fn(async () => ({ models: [{ name: 'fake-deterministic', provider: 'local', label: 'Offline', offline: true, streaming: false, default: true }], tools: [], knowledge_collections: [], skills: [], node_module_id: 'nexura.agents.agent', limits: null })),
      get: vi.fn(), create: vi.fn(), update: vi.fn(), delete: vi.fn(),
    },
    knowledge: {
      listCollections: vi.fn(async () => ({ collections: ['default'] })),
      listDocuments: vi.fn(async () => ({ items: [] as Array<Record<string, unknown>> })),
      retrieve: vi.fn(), answer: vi.fn(), uploadDocument: vi.fn(), deleteDocument: vi.fn(), createCollection: vi.fn(),
    },
    runs: { start: vi.fn() },
    request: vi.fn(async (_method: string, path: string): Promise<unknown> => {
      if (path === '/knowledge/limits') return { max_file_bytes: 5000000, max_workspace_bytes: 50000000, max_top_k: 50, workspace_used_bytes: 0, is_test_fixture: true };
      if (path === '/skills') return { items: [{ name: 'refunds', description: 'Refund rules.', latest_version: 2, version_count: 2, labels: { production: 1, latest: 2 } }], total: 1 };
      if (path === '/skills/refunds/versions') return { name: 'refunds', description: 'Refund rules.', latest_version: 2, version_count: 2, labels: { production: 1, latest: 2 }, versions: [skillVersion(1, '<b>v1</b>'), skillVersion(2, '<img src=x onerror="window.__r4=1"> v2')] };
      if (path.endsWith('/my-membership')) return { role: 'admin' };
      return {};
    }),
  };
  const api = { manager: { client, clearCache: vi.fn() }, connection: { status: 'connected', workspace: 'local-r4' } };
  return { api, client };
});
vi.mock('../../src/api/context', () => ({ useApi: () => fixture.api }));
vi.mock('../../src/api/membership', () => ({ getMyMembership: async () => ({ role: 'admin' }) }));

const { ChatPlayground } = await import('../../src/features/chat/ChatPlayground');

const show = (node: React.ReactNode) => render(<I18nProvider>{node}</I18nProvider>);
function deferred() {
  let resolve!: () => void;
  const promise = new Promise<void>((r) => { resolve = r; });
  return { promise, resolve };
}
const stream = (events: Array<Record<string, unknown> | Promise<void>>) =>
  async function* (_id: string, _body: unknown, init?: { signal?: AbortSignal }) {
    for (const ev of events) {
      if (ev instanceof Promise) {
        await Promise.race([ev, new Promise((_, reject) => init?.signal?.addEventListener('abort', () => reject(new Error('aborted'))))]);
        continue;
      }
      yield ev;
    }
  };
const completed = (text: string) => ({ kind: 'chat_completed', seq: 9, response: {
  session_id: 's1', revision: 2, reply: text, model: 'mock-guarded', delivery: 'final_only', tool_executions: [],
  usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2, is_complete: true }, latency_ms: 1,
  messages: [{ role: 'user', content: 'hi' }, { role: 'assistant', content: text }],
} });
const run = (seq: number, type: string, data: Record<string, unknown>) => ({ kind: 'run_event', seq, type, timestamp: 1, data });

beforeEach(() => {
  vi.stubGlobal('matchMedia', vi.fn((query: string) => ({
    matches: !query.includes('max-width'), addEventListener: vi.fn(), removeEventListener: vi.fn(),
  })));
  localStorage.clear();
  vi.clearAllMocks();
  Element.prototype.scrollIntoView = vi.fn();
});
afterEach(cleanup);

async function send() {
  const user = userEvent.setup();
  show(<ChatPlayground />);
  await user.type(await screen.findByLabelText('Message'), 'hi');
  await user.click(screen.getByTestId('send-message-button'));
}

describe('Playground with output guardrails (P39 G4)', () => {
  it('shows a checking state for a held reply, then only the checked text', async () => {
    const checking = deferred();
    const saved = deferred();
    fixture.client.sessions.chatStream.mockImplementation(stream([
      run(1, 'MODEL_STARTED', { turn: 1, stream_guard: 'hold' }),
      checking.promise,
      run(2, 'MODEL_COMPLETED', { turn: 1 }),
      run(3, 'GUARDRAIL_DECISION', { stage: 'output', action: 'scrub' }),
      run(4, 'FINAL_OUTPUT', { output: 'Your card is [card removed].' }),
      saved.promise,
      completed('Your card is [card removed].'),
    ]));
    await send();
    const bubble = await screen.findByTestId('message-bubble-checking');
    expect(bubble).toHaveTextContent(en.chat.checkingReply);
    expect(bubble).toHaveTextContent(en.chat.checkingReplyNote);
    expect(bubble).toHaveAttribute('aria-busy', 'true');
    expect(screen.getByTestId('turn-activity')).toHaveTextContent(en.chat.checkingReply);
    expect(screen.queryByTestId('message-bubble-streaming')).toBeNull();
    await act(async () => checking.resolve());
    const live = await screen.findByTestId('message-bubble-streaming');
    expect(live).toHaveTextContent('Your card is [card removed].');
    expect(screen.queryByTestId('message-bubble-checking')).toBeNull();
    expect(screen.queryByTestId('reply-checking-rest')).toBeNull();
    await act(async () => saved.resolve());
    await waitFor(() => expect(screen.queryByTestId('message-bubble-streaming')).toBeNull());
    expect(screen.getByTestId('message-bubble-assistant')).toHaveTextContent('Your card is [card removed].');
  });

  it('shows checked windows of a chunked reply with a note that the rest is being checked', async () => {
    const more = deferred();
    fixture.client.sessions.chatStream.mockImplementation(stream([
      run(1, 'MODEL_STARTED', { turn: 1, stream_guard: 'chunked' }),
      run(2, 'TEXT_DELTA', { turn: 1, text: 'Checked part. ', checked: true }),
      more.promise,
      run(3, 'TEXT_DELTA', { turn: 1, text: 'Rest.', checked: true }),
      run(4, 'FINAL_OUTPUT', { output: 'Checked part. Rest.' }),
      completed('Checked part. Rest.'),
    ]));
    await send();
    const live = await screen.findByTestId('message-bubble-streaming');
    expect(live).toHaveTextContent('Checked part.');
    expect(screen.getByTestId('reply-checking-rest')).toHaveTextContent(en.chat.checkingRest);
    await act(async () => more.resolve());
    await waitFor(() => expect(screen.getByTestId('message-bubble-assistant')).toHaveTextContent('Checked part. Rest.'));
  });

  it('discards shown text when a guardrail stops the reply', async () => {
    const tripped = deferred();
    fixture.client.sessions.chatStream.mockImplementation(stream([
      run(1, 'MODEL_STARTED', { turn: 1, stream_guard: 'chunked' }),
      run(2, 'TEXT_DELTA', { turn: 1, text: 'First window. ', checked: true }),
      tripped.promise,
      run(3, 'GUARDRAIL_DECISION', { stage: 'output', action: 'fail' }),
      run(4, 'GUARDRAIL_TRIPPED', { stage: 'output', guardrail: 'judge', phase: 'chunk', shown: true }),
      run(5, 'RUN_FAILED', { error: 'Output guardrail tripped.' }),
      { kind: 'chat_failed', seq: 6, code: 'GuardrailTripwireTriggered', message: 'Output guardrail tripped.', persisted: false },
    ]));
    await send();
    expect(await screen.findByTestId('message-bubble-streaming')).toHaveTextContent('First window.');
    await act(async () => tripped.resolve());
    expect(await screen.findByTestId('turn-interrupted')).toBeInTheDocument();
    expect(screen.queryByTestId('message-bubble-streaming')).toBeNull();
    expect(screen.queryByTestId('message-bubble-checking')).toBeNull();
    expect(screen.getByTestId('chat-transcript')).not.toHaveTextContent('First window.');
  });

  it('keeps streaming an unguarded reply token by token', async () => {
    const gate = deferred();
    fixture.client.sessions.chatStream.mockImplementation(stream([
      run(1, 'MODEL_STARTED', { turn: 1 }),
      run(2, 'TEXT_DELTA', { turn: 1, text: 'Hel' }),
      gate.promise,
      completed('Hel'),
    ]));
    await send();
    expect(await screen.findByTestId('message-bubble-streaming')).toHaveTextContent('Hel');
    expect(screen.queryByTestId('message-bubble-checking')).toBeNull();
    expect(screen.queryByTestId('reply-checking-rest')).toBeNull();
    await act(async () => gate.resolve());
  });

  it('a tool-call turn of a guarded agent ends its checking state', async () => {
    const afterTool = deferred();
    fixture.client.sessions.chatStream.mockImplementation(stream([
      run(1, 'MODEL_STARTED', { turn: 1, stream_guard: 'hold' }),
      run(2, 'TOOL_STARTED', { tool_name: 'lookup' }),
      afterTool.promise,
      completed('done'),
    ]));
    await send();
    await waitFor(() => expect(screen.getByTestId('turn-activity')).toHaveTextContent(/lookup/));
    expect(screen.queryByTestId('message-bubble-checking')).toBeNull();
    await act(async () => afterTool.resolve());
  });

  it('has Hindi strings for every checking message', () => {
    for (const key of ['checkingReply', 'checkingReplyNote', 'checkingRest', 'replyBlocked'] as const) {
      expect(en.chat[key]).toBeTruthy();
      expect(hi.chat[key]).toBeTruthy();
      expect(hi.chat[key]).not.toBe(en.chat[key]);
    }
  });
});
