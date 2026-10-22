// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
// Round 4 (chat, knowledge and authoring resources). Explicit local mock API: no service,
// provider, model or network calls. The streamed text below is a scripted mock of the
// service's TEXT_DELTA events, labelled as such; nothing here claims live model streaming.
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
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
const { KnowledgeView } = await import('../../src/features/knowledge/KnowledgeView');
const { AgentsView } = await import('../../src/features/agents/AgentsView');
const { SkillsView } = await import('../../src/features/skills/SkillsView');
const { LocalOnboarding } = await import('../../src/features/onboarding/LocalOnboarding');

const show = (node: React.ReactNode) => render(<I18nProvider>{node}</I18nProvider>);
function deferred() {
  let resolve!: () => void;
  const promise = new Promise<void>((r) => { resolve = r; });
  return { promise, resolve };
}

function setPhone(phone: boolean) {
  vi.stubGlobal('matchMedia', vi.fn((query: string) => ({
    matches: query.includes('max-width') ? phone : !phone, addEventListener: vi.fn(), removeEventListener: vi.fn(),
  })));
}
const nextFrame = () => act(async () => { await new Promise((resolve) => requestAnimationFrame(() => resolve(undefined))); });

beforeEach(() => {
  setPhone(false);
  localStorage.clear();
  vi.clearAllMocks();
  Element.prototype.scrollIntoView = vi.fn();
});
afterEach(cleanup);

describe('Playground', () => {
  it('labels the offline fake model once, offers three examples and keeps one primary action', async () => {
    show(<ChatPlayground />);
    expect(await screen.findByRole('heading', { level: 1, name: 'Playground' })).toBeInTheDocument();
    expect(screen.getByTestId('model-badge')).toHaveTextContent('Offline fake model');
    expect(screen.getAllByText('Offline fake model')).toHaveLength(1);
    expect(screen.getByLabelText('Chat model')).toHaveValue('fake-deterministic');
    expect(screen.getByTestId('delivery-badge')).toHaveTextContent(/final-only/i);
    expect(screen.getByRole('heading', { name: 'Start a conversation' })).toBeInTheDocument();
    const examples = screen.getAllByTestId(/^quick-prompt-/);
    expect(examples).toHaveLength(3);
    fireEvent.click(screen.getByTestId('quick-prompt-hi-greet'));
    expect(screen.getByLabelText('Message')).toHaveValue('नमस्ते! आप मेरी क्या मदद कर सकते हैं?');
    expect(screen.getByRole('button', { name: 'हिन्दी' })).toHaveAttribute('aria-pressed', 'true');
    expect(document.querySelectorAll('.studio-button--primary')).toHaveLength(1);
    expect(screen.getByTestId('new-chat-button')).not.toHaveClass('studio-button--primary');
  });

  it('renders streamed reply text as it arrives, then replaces it with the saved transcript', async () => {
    const gate = deferred();
    const finish = deferred();
    fixture.client.sessions.chatStream.mockImplementation(async function* () {
      yield { kind: 'run_event', seq: 1, type: 'MODEL_STARTED', timestamp: 1, data: { turn: 1 } };
      yield { kind: 'run_event', seq: 2, type: 'TEXT_DELTA', timestamp: 1, data: { text: 'Hel' } };
      yield { kind: 'run_event', seq: 3, type: 'TEXT_DELTA', timestamp: 1, data: { text: 'lo' } };
      await gate.promise;
      yield { kind: 'run_event', seq: 4, type: 'TEXT_DELTA', timestamp: 1, data: { text: ' there' } };
      await finish.promise;
      yield { kind: 'chat_completed', seq: 5, response: {
        session_id: 's1', revision: 1, reply: 'Hello there', model: 'mock-streaming', delivery: 'streamed', tool_executions: [],
        usage: { prompt_tokens: 1, completion_tokens: 2, total_tokens: 3, is_complete: true }, latency_ms: 4,
        messages: [{ role: 'user', content: 'hi' }, { role: 'assistant', content: 'Hello there' }],
      } };
    });
    const user = userEvent.setup();
    show(<ChatPlayground />);
    await user.type(await screen.findByLabelText('Message'), 'hi');
    await user.click(screen.getByTestId('send-message-button'));
    const live = await screen.findByTestId('message-bubble-streaming');
    await waitFor(() => expect(live).toHaveTextContent('Hello'));
    expect(live).toHaveAttribute('aria-busy', 'true');
    expect(screen.getByTestId('turn-activity')).toHaveTextContent('Writing reply');
    expect(screen.getByTestId('stop-turn-button')).toBeInTheDocument();
    expect(screen.queryByTestId('send-message-button')).toBeNull();
    await act(async () => gate.resolve());
    await waitFor(() => expect(screen.getByTestId('message-bubble-streaming')).toHaveTextContent('Hello there'));
    expect(screen.queryAllByTestId('message-bubble-assistant')).toHaveLength(0);
    await act(async () => finish.resolve());
    await waitFor(() => expect(screen.queryByTestId('message-bubble-streaming')).toBeNull());
    expect(screen.getByTestId('message-bubble-assistant')).toHaveTextContent('Hello there');
    expect(screen.getByTestId('delivery-badge')).toHaveTextContent(/token by token/);
  });

  it('drops partial streamed text when the turn fails, since nothing was saved', async () => {
    fixture.client.sessions.chatStream.mockImplementation(async function* () {
      yield { kind: 'run_event', seq: 1, type: 'TEXT_DELTA', timestamp: 1, data: { text: 'Partial' } };
      yield { kind: 'chat_failed', seq: 2, code: 'MODEL_ERROR', message: 'boom', persisted: false };
    });
    const user = userEvent.setup();
    show(<ChatPlayground />);
    await user.type(await screen.findByLabelText('Message'), 'hi');
    await user.click(screen.getByTestId('send-message-button'));
    expect(await screen.findByTestId('turn-interrupted')).toHaveTextContent('Nothing from this turn was saved');
    expect(screen.queryByTestId('message-bubble-streaming')).toBeNull();
    expect(screen.getByTestId('chat-transcript')).not.toHaveTextContent('Partial');
  });

  it('opens tool payloads only on request, so their content is not in the page beforehand', async () => {
    fixture.client.sessions.chatStream.mockImplementation(async function* () {
      yield { kind: 'chat_completed', seq: 1, response: {
        session_id: 's1', revision: 1, reply: '100', model: 'fake-deterministic', delivery: 'final_only',
        tool_executions: [{ tool: 'calculate', arguments: { expression: '25 * 4' }, output: { result: 'R-100' }, timestamp: 1 }],
        usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2, is_complete: true }, latency_ms: 1,
        messages: [{ role: 'user', content: 'calc' }, { role: 'assistant', content: '100' }],
      } };
    });
    const user = userEvent.setup();
    show(<ChatPlayground />);
    await user.type(await screen.findByLabelText('Message'), 'calc');
    await user.click(screen.getByTestId('send-message-button'));
    const trace = await screen.findByTestId('tool-trace-calculate');
    const toggle = within(trace).getByRole('button');
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(trace).not.toHaveTextContent('R-100');
    await user.click(toggle);
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    expect(trace).toHaveTextContent('R-100');
    expect(screen.getByTestId('metrics-bar')).toHaveTextContent(/Tokens:.*1 prompt/);
  });
});

describe('Playground on phones', () => {
  it('keeps the strip compact: agent, model and reply language move into Settings', async () => {
    setPhone(true);
    show(<ChatPlayground />);
    const heading = await screen.findByRole('heading', { level: 1, name: 'Playground' });
    // The page h1 is the first heading; the Conversations drawer title is not repeated in the pane.
    expect(screen.getAllByRole('heading')[0]).toBe(heading);
    const settings = screen.getByText('Settings').closest('details')!;
    expect(settings).not.toHaveAttribute('open');
    expect(settings).toContainElement(screen.getByTestId('agent-select'));
    expect(settings).toContainElement(screen.getByTestId('model-select'));
    expect(settings).toContainElement(screen.getByRole('group', { name: 'Reply language' }));
    // The offline label and delivery wording stay visible outside Settings.
    expect(settings).not.toContainElement(screen.getByTestId('model-badge'));
    expect(screen.getByTestId('model-badge')).toHaveTextContent('Offline fake model');
    expect(screen.getByLabelText('Message')).toHaveAttribute('placeholder', 'Type a message');
    fireEvent.click(screen.getByRole('button', { name: 'Conversations' }));
    const drawer = await screen.findByRole('dialog', { name: 'Conversations' });
    expect(within(drawer).getAllByText('Conversations')).toHaveLength(1);
    expect(within(drawer).getByTestId('new-chat-button')).toBeInTheDocument();
  });
});

describe('Playground turns and sessions', () => {
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
  const completed = (session: string, text: string) => ({ kind: 'chat_completed', seq: 9, response: {
    session_id: session, revision: 2, reply: text, model: 'mock-streaming', delivery: 'streamed', tool_executions: [],
    usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2, is_complete: true }, latency_ms: 1,
    messages: [{ role: 'user', content: 'hi' }, { role: 'assistant', content: text }],
  } });

  it('drops streamed text when the user stops the turn', async () => {
    const hold = deferred();
    fixture.client.sessions.chatStream.mockImplementation(stream([
      { kind: 'run_event', seq: 1, type: 'TEXT_DELTA', timestamp: 1, data: { text: 'Partial answer' } }, hold.promise,
    ]));
    const user = userEvent.setup();
    show(<ChatPlayground />);
    await user.type(await screen.findByLabelText('Message'), 'hi');
    await user.click(screen.getByTestId('send-message-button'));
    expect(await screen.findByTestId('message-bubble-streaming')).toHaveTextContent('Partial answer');
    await user.click(screen.getByTestId('stop-turn-button'));
    expect(await screen.findByTestId('turn-interrupted')).toHaveTextContent('Stopped by you');
    expect(screen.queryByTestId('message-bubble-streaming')).toBeNull();
    expect(screen.getByTestId('chat-transcript')).not.toHaveTextContent('Partial answer');
    expect(screen.getByTestId('send-message-button')).toBeInTheDocument();
  });

  it('clears the previous model turn text when the next model turn starts', async () => {
    const afterFirst = deferred();
    const afterSecond = deferred();
    fixture.client.sessions.chatStream.mockImplementation(stream([
      { kind: 'run_event', seq: 1, type: 'MODEL_STARTED', timestamp: 1, data: { turn: 1 } },
      { kind: 'run_event', seq: 2, type: 'TEXT_DELTA', timestamp: 1, data: { text: 'Let me look that up.' } },
      afterFirst.promise,
      { kind: 'run_event', seq: 3, type: 'TOOL_STARTED', timestamp: 1, data: { tool_name: 'lookup' } },
      { kind: 'run_event', seq: 4, type: 'MODEL_STARTED', timestamp: 1, data: { turn: 2 } },
      { kind: 'run_event', seq: 5, type: 'TEXT_DELTA', timestamp: 1, data: { text: 'Order shipped.' } },
      afterSecond.promise,
      completed('s1', 'Order shipped.'),
    ]));
    const user = userEvent.setup();
    show(<ChatPlayground />);
    await user.type(await screen.findByLabelText('Message'), 'hi');
    await user.click(screen.getByTestId('send-message-button'));
    expect(await screen.findByTestId('message-bubble-streaming')).toHaveTextContent('Let me look that up.');
    await act(async () => afterFirst.resolve());
    await waitFor(() => expect(screen.getByTestId('message-bubble-streaming')).toHaveTextContent('Order shipped.'));
    expect(screen.getByTestId('message-bubble-streaming')).not.toHaveTextContent('Let me look that up.');
    await act(async () => afterSecond.resolve());
    await waitFor(() => expect(screen.queryByTestId('message-bubble-streaming')).toBeNull());
  });

  it('keeps session controls disabled while a reply runs, with the reason', async () => {
    fixture.client.sessions.list.mockResolvedValue({ items: [
      { session_id: 's1', revision: 1, message_count: 1 }, { session_id: 's2', revision: 3, message_count: 4 },
    ] });
    const hold = deferred();
    fixture.client.sessions.chatStream.mockImplementation(stream([hold.promise, completed('s1', 'done')]));
    const user = userEvent.setup();
    show(<ChatPlayground />);
    expect(await screen.findByTestId('session-item-s1')).toHaveTextContent('1 message · rev 1');
    expect(screen.getByTestId('session-item-s2')).toHaveTextContent('4 messages · rev 3');
    await user.type(screen.getByLabelText('Message'), 'hi');
    await user.click(screen.getByTestId('send-message-button'));
    await screen.findByTestId('stop-turn-button');
    for (const id of ['session-item-s2', 'new-chat-button', 'delete-session-s1']) {
      expect(screen.getByTestId(id)).toBeDisabled();
    }
    expect(screen.getByTestId('session-item-s2')).toHaveAccessibleDescription('Stop the current reply to switch conversations.');
    await act(async () => hold.resolve());
    await waitFor(() => expect(screen.getByTestId('session-item-s2')).not.toBeDisabled());
  });
});

describe('Playground session race (hook)', () => {
  it("a turn's streamed text and saved transcript never reach a session opened during the turn", async () => {
    const { renderHook } = await import('@testing-library/react');
    const { useChatSession } = await import('../../src/features/chat/useChatSession');
    fixture.client.sessions.list.mockResolvedValue({ items: [
      { session_id: 's1', revision: 1, message_count: 0 }, { session_id: 's2', revision: 1, message_count: 1 },
    ] });
    fixture.client.sessions.get.mockImplementation(async (id: string) => ({
      messages: id === 's2' ? [{ role: 'user', content: 'B transcript' }] : [],
    }));
    const hold = deferred();
    let signal: AbortSignal | undefined;
    fixture.client.sessions.chatStream.mockImplementation(async function* (_id: string, _b: unknown, init?: { signal?: AbortSignal }) {
      signal = init?.signal;
      yield { kind: 'run_event', seq: 1, type: 'TEXT_DELTA', timestamp: 1, data: { text: 'A partial' } };
      await hold.promise;
      yield { kind: 'run_event', seq: 2, type: 'TEXT_DELTA', timestamp: 1, data: { text: ' more A' } };
      yield { kind: 'chat_completed', seq: 3, response: {
        session_id: 's1', revision: 2, reply: 'A saved', model: 'mock', delivery: 'streamed', tool_executions: [],
        usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2, is_complete: true }, latency_ms: 1,
        messages: [{ role: 'user', content: 'hi' }, { role: 'assistant', content: 'A saved' }],
      } };
    });
    const { result } = renderHook(() => useChatSession(), { wrapper: ({ children }) => <I18nProvider>{children}</I18nProvider> });
    await waitFor(() => expect(result.current.currentSessionId).toBe('s1'));
    let turn!: Promise<void>;
    act(() => { turn = result.current.sendMessage('hi') as Promise<void>; });
    await waitFor(() => expect(result.current.streamingText).toBe('A partial'));
    await act(async () => { await result.current.selectSession('s2'); });
    expect(signal?.aborted).toBe(true);
    await act(async () => { hold.resolve(); await turn; });
    expect(result.current.currentSessionId).toBe('s2');
    expect(result.current.streamingText).toBe('');
    expect(result.current.messages).toEqual([{ role: 'user', content: 'B transcript' }]);
    expect(result.current.interrupted).toBeNull();
    expect(result.current.sending).toBe(false);
  });
});

describe('Knowledge', () => {
  it('uses Documents, Search and Ask documents tabs with one primary action per tab', async () => {
    const user = userEvent.setup();
    show(<KnowledgeView />);
    expect(await screen.findByRole('note')).toHaveTextContent(en.knowledge.testEmbeddingsNotice);
    const tabs = within(screen.getByRole('tablist', { name: 'Knowledge views' })).getAllByRole('tab');
    expect(tabs.map((tab) => tab.textContent)).toEqual(['Documents', 'Search', 'Ask documents']);
    expect(screen.getByRole('tabpanel', { name: 'Documents' })).toBeInTheDocument();
    expect(document.querySelectorAll('.studio-button--primary')).toHaveLength(1);

    // Add document opens the form in place; limits sit beside the file chooser.
    await user.click(screen.getByRole('button', { name: 'Add document' }));
    await waitFor(() => expect(screen.getByTestId('knowledge-choose-files')).toHaveFocus());
    expect(screen.getByTestId('knowledge-size-limit')).toHaveTextContent(/Max per file: 5 MB/);
    expect(screen.getByLabelText('File name')).toHaveValue('architecture_guide.md');
    expect(screen.getAllByRole('button', { name: 'Add document' })).toHaveLength(1);
    expect(document.querySelectorAll('.studio-button--primary')).toHaveLength(1);
    await user.click(screen.getByRole('button', { name: 'Cancel' }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Add document' })).toHaveFocus());

    await user.click(screen.getByRole('tab', { name: 'Ask documents' }));
    expect(screen.getByTestId('knowledge-agent-badge')).toHaveTextContent('Ask documents · Offline fake model');
    expect(screen.getByRole('button', { name: 'Send' })).toHaveClass('studio-button--primary');
    expect(screen.getByRole('group', { name: 'Answer language' })).toBeInTheDocument();
  });

  it('shows the document count as plain text beside the table', async () => {
    fixture.client.knowledge.listDocuments.mockResolvedValue({ items: [
      { document_id: 'd1', filename: 'a.md', text_length: 10, chunk_count: 1, content_hash: 'abcdef0123', status: 'indexed' },
      { document_id: 'd2', filename: 'b.md', text_length: 20, chunk_count: 2, content_hash: '0123abcdef', status: 'indexed' },
    ] });
    show(<KnowledgeView />);
    expect(await screen.findByTestId('knowledge-document-count')).toHaveTextContent('2 documents');
    expect(screen.getByRole('table', { name: 'Documents in this collection' })).toHaveClass('knowledge-documents');
  });

  it('shows exact spans, highlights and retrieval metadata for each search result', async () => {
    fixture.client.knowledge.retrieve.mockResolvedValue({
      query: 'quokka', collection_id: 'default', workspace_id: 'w', has_sufficient_evidence: true, total_tokens: 1, items: [], latency_ms: 7,
      citations: [{ citation_id: '[1]', document_id: 'doc1', origin: 'quokka.html', page_start: null, page_end: null, start_char: 12, end_char: 40,
        snippet: 'Quokka habitat notes', highlights_utf16: [[0, 6]], chunk_index: 0, matched_by: ['dense', 'bm25'] }],
    });
    const user = userEvent.setup();
    show(<KnowledgeView />);
    await user.click(await screen.findByRole('tab', { name: 'Search' }));
    expect(screen.getByRole('heading', { name: 'Search documents' })).toBeInTheDocument();
    expect(screen.getByText('How search works')).toBeInTheDocument();
    await user.clear(screen.getByLabelText('Search query'));
    await user.type(screen.getByLabelText('Search query'), 'quokka');
    await user.click(screen.getByRole('button', { name: 'Search' }));
    const card = await screen.findByTestId('citation-card-0');
    expect(within(card).getByTestId('citation-origin')).toHaveTextContent('quokka.html');
    expect(card).toHaveTextContent('Span: chars 12..40');
    expect(card).toHaveTextContent('Matched by dense, bm25');
    expect(within(card).getByText('dense, bm25').tagName).toBe('CODE');
    expect(within(card).getByTestId('citation-origin')).toHaveAttribute('title', 'quokka.html');
    expect(within(card).getByTestId('snippet-highlight')).toHaveTextContent('Quokka');

    // Opening the source chunk moves focus to it; Close returns focus to the result.
    fixture.client.request.mockImplementationOnce(async () => ({ chunk_id: 'c0', document_id: 'doc1', chunk_index: 0, text: 'Quokka habitat notes in full.', start_char: 0, end_char: 29, title: 'quokka.html', source_uri: null }));
    const openChunk = within(card).getByRole('button', { name: 'Open source chunk' });
    await user.click(openChunk);
    const panel = await screen.findByRole('region', { name: 'Source chunk' });
    await waitFor(() => expect(panel).toHaveFocus());
    await user.click(within(panel).getByRole('button', { name: 'Close' }));
    await nextFrame();
    expect(openChunk).toHaveFocus();
    expect(fixture.client.knowledge.retrieve).toHaveBeenCalledWith({ query: 'quokka', collection_id: 'default', top_k: 5 });
  });
});

describe('Agents', () => {
  it('groups the editor into labelled sections and promotes Save while editing', async () => {
    const user = userEvent.setup();
    show(<AgentsView />);
    expect(await screen.findByTestId('agents-empty')).toHaveTextContent('NEXURA_CHAT_MODELS');
    expect(screen.getByTestId('agents-new')).toHaveClass('studio-button--primary');
    await waitFor(() => expect(screen.getByTestId('agents-new')).not.toBeDisabled());
    await user.click(screen.getByTestId('agents-new'));
    for (const name of ['Identity', 'Instructions', 'Model and limits', 'Output']) {
      expect(screen.getByRole('heading', { name })).toBeInTheDocument();
    }
    expect(screen.getByRole('group', { name: /^Tools/ })).toBeInTheDocument();
    expect(screen.getByRole('group', { name: /^Skills/ })).toBeInTheDocument();
    expect(screen.getByLabelText('Name')).toBe(screen.getByTestId('agent-name'));
    expect(screen.getByRole('textbox', { name: 'Instructions' })).toBe(screen.getByTestId('agent-instructions'));
    expect(screen.getByTestId('agents-new')).not.toHaveClass('studio-button--primary');
    expect(screen.getByTestId('agent-save')).toHaveClass('studio-button--primary');
    await user.type(screen.getByLabelText('Name'), 'Bad Name');
    await user.click(screen.getByTestId('agent-save'));
    expect(screen.getByLabelText('Name')).toHaveAttribute('aria-invalid', 'true');
    expect(screen.getByLabelText('Name')).toHaveAccessibleDescription(/Lowercase letters/);
    // Unsaved input stays after a validation error.
    expect(screen.getByLabelText('Name')).toHaveValue('Bad Name');
    expect(fixture.client.agents.create).not.toHaveBeenCalled();
  });
});

describe('Agents and Skills on phones', () => {
  it('moves focus to the opened agent and back to its row', async () => {
    setPhone(true);
    fixture.client.agents.list.mockResolvedValue({ items: [{ agent_id: 'agt_1', name: 'helper', description: '', model: 'fake-deterministic', tool_count: 0, tools: [], approval_tools: [], revision: 1, updated_at: 1 }], total: 1, limits: null });
    fixture.client.agents.get.mockResolvedValue({ agent_id: 'agt_1', workspace_id: 'w', revision: 1, created_at: 1, updated_at: 1, created_by: 'p', updated_by: 'p', tools: [],
      spec: { name: 'helper', description: '', instructions: '', model: 'fake-deterministic', tools: [], knowledge: [], skills: [], skills_label: 'production', output_schema: null, limits: { max_turns: 6, max_tool_calls: 10, timeout_seconds: 60 } } });
    const user = userEvent.setup();
    show(<AgentsView />);
    const row = await screen.findByTestId('agent-row-helper');
    await user.click(row);
    await waitFor(() => expect(screen.getByRole('heading', { level: 2, name: 'helper' })).toHaveFocus());
    await user.click(screen.getByRole('button', { name: 'Back to agents' }));
    await nextFrame();
    expect(screen.getByTestId('agent-row-helper')).toHaveFocus();
  });

  it('moves focus to the opened skill and back to its row', async () => {
    setPhone(true);
    const user = userEvent.setup();
    show(<SkillsView />);
    await user.click(await screen.findByTestId('skill-row-refunds'));
    await waitFor(() => expect(screen.getByRole('heading', { level: 2, name: 'refunds' })).toHaveFocus());
    await user.click(screen.getByRole('button', { name: 'Back to skills' }));
    await nextFrame();
    expect(screen.getByTestId('skill-row-refunds')).toHaveFocus();
  });

  it('keeps focus on the row on desktop, where the list stays visible', async () => {
    const user = userEvent.setup();
    show(<SkillsView />);
    const row = await screen.findByTestId('skill-row-refunds');
    await user.click(row);
    await screen.findByTestId('skill-text');
    await nextFrame();
    expect(row).toHaveFocus();
  });
});

describe('Skills', () => {
  it('reads skill text as plain text, names its controls and makes the label action primary', async () => {
    const user = userEvent.setup();
    show(<SkillsView />);
    expect(await screen.findByTestId('skills-new')).toHaveTextContent('Add skill');
    expect(screen.getByLabelText('Label')).toBe(screen.getByTestId('skills-label-filter'));
    const row = await screen.findByTestId('skill-row-refunds');
    expect(within(row).getByTestId('skill-label-refunds-production')).toHaveTextContent('production: v1');
    await user.click(row);
    expect(row).toHaveAttribute('aria-pressed', 'true');
    const text = await screen.findByTestId('skill-text');
    expect(text.textContent).toBe('<img src=x onerror="window.__r4=1"> v2');
    expect(text.querySelector('img')).toBeNull();
    expect(screen.getByLabelText('Version')).toHaveValue('2');
    expect(screen.getByLabelText('Label name')).toHaveValue('production');
    expect(screen.getByTestId('skill-set-label')).toHaveClass('studio-button--primary');
    expect(screen.getByTestId('skills-new')).not.toHaveClass('studio-button--primary');
    expect(document.querySelectorAll('.studio-button--primary')).toHaveLength(1);
  });
});

describe('Quickstart', () => {
  it('runs the local example, shows the recorded status and opens the run', async () => {
    fixture.client.runs.start.mockResolvedValue({ run_id: 'run-r4', status: 'completed', current_state: { reply_hi: 'नमस्ते' } });
    const user = userEvent.setup();
    show(<LocalOnboarding />);
    expect(screen.getByRole('heading', { level: 1, name: 'Run a local example' })).toBeInTheDocument();
    expect(screen.getByText(en.onboarding.description)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Run example' }));
    expect(fixture.client.runs.start).toHaveBeenCalledWith(expect.objectContaining({ initial_state: { order_id: 'ORD-DELHI-501', ticket_id: 'TICKET-DELHI-501' } }));
    expect(await screen.findByLabelText('Example run status')).toHaveTextContent('Completed');
    // Navigation is a real link (open in new tab works), styled as the primary action.
    const openRun = screen.getByRole('link', { name: 'Open run' });
    expect(openRun).toHaveAttribute('href', '#/runs/run-r4');
    expect(openRun).toHaveClass('studio-button--primary');
    expect(screen.getByRole('button', { name: 'Run again' })).not.toHaveClass('studio-button--primary');
    expect(document.querySelectorAll('.studio-button--primary')).toHaveLength(1);
    expect(screen.getByText(/नमस्ते/)).toBeInTheDocument();
    await user.click(openRun);
    expect(window.location.hash).toBe('#/runs/run-r4');
  });

  it('never links to an "undefined" run when the service returns no run id', async () => {
    fixture.client.runs.start.mockResolvedValue({ status: 'failed', current_state: {} });
    const user = userEvent.setup();
    show(<LocalOnboarding />);
    await user.click(screen.getByRole('button', { name: 'Run example' }));
    expect(await screen.findByLabelText('Example run status')).toHaveTextContent('Failed');
    expect(screen.queryByRole('link', { name: 'Open run' })).toBeNull();
    expect(document.body).not.toHaveTextContent('undefined');
  });

  it('reports a pending approval as pending, not as a completed example', async () => {
    fixture.client.runs.start.mockResolvedValue({ run_id: 'run-wait', status: 'pending_approval', current_state: {} });
    const user = userEvent.setup();
    show(<LocalOnboarding />);
    await user.click(screen.getByRole('button', { name: 'Run example' }));
    const status = await screen.findByLabelText('Example run status');
    expect(status).not.toHaveTextContent('Completed');
    expect(status).toHaveTextContent(/approval/i);
  });

  it('is fully translated in Hindi', async () => {
    localStorage.setItem('nexura_locale', 'hi');
    show(<LocalOnboarding />);
    expect(screen.getByRole('heading', { level: 1, name: hi.onboarding.title })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: hi.onboarding.runButton })).toBeInTheDocument();
  });
});
