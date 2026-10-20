// B34 review regressions (promoted from .autopilot/b34/repros) (vitest): service-internal modules and badge refresh outside the main palette.
// Mocked API context; no service, no network.
import { afterEach, describe, expect, it, vi } from 'vitest';
import { createElement } from 'react';
import { render, screen, waitFor, renderHook, act } from '@testing-library/react';

function mod(module_id: string, kind: string, internal: boolean, group: string | null = null) {
  return {
    module_id,
    version: '1.0.0',
    kind,
    title: module_id,
    description: `Description of ${module_id}`,
    is_available: true,
    internal,
    display: group ? { group } : null,
    required_credential_types: [],
    capabilities: {},
  };
}

const MODULES = [
  mod('nexura.data.transform', 'data_transform', false),
  mod('nexura.connectors.slack', 'integration', false, 'connectors'),
  mod('nexura.service.auth', 'service', true),
  mod('nexura.runtime.approvals', 'approval_manager', true),
];

const pendingRun = {
  run_id: 'r1',
  workflow_id: 'wf-1',
  status: 'pending_approval',
  current_node_id: 'gate',
  pending_approval: { approval_id: 'ap-r1', node_id: 'gate' },
  created_at: 1700000000,
  current_state: {},
  step_count: 1,
  events: [],
};

const client = {
  modules: { list: async () => ({ items: MODULES }), get: async () => null },
  workflows: { list: async () => ({ items: [] }), get: async () => ({}) },
  runs: {
    get: async () => pendingRun,
    // Live stream that stays open until the hook aborts it.
    streamEvents: (_id: string, opts: { signal?: AbortSignal }) =>
      (async function* () {
        await new Promise<void>((resolve) => opts.signal?.addEventListener('abort', () => resolve()));
      })(),
    decide: async () => ({ ...pendingRun, status: 'completed', pending_approval: null }),
  },
};

// Stable objects: a fresh manager per render would restart every [manager] effect.
const api = { manager: { client }, connection: { status: 'connected', workspace: 'w' } };
vi.mock('../../src/api/context', () => ({
  useApi: () => api,
}));

import { I18nProvider } from '../../src/i18n';
import { CompositeInspector } from '../../src/features/builder/CompositeInspector';
import { ModulePalette } from '../../src/features/modules/ModulePalette';
import { useRunStream } from '../../src/features/runs/useRunStream';
import { APPROVALS_CHANGED_EVENT } from '../../src/features/approvals/usePendingApprovals';

const base = { nodeId: 'n', label: 'n', handler: '', requires_approval: false, config: {}, input_mapping: {}, output_mapping: {}, isEntry: false };

afterEach(() => {
  vi.restoreAllMocks();
  localStorage.clear();
});

describe('B34 review: internal modules in the composite step pickers', () => {
  it('parallel-branch picker does not offer internal modules as new branches', async () => {
    render(createElement(CompositeInspector, { nodeId: 'n', data: { ...base, parallel: [] } as any, onUpdate: () => {} }));
    await waitFor(() => expect(screen.getByText('nexura.data.transform')).toBeTruthy());
    // Expected: hidden like in the palette. Actual: offered as a checkbox option.
    expect(screen.queryByText('nexura.service.auth')).toBeNull();
    expect(screen.queryByText('nexura.runtime.approvals')).toBeNull();
  });

  it('map-handler select does not offer internal modules', async () => {
    render(createElement(CompositeInspector, { nodeId: 'n', data: { ...base, handler: '', map_over: 'items' } as any, onUpdate: () => {} }));
    await waitFor(() => expect(screen.getByText('nexura.data.transform')).toBeTruthy());
    const sel = document.getElementById('n-map-handler') as HTMLSelectElement;
    const values = Array.from(sel.options).map((o) => o.value);
    expect(values).not.toContain('nexura.service.auth');
    expect(values).not.toContain('nexura.runtime.approvals');
  });
});

describe('B34 review: badge refresh after a decision from the run view', () => {
  it('useRunStream.decideApproval notifies the sidebar badge (APPROVALS_CHANGED_EVENT)', async () => {
    const seen = vi.fn();
    window.addEventListener(APPROVALS_CHANGED_EVENT, seen);
    const { result, unmount } = renderHook(() => useRunStream('r1'));
    await waitFor(() => expect(result.current.run?.pending_approval?.approval_id).toBe('ap-r1'));
    await act(async () => {
      await result.current.decideApproval('approve');
    });
    window.removeEventListener(APPROVALS_CHANGED_EVENT, seen);
    unmount();
    // Expected: the badge is told to refresh. Actual: no event, badge stale for up to 30 s.
    expect(seen).toHaveBeenCalled();
  });
});

describe('B34 review: new palette category labels are translated', () => {
  it('shows Hindi labels for the new/renamed categories when the locale is hi', async () => {
    localStorage.setItem('nexura_locale', 'hi');
    render(
      createElement(I18nProvider, null, createElement(ModulePalette, { open: true, onClose: () => {}, onSelectModule: () => {} }))
    );
    await waitFor(() => expect(screen.getAllByText('nexura.data.transform').length).toBeGreaterThan(0));
    for (const id of ['connectors', 'tools', 'services', 'synthetic']) {
      const label = screen.getByTestId(`palette-category-${id}`).textContent;
      // Expected: a hi.json string. Actual: hard-coded English ("Connectors", "Tools & APIs", ...).
      expect(['Connectors', 'Tools & APIs', 'Governance & Services', 'Local Mock / Legacy'], id).not.toContain(label);
    }
  });
});
