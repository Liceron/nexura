// P39 G6: the agent editor's Guardrails section (presets, judge policy, stream mode), the
// read-only operator policy, effect class badges and the guardrails field of saved specs.
// Mocked API; no network. Playwright is run by the lead, not here.
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ReactNode } from 'react';
import type { AgentGuardrailOptions, AgentOptions, AgentRecord } from '@liceron/nexura-sdk';

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

const GUARDRAILS: AgentGuardrailOptions = {
  available: true,
  install_hint: null,
  defaults: ['pii_redact', 'injection_fast'],
  presets: [
    { name: 'pii_block', description: '', stages: ['input'], network: 'none', cost: 'none', options: ['kinds'] },
    { name: 'pii_redact', description: '', stages: ['output'], network: 'none', cost: 'none', options: ['kinds'] },
    { name: 'injection_fast', description: '', stages: ['input'], network: 'none', cost: 'none', options: ['threshold'] },
    {
      name: 'safety_local',
      description: '',
      stages: ['input', 'output'],
      network: 'local',
      cost: 'local',
      options: ['model'],
      default_model: 'llama-guard3:1b',
      models: ['llama-guard3:1b', 'granite3-guardian:2b'],
    },
    { name: 'judge', description: '', stages: ['input', 'output'], network: 'model', cost: 'model', options: ['policy', 'model'] },
  ],
  policy: {
    configured: true,
    enforced: true,
    policy: {
      name: 'acme-default',
      digest: 'abcdef0123456789abcdef',
      file: 'policy.yaml',
      source: '/etc/nexura/policy.yaml',
      on_error: 'block',
      default_tool_action: 'allow',
      rules: [
        { id: 'a', action: 'block', checks: [], when: {} },
        { id: 'b', action: 'scrub', checks: ['pii'], when: {} },
      ],
      checks: [],
    },
  },
};

const OPTIONS: AgentOptions = {
  models: [
    { name: 'fake-deterministic', provider: 'local', label: 'Offline', offline: true, streaming: false, default: true },
    { name: 'gpt-x', provider: 'openai', label: 'GPT X', offline: false, streaming: true, default: false },
  ],
  tools: [
    {
      module_id: 'demo.lookup',
      tool_name: 'demo_lookup',
      title: 'Lookup',
      description: 'Reads.',
      kind: 'integration',
      requires_approval: false,
      effect_policy: 'read',
      effect_class: 'pure',
    },
    {
      module_id: 'demo.ticket.create',
      tool_name: 'demo_ticket_create',
      title: 'Create ticket',
      description: 'Writes.',
      kind: 'integration',
      requires_approval: true,
      effect_policy: 'non_idempotent_write',
      effect_class: 'non_idempotent',
    },
  ],
  knowledge_collections: ['default'],
  skills: [],
  node_module_id: 'nexura.agents.agent',
  limits: LIMITS,
  guardrails: GUARDRAILS,
};

function record(guardrails: AgentRecord['spec']['guardrails']): AgentRecord {
  return {
    agent_id: 'agt_1',
    workspace_id: 'w',
    revision: 2,
    spec: {
      name: 'helper',
      description: '',
      instructions: '',
      model: 'gpt-x',
      tools: [],
      knowledge: [],
      skills: [],
      skills_label: 'production',
      output_schema: null as unknown as Record<string, unknown>,
      limits: { max_turns: 6, max_tool_calls: 10, timeout_seconds: 60 },
      guardrails,
    },
    created_at: 1,
    updated_at: 1,
    created_by: 'p',
    updated_by: 'p',
    tools: [],
  };
}

const state: { options: AgentOptions; record: AgentRecord } = { options: OPTIONS, record: record(null) };
const agentsApi = {
  list: vi.fn(async () => ({ items: [{ agent_id: 'agt_1', name: 'helper', description: '', model: 'gpt-x', tool_count: 0, tools: [], approval_tools: [], revision: 2, updated_at: 1 }], total: 1, limits: LIMITS })),
  options: vi.fn(async () => state.options),
  get: vi.fn(async (): Promise<AgentRecord> => state.record),
  create: vi.fn(async (spec: unknown) => ({ ...record(null), spec })),
  update: vi.fn(async (_id: string, spec: unknown) => ({ ...state.record, revision: 3, spec })),
  delete: vi.fn(),
};
const client = {
  agents: agentsApi,
  request: async <T,>(_method: string, path: string): Promise<T> => {
    if (path.endsWith('/my-membership')) return { role: 'admin' } as T;
    return {} as T;
  },
};
const api = { manager: { client, clearCache: () => undefined }, connection: { status: 'connected', workspace: 'w' } };
vi.mock('../../src/api/context', () => ({ useApi: () => api }));

import { I18nProvider } from '../../src/i18n';
import { AgentsView } from '../../src/features/agents/AgentsView';
import { draftFromRecord, emptyDraft, guardrailsOfDraft, validateDraft } from '../../src/features/agents/agentForm';
import { effectClassOf } from '../../src/features/agents/AgentGuardrails';
import { installRequestMembershipSource } from './membershipSource';
import en from '../../src/i18n/en.json';
import hi from '../../src/i18n/hi.json';

installRequestMembershipSource();

const wrap = (ui: ReactNode) => render(<I18nProvider>{ui}</I18nProvider>);

beforeEach(() => {
  state.options = OPTIONS;
  state.record = record(null);
  for (const fn of Object.values(agentsApi)) fn.mockClear();
  localStorage.clear();
  Element.prototype.scrollIntoView = vi.fn();
});

describe('guardrails in the agent form model', () => {
  it('starts new agents with the zero-cost presets', () => {
    const draft = emptyDraft('gpt-x');
    expect(draft.guardrail_presets).toEqual(['pii_redact', 'injection_fast']);
    expect(guardrailsOfDraft(draft)).toEqual({
      presets: [{ preset: 'pii_redact' }, { preset: 'injection_fast' }],
      stream_guard: 'auto',
    });
  });

  it('keeps an agent without guardrails at null until something is switched on', () => {
    const draft = draftFromRecord(record(null));
    expect(draft.guardrails_unset).toBe(true);
    expect(validateDraft(draft).spec?.guardrails).toBeNull();
    expect(validateDraft({ ...draft, stream_guard: 'hold' }).spec?.guardrails).toEqual({ presets: [], stream_guard: 'hold' });
  });

  it('round-trips presets and keeps options the form does not edit', () => {
    const saved = record({
      presets: [
        { preset: 'pii_block', kinds: ['card', 'aadhaar'] },
        { preset: 'injection_fast', threshold: 0.8 },
        { preset: 'safety_local', model: 'granite3-guardian:2b' },
        { preset: 'judge', policy: 'Block fraud.', model: 'gpt-x' },
      ],
      stream_guard: 'chunked',
    });
    const draft = draftFromRecord(saved);
    expect(guardrailsOfDraft(draft)).toEqual(saved.spec.guardrails);
  });

  it('requires judge policy text and bounds it', () => {
    const draft = { ...emptyDraft('gpt-x'), name: 'ok', guardrail_presets: ['judge' as const] };
    expect(validateDraft(draft).errors.guardrails).toMatch(/policy text/);
    expect(validateDraft({ ...draft, judge_policy: 'x'.repeat(4001) }).errors.guardrails).toMatch(/4000/);
    expect(validateDraft({ ...draft, judge_policy: 'No fraud.' }).spec?.guardrails).toEqual({
      presets: [{ preset: 'judge', policy: 'No fraud.' }],
      stream_guard: 'auto',
    });
  });

  it('maps effect policies to effect classes', () => {
    expect(effectClassOf('read')).toBe('pure');
    expect(effectClassOf('idempotent_write')).toBe('idempotent');
    expect(effectClassOf('non_idempotent_write')).toBe('non_idempotent');
    expect(effectClassOf('mystery')).toBeNull();
    expect(effectClassOf(undefined)).toBeNull();
  });

  it('has every new string in English and Hindi', () => {
    const keys = (o: Record<string, unknown>, p = ''): string[] =>
      Object.entries(o).flatMap(([k, v]) => (v && typeof v === 'object' ? keys(v as Record<string, unknown>, `${p}${k}.`) : [`${p}${k}`]));
    const pick = (d: { agents: Record<string, unknown> }) =>
      keys(d.agents).filter((k) => k.startsWith('guardrails') || k.startsWith('effect')).sort();
    const english = pick(en as unknown as { agents: Record<string, unknown> });
    expect(english.length).toBeGreaterThan(30);
    expect(pick(hi as unknown as { agents: Record<string, unknown> })).toEqual(english);
  });
});

describe('Guardrails section of the agent editor', () => {
  it('shows the enforced operator policy read-only, preset costs and effect classes', async () => {
    const user = userEvent.setup();
    wrap(<AgentsView />);
    await user.click(await screen.findByTestId('agents-new'));
    const section = await screen.findByTestId('agent-guardrails');
    const note = within(section).getByTestId('agent-guardrails-policy');
    expect(note.textContent).toContain('acme-default');
    expect(note.textContent).toContain('2 rules');
    expect(note.textContent).toContain('abcdef012345');
    expect((screen.getByTestId('agent-guardrail-pii_redact') as HTMLInputElement).checked).toBe(true);
    expect((screen.getByTestId('agent-guardrail-injection_fast') as HTMLInputElement).checked).toBe(true);
    expect((screen.getByTestId('agent-guardrail-judge') as HTMLInputElement).checked).toBe(false);
    expect(screen.getByTestId('agent-guardrail-cost-safety_local').textContent).toBe('Local model');
    expect(screen.getByTestId('agent-guardrail-cost-judge').textContent).toBe('Model call (billed)');
    expect(screen.getByTestId('agent-guardrail-cost-pii_block').textContent).toBe('No model, no cost');
    expect(screen.getByTestId('agent-tool-effect-demo.lookup').textContent).toBe('No side effects');
    expect(screen.getByTestId('agent-tool-effect-demo.ticket.create').textContent).toBe('Not safe to repeat');
  });

  it('sends the chosen presets, judge policy and stream mode', async () => {
    const user = userEvent.setup();
    wrap(<AgentsView />);
    await user.click(await screen.findByTestId('agents-new'));
    await user.type(screen.getByTestId('agent-name'), 'judged');
    await user.click(screen.getByTestId('agent-guardrail-injection_fast'));
    await user.click(screen.getByTestId('agent-guardrail-judge'));
    expect(screen.getByTestId('agent-guardrail-judge-capacity').textContent).toContain('1.2 to 1.9 KB');
    await user.click(screen.getByTestId('agent-save'));
    expect((await screen.findByTestId('agent-error-guardrails')).textContent).toMatch(/policy text/);
    expect(agentsApi.create).not.toHaveBeenCalled();
    await user.type(screen.getByTestId('agent-guardrail-judge-policy'), 'Block loan guarantees.');
    await user.selectOptions(screen.getByTestId('agent-guardrail-judge-model'), 'gpt-x');
    await user.click(screen.getByTestId('agent-guardrail-safety_local'));
    await user.selectOptions(screen.getByTestId('agent-guardrail-safety-model'), 'granite3-guardian:2b');
    await user.selectOptions(screen.getByTestId('agent-guardrail-stream'), 'hold');
    await user.click(screen.getByTestId('agent-save'));
    await waitFor(() => expect(agentsApi.create).toHaveBeenCalledTimes(1));
    const spec = (agentsApi.create.mock.calls[0] as unknown[])[0] as { guardrails: unknown };
    expect(spec.guardrails).toEqual({
      presets: [
        { preset: 'pii_redact' },
        { preset: 'judge', policy: 'Block loan guarantees.', model: 'gpt-x' },
        { preset: 'safety_local', model: 'granite3-guardian:2b' },
      ],
      stream_guard: 'hold',
    });
  });

  it('leaves an old agent without guardrails unchanged on save', async () => {
    const user = userEvent.setup();
    wrap(<AgentsView />);
    await user.click(await screen.findByText('helper'));
    await screen.findByTestId('agent-guardrails');
    expect((screen.getByTestId('agent-guardrail-pii_redact') as HTMLInputElement).checked).toBe(false);
    await user.click(screen.getByTestId('agent-save'));
    await waitFor(() => expect(agentsApi.update).toHaveBeenCalledTimes(1));
    const spec = (agentsApi.update.mock.calls[0] as unknown[])[1] as { guardrails: unknown };
    expect(spec.guardrails).toBeNull();
  });

  it('says so when the server cannot build presets, and shows Hindi labels', async () => {
    state.options = {
      ...OPTIONS,
      guardrails: { ...GUARDRAILS, available: false, install_hint: 'pip install "nexura-guardrails[yaml]"', policy: { configured: false, enforced: false, policy: null } },
    };
    localStorage.setItem('nexura_locale', 'hi');
    const user = userEvent.setup();
    wrap(<AgentsView />);
    await user.click(await screen.findByTestId('agents-new'));
    const section = await screen.findByTestId('agent-guardrails');
    expect(within(section).queryByTestId('agent-guardrails-policy')).toBeNull();
    expect(within(section).getByTestId('agent-guardrails-unavailable').textContent).toContain('nexura-guardrails');
    expect((screen.getByTestId('agent-guardrail-judge') as HTMLInputElement).disabled).toBe(true);
    expect(section.querySelector('legend')?.textContent).toBe('गार्डरेल');
  });

  it('shows the badge only when the policy is enforced, and a summary view without rules', async () => {
    state.options = {
      ...OPTIONS,
      guardrails: { ...GUARDRAILS, policy: { ...GUARDRAILS.policy, enforced: false } },
    };
    const user = userEvent.setup();
    const first = wrap(<AgentsView />);
    await user.click(await screen.findByTestId('agents-new'));
    await screen.findByTestId('agent-guardrails');
    expect(screen.queryByTestId('agent-guardrails-policy')).toBeNull();
    first.unmount();

    state.options = {
      ...OPTIONS,
      guardrails: {
        ...GUARDRAILS,
        policy: { configured: true, enforced: true, detail: 'summary', policy: { digest: 'fedcba9876543210', file: 'guardrails.yaml' } },
      },
    };
    wrap(<AgentsView />);
    await user.click(await screen.findByTestId('agents-new'));
    const note = await screen.findByTestId('agent-guardrails-policy');
    expect(note.textContent).toContain('guardrails.yaml');
    expect(note.textContent).not.toContain('applies to every run of every agent');
    expect(screen.queryByTestId('agent-guardrails-policy-rules')).toBeNull();
    expect(screen.getByTestId('agent-guardrails-policy-exceptions').textContent).toMatch(/workflow module nodes/);
  });

  it('does not offer passthrough while answers are checked', async () => {
    state.options = { ...OPTIONS, guardrails: { ...GUARDRAILS, policy: { configured: false, enforced: false, policy: null } } };
    const user = userEvent.setup();
    wrap(<AgentsView />);
    await user.click(await screen.findByTestId('agents-new'));
    const select = (await screen.findByTestId('agent-guardrail-stream')) as HTMLSelectElement;
    const passthrough = () => Array.from(select.options).find((o) => o.value === 'passthrough') as HTMLOptionElement;
    expect(passthrough().disabled).toBe(true); // pii_redact is on for a new agent
    expect(screen.getByTestId('agent-guardrail-stream-note')).toBeTruthy();
    await user.click(screen.getByTestId('agent-guardrail-pii_redact'));
    expect(passthrough().disabled).toBe(false); // injection_fast alone checks no answer
  });
});

describe('passthrough validation', () => {
  it('refuses passthrough with an answer-checking preset', () => {
    const draft = { ...emptyDraft('gpt-x'), name: 'ok', stream_guard: 'passthrough' as const };
    expect(validateDraft(draft).errors.guardrails).toMatch(/stream unchecked/);
    expect(validateDraft({ ...draft, guardrail_presets: ['injection_fast'] }).errors.guardrails).toBeUndefined();
  });
});
