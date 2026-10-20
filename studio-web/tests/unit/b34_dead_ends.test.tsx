import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, within, renderHook, act } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ReactNode } from 'react';
import { I18nProvider } from '../../src/i18n';
import { ApiProvider } from '../../src/api/context';
import { Shell } from '../../src/app/Shell';
import { ModulePalette } from '../../src/features/modules/ModulePalette';
import {
  MODULE_CATEGORIES,
  isOfferedInPalette,
  moduleMatchesCategory,
} from '../../src/features/modules/types';
import type { PaletteModule } from '../../src/features/modules/types';
import { CreateWorkflowModal } from '../../src/features/workflows/CreateWorkflowModal';
import { INCOMPATIBLE_TEST_TEMPLATE } from '../../src/features/workflows/templates';
import { TemplatesView } from '../../src/features/templates/TemplatesView';
import {
  APPROVALS_CHANGED_EVENT,
  usePendingApprovals,
} from '../../src/features/approvals/usePendingApprovals';

/**
 * B34 Studio dead ends: palette categories match the kinds the service really reports, service-internal
 * modules (descriptor `internal: true`) are not offered, the create-workflow modal no longer lists the
 * test fixture, the sidebar shows the pending-approvals count, and the template "missing credentials"
 * notice links to the Credentials page. All data comes from a mocked fetch (no service, no network).
 */

const CONNECTOR_IDS = [
  'airtable', 'discord', 'gdrive', 'github', 'gsheets', 'imap', 'jira', 'mysql',
  'notion', 'postgres', 's3', 'slack', 'smtp', 'teams', 'webhook',
].map((x) => `nexura.connectors.${x}`);

const INTERNAL_IDS = [
  'nexura.service.auth',
  'nexura.service.workspaces',
  'nexura.service.triggers',
  'nexura.observability',
  'nexura.evaluation.runner',
  'nexura.runtime.approvals',
];

// Mirrors GET /api/v1/modules from the default Studio registry (kinds, groups and internal flags).
const RAW: Array<[string, string, string | null, boolean]> = [
  ['nexura.agents.coordinator', 'agent', null, false],
  ['nexura.runtime.approvals', 'approval_manager', null, true],
  ['nexura.data.transform', 'data_transform', null, false],
  ['nexura.prompt.template', 'data_transform', null, false],
  ['nexura.usage.budget', 'governance', null, false],
  ['mcp.local.calculator', 'integration', null, false],
  ['nexura.channel.email', 'integration', null, false],
  ['nexura.channel.whatsapp', 'integration', null, false],
  ...CONNECTOR_IDS.map((id): [string, string, string, boolean] => [id, 'integration', 'connectors', false]),
  ['nexura.http.request', 'integration', null, false],
  ['nexura.speech', 'integration', null, false],
  ['nexura.sql.query', 'integration', null, false],
  ['nexura.knowledge.retrieval', 'knowledge', null, false],
  ['nexura.knowledge.ingest', 'knowledge', null, false],
  ['synthetic_order_lookup', 'legacy_handler', 'Legacy', false],
  ['tpl_rag_answer', 'legacy_handler', 'Legacy', false],
  ['nexura.models.openai', 'model_adapter', null, false],
  ['nexura.models.provider', 'model_adapter', null, false],
  ['nexura.evaluation.runner', 'service', null, true],
  ['nexura.observability', 'service', null, true],
  ['nexura.service.auth', 'service', null, true],
  ['nexura.service.triggers', 'service', null, true],
  ['nexura.service.workspaces', 'service', null, true],
  ['nexura.tools.sandbox.execute', 'tool', null, false],
];

function descriptor([module_id, kind, group, internal]: [string, string, string | null, boolean]) {
  return {
    module_id,
    version: '1.0.0',
    kind,
    title: `Title ${module_id}`,
    description: `Description of ${module_id}`,
    input_schema: {},
    output_schema: {},
    config_schema: {},
    named_ports: [],
    supported_routes: ['default'],
    required_credential_types: [],
    capabilities: {},
    effect_policy: 'pure',
    min_sdk_version: '0.3.0',
    verification_status: 'mock_verified',
    is_available: true,
    missing_reason: null,
    requires_approval: false,
    digest: 'd',
    display: group ? { label: null, icon: null, group, examples: [] } : null,
    internal,
  };
}

const MODULES = RAW.map(descriptor);

const CAPABILITIES = {
  sdk_version: '0.4.0',
  active_profile: 'local-owner',
  supported_profiles: ['local-owner'],
  capabilities: { modules: true, validation: true },
};

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

function pendingRun(id: string) {
  return {
    run_id: id,
    workflow_id: 'wf-1',
    status: 'pending_approval',
    current_node_id: 'gate',
    pending_approval: { approval_id: `ap-${id}`, node_id: 'gate' },
    created_at: 1700000000,
    current_state: {},
  };
}

type Route = (url: string, init?: RequestInit) => Response | undefined;

function mockFetch(route: Route) {
  return vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
    const url = String(input);
    const hit = route(url, init);
    if (hit) return hit;
    if (url.includes('/capabilities')) return json(CAPABILITIES);
    return json({ items: [], total: 0, limit: 50, offset: 0, has_more: false });
  });
}

function runsCalls(spy: ReturnType<typeof mockFetch>): number {
  return spy.mock.calls.filter(([u]) => String(u).includes('/runs')).length;
}

function wrap(children: ReactNode) {
  return (
    <I18nProvider>
      <ApiProvider>{children}</ApiProvider>
    </I18nProvider>
  );
}

beforeEach(() => {
  window.location.hash = '';
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('B34 module palette categories', () => {
  const offered = MODULES.filter(isOfferedInPalette) as PaletteModule[];
  const ids = (cat: Parameters<typeof moduleMatchesCategory>[1]) =>
    offered.filter((m) => moduleMatchesCategory(m, cat)).map((m) => m.module_id);

  it('places channels by module id and the 15 connectors in their own category', () => {
    expect(ids('channels')).toEqual(['nexura.channel.email', 'nexura.channel.whatsapp']);
    expect(ids('connectors').sort()).toEqual([...CONNECTOR_IDS].sort());
    expect(MODULE_CATEGORIES.map((c) => c.id)).toContain('connectors');
  });

  it('gives every offered module a category and leaves no category empty', () => {
    for (const mod of offered) {
      const cats = MODULE_CATEGORIES.filter((c) => c.id !== 'all' && moduleMatchesCategory(mod, c.id));
      expect(cats.length, mod.module_id).toBeGreaterThan(0);
    }
    for (const cat of MODULE_CATEGORIES) {
      expect(ids(cat.id).length, cat.id).toBeGreaterThan(0);
    }
    expect(ids('tools')).toEqual(
      expect.arrayContaining(['nexura.tools.sandbox.execute', 'nexura.http.request', 'mcp.local.calculator'])
    );
    expect(ids('tools')).not.toContain('nexura.channel.email');
    expect(ids('tools')).not.toContain('nexura.connectors.slack');
  });

  it('does not offer modules flagged internal', () => {
    for (const id of INTERNAL_IDS) {
      expect(offered.some((m) => m.module_id === id), id).toBe(false);
    }
    expect(isOfferedInPalette({ module_id: 'x.y', kind: 'tool' })).toBe(true);
    expect(isOfferedInPalette({ module_id: 'x.y', kind: 'tool', internal: false })).toBe(true);
    expect(isOfferedInPalette({ module_id: 'x.y', kind: 'service', internal: true })).toBe(false);
  });

  it('renders the drawer without internal modules and with populated Channels and Connectors', async () => {
    mockFetch((url) => (url.includes('/modules') ? json({ items: MODULES, total: MODULES.length, modules: MODULES }) : undefined));
    const user = userEvent.setup();
    const onSelect = vi.fn();
    render(wrap(<ModulePalette open onClose={vi.fn()} onSelectModule={onSelect} />));

    const drawer = await screen.findByRole('dialog', { name: 'Available modules drawer' });
    expect(await within(drawer).findByText('nexura.prompt.template')).toBeInTheDocument();
    const offeredCount = MODULES.filter((m) => !m.internal).length;
    expect(within(drawer).getByText(`${offeredCount} available`)).toBeInTheDocument();
    for (const id of INTERNAL_IDS) {
      expect(within(drawer).queryByText(id)).toBeNull();
    }

    await user.click(within(drawer).getByTestId('palette-category-channels'));
    expect(within(drawer).getByText('2 available')).toBeInTheDocument();
    expect(within(drawer).getByText('nexura.channel.email')).toBeInTheDocument();
    expect(within(drawer).getByText('nexura.channel.whatsapp')).toBeInTheDocument();

    await user.click(within(drawer).getByTestId('palette-category-connectors'));
    expect(within(drawer).getByText('15 available')).toBeInTheDocument();
    expect(within(drawer).getByTestId('palette-category-connectors')).toHaveAttribute('aria-pressed', 'true');

    await user.click(within(drawer).getByTestId('palette-category-services'));
    expect(within(drawer).getByText('nexura.usage.budget')).toBeInTheDocument();
    expect(within(drawer).queryByText('nexura.service.auth')).toBeNull();
  });
});

describe('B34 create-workflow modal', () => {
  it('does not list the incompatible test fixture to users', () => {
    mockFetch(() => undefined);
    render(wrap(<CreateWorkflowModal open onClose={vi.fn()} onCreated={vi.fn()} />));
    expect(screen.getByText('Customer Order Triage & Support')).toBeInTheDocument();
    expect(screen.queryByText('Incompatible Billing Workflow (Test Fixture)')).toBeNull();
  });

  it('still lists a fixture passed explicitly through extraTemplates', () => {
    mockFetch(() => undefined);
    render(
      wrap(
        <CreateWorkflowModal
          open
          onClose={vi.fn()}
          onCreated={vi.fn()}
          extraTemplates={[INCOMPATIBLE_TEST_TEMPLATE]}
        />
      )
    );
    expect(screen.getByText('Incompatible Billing Workflow (Test Fixture)')).toBeInTheDocument();
  });
});

describe('B34 sidebar approvals badge', () => {
  it('shows the pending approvals count from the runs API', async () => {
    mockFetch((url) =>
      url.includes('/runs') ? json({ items: [pendingRun('r1'), pendingRun('r2')], total: 2 }) : undefined
    );
    render(wrap(<Shell />));
    const nav = await screen.findByTestId('nav-item-approvals');
    await waitFor(() => expect(within(nav).getByText('2')).toBeInTheDocument());
  });

  it('does not fetch approvals while disabled (offline)', async () => {
    const spy = mockFetch(() => undefined);
    const { result } = renderHook(() => usePendingApprovals({ enabled: false, pollIntervalMs: 20 }), {
      wrapper: ({ children }) => wrap(children),
    });
    await new Promise((r) => setTimeout(r, 80));
    expect(runsCalls(spy)).toBe(0);
    expect(result.current.count).toBe(0);
  });

  it('polls on the given interval and refreshes when another instance records a decision', async () => {
    let pending = [pendingRun('r1')];
    const spy = mockFetch((url) => (url.includes('/runs') ? json({ items: pending, total: pending.length }) : undefined));
    const { result } = renderHook(() => usePendingApprovals({ pollIntervalMs: 30 }), {
      wrapper: ({ children }) => wrap(children),
    });
    await waitFor(() => expect(result.current.count).toBe(1));
    await waitFor(() => expect(runsCalls(spy)).toBeGreaterThanOrEqual(3));

    pending = [];
    await act(async () => {
      window.dispatchEvent(new CustomEvent(APPROVALS_CHANGED_EVENT, { detail: {} }));
    });
    await waitFor(() => expect(result.current.count).toBe(0));
  });

  it('does not poll without an interval (inbox behaviour unchanged)', async () => {
    const spy = mockFetch((url) => (url.includes('/runs') ? json({ items: [], total: 0 }) : undefined));
    renderHook(() => usePendingApprovals(), { wrapper: ({ children }) => wrap(children) });
    await waitFor(() => expect(runsCalls(spy)).toBe(1));
    await new Promise((r) => setTimeout(r, 80));
    expect(runsCalls(spy)).toBe(1);
  });
});

describe('B34 templates missing-credentials link', () => {
  it('links the missing credentials notice to the Credentials page', async () => {
    const summary = {
      id: 'support-desk',
      template_version: 1,
      title: 'Support desk',
      description: 'Starter',
      category: 'support',
      difficulty: 'beginner',
      tags: [],
      required_modules: [],
      required_credentials: ['model_api_key'],
      node_count: 3,
      has_approval: false,
      has_trigger: false,
    };
    mockFetch((url, init) => {
      if (url.includes('/templates/support-desk/instantiate') && init?.method === 'POST') {
        return json({
          workflow_id: 'wf-new',
          template_id: 'support-desk',
          missing_credentials: ['model_api_key'],
          runs_offline: true,
          suggested_trigger: null,
        });
      }
      if (url.includes('/templates')) {
        return json({ items: [summary], total: 1, categories: ['support'], tags: [] });
      }
      return undefined;
    });
    const user = userEvent.setup();
    render(wrap(<TemplatesView />));
    await user.click(await screen.findByTestId('template-use-support-desk'));
    expect(await screen.findByTestId('template-missing-credentials')).toHaveTextContent('model_api_key');
    const link = screen.getByTestId('template-credentials-link');
    expect(link).toHaveAttribute('href', '#/credentials');
    expect(link).toHaveTextContent('Add them on the Credentials page');
  });
});
