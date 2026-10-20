// B34 review fixes: the approvals badge after a permission denial, its total beyond one page,
// internal modules in the composite pickers, the run view notifying the badge, localised
// palette categories. Real ApiProvider with mocked fetch; no service, no network.
import { describe, it, expect, vi, afterEach } from 'vitest';
import { act, render, renderHook, screen, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { I18nProvider } from '../../src/i18n';
import { ApiProvider } from '../../src/api/context';
import { usePendingApprovals } from '../../src/features/approvals/usePendingApprovals';
import { ModulePalette } from '../../src/features/modules/ModulePalette';

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

function mockFetch(runs: () => Response, modules: unknown[] = []) {
  return vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
    const url = String(input);
    if (url.includes('/runs')) return runs();
    if (url.includes('/capabilities')) return json(CAPABILITIES);
    if (url.includes('/modules')) return json({ items: modules, modules, total: modules.length });
    return json({ items: [], total: 0, limit: 50, offset: 0, has_more: false });
  });
}

const runsCalls = (spy: ReturnType<typeof mockFetch>) =>
  spy.mock.calls.filter(([u]) => String(u).includes('/runs')).length;

const wrapper = ({ children }: { children: ReactNode }) => (
  <I18nProvider>
    <ApiProvider>{children}</ApiProvider>
  </I18nProvider>
);

afterEach(() => {
  vi.restoreAllMocks();
  localStorage.clear();
});

describe('approvals badge after a permission denial', () => {
  it('clears the count and stops polling once the runs API answers 403', async () => {
    let denied = false;
    const spy = mockFetch(() =>
      denied
        ? json({ detail: { code: 'PERMISSION_DENIED', message: 'no' } }, 403)
        : json({ items: [pendingRun('r1'), pendingRun('r2')], total: 2 }),
    );
    const { result } = renderHook(() => usePendingApprovals({ pollIntervalMs: 25 }), { wrapper });
    await waitFor(() => expect(result.current.count).toBe(2));
    denied = true;
    await act(async () => {
      await result.current.refresh();
    });
    expect(result.current.count).toBe(0);
    expect(result.current.approvals).toEqual([]);
    const after = runsCalls(spy);
    await new Promise((r) => setTimeout(r, 120)); // several poll intervals
    expect(runsCalls(spy)).toBe(after);
  });

  it('counts the server total, not only the first page', async () => {
    const items = Array.from({ length: 100 }, (_, i) => pendingRun(`r${i}`));
    mockFetch(() => json({ items, total: 137, limit: 100, offset: 0, has_more: true }));
    const { result } = renderHook(() => usePendingApprovals(), { wrapper });
    await waitFor(() => expect(result.current.count).toBe(137));
    expect(result.current.approvals).toHaveLength(100);
  });
});

describe('palette categories are localised', () => {
  it('shows the Hindi labels when the locale is hi', async () => {
    localStorage.setItem('nexura_locale', 'hi');
    mockFetch(() => json({ items: [], total: 0 }));
    render(
      <I18nProvider>
        <ApiProvider>
          <ModulePalette open onClose={() => {}} onSelectModule={() => {}} />
        </ApiProvider>
      </I18nProvider>,
    );
    expect((await screen.findByTestId('palette-category-connectors')).textContent).toBe('कनेक्टर');
    expect(screen.getByTestId('palette-category-tools').textContent).toBe('टूल और API');
  });
});
