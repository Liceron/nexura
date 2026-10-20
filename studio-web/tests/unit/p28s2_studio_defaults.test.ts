// P28 step 2 review finding 7 (promoted from .autopilot/p28/repros): Studio components rendered
// without mountStudio read the default `studioExtensions` registry. Until EDITIONS_PLAN step 5 it
// held the Organisation page; that page moved to the closed edition, so the default registry is
// now empty and these tests check that nothing of it is left. Mocked fetch, no network.
import { describe, it, expect, vi, afterEach } from 'vitest';
import { createElement } from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import { I18nProvider } from '../../src/i18n';
import { ApiProvider } from '../../src/api/context';
import { Shell } from '../../src/app/Shell';
import { parseHash } from '../../src/app/routes';

function mockService() {
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
    const url = String(input);
    if (url.includes('/capabilities')) {
      return new Response(
        JSON.stringify({
          sdk_version: '0.4.0',
          active_profile: 'local-owner',
          supported_profiles: ['local-owner'],
          capabilities: { modules: true },
        }),
        { status: 200, headers: { 'content-type': 'application/json' } },
      );
    }
    return new Response(
      JSON.stringify({ items: [], total: 0, limit: 50, offset: 0, has_more: false }),
      { status: 200, headers: { 'content-type': 'application/json' } },
    );
  });
}

describe('P28 step 2: Studio defaults without mountStudio', () => {
  afterEach(() => {
    vi.restoreAllMocks();
    window.location.hash = '';
  });

  // The Organisation page moved to the closed edition (EDITIONS_PLAN step 5); the positive
  // versions of these two checks are in enterprise/studio-web/tests/unit.
  it('parseHash no longer maps #/organisation (the page moved to the closed edition)', () => {
    expect(parseHash('#/organisation').tab).toBe('workflows');
  });

  it('Shell lists no Organisation entry in the sidebar', async () => {
    mockService();
    render(createElement(I18nProvider, null, createElement(ApiProvider, null, createElement(Shell))));
    await waitFor(() => expect(screen.getByTestId('nav-item-credentials')).toBeTruthy());
    expect(screen.queryByTestId('nav-item-organisation')).toBeNull();
  });
});

describe('P28 step 2: default Studio extension registry', () => {
  it('holds no in-tree extension: Organisation and Publish moved to the closed edition', async () => {
    const { studioExtensions, createDefaultStudioExtensions } = await import('../../src/app/extensions');
    expect(studioExtensions.list().map((e) => e.id)).toEqual([]);
    expect(studioExtensions.panels('workflow-detail').map((p) => p.id)).toEqual([]);
    expect(studioExtensions.routeTab('#/organisation')).toBeUndefined();
    const fresh = createDefaultStudioExtensions();
    expect(fresh).not.toBe(studioExtensions);
    expect(fresh.list().map((e) => e.id)).toEqual([]);
  });

  it('bootstraps a new registry by default and leaves the default one unchanged', async () => {
    const { studioExtensions } = await import('../../src/app/extensions');
    const { registerStudioExtensions } = await import('../../src/app/bootstrap');
    const reports = { id: 'test.reports', navItems: [{ id: 'reports', labelKey: 'nav.reports', icon: 'R' }] };
    const registry = registerStudioExtensions([reports]);
    expect(registry).not.toBe(studioExtensions);
    expect(registry.list().map((e) => e.id)).toEqual(['test.reports']);
    expect(studioExtensions.list().map((e) => e.id)).toEqual([]);
  });
});
