// P28 step 2: the Studio extension registry, bootstrap and the components that read it.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, within } from '@testing-library/react';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import type { ReactNode } from 'react';
import { ApiProvider } from '../../src/api/context';
import { I18nProvider } from '../../src/i18n';
import {
  BUILTIN_NAV_TABS,
  StudioExtensionError,
  StudioExtensionRegistry,
  StudioExtensionsContext,
  mergeAfter,
  type StudioExtension,
  type WorkflowPanelProps,
} from '../../src/app/extensions';
import { BUILTIN_STUDIO_EXTENSIONS } from '../../src/app/builtinExtensions';
import { registerStudioExtensions } from '../../src/app/bootstrap';
import { parseHash } from '../../src/app/routes';
import { Sidebar } from '../../src/app/Sidebar';
import { Shell } from '../../src/app/Shell';
import { WorkflowDetail } from '../../src/features/workflows/WorkflowDetail';

function ReportsPage() {
  return <div data-testid="reports-page">Reports page</div>;
}

function NotesPanel({ workflowId, workflowName }: WorkflowPanelProps) {
  return <div data-testid="notes-panel">{`${workflowId}:${workflowName}`}</div>;
}

const reports: StudioExtension = {
  id: 'test.reports',
  navItems: [{ id: 'reports', labelKey: 'nav.reports', icon: 'R', after: 'skills' }],
  routes: [{ hash: '#/reports', tab: 'reports', component: ReportsPage }],
};

const notes: StudioExtension = {
  id: 'test.notes',
  panels: [{ slot: 'workflow-detail', id: 'notes', labelKey: 'notes.tab', after: 'nodes', component: NotesPanel }],
};

function withRegistry(registry: StudioExtensionRegistry, children: ReactNode) {
  return <StudioExtensionsContext.Provider value={registry}>{children}</StudioExtensionsContext.Provider>;
}

describe('StudioExtensionRegistry', () => {
  it('keeps registration order and exposes nav items, routes and panels', () => {
    const registry = new StudioExtensionRegistry();
    registry.register(reports);
    registry.register(notes);
    expect(registry.list().map((e) => e.id)).toEqual(['test.reports', 'test.notes']);
    expect(registry.navItems().map((n) => n.id)).toEqual(['reports']);
    expect(registry.routeTab('#/reports')).toBe('reports');
    expect(registry.page('reports')).toBe(ReportsPage);
    expect(registry.page('workflows')).toBeUndefined();
    expect(registry.panels('workflow-detail').map((p) => p.id)).toEqual(['notes']);
  });

  it.each<[string, StudioExtension]>([
    ['empty id', { id: '' }],
    ['built-in nav item', { id: 'x', navItems: [{ id: 'agents', labelKey: 'k', icon: '' }] }],
    ['nav item without label', { id: 'x', navItems: [{ id: 'mine', labelKey: '', icon: '' }] }],
    ['malformed hash', { id: 'x', routes: [{ hash: 'reports', tab: 'mine', component: ReportsPage }] }],
    ['built-in hash', { id: 'x', routes: [{ hash: '#/chat', tab: 'mine', component: ReportsPage }] }],
    ['built-in page', { id: 'x', routes: [{ hash: '#/mine', tab: 'system', component: ReportsPage }] }],
    [
      'built-in panel',
      { id: 'x', panels: [{ slot: 'workflow-detail', id: 'json', labelKey: 'k', component: NotesPanel }] },
    ],
  ])('refuses %s', (_label, extension) => {
    const registry = new StudioExtensionRegistry();
    expect(() => registry.register(extension)).toThrow(StudioExtensionError);
    expect(registry.list()).toEqual([]);
  });

  it('refuses duplicates across extensions', () => {
    const registry = new StudioExtensionRegistry();
    registry.register(reports);
    registry.register(notes);
    expect(() => registry.register(reports)).toThrow(/already registered/);
    expect(() => registry.register({ ...reports, id: 'other', routes: [] })).toThrow(/nav item 'reports'/);
    expect(() => registry.register({ ...reports, id: 'other', navItems: [] })).toThrow(/route '#\/reports'/);
    expect(() => registry.register({ ...notes, id: 'other' })).toThrow(/panel 'notes'/);
    expect(registry.list().map((e) => e.id)).toEqual(['test.reports', 'test.notes']);
  });
});

describe('mergeAfter', () => {
  it('places items after their anchor, in registration order, and unknown anchors last', () => {
    const merged = mergeAfter(
      ['a', 'b', 'c'],
      [
        { id: 'x', after: 'a' },
        { id: 'y', after: 'a' },
        { id: 'z', after: 'x' },
        { id: 'end' },
        { id: 'lost', after: 'missing' },
      ],
    );
    expect(merged.map((e) => (typeof e === 'string' ? e : e.id))).toEqual([
      'a', 'x', 'z', 'y', 'b', 'c', 'end', 'lost',
    ]);
  });

  it('never drops items whose anchors form a cycle', () => {
    const merged = mergeAfter(['a'], [{ id: 'p', after: 'q' }, { id: 'q', after: 'p' }]);
    expect(merged.map((e) => (typeof e === 'string' ? e : e.id)).sort()).toEqual(['a', 'p', 'q']);
  });
});

describe('bootstrap', () => {
  it('registers the built-ins (none) first, then the given extensions', () => {
    // Publish (EDITIONS_PLAN step 4) and Organisation (step 5) moved to the closed edition; its
    // Studio entry registers them (enterprise/studio-web/tests/unit has the positive checks).
    const registry = registerStudioExtensions([reports], new StudioExtensionRegistry());
    expect(registry.list().map((e) => e.id)).toEqual(['test.reports']);
    expect(registry.routeTab('#/organisation')).toBeUndefined();
    expect(registry.navItems().find((n) => n.id === 'organisation')).toBeUndefined();
    expect(registry.panels('workflow-detail')).toEqual([]);
    expect(BUILTIN_STUDIO_EXTENSIONS.map((e) => e.id)).toEqual([]);
  });

  it('throws when the same registry is bootstrapped twice', () => {
    const registry = registerStudioExtensions([], new StudioExtensionRegistry());
    expect(() => registerStudioExtensions([], registry)).toThrow(StudioExtensionError);
  });
});

describe('routes', () => {
  it('resolves extension hashes after the built-in ones', () => {
    const registry = new StudioExtensionRegistry();
    registry.register(reports);
    expect(parseHash('#/reports', registry).tab).toBe('reports');
    expect(parseHash('#/agents', registry).tab).toBe('agents');
    // Without an extension the hash is unknown and falls back to the workflow list.
    expect(parseHash('#/reports', new StudioExtensionRegistry()).tab).toBe('workflows');
    expect(parseHash('#/organisation', new StudioExtensionRegistry()).tab).toBe('workflows');
  });

  it('core Studio files do not import the Organisation or Publish features', () => {
    const files = [
      'src/app/Shell.tsx',
      'src/app/Sidebar.tsx',
      'src/app/routes.ts',
      'src/app/extensions.ts',
      'src/features/workflows/WorkflowDetail.tsx',
    ];
    for (const file of files) {
      const source = readFileSync(resolve(__dirname, '../..', file), 'utf-8');
      expect(source, file).not.toMatch(/features\/(organisation|publish)/);
      expect(source, file).not.toMatch(/['"]\.\.\/(organisation|publish)\//);
    }
  });
});

describe('Sidebar', () => {
  it('lists built-in pages in order with extension items after their anchor', () => {
    const registry = registerStudioExtensions([reports], new StudioExtensionRegistry());
    const onSelect = vi.fn();
    render(
      <I18nProvider>
        {withRegistry(registry, <Sidebar currentTab="workflows" onSelectTab={onSelect} />)}
      </I18nProvider>,
    );
    const ids = within(screen.getByRole('navigation', { name: 'Studio navigation' }))
      .getAllByRole('button')
      .map((b) => b.getAttribute('data-testid')!.replace('nav-item-', ''));
    const expected: string[] = [...BUILTIN_NAV_TABS];
    expected.splice(expected.indexOf('skills') + 1, 0, 'reports');
    expect(ids).toEqual(expected);
    expect(ids).not.toContain('organisation');
    fireEvent.click(screen.getByTestId('nav-item-reports'));
    expect(onSelect).toHaveBeenCalledWith('reports');
  });

  it('shows only built-in pages with an empty registry', () => {
    render(
      <I18nProvider>
        {withRegistry(new StudioExtensionRegistry(), <Sidebar currentTab="workflows" onSelectTab={vi.fn()} />)}
      </I18nProvider>,
    );
    expect(screen.queryByTestId('nav-item-organisation')).toBeNull();
    expect(screen.getAllByRole('button')).toHaveLength(BUILTIN_NAV_TABS.length);
  });
});

function mockService(workflow?: Record<string, unknown>) {
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
    const url = String(input);
    const json = (body: unknown) =>
      new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } });
    if (url.includes('/capabilities')) {
      return json({ sdk_version: '0.4.0', active_profile: 'local-owner', supported_profiles: ['local-owner'], capabilities: {} });
    }
    if (workflow && url.includes(`/workflows/${String(workflow.workflow_id)}`)) return json(workflow);
    if (url.includes('/workflows')) return json({ items: [], total: 0, limit: 50, offset: 0, has_more: false });
    return json({});
  });
}

describe('Shell', () => {
  beforeEach(() => {
    window.location.hash = '';
  });
  afterEach(() => {
    vi.restoreAllMocks();
    window.location.hash = '';
  });

  it('renders the page of an extension route', async () => {
    mockService();
    const registry = new StudioExtensionRegistry();
    registry.register(reports);
    window.location.hash = '#/reports';
    render(
      <I18nProvider>
        <ApiProvider>{withRegistry(registry, <Shell />)}</ApiProvider>
      </I18nProvider>,
    );
    expect(await screen.findByTestId('reports-page')).toBeInTheDocument();
    expect(screen.getByTestId('nav-item-reports')).toHaveAttribute('aria-current', 'page');
  });
});

describe('WorkflowDetail', () => {
  afterEach(() => vi.restoreAllMocks());

  it('renders extension panels as tabs after their anchor', async () => {
    mockService({
      workflow_id: 'wf-p28',
      name: 'P28 Workflow',
      revision: 1,
      spec_hash: 'abc',
      spec: { name: 'P28 Workflow', entry_node: 'n1', nodes: { n1: { id: 'n1', handler: 'h' } } },
      created_at: 1,
      updated_at: 1,
    });
    const registry = registerStudioExtensions([notes], new StudioExtensionRegistry());
    render(
      <I18nProvider>
        <ApiProvider>
          {withRegistry(registry, <WorkflowDetail workflowId="wf-p28" onBack={vi.fn()} onOpenWorkflow={vi.fn()} />)}
        </ApiProvider>
      </I18nProvider>,
    );
    const tabs = await screen.findByLabelText('Workflow views');
    const ids = within(tabs)
      .getAllByRole('tab')
      .map((b) => b.getAttribute('data-testid'));
    expect(ids).toEqual(['tab-canvas', 'tab-nodes', 'tab-notes', 'tab-json', 'tab-tests', 'tab-runs']);
    fireEvent.click(screen.getByTestId('tab-notes'));
    expect(screen.getByTestId('tab-notes')).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByTestId('tab-canvas')).toHaveAttribute('aria-selected', 'false');
    expect(await screen.findByTestId('notes-panel')).toHaveTextContent('wf-p28:P28 Workflow');
  });

  it('has no Publish tab without the publish extension', async () => {
    mockService({
      workflow_id: 'wf-bare',
      name: 'Bare',
      revision: 1,
      spec_hash: 'abc',
      spec: { name: 'Bare', entry_node: 'n1', nodes: { n1: { id: 'n1', handler: 'h' } } },
      created_at: 1,
      updated_at: 1,
    });
    render(
      <I18nProvider>
        <ApiProvider>
          {withRegistry(
            new StudioExtensionRegistry(),
            <WorkflowDetail workflowId="wf-bare" onBack={vi.fn()} onOpenWorkflow={vi.fn()} />,
          )}
        </ApiProvider>
      </I18nProvider>,
    );
    await screen.findByLabelText('Workflow views');
    expect(screen.queryByTestId('tab-publish')).toBeNull();
    expect(screen.getByTestId('tab-tests')).toBeInTheDocument();
  });
});
